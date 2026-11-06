# claude-watch

Follow one Claude Code session as it runs: each turn or `/loop` tick, the tools
it calls, and the subagents it spawns. While a subagent is running, its own tool
calls show up inline, prefixed with a tag taken from its description.

```sh
claude-watch <session-id|prefix|path>       # last 5 turns, then follow
claude-watch 5e55 -n 20                     # more history first
claude-watch 5e55 --since 6h --no-follow    # catch up and exit
claude-watch 5e55 -v                        # every tool call, every repeat tick
claude-watch 5e55 --redraw                  # stream ticks live, then fold repeats
```

History shows each turn on two lines. The first has the start time, duration,
tool and agent counts, the next scheduled wakeup, and the label: `tick` for a
`/loop` wakeup, otherwise the prompt that started the turn. The second has the
turn's last assistant text. Below 80 columns the stats drop their padding.

```
── 07:00     6s   2 tools            next 07:30  tick
     ↳ Quiet tick 0. Nothing moved.
── 07:30     6s   2 tools            next 08:00  tick
     ↳ Quiet tick 1. Nothing moved.
── 08:00     6s   2 tools            next 08:30  tick
     ↳ Quiet tick 2. Nothing moved.
── 08:30    10s   2 tools  1 agent   next 09:00  tick
     ↳ One PR moved; dispatched a reviewer.
   ✓ agent Review the parser…  done 6m35s · Agent "Review the parser change" finished
```

Once the history is printed it switches to live mode and streams each line as
it is written. On a terminal, a status line counts down to the next wakeup:

```
── 09:00 tick
   Bash  List open PRs and their checks
   ⧗ agent Review the parser…  spawned general-purpose/sonnet · background
   ┊ Review the parser…   · Read  ~/src/parser.py
   ✓ agent Review the parser…  done 6m35s · One finding: the new branch skips the empty-input case.
   └ 10s · 1 tool · 1 agent · next 09:30
  ⋯ idle · next wakeup 09:30 (in 29m)
```

A quiet `/loop` tick that repeats the one before it folds into a single
counter line under that tick. Quiet means it spawned no agent, edited no file,
hit no error, and finished within two minutes. Repeat means its last message
matches, ignoring numbers. In history:

```
── 18:03    36s   2 tools            next 19:03  tick
     ↳ Quiet tick: 3 PRs open, nothing moved.
   ≡ ×5 same result · 19:04–23:08 · 27s–37s · next 00:08
```

Live, the counter updates in place. To make that possible, a tick's lines are
held until it ends, and then either printed in full or folded. A tick that
stops being quiet prints what was held and streams from then on. `--redraw`
streams every tick as it runs and erases a repeat afterwards; it needs a
terminal. `-v` turns folding off.

## How it reads the store

It reads only the transcript files: the main `<id>.jsonl` and
`<id>/subagents/agent-*.jsonl`. It doesn't use hooks and doesn't talk to the
running process.

- **Turns** open on a `scheduled_task_fire` system record (a `/loop` tick) or on
  a user prompt while no turn is open, and close on `turn_duration`. The user
  messages that `/loop` injects after a fire land inside the open turn, so they
  don't open turns of their own.
- **Subagents** are joined through `subagents/agent-<id>.meta.json`, whose
  `toolUseId` names the parent's `Agent` call. A subagent counts as finished on
  its `SubagentHandback`, on the parent's tool result (for foreground agents), or
  on the `<task-notification>` queued in the parent, whichever arrives first. If
  it writes again after that (resumed through `SendMessage`), it is marked `↻`.
- **Startup** reads backwards from the end in growing windows to find the Nth-last
  turn, so a 100 MB transcript costs a few MB to open. A subagent spawned before
  that window is followed only if its transcript was written to in the last
  15 minutes and has no handback.

Stdlib only; `NO_COLOR` and `--no-color` are honoured.
