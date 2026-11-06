#!/usr/bin/env python3
"""What Codex actually does when asked to "review this PR", read from its rollouts.

Every Codex CLI session leaves a rollout under ~/.codex/sessions/YYYY/MM/DD/ that
records the system prompt it ran under (`session_meta.base_instructions`), every
tool call with its input, and every message. That makes a review process
measurable after the fact: which sessions were PR reviews, whether they fetched
the PR ref, ran tests, hit the web, or posted to GitHub, and what the model was
told. Scanned 2026-09-10 over 960 rollouts: 297 review sessions with tool calls
since 2025-11, median 45 tool calls, 68% ran a test command, 20% posted, and the
system prompt carried no review rubric at all, only "provide an evidence-backed
response".

    codex-review-scan.py reviews [--since YYYY-MM-DD] [--json] [--all]
    codex-review-scan.py trail <rollout path | id prefix>
    codex-review-scan.py prompt [<rollout path | id prefix>] [--out FILE]

`reviews` selects sessions whose first human prompt asks to review a PR, drops
the `codex-auto-review` side sessions (a second model that assesses the agent's
own actions; its prompts quote the agent transcript and match by accident) and
sessions with no tool calls, and tabulates what each one ran. `--all` keeps every
session with tool calls. `trail` prints one session's shell commands in order and
the head of its final answer. `prompt` extracts the system prompt.

The read categories are substring heuristics over tool-call inputs (see
`CATEGORIES`); `posted` is classified from the tool name and the command's HTTP
method, because a read of a comments endpoint is spelled like a write of one.
They are meant for counting across many sessions, not for judging one. Stdlib only.
`CLAUDE_LS_CODEX_DIR` overrides the rollout store, as it does for claude-ls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

# Codex writes shell commands two ways, and both are common in a real store:
# as a JS tool call, tools.exec_command({cmd:"..."}), and as a plain function
# call named exec_command/shell/shell_command whose arguments are JSON.
_CMD = re.compile(r'cmd:"((?:[^"\\]|\\.)*)"')
_SHELL_TOOLS = {"exec_command", "shell", "shell_command", "container.exec"}
_REVIEW_ASK = re.compile(r"review", re.I)
_PR_REF = re.compile(r"\bPR\b|/pull/|pull request|\bpr \d", re.I)
SIDE_MODELS = {"codex-auto-review"}

CATEGORIES = {
    "tests": r"cargo test|go test|pytest|just test|npm test|mix test|zig test|make test",
    "fetch": r"git (?:-C \S+ )?(?:fetch|checkout|switch|worktree)|gh pr checkout",
    "gh_pr": r"gh pr (?:view|diff|checks)|gh api repos/\S+/pulls",
    "web": r"web__run",
}
_CATEGORY_RE = {k: re.compile(v) for k, v in CATEGORIES.items()}

# "posted" cannot be a substring heuristic like the rest: a *read* of a comments
# endpoint is spelled exactly like a *write* of one, so `gh api .../pulls/7/comments`
# (a GET) scored as having posted. Classify it from the tool name and the
# command's method instead.
POSTED = "posted"
CATEGORY_ORDER = (*CATEGORIES, POSTED)
_POSTING_TOOLS = {
    "_add_review_to_pr",
    "_add_comment_to_issue",
    "_reply_to_review_comment",
    "_update_review_comment",
    "_update_issue_comment",
}
# gh's own write subcommands, or `gh api` carrying an explicit write method or a
# field flag (gh api switches to POST as soon as it is given one).
_POST_CMD = re.compile(
    r"gh (?:pr (?:comment|review)|issue comment)\b"
    r"|gh api\b[^\n]*(?:--method\s+(?:POST|PATCH|PUT)|-X\s*(?:POST|PATCH|PUT)"
    r"|(?:^|\s)(?:-f|-F|--field|--raw-field|--input)[\s=])"
)


def sessions_dir() -> Path:
    override = os.environ.get("CLAUDE_LS_CODEX_DIR")
    return Path(override) if override else Path.home() / ".codex" / "sessions"


def short_id(path: Path) -> str:
    tail = path.stem.rsplit("-", 5)
    return tail[-5][:8] if len(tail) >= 6 else path.stem[:8]


@dataclass
class Rollout:
    path: Path
    model: Optional[str] = None
    cli_version: Optional[str] = None
    base_instructions: str = ""
    first_prompt: str = ""
    final: str = ""
    # (tool name, raw input) in order. The name is half the signal: a posting
    # tool carries its intent there, not in its arguments.
    calls: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def inputs(self) -> List[str]:
        return [inp for _, inp in self.calls]

    @property
    def id(self) -> str:
        return short_id(self.path)

    @property
    def date(self) -> str:
        parts = self.path.parts
        return "-".join(parts[-4:-1]) if len(parts) >= 4 and parts[-2].isdigit() else ""

    @property
    def commands(self) -> List[str]:
        out: List[str] = []
        for name, inp in self.calls:
            if name in _SHELL_TOOLS:
                direct = _direct_command(inp)
                if direct:
                    out.append(direct)
                    continue
            out.extend(m.replace('\\"', '"') for m in _CMD.findall(inp))
        return out

    def counts(self) -> Dict[str, int]:
        blob = "\n".join(self.inputs)
        out = {k: len(rx.findall(blob)) for k, rx in _CATEGORY_RE.items()}
        out[POSTED] = sum(1 for name, _ in self.calls if name in _POSTING_TOOLS) + sum(
            1 for c in self.commands if _POST_CMD.search(c)
        )
        return {k: out[k] for k in CATEGORY_ORDER}

    def is_review_ask(self) -> bool:
        return bool(_REVIEW_ASK.search(self.first_prompt) and _PR_REF.search(self.first_prompt))


def _direct_command(inp: str) -> str:
    """The shell command from a direct tool call's JSON arguments, if it holds one."""
    try:
        args = json.loads(inp)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(args, dict):
        return ""
    cmd = args.get("cmd") or args.get("command") or ""
    if isinstance(cmd, list):
        return shlex.join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else ""


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict))
    return ""


def read_rollout(path: Path) -> Rollout:
    r = Rollout(path=path)
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("type")
            p = e.get("payload") or {}
            if t == "session_meta":
                bi = p.get("base_instructions")
                r.base_instructions = bi.get("text", "") if isinstance(bi, dict) else (bi or "")
                r.cli_version = p.get("cli_version")
            elif t == "turn_context":
                r.model = p.get("model") or r.model
            elif t == "response_item":
                kind = p.get("type")
                if kind == "message":
                    txt = _text(p.get("content"))
                    if p.get("role") == "user":
                        # environment_context and plugin notices arrive as tagged user
                        # messages before the human's first line
                        if not r.first_prompt and txt and not txt.startswith("<"):
                            r.first_prompt = txt
                    elif p.get("role") == "assistant":
                        r.final = txt
                elif kind in ("custom_tool_call", "function_call"):
                    r.calls.append(
                        (p.get("name") or "", p.get("input") or p.get("arguments") or "")
                    )
    return r


def iter_rollouts(root: Path, since: str = "") -> Iterator[Path]:
    for p in sorted(root.rglob("rollout-*.jsonl")):
        parts = p.relative_to(root).parts
        if since and len(parts) >= 4 and "-".join(parts[:3]) < since:
            continue
        yield p


def resolve(ref: str, root: Path) -> Path:
    p = Path(ref)
    if p.is_file():
        return p
    hits = [q for q in root.rglob("rollout-*.jsonl") if short_id(q).startswith(ref)]
    if len(hits) != 1:
        sys.exit(f"{ref}: {len(hits)} rollouts match; give a longer prefix or a path")
    return hits[0]


def select_reviews(root: Path, since: str = "", everything: bool = False) -> List[Rollout]:
    out = []
    for p in iter_rollouts(root, since):
        try:
            r = read_rollout(p)
        except OSError:
            continue
        if r.model in SIDE_MODELS or not r.inputs:
            continue
        if everything or r.is_review_ask():
            out.append(r)
    return out


def summarize(rows: List[Rollout]) -> Dict:
    n = len(rows)
    calls = sorted(len(r.inputs) for r in rows)
    per = [r.counts() for r in rows]
    return {
        "sessions": n,
        "calls_median": statistics.median(calls) if calls else 0,
        "calls_p90": calls[int(0.9 * (n - 1))] if calls else 0,
        "with": {k: sum(1 for c in per if c[k]) for k in CATEGORY_ORDER},
        "models": _tally(r.model or "?" for r in rows),
        "first": rows[0].date if rows else "",
        "last": rows[-1].date if rows else "",
    }


def _tally(xs) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def reviews_mode(args: argparse.Namespace) -> None:
    # Dates are compared lexically against the store's YYYY/MM/DD path, so a
    # malformed --since silently filters wrongly rather than failing.
    if args.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        sys.exit(f"--since: expected YYYY-MM-DD, got {args.since!r}")
    rows = select_reviews(sessions_dir(), args.since, args.all)
    s = summarize(rows)
    if args.json:
        json.dump(
            {
                "summary": s,
                "sessions": [
                    {
                        "id": r.id,
                        "date": r.date,
                        "model": r.model,
                        "calls": len(r.inputs),
                        **r.counts(),
                        "prompt": r.first_prompt[:120],
                    }
                    for r in rows
                ],
            },
            sys.stdout,
            indent=2,
        )
        print()
        return
    n = s["sessions"]
    print(
        f"{n} {'sessions' if args.all else 'PR-review sessions'} with tool calls, {s['first']} → {s['last']}; "
        f"tool calls per session median {s['calls_median']:.0f}, p90 {s['calls_p90']}"
    )
    for k in CATEGORY_ORDER:
        w = s["with"][k]
        print(f"  {k:7} {w:4}  ({100 * w // n if n else 0}%)")
    print("  models:", ", ".join(f"{m} ×{c}" for m, c in s["models"].items()))
    print()
    for r in rows[-args.n :] if args.n > 0 else []:
        c = r.counts()
        flags = "".join(k[0] if c[k] else "." for k in CATEGORY_ORDER)
        print(
            f"{r.date} {r.id} {str(r.model)[:14]:14} {len(r.inputs):4} {flags} "
            f"{r.first_prompt[:70].replace(chr(10), ' ')}"
        )
    print(f"\nflags: {' '.join(k[0] + '=' + k for k in CATEGORY_ORDER)}; -n for more rows")


def trail_mode(args: argparse.Namespace) -> None:
    r = read_rollout(resolve(args.ref, sessions_dir()))
    print(f"{r.path.name}\nmodel {r.model}, codex {r.cli_version}, {len(r.inputs)} tool calls")
    print(f"prompt: {r.first_prompt[:300]}\n")
    for cmd in r.commands:
        print("  $", cmd[: args.width])
    print(f"\nfinal answer:\n{r.final[:1500]}")


def prompt_mode(args: argparse.Namespace) -> None:
    root = sessions_dir()
    if args.ref:
        path = resolve(args.ref, root)
    else:
        path = max(iter_rollouts(root), key=lambda p: p.stat().st_mtime, default=None)
        if path is None:
            sys.exit(f"no rollouts under {root}")
    r = read_rollout(path)
    txt = r.base_instructions
    if args.out:
        Path(args.out).write_text(txt)
    print(f"{r.path.name}: model {r.model}, {len(txt)} chars of base_instructions")
    for h in re.findall(r"^#+ .*$", txt, re.M):
        print("  ", h)
    if not args.out:
        print("\n(--out FILE writes the text)")


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("reviews", help="tabulate PR-review sessions")
    a.add_argument("--since", default="", help="YYYY-MM-DD, by rollout date")
    a.add_argument("--json", action="store_true")
    a.add_argument("--all", action="store_true", help="every session with tool calls")
    a.add_argument("-n", type=int, default=20, help="rows to print; 0 for the summary alone")
    a.set_defaults(fn=reviews_mode)
    t = sub.add_parser("trail", help="one session's shell commands in order")
    t.add_argument("ref")
    t.add_argument("--width", type=int, default=160)
    t.set_defaults(fn=trail_mode)
    pm = sub.add_parser("prompt", help="the system prompt a session ran under")
    pm.add_argument("ref", nargs="?", default="")
    pm.add_argument("--out")
    pm.set_defaults(fn=prompt_mode)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
