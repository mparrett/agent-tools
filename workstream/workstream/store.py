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


class Conflict(WeftError):
    """`weft apply` refused a batch because an expectation no longer held
    (exit 8): someone changed the store since it was read. Nothing was
    written. `failed` is weft's list of {i, expect, actual}."""

    def __init__(self, message: str, code: Optional[int], failed: list):
        super().__init__(message, code)
        self.failed = failed


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


def _run(
    args: list[str],
    *,
    timeout: Optional[float] = None,
    actor: Optional[str] = None,
    stdin: Optional[str] = None,
) -> dict:
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
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, input=stdin
        )
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
    if rc == 8:
        failed = (payload or {}).get("failed") if isinstance(payload, dict) else None
        raise Conflict(msg, rc, list(failed or []))
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
    """Drop the session's claims, keep its ref: it stays a member, no longer
    canonical. True when any claim was released. One `release` by holder
    drops every live claim the session holds there, stacked ones included."""
    out = _apply(
        [], [{"op": "release", "node": _ws_id(ws), "holder": _holder(session_ref)}], actor=actor
    )
    return bool(out["results"][0].get("claims"))


# How many times an operation rereads the store and tries again after a
# conflict. Each try is one read and one batch; losing three in a row means
# something else is rewriting the same claims, which a fourth will not fix.
APPLY_RETRIES = 3


def _apply(
    expect: list[dict], ops: list[dict], *, actor: Optional[str] = None, dry_run: bool = False
) -> dict:
    """One `weft apply`: `ops` all land or none do, and only while every
    expectation still holds (else Conflict)."""
    args = ["apply", "--dry-run"] if dry_run else ["apply"]
    try:
        return _run(args, actor=actor, stdin=json.dumps({"expect": expect, "ops": ops}))
    except Conflict:
        raise
    except WeftError as e:
        if e.code == 1 and "apply" in str(e) and "unknown" in str(e).lower():
            raise WeftError(
                "this workstream needs a weft with `weft apply` (weft#10 or later); "
                f"{seam.weft_bin()} has none",
                e.code,
            ) from e
        raise


def _scope(holder: str, nodes: Iterable[str]) -> dict:
    """The expectation that `holder`'s live workstream claims are on exactly
    `nodes`: what makes one canonical workstream per session hold under
    concurrent attaches, including two to different new workstreams."""
    return {"holder": holder, "live_on": sorted(nodes), "ns": NS, "kind": "workstream"}


def _membership_ops(
    node: str, session_ref: str, holder: str, claim: bool, note: Optional[str], subagents
) -> list[dict]:
    ops: list[dict] = [{"op": "ref.add", "node": node, "ref": session_ref, "source": "observed"}]
    if claim:
        op = {"op": "claim", "node": node, "holder": holder, "source": "observed"}
        if note:
            op["note"] = note
        ops.append(op)
    ops += [{"op": "ref.add", "node": node, "ref": r, "source": "observed"} for r in subagents]
    return ops


def _held_by(holder: str) -> list[Workstream]:
    """Workstreams on which `holder` has a live claim, dropped ones included:
    weft's `live_on` counts those, and `ls` (so `list_workstreams`) hides them."""
    out = []
    for n in _run(["export", "--include", "claims"]).get("nodes") or []:
        if n.get("ns") != NS or n.get("kind") != "workstream":
            continue
        w = _workstream(n, n.get("claims") or [], released=n.get("released_claims") or [])
        if holder in w.canonical_holders():
            out.append(w)
    return out


def join(
    ws: Optional[WsArg],
    session_ref: str,
    *,
    canonical: bool,
    note: Optional[str] = None,
    subagents: Iterable[str] = (),
    new: Optional[tuple[str, str, list[str]]] = None,
    actor: Optional[str] = None,
) -> tuple[Workstream, list[Workstream]]:
    """Attach a session, and its subagents' refs, in one batch.

    Canonical (D12): the session's claims on every other workstream are
    released and one is taken here, all or nothing, and only while its claims
    are still where they were read; a concurrent attach makes weft refuse,
    and this rereads and tries again. A session already holding a claim here
    keeps it rather than stacking a second (one attachment, one interval).
    Ancillary: the refs alone.

    `new` is (name, title, anchors): create the workstream in the same batch,
    only while no other workstream carries `ws:<name>`. Returns the
    workstream and the ones whose claim was released."""
    holder = _holder(session_ref)
    subagents = list(subagents)
    last: Optional[Conflict] = None
    for _ in range(APPLY_RETRIES):
        held = _held_by(holder) if canonical else []
        expect: list[dict] = [_scope(holder, (w.id for w in held))] if canonical else []
        if new:
            name, title, anchors = new
            node = "$ws"
            expect.append({"ref": f"ws:{name}", "bound_to": []})
            refs = [f"ws:{name}", *(_canon_ref(a) for a in anchors)]
            ops: list[dict] = [
                {
                    "op": "add",
                    "as": node,
                    "title": title,
                    "kind": "workstream",
                    "ns": NS,
                    "refs": refs,
                    "source": "asserted",
                }
            ]
            claim = canonical
        else:
            current = get(_ws_id(ws))
            node = current.id
            if current.closed:
                # Closed since the caller checked: a retry must not claim it.
                raise WeftError(f"workstream {current.name or node[:12]} is closed", 8)
            expect.append({"node": node, "state": current.state})
            ops = []
            claim = canonical and holder not in current.canonical_holders()
        released = [w for w in held if w.id != node]
        ops += [{"op": "release", "node": w.id, "holder": holder} for w in released]
        ops += _membership_ops(node, session_ref, holder, claim, note, subagents)
        try:
            out = _apply(expect, ops, actor=actor)
        except Conflict as e:
            if new and any("ref" in (f.get("expect") or {}) for f in e.failed):
                raise WeftError(f"a workstream named {new[0]!r} already exists", 8) from e
            last = e
            continue
        wid = out.get("tmp", {}).get(node, node)
        return get(wid), released
    raise WeftError(
        f"attach: claims kept changing underneath it ({APPLY_RETRIES} tries): {last}", 8
    )


# --- closure -----------------------------------------------------------------


def blocked_ids() -> set[str]:
    """Ids of every node weft reports blocked, across namespaces: a task can be
    part-of a workstream from another namespace's repo."""
    return {n["id"] for n in _run(["ls", "--all-ns", "--blocked"]).get("nodes") or []}


def close(
    ws: WsArg, note: Optional[str] = None, *, force: bool = False, actor: Optional[str] = None
) -> Workstream:
    """Mark the workstream done, in one batch. Deciding whether it *should*
    close is the caller's job (cli `close` checks tasks and loose ends); pass
    the Workstream it checked, so the batch can hold that check to account.
    A closed workstream keeps its refs, so `show` still answers who worked on
    it.

    A normal close commits only while nobody holds a claim and the tasks are
    the ones the caller saw, in the states it saw: a claim taken, a task
    linked or a task reopened since then
    raises Conflict rather than being released or closed over. `force`
    releases every live claim under the same lock instead.

    `weft done` takes no note, so a note becomes a `note` node linked
    `derived-from` the workstream (not part-of: it is not work the workstream
    contains) and is itself marked done, since an open note would surface in
    `weft ls --ready` as if it were work.

    A resolved Workstream is used as given, and the result is built from it,
    so closing costs no export; pass one resolved just now."""
    current = ws if isinstance(ws, Workstream) else get(ws)
    expect: list[dict] = [{"node": current.id, "state": current.state}]
    ops: list[dict] = []
    if force:
        ops.append({"op": "release", "node": current.id, "all": True})
    else:
        expect.append({"node": current.id, "claims": []})
        expect += [{"node": t.id, "state": t.state} for t in current.tasks]
        # And no task linked since: weft counts the non-dropped part-of children.
        live = [c.id for c in current.children if c.state != "dropped"]
        expect.append({"node": current.id, "children": live})
    if note:
        ops += [
            {
                "op": "add",
                "as": "$note",
                "title": f"{CLOSED_PREFIX}{note}",
                "kind": "note",
                "ns": NS,
                "source": "asserted",
            },
            {"op": "link", "from": "$note", "kind": "derived-from", "to": current.id},
            {"op": "done", "node": "$note"},
        ]
    ops.append({"op": "done", "node": current.id})
    _apply(expect, ops, actor=actor)
    # Built from what was sent rather than read back, so closing costs no
    # export. The released claims carry the moment after the batch, which is
    # no earlier than weft's own `released_at`.
    closed_at = Instant.now()
    released = current.released + [
        replace(c, live=False, released_at=closed_at) for c in current.claims if c.live
    ]
    return replace(
        current,
        state="done",
        touched=closed_at,
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


def _ref_events(wid: str) -> Optional[tuple[dict, dict]]:
    """The `ref.add` events on node `wid`, as {event id: (ref, source, at)},
    and every `ref.retract`, as {target id: at}; None when the log cannot be
    read.

    No weft command returns a ref's source or history (its fold keeps the
    binding only), so this reads the event log, which weft's EVENTS.md makes
    the contract other tools read: unknown events are skipped, a partial
    final line is dropped, and a newer schema version is refused. A ref bound
    to a node later merged into this one is not followed."""
    try:
        log = _run(["env"]).get("log")
        with open(log, "rb") as fh:
            data = fh.read()
    except (WeftError, OSError, TypeError):
        return None
    lines = data.split(b"\n")
    lines.pop()  # after the last LF: empty, or a write still in flight
    adds: dict[str, tuple[str, str, str]] = {}
    retracted: dict[str, str] = {}
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(ev, dict) or int(ev.get("v") or 0) > LOG_VERSION:
            return None
        kind = ev.get("ev")
        if kind == "ref.add" and ev.get("node") == wid:
            adds[ev.get("id", "")] = (ev.get("ref", ""), ev.get("source", ""), ev.get("at", ""))
        elif kind == "ref.retract":
            retracted[ev.get("target", "")] = ev.get("at", "")
    return adds, retracted


def ref_sources(ws: WsArg) -> dict[str, str]:
    """How each of the workstream's refs was added: `sync` when every live
    `ref.add` binding it was written by sync (`SYNC_SOURCE`), else the weft
    source of the first one that was not. Empty when the log cannot be read;
    the caller then shows no provenance, which is the same as before sync
    existed."""
    got = _ref_events(_ws_id(ws))
    if got is None:
        return {}
    adds, retracted = got
    by_ref: dict[str, list[str]] = {}
    for eid, (ref, source, _) in adds.items():
        if eid not in retracted:
            by_ref.setdefault(ref, []).append(source)
    return {
        ref: "sync"
        if all(x == SYNC_SOURCE for x in srcs)
        else next(x for x in srcs if x != SYNC_SOURCE)
        for ref, srcs in by_ref.items()
    }


def removed_refs(ws: WsArg) -> dict[str, datetime]:
    """Refs once bound to the workstream and since taken off, by canonical
    ref, with when the last binding went (UTC). `sync` reads this so it does
    not put back what someone removed on the strength of evidence from before
    the removal. Empty when the log cannot be read, which is as before."""
    got = _ref_events(_ws_id(ws))
    if got is None:
        return {}
    adds, retracted = got
    live: set[str] = set()
    gone: dict[str, datetime] = {}
    for eid, (ref, _, _) in adds.items():
        canon = _canon_ref(ref)
        if eid not in retracted:
            live.add(canon)
            continue
        try:
            at = datetime.fromisoformat(retracted[eid]).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if canon not in gone or at > gone[canon]:
            gone[canon] = at
    return {ref: at for ref, at in gone.items() if ref not in live}


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
