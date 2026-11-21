#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""claude-closed — list recently closed Claude Code background sessions, with how to reopen each.

In agent view, ctrl+x stops a session and ctrl+x twice removes it from the list.
Either is easy to press too eagerly, and a removed session no longer shows in
`claude agents --all`. The daemon log still records every session it settled, so
this reads that, drops sessions that are running again, and fills in each
session's name and directory from `claude agents --all` or, for a removed one,
from its transcript.

Each row says how the session ended:

  stopped   ctrl+x, `claude stop`, or a tool such as claude-down
  retired   the daemon's own idle timeout
  exited    finished on its own
  crashed   the session process died

and gives `claude --bg --resume <session-id>`, which brings it back into the
background under the same id.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CLOSED_CLAUDE_DIR") or Path.home() / ".claude")

LOG_LINE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] \[bg\] bg (?P<event>[a-z-]+) (?P<id>[0-9a-f]{8})\b(?P<rest>.*)"
)
# `settled <id> (killed)`: the reason the session's process went away.
SETTLED = re.compile(r"\((?P<reason>[a-z-]+)\)")
# `retire <id>: idle-prompt, idle 60m`: the daemon chose to end it, and why.
RETIRE = re.compile(r":\s*(?P<why>[a-z-]+)(?:,\s*(?P<idle>idle \w+))?")
# A retire is followed by its settle within moments; past this they are unrelated.
RETIRE_SETTLE_WINDOW = timedelta(seconds=30)
# Pre-claimed hosts the daemon keeps warm; they were never anyone's session.
SPARE_REASONS = {"stale-spare", "orphan-spare"}

DURATION = re.compile(r"^(\d+)([mhd])$")


def parse_ts(text):
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def read_log_events(paths):
    """Settle and claim events per short id, in log order."""
    events = {}
    for path in paths:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            m = LOG_LINE.match(line)
            if not m:
                continue
            ts = parse_ts(m["ts"])
            if ts is None:
                continue
            events.setdefault(m["id"], []).append((ts, m["event"], m["rest"]))
    for evs in events.values():
        evs.sort(key=lambda e: e[0])
    return events


def last_close(evs):
    """(when, how) for the session's latest close, or None if its last event isn't one.

    A session can be reopened and closed again, and only the latest close counts;
    one whose latest event is a claim is running (or starting) again.
    """
    closed = None
    retired = None
    for ts, event, rest in evs:
        if event == "retire":
            m = RETIRE.search(rest)
            retired = (ts, m["why"] if m else "", (m["idle"] or "") if m else "")
        elif event == "settled":
            m = SETTLED.search(rest)
            reason = m["reason"] if m else ""
            how = {"killed": "stopped", "done": "exited", "crashed": "crashed"}.get(reason, reason)
            if retired and ts - retired[0] <= RETIRE_SETTLE_WINDOW:
                closed = (ts, "retired", retired[1], retired[2])
            else:
                closed = (ts, how, "", "")
            retired = None
        elif event in ("claimed-spare", "spawned", "adopt"):
            closed = None
    return closed


def run_json(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def transcript_meta(short_id):
    """sessionId, cwd and title for a session, from its transcript, or None."""
    found = []
    for path in (CLAUDE_DIR / "projects").glob(f"*/{short_id}-*.jsonl"):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue  # removed since the glob matched
    for _, path in sorted(found, reverse=True):
        meta = {"sessionId": path.stem, "cwd": None, "name": None}
        agent_name = None
        try:
            with open(path, "rb") as f:
                for raw in f:
                    try:
                        e = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(e, dict):
                        continue
                    if e.get("cwd"):
                        meta["cwd"] = e["cwd"]
                    # The user-facing name; the latest rename wins.
                    if e.get("type") == "custom-title" and e.get("customTitle"):
                        meta["name"] = e["customTitle"]
                    elif e.get("type") == "agent-name" and e.get("agentName"):
                        agent_name = e["agentName"]
        except OSError:
            continue
        meta["name"] = meta["name"] or agent_name
        return meta
    return None


def ago(then, now):
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 120:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def tilde(path):
    home = str(Path.home())
    if path and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path or "?"


def closed_sessions(now, since):
    events = read_log_events(sorted(CLAUDE_DIR.glob("daemon.log*")))
    live = run_json(["claude", "agents", "--json"])
    listed = run_json(["claude", "agents", "--json", "--all"])
    live_ids = {r["id"] for r in (live or []) if r.get("kind") == "background"}
    by_id = {r["id"]: r for r in (listed or []) if r.get("kind") == "background"}

    rows = []
    for short, evs in events.items():
        close = last_close(evs)
        if close is None or short in live_ids:
            continue
        when, how, why, idle = close
        if when < now - since or why in SPARE_REASONS:
            continue
        row = by_id.get(short)
        meta = None if row else transcript_meta(short)
        if row is None and meta is None:
            # No listing and no transcript: a warm spare the daemon discarded.
            continue
        rows.append(
            {
                "id": short,
                "sessionId": (row or meta)["sessionId"],
                "name": (row or meta).get("name"),
                "cwd": (row or meta).get("cwd"),
                "closedAt": when.isoformat(),
                "how": how,
                "why": why,
                "idle": idle,
                # ctrl+x twice removes it from `claude agents`; it is still resumable.
                # Unknown (None) when the full listing couldn't be read.
                "removed": None if listed is None else row is None,
                "_when": when,
            }
        )
    rows.sort(key=lambda r: r["_when"], reverse=True)
    problems = []
    if live is None:
        problems.append(
            "`claude agents --json` failed, so sessions running again may be listed too"
        )
    if listed is None:
        problems.append(
            "`claude agents --json --all` failed, so whether each session was removed "
            "from the list is unknown"
        )
    return rows, problems


def duration(text):
    m = DURATION.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"expected e.g. 30m, 6h or 3d, not {text!r}")
    n, unit = int(m[1]), m[2]
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def positive(text):
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"expected a positive count, not {text!r}")
    return n


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="claude-closed",
        description="List recently closed Claude Code background sessions, newest first, "
        "with the command that reopens each.",
    )
    p.add_argument(
        "--since",
        type=duration,
        default=timedelta(hours=24),
        metavar="AGE",
        help="how far back to look: 30m, 6h, 3d (default 24h)",
    )
    p.add_argument(
        "--stopped",
        action="store_true",
        help="only sessions someone stopped (ctrl+x, `claude stop`), not ones that "
        "retired, exited or crashed",
    )
    p.add_argument(
        "-n", "--limit", type=positive, default=20, help="most rows to show (default 20)"
    )
    p.add_argument("--json", action="store_true", help="emit the rows as JSON")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = datetime.now(timezone.utc)
    rows, problems = closed_sessions(now, args.since)
    # stderr, so --json output stays parseable while a script still sees the caveat.
    for problem in problems:
        print(f"claude-closed: {problem}", file=sys.stderr)
    if args.stopped:
        rows = [r for r in rows if r["how"] == "stopped"]
    rows = rows[: args.limit]

    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "_when"} for r in rows], indent=2))
        return 0
    if not rows:
        print("no background sessions closed in that window")
        return 0
    for n, r in enumerate(rows, 1):
        how = r["how"] + (f" ({r['idle']})" if r["idle"] else "")
        flag = "  removed from list" if r["removed"] else ""
        print(
            f"{n:>3}. {ago(r['_when'], now):>4} ago  {how:<18} "
            f"{r['name'] or '(unnamed)'}  [{tilde(r['cwd'])}]{flag}"
        )
        print(f"       reopen: claude --bg --resume {r['sessionId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
