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
from datetime import datetime, timezone
from pathlib import Path

POLL_SECONDS = 5
# `claude daemon stop` waits on its workers; past this the daemon is wedged and
# the operator needs to know rather than watch a silent prompt.
CMD_TIMEOUT_SECONDS = 120
# Transcripts are filed under the directory a session started in, which need not
# be its cwd now, so the lookup globs every project. Override for tests.
PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_DOWN_PROJECTS_DIR") or Path.home() / ".claude" / "projects"
)
# Enough tail to hold several entries; a single tool result can run long.
TAIL_BYTES = 256 * 1024
# The plan's numbering is by quiet time, which reshuffles as sessions work, so a
# --pick resolves against the saved plan only while it is this fresh.
PICK_MAX_AGE_SECONDS = 15 * 60


def run(cmd, timeout=CMD_TIMEOUT_SECONDS):
    """Run a command; return (stdout, error). Exactly one is None."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip() or f"exit {p.returncode}"
    return p.stdout, None


def fetch_background():
    """Background sessions from `claude agents --json`; return (rows, error)."""
    out, err = run(["claude", "agents", "--json"])
    if out is None:
        return None, f"claude agents --json failed: {err}"
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as e:
        return None, f"bad JSON from claude agents: {e}"
    # The listing includes interactive sessions too; `claude stop` only manages
    # background ones, and an interactive one belongs to whoever is at its terminal.
    return [r for r in rows if r.get("kind") == "background"], None


def list_background():
    rows, err = fetch_background()
    if rows is None:
        sys.exit(f"claude-down: {err}")
    return rows


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


def entry_time(line):
    """The aware UTC timestamp of one transcript line, or None if it has none.

    The tail read usually starts mid-line, sometimes mid-character, so a line
    may be undecodable as well as unparseable; both just mean "not an entry".
    """
    try:
        ts = json.loads(line).get("timestamp")
    except (ValueError, AttributeError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    if not isinstance(ts, str):
        return None
    try:
        stamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Mixing naive and aware datetimes fails in the comparisons downstream.
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def last_activity(session_id):
    """When the session last wrote to its transcript, or None if there is none.

    Neither `claude agents --json` nor the daemon's roster records activity, but
    every turn appends timestamped entries to the transcript. The last entry's
    timestamp is used rather than the file's mtime, which a copy or touch moves.
    """
    newest = None
    for path in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        try:
            st = path.stat()
            with open(path, "rb") as f:
                f.seek(max(0, st.st_size - TAIL_BYTES))
                chunk = f.read()
        except OSError:
            # Removed since the glob matched, e.g. a session cleaning up as it ends.
            continue
        stamp = next(
            (s for s in map(entry_time, reversed(chunk.splitlines())) if s), None
        ) or datetime.fromtimestamp(st.st_mtime, timezone.utc)
        newest = stamp if newest is None else max(newest, stamp)
    return newest


def ago(then, now):
    if then is None:
        return "?"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 120:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def describe(row, held, now):
    state = "busy" if is_busy(row) else "idle"
    quiet = ago(row.get("_last"), now)
    n = f"{row['_n']:>3}." if row.get("_n") else "    "
    line = f"{n} {row['id']}  {state:4} {quiet:>4}  {row.get('name') or '(unnamed)'}  [{row.get('cwd', '')}]"
    for cmd in held.get(row.get("sessionId"), []):
        first = cmd.splitlines()[0] if cmd else ""
        line += f"\n                   holds: {first[:100]}"
    return line


def quietest_first(rows):
    """Longest since last activity first; sessions with no transcript go last."""
    oldest = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(rows, key=lambda r: r.get("_last") or oldest)


def write_snapshot(rows, directory, now):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = f"{now:%Y-%m-%dT%H%M%S}"
    records = []
    for r in rows:
        rec = {k: v for k, v in r.items() if not k.startswith("_")}
        if r.get("_last"):
            rec["lastActivity"] = r["_last"].isoformat()
        records.append(rec)
    (directory / f"{stamp}.json").write_text(json.dumps(records, indent=2) + "\n")
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


def plan_path(directory):
    """This caller's saved plan.

    Every caller leaves itself out of its own plan, so two callers number the
    same fleet differently; one shared file would let a plan run from another
    session renumber the rows under a pending --pick. Keyed by Claude session,
    or by terminal from a plain shell.
    """
    key = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not key:
        try:
            key = os.ttyname(0)
        except OSError:
            key = "default"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key.strip("/"))
    return directory / f"plan-{safe}.json"


def save_plan(rows, directory, now):
    """Record the plan's numbering so a later --pick can name rows by it."""
    directory.mkdir(parents=True, exist_ok=True)
    plan = {
        "at": now.isoformat(),
        "rows": [{"n": r["_n"], "id": r["id"], "name": r.get("name")} for r in rows],
    }
    plan_path(directory).write_text(json.dumps(plan, indent=2) + "\n")


def resolve_picks(picks, directory, now):
    """Map plan numbers to session ids via the saved plan; exit on anything doubtful."""
    path = plan_path(directory)
    try:
        plan = json.loads(path.read_text())
        at = datetime.fromisoformat(plan["at"])
        numbered = {r["n"]: r for r in plan["rows"]}
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit(
            "claude-down: no saved plan to pick from here; run `claude-down` first "
            "(plans are kept per session or terminal)"
        )
    age = (now - at).total_seconds()
    if age > PICK_MAX_AGE_SECONDS:
        sys.exit(
            f"claude-down: the saved plan is {ago(at, now)} old and its numbering may have "
            "shifted; run `claude-down` again"
        )
    unknown = [n for n in picks if n not in numbered]
    if unknown:
        sys.exit(
            f"claude-down: the plan has no row {', '.join(map(str, unknown))} "
            f"(it has 1-{len(numbered)})"
        )
    return {numbered[n]["id"]: n for n in picks}


def pick_numbers(values):
    """`--pick 3 --pick 5,7` -> [3, 5, 7]."""
    out = []
    for v in values:
        for part in v.split(","):
            part = part.strip()
            if not part.isdigit() or int(part) < 1:
                raise argparse.ArgumentTypeError(f"not a row number: {part!r}")
            out.append(int(part))
    return list(dict.fromkeys(out))


def stop(row):
    _, err = run(["claude", "stop", row["id"]])
    if err:
        print(f"  ! {row['id']}: {err}", file=sys.stderr)
        return False
    print(f"  stopped {row['id']}  {row.get('name') or ''}")
    return True


def wait_for_idle(ids, grace):
    """Poll until none of `ids` is busy or `grace` seconds pass; return the still-busy ids.

    A failed poll ends the wait rather than the run: by now some sessions are
    stopped, and exiting here would leave the rest, and the daemon, running.
    """
    deadline = time.monotonic() + grace
    busy = set(ids)
    while busy and time.monotonic() < deadline:
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        rows, err = fetch_background()
        if rows is None:
            print(f"  ! {err}; stopping without waiting further", file=sys.stderr)
            break
        current = {r["id"]: r for r in rows}
        # A session that vanished meanwhile settled on its own.
        busy = {i for i in busy if i in current and is_busy(current[i])}
    return busy


def hand_off(commands):
    """Run `commands` in a detached shell that outlives this session.

    Stopping this session, or the daemon, kills the process running claude-down
    along with anything still attached to it, so the last steps have to belong
    to a process in its own session.
    """
    subprocess.Popen(
        ["sh", "-c", "; ".join(commands)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def refresh(targets, me, include_new):
    """Re-read statuses just before stopping.

    The plan's listing is seconds old by then, and a session idle in it may have
    started a turn (a /loop tick, a monitor wakeup) that a no-grace stop would
    cut off. Sessions that have exited drop out; with include_new, sessions that
    appeared since the plan join.
    """
    rows, err = fetch_background()
    if rows is None:
        print(f"  ! {err}; using the plan's listing", file=sys.stderr)
        return targets
    known = {r["id"] for r in targets}
    me_ids = {r["id"] for r in me}
    fresh = [r for r in rows if r["id"] in known]
    if include_new:
        fresh += [r for r in rows if r["id"] not in known and r["id"] not in me_ids]
    return fresh


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
    p.add_argument(
        "--pick",
        action="append",
        default=[],
        metavar="N",
        help="stop just the row numbered N in the last plan (repeatable, or N,M); "
        "leaves the daemon running",
    )
    p.add_argument("--keep-daemon", action="store_true", help="skip `claude daemon stop`")
    p.add_argument(
        "--include-self",
        action="store_true",
        help="also stop the session running this, last, from a detached shell",
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
    try:
        args.pick = pick_numbers(args.pick)
    except argparse.ArgumentTypeError as e:
        p.error(str(e))
    return args


def main(argv=None):
    args = parse_args(argv)
    picked = {}
    if args.pick:
        picked = resolve_picks(args.pick, args.snapshot_dir, datetime.now(timezone.utc))
        args.only = [*args.only, *picked]
    rows = list_background()
    self_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    me = [r for r in rows if self_sid and r.get("sessionId") == self_sid]
    if args.only:
        wanted = set(args.only)
        rows = [r for r in rows if r["id"] in wanted]
        missing = wanted - {r["id"] for r in rows}
        if missing:
            gone = [f"{i} (row {picked[i]})" if i in picked else i for i in sorted(missing)]
            # Picked ids came from a real listing, so absence means it ended; an
            # --only id may just be mistyped.
            hint = "; it may have stopped already" if missing <= picked.keys() else ""
            sys.exit(f"claude-down: no background session {', '.join(gone)}{hint}")
        if not args.include_self:
            mine = [r for r in rows if r in me]
            if mine:
                sys.exit(
                    f"claude-down: {mine[0]['id']} is the session running this; "
                    "add --include-self to stop it"
                )
    targets = [r for r in rows if args.include_self or r not in me]
    stop_daemon = not (args.keep_daemon or args.only or (me and not args.include_self))

    held = holders_by_session()
    for r in targets:
        r["_last"] = last_activity(r.get("sessionId", ""))
    idle = quietest_first([r for r in targets if not is_busy(r)])
    busy = quietest_first([r for r in targets if is_busy(r)])
    now = datetime.now(timezone.utc)
    if args.only:
        # Picked rows keep the number they were picked by; --only rows go unnumbered.
        for r in targets:
            r["_n"] = picked.get(r["id"])
    else:
        for n, r in enumerate(idle + busy, 1):
            r["_n"] = n

    print(f"{len(targets)} background session(s) to stop: {len(idle)} idle, {len(busy)} busy")
    print(
        "  (the time after each state is how long since the session last wrote to its transcript)"
    )
    for r in idle + busy:
        print(describe(r, held, now))
    if me and me[0] in rows and not args.include_self:
        print(f"skipping this session ({me[0]['id']}), and the daemon stop that would end it")
    if stop_daemon:
        print("then: claude daemon stop --any")

    if not args.go:
        if not args.only:
            save_plan(idle + busy, args.snapshot_dir, now)
            print("\n(plan only; pass --go to carry it out, or --go --pick N to stop row N)")
        else:
            print("\n(plan only; pass --go to carry it out)")
        return 0

    if rows:
        md = write_snapshot(rows, args.snapshot_dir, datetime.now())
        print(f"\nsnapshot: {md}")

    others = refresh([r for r in targets if r not in me], me, include_new=not args.only)
    failed = 0
    for r in others:
        if not is_busy(r):
            failed += not stop(r)
    pending = [r for r in others if is_busy(r)]
    if pending and args.grace:
        print(f"waiting up to {args.grace}s for {len(pending)} busy session(s) to settle")
        wait_for_idle([r["id"] for r in pending], args.grace)
    for r in pending:
        failed += not stop(r)

    final = [f"claude stop {r['id']}" for r in targets if r in me]
    if stop_daemon:
        final.append("claude daemon stop --any")
    if any(r in me for r in targets):
        # Either step ends this session, so neither can run in it.
        hand_off(final)
        print(f"handed off to a detached shell: {'; '.join(final)}")
    elif stop_daemon:
        print("stopping the daemon")
        _, err = run(["claude", "daemon", "stop", "--any"])
        if err:
            print(f"  ! claude daemon stop: {err}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
