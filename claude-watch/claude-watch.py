#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Follow one Claude Code session live: its turns or /loop ticks, the tools it
calls, and the subagents it spawns, with each subagent's own tool calls inline."""

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS = Path(os.environ.get("CLAUDE_WATCH_PROJECTS") or Path.home() / ".claude" / "projects")
HOME = str(Path.home())

# A subagent spawned before the replayed window is only worth following if it is
# plausibly still working; past this, an unfinished transcript was abandoned.
STALE_AGENT_SECONDS = 15 * 60

# Records that carry nothing a person watching wants to see. About 60% of a long
# session's lines are these.
NOISE_TYPES = {
    "attachment", "pr-link", "last-prompt", "mode", "permission-mode", "atis-latch",
    "ai-title", "api-request", "api-request-blob", "api-request-shape",
    "file-history-snapshot", "file-history-delta",
}  # fmt: skip

TAG_WIDTH = 20

# Live, a /loop tick is held back until it ends, so a tick that repeats the one
# before can fold into a counter line instead of printing in full. A tick that
# spawns an agent, edits a file, errors, or runs past this long is no repeat,
# and streams from then on.
HOLD_SECONDS = 120
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}

AGENT_COLORS = ("36", "35", "33", "34", "32", "31")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def hhmm(dt):
    return dt.astimezone().strftime("%H:%M") if dt else "--:--"


def fmt_dur(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s" if seconds < 600 else f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def local_day(dt):
    return dt.astimezone().date() if dt else None


def first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def tilde(path):
    return "~" + path[len(HOME) :] if isinstance(path, str) and path.startswith(HOME) else path


def tag_text(text, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text or "", re.S)
    return m.group(1).strip() if m else ""


def resolve_session(ref, base=None):
    """A path, a full session id, or a unique id prefix -> the main transcript."""
    p = Path(ref).expanduser()
    if p.is_file():
        return p
    base = base or PROJECTS
    hits = sorted(base.glob(f"*/{ref}*.jsonl"))
    if not hits:
        sys.exit(f"claude-watch: no session matching {ref!r} under {tilde(str(base))}")
    if len(hits) > 1:
        names = "\n  ".join(h.stem for h in hits[:10])
        sys.exit(f"claude-watch: {ref!r} is ambiguous:\n  {names}")
    return hits[0]


def _loads(line):
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


class Tail:
    """Incremental JSONL reader. Holds back a trailing partial line, since the
    session may be mid-write when we read."""

    def __init__(self, path, offset=0):
        self.path = Path(path)
        self.offset = offset
        self._buf = b""

    def read(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except FileNotFoundError:
            return []
        if not data:
            return []
        self.offset += len(data)
        lines = (self._buf + data).split(b"\n")
        self._buf = lines.pop()
        return [r for r in map(_loads, lines) if r]


def message_blocks(rec):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def prompt_text(rec):
    """The typed (or injected) text of a user record, or None for tool results."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return None
    blocks = message_blocks(rec)
    if not blocks or any(b.get("type") == "tool_result" for b in blocks):
        return None
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text or None


def prompt_label(text):
    if text.startswith("<command-message>") or text.startswith("<command-name>"):
        return f"{tag_text(text, 'command-name')} {tag_text(text, 'command-args')}".strip()
    if text.startswith("<task-notification>"):
        return "⚑ " + (tag_text(text, "summary") or "task notification")
    m = re.match(r"<([a-z][a-z0-9-]*)>", text)
    if m:
        return f"<{m.group(1)}>"
    return "you: " + first_line(text)


def turn_marker(rec, in_turn):
    """('start', label) / ('end', None) / None, given whether a turn is open.
    A /loop fire is followed by injected wrapper messages inside the same turn,
    so a wrapper only opens a turn when none is open. A typed prompt always
    opens one: an interrupted turn writes no turn_duration, so an open turn
    at a typed prompt is one that ended unrecorded."""
    if rec.get("type") == "system":
        if rec.get("subtype") == "scheduled_task_fire":
            # Every /loop wakeup gets a fresh one-shot cron, so the expression
            # says nothing the timestamp doesn't.
            return ("start", "tick")
        if rec.get("subtype") == "turn_duration":
            return ("end", None)
        return None
    text = prompt_text(rec)
    if text is None:
        return None
    if text.startswith("[Request interrupted"):
        return ("end", None)
    if not in_turn or not text.startswith("<"):
        return ("start", prompt_label(text))
    return None


_MARKERS = (b'"scheduled_task_fire"', b'"turn_duration"', b'"type":"user"')


def find_start_offset(path, turns=None, since=None):
    """Byte offset where replay should begin: the start of the Nth-last turn, or
    of the first turn at/after `since`. Reads backwards in growing windows, so a
    100MB transcript costs a few MB for the usual case."""
    size = path.stat().st_size
    window = 4 << 20
    while True:
        lo = max(0, size - window)
        with open(path, "rb") as f:
            f.seek(lo)
            data = f.read(size - lo)
        pos = lo
        if lo:
            cut = data.find(b"\n") + 1
            data, pos = data[cut:], lo + cut
        starts = []  # (offset, ts)
        in_turn = False
        for line in data.split(b"\n"):
            if any(m in line for m in _MARKERS) and (rec := _loads(line)):
                mark = turn_marker(rec, in_turn)
                if mark and mark[0] == "start":
                    starts.append((pos, parse_ts(rec.get("timestamp"))))
                    in_turn = True
                elif mark:
                    in_turn = False
            pos += len(line) + 1
        if since is not None:
            if lo == 0 or (starts and starts[0][1] and starts[0][1] < since):
                after = [o for o, ts in starts if ts and ts >= since]
                return after[0] if after else size
        elif len(starts) >= turns or lo == 0:
            return starts[-turns][0] if len(starts) >= turns else (starts[0][0] if starts else 0)
        window *= 4


def tool_summary(name, inp):
    inp = inp or {}
    if name == "Bash":
        return inp.get("description") or first_line(inp.get("command", ""))
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return tilde(inp.get("file_path") or inp.get("notebook_path") or "")
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    if name == "ScheduleWakeup":
        return f"in {fmt_dur(inp.get('delaySeconds', 0))}" + (
            f" · {inp['reason']}" if inp.get("reason") else ""
        )
    if name == "Skill":
        return inp.get("skill", "")
    for v in inp.values():
        if isinstance(v, str) and v:
            return first_line(v)
    return ""


@dataclass
class Agent:
    tool_use_id: str
    desc: str
    kind: str = ""
    model: str = ""
    background: bool = False
    agent_id: str = ""
    started: datetime | None = None
    done: bool = False
    done_at: datetime | None = None
    tail: Tail | None = None
    depth: int = 1
    color: str = "36"

    @property
    def tag(self):
        if len(self.desc) <= TAG_WIDTH:
            return self.desc
        cut = self.desc[: TAG_WIDTH - 1]
        if self.desc[TAG_WIDTH - 1] != " " and " " in cut[8:]:
            cut = cut.rsplit(" ", 1)[0]
        return cut.rstrip() + "…"


@dataclass
class Turn:
    label: str
    started: datetime | None
    tools: int = 0
    agents: int = 0
    last_text: str = ""
    detail: list = field(default_factory=list)
    loud: bool = False  # did something a repeat would hide
    held: bool = False  # live, and its lines are buffered until it ends
    mark: int = 0  # lines written before its header, for --redraw
    after_run: bool = False  # its header went right under the run line


@dataclass
class Run:
    """Quiet ticks that repeated the one printed in full above them."""

    key: str
    day: object
    first: datetime | None = None
    last: datetime | None = None
    count: int = 0
    durs: list = field(default_factory=list)
    next: datetime | None = None
    dirty: bool = False  # replay: counted but not printed yet


class Watcher:
    def __init__(
        self, path, out=sys.stdout, color=False, width=None, verbose=False, redraw=False, tty=False
    ):
        self.path = path
        self.subdir = path.with_suffix("") / "subagents"
        self.out = out
        self.color = color
        self.width = width
        self.verbose = verbose
        self.redraw = redraw
        self.tty = tty
        self.run = None
        self.run_bottom = False  # the run line is the last line on screen
        self.written = 0
        self.live = False
        self.turn = None
        self.agents = {}  # tool_use_id -> Agent
        self.metas = {}  # tool_use_id -> (agent_id, meta)
        self.next_wakeup = None
        self.last_day = None
        self.status_shown = False

    # -- output ----------------------------------------------------------------

    def c(self, code, s):
        return f"\x1b[{code}m{s}\x1b[0m" if self.color and s else s

    def cols(self):
        return self.width or shutil.get_terminal_size((120, 40)).columns

    def fit(self, prefix_len, text):
        room = max(10, self.cols() - prefix_len)
        text = re.sub(r"\s*\n\s*", " ", text.strip())
        return text if len(text) <= room else text[: room - 1] + "…"

    def rows(self):
        return shutil.get_terminal_size((120, 40)).lines

    def clear_status(self):
        if self.status_shown:
            self.out.write("\r\x1b[K")
            self.status_shown = False

    def _put(self, line):
        self.clear_status()
        self.out.write(line + "\n")
        self.out.flush()
        self.written += 1

    def write(self, line):
        self.settle_run()
        self._put(line)
        self.run_bottom = False

    def settle_run(self):
        if self.run and self.run.dirty:
            self.run.dirty = False
            self._put(self.run_line())
            self.run_bottom = True

    def day_rule(self, ts):
        day = ts.astimezone().date() if ts else None
        if day and day != self.last_day:
            self.last_day = day
            self.write(self.c("2", f"── {day:%a %Y-%m-%d} " + "─" * 20))

    def emit(self, rendered, notable=False):
        """Live: print now. Replay: buffer inside a turn, so a finished turn can
        collapse to one line; outside a turn, print only what is notable."""
        if notable:
            self.stir()
        if self.live and not (self.turn and self.turn.held):
            self.write(rendered)
        elif self.turn:
            self.turn.detail.append((notable, rendered))
        elif notable or self.verbose:
            self.write(rendered)

    def line(self, prefix, prefix_len, text, style=None, notable=False):
        body = self.fit(prefix_len, text)
        self.emit(prefix + (self.c(style, body) if style else body), notable)

    def status(self):
        if not (self.live and self.color):
            return
        now = datetime.now(timezone.utc)
        parts = []
        if t := self.turn:
            secs = (now - t.started).total_seconds() if t.started else 0
            parts.append(f"working {fmt_dur(secs)}")
            if t.held:
                parts.append(f"{t.tools} tool{'s' * (t.tools != 1)}")
        else:
            parts.append("idle")
            if self.next_wakeup and self.next_wakeup > now:
                left = (self.next_wakeup - now).total_seconds()
                parts.append(f"next wakeup {hhmm(self.next_wakeup)} (in {fmt_dur(left)})")
        running = sum(1 for a in self.agents.values() if a.tail and not a.done)
        if running:
            parts.append(f"{running} agent{'s' if running > 1 else ''} running")
        self.out.write("\r\x1b[K" + self.c("2", "  ⋯ " + self.fit(4, " · ".join(parts))))
        self.out.flush()
        self.status_shown = True

    # -- turns -----------------------------------------------------------------

    def start_turn(self, label, ts):
        if self.turn:
            self.end_turn(None, ts)
        self.turn = Turn(label, ts)
        if self.live:
            if label == "tick" and not (self.redraw or self.verbose):
                self.turn.held = True
            else:
                self.header(self.turn)

    def header(self, t):
        self.day_rule(t.started)
        t.after_run, t.mark = self.run_bottom, self.written
        head = f"── {hhmm(t.started)} "
        self.write(self.c("1", head) + self.c("1;36", self.fit(len(head), t.label)))

    def release(self, t):
        """Print a held tick so far; it streams from here on."""
        t.held = False
        self.header(t)
        for _, rendered in t.detail:
            self.write(rendered)
        t.detail = []

    def stir(self):
        """The open turn did something worth seeing, so it is no repeat."""
        if t := self.turn:
            t.loud = True
            if t.held:
                self.release(t)

    def quiet_key(self, t):
        if self.verbose or t.label != "tick" or t.loud:
            return None
        # Counts change between otherwise identical reports ("3 PRs open").
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", first_line(t.last_text).lower()))

    def run_line(self):
        r = self.run
        span = hhmm(r.first)
        if hhmm(r.last) != span:
            span += f"–{hhmm(r.last)}"
        lo, hi = fmt_dur(min(r.durs)), fmt_dur(max(r.durs))
        parts = [f"×{r.count} same result", span, lo if lo == hi else f"{lo}–{hi}"]
        if r.next:
            parts.append(f"next {hhmm(r.next)}")
        return "   " + self.c("2", self.fit(3, "≡ " + " · ".join(parts)))

    def extend_run(self, t, secs, nxt):
        """Fold a tick that repeats the run's into the run line. False if it
        is no repeat and has to print in full."""
        key, r = self.quiet_key(t), self.run
        if key is None or not r or r.key != key or r.day != local_day(t.started):
            return False
        r.count += 1
        r.first = r.first or t.started
        r.last = t.started
        r.durs.append(secs)
        r.next = nxt
        if not self.live:
            r.dirty = True
            return True
        # Lines above the cursor that the new run line replaces: the old run
        # line, and under --redraw the tick that streamed beneath it.
        gone = 1 if self.run_bottom else 0
        if not t.held:
            gone = self.written - t.mark + t.after_run
        self.clear_status()
        if gone and self.tty and gone < self.rows():
            self.out.write(f"\x1b[{gone}A\r\x1b[J")
        self._put(self.run_line())
        self.run_bottom = True
        return True

    def end_turn(self, duration_ms, ts):
        t = self.turn
        self.turn = None
        if duration_ms is None and t.started and ts:
            duration_ms = (ts - t.started).total_seconds() * 1000
        secs = (duration_ms or 0) / 1000
        if secs > HOLD_SECONDS:
            t.loud = True
        wake = self.next_wakeup and ts and self.next_wakeup > ts
        if self.extend_run(t, secs, self.next_wakeup if wake else None):
            return
        self.print_turn(t, secs, wake)
        key = self.quiet_key(t)
        self.run = Run(key, local_day(t.started)) if key is not None else None

    def print_turn(self, t, secs, wake):
        dur = fmt_dur(secs)
        tools = f"{t.tools} tool{'s' * (t.tools != 1)}"
        agents = f"{t.agents} agent{'s' * (t.agents != 1)}" if t.agents else ""
        nxt = f"next {hhmm(self.next_wakeup)}" if wake else ""
        if self.live:
            if t.held:
                self.release(t)
            self.write(self.c("2", "   └ " + " · ".join(x for x in (dur, tools, agents, nxt) if x)))
            return
        self.day_rule(t.started)
        # Fixed-width columns so turns line up and the label starts in one
        # place; the summary gets a line of its own. A narrow terminal can't
        # spare the padding, so there the stats are just joined.
        if self.cols() < 80:
            cols = " ".join(x for x in (dur, tools, agents, nxt) if x) + "  "
        else:
            cols = f"{dur:>6}  {tools:>8}  {agents:<8}  {nxt:<10}  "
        head = f"── {hhmm(t.started)} "
        label = self.fit(len(head) + len(cols), t.label)
        self.write(self.c("1", head) + self.c("2", cols) + self.c("36", label))
        if summary := first_line(t.last_text):
            self.write("     " + self.c("2", "↳ ") + self.fit(7, summary))
        for notable, rendered in t.detail:
            if notable or self.verbose:
                self.write(rendered)

    def flush_open_turn(self):
        self.live = True
        self.settle_run()
        if self.turn:
            self.header(self.turn)
            for _, rendered in self.turn.detail:
                self.write(rendered)
            self.turn.detail = []

    def go_live(self):
        """Switch from replay to streaming, flushing a turn still in progress."""
        self.flush_open_turn()
        self.load_metas()
        now = time.time()
        for a in self.agents.values():
            if a.done or a.tool_use_id in self.metas:
                continue
            # No meta yet: if it was just spawned, attach_agents picks it up
            # once the meta lands. Anything older never had one to find, and
            # attaching it would replay its whole transcript.
            if not (a.started and now - a.started.timestamp() < STALE_AGENT_SECONDS):
                a.done = True
        for tid, (agent_id, meta) in self.metas.items():
            a = self.agents.get(tid)
            if a and (a.done or a.tail):
                continue
            f = self.subdir / f"agent-{agent_id}.jsonl"
            try:
                idle = now - f.stat().st_mtime
                stale = idle > STALE_AGENT_SECONDS or _finished(f, idle)
            except FileNotFoundError:
                stale = True
            if stale:
                # Settled, so attach_agents won't replay its whole transcript.
                if a:
                    a.done = True
                continue
            if a is None:
                a = self.add_agent(tid, meta.get("description", agent_id), None, meta)
            a.agent_id = agent_id
            a.tail = Tail(f, f.stat().st_size)
            self.agent_line(a, "⧗", f"running · last write {fmt_dur(idle)} ago · following", "2")

    # -- records ---------------------------------------------------------------

    def feed(self, rec):
        rtype = rec.get("type")
        if rtype in NOISE_TYPES:
            return
        ts = parse_ts(rec.get("timestamp"))
        mark = turn_marker(rec, self.turn is not None)
        if mark and mark[0] == "start":
            self.start_turn(mark[1], ts)
            return
        if mark:
            if self.turn:
                self.end_turn(rec.get("durationMs"), ts)
            return
        if rtype == "queue-operation" and rec.get("operation") == "enqueue":
            self.notification(rec.get("content") or "", ts)
        elif rtype == "system":
            self.system(rec)
        elif rtype == "assistant":
            self.assistant(rec, ts, None)
        elif rtype == "user":
            self.tool_results(rec, ts, None)

    def system(self, rec):
        sub = rec.get("subtype")
        if sub == "away_summary":
            self.line("   ", 3, "ⓘ " + first_line(rec.get("content", "")), "2;3", notable=True)
        elif sub == "compact_boundary":
            self.line("   ", 3, "⋯ context compacted", "2")

    def assistant(self, rec, ts, agent):
        for b in message_blocks(rec):
            if b.get("type") == "text" and b.get("text", "").strip():
                if agent:
                    self.sub_line(agent, "»", first_line(b["text"]), "3")
                else:
                    if self.turn:
                        self.turn.last_text = b["text"]
                    self.line("   ", 3, "» " + first_line(b["text"]), "3")
            elif b.get("type") == "tool_use":
                self.tool_use(b, ts, agent)

    def tool_use(self, b, ts, agent):
        name, inp = b.get("name", "?"), b.get("input") or {}
        if name in ("Agent", "Task"):
            a = self.add_agent(
                b.get("id"), inp.get("description") or "agent", ts, inp, parent=agent
            )
            if self.turn and not agent:
                self.turn.agents += 1
            self.stir()
            kind = "/".join(x for x in (a.kind, a.model) if x)
            self.agent_line(
                a,
                "⧗",
                f"spawned {kind}" + (" · background" if a.background else ""),
                None,
                notable=False,
            )
            return
        if name == "SubagentHandback" and agent:
            self.finish(agent, inp.get("message", ""), ts)
            return
        if agent:
            self.sub_line(agent, "·", f"{name}  {tool_summary(name, inp)}")
            return
        if self.turn:
            self.turn.tools += 1
        if name in EDIT_TOOLS:
            self.stir()
        if name == "ScheduleWakeup" and ts and inp.get("delaySeconds"):
            self.next_wakeup = ts + timedelta(seconds=inp["delaySeconds"])
        self.line("   " + self.c("1", name) + "  ", 5 + len(name), tool_summary(name, inp))

    def tool_results(self, rec, ts, agent):
        for b in message_blocks(rec):
            if b.get("type") != "tool_result":
                continue
            tid = b.get("tool_use_id")
            content = b.get("content")
            text = (
                content
                if isinstance(content, str)
                else "\n".join(x.get("text", "") for x in content or [] if isinstance(x, dict))
            )
            a = self.agents.get(tid)
            if a:
                if text.startswith("Async agent launched"):
                    m = re.search(r"agentId: (\w+)", text)
                    if m:
                        a.agent_id = m.group(1)
                elif not a.done:
                    self.finish(a, text, ts)
            elif b.get("is_error") and not agent:
                self.stir()
                self.line("   ", 3, "✗ " + first_line(text), "31")

    def notification(self, text, ts):
        if not text.startswith("<task-notification>"):
            return
        a = self.agents.get(tag_text(text, "tool-use-id"))
        if a:
            if not a.done:
                self.finish(a, tag_text(text, "summary"), ts)
            return
        status = tag_text(text, "status")
        summary = tag_text(text, "summary") or "background task"
        if status and status not in summary:
            summary += f" ({status})"
        self.line("   ", 3, "⚑ " + summary, "2", notable=True)

    # -- agents ----------------------------------------------------------------

    def add_agent(self, tid, desc, ts, inp, parent=None):
        a = Agent(
            tool_use_id=tid,
            desc=desc,
            kind=inp.get("subagent_type") or inp.get("agentType") or "",
            model=inp.get("model") or "",
            background=bool(inp.get("run_in_background"))
            or inp.get("requestShape") == "background",
            started=ts,
            depth=(parent.depth + 1) if parent else 1,
            color=AGENT_COLORS[len(self.agents) % len(AGENT_COLORS)],
        )
        self.agents[tid] = a
        return a

    def finish(self, a, summary, ts):
        if a.done:
            return
        a.done, a.done_at = True, ts
        took = f" {fmt_dur((ts - a.started).total_seconds())}" if ts and a.started else ""
        self.agent_line(a, "✓", f"done{took} · {first_line(summary)}", None, notable=True)

    def agent_line(self, a, mark, text, style, notable=True):
        indent = "   " * a.depth
        head = f"{indent}{mark} agent {a.tag}  "
        body = self.fit(len(head), text)
        self.emit(
            indent
            + self.c(a.color, f"{mark} agent {a.tag}")
            + "  "
            + (self.c(style, body) if style else body),
            notable,
        )

    def sub_line(self, a, mark, text, style=None):
        if not self.live:
            return
        indent = "   " * a.depth
        head = f"{indent}┊ {a.tag:<{TAG_WIDTH}} {mark} "
        body = self.fit(len(head), text)
        self.emit(
            indent
            + self.c(a.color, f"┊ {a.tag:<{TAG_WIDTH}}")
            + f" {mark} "
            + (self.c(style, body) if style else body),
            notable=True,
        )

    def load_metas(self):
        try:
            entries = list(self.subdir.glob("*.meta.json"))
        except OSError:
            return
        for f in entries:
            agent_id = f.name[len("agent-") : -len(".meta.json")]
            if any(v[0] == agent_id for v in self.metas.values()):
                continue
            try:
                meta = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if meta.get("toolUseId"):
                self.metas[meta["toolUseId"]] = (agent_id, meta)

    def attach_agents(self):
        """Open a tail on each live-spawned agent once its transcript is known."""
        pending = [a for a in self.agents.values() if a.tail is None and not a.done]
        if not pending:
            return
        if any(a.tool_use_id not in self.metas for a in pending):
            self.load_metas()
        for a in pending:
            if a.tool_use_id in self.metas:
                agent_id, meta = self.metas[a.tool_use_id]
                a.agent_id = agent_id
                a.kind = a.kind or meta.get("agentType", "")
                a.model = a.model or meta.get("model", "")
            if a.agent_id:
                a.tail = Tail(self.subdir / f"agent-{a.agent_id}.jsonl")

    def poll(self):
        # Subagents first: a handback and the parent's task-notification usually
        # land in the same interval, and the handback carries the real summary.
        self.attach_agents()
        for a in list(self.agents.values()):
            if not a.tail:
                continue
            for rec in a.tail.read():
                ts = parse_ts(rec.get("timestamp"))
                if rec.get("type") == "assistant":
                    # Only a write after completion is a resume; one stamped
                    # earlier is the tail catching up with the notification.
                    if a.done and ts and a.done_at and ts > a.done_at:
                        a.done = False
                        self.agent_line(a, "↻", "resumed", "2")
                    if not a.done:
                        self.assistant(rec, ts, a)
                elif rec.get("type") == "user":
                    self.tool_results(rec, ts, a)
        for rec in self.main.read():
            self.feed(rec)
        self.attach_agents()
        t = self.turn
        if t and t.held and t.started:
            if (datetime.now(timezone.utc) - t.started).total_seconds() > HOLD_SECONDS:
                self.stir()

    def replay(self, offset):
        self.main = Tail(self.path, offset)
        for rec in self.main.read():
            self.feed(rec)


def _finished(path, idle):
    """A background agent ends on a handback. A foreground one just stops on a
    message with no tool call; its text and the tool call after it are
    separate records written moments apart, so it must also have gone quiet."""
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - 65536))
        data = f.read()
    if b'"SubagentHandback"' in data:
        return True
    recs = (_loads(line) for line in reversed(data.split(b"\n")))
    last = next((r for r in recs if r and r.get("type") in ("assistant", "user")), None)
    if not last or last["type"] != "assistant" or idle < 30:
        return False
    return not any(b.get("type") == "tool_use" for b in message_blocks(last))


def parse_since(s):
    m = re.fullmatch(r"(\d+)([smhd])", s)
    if not m:
        raise argparse.ArgumentTypeError(f"expected e.g. 30m, 6h, 2d — got {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="claude-watch",
        description="Follow a Claude Code session: turns and /loop ticks, tool calls, "
        "and the subagents it spawns (followed inline while they run).",
    )
    ap.add_argument("session", help="session id, unique id prefix, or transcript path")
    ap.add_argument(
        "-n", "--turns", type=int, default=5, help="turns of history to show first (default 5)"
    )
    ap.add_argument(
        "--since", type=parse_since, help="history since a duration ago (30m, 6h, 2d) instead of -n"
    )
    ap.add_argument("--no-follow", action="store_true", help="print history and exit")
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="expand history turns to every tool call, and show repeat ticks in full",
    )
    ap.add_argument(
        "--redraw",
        action="store_true",
        help="stream each tick as it runs, then redraw a repeat into the counter line "
        "(default: hold a tick until it ends)",
    )
    ap.add_argument(
        "--color", action=argparse.BooleanOptionalAction, default=None, help="force color on/off"
    )
    ap.add_argument(
        "--interval", type=float, default=1.0, help="poll interval in seconds (default 1)"
    )
    args = ap.parse_args(argv)

    path = resolve_session(args.session)
    color = (
        args.color
        if args.color is not None
        else (sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    )
    w = Watcher(
        path, color=color, verbose=args.verbose, redraw=args.redraw, tty=sys.stdout.isatty()
    )
    since = datetime.now(timezone.utc) - args.since if args.since else None
    w.write(w.c("2", w.fit(0, f"claude-watch {path.stem}  ({tilde(str(path.parent))})")))
    w.replay(find_start_offset(path, turns=max(1, args.turns), since=since))
    if args.no_follow:
        w.flush_open_turn()
        return
    w.go_live()
    saved = _echo_off()
    try:
        while True:
            w.status()
            time.sleep(args.interval)
            w.poll()
    except KeyboardInterrupt:
        if w.status_shown:
            w.out.write("\r\x1b[K")
    finally:
        _echo_restore(saved)


def _echo_off():
    """Stop the terminal echoing keystrokes while following: an echoed Enter
    moves the cursor off the status line, which then stays behind as a stale
    line. Returns the settings to restore, or None when stdin isn't a tty."""
    try:
        import termios
    except ImportError:
        return None
    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):
        return None
    quiet = list(saved)
    quiet[3] &= ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
    return saved


def _echo_restore(saved):
    if saved is None:
        return
    import termios

    # TCSAFLUSH also drops whatever was typed while echo was off, so it
    # doesn't land on the shell's prompt.
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, saved)


if __name__ == "__main__":
    main()
