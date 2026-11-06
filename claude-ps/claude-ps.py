#!/usr/bin/env -S uv run --quiet --with rich python3
"""List running Claude processes with TTY, CWD, and resource usage."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

# The CWD column trims these prefixes for compactness. The shipped default is the
# generic ~/projects/; override with the PROJECT_DIRS env var (colon-separated)
# or one or more --project-dir flags. Personal hubs (e.g. ~/projects-new/) belong
# in an override, not the default.
DEFAULT_PROJECT_DIRS = ("~/projects/",)

# Ceiling for the `--status` call to `claude-ls pending` (a full project scan).
# The glance is meant to run under `watch`, so a stuck scan degrades rather than
# wedging the live view.
#
# 5s used to sit right on top of the real cost: the scan measured 3.4-4.1s against
# a live store, so roughly one run in seven tripped the ceiling and reported the
# whole fleet as not-waiting. A timeout is not evidence of an idle fleet, so the
# headroom matters more than the latency here — and degradation is now visible
# (waiting=None) instead of masquerading as a negative result.
PENDING_TIMEOUT_SECONDS = 15

# Claude Code writes one record per running session here, so the process<->session
# join needs no hook of our own. Override for tests.
SESSIONS_DIR = Path(
    os.environ.get("CLAUDE_PS_SESSIONS_DIR") or Path.home() / ".claude" / "sessions"
)

# `ps -o lstart=` layout, which the registry's `procStart` also uses — see
# registry_session_for for the catch.
LSTART_FMT = "%a %b %d %H:%M:%S %Y"


def parse_args(argv=None):
    """Parse the command line.

    Hand-rolled `"--flag" in sys.argv` membership tests used to stand in for this,
    which meant `--help` printed the process table and a misspelled flag was
    silently ignored with a zero exit -- so `--jsonn` in a script looked like it
    had worked and quietly returned human output instead of JSON.
    """
    parser = argparse.ArgumentParser(
        prog="claude-ps",
        description=(
            "List running Claude processes with TTY, CWD, tmux session, and "
            "resource usage -- a `ps` tuned for Claude Code."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="annotate which sessions are waiting on input (calls `claude-ls pending`)",
    )
    parser.add_argument("--json", action="store_true", help="emit the listing as JSON")
    parser.add_argument(
        "--sort",
        choices=("age", "mem", "cpu"),
        default="age",
        help="row order: age (oldest first, the default), mem or cpu (largest first)",
    )
    parser.add_argument(
        "--shells",
        action="store_true",
        help="list the shells sessions are holding open (monitors, sleeps, servers)",
    )
    parser.add_argument(
        "--color",
        action="store_true",
        help="force color even when stdout is not a terminal",
    )
    parser.add_argument(
        "--project-dir",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "prefix to trim from the CWD column; repeatable. "
            "Overrides the PROJECT_DIRS env var, which overrides the default."
        ),
    )
    return parser.parse_args(argv)


def project_dir_prefixes(cli_dirs=()):
    """Resolve CWD-trim prefixes: --project-dir flags > PROJECT_DIRS env > default.

    Prefixes are normalized to ~-relative form with a trailing slash so they match
    the tilde-form CWDs in the table (shell-expanded flag values are re-tilded).
    """
    dirs = list(cli_dirs)
    if not dirs:
        dirs = [p for p in (os.getenv("PROJECT_DIRS") or "").split(":") if p]
    if not dirs:
        dirs = list(DEFAULT_PROJECT_DIRS)

    home = os.path.expanduser("~")
    out = []
    for p in dirs:
        p = os.path.expanduser(p)
        if p.startswith(home):
            p = "~" + p[len(home) :]
        if not p.endswith("/"):
            p += "/"
        out.append(p)
    return tuple(out)


# A Bash tool call runs as a direct child of the session's `claude` process,
# wrapped by the harness: source a shell snapshot, then `eval` the real command.
# Both halves earn their place -- the snapshot path is what identifies the shell
# as Claude's rather than a coincidental zsh, and the eval payload is the only
# place the command survives verbatim.
SHELL_SNAPSHOT_MARKER = "shell-snapshots/snapshot-"
SHELL_EVAL = re.compile(r"&& eval '(.*)' < /dev/null", re.DOTALL)

# EVERY in-flight Bash call -- foreground as much as backgrounded -- streams to
# <root>/<project>/<session-id>/tasks/<task-id>.output and holds it open for as
# long as it runs. So the file names the shell's session and task exactly, but
# says nothing about whether the call was backgrounded. Only the transcript does.
TASK_OUTPUT = re.compile(r"/([^/]+)/([0-9a-f-]{36})/tasks/([^/]+)\.output$")

# How the harness announces a backgrounded call, in the tool result: once for an
# explicit run_in_background, once for a foreground call it moved off after a
# timeout. The second carries nothing in the tool *input*, so matching the result
# text is the only way to catch both.
BACKGROUNDED = re.compile(r"background with ID: (\w+)|moved to the background \(ID: (\w+)\)")

TRANSCRIPTS = Path.home() / ".claude" / "projects"

# Descending past these finds what a shell is really waiting on.
SHELL_NAMES = {"sh", "bash", "zsh", "-zsh", "-bash", "dash"}

# A held command is often a whole shell program. The table folds it rather than
# truncating -- seeing the loop condition is the point -- so it needs a ceiling,
# or one row becomes a screenful. --json carries the command untrimmed.
COMMAND_DISPLAY_CHARS = 220


def shell_command(cmdline):
    """The command a Claude-owned shell is running, or None if it isn't one.

    Returned verbatim. Collapsing its whitespace here would rewrite the command:
    `printf "%s\\n" "a  b"` is not the same program as `printf "%s\\n" "a b"`,
    and --json promises the command as issued. The table does its own flattening.
    """
    if SHELL_SNAPSHOT_MARKER not in cmdline:
        return None
    match = SHELL_EVAL.search(cmdline)
    if not match:
        # A Claude shell whose wrapper this doesn't recognize is still a held
        # shell. Report the raw command line rather than dropping the row: the
        # wrapper is the harness's to change, and a missed row reads as "nothing
        # running", which is the one answer that must not be wrong.
        return cmdline
    return match.group(1)


def all_processes():
    """{pid: (ppid, etime, command)} for every process, commands untruncated.

    Separate from get_claude_pids()'s scan because this one needs the whole
    table: the shells are found as children, so every process is a candidate
    parent until proven otherwise.
    """
    result = subprocess.run(
        ["ps", "-ww", "-eo", "pid=,ppid=,etime=,command="], capture_output=True, text=True
    )
    procs = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            procs[parts[0]] = (parts[1], parts[2], parts[3])
    return procs


_MEM_UNITS = {"B": 1 / 1024, "K": 1, "M": 1024, "G": 1024 * 1024}


def parse_top_mem(output):
    """{pid: KB} from `top -l 1 -stats pid,mem` output.

    top's MEM is the process's footprint, compressed and swapped pages included;
    ps's RSS counts only what is resident, which on a machine under memory
    pressure is a fraction of what a session holds. Values look like `15M`,
    `4304K` or `1024M+` (the sign is top's trend marker).
    """
    mem = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        value = parts[1].rstrip("+-")
        unit = _MEM_UNITS.get(value[-1:])
        try:
            mem[parts[0]] = int(float(value[:-1]) * unit) if unit else int(value) // 1024
        except ValueError:
            continue
    return mem


def memory_by_pid():
    """{pid: KB} for every process: top's footprint on macOS, else ps's RSS."""
    if sys.platform == "darwin":
        result = subprocess.run(
            ["top", "-l", "1", "-stats", "pid,mem"], capture_output=True, text=True
        )
        mem = parse_top_mem(result.stdout) if result.returncode == 0 else {}
        if mem:
            return mem
    result = subprocess.run(["ps", "-eo", "pid=,rss="], capture_output=True, text=True)
    rows = (line.split() for line in result.stdout.splitlines())
    return {r[0]: int(r[1]) for r in rows if len(r) == 2 and r[1].isdigit()}


def session_memory(pid, procs, mem, stop=()):
    """KB held by pid and its descendants: language servers, MCP servers and
    tool shells a session starts are its cost. A descendant in `stop` (another
    listed claude) is counted on its own row instead."""
    children = {}
    for child, (ppid, _etime, _cmd) in procs.items():
        children.setdefault(ppid, []).append(child)
    total, todo = 0, [pid]
    while todo:
        p = todo.pop()
        total += mem.get(p, 0)
        todo.extend(c for c in children.get(p, ()) if c not in stop)
    return total


def is_orphan(p):
    """Parent gone, adopted by launchd/init. The Claude daemon runs that way by
    design, so it is not one; a background-session host whose daemon exited is."""
    return p["ppid"] == "1" and " daemon run" not in p["args"]


def format_mem(kb):
    return f"{kb / 1048576:.1f}G" if kb >= 1048576 else f"{kb // 1024}M"


def parse_task_owners(lsof_output):
    """{pid: (project_slug, session_id, task_id)} from `lsof -F pn` output."""
    owners = {}
    pid = None
    for line in lsof_output.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            match = TASK_OUTPUT.search(line)
            if match:
                owners[pid] = (match.group(1), match.group(2), match.group(3))
    return owners


def backgrounded_task_ids(project_slug, session_id, root=None):
    """Task ids this session's transcript records as having been backgrounded.

    The process tree cannot answer this: a foreground call holds the same kind of
    task file as a backgrounded one, and they differ only in what the harness
    wrote in the tool result. The task path names the transcript that knows --
    the slug is the project directory, the uuid is the file.

    Returns None when the transcript can't be read, which callers must treat as
    "no opinion" rather than as "nothing was backgrounded".
    """
    path = Path(root or TRANSCRIPTS) / project_slug / f"{session_id}.jsonl"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return {m.group(1) or m.group(2) for m in BACKGROUNDED.finditer(text)}


def blocking_leaves(procs, children, pid):
    """What the shell is actually waiting on -- `sleep`, `gh`, a server.

    Not merely its immediate children: a payload that forks a subshell (a
    `( ... )` group, a pipeline) puts another shell there, and reporting `zsh`
    names the scaffolding rather than the blocker. Descend until the name is not
    a shell.
    """
    leaves, stack, seen = [], list(children.get(pid, [])), set()
    while stack:
        kid = stack.pop(0)
        if kid in seen or kid not in procs:
            continue
        seen.add(kid)
        name = procs[kid][2].split()[0].rsplit("/", 1)[-1]
        grandchildren = children.get(kid, [])
        if name in SHELL_NAMES and grandchildren:
            stack.extend(grandchildren)
        else:
            leaves.append(name)
    return leaves


def task_owners_for(pids):
    """Which of these shells are still writing to a background task's output file.

    The .output file outlives its task, so the file existing proves nothing. A
    live writer is what separates a backgrounded call still running from one
    that finished hours ago.

    Batched -p over the shell pids rather than a pass over every .output file on
    disk: there are a couple of shells and hundreds of files. Returns {} when
    lsof is unavailable, which callers read as "no opinion" -- every shell then
    reports as foreground, which is the safe direction to be wrong in.
    """
    if not pids:
        return {}
    try:
        result = subprocess.run(
            ["lsof", "-F", "pn", "-p", ",".join(pids)], capture_output=True, text=True
        )
    except (FileNotFoundError, OSError):
        return {}
    # Not gated on returncode, for the same reason get_lsof_facts isn't: lsof
    # exits non-zero when any requested pid has gone away, which is routine.
    return parse_task_owners(result.stdout)


def own_ancestry(procs, pid=None):
    """Our own pid and every ancestor of it.

    `claude-ps --shells` runs inside exactly the kind of shell it reports, so
    without this it always finds itself -- and the row is noise of the worst
    kind, since it is the one shell guaranteed to be harmless.
    """
    seen = set()
    pid = str(pid if pid is not None else os.getpid())
    while pid in procs and pid not in seen:
        seen.add(pid)
        pid = procs[pid][0]
    return seen


def collect_shells(procs, claude_pids, ancestry=()):
    """One row per shell a Claude process is holding open.

    Identity only -- which shell, under which session's process, running what,
    and blocked on what. Whether the call was backgrounded is decided separately,
    from the transcript: nothing in the process tree distinguishes the two.
    """
    children = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)

    rows = []
    for cpid in claude_pids:
        for kid in sorted(children.get(cpid, []), key=int):
            if kid not in procs or kid in ancestry:
                continue
            command = shell_command(procs[kid][2])
            if command is None:
                continue
            rows.append(
                {
                    "claude_pid": cpid,
                    "pid": kid,
                    "age": procs[kid][1],
                    "blocking": blocking_leaves(procs, children, kid),
                    "command": command,
                }
            )
    return rows


def classify_shells(rows, owners, backgrounded):
    """Label each shell background / foreground / unknown, in place.

    `backgrounded` maps (project_slug, session_id) to the task ids that session's
    transcript announced, or to None when the transcript could not be read.

    Unknown has to stay a distinct answer. Calling an unreadable shell
    "foreground" is what would turn a healthy long-running monitor into a bogus
    abandoned-shell warning, and that warning is the one thing this view says
    with any force.
    """
    for row in rows:
        slug, session_id, task_id = owners.get(row["pid"], (None, None, None))
        row["session_id"] = session_id
        row["task_id"] = task_id
        ids = backgrounded.get((slug, session_id)) if task_id else None
        if task_id is None or ids is None:
            row["kind"] = "unknown"
        elif task_id in ids:
            row["kind"] = "background"
        else:
            row["kind"] = "foreground"
    return rows


def held_for_an_hour(age):
    """True when a `ps` ELAPSED string is an hour or more: [[dd-]hh:]mm:ss."""
    return age.count(":") >= 2 or "-" in age


def get_claude_pids():
    """Candidate pids, from two enumerations unioned.

    pgrep alone silently omits the caller's own ancestor: run from inside a
    session, `pgrep -f claude` does not return that session's process even when
    its args are literally `claude --model opus`. The session you are sitting in
    is the one you cannot see, and nothing about the output says so.

    A `ps` scan for a `claude` basename finds it, but is not a replacement —
    pgrep still catches processes this misses. So take the union and let the
    version-handle confirmation in get_lsof_facts() supply precision: widening
    the candidate pass costs nothing once every candidate has to prove itself.
    """
    pids = set()
    result = subprocess.run(["pgrep", "-f", "claude"], capture_output=True, text=True)
    if result.returncode == 0:
        pids.update(result.stdout.split())

    scan = subprocess.run(["ps", "-Ao", "pid=,comm="], capture_output=True, text=True)
    if scan.returncode == 0:
        for line in scan.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip().rsplit("/", 1)[-1] == "claude":
                pids.add(parts[0])

    # Numeric sort so the listing order stays stable rather than lexicographic.
    return sorted(pids, key=lambda p: int(p) if p.isdigit() else 0)


def normalize_tty(tty):
    # Common key for matching `ps -o tty=` against tmux's `#{pane_tty}` across
    # platforms: macOS ps reports `ttys001` vs tmux's `/dev/ttys001`; Linux ps
    # reports `pts/0` vs tmux's `/dev/pts/0`. Dropping a leading `/dev/` from
    # either form lines the two sides up on both OSes.
    return tty[len("/dev/") :] if tty.startswith("/dev/") else tty


def get_proc_info(pid):
    result = subprocess.run(
        ["ps", "-p", pid, "-o", "pid=,ppid=,tty=,lstart=,%cpu=,cputime=,args="],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line:
        return None
    parts = line.split()
    # fields: pid, ppid, tty, lstart (5 tokens), %cpu, cputime, args...
    ppid = parts[1]
    tty = parts[2]
    lstart_str = " ".join(parts[3:8])
    cpu_pct = parts[8]
    cputime = parts[9]
    args = " ".join(parts[10:])
    try:
        started = datetime.strptime(lstart_str, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        started = None
    return {
        "pid": pid,
        "ppid": ppid,
        "tty": normalize_tty(tty),
        "started": started,
        "cpu_pct": cpu_pct,
        "cputime": cputime,
        "args": args,
    }


def tilde_trim(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def extract_claude_version(path):
    # A running session's executable lives at ~/.local/share/claude/versions/<v>,
    # so the path names the version it is running. Returns None for any other file.
    marker = "/.local/share/claude/versions/"
    if marker not in path:
        return None
    return path.split(marker, 1)[1].split("/", 1)[0] or None


def version_key(version):
    """Order a version string numerically: 2.1.99 sorts before 2.1.100.

    String order inverts at every 9→10 digit boundary, and patch numbers are
    already three digits, so the fleet spans one routinely — a long-lived session
    across that boundary is precisely what the stale-build footer exists to catch,
    and lexicographic order would name the older build as the newest.

    Non-numeric parts (a `-rc1` suffix, say) sort after numeric ones rather than
    raising, since the footer is cosmetic and an unparseable version should not
    take the listing down.
    """
    return [(0, int(part)) if part.isdigit() else (1, part) for part in version.split(".")]


def get_lsof_facts(pids):
    """One batched lsof for every candidate: {pid: {"cwd": str, "version": str|None}}.

    Batched on purpose. This was one lsof spawn per pid for the CWD column alone,
    and adding the version probe separately would have doubled it; `lsof -p` takes
    a comma-separated list, so both facts come off a single call. Measured on 24
    processes: 0.22s batched against 1.68s for the per-pid loop.

    Uses -F (one field per line, tagged) rather than the human table, whose NAME
    column is whitespace-separated from the fields before it and cannot be split
    safely when a path contains a space.

    Returns {} when lsof is unavailable, which callers must treat as "no opinion"
    rather than as an empty result.
    """
    if not pids:
        return {}
    try:
        result = subprocess.run(
            ["lsof", "-F", "pfn", "-p", ",".join(pids)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {}
    # Not gated on returncode: lsof exits non-zero when any requested pid has gone
    # away, which is routine here and does not invalidate the rest of the output.
    facts = {}
    pid = fd = None
    for line in result.stdout.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid, fd = value, None
            facts.setdefault(pid, {"cwd": "", "version": None})
        elif tag == "f":
            fd = value
        elif tag == "n" and pid is not None:
            if fd == "cwd" and not facts[pid]["cwd"]:
                facts[pid]["cwd"] = tilde_trim(value)
            elif fd == "txt" and facts[pid]["version"] is None:
                facts[pid]["version"] = extract_claude_version(value)
    return facts


def format_age(started):
    if not started:
        return "?"
    delta = datetime.now() - started
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return f"{days}d {hours}h"
    minutes = delta.seconds // 60
    if hours > 0:
        return f"{hours}h {minutes % 60}m"
    return f"{minutes}m"


def shorten_cwd(cwd, prefixes):
    for prefix in prefixes:
        if cwd.startswith(prefix):
            return cwd[len(prefix) :]
    return cwd


def get_tmux_tty_map():
    # tmux is optional: it only enriches the Session column. When it isn't
    # installed the binary is missing and subprocess raises FileNotFoundError —
    # degrade to no mapping rather than crashing the whole listing.
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_tty} #{session_name}"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {}
    if result.returncode != 0:
        return {}
    mapping = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            tty = normalize_tty(parts[0])
            mapping[tty] = parts[1]
    return mapping


def fetch_pending_rows():
    # Ask claude-ls which sessions are waiting on user input. claude-ls is optional
    # and looked up on PATH, exactly like tmux: if it's absent, errors, or (since
    # pending does a full project scan and --status runs under `watch`) exceeds the
    # timeout, degrade rather than failing or wedging the listing.
    #
    # Returns the rows, or None when the answer could not be obtained at all. The
    # distinction is the point: an empty list means "scanned, nothing waiting",
    # while None means "don't know". Collapsing both rendered a failed scan as a
    # confident fleet-wide "not waiting" — a silent false negative, and the worse
    # of the two errors under `watch`, where the flag simply blinks out.
    #
    # Fetch is split from the two derivations below so one scan feeds both the
    # session join and the cwd fallback.
    try:
        result = subprocess.run(
            ["claude-ls", "pending", "--json"],
            capture_output=True,
            text=True,
            timeout=PENDING_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        rows = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    # Valid JSON isn't necessarily the expected list-of-objects — a bare object or
    # scalar would make the loop below iterate keys/chars and crash on row.get().
    # Degrade like every other unexpected-output case rather than wedge --status.
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def waiting_by_cwd(rows):
    # The fallback join: {cwd: minutes}, for processes the registry cannot place.
    # Lossy by construction — several waiting sessions can share a directory, and
    # this keeps only the freshest — which is exactly why the session join below
    # is preferred when it is available.
    if rows is None:
        return None
    waiting = {}
    for row in rows:
        cwd = row.get("cwd")
        if not cwd:
            continue
        minutes = row.get("minutes_waiting")
        if cwd not in waiting or (minutes is not None and minutes < waiting[cwd]):
            waiting[cwd] = minutes if minutes is not None else waiting.get(cwd, 0)
    return waiting


def waiting_by_session(rows):
    # The exact join: {session_id: minutes}. Nothing is collapsed here, so a row
    # carries the wait of that session rather than a directory-wide minimum.
    if rows is None:
        return None
    waiting = {}
    for row in rows:
        sid = row.get("session_id")
        if sid:
            waiting[sid] = row.get("minutes_waiting")
    return waiting


def load_session_registry():
    """Claude Code's per-process session records: {pid: {session_id, proc_start}}.

    One JSON file per running session in SESSIONS_DIR, written by Claude Code and
    living exactly as long as the process. This used to be an opt-in hook of our
    own; the CLI writes the same join itself as of 2.1.2xx, so the exact join is
    now the default rather than something the user installs.

    Keyed by pid for a per-process lookup. A stale file can outlive its session
    (a kill, a reboot) and the pid it names can be reused, so the pid alone is
    not a join — registry_session_for confirms it against the start time.

    The directory is another program's output: absent, unreadable and malformed
    files are all normal, and each is skipped rather than raised.
    """
    try:
        paths = sorted(SESSIONS_DIR.glob("*.json"))
    except OSError:
        return {}
    by_pid = {}
    for path in paths:
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        pid, sid = rec.get("pid"), rec.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or not sid:
            continue
        by_pid[str(pid)] = {"session_id": sid, "proc_start": rec.get("procStart")}
    return by_pid


def registry_session_for(proc, registry):
    """The session id this process is running, or None if it can't be confirmed.

    A pid match alone is not enough: pids are recycled, and a recycled one would
    silently attribute another session's state to this process. The record stores
    the owning process's start time, so requiring it to match the start time we
    observed turns a plausible match into a confirmed one.

    The catch, and the reason this is not a string compare: both sides use
    lstart's layout, but Claude Code writes `procStart` in UTC while `ps -o
    lstart=` prints local time, so one process reads "Wed Sep  2 21:16:00 2026"
    in the file and "Wed Sep  2 14:16:00 2026" from ps on a PDT machine. Comparing
    the text rejects every live session — and the failure is invisible, because
    losing the exact join looks exactly like the old un-installed hook: every row
    silently falls back to the cwd match. Compare instants instead.
    """
    entry = registry.get(str(proc["pid"]))
    if not entry:
        return None
    recorded, observed = entry.get("proc_start"), proc.get("started")
    if recorded and observed:
        try:
            rec = datetime.strptime(" ".join(recorded.split()), LSTART_FMT)
        except (ValueError, TypeError):
            return None  # unparseable record: unconfirmed, so unusable
        rec = rec.replace(tzinfo=timezone.utc)
        # `started` is naive, parsed from local ps output; astimezone() reads it as
        # local. The tolerance covers the two sources formatting the same instant a
        # second apart, not clock skew — they are the same clock.
        #
        # astimezone() resolves an ambiguous local time with fold=0, so a process
        # started in the repeated hour at a DST fall-back reads an hour off and
        # drops to the cwd join for that hour. Fails safe, and claude-ls's
        # _proc_start_matches has the same behaviour; left alone deliberately.
        if abs((rec - observed.astimezone()).total_seconds()) > 2:
            return None
    return entry.get("session_id")


def annotate_waiting(procs, by_cwd, by_session=None, registry=None):
    # Set `waiting`, `waiting_minutes` and `waiting_source` in place, preferring the
    # exact join and falling back to the directory one per process. The two still
    # coexist: a session started outside tmux, or under a CLI too old to write the
    # registry, has no record and is placed by directory like it always was.
    #
    # `waiting_source` is reported rather than inferred: "session" is that process's
    # own state, while "cwd" is a claim about its directory that happens to be
    # attached to a row. Reading them as the same thing is the confusion this whole
    # column has caused.
    #
    # by_cwd is None when the scan failed: propagate waiting=None ("don't know")
    # rather than inventing a negative answer nothing established.
    registry = registry or {}
    for p in procs:
        if by_cwd is None:
            p["waiting"] = None
            p["waiting_minutes"] = None
            p["waiting_source"] = None
            continue

        sid = registry_session_for(p, registry) if by_session is not None else None
        if sid is not None:
            p["waiting"] = sid in by_session
            p["waiting_minutes"] = by_session.get(sid)
            p["waiting_source"] = "session"
            continue

        # get_lsof_facts tilde-trims paths under $HOME, so expand back to the
        # absolute path claude-ls records in the transcript before matching.
        key = os.path.expanduser(p["cwd"])
        p["waiting"] = key in by_cwd
        p["waiting_minutes"] = by_cwd.get(key)
        p["waiting_source"] = "cwd"


def classify_command(args):
    if args.startswith("claude"):
        return args
    if "remind.py" in args:
        return "remind daemon"
    if "tmux" in args:
        parts = args.split()
        for i, p in enumerate(parts):
            if p == "-t" and i + 1 < len(parts):
                return f"tmux ({parts[i + 1]})"
        return "tmux"
    if "shell-snapshots" in args:
        return "zsh (shell-snapshot)"
    return args[:40] + "…" if len(args) > 40 else args


def collect_procs(pids, facts, confirm):
    """Build the process rows, optionally dropping candidates lsof did not confirm.

    `confirm=False` keeps every candidate and leaves `version` unset — the shape the
    listing degrades to whenever lsof has no usable opinion.
    """
    procs = []
    for pid in pids:
        fact = facts.get(pid)
        if confirm and (fact is None or fact["version"] is None):
            continue
        info = get_proc_info(pid)
        if info:
            info["cwd"] = fact["cwd"] if fact else ""
            info["version"] = fact["version"] if fact else None
            procs.append(info)
    return procs


def display_command(command, width=COMMAND_DISPLAY_CHARS):
    """A held command flattened to one bounded line for the table.

    Truncated by slicing rather than by textwrap.shorten, which drops the whole
    string when it holds no spaces to break on -- and a long unbroken command is
    exactly the case where seeing the front of it matters.
    """
    flat = " ".join(command.split())
    return flat if len(flat) <= width else flat[: width - 1] + "\u2026"


def build_process_table(procs, my_pid, prefixes, status, width=None):
    """The default listing.

    Session, CWD and Command carry process text, so they are `Text` rather than
    markup strings for the same reason `build_shell_table`'s cells are: rich
    parses markup in a str, and a `claude` invocation holding `[/red]` -- which
    `classify_command` returns verbatim -- raises MarkupError and takes the
    whole listing down rather than mangling one cell. The self-marker, age, CPU
    and status cells stay strings, because that markup is ours.
    """
    # Laid out here rather than by rich: a one-line text column measures at its
    # full length, so one long command squeezed PID, Age, CPU and Mem to "…"
    # and, wrapping instead, stacked a command one word per line. Short columns
    # get exact widths, CWD and Command split what is left and are cut with an
    # ellipsis, and below 100 columns PPID is dropped (--json keeps it).
    narrow = width is not None and width < 100
    session_w = min(12 if narrow else 20, max([len(p["session"]) for p in procs] + [7]))
    short = [("PID", 5), *([] if narrow else [("PPID", 5)]), ("Age", 7), ("CPU", 6), ("Mem", 5)]
    status_w = (11 if narrow else 15) if status else 0
    cwd_w = cmd_w = None
    if width is not None:
        ncols = len(short) + 3 + (1 if status else 0)
        avail = width - sum(w for _, w in short) - session_w - status_w - (3 * ncols + 1)
        cwd_w = max(6, min(40, avail * 2 // 5))
        cmd_w = max(6, min(60, avail - cwd_w))

    table = Table(title="Claude Processes", show_lines=False)
    for name, w in short:
        style = "dim" if name in ("PID", "PPID") else None
        justify = "left" if style else "right"
        table.add_column(name, style=style, justify=justify, no_wrap=True, width=w)
    table.add_column("Session", style="magenta", no_wrap=True, width=session_w)
    table.add_column("CWD", style="cyan", no_wrap=True, width=cwd_w)
    table.add_column("Command", no_wrap=True, width=cmd_w)
    if status:
        table.add_column("Status", no_wrap=True, width=status_w)

    def fit(text, n):
        if n is not None:
            text.truncate(n, overflow="ellipsis")
        return text

    def fit_left(path, n):
        # Paths share their front (~/projects-new/...), so keep the end.
        return Text(path if n is None or len(path) <= n else "\u2026" + path[-(n - 1) :])

    for p in procs:
        is_me = p["pid"] == my_pid
        age = format_age(p["started"])
        is_old = p["started"] and (datetime.now() - p["started"]).days >= 3
        is_hot = float(p["cpu_pct"]) > 20

        age_str = f"[yellow]{age}[/yellow]" if is_old else age
        cpu_str = f"[red]{p['cpu_pct']}%[/red]" if is_hot else f"{p['cpu_pct']}%"
        orphan = is_orphan(p)
        ppid_str = f"[yellow]{p['ppid']}[/yellow]" if orphan else p["ppid"]
        # The PID carries the mark too: below 100 columns PPID is not shown.
        pid_str = f"[yellow]{p['pid']}[/yellow]" if orphan else p["pid"]
        mem_kb = p.get("mem_kb")
        mem_str = "?" if mem_kb is None else format_mem(mem_kb)
        if mem_kb is not None and mem_kb >= 1048576:
            mem_str = f"[yellow]{mem_str}[/yellow]"

        command = fit(Text(classify_command(p["args"])), cmd_w - 2 if is_me and cmd_w else cmd_w)
        if is_me:
            # Appended as styled text rather than concatenated markup, so the
            # command it follows stays escaped.
            command.append(" ◀", style="green")

        row = [
            pid_str,
            *([] if narrow else [ppid_str]),
            age_str,
            cpu_str,
            mem_str,
            fit(Text(p["session"]), session_w),
            fit_left(shorten_cwd(p["cwd"], prefixes), cwd_w),
            command,
        ]
        if status:
            mins = p["waiting_minutes"]
            if p["waiting"] is None:
                # Not "" — a blank cell is how a known-idle session reads, and the
                # scan never established that.
                row.append("[dim]?[/dim]")
            elif p["waiting"]:
                label = f"⏳ waiting {mins}m" if mins is not None else "⏳ waiting"
                # A cwd-sourced hit is a claim about the directory, not this
                # process, and its minutes are the directory's freshest wait.
                # Label it as such instead of dressing it up as a per-session fact.
                if p["waiting_source"] == "cwd":
                    label = f"⏳ dir {mins}m" if mins is not None else "⏳ dir"
                row.append(f"[yellow]{label}[/yellow]")
            else:
                row.append("")
        table.add_row(*row)

    return table


def build_shell_table(rows):
    """The `--shells` table.

    Every cell carrying process text is a `Text`, not a markup string. A held
    command is arbitrary shell input, and rich parses markup in a str: a command
    containing `[/red]` raises MarkupError and takes the entire listing down
    rather than mangling one cell. Only `Kind` stays a str, because its markup
    is ours.
    """
    table = Table(title="Shells Held by Claude", show_lines=False)
    table.add_column("Session", style="magenta", no_wrap=True)
    table.add_column("PID", style="dim", no_wrap=True, width=5)
    table.add_column("Age", justify="right", no_wrap=True, width=5)
    table.add_column("Kind", no_wrap=True, min_width=10)
    table.add_column("Blocking", style="cyan", no_wrap=True, min_width=8)
    # The only wrappable column, and the one with slack to give: rich shrinks a
    # wrappable column before it squeezes the fixed ones, and with every column
    # no-wrap it squeezes them all to nothing on an 80-column terminal.
    table.add_column("Command", ratio=2, overflow="fold")

    labels = {
        "background": "task {}",
        "foreground": "[yellow]foreground[/yellow]",
        "unknown": "[dim]unknown[/dim]",
    }
    for row in rows:
        table.add_row(
            Text(row["session"] or row["claude_pid"]),
            row["pid"],
            row["age"],
            labels[row["kind"]].format(row["task_id"] or ""),
            Text(", ".join(row["blocking"]) or "-"),
            Text(display_command(row["command"])),
        )
    return table


def render_shells(claude_procs, as_json, force_color):
    """Print the shells the fleet is holding open."""
    procs = all_processes()
    by_pid = {c["pid"]: c for c in claude_procs}
    shells = collect_shells(procs, list(by_pid), own_ancestry(procs))

    # lsof comes second because it needs the shell pids the first pass found;
    # asking about every process instead would be a far bigger call.
    owners = task_owners_for([row["pid"] for row in shells])
    # One transcript read per session, not per shell.
    backgrounded = {}
    for slug, session_id, _ in owners.values():
        if (slug, session_id) not in backgrounded:
            backgrounded[(slug, session_id)] = backgrounded_task_ids(slug, session_id)
    classify_shells(shells, owners, backgrounded)

    for row in shells:
        parent = by_pid.get(row["claude_pid"], {})
        row["session"] = parent.get("session", "")
        row["cwd"] = parent.get("cwd", "")

    if as_json:
        print(json.dumps(shells, indent=2))
        return

    if not shells:
        print("No shells are being held.")
        return

    # True forces color; None lets rich detect a terminal. False would mean
    # "never a terminal", which is what --color's absence used to pass.
    console = Console(force_terminal=True if force_color else None)
    console.print(build_shell_table(shells))

    # A foreground call is supposed to return, or be moved to the background. One
    # still running after an hour means the tool call that owns it is long gone
    # and nothing will reap the shell. Said only where the transcript confirmed
    # the call was never backgrounded -- an unreadable transcript is not evidence.
    stale = [r for r in shells if r["kind"] == "foreground" and held_for_an_hour(r["age"])]
    if stale:
        console.print(
            f"[dim]{len(stale)} shell(s) held over an hour by a call the transcript never "
            f"backgrounded — the tool call that started them has almost certainly ended.[/dim]"
        )


def main(argv=None):
    args = parse_args(argv)
    # Resolved once: shorten_cwd runs per row, and used to re-scan argv and the
    # environment for every process in the listing.
    prefixes = project_dir_prefixes(args.project_dir)
    my_pid = str(os.getppid())
    pids = get_claude_pids()
    if not pids:
        print("No Claude processes found.")
        return

    # `pgrep -f claude` matches the whole command line, so the candidate list picks
    # up anything running under a path containing "claude" — shells, esbuild,
    # file watchers — and even a binary literally named `claude` in a scratchpad.
    # Confirm each one by the handle it holds on its own executable under
    # ~/.local/share/claude/versions/, which no impostor has and which `comm`
    # matching cannot distinguish.
    facts = get_lsof_facts(pids)
    procs = collect_procs(pids, facts, confirm=bool(facts))

    # lsof answered, and confirmed nothing. The version-handle marker assumes the
    # native installer's layout; an npm launch (`node .../claude-code/cli.js`) holds
    # its txt handle on the node binary and has no path under the versions dir, so
    # every candidate fails a check that was only meant to drop impostors. Degrade
    # to the unconfirmed match, as the missing-lsof path already does — a listing
    # with some junk in it beats "No Claude processes found." on a live fleet.
    if not procs:
        procs = collect_procs(pids, facts, confirm=False)

    if not procs:
        print("No Claude processes found.")
        return

    tmux_map = get_tmux_tty_map()
    for p in procs:
        p["session"] = tmux_map.get(p["tty"], "")

    procs.sort(key=lambda p: p["started"] or datetime.min)

    mem = memory_by_pid()
    if mem:
        table_procs = all_processes()
        listed = {p["pid"] for p in procs}
        for p in procs:
            p["mem_kb"] = session_memory(p["pid"], table_procs, mem, stop=listed - {p["pid"]})
    if args.sort == "mem":
        procs.sort(key=lambda p: p.get("mem_kb") or 0, reverse=True)
    elif args.sort == "cpu":
        procs.sort(key=lambda p: float(p["cpu_pct"]), reverse=True)

    if args.shells:
        render_shells(procs, args.json, args.color)
        return

    # Resting-state layer (opt-in): which sessions are waiting on user input. Only
    # pay the claude-ls call when --status (or --status JSON) is requested, never
    # on the default listing.
    status = args.status
    if status:
        rows = fetch_pending_rows()
        annotate_waiting(
            procs,
            waiting_by_cwd(rows),
            waiting_by_session(rows),
            load_session_registry(),
        )

    if args.json:
        out = []
        for p in procs:
            row = {
                "pid": p["pid"],
                "ppid": p["ppid"],
                "orphan": is_orphan(p),
                "age": format_age(p["started"]),
                "cpu_pct": p["cpu_pct"],
                "mem_kb": p.get("mem_kb"),
                "session": p["session"],
                "cwd": p["cwd"],
                "version": p.get("version"),
                "command": classify_command(p["args"]),
            }
            if status:
                row["waiting"] = p["waiting"]
                row["waiting_minutes"] = p["waiting_minutes"]
                row["waiting_source"] = p["waiting_source"]
            out.append(row)
        print(json.dumps(out, indent=2))
        return

    console = Console(force_terminal=True if args.color else None)
    console.print(build_process_table(procs, my_pid, prefixes, status, width=console.width))

    if any(p.get("mem_kb") is not None for p in procs):
        total = sum(p.get("mem_kb") or 0 for p in procs)
        console.print(
            f"\n[dim]Mem: each session with the processes it started (language and MCP "
            f"servers, tool shells); {format_mem(total)} in all[/dim]"
        )

    orphans = [p for p in procs if is_orphan(p)]
    if orphans:
        console.print(
            f"\n[yellow]⚠ {len(orphans)} orphaned (parent gone, PID in yellow): "
            f"{', '.join(p['pid'] for p in orphans)}[/yellow]"
        )

    old = [p for p in procs if p["started"] and (datetime.now() - p["started"]).days >= 3]
    if old:
        old_kb = sum(p.get("mem_kb") or 0 for p in old)
        held = f", holding {format_mem(old_kb)}" if old_kb else ""
        console.print(f"\n[yellow]⚠ {len(old)} process(es) older than 3 days{held}[/yellow]")

    # The version comes free with the identity check, and a session pinned to an
    # older binary than its siblings is exactly the one worth restarting.
    versions = sorted({p["version"] for p in procs if p.get("version")}, key=version_key)
    if len(versions) > 1:
        newest = versions[-1]
        stale = sum(1 for p in procs if p.get("version") and p["version"] != newest)
        console.print(
            f"[dim]{stale} session(s) on an older build than {newest} "
            f"(running: {', '.join(versions)})[/dim]"
        )

    if status:
        if any(p["waiting"] is None for p in procs):
            console.print(
                f"\n[dim]? status unknown — `claude-ls pending` did not answer "
                f"(not on PATH, failed, or exceeded {PENDING_TIMEOUT_SECONDS}s)[/dim]"
            )
        else:
            n = sum(1 for p in procs if p["waiting"])
            if n:
                console.print(f"\n[yellow]⏳ {n} session(s) waiting on input[/yellow]")
            # Only the rows actually showing `dir` — the note explains that label, so
            # counting cwd-sourced rows that came back "not waiting" describes rows
            # the reader cannot see.
            approx = sum(1 for p in procs if p["waiting"] and p["waiting_source"] == "cwd")
            if approx:
                console.print(
                    f"[dim]{approx} row(s) matched by directory, not session — `dir` "
                    f"means some session in that directory is waiting, not this one "
                    f"(no session record: started outside tmux, or an older CLI).[/dim]"
                )


if __name__ == "__main__":
    main()
