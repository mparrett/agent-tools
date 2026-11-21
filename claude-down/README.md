# claude-down

Stop every Claude Code background session, then the daemon, before a reboot or
reset. Prints the plan unless `--go` is given.

Background sessions accumulate. `/exit` in agent view backgrounds a session
rather than ending it. The daemon retires a session only after it has settled
and sat idle for about an hour, so one that left a monitor, a server or a
polling loop running stays busy and never ages out. Claude Code offers
`claude stop <id>` for one session and `claude daemon stop` for all of them at
once. `claude-down` sequences the two so the stop is graceful:

1. Snapshot `claude agents --json`, plus a markdown file with a
   `claude attach <id>` line for each session.
2. Stop the idle sessions.
3. Give the busy ones `--grace` seconds (60 by default) to finish their turn,
   then stop them too.
4. Run `claude daemon stop --any`.

`claude stop` keeps the conversation, so `claude attach <id>` resumes a session
afterwards. Interactive sessions are left alone.

## How long each session has been quiet

Next to its state, the plan shows how long ago each session last wrote to its
transcript (`7d`, `3h`, `20m`, `?` when no transcript is found), and lists the
quietest first. Neither `claude agents --json` nor the daemon records activity,
so this is read from the last timestamped entry in
`~/.claude/projects/*/<sessionId>.jsonl`; the snapshot keeps it as
`lastActivity`. A session idle for days is one the daemon's hourly retirement
missed. One shown as busy whose transcript hasn't moved in hours is probably stuck.

## Pairs with claude-ps

The plan lists, under each busy session, the background commands it is holding
open, taken from `claude-ps --shells --json`. These are usually why a "done"
session never retired, and they are worth reading before you stop it. Without
`claude-ps` on `PATH` the plan omits them.

## Stopping one row from the plan

The plan numbers its rows, and `--pick N` stops just that row:

```bash
claude-down                 # the plan, numbered; also saves the numbering
claude-down --pick 3        # preview row 3
claude-down --go --pick 3   # stop it (or --pick 3,5 for several)
```

Rows are ordered by quiet time, which reshuffles as sessions work, so `--pick`
resolves numbers against the numbering your last full plan saved, not a fresh
listing. Plans are kept per caller (`plan-<session or terminal>.json` in the
snapshot directory), so a plan run from another session can't renumber yours.
A pick refuses when the plan is more than 15 minutes old, when it names a row the
plan doesn't have or a session that has since stopped, and when it names the
session running it, unless `--include-self` is given. Like `--only`, it leaves the
daemon running, and previewing a pick doesn't renumber the saved plan.

## The session running it

Run from inside a background session, `claude-down` skips that session, and the
daemon stop with it, since stopping the daemon would end it too. `--include-self`
stops it last, after everything else.

## Usage

```bash
claude-down                    # the plan: what would stop, and what holds the busy ones
claude-down --go               # carry it out
claude-down --go --grace 0     # don't wait for busy sessions
claude-down --go --only <id>   # one session by id; the daemon keeps running
claude-down --go --pick 3      # one session by its number in the last plan
claude-down --go --keep-daemon
```

Snapshots go to `~/.local/state/claude-down/` (`latest.md` links to the newest),
or to `$CLAUDE_DOWN_DIR`, or to `--snapshot-dir`. Exit status is 1 if any stop
failed. The remaining stops still run.
