# claude-closed

List recently closed Claude Code background sessions, newest first, with the
command that reopens each.

In agent view, ctrl+x stops a session and ctrl+x twice removes it from the list.
Both are easy to press too eagerly, and a removed session no longer appears in
`claude agents --all`. The daemon log still records every session it settled.
`claude-closed` reads that log and drops sessions that are running again. It
fills in each session's name and directory from `claude agents --all` or, for a
removed session, from its transcript.

```
  1.   2m ago  stopped            hud improvements  [~/projects/mods]  removed from list
       reopen: claude --bg --resume <session-id>
  2.   3h ago  retired (idle 60m) weft worker  [~/projects/weft]
       reopen: claude --bg --resume <session-id>
```

How a session ended:

| | |
|---|---|
| `stopped` | ctrl+x, `claude stop`, or a tool such as `claude-down` |
| `retired` | the daemon's idle timeout, with how long the session had been idle |
| `exited` | finished on its own |
| `crashed` | the session process died |

`removed from list` marks a session that ctrl+x twice, or `claude rm`, took out
of `claude agents`. It can still be reopened.

`claude --bg --resume <session-id>` brings a session back into the background
under the same id, with its saved name and model, idle until you send it a
prompt.

## Usage

```bash
claude-closed                  # closed in the last 24h
claude-closed --since 30m      # or 6h, 3d
claude-closed --stopped        # only the ones someone stopped
claude-closed -n 5             # at most 5 rows (default 20)
claude-closed --json
claude-closed --html > closed.html && open closed.html
```

`--html` writes one self-contained page to stdout, with no external assets. It
is a table of the same rows, with a copy button beside each reopen command, and
it follows the system light or dark setting. Hover over a row's age for the
exact close time.

The log is `~/.claude/daemon.log`, plus any rotated `daemon.log.*` beside it.
History goes back only as far as the log does.
