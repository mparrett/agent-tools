#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""claude-down — stop every Claude Code background session, gracefully, before a reboot.

`/exit` in agent view backgrounds a session rather than ending it, and the daemon
only retires sessions that have settled. One that left a monitor or server running
stays busy forever, so background sessions accumulate. Claude Code has
`claude stop <id>` for one session and `claude daemon stop` for all of them at
once; this sequences the two:

  1. snapshot `claude agents --json`, with a `claude attach <id>` line per session
  2. stop the idle sessions
  3. give busy ones --grace seconds to finish their turn, then stop the rest
  4. `claude daemon stop --any`

With no flags it prints the plan and changes nothing; --go carries it out.
`claude stop` keeps the conversation, so each session can be resumed afterwards.

The session running this is skipped, along with the daemon stop, since stopping
the daemon would end it too. Pass --include-self to stop it last.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

POLL_SECONDS = 5
# `claude daemon stop` waits on its workers; past this the daemon is wedged and
# the operator needs to know rather than watch a silent prompt.
CMD_TIMEOUT_SECONDS = 120


def run(cmd, timeout=CMD_TIMEOUT_SECONDS):
    """Run a command; return (stdout, error). Exactly one is None."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip() or f"exit {p.returncode}"
    return p.stdout, None


def list_background():
    out, err = run(["claude", "agents", "--json"])
    if out is None:
        sys.exit(f"claude-down: claude agents --json failed: {err}")
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as e:
        sys.exit(f"claude-down: bad JSON from claude agents: {e}")
    # The listing includes interactive sessions too; `claude stop` only manages
    # background ones, and an interactive one belongs to whoever is at its terminal.
    return [r for r in rows if r.get("kind") == "background"]


def holders_by_session():
    """sessionId -> commands a session holds open in the background, from claude-ps.

    This is what keeps a "done" session busy, so it is what to read before
    stopping it. claude-ps is optional: without it the plan just omits the column.
    """
    out, _ = run(["claude-ps", "--shells", "--json"], timeout=30)
    if out is None:
        return {}
    try:
        shells = json.loads(out)
    except json.JSONDecodeError:
        return {}
    held = {}
    for s in shells:
        if s.get("kind") == "background" and s.get("session_id"):
            held.setdefault(s["session_id"], []).append(s.get("command", ""))
    return held


def is_busy(row):
    return row.get("status") == "busy"


def describe(row, held):
    state = "busy" if is_busy(row) else "idle"
    line = f"  {row['id']}  {state:4}  {row.get('name') or '(unnamed)'}  [{row.get('cwd', '')}]"
    for cmd in held.get(row.get("sessionId"), []):
        first = cmd.splitlines()[0] if cmd else ""
        line += f"\n            holds: {first[:100]}"
    return line


def write_snapshot(rows, directory, now):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = f"{now:%Y-%m-%dT%H%M%S}"
    (directory / f"{stamp}.json").write_text(json.dumps(rows, indent=2) + "\n")
    lines = [f"# Background sessions stopped — {now:%Y-%m-%d %H:%M}", ""]
    for r in rows:
        lines.append(f"- **{r.get('name') or '(unnamed)'}** — `{r.get('cwd', '')}`")
        lines.append(f"  - resume: `claude attach {r['id']}`")
    md = directory / f"{stamp}.md"
    md.write_text("\n".join(lines) + "\n")
    latest = directory / "latest.md"
    latest.unlink(missing_ok=True)
    latest.symlink_to(md.name)
    return md


def stop(row):
    _, err = run(["claude", "stop", row["id"]])
    if err:
        print(f"  ! {row['id']}: {err}", file=sys.stderr)
        return False
    print(f"  stopped {row['id']}  {row.get('name') or ''}")
    return True


def wait_for_idle(ids, grace):
    """Poll until none of `ids` is busy or `grace` seconds pass; return the still-busy ids."""
    deadline = time.monotonic() + grace
    busy = set(ids)
    while busy and time.monotonic() < deadline:
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        current = {r["id"]: r for r in list_background()}
        # A session that vanished meanwhile settled on its own.
        busy = {i for i in busy if i in current and is_busy(current[i])}
    return busy


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="claude-down",
        description="Stop every Claude Code background session, then the daemon. "
        "Prints the plan unless --go is given.",
    )
    p.add_argument("--go", action="store_true", help="carry out the plan")
    p.add_argument(
        "--grace",
        type=int,
        default=60,
        metavar="SECONDS",
        help="how long busy sessions get to finish their turn (default 60)",
    )
    p.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ID",
        help="stop just this session id (repeatable); leaves the daemon running",
    )
    p.add_argument("--keep-daemon", action="store_true", help="skip `claude daemon stop`")
    p.add_argument(
        "--include-self",
        action="store_true",
        help="also stop the session running this (it goes last, with the daemon)",
    )
    p.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path(
            os.environ.get("CLAUDE_DOWN_DIR") or Path.home() / ".local" / "state" / "claude-down"
        ),
        help="where the snapshot goes (default $CLAUDE_DOWN_DIR or ~/.local/state/claude-down)",
    )
    args = p.parse_args(argv)
    if args.grace < 0:
        p.error("--grace must be >= 0")
    return args


def main(argv=None):
    args = parse_args(argv)
    rows = list_background()
    self_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    me = [r for r in rows if self_sid and r.get("sessionId") == self_sid]
    if args.only:
        wanted = set(args.only)
        rows = [r for r in rows if r["id"] in wanted]
        missing = wanted - {r["id"] for r in rows}
        if missing:
            sys.exit(f"claude-down: no background session {', '.join(sorted(missing))}")
    targets = [r for r in rows if args.include_self or r not in me]
    stop_daemon = not (args.keep_daemon or args.only or (me and not args.include_self))

    held = holders_by_session()
    idle = [r for r in targets if not is_busy(r)]
    busy = [r for r in targets if is_busy(r)]

    print(f"{len(targets)} background session(s) to stop: {len(idle)} idle, {len(busy)} busy")
    for r in idle + busy:
        print(describe(r, held))
    if me and me[0] in rows and not args.include_self:
        print(f"skipping this session ({me[0]['id']}), and the daemon stop that would end it")
    if stop_daemon:
        print("then: claude daemon stop --any")

    if not args.go:
        print("\n(plan only; pass --go to carry it out)")
        return 0

    if rows:
        md = write_snapshot(rows, args.snapshot_dir, datetime.now())
        print(f"\nsnapshot: {md}")

    # This session, when included, goes after everything else: stopping it ends
    # the run, so anything scheduled after it would never happen.
    last = [r for r in targets if r in me]
    failed = 0
    for r in idle:
        if r not in last:
            failed += not stop(r)
    pending = [r for r in busy if r not in last]
    if pending and args.grace:
        print(f"waiting up to {args.grace}s for {len(pending)} busy session(s) to settle")
        wait_for_idle([r["id"] for r in pending], args.grace)
    for r in pending:
        failed += not stop(r)

    if stop_daemon:
        print("stopping the daemon")
        _, err = run(["claude", "daemon", "stop", "--any"])
        if err:
            print(f"  ! claude daemon stop: {err}", file=sys.stderr)
            failed += 1
    for r in last:
        failed += not stop(r)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
