"""Which workstream does a session belong to? (spec D4, D9, D12)

Ranking is evidence-first: a pasted session id, a shared PR, a branch or a
worktree each name the work directly; where the session's tools acted says
which repos it touched; shared files say almost nothing, because scaffolding
files (CLAUDE.md, MEMORY.md, READMEs) bridge every session on the machine into
one blob. cwd and the project slug are never a signal on their own: the mined
cases include a session launched in one repo that did all its work in another.

Every candidate carries human-readable reasons citing the evidence, because the
agent reading them at session start must decide whether the match is real, and
a bare score cannot be argued with.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from workstream import seam
from workstream.sessions import SessionFacts
from workstream.store import Workstream

# Weights, highest first. The cap on each keeps one noisy signal (a session that
# pasted five ids) from drowning a stronger one.
W_PASTED = 1.0
W_PASTED_MAX = 3.0
W_LINEAGE = 0.9
W_PR = 0.9
W_BRANCH = 0.8
W_WORKTREE = 0.8
W_PARENT = 0.95
W_MESSAGE = 0.5
# An anchor named in the session's recent transcript: a `gh pr view 203`, a
# worktree path in `git worktree list` output, a `--head mixer-seam`. Weaker
# than having touched the anchor, stronger than merely sharing a repo.
W_MENTION_PR = 0.7
W_MENTION_WT = 0.7
W_MENTION_BRANCH = 0.6
MENTION_MIN_BRANCH_LEN = 6
W_REPO = 0.3
W_REPO_MAX = 0.6
W_FILE = 0.05
W_FILE_MAX = 0.2

# Files that every session touches and no workstream owns.
SCAFFOLDING = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    "MEMORY.md",
    "AGENTS.md",
    "README.md",
    "justfile",
    "*.lock",
    "*/memory/*",
    "*/.claude/*",
)


@dataclass
class Candidate:
    ws: Workstream
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def add(self, weight: float, reason: str) -> None:
        self.score += weight
        self.reasons.append(reason)


@dataclass
class Membership:
    """A workstream's members as the ranker sees them: session ids attached by
    ref, plus the anchors it holds, each already split by scheme."""

    ws: Workstream
    sessions: set[str] = field(default_factory=set)
    prs: set[str] = field(default_factory=set)
    branches: set[str] = field(default_factory=set)
    worktrees: set[str] = field(default_factory=set)
    repos: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, ws: Workstream) -> "Membership":
        m = cls(ws)
        for ref in ws.refs:
            scheme, _, value = ref.partition(":")
            if scheme in ("claude", "codex"):
                m.sessions.add(value)
            elif scheme == "agent":
                # agent:<parent>/<id>: both ids count as members.
                parent, _, agent = value.partition("/")
                m.sessions.update(x for x in (parent, agent) if x)
            elif scheme in ("gh", "pr"):
                m.prs.add(value)
            elif scheme == "branch":
                m.branches.add(value)
            elif scheme == "wt":
                m.worktrees.add(_norm_path(value))
                root = _repo_of_worktree(value)
                if root:
                    m.repos.add(root)
            elif scheme == "repo":
                m.repos.add(_norm_path(value))
        return m


def _norm_path(p: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(p))
    except OSError:
        return p


def _repo_of_worktree(path: str) -> Optional[str]:
    # A worktree under <repo>/.claude/worktrees/<name> belongs to <repo>; naming
    # the repo lets repo overlap count for a session working in a sibling
    # worktree. Worktrees kept outside the repo carry no such marker.
    p = _norm_path(path)
    marker = f"{os.sep}.claude{os.sep}worktrees{os.sep}"
    if marker in p:
        return p.split(marker, 1)[0]
    return None


def _short(sid: str) -> str:
    return sid[:8]


def is_scaffolding(path: str) -> bool:
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(path, pat) for pat in SCAFFOLDING)


def _branch_refs(session: SessionFacts) -> set[str]:
    """`branch:<repo-basename>@<branch>` values a session's checkouts imply."""
    out: set[str] = set()
    if not session.work:
        return out
    repos = [os.path.basename(r.rstrip("/")) for r in session.repos] or [
        os.path.basename(session.cwd.rstrip("/"))
    ]
    for b in session.work.branches:
        for r in repos:
            if r:
                out.add(f"{r}@{b}")
    return out


def tail_text(session: SessionFacts) -> str:
    """Every text fragment in the transcript's tail, joined: prompts, replies,
    commands and their results, which is where a fresh session's first lookup
    (`gh pr view 203`, `git worktree list`) leaves its trace."""
    try:
        recs = seam.tail_entries(Path(session.path))
    except (OSError, TypeError):
        return ""
    parts: list[str] = []
    for rec in recs:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    parts.append(str(block.get("input", "")))
                elif block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, str):
                        parts.append(c)
                    elif isinstance(c, list):
                        parts.extend(str(b.get("text", "")) for b in c if isinstance(b, dict))
    return "\n".join(parts)


def mentions(text: str, m: "Membership") -> list[tuple[float, str]]:
    """(weight, reason) for each of the membership's anchors the text names."""
    out: list[tuple[float, str]] = []
    if not text:
        return out
    for pr in sorted(m.prs):
        repo, _, num = pr.partition("#")
        # The digit guard keeps #57 from matching inside #570.
        named = bool(re.search(rf"{re.escape(repo)}(?:#|/pull/){num}(?!\d)", text))
        if not named and repo in text and re.search(rf"(?<![\w#/]){num}(?!\d)", text):
            named = True
        if named:
            out.append((W_MENTION_PR, f"names {pr}, an anchor here"))
    for wt in sorted(m.worktrees):
        if (
            wt in text
            or os.path.basename(wt) in text
            and os.path.dirname(wt).split(os.sep)[-1] in text
        ):
            out.append((W_MENTION_WT, f"names worktree {wt}, an anchor here"))
    for b in sorted(m.branches):
        name = b.partition("@")[2]
        if len(name) >= MENTION_MIN_BRANCH_LEN and re.search(
            rf"(?<![\w/-]){re.escape(name)}(?![\w-])", text
        ):
            out.append((W_MENTION_BRANCH, f"names branch {name}, an anchor here"))
    return out


def rank(
    session: SessionFacts,
    workstreams: Iterable[Workstream],
    recent: Iterable[SessionFacts] = (),
    messages: Iterable = (),
) -> list[Candidate]:
    """Rank `workstreams` for `session`, best first; zero-score candidates are
    dropped. `recent` supplies other sessions' facts for the file-overlap and
    lineage signals; `messages` are hooklog MessageEvents involving `session`."""
    members = [Membership.of(ws) for ws in workstreams]
    by_sid: dict[str, list[Membership]] = {}
    for m in members:
        for sid in m.sessions:
            by_sid.setdefault(sid, []).append(m)
    recent_by_id = {s.id: s for s in recent}
    cands = {m.ws.id: Candidate(m.ws) for m in members}

    def attached_to(sid: str) -> list[Membership]:
        hits = by_sid.get(sid, [])
        if hits:
            return hits
        # Refs carry full ids; evidence may carry a prefix.
        return [m for k, ms in by_sid.items() if k.startswith(sid) or sid.startswith(k) for m in ms]

    # Pasted session ids: the single best linkage signal in the mined store.
    pasted_hits: dict[str, float] = {}
    for sid, at in session.pasted_sessions:
        for m in attached_to(sid):
            if pasted_hits.get(m.ws.id, 0.0) >= W_PASTED_MAX:
                continue
            pasted_hits[m.ws.id] = pasted_hits.get(m.ws.id, 0.0) + W_PASTED
            when = at.iso() if at else "unknown time"
            cands[m.ws.id].add(W_PASTED, f"pasted session {_short(sid)} at {when}")

    # Lineage: the root this was /clear'ed from, the pane chain, fork siblings.
    lineage = [x for x in [session.origin, *session.chain, *session.fork_siblings] if x]
    seen_lineage: set[str] = set()
    for sid in lineage:
        if sid == session.id or sid in seen_lineage:
            continue
        seen_lineage.add(sid)
        for m in attached_to(sid):
            how = "fork sibling" if sid in session.fork_siblings else "same pane lineage"
            cands[m.ws.id].add(W_LINEAGE, f"{how} {_short(sid)} is attached")

    # Subagents inherit their parent's workstream (D9).
    if session.kind in ("subagent", "codex") and session.parent_id:
        for m in attached_to(session.parent_id):
            cands[m.ws.id].add(W_PARENT, f"parent session {_short(session.parent_id)} is attached")

    # Anchors the session touched that the workstream holds.
    prs = set(session.work.prs) if session.work else set()
    for m in members:
        for pr in sorted(prs & m.prs):
            cands[m.ws.id].add(W_PR, f"touched {pr}, an anchor here")
        for b in sorted(_branch_refs(session) & m.branches):
            cands[m.ws.id].add(W_BRANCH, f"checked out branch {b}, an anchor here")
        wts = {_norm_path(w) for w in session.worktrees}
        if session.cwd:
            wts.add(_norm_path(session.cwd))
        for w in sorted(m.worktrees):
            if any(x == w or x.startswith(w + os.sep) for x in wts):
                cands[m.ws.id].add(W_WORKTREE, f"worked in worktree {w}, an anchor here")

    # Anchors named in the recent transcript.
    text = tail_text(session)
    for m in members:
        for w, reason in mentions(text, m):
            cands[m.ws.id].add(w, reason)

    # Cross-session messages with an attached session: one reason per peer,
    # scored once, so a long thread does not outrank a shared PR.
    peers: dict[str, int] = {}
    for ev in messages:
        if getattr(ev, "own_subagent", False):
            continue  # a session steering its own subagent is not cross-session evidence
        other = None
        if ev.from_sid == session.id and ev.to_sid:
            other = ev.to_sid
        elif ev.to_sid == session.id:
            other = ev.from_sid
        if other:
            peers[other] = peers.get(other, 0) + 1
    for other, n in peers.items():
        for m in attached_to(other):
            times = f" ({n} messages)" if n > 1 else ""
            cands[m.ws.id].add(W_MESSAGE, f"messaged {_short(other)}, which is attached{times}")

    # Repo overlap, from where tools acted (never cwd alone).
    session_repos = {_norm_path(r) for r in session.repos}
    for m in members:
        if not m.sessions:
            continue
        ws_repos = set(m.repos)
        for sid in m.sessions:
            other = recent_by_id.get(sid)
            if other:
                ws_repos.update(_norm_path(r) for r in other.repos)
        shared = sorted(session_repos & ws_repos)
        if shared:
            w = min(W_REPO * len(shared), W_REPO_MAX)
            names = ", ".join(os.path.basename(r) for r in shared)
            cands[m.ws.id].add(w, f"acted in {names}, where its sessions worked")

    # Shared files, scaffolding excluded, heavily down-weighted.
    my_files = {
        p for p in (session.work.edited_paths if session.work else []) if not is_scaffolding(p)
    }
    if my_files:
        for m in members:
            shared: set[str] = set()
            for sid in m.sessions:
                other = recent_by_id.get(sid)
                if other and other.work:
                    shared |= my_files & {
                        p for p in other.work.edited_paths if not is_scaffolding(p)
                    }
            if shared:
                w = min(W_FILE * len(shared), W_FILE_MAX)
                cands[m.ws.id].add(w, f"edited {len(shared)} file(s) its sessions also edited")

    # Already a member: say so, but let evidence rank it.
    for m in members:
        if session.id in m.sessions:
            cands[m.ws.id].reasons.insert(0, "already attached")

    ranked = [c for c in cands.values() if c.score > 0 or "already attached" in c.reasons]
    ranked.sort(key=lambda c: (-c.score, c.ws.title))
    return ranked
