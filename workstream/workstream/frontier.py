"""The frontier of a workstream: where every session stopped, what each anchor
looks like right now, and the ways to pick it up (spec D3a/b, D10, D11).

Evidence only. The prose a person steers by (D3c) is written by an agent from
this, into project-docs; `capture` writes the evidence beside it so the prose
can cite it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from workstream import anchors as anchors_mod
from workstream import hooklog as hooklog_mod
from workstream import seam, store
from workstream import sessions as sessions_mod
from workstream.clock import Instant
from workstream.sessions import SessionFacts
from workstream.store import AttachInterval, Window, Workstream

# D11 thresholds, stated as facts on the resume option rather than turned
# into a verdict: the reader weighs them against what it is about to do. From
# the mining doc (2026-11-02): dropped sessions had a median peak context near
# 110k and 5 of 14 sat at 150k or more, where a resume spends most of its
# budget re-reading; 60 min is the prompt-cache TTL in force on this account,
# past which a resume is as cold as a fresh start.
RESUME_MAX_CONTEXT = 150_000
CACHE_TTL = timedelta(minutes=60)


@dataclass
class SessionFrontier:
    facts: SessionFacts
    role: str  # canonical | ancillary | subagent | codex
    lifecycle: Optional[str] = None  # "SessionEnd(other)", "SessionStart(compact)", ...
    lifecycle_at: Optional[Instant] = None
    live: bool = False
    dropped: bool = True  # no capture names it (D10)
    warnings: list[str] = field(default_factory=list)
    # Canonical attaches, oldest first, matched by claim holder the way `role`
    # is. `role` is the session's role now: one whose claims were all released
    # is ancillary here, and these say when it was canonical.
    intervals: list[AttachInterval] = field(default_factory=list)
    # The live registry's record, for addressing a message: name, tmux pane.
    address: dict = field(default_factory=dict)
    # The stretches of the transcript that are this workstream's work
    # (store.session_windows); `facts` is already cut to them. None: the whole
    # session counts (ancillary, or a subagent of an unscoped parent).
    scope: Optional[list[Window]] = None


@dataclass
class Recommendation:
    """The pre-options single verdict, now derived from `Options.default` so
    readers of `recommendation` keep working: `message` maps to `fresh`, as
    the old verdict did for a live session."""

    action: str  # resume | fresh
    session_id: Optional[str]
    why: list[str]


@dataclass
class Option:
    how: str  # the command or the step, as the reader would run it
    facts: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)  # the same facts, structured, for --json


@dataclass
class Options:
    """The ways to pick a workstream up, each with the facts that bear on it.
    The reader chooses; `default` is a soft lean with a one-clause reason."""

    session_id: Optional[str]  # the newest canonical session, if any
    default: str  # message | fresh | resume
    default_why: str
    fresh: Option
    message: Optional[Option] = None  # only while that session is live
    resume: Optional[Option] = None  # only when there is a canonical session

    def recommendation(self) -> Recommendation:
        action = "resume" if self.default == "resume" else "fresh"
        why = [self.default_why] + (self.resume.facts if self.resume else [])
        return Recommendation(action, self.session_id, why)


@dataclass
class TaskStatus:
    id: str
    title: str
    kind: str
    state: str
    blocked: Optional[bool] = None  # None: not open, or weft could not say


@dataclass
class Frontier:
    ws: Workstream
    as_of: Instant
    sessions: list[SessionFrontier]
    anchors: list[anchors_mod.Anchor]
    messages: list
    timeline: list[tuple[Instant, str, str]]
    loose_ends: list[str]
    options: Options
    captures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tasks: list[TaskStatus] = field(default_factory=list)
    # Loose ends that should stop `close`: all of them except sessions with no
    # recorded frontier (a capture question, not unfinished work) and the
    # "workstream not closed" nudge that closing answers.
    closure_blockers: list[str] = field(default_factory=list)

    @property
    def recommendation(self) -> Recommendation:
        return self.options.recommendation()


def docs_root() -> Path:
    override = os.environ.get("WORKSTREAM_DOCS_ROOT")
    return Path(override) if override else Path.home() / "projects-new" / "project-docs" / "docs"


def _session_refs(ws: Workstream) -> list[tuple[str, str]]:
    """(scheme, id) for every session ref on the workstream, in ref order."""
    out = []
    for ref in ws.refs:
        scheme, _, value = ref.partition(":")
        if scheme in ("claude", "codex"):
            out.append((scheme, value))
        elif scheme == "agent":
            out.append((scheme, value.partition("/")[2] or value))
    return out


def _anchor_refs(ws: Workstream) -> list[str]:
    return [r for r in ws.refs if r.partition(":")[0] in ("gh", "pr", "branch", "wt", "file")]


def _capture_paths(ws: Workstream) -> list[str]:
    return [r.partition(":")[2] for r in ws.refs if r.startswith("frontier:")]


def _captured_sessions(ws: Workstream) -> set[str]:
    """Session ids named by any recorded capture; reading each file is what
    lets `dropped` mean 'no recorded frontier' rather than 'ended badly'."""
    named: set[str] = set()
    for p in _capture_paths(ws):
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in data.get("sessions", []):
            sid = s.get("id") if isinstance(s, dict) else None
            if sid:
                named.add(sid)
    return named


def _role(ws: Workstream, facts: SessionFacts) -> str:
    if facts.kind == "subagent":
        return "subagent"
    if facts.kind == "codex":
        return "codex"
    holders = ws.canonical_holders()
    return "canonical" if f"agent:{facts.ref}" in holders else "ancillary"


def session_scope(ref: str, ws: Workstream, history=()) -> Optional[list[Window]]:
    """The windows a session's evidence counts in for `ws`: its own canonical
    intervals, or for a subagent its parent's (D9: subagents count with their
    parent). None when neither ever held a claim there."""
    wins = store.session_windows(ref, ws, history)
    if wins is None and ref.startswith("agent:"):
        parent = ref[6:].partition("/")[0]
        wins = store.session_windows(f"claude:{parent}", ws, history)
    return wins


def _held_by_members(ws: Workstream) -> bool:
    members = {f"agent:{r}" for r in ws.refs if r.partition(":")[0] in ("claude", "codex", "agent")}
    return any(i.holder in members for i in ws.intervals())


# One weft listing per CLI invocation: `cli.main` sets this to {} on entry and
# back to None on exit. Outside the CLI (tests, library callers) nothing is
# cached, since claims may change between two builds.
_history_cache: Optional[dict] = None


def history_for(ws: Workstream, warnings: list[str]) -> list[Workstream]:
    """Every workstream, closed ones included, for the gap rule in
    `store.session_windows`; nothing is fetched when no member of `ws` ever
    held a claim on it, since then no window exists to compute."""
    if not _held_by_members(ws):
        return []
    cache = _history_cache
    if cache is not None and "history" in cache:
        return cache["history"]
    try:
        out = store.list_workstreams(include_closed=True)
    except Exception as e:  # degrade: windows then start at the session's start
        warnings.append(
            f"other workstreams unavailable, intervals not trimmed at earlier claims: {e}"
        )
        return []
    if cache is not None:
        cache["history"] = out
    return out


def _in(at: Optional[Instant], wins: Optional[list[Window]]) -> bool:
    return store.in_windows(at.utc if at else None, wins)


def scoped(
    facts: SessionFacts,
    wins: Optional[list[Window]],
    starts: Optional[dict[str, Optional[Instant]]] = None,
) -> SessionFacts:
    """`facts` cut to the windows: what the session did, said and spawned there.
    Identity, lineage, context sizes and locations stay whole: they describe
    the transcript, and a resume loads the whole transcript.

    `starts` maps subagent id to its start, from the facts the caller already
    read; the subagent list keeps those that started inside the windows, so a
    subagent the caller did not read is dropped rather than opened here. None
    leaves the list as it is (sync, which never reads it)."""
    if wins is None:
        return facts
    last = facts.last_assistant
    subs = facts.subagents
    if starts is not None:
        subs = [a for a in subs if _in(starts.get(a), wins)]
    return replace(
        facts,
        acted=[a for a in facts.acted if _in(a.at, wins)],
        human_prompts=[p for p in facts.human_prompts if _in(p[0], wins)],
        last_assistant=last if last and _in(last[0], wins) else None,
        last_tools=[t for t in facts.last_tools if _in(t[0], wins)],
        subagents=subs,
    )


def _lifecycle_text(ev) -> str:
    detail = ev.source or ev.reason
    return f"{ev.event}({detail})" if detail else ev.event


def foreign_actions(ws: Workstream, recent, member_ids: set[str]) -> list[str]:
    """Merges and closes of this workstream's PR anchors by sessions attached
    elsewhere: the cross-repo case, where a session rooted in one repo merged
    another workstream's PRs while that workstream's owner was live."""
    nums: dict[str, str] = {}
    for ref in ws.refs:
        scheme, _, value = ref.partition(":")
        if scheme in ("gh", "pr"):
            repo, _, num = value.partition("#")
            nums[num] = value
    if not nums:
        return []
    out: list[str] = []
    for f in recent:
        if f.id in member_ids or f.kind == "subagent":
            continue
        for at, name, summary in f.last_tools:
            if name != "Bash":
                continue
            m = _MERGE_RE.search(summary)
            if not m or m.group(2) not in nums:
                continue
            verb = "merged" if m.group(1) == "merge" else "closed"
            repo = nums[m.group(2)].partition("#")[0]
            # An explicit --repo is the answer; the repo fallbacks below are for
            # a bare `gh pr merge N` run from inside a checkout.
            named = _REPO_FLAG_RE.search(summary)
            if named and named.group(1) != repo:
                continue
            basename = repo.split("/")[-1]
            in_checkout = any(os.path.basename(r.rstrip("/")) == basename for r in f.repos)
            if repo not in summary and not in_checkout:
                continue
            when = at.iso() if at else "unknown time"
            out.append(
                f"gh:{nums[m.group(2)]} was {verb} by {f.id[:8]} at {when}, a session not attached here"
            )
    return out


_MERGE_RE = re.compile(r"gh pr (merge|close) (?:[^\s]*\s+)*?#?(\d+)")
_REPO_FLAG_RE = re.compile(r"(?:--repo[=\s]|-R\s)\s*(\S+)")


NOT_CLOSED = "all tasks done; workstream not closed"


def tasks_for(ws: Workstream, blocked: Optional[set[str]]) -> list[TaskStatus]:
    """Open first (unblocked before blocked), then done, then dropped."""
    out = []
    for c in ws.tasks:
        is_open = c.state not in store.SETTLED
        out.append(
            TaskStatus(
                c.id,
                c.title,
                c.kind,
                c.state,
                (c.id in blocked) if is_open and blocked is not None else None,
            )
        )
    rank = {"done": 2, "dropped": 3}
    return sorted(out, key=lambda t: rank.get(t.state, 1 if t.blocked else 0))


def _task_loose_ends(ws: Workstream, tasks: list[TaskStatus]) -> list[str]:
    if not tasks:
        return []
    open_ = [t for t in tasks if t.state not in store.SETTLED]
    if open_:
        n = len(open_)
        if any(t.blocked is None for t in open_):
            m = "blocked unknown"
        else:
            m = f"{sum(1 for t in open_ if t.blocked)} blocked"
        return [f"{n} task{'' if n == 1 else 's'} open ({m})"]
    if not ws.closed:
        return [NOT_CLOSED]
    return []


def build(
    ws: Workstream,
    *,
    log=None,
    gh_runner=None,
    now: Optional[Instant] = None,
    recent=(),
    blocked: Optional[set[str]] = None,
    gh_cache_only: bool = False,
    history: Optional[list[Workstream]] = None,
) -> Frontier:
    """`blocked` is the set of node ids weft reports blocked; None asks weft,
    and only when the workstream has an open task. `gh_cache_only` answers PR
    anchors from the gh cache alone (anchors.liveness). `history` is every
    workstream the members may have claimed, closed ones included, for
    `store.session_windows`; None lists them from weft when it matters.

    A session that holds or held a canonical claim here is cut to its windows
    (`scoped`): its prompts, tools, writes, subagents, messages and timeline
    rows from before the claim belong to the work it did then."""
    as_of = now or Instant.now()
    warnings: list[str] = []
    if log is None:
        try:
            log = hooklog_mod.load()
        except Exception as e:  # the hooklog is optional evidence
            warnings.append(f"hooklog unavailable: {e}")
            log = None

    captured = _captured_sessions(ws)
    live: dict[str, dict] = {}
    try:
        registry = sessions_mod.seam.live_registry()
        live = {r.get("sessionId", ""): r for r in registry if isinstance(r, dict)}
    except Exception as e:
        warnings.append(f"live registry unavailable: {e}")

    ivs = ws.intervals()
    if history is None:
        history = history_for(ws, warnings)
    sfs: list[SessionFrontier] = []
    # Uncut, for questions about what a directory held rather than whose
    # work it was (settle_gone_worktrees).
    whole: list[SessionFacts] = []
    loaded: list[SessionFacts] = []
    for scheme, sid in _session_refs(ws):
        try:
            loaded.append(sessions_mod.facts(sid, log=log))
        except Exception as e:
            warnings.append(f"{scheme}:{sid[:8]}: {e}")
    # Each member is read once, above; a scoped parent's subagent list is cut
    # with these starts rather than by opening each subagent again.
    starts = {f.id: f.started for f in loaded if f.kind == "subagent"}
    for facts in loaded:
        wins = session_scope(facts.ref, ws, history)
        if facts.kind == "subagent" and wins is not None and not _in(facts.started, wins):
            # Spawned for the work the parent did before or after this claim.
            continue
        whole.append(facts)
        sf = SessionFrontier(facts=scoped(facts, wins, starts), role=_role(ws, facts), scope=wins)
        sf.intervals = [i for i in ivs if i.holder == f"agent:{facts.ref}"]
        sf.live = facts.id in live
        if sf.live:
            rec = live[facts.id]
            sf.address = {k: rec[k] for k in ("name", "tmux") if rec.get(k)}
        sf.dropped = facts.id not in captured
        if log is not None:
            ev = log.last_lifecycle(facts.id)
            if ev is not None:
                sf.lifecycle = _lifecycle_text(ev)
                sf.lifecycle_at = ev.at
        sfs.append(sf)

    # Subagents under their parents, canonical before ancillary, newest first.
    order = {"canonical": 0, "ancillary": 1, "codex": 2, "subagent": 3}
    sfs.sort(
        key=lambda s: (
            order[s.role],
            -(s.facts.last_seen.utc.timestamp() if s.facts.last_seen else 0),
        )
    )

    # A `branch:` ref names a repo by basename; the sessions know where it is.
    hints: dict[str, str] = {}
    for s in sfs:
        for r in s.facts.repos:
            hints.setdefault(os.path.basename(r.rstrip("/")), r)
    for name, root in worktree_repo_hints(ws.refs).items():
        hints.setdefault(name, root)
    anchor_list = anchors_mod.liveness_many(
        _anchor_refs(ws), gh_runner=gh_runner, repo_hints=hints, gh_cache_only=gh_cache_only
    )
    settle_gone_worktrees(
        anchor_list,
        whole,
        hints=hints,
        gh_runner=gh_runner,
        gh_cache_only=gh_cache_only,
    )

    member_ids = {s.facts.id for s in sfs}
    scope_of = {s.facts.id: s.scope for s in sfs}
    messages = []
    if log is not None:
        # A message involving a member counts unless a scoped member it
        # involves sent or got it outside its windows: then it was that
        # session's other work, even when the other end is unscoped here.
        messages = []
        for m in log.messages(involving=member_ids):
            ends = [sid for sid in (m.from_sid, m.to_sid) if sid in member_ids]
            if ends and all(_in(m.at, scope_of[sid]) for sid in ends):
                messages.append(m)

    if blocked is None and any(c.state not in store.SETTLED for c in ws.tasks):
        try:
            blocked = store.blocked_ids()
        except store.WeftError as e:
            warnings.append(f"blocked tasks unavailable: {e}")
    tasks = tasks_for(ws, blocked)
    task_ends = _task_loose_ends(ws, tasks)

    timeline = _timeline(sfs, messages, anchor_list)
    foreign = foreign_actions(ws, recent, member_ids)
    loose = _loose_ends(sfs, anchor_list, messages) + foreign + task_ends
    blockers = (
        _loose_ends(sfs, anchor_list, messages, include_dropped=False)
        + foreign
        + [t for t in task_ends if t != NOT_CLOSED]
    )
    captures = _capture_paths(ws)
    opts = options(ws, sfs, as_of, captures)
    return Frontier(
        ws=ws,
        as_of=as_of,
        sessions=sfs,
        anchors=anchor_list,
        messages=messages,
        timeline=timeline,
        loose_ends=loose,
        options=opts,
        captures=captures,
        warnings=warnings,
        tasks=tasks,
        closure_blockers=blockers,
    )


def _timeline(sfs, messages, anchor_list) -> list[tuple[Instant, str, str]]:
    rows: list[tuple[Instant, str, str]] = []
    for s in sfs:
        f = s.facts
        if f.started and _in(f.started, s.scope):
            rows.append(
                (f.started, "session", f"{f.ref} started ({s.role}): {f.first_prompt[:80]}")
            )
        for iv in s.intervals:
            rows.append((iv.at, "claim", f"{f.id[:8]} took the canonical claim"))
            if iv.released_at:
                rows.append((iv.released_at, "release", f"{f.id[:8]} released the claim"))
        if s.lifecycle_at and s.lifecycle and _in(s.lifecycle_at, s.scope):
            rows.append((s.lifecycle_at, "lifecycle", f"{f.id[:8]} {s.lifecycle}"))
        if f.last_seen and _in(f.last_seen, s.scope):
            rows.append((f.last_seen, "last-seen", f"{f.id[:8]} last record"))
        for at, text in f.human_prompts[-3:]:
            rows.append((at, "prompt", f"{f.id[:8]}: {text[:100]}"))
    for m in messages:
        rows.append(
            (m.at, "message", f"{m.from_sid[:8]} → {m.to_text()}: {m.summary or m.body[:80]}")
        )
    for a in anchor_list:
        upd = a.liveness.get("updated_at")
        if a.scheme == "gh" and upd:
            try:
                dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                rows.append(
                    (
                        Instant(dt.astimezone(timezone.utc), timedelta(0)),
                        "anchor",
                        f"{a.ref} updated upstream ({a.liveness.get('state')}, "
                        f"{a.liveness.get('review_decision') or 'no review decision'})",
                    )
                )
            except ValueError:
                pass
    rows.sort(key=lambda r: r[0].utc)
    return rows


def _loose_ends(sfs, anchor_list, messages, *, include_dropped: bool = True) -> list[str]:
    out: list[str] = []
    for a in anchor_list:
        lv = a.liveness
        if a.scheme == "wt" and lv.get("exists"):
            bits = []
            # Once the content is on the default branch (a squash merge
            # included), its commits and their push state are no longer
            # outstanding; the deleted remote branch would otherwise read as
            # "no upstream" forever. Uncommitted changes still are.
            merged = lv.get("merged_into_default") is True
            if lv.get("unmerged") and not merged:
                bits.append(
                    f"{lv['unmerged']} commit(s) not on {lv.get('default_branch') or 'the default branch'}"
                )
            if lv.get("dirty"):
                bits.append("uncommitted changes")
            up = None if merged else lv.get("unpushed")
            if up in ("no remote", "no upstream"):
                bits.append(up)
            elif isinstance(up, int) and up > 0:
                bits.append(f"{up} unpushed commit(s)")
            if bits:
                out.append(f"worktree {a.value}: " + ", ".join(bits))
        elif a.scheme == "wt" and lv.get("exists") is False:
            # Retiring a worktree once its branch merged is the normal end of
            # a slice; only a gone worktree whose work did not land is loose.
            if lv.get("merged_into_default") is True:
                pass
            elif lv.get("merged_into_default") is False and lv.get("branch"):
                out.append(f"worktree {a.value} is gone; its branch {lv['branch']} is not merged")
            else:
                out.append(f"worktree {a.value} is gone")
        elif (
            a.scheme == "branch"
            and lv.get("exists_local")
            and not lv.get("exists_remote")
            and lv.get("merged_into_default") is not True
        ):
            out.append(f"branch {a.value} has no remote copy")
        elif a.scheme == "gh":
            if lv.get("review_decision") == "CHANGES_REQUESTED" and lv.get("state") == "OPEN":
                who = ", ".join(
                    r.get("author", "?")
                    for r in lv.get("reviews", [])
                    if r.get("state") == "CHANGES_REQUESTED"
                )
                out.append(
                    f"{a.ref}: changes requested{f' by {who}' if who else ''}, not yet addressed"
                )
            elif lv.get("state") == "OPEN" and lv.get("is_draft"):
                out.append(f"{a.ref}: still a draft")
        for w in a.warnings:
            out.append(f"{a.ref}: {w}")
    for s in sfs:
        f = s.facts
        if include_dropped and s.dropped and s.role in ("canonical", "ancillary"):
            how = s.lifecycle or "no lifecycle event"
            out.append(f"{f.id[:8]} ({s.role}) has no recorded frontier; last {how}")
        if f.last_assistant and f.last_assistant[1].rstrip().endswith("?"):
            out.append(
                f"{f.id[:8]} ended on a question for the human: {f.last_assistant[1][-120:]}"
            )
    for m in messages:
        if m.to_sid is None:
            out.append(
                f"message from {m.from_sid[:8]} to {m.to_raw!r} could not be resolved to a session"
            )
    return out


# --- gone worktrees ----------------------------------------------------------
#
# A `wt:` probe of a missing directory has no HEAD to ask, so it cannot say
# what the worktree held. The sessions that worked in it, and the
# workstream's own branch and PR anchors, can: settle_gone_worktrees derives
# the branch from them and asks whether it merged.

_CLAUDE_WT_DIR = os.path.join(".claude", "worktrees")


def _real(path: str) -> str:
    # Anchors are stored realpath'd (store._canon_ref); a session's paths are as typed.
    return os.path.realpath(os.path.expanduser(path))


def worktree_named_for(path: str, branch: str, repo: Optional[str] = None) -> bool:
    """Whether a worktree directory's name says it holds `branch`: `just wt`
    makes `<repo>-<branch with / as ->`; EnterWorktree makes
    `<repo>/.claude/worktrees/<name>` on a branch named for `<name>`."""
    path = path.rstrip("/")
    leaf = os.path.basename(path)
    flat = branch.replace("/", "-")
    if repo and leaf == f"{repo}-{flat}":
        return True
    if os.path.dirname(path).endswith(_CLAUDE_WT_DIR):
        return leaf == flat or branch == f"worktree-{leaf}" or branch.endswith("/" + leaf)
    return False


def _inside(d: str, real: str) -> bool:
    d = _real(d) if d else ""
    return d == real or d.startswith(real + "/")


def _bare_name_matches(path: str, name: str, repos: Iterable[str]) -> bool:
    """Whether a bare `just wt <name>` (path not printed) made this worktree:
    the leaf is exactly `<repo>-<name with / as ->` for a repo the sessions
    know, or exactly the name under `.claude/worktrees`. A suffix match is
    not enough: `kiln-fix-tempo` ends in `-tempo` too, and `just wt tempo`
    did not make it."""
    path = path.rstrip("/")
    leaf, flat = os.path.basename(path), name.replace("/", "-")
    if os.path.dirname(path).endswith(_CLAUDE_WT_DIR):
        return leaf == flat
    return any(leaf == f"{r}-{flat}" for r in repos)


def _acted_repo_name(a) -> Optional[str]:
    """The repo an act ran in, by basename: its dir's canonical root while it
    exists, else the remote a push went to."""
    if a.dir and os.path.isdir(a.dir):
        root = seam.canonical_repo_root(a.dir) or seam.repo_root_for(a.dir)
        if root:
            return os.path.basename(root.rstrip("/"))
    if a.repo:
        tail = a.repo.rstrip("/").rpartition("/")[2].rpartition(":")[2]
        return tail.removesuffix(".git") or None
    return None


def _session_branch(
    path: str, facts: list[SessionFacts], repos: Iterable[str] = ()
) -> Optional[tuple[str, str, Optional[str]]]:
    """(branch, how, repo basename or None) for a worktree from what sessions
    did in it: the branch of its last commit, else the branch it was created
    or entered on, else the one branch pushed from it. Two different pushes
    decide nothing. `repos` are basenames beyond the sessions' own that a
    `just wt` prefix may name."""
    real = _real(path)
    known = {os.path.basename(r.rstrip("/")) for f in facts for r in f.repos} | set(repos)
    commits, pushed = [], []
    for f in facts:
        for a in f.acted:
            if a.kind == "branch" and _inside(a.dir, real):
                (commits if a.how == "git commit" else pushed).append((a, f))
    if commits:
        a, f = max(commits, key=lambda x: x[0].at.utc.timestamp() if x[0].at else 0.0)
        return a.value, f"{f.id[:8]} committed on it there", _acted_repo_name(a)
    for f in facts:
        for key, branch in f.worktree_branches.items():
            if os.path.isabs(key):
                if _real(key) == real:
                    return branch, f"{f.id[:8]} created or entered it on that branch", None
            elif branch == key and _bare_name_matches(real, key, known):
                return branch, f"{f.id[:8]} created it with `just wt {key}`", None
    names = {a.value for a, _ in pushed}
    if len(names) == 1:
        a, f = pushed[-1]
        return a.value, f"{f.id[:8]} pushed it from there", _acted_repo_name(a)
    return None


def _last_acted(path: str, facts: list[SessionFacts]) -> Optional[datetime]:
    """When a session last worked inside the worktree: created or entered
    it, edited, committed or pushed there."""
    real = _real(path)
    times = [
        a.at.utc
        for f in facts
        for a in f.acted
        if a.at is not None
        and (
            (a.kind == "branch" and _inside(a.dir, real))
            or (a.kind == "wt" and _inside(a.value, real))
        )
    ]
    return max(times) if times else None


def _parse_utc(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _claude_wt_root(path: str) -> Optional[str]:
    """The repo a `<repo>/.claude/worktrees/<name>` path lives in, else None."""
    marker = os.sep + _CLAUDE_WT_DIR + os.sep
    path = path.rstrip("/") + os.sep
    return path.split(marker)[0] if marker in path else None


def worktree_repo_hints(refs) -> dict[str, str]:
    """Repo basename -> root, from `wt:` refs whose path names the repo. The
    sessions' repos come from the paths they edited, which all sat inside a
    worktree that may be gone; the repo it lived in usually is not."""
    out: dict[str, str] = {}
    for ref in refs:
        if ref.startswith("wt:"):
            root = _claude_wt_root(ref[3:])
            if root and os.path.isdir(root):
                out.setdefault(os.path.basename(root), root)
    return out


def _repo_names(path: str, branch: str, hints: dict[str, str], acted: Optional[str]) -> list[str]:
    """Basenames of the repo a gone worktree belonged to, for a
    `branch:<repo>@<branch>` probe: the repo the session evidence ran in, and
    whatever the directory's name says (a `.claude/worktrees` path names its
    repo outright and goes into `hints`; a `just wt` path starts with it).
    Never every repo the sessions touched: that is a git probe per repo per
    candidate on every `frontier`, for a guess."""
    out: list[str] = [acted] if acted else []
    path = path.rstrip("/")
    root = _claude_wt_root(path)
    if root:
        name = os.path.basename(root)
        hints.setdefault(name, root)
        out.append(name)
    leaf, flat = os.path.basename(path), branch.replace("/", "-")
    if leaf.endswith("-" + flat) and len(leaf) > len(flat) + 1:
        out.append(leaf[: -len(flat) - 1])
    return list(dict.fromkeys(out))


def _merged_by_anchor(
    branch: str, anchor_list, last_acted: Optional[datetime]
) -> tuple[Optional[bool], Optional[dict], bool]:
    """(merged, merged_via, answered): what the workstream's own probed
    anchors say about `branch`. `answered` means no further probe can add
    anything: a merged PR credited, or a `branch:` anchor that decided.

    A PR is credited only if it merged no earlier than the last time a
    session worked in the worktree: branch names get reused, and work done
    after an old PR with the same head merged is not in it. With no session
    time to compare, the name is all there is, and `merged_via` says so
    (`head_only`), as anchors.py does for a deleted branch."""
    for a in anchor_list:
        lv = a.liveness
        if a.scheme != "gh" or lv.get("head_ref") != branch or lv.get("state") != "MERGED":
            continue
        merged_at = _parse_utc(lv.get("merged_at"))
        via = {"pr_anchor": a.ref, "merged_at": lv.get("merged_at")}
        if last_acted is None or merged_at is None:
            return True, {**via, "head_only": True}, True
        if merged_at >= last_acted:
            return True, via, True
    verdict = None
    for a in anchor_list:
        if a.scheme == "branch" and a.value.partition("@")[2] == branch:
            m = a.liveness.get("merged_into_default")
            if m is True:
                return True, a.liveness.get("merged_via") or {"branch_anchor": a.ref}, True
            if m is False:
                verdict = False
    return verdict, None, verdict is not None


def settle_gone_worktrees(
    anchor_list: list[anchors_mod.Anchor],
    facts: list[SessionFacts],
    *,
    hints: Optional[dict[str, str]] = None,
    gh_runner=None,
    gh_cache_only: bool = False,
) -> None:
    """Fill in a gone `wt:` anchor's liveness with the branch it held and
    whether that branch merged, in place. The branch comes from the sessions'
    evidence first, then from a branch or PR anchor whose name matches the
    worktree's by the `just wt` or `.claude/worktrees` convention. Sets
    `branch`, `branch_from`, `merged_into_default`, `merged_via` and `status`;
    leaves them unset when nothing can be derived, so the loose end stands."""
    hints = dict(hints or {})
    for a in anchor_list:
        lv = a.liveness
        if a.scheme != "wt" or lv.get("exists") is not False:
            continue
        cands: list[tuple[str, str, Optional[str]]] = []
        found = _session_branch(a.value, facts, hints)
        if found:
            cands.append(found)
        for b in anchor_list:
            if b.scheme == "branch":
                repo, _, name = b.value.partition("@")
                if name and worktree_named_for(a.value, name, repo):
                    cands.append((name, f"named for {b.ref}", None))
            elif b.scheme == "gh" and b.liveness.get("head_ref"):
                head = b.liveness["head_ref"]
                repo = b.value.partition("#")[0].rpartition("/")[2]
                if worktree_named_for(a.value, head, repo):
                    cands.append((head, f"named for the head of {b.ref}", None))
        last = _last_acted(a.value, facts)
        seen: set[str] = set()
        for branch, how, acted_repo in cands:
            if branch in seen:
                continue
            seen.add(branch)
            merged, via, answered = _merged_by_anchor(branch, anchor_list, last)
            if not answered:
                for repo in _repo_names(a.value, branch, hints, acted_repo):
                    probe = anchors_mod.liveness(
                        f"branch:{repo}@{branch}",
                        repo_hints=hints,
                        gh_runner=gh_runner,
                        gh_cache_only=gh_cache_only,
                    ).liveness
                    if probe.get("merged_into_default") is True:
                        merged, via = True, probe.get("merged_via") or {"reachable": True}
                        break
                    if probe.get("merged_into_default") is False:
                        merged = False
            if merged is None:
                continue
            state = "merged" if merged else "not merged"
            if merged and (via or {}).get("head_only"):
                state = "merged, by branch name only"
            lv.update(
                branch=branch,
                branch_from=how,
                merged_into_default=merged,
                merged_via=via,
                status=f"gone (branch {branch} {state})",
            )
            break


def gone_and_merged(a: anchors_mod.Anchor) -> bool:
    """A worktree retired after its branch merged: history, not a loose end."""
    return (
        a.scheme == "wt"
        and a.liveness.get("exists") is False
        and a.liveness.get("merged_into_default") is True
    )


def merged_by_name_only(a: anchors_mod.Anchor) -> bool:
    """Merged on the strength of a branch name alone, which a reused name
    can fake: enough to stop nagging, not enough to forget the anchor."""
    return bool((a.liveness.get("merged_via") or {}).get("head_only"))


def prose_path(ws: Workstream, sfs: list[SessionFrontier], root: Optional[Path] = None) -> Path:
    """Where the D3c prose frontier lives, whether or not it is written yet."""
    root = root or docs_root()
    name = ws.name or ws.id[:12]
    return root / slug_for(ws, sfs, root) / "workstreams" / f"{name}.md"


def newest_capture(paths) -> Optional[tuple[str, float]]:
    """(path, mtime) of the most recently written capture that still exists."""
    best = None
    for p in paths:
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        if best is None or m > best[1]:
            best = (p, m)
    return best


# Who wrote a capture, kept in its JSON as `recorded_by`. Captures from before
# the field existed have none, and readers say nothing about them.
BY_RECORD = "frontier --record"
BY_RETIRE = "retire"
BY_SESSION_END = "session_end_hook"


def capture_meta(path: str) -> dict:
    """`as_of`, `recorded_by` and the session ids a capture names; {} when the
    file is gone or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    ids = [s.get("id") for s in data.get("sessions", []) if isinstance(s, dict) and s.get("id")]
    return {"as_of": data.get("as_of"), "recorded_by": data.get("recorded_by"), "sessions": ids}


def _resume_option(s: SessionFrontier, as_of: Instant) -> tuple[Option, bool, bool]:
    """The resume option, and whether its context is within the cap and its
    cache warm: the two facts the default leans on."""
    f = s.facts
    ctx: Optional[int]
    if f.compaction_count:
        ctx = f.context_after_compaction
        n = f.compaction_count
        detail = (
            f"({n} compaction{'' if n == 1 else 's'}; whole-transcript peak {f.peak_context:,})"
        )
        ctx_fact = (
            f"no turn since the last compaction, so its resume cost is unknown {detail}"
            if ctx is None
            else f"context since the last compaction {ctx:,} {detail}"
        )
    else:
        ctx = f.peak_context
        ctx_fact = f"peak context {ctx:,} (never compacted)"
    # Unknown is not small: a resume is only the lean when the cost is known.
    small = ctx is not None and ctx <= RESUME_MAX_CONTEXT
    facts = [ctx_fact]
    if ctx is not None:
        facts.append(
            f"{'within' if small else 'above'} the resume cap "
            f"(RESUME_MAX_CONTEXT {RESUME_MAX_CONTEXT:,})"
        )
    idle_min = None
    warm = False
    if f.last_seen:
        idle = as_of.utc - f.last_seen.utc
        idle_min = int(idle.total_seconds() // 60)
        warm = idle <= CACHE_TTL
        facts.append(
            f"idle {_fmt_td(idle)}, {'within' if warm else 'past'} CACHE_TTL "
            f"{_fmt_td(CACHE_TTL)}: the cache is {'warm' if warm else 'cold'}"
        )
    else:
        facts.append("no last activity recorded, so cache warmth is unknown")
    if s.live:
        facts.append("it is running now: resuming would drive one transcript from two processes")
    cmd = f"claude --resume {f.id}"
    if f.cwd:
        cmd = f"cd {f.cwd} && {cmd}"
    data = {
        "command": cmd,
        "context": ctx,
        "context_basis": "after_compaction" if f.compaction_count else "peak",
        "peak_context": f.peak_context,
        "compaction_count": f.compaction_count,
        "within_resume_cap": None if ctx is None else small,
        "idle_min": idle_min,
        "cache_warm": warm,
    }
    return Option(cmd, facts, data), small, warm


def options(
    ws: Workstream,
    sfs: list[SessionFrontier],
    as_of: Instant,
    captures=(),
    *,
    root: Optional[Path] = None,
) -> Options:
    """Message the newest canonical session while it runs, start fresh from
    the recorded frontier, or resume its transcript; each with its facts."""
    prose = prose_path(ws, sfs, root)
    fresh_facts = [f"prose frontier: {prose}" + ("" if prose.exists() else " (not written yet)")]
    newest = newest_capture(captures)
    if newest:
        age = as_of.utc - datetime.fromtimestamp(newest[1], timezone.utc)
        fresh_facts.append(f"newest capture: {newest[0]} ({_fmt_td(age)} old)")
    else:
        fresh_facts.append("no capture recorded yet (`workstream frontier --record` writes one)")
    canon = [s for s in sfs if s.role == "canonical"]
    name = ws.name or ws.id[:12]
    how = f"start a new session, `workstream attach {name}`, read the prose frontier and the newest capture"
    if canon:
        how += f", then read {canon[0].facts.id[:8]} selectively (`claude-ls summary {canon[0].facts.id[:8]}`)"
    fresh = Option(
        how,
        fresh_facts,
        {
            "prose_frontier": str(prose),
            "prose_exists": prose.exists(),
            "newest_capture": newest[0] if newest else None,
        },
    )
    if not canon:
        return Options(None, "fresh", "no canonical session is attached", fresh)
    s = canon[0]
    resume, small, warm = _resume_option(s, as_of)
    message = None
    if s.live:
        addr = s.address
        bits = []
        if addr.get("name"):
            bits.append(f'SendMessage to "{addr["name"]}"')
        if addr.get("tmux"):
            bits.append(f"tmux pane {addr['tmux']}")
        message = Option(
            " or ".join(bits) or f"message session {s.facts.id[:8]} (no registry name to address)",
            [f"{s.facts.id[:8]} is running and owns this workstream's canonical claim"],
            dict(addr),
        )
        return Options(
            s.facts.id,
            "message",
            "the session is still running; message it rather than resuming its transcript",
            fresh,
            message,
            resume,
        )
    if small and warm:
        why = "its context is within the resume cap and the cache is still warm"
        return Options(s.facts.id, "resume", why, fresh, None, resume)
    cold = (
        "the cache is cold"
        if resume.data["idle_min"] is not None
        else "its last activity is unknown"
    )
    over = (
        "its context since the last compaction is unknown"
        if resume.data["context"] is None
        else "its context is above the resume cap"
    )
    why = {
        (False, False): f"{over} and {cold}",
        (False, True): over,
        (True, False): cold,
    }[(small, warm)]
    return Options(s.facts.id, "fresh", why, fresh, None, resume)


def _fmt_td(td: timedelta) -> str:
    m = int(td.total_seconds() // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


# --- anchor status words -----------------------------------------------------


def anchor_status(a: anchors_mod.Anchor) -> str:
    """One word (or two) for an anchor's liveness, so a reader can skim:
    merged, open, draft, closed, gone (merged), gone, dirty, unpushed,
    unmerged, exists, unknown. Worse news wins where several apply: a dirty
    worktree with unpushed commits reads `dirty`."""
    lv = a.liveness
    merged = lv.get("merged_into_default") is True
    if a.scheme in ("gh", "pr"):
        state = str(lv.get("state") or "").upper()
        if state == "MERGED":
            return "merged"
        if state == "OPEN":
            return "draft" if lv.get("is_draft") else "open"
        if state == "CLOSED":
            return "closed"
        return "unknown"
    if a.scheme == "wt":
        if lv.get("exists") is False:
            return "gone (merged)" if merged else "gone"
        if lv.get("exists") is not True:
            return "unknown"
        if lv.get("dirty"):
            return "dirty"
        if merged:
            return "merged"
        up = lv.get("unpushed")
        if up in ("no remote", "no upstream") or (isinstance(up, int) and up > 0):
            return "unpushed"
        if lv.get("unmerged"):
            return "unmerged"
        return "exists"
    if a.scheme == "branch":
        local, remote = lv.get("exists_local"), lv.get("exists_remote")
        if not local and not remote:
            if local is None and remote is None:
                return "unknown"
            return "gone (merged)" if merged else "gone"
        if merged:
            return "merged"
        if local and not remote:
            return "unpushed"
        return "unmerged" if lv.get("merged_into_default") is False else "exists"
    if "exists" in lv:
        return "exists" if lv["exists"] else "gone"
    return "unknown"


# --- brief (the SessionStart hook's line) ------------------------------------


@dataclass
class Brief:
    """A frontier cheap enough for the SessionStart hook: weft's view of the
    workstream plus capture files on disk. No transcript reads beyond what the
    hook already did, no anchor probes, nothing that spawns gh or git."""

    name: str
    title: str
    canonical: bool
    open_tasks: int
    blocked: Optional[int]  # None: weft could not say
    loose_ends: list[str]  # those knowable without anchors; open tasks are counted above
    capture_age: Optional[timedelta]
    capture_by: Optional[str] = None  # the newest capture's `recorded_by`, if it has one


def brief(ws: Workstream, me_ref: str, as_of: Instant, blocked: Optional[set[str]]) -> Brief:
    open_ = [c for c in ws.tasks if c.state not in store.SETTLED]
    captured = _captured_sessions(ws)
    # The starting session has no capture yet by definition; it is not a loose end.
    loose = [
        f"{sid[:8]} has no recorded frontier"
        for scheme, sid in _session_refs(ws)
        if scheme == "claude" and f"claude:{sid}" != me_ref and sid not in captured
    ]
    if ws.tasks and not open_ and not ws.closed:
        loose.append(NOT_CLOSED)
    newest = newest_capture(_capture_paths(ws))
    return Brief(
        name=ws.name or ws.id[:12],
        title=ws.title,
        canonical=f"agent:{me_ref}" in ws.canonical_holders(),
        open_tasks=len(open_),
        blocked=None if blocked is None else sum(1 for c in open_ if c.id in blocked),
        loose_ends=loose,
        capture_age=(as_of.utc - datetime.fromtimestamp(newest[1], timezone.utc))
        if newest
        else None,
        capture_by=capture_meta(newest[0]).get("recorded_by") if newest else None,
    )


def brief_line(b: Brief) -> str:
    tasks = f"{b.open_tasks} task{'' if b.open_tasks == 1 else 's'} open"
    if b.open_tasks:
        tasks += f" ({'blocked unknown' if b.blocked is None else f'{b.blocked} blocked'})"
    n = len(b.loose_ends)
    loose = f"{n} loose end{'' if n == 1 else 's'} before anchor checks"
    cap = (
        f"newest capture {_fmt_td(b.capture_age)} ago"
        if b.capture_age is not None
        else "no capture yet"
    )
    # A hook capture is a machine snapshot taken because nobody recorded one on
    # purpose; the reader should know the prose frontier may be behind it.
    if b.capture_age is not None and b.capture_by == BY_SESSION_END:
        cap += " (by the SessionEnd hook, not recorded on purpose)"
    elif b.capture_age is not None and b.capture_by:
        cap += f" (by `{b.capture_by}`)"
    role = "canonical" if b.canonical else "ancillary"
    return f"- {b.name} ({role}): {b.title}; {tasks}; {loose}; {cap}. Full frontier: `workstream frontier {b.name}`"


# --- capture -----------------------------------------------------------------


def slug_for(
    ws: Workstream, sfs: list[SessionFrontier], root: Path, explicit: Optional[str] = None
) -> str:
    """The project-docs slug: the one asked for, else the basename of the repo
    the workstream's sessions worked in most, if a docs dir exists for it,
    else `_shared`."""
    if explicit:
        return explicit
    counts: dict[str, int] = {}
    for s in sfs:
        for r in s.facts.repos:
            counts[os.path.basename(r.rstrip("/"))] = (
                counts.get(os.path.basename(r.rstrip("/")), 0) + 1
            )
    for name, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
        if (root / name).is_dir():
            return name
    return "_shared"


def to_dict(fr: Frontier) -> dict:
    def inst(i: Optional[Instant]):
        return i.iso() if i else None

    return {
        "workstream": {
            "id": fr.ws.id,
            "name": fr.ws.name,
            "title": fr.ws.title,
            "state": fr.ws.state,
            "closed_note": fr.ws.closed_note,
        },
        "as_of": inst(fr.as_of),
        "sessions": [
            {
                "id": s.facts.id,
                "ref": s.facts.ref,
                "kind": s.facts.kind,
                "role": s.role,
                "live": s.live,
                "dropped": s.dropped,
                "lifecycle": s.lifecycle,
                "lifecycle_at": inst(s.lifecycle_at),
                "started": inst(s.facts.started),
                "last_seen": inst(s.facts.last_seen),
                "peak_context": s.facts.peak_context,
                "compaction_count": s.facts.compaction_count,
                "context_after_compaction": s.facts.context_after_compaction,
                "model": s.facts.model,
                "cwd": s.facts.cwd,
                "repos": s.facts.repos,
                "prs": s.facts.work.prs if s.facts.work else [],
                "branches": s.facts.work.branches if s.facts.work else [],
                "worktrees": s.facts.worktrees,
                "handoff_files": s.facts.handoff_files,
                "human_prompts": [(inst(a), t) for a, t in s.facts.human_prompts],
                "last_assistant": [inst(s.facts.last_assistant[0]), s.facts.last_assistant[1]]
                if s.facts.last_assistant
                else None,
                "last_tools": [(inst(a), n, t) for a, n, t in s.facts.last_tools],
                "subagents": s.facts.subagents,
                "parent_id": s.facts.parent_id,
                "intervals": [
                    {
                        "at": inst(i.at),
                        "expires": inst(i.expires),
                        "released_at": inst(i.released_at),
                        "note": i.note,
                    }
                    for i in s.intervals
                ],
                # The windows the fields above are cut to; None: the whole session.
                "scope": None if s.scope is None else [[_iso(a), _iso(b)] for a, b in s.scope],
            }
            for s in fr.sessions
        ],
        "anchors": [{**asdict(a), "status": anchor_status(a)} for a in fr.anchors],
        "messages": [
            {
                "at": inst(m.at),
                "from": m.from_sid,
                "to": m.to_sid,
                "to_raw": m.to_raw,
                "summary": m.summary,
                "how": m.how,
            }
            for m in fr.messages
        ],
        "timeline": [(inst(a), k, t) for a, k, t in fr.timeline],
        "tasks": [asdict(t) for t in fr.tasks],
        "loose_ends": fr.loose_ends,
        "closure_blockers": fr.closure_blockers,
        "options": {
            "session_id": fr.options.session_id,
            "default": fr.options.default,
            "default_why": fr.options.default_why,
            **{
                k: (asdict(o) if o else None)
                for k, o in (
                    ("message", fr.options.message),
                    ("fresh", fr.options.fresh),
                    ("resume", fr.options.resume),
                )
            },
        },
        # Derived from options.default for readers of the pre-options shape.
        "recommendation": asdict(fr.recommendation),
        "thresholds": {
            "RESUME_MAX_CONTEXT": RESUME_MAX_CONTEXT,
            "CACHE_TTL_MIN": int(CACHE_TTL.total_seconds() // 60),
        },
        "warnings": fr.warnings,
    }


def capture(
    fr: Frontier,
    *,
    root: Optional[Path] = None,
    slug: Optional[str] = None,
    record: bool = True,
    recorded_by: str = BY_RECORD,
) -> Path:
    """Write the evidence as JSON under project-docs and bind it to the
    workstream as a `frontier:` ref, so `dropped` can be answered later.
    `recorded_by` says which path wrote it (BY_*)."""
    root = root or docs_root()
    name = fr.ws.name or fr.ws.id[:12]
    d = root / slug_for(fr.ws, fr.sessions, root, slug) / "workstreams" / name / "captures"
    d.mkdir(parents=True, exist_ok=True)
    stamp = fr.as_of.utc.strftime("%Y%m%dT%H%M%SZ")
    newest = fr.sessions[0].facts.id[:8] if fr.sessions else "none"
    path = d / f"{stamp}-{newest}.json"
    data = {**to_dict(fr), "recorded_by": recorded_by}
    # Write beside the target and rename: a hook killed mid-write then leaves
    # either nothing or a whole file, never a truncated capture.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if record:
        store.record_frontier(fr.ws, str(path))
    return path
