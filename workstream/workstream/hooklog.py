"""Reader for hooklog's `hook_events.log`: lifecycle, messages, workspaces.

hooklog appends one `{"timestamp", "hook_data"}` line per Claude Code hook
event and rotates `hook_events.log` -> `.1` (kept plain so it can be grepped)
-> `.N.gz`. Its timestamps are naive local time across zone changes, so every
event gets its `Instant` from an `OffsetTimeline` built from SessionStart /
UserPromptSubmit events paired with the head of their own transcript.

Hold one `HookLog` and ask it several questions; the module-level functions
each re-read the log and exist for one-shot callers.
"""

from __future__ import annotations

import gzip
import json
import mmap
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple, Optional, Union

from . import seam
from .clock import MAX_RESIDUAL, Instant, OffsetTimeline, offset_of

LOG_NAME = "hook_events.log"
LIFECYCLE_EVENTS = ("SessionStart", "SessionEnd", "PreCompact")
MESSAGE_WINDOW = timedelta(minutes=10)
HEAD_LINES = 40
# Sources that begin a fresh transcript. A resumed or compacted session's
# transcript head is from its first run, so pairing it with this event's time
# would read the gap as a zone offset.
FRESH_SOURCES = ("startup", "clear")
# Zones span 26 hours; a naive-local `since` must reach this far back to be
# sure of covering an instant in any of them.
_ZONE_SPAN = timedelta(hours=14)

Since = Union[Instant, datetime, None]

# Tool events are most of the log and almost none of them matter here, so
# read_events drops them before parsing unless the raw line names one of these
# tools or carries one of the Bash workspace markers. Widen these to keep more.
# Measured on the real log, 14-day window, warm page cache, 2026-11-02:
# read_events 1.2-1.5 s -> 0.7-0.8 s (72,440 -> 16,506 events parsed); the
# peer scan 4.7-5.0 s -> 0.8-0.9 s (mmap, top-level transcripts only); load()
# plus messages() about 5.6 s -> 1.9 s. Cold cache, the peer scan reached 4 s.
KEPT_TOOLS = {"SendMessage", "ListAgents", "EnterWorktree", "ExitWorktree"}
BASH_WORKSPACE_MARKERS = ("worktree add", "worktree remove", "just wt", "wt new", "wt rm")
_TOOL_EVENTS = ('"PreToolUse"', '"PostToolUse"')
_EVENT_KEY = '"hook_event_name"'
_TOOL_KEY = '"tool_name"'
_INPUT_KEY = '"tool_input"'
_PEER_MARKER = b'"kind":"peer"'
_SUBAGENT_ID_RE = re.compile(r"a[0-9a-f]{16}")


@dataclass
class HookEvent:
    at_local: datetime
    at: Optional[Instant]
    session_id: str
    cwd: str
    event: str
    source: Optional[str] = None
    reason: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    tool_response: Union[dict, str, None] = None
    agent_type: Optional[str] = None
    transcript_path: Optional[str] = None
    tool_use_id: Optional[str] = None
    model: Optional[str] = None


@dataclass
class MessageEvent:
    at: Instant
    from_sid: str
    to_raw: str
    summary: str
    body: str
    to_sid: Optional[str] = None
    # "body" | "registry-name" | "registry-former-name" | "registry-socket"
    # | "registry-id" | None when unresolved
    how: Optional[str] = None  # also "subagent": the sender's own subagent, by agent id

    @property
    def own_subagent(self) -> bool:
        return self.how == "subagent"

    def to_text(self) -> str:
        """The recipient as every renderer should print it: a subagent's own id,
        never its parent's (to_sid is `<parent>/<agent>` for those)."""
        if self.own_subagent and self.to_sid:
            return f"own subagent {self.to_sid.partition('/')[2][:8]}"
        return self.to_sid[:8] if self.to_sid else f"{self.to_raw} (unresolved)"


@dataclass
class WorkspaceEvent:
    at: Instant
    session_id: str
    kind: str  # "enter" | "exit" | "add" | "remove"
    via: str  # "EnterWorktree" | "ExitWorktree" | "git" | "just" | "wt"
    name: Optional[str] = None
    path: Optional[str] = None
    branch: Optional[str] = None
    action: Optional[str] = None
    command: Optional[str] = None


class Segment(NamedTuple):
    start: Instant
    end: Optional[Instant]
    source: Optional[str]
    reason: Optional[str]


@dataclass
class _ToolCall:
    pre: Optional[HookEvent]
    post: Optional[HookEvent]

    @property
    def first(self) -> HookEvent:
        return self.pre or self.post  # type: ignore[return-value]

    @property
    def tool_input(self) -> dict:
        ev = self.pre if self.pre and self.pre.tool_input else self.post
        return (ev.tool_input if ev else None) or {}

    @property
    def tool_response(self):
        return self.post.tool_response if self.post else None


# ---------------------------------------------------------------- reading


def log_files(log_dir: Optional[Path] = None) -> list[Path]:
    """Live log then archives, newest first. `.N` and `.N.gz` can coexist
    briefly while hooklog's detached gzip runs; both are returned."""
    d = Path(log_dir) if log_dir is not None else seam.hooklog_dir()
    if not d.is_dir():
        return []
    numbered: list[tuple[int, int, Path]] = []
    for p in d.glob(LOG_NAME + ".*"):
        rest = p.name[len(LOG_NAME) + 1 :]
        gz = rest.endswith(".gz")
        num = rest[:-3] if gz else rest
        if num.isdigit():
            numbered.append((int(num), int(gz), p))
    live = d / LOG_NAME
    return ([live] if live.is_file() else []) + [p for _, _, p in sorted(numbered)]


def _read_lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
            yield from fh
    except (OSError, EOFError):
        # A gzip still being written by hooklog's detached compressor is
        # truncated; what was read before the error stands.
        return


def _parse_line(line: str) -> Optional[HookEvent]:
    try:
        rec = json.loads(line)
        at_local = datetime.fromisoformat(rec["timestamp"])
        hd = rec["hook_data"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(hd, dict) or at_local.tzinfo is not None:
        return None
    ti = hd.get("tool_input")
    return HookEvent(
        at_local=at_local,
        at=None,
        session_id=hd.get("session_id") or "",
        cwd=hd.get("cwd") or "",
        event=hd.get("hook_event_name") or "",
        source=hd.get("source"),
        reason=hd.get("reason"),
        tool_name=hd.get("tool_name"),
        tool_input=ti if isinstance(ti, dict) else None,
        tool_response=hd.get("tool_response"),
        agent_type=hd.get("agent_type"),
        transcript_path=hd.get("transcript_path"),
        tool_use_id=hd.get("tool_use_id"),
        model=hd.get("model"),
    )


def _value_after(line: str, key: str) -> str:
    """The few characters after `key` in a raw line: enough to hold its value."""
    i = line.find(key)
    return "" if i < 0 else line[i + len(key) : i + len(key) + 40]


def _string_value(fragment: str) -> str:
    """The first JSON string in `fragment` (`: "Bash", ...` -> `Bash`)."""
    parts = fragment.split('"', 2)
    return parts[1] if len(parts) == 3 else ""


def _may_matter(line: str, kept_tools: frozenset) -> bool:
    """False only for a tool event that cannot feed any view here. Reads the
    event and tool names from their keys rather than scanning the line, since
    Post lines carry tool responses up to 32 KB."""
    event = _value_after(line, _EVENT_KEY)
    if not event or not any(t in event for t in _TOOL_EVENTS):
        return True
    tool = _string_value(_value_after(line, _TOOL_KEY))
    if tool in kept_tools:
        return True
    # Workspace markers count only in a Bash call's input, and only on the
    # Pre (workspace_events reads nothing from a Bash response): "worktree" is
    # in the cwd and transcript path of nearly every call made from inside one.
    if tool != "Bash" or '"PreToolUse"' not in event:
        return False
    i = line.find(_INPUT_KEY)
    return i >= 0 and any(m in line[i:] for m in BASH_WORKSPACE_MARKERS)


def _naive_cut(since: Since) -> Optional[datetime]:
    """The earliest naive local time that could be at or after `since`."""
    if since is None:
        return None
    if isinstance(since, Instant):
        return since.utc.replace(tzinfo=None) - _ZONE_SPAN
    if since.tzinfo is not None:
        return since.astimezone().replace(tzinfo=None) - _ZONE_SPAN
    return since


def read_events(
    since: Since = None, log_dir: Optional[Path] = None, warnings: Optional[list] = None
) -> list[HookEvent]:
    """Raw events (no `at` yet), chronological. A naive `since` cuts on local
    wall time; an Instant cuts loosely here and exactly once `at` is known."""
    cut = _naive_cut(since)
    kept_tools = frozenset(KEPT_TOOLS)
    out: list[HookEvent] = []
    bad = 0
    files = log_files(log_dir)
    if not files and warnings is not None:
        warnings.append(f"hooklog: no {LOG_NAME} under {log_dir or seam.hooklog_dir()}")
    for path in files:
        newest: Optional[datetime] = None
        for line in _read_lines(path):
            if not line.strip() or not _may_matter(line, kept_tools):
                continue
            ev = _parse_line(line)
            if ev is None:
                bad += 1
                continue
            if newest is None or ev.at_local > newest:
                newest = ev.at_local
            if cut is None or ev.at_local >= cut:
                out.append(ev)
        if cut is not None and newest is not None and newest < cut:
            break
    if bad and warnings is not None:
        warnings.append(f"hooklog: skipped {bad} unparseable line(s)")
    out.sort(key=lambda e: e.at_local)
    return out


# ---------------------------------------------------------------- clock pairing


def _head_timestamps(path: Path, n: int = HEAD_LINES) -> list[datetime]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= n:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = seam.parse_ts(rec.get("timestamp", "")) if isinstance(rec, dict) else None
                if ts is not None and ts.tzinfo is not None:
                    out.append(ts)
    except OSError:
        pass
    return out


def _transcript_for(ev: HookEvent, projects: Optional[Path], index: dict) -> Optional[Path]:
    """The event's transcript_path, else the session's file in the store
    (moved or re-rooted projects). `index` is filled on first need: one
    listing of the store, not one glob per session."""
    if ev.transcript_path and Path(ev.transcript_path).is_file():
        return Path(ev.transcript_path)
    if projects is None or not ev.session_id:
        return None
    if not index:
        index.update((p.stem, p) for p in Path(projects).glob("*/*.jsonl"))
        index.setdefault("", None)
    return index.get(ev.session_id)


def pair_with_transcripts(
    events: Iterable[HookEvent], projects_dir: Optional[Path] = None
) -> list[tuple[datetime, datetime]]:
    """(naive local, utc) readings of one moment, at most one per fresh session
    segment, for `OffsetTimeline.from_pairs`.

    Each SessionStart with a fresh source, and the first UserPromptSubmit after
    it, is tried against every timestamp in its transcript's first lines; the
    reading with the smallest residual off a 15-minute multiple wins.
    """
    # CONTRACT: the brief pairs SessionStart with the transcript's first record.
    # Claude Code writes nothing to the transcript until the first prompt, so
    # that pair is off by however long the session sat idle and fails the
    # residual check whenever that exceeds 2 min. The first UserPromptSubmit
    # lands within milliseconds of the first user record, so it is tried too
    # and wins on residual; SessionStart still pairs when the prompt came fast.
    events = list(events)
    projects = Path(projects_dir) if projects_dir is not None else None
    index: dict = {}
    pairs: list[tuple[datetime, datetime]] = []
    for i, start in enumerate(events):
        if start.event != "SessionStart" or (start.source or "startup") not in FRESH_SOURCES:
            continue
        tries = [start]
        for later in events[i + 1 :]:
            if later.session_id != start.session_id:
                continue
            if later.event == "SessionStart":
                break
            if later.event == "UserPromptSubmit":
                tries.append(later)
                break
        path = _transcript_for(start, projects, index)
        if path is None:
            continue
        heads = _head_timestamps(path)
        best: Optional[tuple[timedelta, datetime, datetime]] = None
        for ev in tries:
            for utc in heads:
                got = offset_of(ev.at_local, utc)
                if got is None or got[1] > MAX_RESIDUAL:
                    continue
                if best is None or got[1] < best[0]:
                    best = (got[1], ev.at_local, utc)
        if best is not None:
            pairs.append((best[1], best[2]))
    return pairs


# ---------------------------------------------------------------- workspaces

_GIT_WT_RE = re.compile(r"\bgit\s+(?:-C\s+(\S+)\s+)?worktree\s+(add|remove)\b")
_JUST_WT_RE = re.compile(r"\bjust\s+(wt|wt-rm)\s+([^\s;&|)]+)")
_WT_CLI_RE = re.compile(r"(?:^|[;&|(]\s*|\s)wt\s+(new|rm)\b(?:\s+([^\s;&|)-][^\s;&|)]*))?")
# git worktree add options that consume the next token.
_GIT_WT_ARG_OPTS = {"-b", "-B", "--reason"}


def shell_unresolved(path: str) -> bool:
    """True for a path the shell would have rewritten before git saw it: an
    unexpanded `$VAR`, a backtick, or a `~` that is not the first character.

    These come from reading a command's text rather than running it
    (`cd $R && git worktree add …`, a path quoted in backticks), so they name
    worktrees that never existed. Reporting them as "no longer exists" reads as
    a removed worktree, so they are dropped before they become candidates.
    """
    return "$" in path or "`" in path or "/~/" in path or path.endswith("/~")


def _git_worktree_path(command: str, verb: str, cwd: str, git_c: Optional[str]) -> Optional[str]:
    """The path operand of `git worktree add|remove`, made absolute."""
    m = re.search(rf"\bworktree\s+{verb}\b(.*)", command)
    if not m:
        return None
    try:
        tokens = shlex.split(m.group(1).split("&&")[0].split(";")[0])
    except ValueError:
        tokens = m.group(1).split()
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok in _GIT_WT_ARG_OPTS:
            skip = True
            continue
        if tok.startswith("-"):
            continue
        base = Path(os.path.expanduser(git_c)) if git_c else Path(cwd or "/")
        if not base.is_absolute():
            base = Path(cwd or "/") / base
        path = os.path.normpath(str(base / os.path.expanduser(tok)))
        return None if shell_unresolved(path) else path
    return None


def _bash_workspace_events(call: _ToolCall, at: Instant) -> list[WorkspaceEvent]:
    ev = call.first
    command = call.tool_input.get("command") or ""
    out = []
    for m in _GIT_WT_RE.finditer(command):
        verb = m.group(2)
        out.append(
            WorkspaceEvent(
                at=at,
                session_id=ev.session_id,
                kind=verb,
                via="git",
                path=_git_worktree_path(command[m.start() :], verb, ev.cwd, m.group(1)),
                command=command,
            )
        )
    for m in _JUST_WT_RE.finditer(command):
        out.append(
            WorkspaceEvent(
                at=at,
                session_id=ev.session_id,
                kind="add" if m.group(1) == "wt" else "remove",
                via="just",
                name=m.group(2),
                command=command,
            )
        )
    for m in _WT_CLI_RE.finditer(command):
        out.append(
            WorkspaceEvent(
                at=at,
                session_id=ev.session_id,
                kind="add" if m.group(1) == "new" else "remove",
                via="wt",
                name=m.group(2),
                command=command,
            )
        )
    return out


# ---------------------------------------------------------------- the log


def _ms(inst: Instant) -> int:
    return int(inst.utc.timestamp() * 1000)


@dataclass
class HookLog:
    events: list[HookEvent]
    timeline: OffsetTimeline
    projects: Optional[Path] = None
    registry: Optional[list] = None
    warnings: list[str] = field(default_factory=list)
    _peer_cache: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------ generic

    def _since_ok(self, ev: HookEvent, since: Since) -> bool:
        if since is None:
            return True
        if isinstance(since, Instant):
            return ev.at is not None and ev.at.utc >= since.utc
        if since.tzinfo is not None:
            return ev.at is not None and ev.at.utc >= since
        return ev.at_local >= since

    def for_session(self, session_id: str) -> list[HookEvent]:
        return [e for e in self.events if e.session_id == session_id]

    def _tool_calls(self, tool_name: str, session_id: Optional[str] = None) -> list[_ToolCall]:
        """Pre/Post pairs for one tool, one entry per call. Pairs by
        tool_use_id when present, else the oldest open Pre with equal input."""
        calls: list[_ToolCall] = []
        open_: list[_ToolCall] = []
        for ev in self.events:
            if ev.tool_name != tool_name or ev.event not in ("PreToolUse", "PostToolUse"):
                continue
            if session_id is not None and ev.session_id != session_id:
                continue
            if ev.event == "PreToolUse":
                call = _ToolCall(ev, None)
                calls.append(call)
                open_.append(call)
                continue
            match = None
            for call in open_:
                pre = call.pre
                if pre is None or pre.session_id != ev.session_id:
                    continue
                if pre.tool_use_id and ev.tool_use_id:
                    if pre.tool_use_id == ev.tool_use_id:
                        match = call
                        break
                elif (pre.tool_input or {}) == (ev.tool_input or {}):
                    match = call
                    break
            if match is not None:
                match.post = ev
                open_.remove(match)
            else:
                calls.append(_ToolCall(None, ev))
        return calls

    # ------------------------------------------------ lifecycle

    def lifecycle(self, session_id: str) -> list[HookEvent]:
        return [e for e in self.for_session(session_id) if e.event in LIFECYCLE_EVENTS]

    def last_lifecycle(self, session_id: str) -> Optional[HookEvent]:
        lc = self.lifecycle(session_id)
        return lc[-1] if lc else None

    def segments(self, session_id: str) -> list[Segment]:
        out: list[Segment] = []
        current: Optional[HookEvent] = None
        for ev in self.lifecycle(session_id):
            if ev.event == "SessionStart":
                if current is not None:
                    out.append(Segment(current.at, None, current.source, None))
                current = ev
            elif ev.event == "SessionEnd" and current is not None:
                out.append(Segment(current.at, ev.at, current.source, ev.reason))
                current = None
        if current is not None:
            out.append(Segment(current.at, None, current.source, None))
        return out

    # ------------------------------------------------ messages

    def _registry(self) -> list:
        if self.registry is None:
            try:
                self.registry = list(seam.live_registry())
            except Exception as exc:  # degrade: a broken registry must not cost the messages
                self.warnings.append(f"hooklog: live registry unavailable ({exc})")
                self.registry = []
        return self.registry

    def _sender_name(self, sid: str) -> Optional[str]:
        for r in self._registry():
            if r.get("sessionId") == sid:
                return r.get("name") or None
        return None

    def _peer_records(
        self, earliest: Instant, sids: Optional[set[str]] = None
    ) -> list[tuple[Instant, str, dict]]:
        """(at, recipient sid, origin) for every incoming peer message in a
        transcript touched since `earliest`, or only in the transcripts of
        `sids` when given. Only top-level transcripts are read: subagents
        (`<sid>/subagents/`) are never messaged by peers."""
        out: list[tuple[Instant, str, dict]] = []
        if self.projects is None or not Path(self.projects).is_dir():
            return out
        floor = earliest.utc.timestamp() - MESSAGE_WINDOW.total_seconds()
        for path in Path(self.projects).glob("*/*.jsonl"):
            if sids is not None and path.stem not in sids:
                continue
            try:
                if path.stat().st_mtime < floor:
                    continue
            except OSError:
                continue
            if path not in self._peer_cache:
                self._peer_cache[path] = self._scan_peers(path)
            out.extend(self._peer_cache[path])
        return out

    def _scan_peers(self, path: Path) -> list[tuple[Instant, str, dict]]:
        # mmap + find rather than reading lines: the window's transcripts run
        # to gigabytes and a few hundred lines in them matter. Claude Code
        # writes compact JSON, so the marker has no space after the colon.
        found = []
        try:
            with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
                i = m.find(_PEER_MARKER)
                while i >= 0:
                    start = m.rfind(b"\n", 0, i) + 1
                    end = m.find(b"\n", i)
                    end = len(m) if end < 0 else end
                    rec = self._peer_record(m[start:end])
                    if rec is not None:
                        found.append(rec)
                    i = m.find(_PEER_MARKER, end)
        except (OSError, ValueError):
            # ValueError: mmap of an empty file.
            pass
        return found

    def _peer_record(self, raw: bytes) -> Optional[tuple[Instant, str, dict]]:
        try:
            rec = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(rec, dict):
            return None
        origin = rec.get("origin")
        if (
            rec.get("type") != "user"
            or not rec.get("isMeta")
            or not isinstance(origin, dict)
            or origin.get("kind") != "peer"
        ):
            return None
        ts = seam.parse_ts(rec.get("timestamp", ""))
        if ts is None or ts.tzinfo is None:
            return None
        return Instant(ts, self.timeline.offset_at_utc(ts)), rec.get("sessionId") or "", origin

    def _resolve_by_registry(self, to: str, at: Instant) -> tuple[Optional[str], Optional[str]]:
        reg = self._registry()
        for r in reg:
            if r.get("name") and r.get("name") == to:
                return r.get("sessionId"), "registry-name"
        # A former name points at whoever held it when the message was sent:
        # the holder whose tenure ended soonest after the send.
        best: Optional[tuple[int, str]] = None
        at_ms = _ms(at)
        for r in reg:
            for former in r.get("formerNames") or []:
                if not isinstance(former, dict) or former.get("name") != to:
                    continue
                until = former.get("until")
                if isinstance(until, (int, float)) and until >= at_ms:
                    if best is None or until < best[0]:
                        best = (int(until), r.get("sessionId"))
        if best is not None:
            return best[1], "registry-former-name"
        if to.startswith("uds:"):
            sock = to[len("uds:") :]
            for r in reg:
                if r.get("messagingSocketPath") in (sock, to):
                    return r.get("sessionId"), "registry-socket"
        for r in reg:
            if r.get("sessionId") == to:
                return to, "registry-id"
        return None, None

    def messages(
        self, since: Since = None, involving: Optional[set[str]] = None
    ) -> list[MessageEvent]:
        """SendMessage calls, recipients resolved now (D5): a peer record in the
        recipient's transcript with the same body, then the live registry.

        `involving` keeps only messages sent by or resolved to one of those
        sessions, and is what keeps a workstream's view cheap: resolving every
        call in the log means scanning every transcript touched since the
        oldest one, gigabytes on a busy store. A call from outside the set can
        only matter if its recipient is inside it, so it is matched against the
        members' transcripts alone, each member record answering one call,
        nearest first. Same answers as the unfiltered call, except when one
        body went to two sessions within MESSAGE_WINDOW: the unfiltered call
        can pick a closer non-member record, or land both calls on one."""
        out: list[MessageEvent] = []
        for call in self._tool_calls("SendMessage"):
            ev = call.first
            if ev.at is None or not self._since_ok(ev, since):
                continue
            ti = call.tool_input
            out.append(
                MessageEvent(
                    at=ev.at,
                    from_sid=ev.session_id,
                    to_raw=str(ti.get("to") or ""),
                    summary=str(ti.get("summary") or ""),
                    body=str(ti.get("message") or ""),
                )
            )
        if not out:
            return out
        if involving is None:
            peers = self._peer_records(min(m.at for m in out))
            for m in out:
                self._resolve_recipient(m, peers)
            return out

        sent = [m for m in out if m.from_sid in involving]
        others = [m for m in out if m.from_sid not in involving]
        kept = list(sent)
        if sent:
            peers = self._peer_records(min(m.at for m in sent))
            for m in sent:
                self._resolve_recipient(m, peers)
        if others:
            kept.extend(self._outsider_calls(others, involving))
        keep = {id(m) for m in kept}
        return [m for m in out if id(m) in keep]  # log order, as unfiltered

    def _outsider_calls(self, calls: list[MessageEvent], involving: set[str]) -> list[MessageEvent]:
        """The calls from outside `involving` that reached a member."""
        mine = self._peer_records(min(m.at for m in calls), sids=involving)
        # One peer record answers one call. Searching the members' transcripts
        # alone, a body sent to a member and again to an outsider would
        # otherwise land both calls on the member's single record: pair them
        # nearest first, and a call that loses its record went elsewhere.
        pairs = []
        for ci, m in enumerate(calls):
            sender = self._sender_name(m.from_sid)
            for pi, rec in enumerate(mine):
                gap = self._body_gap(m, sender, rec)
                if gap is not None:
                    pairs.append((gap, ci, pi))
        pairs.sort(key=lambda x: x[0])
        tried = {ci for _, ci, _ in pairs}
        won: set[int] = set()
        used: set[int] = set()
        out = []
        for _, ci, pi in pairs:
            if ci in won or pi in used:
                continue
            won.add(ci)
            used.add(pi)
            m = calls[ci]
            m.to_sid, m.how = mine[pi][1], "body"
            out.append(m)

        # No body match among the members: only the registry can still name
        # one, and a non-member's transcript may hold the body match that
        # wins. Settle those with one full scan, for these few calls only.
        escalate = [
            m
            for ci, m in enumerate(calls)
            if ci not in tried
            and not _SUBAGENT_ID_RE.fullmatch(m.to_raw)
            and self._resolve_by_registry(m.to_raw, m.at)[0] in involving
        ]
        if escalate:
            peers = self._peer_records(min(m.at for m in escalate))
            for m in escalate:
                self._resolve_recipient(m, peers)
                if m.to_sid in involving:
                    out.append(m)
        return out

    def _body_gap(
        self, m: MessageEvent, sender: Optional[str], rec: tuple[Instant, str, dict]
    ) -> Optional[timedelta]:
        """How far `rec` sits from `m` in time, if it is `m`'s body arriving."""
        at, sid, origin = rec
        if origin.get("body") != m.body or not sid or sid == m.from_sid:
            return None
        if sender and origin.get("name") and origin.get("name") != sender:
            return None
        gap = abs(at.utc - m.at.utc)
        return gap if gap <= MESSAGE_WINDOW else None

    def _match_body(self, m: MessageEvent, peers: list[tuple[Instant, str, dict]]) -> Optional[str]:
        """The recipient whose peer record carries `m`'s body nearest in time."""
        sender = self._sender_name(m.from_sid)
        best: Optional[tuple[timedelta, str]] = None
        for rec in peers:
            gap = self._body_gap(m, sender, rec)
            if gap is not None and (best is None or gap < best[0]):
                best = (gap, rec[1])
        return best[1] if best is not None else None

    def _resolve_recipient(self, m: MessageEvent, peers: list[tuple[Instant, str, dict]]) -> None:
        to = self._match_body(m, peers)
        if to is not None:
            m.to_sid, m.how = to, "body"
            return
        if _SUBAGENT_ID_RE.fullmatch(m.to_raw):
            # A message to the sender's own subagent names it by agent id;
            # no peer record is written for that, and no registry row.
            m.to_sid, m.how = f"{m.from_sid}/{m.to_raw}", "subagent"
            return
        m.to_sid, m.how = self._resolve_by_registry(m.to_raw, m.at)

    def list_agents_calls(self, since: Since = None) -> list[HookEvent]:
        return [
            e
            for e in self.events
            if e.event == "PreToolUse" and e.tool_name == "ListAgents" and self._since_ok(e, since)
        ]

    # ------------------------------------------------ workspaces

    def workspace_events(self, session_id: str) -> list[WorkspaceEvent]:
        out: list[WorkspaceEvent] = []
        for call in self._tool_calls("EnterWorktree", session_id):
            resp = call.tool_response if isinstance(call.tool_response, dict) else {}
            out.append(
                WorkspaceEvent(
                    at=call.first.at,  # type: ignore[arg-type]
                    session_id=session_id,
                    kind="enter",
                    via="EnterWorktree",
                    name=call.tool_input.get("name"),
                    path=resp.get("worktreePath") or call.tool_input.get("path"),
                    branch=resp.get("worktreeBranch"),
                )
            )
        for call in self._tool_calls("ExitWorktree", session_id):
            resp = call.tool_response if isinstance(call.tool_response, dict) else {}
            out.append(
                WorkspaceEvent(
                    at=call.first.at,  # type: ignore[arg-type]
                    session_id=session_id,
                    kind="exit",
                    via="ExitWorktree",
                    path=resp.get("worktreePath"),
                    branch=resp.get("worktreeBranch"),
                    action=call.tool_input.get("action"),
                )
            )
        for call in self._tool_calls("Bash", session_id):
            out.extend(_bash_workspace_events(call, call.first.at))  # type: ignore[arg-type]
        out.sort(key=lambda w: w.at.utc)
        return out


def load(
    since: Since = None,
    log_dir: Optional[Path] = None,
    projects_dir: Optional[Path] = None,
    registry: Optional[list] = None,
) -> HookLog:
    """Read the log, infer offsets, and stamp every event with its Instant.

    The timeline is built from every event read, including those before an
    Instant `since` (read loosely by up to a zone's span), so the edge of the
    window has nearby samples.
    """
    warnings: list[str] = []
    projects = Path(projects_dir) if projects_dir is not None else seam.projects_dir()
    events = read_events(since, log_dir, warnings)
    timeline = OffsetTimeline.from_pairs(pair_with_transcripts(events, projects))
    if events and not timeline:
        warnings.append("hooklog: no clock pairs; using the host zone for every event")
    for ev in events:
        ev.at = timeline.instant(ev.at_local)
    log = HookLog(events, timeline, projects, registry, warnings)
    if since is not None and (isinstance(since, Instant) or since.tzinfo is not None):
        log.events = [e for e in events if log._since_ok(e, since)]
    return log


# One-shot conveniences: each re-reads the log.


def lifecycle(session_id: str, log: Optional[HookLog] = None) -> list[HookEvent]:
    return (log or load()).lifecycle(session_id)


def last_lifecycle(session_id: str, log: Optional[HookLog] = None) -> Optional[HookEvent]:
    return (log or load()).last_lifecycle(session_id)


def segments(session_id: str, log: Optional[HookLog] = None) -> list[Segment]:
    return (log or load()).segments(session_id)


def messages(since: Since = None, log: Optional[HookLog] = None) -> list[MessageEvent]:
    return (log or load(since)).messages(since)


def list_agents_calls(since: Since = None, log: Optional[HookLog] = None) -> list[HookEvent]:
    return (log or load(since)).list_agents_calls(since)


def workspace_events(session_id: str, log: Optional[HookLog] = None) -> list[WorkspaceEvent]:
    return (log or load()).workspace_events(session_id)
