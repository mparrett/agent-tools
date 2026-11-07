"""SessionFacts: what one Claude main, subagent, or Codex transcript did and where.

Every reading of a transcript goes through the seam's claude-ls primitives;
this module adds only what claude-ls has no reason to compute: the peak (not
cumulative) context, where tools *acted* (D4), pasted session ids, worktrees a
session made, and fork siblings.

Two passes. `iter_sessions` is the cheap one: a bounded head read plus the
seam's tail read, so a store of thousands of transcripts lists in seconds. It
leaves the whole-file facts (`work`, `repos`, `pasted_sessions`, `worktrees`,
`handoff_files`) empty and says so with `complete=False`. `facts` reads the
whole file and fills everything.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import seam
from .clock import Instant, parse_transcript_ts
from .hooklog import shell_unresolved

HEAD_BYTES = 128 * 1024
TAIL_BYTES = 256 * 1024
FORK_WINDOW_S = 60
PROMPTS_KEPT = 12
TOOLS_KEPT = 10
LAST_ASSISTANT_CHARS = 600
TOOL_SUMMARY_CHARS = 120

_SUBAGENT_PREFIX = "agent-"
_EPOCH = datetime.fromtimestamp(0, timezone.utc)


class NotFound(LookupError):
    pass


class Ambiguous(LookupError):
    def __init__(self, prefix: str, matches: list[str]):
        super().__init__(f"{prefix!r} matches {len(matches)} sessions: {', '.join(matches[:5])}")
        self.matches = matches


@dataclass(frozen=True)
class Acted:
    """One anchor this session's own tools acted on: the evidence `sync` turns
    into a ref. Only writes count (a commit, a push, a PR opened, merged,
    reviewed or commented on, an edit inside a worktree), never a read.

    kind "pr": value is `owner/repo#N`, or `#N` when the command named no repo
    (`dir`'s origin then says which). kind "branch": the branch name, its repo
    resolved from `dir`, or from `repo` (the push's remote) when `dir` is gone.
    kind "wt": an absolute worktree path. kind "repo": a GitHub repo
    (`owner/name`) a push went to; never an anchor, only the "acted in this
    repo" half of the tail-mention rule.
    """

    kind: str
    value: str
    how: str
    at: Optional[Instant] = None
    dir: str = ""
    repo: str = ""


@dataclass
class SessionFacts:
    id: str
    kind: str  # "claude" | "subagent" | "codex"
    path: str
    parent_id: Optional[str] = None
    project_dir: Optional[str] = None
    cwd: str = ""
    started: Optional[Instant] = None
    last_seen: Optional[Instant] = None
    origin: Optional[str] = None
    chain: list[str] = field(default_factory=list)
    fork_siblings: list[str] = field(default_factory=list)
    model: str = ""
    peak_context: int = 0
    # Compactions (see _scan_claude for the record shapes). A resume loads the
    # last compaction summary onward, so the context it costs is the peak after that
    # record, not over the whole transcript; with no compaction the two are
    # the same number. None: compacted, no turn since, and no size recorded.
    compaction_count: int = 0
    context_after_compaction: Optional[int] = 0
    work: Optional[seam.WorkSignals] = None
    repos: list[str] = field(default_factory=list)
    # CONTRACT: `gh --repo owner/name` names a GitHub repo, not a path, so it is
    # kept apart from `repos` (canonical local roots) rather than mixed into it.
    gh_repos: list[str] = field(default_factory=list)
    pasted_sessions: list[tuple[str, Instant]] = field(default_factory=list)
    worktrees: list[str] = field(default_factory=list)
    # CONTRACT: addition. The branch a worktree was created or entered on, by
    # path (or by bare `just wt` name when its output named no path): once the
    # directory is gone, this is how a frontier still knows what it held.
    worktree_branches: dict[str, str] = field(default_factory=dict)
    handoff_files: list[str] = field(default_factory=list)
    # CONTRACT: addition. What the session's tools wrote to, for `sync`; full pass only.
    acted: list[Acted] = field(default_factory=list)
    first_prompt: str = ""  # CONTRACT: addition; the fork-sibling key, kept for callers
    human_prompts: list[tuple[Instant, str]] = field(default_factory=list)
    last_assistant: Optional[tuple[Instant, str]] = None
    last_tools: list[tuple[Instant, str, str]] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    # CONTRACT: the three fields below are additions. `agent_meta` is the
    # subagent's .meta.json (agentType, description, name); `complete` tells a
    # caller holding an iter_sessions result that the whole-file fields are
    # unpopulated, not empty; `warnings` follows the degrade-never-fail rule.
    agent_meta: dict = field(default_factory=dict)
    complete: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        if self.kind == "subagent":
            return f"agent:{self.parent_id}/{self.id}"
        if self.kind == "codex":
            return f"codex:{self.id}"
        return f"claude:{self.id}"


# --------------------------------------------------------------------------
# Discovery


@dataclass(frozen=True)
class _Ref:
    id: str
    kind: str
    path: Path
    parent_id: Optional[str] = None
    project_dir: Optional[Path] = None


def _codex_id(path: Path, meta: dict) -> str:
    sid = meta.get("id") or meta.get("session_id")
    if sid:
        return str(sid)
    # rollout-<YYYY-MM-DDTHH-MM-SS>-<uuid>: the uuid is the last five dash groups.
    parts = path.stem.rsplit("-", 5)
    return "-".join(parts[-5:]) if len(parts) >= 6 else path.stem


def _claude_refs() -> Iterator[_Ref]:
    root = seam.projects_dir()
    if not root.is_dir():
        return
    for proj in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in proj.glob("*.jsonl"):
            yield _Ref(f.stem, "claude", f, None, proj)
        for f in proj.glob(f"*/subagents/{_SUBAGENT_PREFIX}*.jsonl"):
            yield _Ref(f.stem[len(_SUBAGENT_PREFIX) :], "subagent", f, f.parent.parent.name, proj)


def _codex_refs() -> Iterator[_Ref]:
    for r in seam.iter_codex_session_refs(per_project=None, include_subagents=True):
        meta = seam.codex_session_meta(r.path) or {}
        parent = meta.get("parent_thread_id") or None
        yield _Ref(
            _codex_id(r.path, meta), "codex", r.path, parent, seam.projects_dir() / r.project_key
        )


# Per-invocation memo, set and cleared by cli.main (like frontier's history
# cache). One `show` resolves every member and lists recent sessions; without
# it each of those re-globbed the whole store and re-read every Codex
# rollout's metadata. None outside a CLI invocation, so library callers and
# tests that add transcripts between calls always see the store as it is.
_snapshot: Optional[dict] = None


def _all_refs(include_codex: bool = True) -> list[_Ref]:
    key = ("refs", include_codex)
    if _snapshot is not None and key in _snapshot:
        return _snapshot[key]
    refs = list(_claude_refs())
    if include_codex:
        refs.extend(_codex_refs())
    if _snapshot is not None:
        _snapshot[key] = refs
    return refs


def known_session_ids(include_codex: bool = True) -> set[str]:
    return {r.id for r in _all_refs(include_codex)}


def _match(prefix: str, refs: list[_Ref]) -> list[_Ref]:
    p = prefix.strip().lower()
    for scheme in ("claude:", "codex:", "agent:"):
        if p.startswith(scheme):
            p = p[len(scheme) :].rsplit("/", 1)[-1]
    if p.startswith(_SUBAGENT_PREFIX):
        p = p[len(_SUBAGENT_PREFIX) :]
    if not p:
        return []
    exact = [r for r in refs if r.id.lower() == p]
    if exact:
        return exact
    return [r for r in refs if r.id.lower().startswith(p)]


def _resolve_ref(prefix: str) -> _Ref:
    hits = _match(prefix, _all_refs())
    if not hits:
        raise NotFound(prefix)
    ids = sorted({r.id for r in hits})
    if len(ids) > 1:
        raise Ambiguous(prefix, ids)
    return hits[0]


def resolve(prefix: str) -> str:
    return _resolve_ref(prefix).id


# --------------------------------------------------------------------------
# Reading


def _head_entries(path: Path, nbytes: int = HEAD_BYTES) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(nbytes)
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(data) == nbytes:
        lines = lines[:-1]  # likely cut mid-record
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _cheap_entries(path: Path) -> list[dict]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    # Head and tail would overlap and double-count; read a small file once.
    if size <= HEAD_BYTES + TAIL_BYTES:
        return _full_entries(path)
    return _head_entries(path) + seam.tail_entries(path, TAIL_BYTES)


def _full_entries(path: Path) -> list[dict]:
    try:
        return seam.load_raw_entries(str(path))
    except OSError:
        return []


def _instant(ts, hint=None) -> Optional[Instant]:
    return parse_transcript_ts(ts, hint) if isinstance(ts, str) and ts else None


def _one_line(text: str, limit: int = TOOL_SUMMARY_CHARS) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _tool_summary(name: str, tin: dict) -> str:
    for key in (
        "command",
        "file_path",
        "notebook_path",
        "path",
        "pattern",
        "description",
        "url",
        "query",
    ):
        v = tin.get(key)
        if isinstance(v, str) and v:
            return _one_line(v)
        if isinstance(v, list) and v:
            return _one_line(" ".join(map(str, v)))
    for v in tin.values():
        if isinstance(v, str) and v:
            return _one_line(v)
    return ""


@dataclass
class _Tool:
    at: Optional[Instant]
    name: str
    input: dict
    command: str  # shell text, "" for non-shell tools
    cwd: str
    call_id: str = ""


@dataclass
class _Scan:
    """One pass over records, normalized across Claude and Codex shapes."""

    prompts: list[tuple[Instant, str]] = field(default_factory=list)
    first_prompt: str = ""
    last_assistant: Optional[tuple[Instant, str]] = None
    tools: list[_Tool] = field(default_factory=list)
    results: dict[str, tuple[str, dict]] = field(default_factory=dict)  # call id -> (text, extra)
    peak_context: int = 0
    compaction_count: int = 0
    context_after_compaction: Optional[int] = 0
    first_ts: Optional[Instant] = None
    last_ts: Optional[Instant] = None
    cwd: str = ""


def _track_ts(scan: _Scan, at: Optional[Instant]) -> None:
    if at is None:
        return
    if scan.first_ts is None or at.utc < scan.first_ts.utc:
        scan.first_ts = at
    if scan.last_ts is None or at.utc > scan.last_ts.utc:
        scan.last_ts = at


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def _scan_claude(entries: list[dict], is_subagent: bool, hint=None) -> _Scan:
    scan = _Scan()
    # Claude Code writes a compaction as a `compact_boundary` system record
    # followed by an `isCompactSummary` user record; claude-ls counts the
    # former. Either one starts the post-compaction context, and a summary
    # right after its boundary is the same compaction, not a second one. The
    # boundary's `postTokens` is the context the compaction left, which stands
    # in until an assistant turn reports its own usage.
    after_boundary = False
    for e in entries:
        at = _instant(e.get("timestamp"), hint)
        _track_ts(scan, at)
        rec_cwd = e.get("cwd") or scan.cwd
        if e.get("cwd") and not scan.cwd:
            scan.cwd = e["cwd"]
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "compact_boundary":
            scan.compaction_count += 1
            post = (e.get("compactMetadata") or {}).get("postTokens")
            scan.context_after_compaction = post if isinstance(post, int) else None
            after_boundary = True
            continue
        if e.get("isCompactSummary"):
            if not after_boundary:
                scan.compaction_count += 1
                scan.context_after_compaction = None
            after_boundary = False
        if kind == "assistant":
            after_boundary = False
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if kind == "assistant":
            usage = msg.get("usage") or {}
            ctx = sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            scan.peak_context = max(scan.peak_context, ctx)
            scan.context_after_compaction = max(scan.context_after_compaction or 0, ctx)
            if not isinstance(content, list):
                continue
            texts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                    texts.append(b["text"].strip())
                elif b.get("type") == "tool_use":
                    tin = b.get("input") if isinstance(b.get("input"), dict) else {}
                    name = b.get("name", "")
                    cmd = tin.get("command", "") if name == "Bash" else ""
                    scan.tools.append(
                        _Tool(
                            at,
                            name,
                            tin,
                            cmd if isinstance(cmd, str) else "",
                            rec_cwd,
                            b.get("id", ""),
                        )
                    )
            if texts and at is not None:
                scan.last_assistant = (at, texts[-1][:LAST_ASSISTANT_CHARS])
        elif kind == "user":
            if isinstance(content, list):
                extra = e.get("toolUseResult") if isinstance(e.get("toolUseResult"), dict) else {}
                for b in content:
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "tool_result"
                        and b.get("tool_use_id")
                    ):
                        scan.results[b["tool_use_id"]] = (_result_text(b.get("content")), extra)
            # Every record in a subagent transcript is a sidechain; its prompts
            # are the delegation it was given, which is what it was asked to do.
            if (
                e.get("isMeta")
                or e.get("isCompactSummary")
                or (e.get("isSidechain") and not is_subagent)
            ):
                continue
            text = seam.user_prompt_text(e)
            if text and at is not None:
                scan.first_prompt = scan.first_prompt or text
                scan.prompts.append((at, text))
    return scan


def _codex_command(args: dict) -> str:
    cmd = args.get("cmd") or args.get("command") or ""
    if isinstance(cmd, list):
        # ["bash", "-lc", "<script>"]: the script is what ran.
        if (
            len(cmd) >= 3
            and os.path.basename(str(cmd[0])) in ("bash", "sh", "zsh")
            and cmd[1].startswith("-")
        ):
            return str(cmd[-1])
        return " ".join(map(str, cmd))
    return cmd if isinstance(cmd, str) else ""


def _scan_codex(entries: list[dict], hint=None) -> _Scan:
    scan = _Scan()
    for e in entries:
        at = _instant(e.get("timestamp"), hint)
        _track_ts(scan, at)
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        etype, ptype = e.get("type"), p.get("type")
        if etype == "session_meta":
            scan.cwd = scan.cwd or p.get("cwd", "")
        elif etype == "turn_context" and p.get("cwd"):
            scan.cwd = scan.cwd or p["cwd"]
        if ptype == "user_message":
            text = (p.get("message") or "").strip() if isinstance(p.get("message"), str) else ""
            if text and not seam.is_wrapper_text(text) and at is not None:
                scan.first_prompt = scan.first_prompt or text
                scan.prompts.append((at, text))
        elif ptype == "agent_message":
            text = p.get("message")
            if isinstance(text, str) and text.strip() and at is not None:
                scan.last_assistant = (at, text.strip()[:LAST_ASSISTANT_CHARS])
        elif ptype == "token_count":
            # last_token_usage is per turn; Codex's input_tokens already includes the cached part.
            usage = (p.get("info") or {}).get("last_token_usage") or {}
            scan.peak_context = max(scan.peak_context, int(usage.get("input_tokens") or 0))
            # Codex compaction is not read here, so its whole rollout counts.
            scan.context_after_compaction = scan.peak_context
        elif ptype in ("function_call", "custom_tool_call"):
            raw = p.get("arguments") if ptype == "function_call" else None
            args: dict = {}
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    args = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    args = {"command": raw}
            elif ptype == "custom_tool_call" and isinstance(p.get("input"), str):
                args = {"input": p["input"]}
            cmd = _codex_command(args) if ptype == "function_call" else ""
            cwd = args.get("workdir") or scan.cwd
            scan.tools.append(_Tool(at, p.get("name", ""), args, cmd, cwd, p.get("call_id", "")))
        elif ptype in ("function_call_output", "custom_tool_call_output") and p.get("call_id"):
            out = p.get("output")
            scan.results[p["call_id"]] = (out if isinstance(out, str) else json.dumps(out), {})
    return scan


# --------------------------------------------------------------------------
# Location evidence (D4): where tools acted, not where the session launched

_PATH_TOK = r"(\"[^\"]+\"|'[^']+'|[^\s;&|()]+)"
_PATH_PATTERNS = [
    re.compile(r"\bgit\s+-C\s+" + _PATH_TOK),
    re.compile(r"(?:^|&&|\|\||;|\n|\()\s*cd\s+" + _PATH_TOK + r"\s*(?:&&|;)"),
    re.compile(r"\buv\s+run\b[^\n;&|]*?--directory(?:=|\s+)" + _PATH_TOK),
]
_GH_REPO_RE = re.compile(r"\bgh\s[^\n;&|]*?(?:--repo(?:=|\s+)|-R\s+)[\"']?([\w.-]+/[\w.-]+)")
_WT_ADD_RE = re.compile(r"\bgit\s+(?:-C\s+" + _PATH_TOK + r"\s+)?worktree\s+add\s+([^\n;&|]+)")
_WT_NAMED_RE = re.compile(
    r"(?:\bjust\s+wt|(?:^|&&|;|\n)\s*wt\s+new)\s+(?:-\S+\s+)*([^\s;&|-][^\s;&|]*)"
)
_CREATED_WT_RE = re.compile(r"Created worktree at [“\"']([^”\"']+)[”\"']")
_ABS_PATH_RE = re.compile(r"(?:^|[\s“\"'(=])((?:/|~/)[^\s”\"')]+)")
# Values to `git worktree add` options that take an argument.
_WT_ADD_OPTS_WITH_ARG = {"-b", "-B", "--reason"}


def _unquote(tok: str) -> str:
    return tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok


def _abs_path(raw: str, base: str) -> Optional[str]:
    p = os.path.expandvars(os.path.expanduser(_unquote(raw)))
    if not p or shell_unresolved(p):
        return None
    if not os.path.isabs(p):
        if not base:
            return None
        p = os.path.join(base, p)
    p = os.path.normpath(p)
    # The base can carry the problem too: a cwd of `<repo>/~/x` came from an earlier misread.
    return None if shell_unresolved(p) else p


def _canonical(path: str) -> Optional[str]:
    return seam.canonical_repo_root(path) or seam.repo_root_for(path)


def _command_paths(cmd: str, cwd: str) -> list[str]:
    out = []
    for pat in _PATH_PATTERNS:
        for m in pat.finditer(cmd):
            p = _abs_path(m.group(1), cwd)
            if p:
                out.append(p)
    return out


def _worktree_add_path(m: re.Match, cwd: str) -> Optional[str]:
    base = _abs_path(m.group(1), cwd) if m.group(1) else cwd
    try:
        args = shlex.split(m.group(2))
    except ValueError:
        args = m.group(2).split()
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in _WT_ADD_OPTS_WITH_ARG:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return _abs_path(a, base or "")
    return None


def _named_worktree(name: str, result: str) -> str:
    """`just wt <name>` / `wt new <name>` print the path they made; fall back to
    the bare name when the output is missing or names no matching path."""
    leaf = name.rstrip("/").replace("/", "-")
    for m in _ABS_PATH_RE.finditer(result or ""):
        cand = os.path.expanduser(m.group(1).rstrip(".,:;"))
        if leaf and leaf in os.path.basename(cand.rstrip("/")):
            return os.path.normpath(cand)
    return name


def _add_unique(seq: list, item) -> None:
    if item and item not in seq:
        seq.append(item)


def _locations(
    scan: _Scan, edited_paths: Iterable[str]
) -> tuple[list[str], list[str], list[tuple[str, Optional[Instant], str]], dict[str, str]]:
    """(repos, gh_repos, worktrees, worktree_branches) from the tool stream
    and edited paths. Each worktree comes as (path, when, how), first sighting
    only: `worktrees` keeps the paths for ranking, and `sync` takes the
    absolute ones as evidence. `worktree_branches` maps a worktree to the
    branch its creation named, where the creation said."""
    repos: list[str] = []
    gh_repos: list[str] = []
    worktrees: list[tuple[str, Optional[Instant], str]] = []
    branches: dict[str, str] = {}
    seen: set[str] = set()

    def wt(
        path: Optional[str], at: Optional[Instant], how: str, branch: Optional[str] = None
    ) -> None:
        if path and path not in seen:
            seen.add(path)
            worktrees.append((path, at, how))
        if path and isinstance(branch, str) and valid_branch(branch):
            branches.setdefault(path, branch)

    for fp in edited_paths:
        _add_unique(repos, _canonical(fp))
    for t in scan.tools:
        result, extra = scan.results.get(t.call_id, ("", {}))
        if t.command:
            for p in _command_paths(t.command, t.cwd):
                _add_unique(repos, _canonical(p))
            for m in _GH_REPO_RE.finditer(t.command):
                _add_unique(gh_repos, m.group(1))
            for m in _WT_ADD_RE.finditer(t.command):
                wt(_worktree_add_path(m, t.cwd), t.at, "git worktree add")
            for m in _WT_NAMED_RE.finditer(t.command):
                # `just wt <branch>` names the branch; `wt new`'s argument is not known to be one.
                branch = m.group(1) if m.group(0).lstrip().startswith("just") else None
                wt(_named_worktree(m.group(1), result), t.at, "created the worktree", branch)
        if t.name == "EnterWorktree":
            # Created: the path is only in the result. Entered an existing one: it is the input.
            m = _CREATED_WT_RE.search(result or "")
            p = (
                (m.group(1) if m else None)
                or (extra or {}).get("worktreePath")
                or t.input.get("path")
            )
            if isinstance(p, str):
                wt(
                    _abs_path(p, t.cwd) or p,
                    t.at,
                    "EnterWorktree",
                    (extra or {}).get("worktreeBranch"),
                )
    return repos, gh_repos, worktrees, branches


# --------------------------------------------------------------------------
# Acted-on anchors (sync's evidence): writes only, never reads or mentions

# `gh pr <verb>` verbs that change the PR or claim it; view/list/diff/checks read.
_GH_PR_WRITE_VERBS = {
    "create",
    "merge",
    "review",
    "edit",
    "comment",
    "ready",
    "close",
    "reopen",
    "checkout",
}
# gh pr options that take a value, so the value is not mistaken for the PR.
_GH_PR_VALUE_OPTS = {
    "-R",
    "--repo",
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-t",
    "--title",
    "-B",
    "--base",
    "-H",
    "--head",
    "-a",
    "--assignee",
    "-l",
    "--label",
    "-r",
    "--reviewer",
    "-m",
    "--milestone",
    "-p",
    "--project",
    "-T",
    "--template",
    "--subject",
    "-A",
    "--author-email",
    "--match-head-commit",
    "--add-label",
    "--remove-label",
    "--add-reviewer",
    "--remove-reviewer",
    "--add-assignee",
    "--remove-assignee",
    "--add-project",
    "--remove-project",
    "--recover",
    "--branch",
}
_GH_API_PULL_RE = re.compile(r"^/?repos/([^/\s]+)/([^/\s]+)/pulls/(\d+)(?:/(\w+))?")
_GH_API_WRITE_OPTS = {"-f", "-F", "--field", "--raw-field", "--input"}
_GIT_VALUE_GLOBALS = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_GIT_PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
_PR_NUM_ARG_RE = re.compile(r"^#?(\d+)$")
_REPO_HASH_NUM_RE = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d+)(?!\d)")
_BARE_HASH_NUM_RE = re.compile(r"(?<![\w/])#(\d+)(?!\d)")
# `[branch sha] subject`, `[branch (root-commit) sha] subject`; a detached
# commit prints `[detached HEAD sha]`, which names no branch and does not match.
_COMMIT_LINE_RE = re.compile(r"^\[([^\s\]]+)(?: \([^)]*\))? [0-9a-f]{7,40}\]", re.M)
# git push's per-ref status lines: ` * [new branch]  a -> a`, `   1a..2b  a -> a`.
_PUSH_LINE_RE = re.compile(
    r"^\s*[+*=!-]?\s*(\[[^\]]+\]|[0-9a-f]+\.\.\.?[0-9a-f]+)\s+(\S+)\s+->\s+(\S+)", re.M
)
# Any per-ref status line, arrow or not (` - [deleted]   feat` has none).
_PUSH_ANY_STATUS_RE = re.compile(r"^\s*[+*=!-]?\s*\[[^\]]+\]\s+\S", re.M)
_PUSH_TO_RE = re.compile(r"^To\s+(\S+)", re.M)
# git's own failure lines; a push that printed one and no status line pushed nothing.
_PUSH_FAILED_RE = re.compile(r"^(?:fatal|error|hint):", re.M)
# What `push -u` prints last when it worked; the one line `| tail -1` keeps.
_PUSH_TRACKING_RE = re.compile(r"^branch '[^']+' set up to track", re.M | re.I)
# A pipe into one of these passes all of the output through.
_PASS_THROUGH = {"tee", "cat"}
# Default branches name the repo, not a piece of work in it.
_NOT_WORK_BRANCHES = {"main", "master", "HEAD", "trunk", "develop"}

# Every name that becomes a ref passes one of these, so whatever a parser
# misreads (a `2>`, a quote, a fragment of a heredoc) is dropped, not anchored.
# Branch: the subset of `git check-ref-format --branch` that real branches use.
_BRANCH_OK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def valid_branch(name: str) -> bool:
    return (
        bool(_BRANCH_OK_RE.match(name))
        and ".." not in name
        and "//" not in name
        and "/." not in name
        and not name.endswith((".lock", "/", "."))
    )


def valid_repo(slug: str) -> bool:
    return bool(REPO_SLUG_RE.match(slug)) and not any(
        part in (".", "..") for part in slug.split("/")
    )


# --- shell: one simple command at a time -------------------------------------

# `<<EOF`, `<<-'EOF'`, `<< "EOF"`; not the here-string `<<<`.
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][\w-]*)\2")
_REDIRECT_ALONE_RE = re.compile(r"^\d*(?:>>?|<|>&|<&|&>>?)$")
_REDIRECT_RE = re.compile(r"^\d*(?:>>?|<|&>)")
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")


def strip_heredocs(cmd: str) -> str:
    """The command with every heredoc body removed. A body is file content
    (`cat > notes.md <<'EOF' ... git push origin x ... EOF`), not commands."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in cmd.split("\n"):
        if pending:
            delim, dash = pending[0]
            if (line.lstrip("\t") if dash else line).rstrip() == delim:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend((m.group(3), bool(m.group(1))) for m in _HEREDOC_RE.finditer(line))
    return "\n".join(out)


def _split_segments(text: str) -> list[str]:
    """Split on `;`, `&&`, `||`, `|`, `&`, newlines and parens outside quotes.
    `2>&1` and `&>` are redirections, not separators. Each segment comes with
    the separator after it, so a caller can tell `a | b` from `a || b`."""
    segs: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] != "\n":  # a line continuation joins, nothing else
                buf += [c, text[i + 1]]
            i += 2
            continue
        if c in "'\"":
            quote = c
        elif c == "#" and (not buf or buf[-1].isspace()):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c in ";\n|()" or (
            c == "&" and not (buf and buf[-1] in "<>") and text[i + 1 : i + 2] != ">"
        ):
            sep = c
            if c in "|&" and text[i + 1 : i + 2] == c:
                sep = c * 2
            elif c == "|" and text[i + 1 : i + 2] == "&":
                sep = "|"  # `|&` pipes stderr too
            segs.append(("".join(buf), sep))
            buf = []
            i += len(sep) if sep in ("||", "&&") else (2 if text[i : i + 2] == "|&" else 1)
            continue
        buf.append(c)
        i += 1
    segs.append(("".join(buf), ""))
    return [x for x in segs if x[0].strip()]


# Words that run the command after them: shell keywords and wrappers.
_PREFIX_WORDS = {"do", "then", "else", "elif", "!", "time", "command", "exec", "nohup", "builtin"}
# Wrapper options that take a value (`timeout -s KILL 60 git ...`, `env -u X git ...`).
_TIMEOUT_VALUE_OPTS = {"-s", "--signal", "-k", "--kill-after"}
_ENV_VALUE_OPTS = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
_GH_GLOBAL_VALUE_OPTS = {"-R", "--repo", "--hostname"}


def _skip_opts(words: list[str], value_opts: set[str]) -> list[str]:
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        i += 2 if words[i] in value_opts else 1
    return words[i:]


def _strip_prefixes(words: list[str]) -> list[str]:
    """The command a segment runs, under any `do`, `!`, `time`, `timeout N`,
    `env VAR=x` and the like, with gh's global `--repo X` moved after the
    subcommand where `gh pr` parsing finds it (`gh --repo o/r pr merge 31`)."""
    while words:
        head = os.path.basename(words[0])
        if words[0] in _PREFIX_WORDS:
            words = words[1:]
        elif _ASSIGN_RE.match(words[0]):
            words = words[1:]
        elif head == "timeout":
            words = _skip_opts(words[1:], _TIMEOUT_VALUE_OPTS)[1:]  # then the duration
        elif head == "env":
            words = _skip_opts(words[1:], _ENV_VALUE_OPTS)
        else:
            break
    if words and os.path.basename(words[0]) == "gh":
        moved: list[str] = []
        i = 1
        while i < len(words) and words[i].startswith("-"):
            opt, _, val = words[i].partition("=")
            if opt not in _GH_GLOBAL_VALUE_OPTS:
                break
            pair = [opt, val] if val else words[i : i + 2]
            i += 1 if val else 2
            if opt != "--hostname":
                moved += pair
        words = [words[0], *words[i:], *moved]
    return words


def _segments(cmd: str) -> list[tuple[list[str], bool]]:
    """(words, filtered) for each simple command in `cmd`: heredoc bodies
    stripped, redirections dropped, wrappers and leading `VAR=value`
    assignments removed (`_strip_prefixes`). `filtered` is true when its
    output was piped into something that can cut it (`| tail -1`), so the
    output a tool result shows is not all the command printed. A segment
    shlex cannot parse is left out rather than guessed at."""
    parsed: list[tuple[list[str], str]] = []
    for seg, sep in _split_segments(strip_heredocs(cmd)):
        try:
            toks = shlex.split(seg)
        except ValueError:
            parsed.append(([], sep))
            continue
        words: list[str] = []
        skip = False
        for t in toks:
            if skip:
                skip = False
            elif _REDIRECT_ALONE_RE.match(t):
                skip = True
            elif not _REDIRECT_RE.match(t):
                words.append(t)
        parsed.append((_strip_prefixes(words), sep))
    out = []
    for i, (words, sep) in enumerate(parsed):
        if not words:
            continue
        nxt = parsed[i + 1][0] if i + 1 < len(parsed) else []
        filtered = sep == "|" and not (nxt and os.path.basename(nxt[0]) in _PASS_THROUGH)
        out.append((words, filtered))
    return out


def shell_segments(cmd: str) -> list[list[str]]:
    """Each simple command in `cmd` as its words (see `_segments`)."""
    return [words for words, _ in _segments(cmd)]


def _positionals(args: list[str], value_opts: set[str]) -> list[str]:
    out = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in value_opts:
            skip = True
        elif not a.startswith("-") or a == "-":
            out.append(a)
    return out


def linked_worktree_root(path: str) -> Optional[str]:
    """The root of the linked worktree holding `path`, or None for a main
    checkout, a non-repo, or a path that is gone."""
    root = seam.repo_root_for(path)
    if not root:
        return None
    canon = seam.canonical_repo_root(path)
    return root if canon and os.path.realpath(canon) != os.path.realpath(root) else None


def _gh_repo_flag(args: list[str]) -> str:
    for i, a in enumerate(args):
        if a in ("--repo", "-R") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--repo="):
            return a.split("=", 1)[1]
    return ""


def _pr(slug: str, num: str) -> list[str]:
    """`owner/repo#N`, `#N` when no repo was named, nothing for a malformed repo."""
    if slug and not valid_repo(slug):
        return []
    return [f"{slug}#{num}"]


def _gh_pr_acts(verb: str, args: list[str], result: str) -> list[str]:
    """`owner/repo#N` or `#N` for the PR a `gh pr <verb>` acted on."""
    repo = _gh_repo_flag(args)
    if repo and not valid_repo(repo):
        return []
    target = _positionals(args, _GH_PR_VALUE_OPTS)[:1]
    for a in target:
        m = seam.PR_URL_RE.search(a)
        if m:
            return _pr(f"{m.group(1)}/{m.group(2)}", m.group(3))
        m = _REPO_HASH_NUM_RE.fullmatch(a)
        if m:
            return _pr(m.group(1), m.group(2))
        m = _PR_NUM_ARG_RE.match(a)
        if m:
            return _pr(repo, m.group(1))
        break  # a branch name: gh resolved it to a PR, and its output says which
    # No PR named: gh acted on the current branch's PR, and its output says which.
    m = seam.PR_URL_RE.search(result or "")
    if m:
        return _pr(f"{m.group(1)}/{m.group(2)}", m.group(3))
    if verb == "create":
        return []  # a create that printed no URL failed
    m = _REPO_HASH_NUM_RE.search(result or "")
    if m:
        return _pr(m.group(1), m.group(2))
    m = _BARE_HASH_NUM_RE.search(result or "")
    return _pr(repo, m.group(1)) if m else []


def _gh_api_pr(args: list[str]) -> Optional[tuple[str, str]]:
    """(`owner/repo#N`, how) for a gh api call that writes to a PR, else None."""
    write = False
    target = None
    for i, a in enumerate(args):
        if a in _GH_API_WRITE_OPTS or a.split("=", 1)[0] in _GH_API_WRITE_OPTS:
            write = True
        method = None
        if a in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1]
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:]
        elif a.startswith("--method="):
            method = a.split("=", 1)[1]
        if method and method.upper() in ("POST", "PUT", "PATCH"):
            write = True
        m = _GH_API_PULL_RE.match(a)
        if m and target is None:
            target = m
    if not (write and target):
        return None
    slug = f"{target.group(1)}/{target.group(2)}"
    if not valid_repo(slug):
        return None
    sub = f"/{target.group(4)}" if target.group(4) else ""
    return f"{slug}#{target.group(3)}", f"gh api pulls/{target.group(3)}{sub}"


def _git_call(words: list[str], cur: str) -> Optional[tuple[str, list[str], str]]:
    """(verb, args, dir) for a `git` command; `-C` sets the dir it acted in."""
    if os.path.basename(words[0]) != "git":
        return None
    i, where = 1, cur
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "-C" and i + 1 < len(words):
            where = _abs_path(words[i + 1], where) or where
            i += 2
        elif words[i] in _GIT_VALUE_GLOBALS:
            i += 2
        else:
            i += 1
    if i >= len(words):
        return None
    return words[i], words[i + 1 :], where


def _pushed_branches(args: list[str], result: str, filtered: bool = False) -> list[str]:
    """Branches a push landed on the remote. A rejected ref, a deleted one and
    a tag are not work pushed; nor is anything when the push printed status
    lines and none was accepted. `filtered`: the output went through a pipe
    that may have cut it, so only empty output or the tracking message left
    by `-u` lets the refspec stand; anything else is inconclusive."""
    out = []
    saw_status = False
    for m in _PUSH_LINE_RE.finditer(result or ""):
        saw_status = True
        status, dst = m.group(1), m.group(3)
        if any(x in status for x in ("rejected", "tag", "deleted")):
            continue
        out.append(dst.removeprefix("refs/heads/"))
    saw_status = saw_status or bool(_PUSH_ANY_STATUS_RE.search(result or ""))
    if saw_status or _PUSH_FAILED_RE.search(result or ""):
        return out
    if filtered and (result or "").strip() and not _PUSH_TRACKING_RE.search(result):
        return []
    # Quiet push (`-q`, or output cut by `| tail`): the refspecs on the command
    # line are all there is. `--delete` and `:dst` remove a branch, not push one.
    if "--delete" in args or "-d" in args or "--tags" in args:
        return []
    for spec in _positionals(args, _GIT_PUSH_VALUE_OPTS)[1:]:
        if spec.startswith(":"):
            continue
        out.append(spec.removeprefix("+").rsplit(":", 1)[-1].removeprefix("refs/heads/"))
    return out


def _worktree_acts(hits: Iterable[tuple[str, Optional[Instant], str]]) -> list[Acted]:
    """Worktrees created or entered, from `_locations`; bare `just wt` names are not paths."""
    return [Acted("wt", p, how, at, p) for p, at, how in hits if os.path.isabs(p)]


def _segment_acts(
    words: list[str], cur: str, result: str, at, filtered: bool = False
) -> list[Acted]:
    out: list[Acted] = []
    git = _git_call(words, cur)
    if git:
        verb, args, where = git
        wt = linked_worktree_root(where) if where else None
        if verb == "commit":
            for m in _COMMIT_LINE_RE.finditer(result or ""):
                out.append(Acted("branch", m.group(1), "git commit", at, where))
                if wt:
                    out.append(Acted("wt", wt, "git commit", at, wt))
        elif verb == "push":
            to = _PUSH_TO_RE.search(result or "")
            remote = to.group(1) if to else ""
            branches = _pushed_branches(args, result, filtered)
            out += [Acted("branch", b, "git push", at, where, remote) for b in branches]
            if branches and remote:
                out.append(Acted("repo", remote, "git push", at, where))
            if branches and wt:
                out.append(Acted("wt", wt, "git push", at, wt))
        return out
    if os.path.basename(words[0]) != "gh" or len(words) < 3:
        return out
    if words[1] == "pr" and words[2] in _GH_PR_WRITE_VERBS:
        for pr in _gh_pr_acts(words[2], words[3:], result):
            out.append(Acted("pr", pr, f"gh pr {words[2]}", at, cur))
    elif words[1] == "api":
        hit = _gh_api_pr(words[2:])
        if hit:
            out.append(Acted("pr", hit[0], hit[1], at, cur))
    return out


def _acted(scan: _Scan) -> list[Acted]:
    """Writes inside a worktree, commits, pushes and PR actions, read one
    simple command at a time. Worktrees created or entered come from
    `_locations` (see `_worktree_acts`)."""
    out: list[Acted] = []
    for t in scan.tools:
        result, _ = scan.results.get(t.call_id, ("", {}))
        if t.name in seam.EDIT_TOOLS:
            fp = t.input.get("file_path") or t.input.get("notebook_path")
            if isinstance(fp, str) and fp:
                wt = linked_worktree_root(fp)
                if wt:
                    out.append(Acted("wt", wt, f"edited {os.path.basename(fp)}", t.at, wt))
            continue
        if not t.command:
            continue
        cur = t.cwd
        for words, filtered in _segments(t.command):
            if words[0] == "cd":
                cur = (_abs_path(words[1], cur) if len(words) > 1 else None) or cur
                continue
            out += _segment_acts(words, cur, result, t.at, filtered)
    return [a for a in out if _plausible(a)]


def _plausible(a: Acted) -> bool:
    if a.kind == "branch":
        return valid_branch(a.value) and a.value not in _NOT_WORK_BRANCHES
    if a.kind == "pr":
        slug, _, num = a.value.partition("#")
        return num.isdigit() and (not slug or valid_repo(slug))
    return True


# --------------------------------------------------------------------------
# Pasted session ids

# A uuid, or a hex run of 8+ that may continue into uuid groups. Bounded so a
# hex run inside a longer word (a sha in a URL path segment still counts) is
# not split mid-token.
_HEX_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{8,}(?:-[0-9a-f]+){0,4})(?![0-9A-Za-z])")


def _pasted(
    prompts: list[tuple[Instant, str]], known: set[str], own: set[str]
) -> list[tuple[str, Instant]]:
    by_lower = {k.lower(): k for k in known}
    keys = sorted(by_lower)
    out: list[tuple[str, Instant]] = []
    seen: set[str] = set()
    for at, text in prompts:
        for m in _HEX_TOKEN_RE.finditer(text.lower()):
            tok = m.group(1)
            if tok in by_lower:
                hit = [tok]
            else:
                hit = [k for k in keys if k.startswith(tok)]
            # Unresolvable or ambiguous: a sha, a hash, or a typo, not a session.
            if len(hit) != 1:
                continue
            sid = by_lower[hit[0]]
            if sid in own or sid in seen:
                continue
            seen.add(sid)
            out.append((sid, at))
    return out


# --------------------------------------------------------------------------
# Assembly


def _subagent_ids(project_dir: Path, sid: str) -> list[str]:
    d = project_dir / sid / "subagents"
    if not d.is_dir():
        return []
    return sorted(f.stem[len(_SUBAGENT_PREFIX) :] for f in d.glob(f"{_SUBAGENT_PREFIX}*.jsonl"))


def _agent_meta(path: Path) -> dict:
    meta = path.with_name(path.stem + ".meta.json")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _build(ref: _Ref, entries: list[dict], full: bool, log=None) -> SessionFacts:
    is_codex = ref.kind == "codex"
    hint = log.timeline if log is not None else None
    scan = (
        _scan_codex(entries, hint)
        if is_codex
        else _scan_claude(entries, ref.kind == "subagent", hint)
    )
    meta = seam.extract_session_metadata(entries) if entries else None
    sf = SessionFacts(
        id=ref.id,
        kind=ref.kind,
        path=str(ref.path),
        parent_id=ref.parent_id,
        project_dir=str(ref.project_dir) if ref.project_dir else None,
        cwd=scan.cwd or (meta.cwd if meta else ""),
        started=scan.first_ts,
        last_seen=scan.last_ts,
        model=meta.model if meta else "",
        peak_context=scan.peak_context,
        compaction_count=scan.compaction_count,
        context_after_compaction=scan.context_after_compaction,
        first_prompt=scan.first_prompt,
        human_prompts=scan.prompts[-PROMPTS_KEPT:],
        last_assistant=scan.last_assistant,
        last_tools=[(t.at, t.name, _tool_summary(t.name, t.input)) for t in scan.tools if t.at][
            -TOOLS_KEPT:
        ],
        complete=full,
    )
    if ref.kind == "claude":
        sf.origin = seam.session_origin(ref.path)
        if ref.project_dir:
            sf.subagents = _subagent_ids(ref.project_dir, ref.id)
    else:
        sf.chain = [ref.id]
    if ref.kind == "subagent":
        sf.agent_meta = _agent_meta(ref.path)
    if full:
        sf.work = seam.derive_work_signals(entries)
        sf.repos, sf.gh_repos, wt_hits, sf.worktree_branches = _locations(
            scan, sf.work.edited_paths
        )
        sf.worktrees = [p for p, _, _ in wt_hits]
        sf.acted = _worktree_acts(wt_hits) + _acted(scan)
        sf.handoff_files = [
            p for p in sf.work.edited_paths if "handoff" in os.path.basename(p).lower()
        ]
        if log is not None:
            _merge_hook_worktrees(sf, log)
        known = known_session_ids()
        sf.pasted_sessions = _pasted(
            scan.prompts, known, {ref.id, *([sf.origin] if sf.origin else [])}
        )
    return sf


def _merge_hook_worktrees(sf: SessionFacts, log) -> None:
    try:
        events = log.workspace_events(sf.id)
    except (OSError, ValueError) as exc:
        sf.warnings.append(f"hooklog workspace events unavailable: {exc}")
        return
    for ev in events:
        if ev.kind in ("add", "enter"):
            p = ev.path or ev.name
            if p and p not in sf.worktrees:
                sf.worktrees.append(p)
                sf.acted.extend(_worktree_acts([(p, ev.at, f"{ev.kind} (hooklog)")]))
            # EnterWorktree reports its branch; `just wt <name>` is named for it.
            branch = ev.branch or (ev.name if ev.via == "just" else None)
            if p and branch and valid_branch(branch):
                sf.worktree_branches.setdefault(p, branch)


def _fork_groups(mains: list[tuple[str, str, Optional[Instant]]]) -> dict[str, list[str]]:
    """sid -> siblings, among (sid, first prompt, start) of one project's main sessions."""
    by_prompt: dict[str, list[tuple[str, Instant]]] = defaultdict(list)
    for sid, prompt, start in mains:
        if prompt and start is not None:
            by_prompt[prompt].append((sid, start))
    out: dict[str, list[str]] = {}
    for group in by_prompt.values():
        for sid, start in group:
            sibs = [
                o
                for o, ostart in group
                if o != sid and abs((ostart.utc - start.utc).total_seconds()) <= FORK_WINDOW_S
            ]
            if sibs:
                out[sid] = sorted(sibs)
    return out


def _first_prompt_and_start(path: Path) -> tuple[str, Optional[Instant]]:
    # facts() asks this of every sibling in a member's project folder, and a
    # workstream's members usually share one folder: memoized per invocation,
    # it is one head read per sibling rather than one per sibling per member.
    key = ("head", path)
    if _snapshot is not None and key in _snapshot:
        return _snapshot[key]
    scan = _scan_claude(_head_entries(path), is_subagent=False)
    out = (scan.first_prompt, scan.first_ts)
    if _snapshot is not None:
        _snapshot[key] = out
    return out


def facts(sid_or_prefix: str, log=None) -> SessionFacts:
    """Every fact about one session, from a full read of its transcript.

    `log` is a loaded `hooklog.HookLog`. Loading one reads and pairs the whole
    hook log, too costly to do per session, so it is the caller's to pass: with
    it, Instants carry the inferred historical offset rather than the host's,
    and worktrees the hooks saw are merged in.
    """
    ref = _resolve_ref(sid_or_prefix)
    sf = _build(ref, _full_entries(ref.path), full=True, log=log)
    if ref.kind == "claude":
        sf.chain = [m.session_id for m in seam.session_chain(ref.path)] or [ref.id]
        if sf.first_prompt and sf.started:
            mains = [(ref.id, sf.first_prompt, sf.started)]
            for f in ref.path.parent.glob("*.jsonl"):
                if f != ref.path:
                    mains.append((f.stem, *_first_prompt_and_start(f)))
            sf.fork_siblings = _fork_groups(mains).get(ref.id, [])
    return sf


def _since_epoch(since) -> Optional[float]:
    if since is None:
        return None
    dt = getattr(since, "utc", since)
    return dt.timestamp() if isinstance(dt, datetime) else float(dt)


def iter_sessions(
    since: Optional[Instant] = None, include_codex: bool = True, log=None
) -> Iterator[SessionFacts]:
    """Every session touched at or after `since`, newest first, from head and tail
    reads only. Whole-file fields are left empty (`complete=False`)."""
    cutoff = _since_epoch(since)
    out: list[SessionFacts] = []
    for ref in _all_refs(include_codex):
        try:
            mtime = ref.path.stat().st_mtime
        except OSError:
            continue
        if cutoff is not None and mtime < cutoff:
            continue
        out.append(_build(ref, _cheap_entries(ref.path), full=False, log=log))

    # Chains and forks are per project folder, computed once here from the
    # head facts already read rather than by seam.session_chain per session,
    # which re-lists the folder each time.
    mains_by_proj: dict[Optional[str], list[SessionFacts]] = defaultdict(list)
    for sf in out:
        if sf.kind == "claude":
            mains_by_proj[sf.project_dir].append(sf)
    for group in mains_by_proj.values():
        lineage: dict[str, list[SessionFacts]] = defaultdict(list)
        for sf in group:
            lineage[sf.origin or sf.id].append(sf)
        for members in lineage.values():
            members.sort(key=lambda s: s.started.utc if s.started else _EPOCH)
            ids = [m.id for m in members]
            for m in members:
                m.chain = ids
        forks = _fork_groups([(sf.id, sf.first_prompt, sf.started) for sf in group])
        for sf in group:
            sf.fork_siblings = forks.get(sf.id, [])

    out.sort(key=lambda s: s.last_seen.utc.timestamp() if s.last_seen else 0.0, reverse=True)
    yield from out
