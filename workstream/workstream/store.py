"""D2: the weft adapter. weft holds workstream identity and membership.

Every call shells out to `weft <cmd> ... --json` with a timeout, and branches
on weft's exit code, never its stderr (weft's own contract). The environment
passes through untouched, so `WEFT_HOME`, `WEFT_ACTOR` and `WEFT_NS` reach
weft as the caller set them; `actor=` overrides `WEFT_ACTOR` for one call.
With neither, a call made inside a Claude Code session is attributed to that
session (`_default_actor`), and one from a plain terminal falls to weft's own
default, the git email.

Shapes this module relies on (checked against weft on 2026-11-02):
- `add`, `ref add`, `release`, `ls`: `{"nodes": [node, ...]}`; `release` adds
  `"released": bool`, `ref add` adds `"bound": bool`. `add` omits
  `created`/`touched`, so `create` re-reads the node with `show`.
- node: `id, ns, kind, title, state, by, source, refs: [str], created, touched`.
  `refs` is absent when a node has none.
- `show`: `{"node": node, "claims"?: [claim], "edges"?: [edge]}`; claim is
  `holder, by, source, note?, at, expires, live`; edge is
  `kind, direction ("in"|"out"), reading, id, title, known` (no kind or
  state of the other node, hence the one `export` in `_children`).
- `export --json --include all`: `{"nodes": [...], "edges": [...]}`; there a
  node's `refs` are `[{"ref": str, ...}]` and its `claims` lack `live`.
  `--include claims` (or `all`) also adds `released_claims`, oldest first:
  claim fields plus `released_at, released_by, released_source`, `note`
  kept. A holder that claimed, released and claimed again has one entry in
  each array. `show` never lists released claims, and older weft builds
  omits the array (its fold dropped a claim on release).
- `done`: `{"done": bool, "nodes": [node]}`; idempotent on a done node.
  No `--note`: a closure note is its own node (see `close`).
- `ls --blocked`: `{"nodes": [...]}`, open nodes with an unresolved
  `depends-on`; a `part-of` parent never blocks its children.
- errors under `--json`: `{"error": str, "code": int, "candidates"?: [id]}`
  on stdout. Usage errors (exit 1) print plain text to stderr instead.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple, Optional, Union

from workstream import seam
from workstream.clock import Instant

NS = "workstreams"
TIMEOUT = 10.0
UNASSOCIATED_PREFIX = "unassociated: "
CLOSED_PREFIX = "closed: "
# weft states a node can leave open for; a task in either is settled for closure.
SETTLED = ("done", "dropped")
# Bound on detach's release loop, in case weft ever reports released=true forever.
MAX_RELEASES = 50
# How `sync` writes an anchor. weft's `source` is a closed set (observed,
# asserted, inferred) and rejects anything else, so "added by sync" is spelled
# `inferred`: nothing else in this tool writes a ref that way. `--anchor` writes
# `asserted` and session membership `observed`.
SYNC_SOURCE = "inferred"
# weft's event-log schema version this reader folds (EVENTS.md §1.3).
LOG_VERSION = 1


class WeftError(Exception):
    """weft failed. `code` is weft's exit code, or None when it never answered
    (missing binary, timeout, unparseable output)."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class NotFound(WeftError):
    pass


class Ambiguous(WeftError):
    def __init__(
        self, message: str, code: Optional[int] = 4, candidates: Optional[list[str]] = None
    ):
        super().__init__(message, code)
        self.candidates = candidates or []


class ExternalFailed(WeftError):
    pass


class StoreError(WeftError):
    """weft's log is corrupt or locked: a human has to look."""


@dataclass(frozen=True)
class Claim:
    holder: str
    at: Instant
    expires: Optional[Instant]
    note: Optional[str] = None
    live: bool = True
    source: Optional[str] = None
    released_at: Optional[Instant] = None


@dataclass(frozen=True)
class AttachInterval:
    holder: str
    at: Instant
    expires: Optional[Instant]
    # None while the claim is held or merely expired, and for every interval
    # when weft predates `released_claims` (see attach_intervals).
    released_at: Optional[Instant] = None
    note: Optional[str] = None


# A stretch of a session's transcript that counts for one workstream, in UTC.
# None at the start: from the session's own start; None at the end: still open.
Window = tuple[Optional[datetime], Optional[datetime]]


class Child(NamedTuple):
    """A node `part-of` a workstream: a sub-workstream, or a task (any other kind)."""

    id: str
    title: str
    kind: str
    state: str


@dataclass
class Workstream:
    id: str
    name: Optional[str]
    title: str
    state: str
    created: Optional[Instant]
    touched: Optional[Instant]
    refs: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    closed_note: Optional[str] = None
    # Claims let go of, oldest first; `claims` stays the current ones (held or
    # expired). Empty when weft predates `released_claims`.
    released: list[Claim] = field(default_factory=list)

    def canonical_holders(self) -> list[str]:
        return [c.holder for c in self.claims if c.live]

    def intervals(self) -> list[AttachInterval]:
        """Every canonical attach this workstream knows of, oldest first."""
        return _intervals([*self.claims, *self.released])

    @property
    def closed(self) -> bool:
        return self.state == "done"

    @property
    def tasks(self) -> list[Child]:
        return [c for c in self.children if c.kind != "workstream"]

    @property
    def sub_workstreams(self) -> list[Child]:
        return [c for c in self.children if c.kind == "workstream"]


WsArg = Union[Workstream, str]


def _interval_end(iv: AttachInterval, now: datetime) -> Optional[datetime]:
    """When a claim stopped counting: its release, else its expiry once that
    has passed (an expired claim was let go of without a release), else None."""
    if iv.released_at is not None:
        return iv.released_at.utc
    if iv.expires is not None and iv.expires.utc <= now:
        return iv.expires.utc
    return None


def session_windows(
    session_ref: str,
    ws: Workstream,
    history: Iterable[Workstream] = (),
    now: Optional[datetime] = None,
) -> Optional[list[Window]]:
    """The stretches of a session's transcript that are `ws`'s work, one per
    canonical claim it holds or held there; None when it never held one
    (ancillary: referenced, not owned, so there is no interval to scope by).

    A window ends at the claim's release, or at its expiry when it lapsed
    unreleased, and is open while the claim is held. It starts at the claim,
    reaching back over the gap before it: to where the session's previous
    claim on any workstream in `history` ended, or to the session's start when
    it never claimed before. The work before an attach is usually the work the
    attach names (a session explores, then runs `attach --new`), and the claim
    before it already accounts for everything earlier. `attach` releases a
    session's other canonical claims (D12), so a previous claim still held is
    old data; the window then starts at its own claim rather than overlap it.
    `history` should include closed workstreams, since closing is how many
    claims end."""
    now = now or datetime.now(timezone.utc)
    holder = f"agent:{session_ref}"
    mine = [i for i in ws.intervals() if i.holder == holder]
    if not mine:
        return None
    every: dict[tuple[str, datetime], AttachInterval] = {}
    for w in [ws, *history]:
        for i in w.intervals():
            if i.holder == holder:
                every[(w.id, i.at.utc)] = i
    out: list[Window] = []
    for iv in mine:
        ends = [_interval_end(j, now) for j in every.values() if j.at.utc < iv.at.utc]
        if not ends:
            start = None
        elif any(e is None for e in ends):
            start = iv.at.utc
        else:
            start = min(iv.at.utc, max(ends))
        out.append((start, _interval_end(iv, now)))
    return out


def in_windows(at: Optional[datetime], windows: Optional[list[Window]]) -> bool:
    """Whether an event at `at` (UTC) falls in `windows`. None windows: the
    whole session counts. An event with no time cannot be placed, so a scoped
    session leaves it out."""
    if windows is None:
        return True
    if at is None:
        return False
    return any((s is None or at >= s) and (e is None or at <= e) for s, e in windows)


# --- plumbing ---------------------------------------------------------------


def _default_actor() -> Optional[str]:
    """The Claude Code session running this command, as a claim holder.

    Without it every write an agent made, a sync's inferred refs included,
    was recorded as the human's git email, so a person's sweep and an agent's
    sync could not be told apart in the log. Claude Code sets the variable in
    every Bash call, which reaches the CLI with no hook or install step; a
    plain terminal has none. A command a person types through `!` inside a
    session is attributed to the session; WEFT_ACTOR overrides that."""
    sid = _session_for_actor(os.environ.get("CLAUDE_CODE_SESSION_ID") or None)
    return _holder(f"claude:{sid}") if sid else None


@functools.lru_cache(maxsize=4)
def _session_for_actor(env_sid: Optional[str]) -> Optional[str]:
    """The live session this process belongs to, or None.

    The same resolution `_me` uses (seam.me_session_id), with one difference:
    an inherited id the live registry does not hold is dropped rather than
    trusted. A tmux pane or job started from a session keeps its
    CLAUDE_CODE_SESSION_ID after the session ends, and a person typing there
    is not that agent. Cached: a sync makes hundreds of weft calls, and the
    lookup can walk the process table."""
    try:
        registry = seam.live_registry()
        sid = seam.me_session_id(registry)
    except Exception:
        return env_sid
    if sid and registry and sid not in {r.get("sessionId") for r in registry}:
        return None
    return sid


def _run(args: list[str], *, timeout: Optional[float] = None, actor: Optional[str] = None) -> dict:
    timeout = TIMEOUT if timeout is None else timeout
    cmd = [seam.weft_bin(), *args, "--json"]
    env = dict(os.environ)
    if actor:
        env["WEFT_ACTOR"] = actor
    elif "WEFT_ACTOR" not in env:
        # Present but empty is a choice too: weft then uses its own default.
        default = _default_actor()
        if default:
            env["WEFT_ACTOR"] = default
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise WeftError(f"weft not found ({cmd[0]}): {e}") from e
    except subprocess.TimeoutExpired as e:
        raise WeftError(f"weft {args[0]} timed out after {timeout:g}s") from e
    payload: Any = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
    if proc.returncode == 0:
        if not isinstance(payload, dict):
            raise WeftError(f"weft {args[0]}: unparseable output: {proc.stdout[:200]!r}")
        return payload
    msg = (payload or {}).get("error") if isinstance(payload, dict) else None
    msg = msg or proc.stderr.strip() or f"weft {args[0]} exited {proc.returncode}"
    rc = proc.returncode
    if rc == 2:
        raise NotFound(msg, rc)
    if rc == 4:
        cands = (payload or {}).get("candidates") if isinstance(payload, dict) else None
        raise Ambiguous(msg, rc, list(cands or []))
    if rc == 7:
        raise ExternalFailed(msg, rc)
    if rc == 6:
        raise StoreError(msg, rc)
    raise WeftError(msg, rc)


def _instant(s: Optional[str]) -> Optional[Instant]:
    """weft writes RFC3339 with the writer's offset; keep both the moment and
    the offset (D0). Go may emit up to 9 fractional digits; 3.11's
    fromisoformat truncates past 6."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return Instant(dt.astimezone(timezone.utc), dt.utcoffset() or timedelta(0))


def _refs(node: dict) -> list[str]:
    # export wraps each ref in an object; every other command lists strings.
    return [r["ref"] if isinstance(r, dict) else r for r in node.get("refs") or []]


def _name(refs: list[str]) -> Optional[str]:
    names = sorted(r[3:] for r in refs if r.startswith("ws:"))
    return names[0] if names else None


def _claim(c: dict, now: datetime) -> Claim:
    expires = _instant(c.get("expires"))
    released_at = _instant(c.get("released_at"))
    if released_at is not None:
        live = False
    else:
        live = c["live"] if "live" in c else (expires is None or expires.utc > now)
    return Claim(
        holder=c["holder"],
        at=_instant(c["at"]),
        expires=expires,
        note=c.get("note"),
        live=bool(live),
        source=c.get("source"),
        released_at=released_at,
    )


def _intervals(claims: list[Claim]) -> list[AttachInterval]:
    out = [AttachInterval(c.holder, c.at, c.expires, c.released_at, c.note) for c in claims]
    return sorted(out, key=lambda i: i.at.utc)


def _workstream(
    node: dict,
    claims: list[dict] = (),
    children=(),
    closed_note: Optional[str] = None,
    released: list[dict] = (),
) -> Workstream:
    refs = _refs(node)
    now = datetime.now(timezone.utc)
    return Workstream(
        id=node["id"],
        name=_name(refs),
        title=node.get("title", ""),
        state=node.get("state", ""),
        created=_instant(node.get("created")),
        touched=_instant(node.get("touched")),
        refs=refs,
        claims=[_claim(c, now) for c in claims],
        children=list(children),
        closed_note=closed_note,
        released=[_claim(c, now) for c in released],
    )


def _children(show: dict, nodes: dict[str, dict]) -> list[Child]:
    """Inbound part-of edges: sub-workstreams and tasks. Anchors are refs,
    never part-of, so these are all the work the workstream contains. `nodes`
    is one export by id, answering every child's kind and state; a `show` per
    child made a workstream with N tasks cost N+1 weft processes on every `get`."""
    edges = [
        e
        for e in show.get("edges") or []
        if e.get("kind") == "part-of" and e.get("direction") == "in" and e.get("known", True)
    ]
    return [
        Child(
            e["id"],
            e.get("title", ""),
            nodes[e["id"]].get("kind", ""),
            nodes[e["id"]].get("state", ""),
        )
        for e in edges
        if e["id"] in nodes
    ]


def _closed_note(show: dict) -> Optional[str]:
    """The newest `closed: ...` note derived from the workstream, if any."""
    notes = [
        e.get("title", "")[len(CLOSED_PREFIX) :]
        for e in show.get("edges") or []
        if e.get("kind") == "derived-from"
        and e.get("direction") == "in"
        and e.get("title", "").startswith(CLOSED_PREFIX)
    ]
    return notes[-1] if notes else None


def _canon_ref(ref: str) -> str:
    """weft compares refs as bytes, so `wt:./x`, `wt:~/x` and a symlinked
    spelling would be three anchors for one workspace. Workspaces are always
    stored absolute and symlink-resolved."""
    if ref.startswith("wt:"):
        return "wt:" + os.path.realpath(os.path.expanduser(ref[3:]))
    return ref


def _ws_id(ws: WsArg) -> str:
    return ws.id if isinstance(ws, Workstream) else get(ws).id


def _since_arg(since: Union[str, timedelta, Instant, None]) -> Optional[str]:
    if since is None:
        return None
    if isinstance(since, str):
        return since
    if isinstance(since, Instant):
        since = datetime.now(timezone.utc) - since.utc
    hours = max(1, int(-(-since.total_seconds() // 3600)))  # round up: never narrower than asked
    return f"{hours}h"


# --- workstreams -------------------------------------------------------------


def create(
    name: str,
    title: str,
    anchors: Optional[list[str]] = None,
    source: str = "asserted",
    *,
    actor: Optional[str] = None,
) -> Workstream:
    args = ["add", title, "--kind", "workstream", "--ns", NS, "--ref", f"ws:{name}"]
    for a in anchors or []:
        args += ["--ref", _canon_ref(a)]
    args += ["--source", source]
    node = _run(args, actor=actor)["nodes"][0]
    # add's reply has no created/touched; show is the full record.
    return get(node["id"])


def export_nodes() -> dict[str, dict]:
    """One `weft export --include claims`, by node id: what `get` needs beside
    `show`. A caller resolving several workstreams fetches it once and passes
    it to each `get`."""
    return {n["id"]: n for n in _run(["export", "--include", "claims"]).get("nodes") or []}


def get(name_or_prefix: str, nodes: Optional[dict[str, dict]] = None) -> Workstream:
    """By name (`ws:<name>` ref), then by id, prefix, or any exact ref.
    `nodes` is a prior `export_nodes()`; without it, `get` exports itself."""
    tries = []
    if not name_or_prefix.startswith("ws:"):
        tries.append(f"ws:{name_or_prefix}")
    tries.append(name_or_prefix)
    last: Optional[NotFound] = None
    for key in tries:
        try:
            show = _run(["show", key])
        except NotFound as e:
            last = e
            continue
        node = show["node"]
        if node.get("kind") != "workstream":
            # A prefix that happens to hit a note or a task is not a workstream.
            last = NotFound(f"{key!r} is a {node.get('kind')}, not a workstream", 2)
            continue
        # show never lists released claims; the export that answers the
        # children's kinds carries them, so one more process covers both.
        if nodes is None:
            nodes = export_nodes()
        released = (nodes.get(node["id"]) or {}).get("released_claims") or []
        return _workstream(
            node, show.get("claims") or [], _children(show, nodes), _closed_note(show), released
        )
    raise last or NotFound(f"no workstream {name_or_prefix!r}", 2)


def list_workstreams(
    since: Union[str, timedelta, Instant, None] = None, *, include_closed: bool = False
) -> list[Workstream]:
    """Workstreams in the `workstreams` namespace. `ls` carries no claims, so a
    single export fills them in, released ones included; children are left
    empty here (use `get`).
    weft's `ls` already hides dropped nodes; closed (`done`) ones are hidden
    here unless asked for, so match and the hook stop offering finished work."""
    args = ["ls", "--ns", NS, "--kind", "workstream"]
    s = _since_arg(since)
    if s:
        args += ["--since", s]
    nodes = _run(args).get("nodes") or []
    if not include_closed:
        nodes = [n for n in nodes if n.get("state") != "done"]
    if not nodes:
        return []
    exported = {n["id"]: n for n in _run(["export", "--include", "claims"]).get("nodes") or []}
    out = []
    for n in nodes:
        e = exported.get(n["id"]) or {}
        out.append(_workstream(n, e.get("claims") or [], released=e.get("released_claims") or []))
    return out


# --- membership --------------------------------------------------------------


def _holder(session_ref: str) -> str:
    return f"agent:{session_ref}"


def attach(
    ws: WsArg,
    session_ref: str,
    canonical: bool,
    note: Optional[str] = None,
    *,
    actor: Optional[str] = None,
) -> None:
    """Membership is the ref; canonical (D12) is additionally a live claim held
    by `agent:<session_ref>`. Ancillary is the ref alone.

    A session that already holds a live claim keeps it: weft stacks a second
    claim rather than renewing one, and a later release would turn the
    stack into stints that never happened. One continuous attachment is one
    claim, so its note stays the one it was taken with."""
    current = ws if isinstance(ws, Workstream) else get(ws)
    wid = current.id
    _run(["ref", "add", wid, session_ref, "--source", "observed"], actor=actor)
    if canonical and _holder(session_ref) not in current.canonical_holders():
        args = ["claim", wid, "--holder", _holder(session_ref), "--source", "observed"]
        if note:
            args += ["--note", note]
        _run(args, actor=actor)


def detach(ws: WsArg, session_ref: str, *, actor: Optional[str] = None) -> bool:
    """Drop the session's claim, keep its ref: it stays a member, no longer
    canonical. True when any claim was released. `weft release --holder`
    selects by holder, so any caller can release a session's claim, but it
    drops one claim per call, and a session attached canonically twice holds
    two; loop until weft says nothing was released."""
    return _release_holder(_ws_id(ws), _holder(session_ref), actor=actor)


def _release_holder(wid: str, holder: str, *, actor: Optional[str] = None) -> bool:
    released = False
    for _ in range(MAX_RELEASES):
        out = _run(["release", wid, "--holder", holder], actor=actor)
        if not out.get("released"):
            break
        released = True
    return released


# --- closure -----------------------------------------------------------------


def blocked_ids() -> set[str]:
    """Ids of every node weft reports blocked, across namespaces: a task can be
    part-of a workstream from another namespace's repo."""
    return {n["id"] for n in _run(["ls", "--all-ns", "--blocked"]).get("nodes") or []}


def close(ws: WsArg, note: Optional[str] = None, *, actor: Optional[str] = None) -> Workstream:
    """Release every live claim, then mark the workstream done. Deciding
    whether it *should* close is the caller's job (cli `close` checks tasks
    and loose ends). A closed workstream keeps its refs, so `show` still
    answers who worked on it.

    `weft done` takes no note, so a note becomes a `note` node linked
    `derived-from` the workstream (not part-of: it is not work the workstream
    contains) and is itself marked done, since an open note would surface in
    `weft ls --ready` as if it were work.

    A resolved Workstream is used as given (only its id and claims matter)
    and the result is built from `done`'s reply, so closing costs no `show`.
    Its claims are the ones released, so pass one resolved just now; a
    stale snapshot would leave claims taken since then in place. The claims
    released here move to `released` stamped with the close time (`done`'s
    `touched`), a moment after weft's own `released_at`, which only an
    export could read."""
    current = ws if isinstance(ws, Workstream) else get(ws)
    holders = list(dict.fromkeys(current.canonical_holders()))
    for holder in holders:
        _release_holder(current.id, holder, actor=actor)
    if note:
        nid = _run(
            [
                "add",
                f"{CLOSED_PREFIX}{note}",
                "--kind",
                "note",
                "--ns",
                NS,
                "--source",
                "asserted",
            ],
            actor=actor,
        )["nodes"][0]["id"]
        _run(["link", nid, "derived-from", current.id], actor=actor)
        _run(["done", nid], actor=actor)
    node = _run(["done", current.id], actor=actor)["nodes"][0]
    closed_at = _instant(node.get("touched")) or Instant.now()
    released = current.released + [
        replace(c, live=False, released_at=closed_at) for c in current.claims if c.live
    ]
    return replace(
        current,
        state=node.get("state", "done"),
        touched=_instant(node.get("touched")) or current.touched,
        # weft keeps expired claims current; released ones are in `released`.
        claims=[c for c in current.claims if not c.live],
        closed_note=note or current.closed_note,
        released=released,
    )


def add_anchor(
    ws: WsArg, ref: str, *, source: str = "asserted", actor: Optional[str] = None
) -> None:
    # Anchors are refs: part-of is a single-parent forest reserved for sub-workstreams.
    _run(["ref", "add", _ws_id(ws), _canon_ref(ref), "--source", source], actor=actor)


def ref_sources(ws: WsArg) -> dict[str, str]:
    """How each of the workstream's refs was added: `sync` when every live
    `ref.add` binding it was written by sync (`SYNC_SOURCE`), else the weft
    source of the first one that was not.

    No weft command returns a ref's source (its fold keeps the binding only),
    so this reads the event log, which weft's EVENTS.md makes the contract
    other tools read: unknown events are skipped, a partial final line is
    dropped, and a newer schema version is refused. A ref bound to a node
    later merged into this one is not followed and comes back without a
    source. Empty when the log cannot be read; the caller then shows no
    provenance, which is the same as before sync existed."""
    wid = _ws_id(ws)
    try:
        log = _run(["env"]).get("log")
        with open(log, "rb") as fh:
            data = fh.read()
    except (WeftError, OSError, TypeError):
        return {}
    lines = data.split(b"\n")
    lines.pop()  # after the last LF: empty, or a write still in flight
    adds: dict[str, tuple[str, str]] = {}
    retracted: set[str] = set()
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(ev, dict) or int(ev.get("v") or 0) > LOG_VERSION:
            return {}
        kind = ev.get("ev")
        if kind == "ref.add" and ev.get("node") == wid:
            adds[ev.get("id", "")] = (ev.get("ref", ""), ev.get("source", ""))
        elif kind == "ref.retract":
            retracted.add(ev.get("target", ""))
    by_ref: dict[str, list[str]] = {}
    for eid, (ref, source) in adds.items():
        if eid not in retracted:
            by_ref.setdefault(ref, []).append(source)
    return {
        ref: "sync"
        if all(x == SYNC_SOURCE for x in srcs)
        else next(x for x in srcs if x != SYNC_SOURCE)
        for ref, srcs in by_ref.items()
    }


def rm_anchor(ws: WsArg, ref: str, *, actor: Optional[str] = None) -> None:
    _run(["ref", "rm", _ws_id(ws), _canon_ref(ref)], actor=actor)


def record_frontier(ws: WsArg, capture_path: str, *, actor: Optional[str] = None) -> None:
    _run(
        ["ref", "add", _ws_id(ws), f"frontier:{capture_path}", "--source", "observed"], actor=actor
    )


# --- unassociated sessions ---------------------------------------------------


def record_unassociated(session_ref: str, reason: str, *, actor: Optional[str] = None) -> str:
    """A note saying this session belongs to no workstream, so the hook stops
    asking. Returns the note's id. The note is marked done at once: an open
    note in this namespace would be listed by `weft ls --ready` as work."""
    out = _run(
        [
            "add",
            f"{UNASSOCIATED_PREFIX}{reason}",
            "--kind",
            "note",
            "--ns",
            NS,
            "--ref",
            session_ref,
            "--source",
            "asserted",
        ],
        actor=actor,
    )
    nid = out["nodes"][0]["id"]
    _run(["done", nid], actor=actor)
    return nid


def unassociated(session_ref: str) -> Optional[str]:
    """The newest recorded reason, or None. Not `show <session_ref>`: the same
    ref sits on every workstream the session is attached to, so it would be
    ambiguous exactly when it matters."""
    nodes = _run(["ls", "--ns", NS, "--kind", "note"]).get("nodes") or []
    hits = [
        n
        for n in nodes
        if session_ref in _refs(n)
        and n.get("title", "").startswith(UNASSOCIATED_PREFIX)
        and n.get("state") not in ("dropped",)
    ]
    if not hits:
        return None
    newest = max(hits, key=lambda n: n.get("created") or "")
    return newest["title"][len(UNASSOCIATED_PREFIX) :]


# --- attach intervals --------------------------------------------------------


def attach_intervals(ws: WsArg) -> list[AttachInterval]:
    """Every canonical attach on the workstream, oldest first: held and
    expired claims (`live` is not in export, so it is recomputed from
    `expires`) and released ones, which end at weft's `released_at`.

    Older weft has no `released_claims`: its fold dropped a claim on
    release, so those intervals are missing and the rest carry no end. That
    is the only case left where a caller must close an interval itself.
    """
    wid = _ws_id(ws)
    for n in _run(["export", "--include", "all"]).get("nodes") or []:
        if n["id"] == wid:
            break
    else:
        raise NotFound(f"workstream {wid[:12]} not in export", 2)
    return _workstream(
        n, n.get("claims") or [], released=n.get("released_claims") or []
    ).intervals()
