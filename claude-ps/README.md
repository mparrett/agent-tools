# claude-ps

List running Claude processes with their TTY, working directory, and resource
usage — a `ps` tailored to Claude Code sessions.

Columns: PID, PPID, age, CPU, memory, tmux session, CWD, and command.

Memory is each session together with the processes it started: language
servers, MCP servers, tool shells. On macOS it is `top`'s footprint, which counts
compressed and swapped pages; `ps`'s RSS counts only what is resident, a fraction
of what a session holds once the machine swaps. Elsewhere it falls back to RSS. A
nested `claude` that is listed on its own row is not counted twice.

Rows are oldest first; `--sort mem` or `--sort cpu` puts the largest first. The
table fits the terminal: CWD and Command are cut to one line (CWD keeps its end),
and below 100 columns PPID is left out (`--json` keeps it).

## Which processes count

Candidates come from `pgrep -f claude`, which matches the whole command line and
so picks up anything running under a path containing "claude" — shells, bundlers,
file watchers — along with any binary named `claude`. Each candidate is then
confirmed by the handle it holds on its own executable under
`~/.local/share/claude/versions/<version>`. Only a real session has one.

Matching on the process name instead would not do: a stray executable named
`claude` in a scratchpad passes that check and fails this one.

The version string comes along with the check, so it appears in `--json` and a
footer line flags sessions running an older build than the newest one present —
the ones worth restarting. If `lsof` is unavailable the confirmation cannot run
and the listing falls back to the bare `pgrep` match.

## Usage

```bash
./claude-ps.py            # pretty table of running Claude processes
./claude-ps.py --json     # machine-readable JSON output
./claude-ps.py --color    # force ANSI color (e.g. under `watch`)
./claude-ps.py --status   # add a Status column flagging sessions waiting on input
./claude-ps.py --shells   # shells sessions are holding open (monitors, sleeps, servers)
./claude-ps.py --help     # the flags, and what each one does
```

Live view:

```bash
watch -n2 ./claude-ps.py --color
```

## Held shells

`--shells` answers a different question from the listing: not which sessions are
running, but which ones are **holding a shell open** — a monitor loop, a `sleep`,
a server someone started and never stopped.

A Bash tool call runs as a direct child of the session's `claude` process, wrapped
by the harness: it sources a shell snapshot, then `eval`s the real command. The
snapshot path is what identifies the shell as Claude's rather than a coincidental
`zsh`, and the `eval` payload is the only place the command survives verbatim, so
the listing can show what is actually running. `Blocking` reports the deepest
descendant that isn't itself a shell — `sleep`, `gh`, a server — which is usually
the whole diagnosis.

### Why `Kind` needs the transcript

Every in-flight call — foreground exactly as much as backgrounded — streams to
`<tmp>/claude-<uid>/<project>/<session-id>/tasks/<task-id>.output` and holds that
file open for as long as it runs. So `lsof` over the shell pids answers *which
session and which task*, and the session id falls out of the path, but it cannot
answer whether the call was backgrounded: both look identical on disk.

The transcript is the only thing that knows. The harness announces a backgrounded
call in its tool result two ways — `Command running in background with ID: <id>`
for an explicit `run_in_background`, and `moved to the background (ID: <id>)` for
a foreground call it gave up waiting on. The second leaves no trace in the tool
*input*, so matching the result text is the only way to catch both. The task path
names the transcript to read: the slug is the project directory, the uuid is the
file.

So `Kind` is one of three:

| | |
|---|---|
| `task <id>` | the transcript announced this one; it is a background task |
| `foreground` | a live call the transcript never backgrounded |
| `unknown` | no task file, or the transcript could not be read |

`unknown` stays a distinct answer on purpose. A shell held for over an hour by a
call the transcript never backgrounded has outlived the tool call that started it,
and a footer says so — but only for a confirmed `foreground`. Reporting an
unreadable shell as foreground would flag a healthy long-running monitor as
garbage to reap.

**What it cannot see:** a command that detaches itself (`cmd &`, `nohup`, `( … & )`)
is reparented away from `claude` and never gets a task file, so it belongs to no
session as far as this view is concerned. If a stray server outlives its session,
that is the shape it takes.

## Status column

`--status` adds a resting-state glance across the fleet: a `Status` column that
flags processes whose session is waiting on user input. It shells out to
`claude-ls pending --json` (one call, on PATH) and joins the result to each
process by absolute working directory — the real `cwd` each waiting session
records in its transcript. `--json` then carries `waiting` and `waiting_minutes`
per row. The default listing makes no such call.

There are two joins, and `waiting_source` in `--json` says which one answered for
each row.

`session` is exact — that process's own session, with its own wait. See below
for where the join comes from.

`cwd` is the fallback and is a claim about the **directory**, not the process:
when several sessions share a working directory and any of them is waiting, every
process there is flagged, and the minutes are the directory's freshest wait
rather than that row's. The table labels these `dir` instead of `waiting` so the
two are not read as the same fact, and a footer counts them.

Status has three states, not two. A blank cell means the scan ran and found that
directory idle; `?` means the scan gave no answer — `claude-ls` absent from PATH,
a non-zero exit, unparseable output, or a scan exceeding
`PENDING_TIMEOUT_SECONDS`. In `--json` these are `waiting: false` and
`waiting: null`. They are kept apart deliberately: a failed scan reported as "not
waiting" is a silent false negative across the whole fleet, and under `watch` it
reads as a session that just woke up.

## Exact attribution: Claude Code's session records

Nothing on the process side of a running session identifies which session it is:
the transcript records no pid or tty, a session holds no handle on its own
transcript, and macOS will not surrender the environment. That is why the
fallback join is by directory.

Claude Code closes the gap itself. It writes one record per running session to
`~/.claude/sessions/<pid>.json` — pid, session id, cwd, tmux pane, status — and
the file lives exactly as long as the process. Nothing to install: `--status`
uses it when it is there. (`CLAUDE_PS_SESSIONS_DIR` overrides the directory, for
tests.)

This replaced an opt-in hook of our own, which wrote the same pair to
`~/.local/state/claude-ps/sessions.json`. Because it was opt-in it usually wasn't
installed, so `--status` ran on the lossy `cwd` join while telling the user to go
install something to fix it.

A session can still be missing a record — one started outside tmux, or running a
CLI older than 2.1.2xx. Those rows fall back to the `cwd` join; they are never
reported as a confident negative.

Reading a record does not trust the pid alone. Pids get recycled, so each record
carries the owning process's start time and a row is only claimed when that
matches — otherwise it falls back rather than attributing another session's state
to the wrong process. The comparison is by instant, not by text: Claude Code
writes `procStart` in UTC and `ps` prints local time, so comparing the strings
would reject every live session and silently drop the whole fleet back to `cwd`.

## CWD column prefixes

The CWD column trims a leading project-hub prefix for compactness. The default is
`~/projects/`. Override it with the `PROJECT_DIRS` env var (colon-separated) or one
or more `--project-dir` flags:

```bash
PROJECT_DIRS="~/projects-new/:~/work/" ./claude-ps.py
./claude-ps.py --project-dir ~/projects-new/ --project-dir ~/work/
```
