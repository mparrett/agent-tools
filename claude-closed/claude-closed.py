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
import html
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
            how = {"killed": "stopped", "done": "exited", "crashed": "crashed"}.get(
                reason, reason or "unknown"
            )
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


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Closed sessions</title>
<style>
:root {
  --bg: #fbfbfa; --fg: #1d1d1b; --muted: #6b6b66; --line: #e6e5e1; --code: #f1f0ec;
  --stopped: #b3471d; --retired: #4a6a8a; --exited: #3d7a4f; --crashed: #a3263a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #18181a; --fg: #e8e7e3; --muted: #9a9993; --line: #2c2c2f; --code: #232326;
    --stopped: #f0875a; --retired: #8fb0d3; --exited: #79c38f; --crashed: #f07a8c;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 20px; }
.note { border-left: 3px solid var(--stopped); padding: 6px 10px; margin: 0 0 16px; color: var(--muted); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px; padding: 0 10px 8px; }
td { padding: 10px; border-top: 1px solid var(--line); vertical-align: top; }
td.n, td.ago { color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
.name { font-weight: 500; }
.dir { color: var(--muted); font-size: 12px; }
.how { font-size: 12px; font-weight: 600; white-space: nowrap; }
.how small { font-weight: 400; color: var(--muted); }
.how-stopped { color: var(--stopped); } .how-retired { color: var(--retired); }
.how-exited { color: var(--exited); } .how-crashed { color: var(--crashed); }
.tag { font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; margin-left: 6px; }
.cmd { display: flex; gap: 6px; align-items: center; }
code {
  font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--code);
  padding: 3px 6px; border-radius: 4px; overflow-wrap: anywhere; user-select: all;
}
button {
  font: inherit; font-size: 12px; color: var(--fg); background: transparent; cursor: pointer;
  border: 1px solid var(--line); border-radius: 4px; padding: 2px 8px; white-space: nowrap;
}
button:hover { border-color: var(--muted); }
.empty { color: var(--muted); padding: 24px 0; }
@media (max-width: 720px) {
  thead { display: none; }
  tr { display: block; border-top: 1px solid var(--line); padding: 10px 0; }
  td { display: block; border: 0; padding: 2px 0; }
  td.n { display: none; }
}
</style>
</head>
<body>
<main>
<h1>Closed sessions</h1>
<p class="sub">{subtitle}</p>
{notes}
{body}
</main>
<script>
// Clipboard API needs a secure context, which a file:// page may not be; fall
// back to selecting the text so a manual copy still works.
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-cmd]");
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.cmd);
    b.textContent = "copied";
  } catch {
    getSelection().selectAllChildren(b.previousElementSibling);
    b.textContent = "selected";
  }
  setTimeout(() => (b.textContent = "copy"), 1200);
});
</script>
</body>
</html>
"""


def window_label(since):
    minutes = int(since.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    # "24h" reads better than "1d"; days only once they are the natural unit.
    if minutes >= 48 * 60 and minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def render_html(rows, now, since, problems, matched=None, closed=None, stopped_only=False):
    """`matched` counts rows before -n cut them, `closed` before --stopped filtered them."""
    e = html.escape
    matched = len(rows) if matched is None else matched
    verb = "stopped" if stopped_only else "closed"
    subtitle = (
        f"{matched} background session{'s' if matched != 1 else ''} {verb} in the last "
        f"{window_label(since)}"
    )
    if stopped_only and closed is not None:
        subtitle += f" ({closed} closed in all)"
    if len(rows) < matched:
        subtitle += f", showing the newest {len(rows)}"
    else:
        subtitle += ", newest first"
    subtitle += f" &middot; generated {e(now.astimezone().strftime('%Y-%m-%d %H:%M'))}"
    notes = "".join(f'<p class="note">{e(p)}</p>' for p in problems)
    if not rows:
        body = f'<p class="empty">No background sessions {verb} in that window.</p>'
    else:
        out = [
            "<table><thead><tr><th>#</th><th>Closed</th><th>How</th><th>Session</th>"
            "<th>Reopen</th></tr></thead><tbody>"
        ]
        for n, r in enumerate(rows, 1):
            cmd = f"claude --bg --resume {r['sessionId']}"
            idle = f" <small>{e(r['idle'])}</small>" if r["idle"] else ""
            tag = '<span class="tag">removed from list</span>' if r["removed"] else ""
            closed_at = r["_when"].astimezone().strftime("%Y-%m-%d %H:%M")
            out.append(
                f'<tr><td class="n">{n}</td>'
                f'<td class="ago" title="{e(closed_at)}">{e(ago(r["_when"], now))} ago</td>'
                f'<td class="how how-{e(r["how"])}">{e(r["how"])}{idle}</td>'
                f'<td><span class="name">{e(r["name"] or "(unnamed)")}</span>{tag}'
                f'<div class="dir">{e(tilde(r["cwd"]))}</div></td>'
                f'<td><div class="cmd"><code>{e(cmd)}</code>'
                f'<button type="button" data-cmd="{e(cmd)}">copy</button></div></td></tr>'
            )
        out.append("</tbody></table>")
        body = "".join(out)
    # str.replace rather than str.format: the CSS and script are full of braces.
    return PAGE.replace("{subtitle}", subtitle).replace("{notes}", notes).replace("{body}", body)


def duration(text):
    m = DURATION.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"expected e.g. 30m, 6h or 3d, not {text!r}")
    n, unit = int(m[1]), m[2]
    if n == 0:
        raise argparse.ArgumentTypeError("the window must be longer than zero")
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
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="emit the rows as JSON")
    fmt.add_argument(
        "--html",
        action="store_true",
        help="emit a self-contained HTML page (redirect it to a file and open it)",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = datetime.now(timezone.utc)
    rows, problems = closed_sessions(now, args.since)
    # stderr, so --json output stays parseable while a script still sees the caveat.
    for problem in problems:
        print(f"claude-closed: {problem}", file=sys.stderr)
    closed = len(rows)
    if args.stopped:
        rows = [r for r in rows if r["how"] == "stopped"]
    matched = len(rows)
    rows = rows[: args.limit]

    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "_when"} for r in rows], indent=2))
        return 0
    if args.html:
        page = render_html(rows, now, args.since, problems, matched, closed, args.stopped)
        print(page, end="")
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
