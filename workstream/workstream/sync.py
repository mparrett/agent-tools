"""Accrue a workstream's anchors from what its sessions acted on.

Anchors used to arrive only when someone ran `attach --anchor`, so a
workstream silently missed the PR its own session opened an hour later. The
evidence was already in the transcript: `sessions.facts` records every write
a session's tools made (`SessionFacts.acted`). This turns that evidence into
refs.

It also takes away: a `wt:` anchor whose directory is gone and whose branch
merged is retracted, since retiring the worktree after the merge is how a
slice ends. The captures already written still name it.

Strong evidence only, because an anchor is a claim about what the work *is*:
a PR the session opened, merged, reviewed or commented on; a branch it
committed or pushed on; a worktree it created, entered, edited in or committed
in; and a PR named in the transcript tail when the session also wrote to that
PR's repo. Repo overlap, shared files and cwd rank candidates in `attribution`
and never become anchors here: a session that read a repo did not do its work.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from workstream import anchors, frontier, seam, sessions, store
from workstream.clock import Instant
from workstream.sessions import _REPO_HASH_NUM_RE, SessionFacts

SESSION_SCHEMES = ("claude", "codex", "agent")


@dataclass
class Found:
    """One candidate anchor and every write that justified it."""

    ref: str
    session: str
    how: list[str] = field(default_factory=list)
    at: Optional[Instant] = None
    # Set when the evidence could not become a ref (repo unknown, worktree
    # gone, a mention in a repo the session never wrote to).
    unusable: str = ""


@dataclass
class Report:
    added: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    by_session: dict[str, list[dict]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Ancillary sessions left out, as {workstream, session}: see `owners`.
    ancillary: list[dict] = field(default_factory=list)
    # Gone worktrees whose branch merged, as {workstream, ref, branch, how, reason}.
    retracted: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "skipped": self.skipped,
            "by_session": self.by_session,
            "warnings": self.warnings,
            "ancillary": self.ancillary,
            "retracted": self.retracted,
        }


# --- resolving evidence to refs ----------------------------------------------


class _Repos:
    """Local dir -> canonical root and GitHub slug, asked of git once per root."""

    def __init__(self) -> None:
        self._slug: dict[str, Optional[str]] = {}

    @staticmethod
    def root(path: str) -> Optional[str]:
        if not path:
            return None
        return seam.canonical_repo_root(path) or seam.repo_root_for(path)

    def slug(self, path: str) -> Optional[str]:
        root = self.root(path)
        if not root:
            return None
        if root not in self._slug:
            rc, url = anchors._git(root, "remote", "get-url", "origin")
            slug = anchors.gh_repo_from_url(url) if rc == 0 else None
            self._slug[root] = slug if slug and sessions.valid_repo(slug) else None
        return self._slug[root]


def _push_slug(url: str) -> Optional[str]:
    """`owner/name` for a push's `To` line. git drops the user from an
    scp-style URL when it prints one (`To github.com:o/r.git`), which
    `gh_repo_from_url` needs back to recognise the form."""
    if re.match(r"^[\w.-]+:[\w.-]+/", url) and "://" not in url:
        url = "git@" + url
    slug = anchors.gh_repo_from_url(url)
    return slug if slug and sessions.valid_repo(slug) else None


def _remote_name(url: str) -> Optional[str]:
    """The repo name a push URL ends in: `git@host:o/kiln.git` -> `kiln`."""
    slug = _push_slug(url)
    tail = slug.rsplit("/", 1)[-1] if slug else os.path.basename(url.rstrip("/"))
    return tail.removesuffix(".git") or None


def _tail_prose(sf: SessionFacts, wins: Optional[list[store.Window]] = None) -> str:
    """Prompts, assistant text and shell commands from the transcript tail,
    within `wins` when given (store.session_windows).

    Tool results are left out on purpose: `gh pr list --json url` prints every
    open PR in a repo, and the session doing that acted on none of them. So are
    other tool inputs: an Edit's `new_string` to a CHANGELOG names every PR in
    it, and writing a file is not naming the work. Codex patches likewise."""
    try:
        recs = seam.tail_entries(Path(sf.path))
    except (OSError, TypeError):
        return ""
    parts: list[str] = []
    for rec in recs:
        if wins is not None:
            at = sessions._instant(rec.get("timestamp"))
            if not store.in_windows(at.utc if at else None, wins):
                continue
        payload = rec.get("payload")
        if isinstance(payload, dict):  # Codex
            if payload.get("type") in ("user_message", "agent_message") and isinstance(
                payload.get("message"), str
            ):
                parts.append(payload["message"])
            elif payload.get("type") == "function_call":
                parts.append(_codex_shell(payload.get("arguments")))
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif b.get("type") == "tool_use":
                    tin = b.get("input")
                    cmd = tin.get("command") if isinstance(tin, dict) else None
                    if isinstance(cmd, str):
                        parts.append(sessions.strip_heredocs(cmd))
    return "\n".join(parts)


def _codex_shell(raw) -> str:
    """The shell text of a Codex exec call, minus `apply_patch`, whose heredoc
    is a file body rather than a command."""
    try:
        args = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return ""
    cmd = sessions._codex_command(args) if isinstance(args, dict) else ""
    return "" if "apply_patch" in cmd else sessions.strip_heredocs(cmd)


def evidence(
    sf: SessionFacts,
    repos: Optional[_Repos] = None,
    wins: Optional[list[store.Window]] = None,
) -> list[Found]:
    """Candidate anchors for one session, in transcript order, each once.
    `wins` (store.session_windows) keeps only the writes and tail text inside
    them: a session that moved on to a second workstream gives each the work
    it did while attached there."""
    repos = repos or _Repos()
    sf = frontier.scoped(sf, wins)
    by_ref: dict[str, Found] = {}
    order: list[str] = []

    def note(ref: str, how: str, at: Optional[Instant], unusable: str = "") -> None:
        f = by_ref.get(ref)
        if f is None:
            f = by_ref[ref] = Found(ref, sf.ref, at=at, unusable=unusable)
            order.append(ref)
        if how not in f.how:
            f.how.append(how)

    wrote_to: set[str] = set()  # GitHub slugs the session wrote to
    for a in sf.acted:
        if a.kind == "pr":
            slug, _, num = a.value.partition("#")
            slug = slug or repos.slug(a.dir) or ""
            if slug:
                wrote_to.add(slug)
                note(f"gh:{slug}#{num}", a.how, a.at)
            else:
                note(f"gh:?#{num}", a.how, a.at, "no --repo and no GitHub origin to name its repo")
        elif a.kind == "branch":
            root = repos.root(a.dir)
            name = os.path.basename(root.rstrip("/")) if root else _remote_name(a.repo or "")
            slug = repos.slug(a.dir) or _push_slug(a.repo or "")
            if slug:
                wrote_to.add(slug)
            if name:
                note(f"branch:{name}@{a.value}", a.how, a.at)
            else:
                note(f"branch:?@{a.value}", a.how, a.at, "the repo it ran in is gone")
        elif a.kind == "wt":
            slug = repos.slug(a.value)
            if slug:
                wrote_to.add(slug)
            ref = store._canon_ref(f"wt:{a.value}")
            if os.path.isdir(a.value):
                note(ref, a.how, a.at)
            else:
                note(ref, a.how, a.at, "the worktree no longer exists")
        elif a.kind == "repo":
            slug = _push_slug(a.value)
            if slug:
                wrote_to.add(slug)
    for p in sf.work.edited_paths if sf.work else []:
        slug = repos.slug(p)
        if slug:
            wrote_to.add(slug)

    text = _tail_prose(sf, wins)
    named: list[str] = []
    for m in seam.PR_URL_RE.finditer(text):
        named.append(f"{m.group(1)}/{m.group(2)}#{m.group(3)}")
    for m in _REPO_HASH_NUM_RE.finditer(text):
        named.append(f"{m.group(1)}#{m.group(2)}")
    for pr in dict.fromkeys(named):
        ref = f"gh:{pr}"
        slug = pr.partition("#")[0]
        if ref in by_ref or not sessions.valid_repo(slug):
            continue
        if slug in wrote_to:
            note(ref, f"named in the transcript tail; it wrote to {slug}", None)
        else:
            note(ref, "named in the transcript tail", None, f"it wrote nothing to {slug}")
    return [by_ref[r] for r in order]


# --- applying ----------------------------------------------------------------


def session_members(ws: store.Workstream) -> list[tuple[str, str]]:
    """(session ref, session id) for every session attached to `ws`."""
    out = []
    for ref in ws.refs:
        scheme, _, value = ref.partition(":")
        if scheme not in SESSION_SCHEMES:
            continue
        sid = value.partition("/")[2] if scheme == "agent" else value
        if sid:
            out.append((ref, sid))
    return out


def owners(ws: store.Workstream) -> set[str]:
    """Session refs whose evidence is the workstream's: every session that
    holds or held a canonical claim on it, released intervals included, and
    the subagents of those sessions (D9). An ancillary session is referenced,
    not owned: it helped here, and the rest of what it did that afternoon is
    other work."""
    held = {iv.holder.removeprefix("agent:") for iv in ws.intervals()}
    out = set(held)
    for ref, _ in session_members(ws):
        if ref.startswith("agent:") and f"claude:{ref[6:].partition('/')[0]}" in held:
            out.add(ref)
    return out


def _label(ws: store.Workstream) -> str:
    return ws.name or ws.id[:12]


def _anchor_refs(ws: store.Workstream) -> set[str]:
    return {store._canon_ref(r) for r in ws.refs if r.partition(":")[0] not in SESSION_SCHEMES}


def run(
    targets: Iterable[store.Workstream],
    *,
    others: Iterable[store.Workstream] = (),
    only: Optional[str] = None,
    ancillary: bool = False,
    facts: Optional[dict[str, SessionFacts]] = None,
    log=None,
    dry_run: bool = False,
    retract: bool = True,
    gh_runner=None,
    history: Optional[Iterable[store.Workstream]] = None,
    progress: Optional[Callable[[str, int, int], None]] = None,
    exclude: Iterable[tuple[str, str]] = (),
) -> Report:
    """Sync each target workstream from the sessions that own it (`owners`).
    `ancillary` adds every other attached session; `only` (a session id)
    syncs that one session alone, ancillary or not, since naming it is the
    decision. `others` are the remaining live workstreams: a ref one of
    them already holds is skipped rather than added twice, since the session
    acted on it for that other work. Canonical attachments go first, so a
    session attached to two workstreams gives its evidence to the one it owns.
    `facts` reuses SessionFacts the caller already has, by session id, and
    gains the ones read here.
    `retract` also removes each target's gone, merged worktrees (`_retract`).

    A session that holds or held a canonical claim gives only what it did
    inside its windows for that workstream (frontier.session_scope); an
    ancillary one, having none, gives its whole transcript. `history` is
    every workstream whose claims bound those windows, closed ones included;
    it defaults to `targets` and `others`. `progress(step, done, total)` is
    called before each session is read and each workstream's gone worktrees
    are checked, since a sweep reads transcripts for seconds at a time.
    `exclude` holds (workstream label, ref) pairs a person declined: each is
    skipped as declined and, never being held, stays open to the next
    workstream that has evidence for it."""
    exclude = set(exclude)
    targets = list(targets)
    others = list(others)
    history = list(history) if history is not None else [*targets, *others]
    # Filled in place, so a caller sweeping twice reads each transcript once.
    facts = facts if facts is not None else {}
    report = Report()
    held: dict[str, str] = {}
    for ws in [*others, *targets]:
        for r in _anchor_refs(ws):
            held.setdefault(r, _label(ws))
    have = {ws.id: _anchor_refs(ws) for ws in targets}

    pairs = []
    for ws in targets:
        canon = set(ws.canonical_holders())
        owned = owners(ws)
        for ref, sid in session_members(ws):
            if only and sid != only:
                continue
            if not (only or ancillary or ref in owned):
                report.ancillary.append({"workstream": _label(ws), "session": ref})
                continue
            pairs.append((f"agent:{ref}" not in canon, ws, ref, sid))
    pairs.sort(key=lambda p: p[0])  # stable: canonical first, then given order

    repos = _Repos()
    # By session and window: one session's evidence differs per workstream.
    found_by: dict[tuple, list[Found]] = {}
    missing: set[str] = set()
    for n, (_, ws, sref, sid) in enumerate(pairs):
        if progress:
            progress(f"{_label(ws)}: {sref}", n, len(pairs))
        if sid in missing:
            continue
        wins = frontier.session_scope(sref, ws, history)
        key = (sid, tuple(wins) if wins is not None else None)
        if key not in found_by:
            sf = facts.get(sid)
            if sf is None:
                try:
                    sf = facts[sid] = sessions.facts(sid, log=log)
                except (sessions.NotFound, sessions.Ambiguous) as e:
                    report.warnings.append(f"{sref}: transcript not found ({e})")
                    missing.add(sid)
                    continue
            found_by[key] = evidence(sf, repos, wins)
        label = _label(ws)
        for f in found_by[key]:
            canon = store._canon_ref(f.ref)
            row = {
                "workstream": label,
                "ref": canon,
                "session": f.session,
                "how": ", ".join(f.how),
                "at": f.at.iso() if f.at else None,
            }
            if f.unusable:
                reason = f.unusable
            elif canon in have[ws.id]:
                reason = "already an anchor"
            elif (label, canon) in exclude:
                reason = "declined"
            elif canon in held and held[canon] != label:
                reason = f"an anchor of {held[canon]}"
            else:
                reason = ""
            if reason:
                row["reason"] = reason
                report.skipped.append(row)
                report.by_session.setdefault(f.session, []).append({**row, "result": "skipped"})
                continue
            if not dry_run:
                store.add_anchor(ws, canon, source=store.SYNC_SOURCE)
            have[ws.id].add(canon)
            held[canon] = label
            report.added.append(row)
            report.by_session.setdefault(f.session, []).append({**row, "result": "added"})
    if retract:
        for n, ws in enumerate(targets):
            if progress:
                progress(f"{_label(ws)}: gone worktrees", n, len(targets))
            _retract(ws, facts, report, log=log, dry_run=dry_run, gh_runner=gh_runner)
    return report


def _retract(
    ws: store.Workstream,
    facts: dict[str, SessionFacts],
    report: Report,
    *,
    log=None,
    dry_run: bool,
    gh_runner=None,
) -> None:
    """Retract `wt:` anchors whose directory is gone and whose branch merged
    (frontier.settle_gone_worktrees). Every attached session's evidence counts
    here, not only the owners': the question is what the directory held, not
    whose work it was. Nothing is read unless a worktree is actually gone."""
    gone = [r for r in ws.refs if r.startswith("wt:") and not os.path.isdir(r[3:])]
    if not gone:
        return
    members = []
    for sref, sid in session_members(ws):
        sf = facts.get(sid)
        if sf is None:
            try:
                sf = facts[sid] = sessions.facts(sid, log=log)
            except (sessions.NotFound, sessions.Ambiguous) as e:
                report.warnings.append(f"{sref}: transcript not found ({e})")
                continue
        members.append(sf)
    hints: dict[str, str] = {}
    for sf in members:
        for r in sf.repos:
            hints.setdefault(os.path.basename(r.rstrip("/")), r)
    for name, root in frontier.worktree_repo_hints(ws.refs).items():
        hints.setdefault(name, root)
    others = [r for r in ws.refs if r.partition(":")[0] in ("branch", "gh")]
    probed = anchors.liveness_many([*gone, *others], repo_hints=hints, gh_runner=gh_runner)
    frontier.settle_gone_worktrees(probed, members, hints=hints, gh_runner=gh_runner)
    for a in probed:
        if not frontier.gone_and_merged(a):
            continue
        lv = a.liveness
        if frontier.merged_by_name_only(a):
            # A reused branch name can make an old PR look like this
            # worktree's merge; retracting would lose the only pointer to
            # work that may never have landed. Settled for display, kept here.
            report.skipped.append(
                {
                    "workstream": _label(ws),
                    "ref": a.ref,
                    "session": f"branch {lv.get('branch')}",
                    "how": lv.get("branch_from") or "",
                    "at": None,
                    "reason": "gone, merged by branch name only (head_only); not retracted",
                }
            )
            continue
        if not dry_run:
            store.rm_anchor(ws, a.ref)
        report.retracted.append(
            {
                "workstream": _label(ws),
                "ref": a.ref,
                "branch": lv.get("branch"),
                "how": lv.get("branch_from"),
                "reason": lv.get("status"),
            }
        )
