# claude-ls

CLI viewer, search, and resume tool for Claude Code session logs.

`claude-ls` is a self-contained script with inline `uv` dependencies.

## Quick start

```sh
./claude-ls.py last              # last assistant response (auto-detects project)
./claude-ls.py last --json        # same, as JSON (last_text + recent messages)
./claude-ls.py last --cwd ~/p/foo # resolve another directory's session, not the CWD
./claude-ls.py list              # recent messages from the current project
./claude-ls.py browse            # interactive browser
```

`last --json` (optionally with `--cwd <path>`) is the scripting surface for fleet
tooling: hand it a directory, get back that session's resting state — `session_id`,
`project`, the real `cwd` and `git_branch` (read from the transcript), `mtime`, and
`last_text` plus a `messages` array. `messages` holds the recent messages of the
selected `--role` (default `assistant`), so it is assistant-only unless you pass
`--role all`; `last_text` is always the last assistant turn.

## Search

```sh
./claude-ls.py search "query"    # find sessions containing text
./claude-ls.py search "bug" -C   # show matching context
./claude-ls.py search "bug" -r 1 # resume the 1st result (or -r <id-substring>)
./claude-ls.py list -c chess     # find sessions by project/session name
```

Results are numbered, so after a search you can up-arrow, append `-r <#>`, and
resume that row — or pass a session-id substring. It hands off to `claude -r`
with the same double-drive check as `resume`; add `-f`/`--force` to skip that
check and resume anyway.

`resume` refuses when something is already driving the session: a live
`claude -r <id>` on that exact id, or a tmux pane whose working directory is the
resume target and whose process tree holds a running Claude. Matching is on cwd
and process, never on session name, so a `foo-chained` session next to `foo` no
longer blocks and an idle same-named session no longer looks busy. If nothing is
observably running but the transcript was written in the last few minutes, it
cautions and proceeds — a transcript is equally warm right after you quit.

## Other modes

- `info <id>` — the session card: project (the cwd the transcript recorded), when, duration, the model answering now (and the one the session started on, if it switched), compactions, where it sits among the sessions its pane was `/clear`ed through, work signals, first/last message, and ready-to-run follow-ups (`show` and `summary` are aliases)
- `sessions` / `get <id>` — inspect specific sessions
- `get` vs `export` — `get` prints raw message text for piping (no header, no metadata); `export` renders a markdown document (frontmatter table, structure). Reach for `get` in a pipeline, `export` when a human or a doc will read it. `export` follows a session's slug-mates and its pane's `/clear` lineage, concatenating them oldest first with a boundary between files; `--no-follow` exports the one file

- `here` (alias `find`) — sessions anchored to the current dir; from a git worktree it also surfaces ones started in the source repo (`--deep` also scans for sessions that did work here but launched elsewhere). A session that was `/clear`ed from another shows `↺ cleared from <id>` under its topic, as does `sessions`
- `pending` — sessions waiting on user input or a permission prompt; `pending --json` includes each session's real `cwd` and `git_branch` (read from the transcript), so callers can join a waiting session to a process by path, and the full `session_id` (the listing abbreviates it for display, the JSON does not — it is a join key)
- `activity` — recent session activity
- `who` — the live fleet, grouped by repo: tmux pane, checkout @ branch, taken from where the session's edits land rather than where it launched (a session started in the main checkout and working in a worktree shows the worktree; two live sessions editing one checkout are marked `!`), busy/idle status, model, topic, last prompt, files touched. From inside a session, `who --me` shows that session and its repo-mates, resolved from `CLAUDE_CODE_SESSION_ID` (or the process ancestry). `who --json` is the form a hook consumes. Reads Claude Code's own `~/.claude/sessions/<pid>.json` registry, so nothing has to be posted and nothing goes stale. A pane that has been `/clear`ed shows `cleared from <id> · N sessions in this pane`
- `resume` — `cd` into a session's anchoring dir and hand off to `claude -r`. When the id you pass matches several sessions (e.g. a short partial id that's a substring of more than one), it lists the candidates by directory and mtime; choose one with `resume <id> --pick <n>`. When the id names a session that was `/clear`ed and its pane now runs a later session, `resume` refuses and points at the pane and the later id (`--attach` jumps there, `--force` resumes the old transcript anyway)

### `-n` means rows

`-n` is the count of *rows this command lists* — sessions, results, matches — on
`list`, `sessions`, `search`, `browse`, `here` and `pending`. Commands that count
something else spell it out: `last --turns`, `get --tail`. `resume` takes
`--dry-run` (it was `-n`, which read as a count and silently consumed nothing).

### `--sort` picks which timestamp

`sessions`, `here` and `list --contains` order newest-first by the last message in
the transcript. They used to order by file mtime, which is not when the
conversation last moved: sidecar records get appended to finished transcripts, so
a file can be touched long after its final turn. Of the 12 most-recently-touched
transcripts in one real store, 8 had drifted from their last message and 4 by more
than a day (measured 2026-09-18) — enough to float a two-day-stale conversation to
the top of a listing that claims to be newest-first.

- `--sort active` (default) — the last message in the transcript
- `--sort touched` — file mtime, what every release before this one used
- `--sort created` — the first message

The time column follows the key, so the value you sorted on is the one you see.
`active` and `created` read the transcript — the last (or first) record that is a
turn someone took, which skips the sidecar records on the Claude side and the
`token_count` / `task_complete` lifecycle events a Codex rollout ends with.

Ranking the whole store stays cheap because a message can't predate the file
holding it: candidates are walked newest-mtime-first and the walk stops once the
next mtime can't beat the worst row already in the top N. Listing the 10 newest of
833 transcripts opened 28 of them (measured 2026-09-18).

The same order decides which session a command opens when you name none. `last`,
`get`, `export` and `browse` used to take whichever file was written to last, so
they could open a different session than the one at the top of `sessions`.

### What a row calls a session

The topic column is the first thing the human typed. Two things are not that, and
both used to win it:

- **The slug.** Claude Code names a session with a generated phrase
  (`moonlit-cooking-avalanche`), and the listing preferred it — 27 of 852 rows in a
  real store showed one while the prompt underneath it was available (2026-09-19).
  It is now the fallback for a session nobody typed into, which is what `who` has
  always done.
- **A placeholder.** A pasted image is written as `[Image: source: <path>]`, and a
  message that is nothing but one bracketed stand-in is not prose. It still counts
  as a turn; it just can't be the topic.

## Lineage across `/clear`

`/clear` keeps the process, the tmux pane, and the registry entry; it mints a new
session id and starts a new transcript, and the old one stays on disk. Every
record written after that carries `session_id`, the session the process was
first started as, beside `sessionId`, the current one. It is a root pointer: a
pane cleared three times has three transcripts all naming the first. `info`,
`here`, `sessions`, `who`, and `resume` read it, so a pane's history reads as one
stream rather than unrelated sessions, and `export` concatenates the chain into one
document.

Two facts shape how it is read. The pointer is taken from the transcript's head,
because a session later resumed with `claude -r` in a fresh process appends
records without the field. And retention deletes roots before their
descendants, so a chain is gathered by shared origin, never walked from its
root; the card says "root transcript gone" when that has happened. Transcripts
from builds that predate the field simply have no lineage.

## Who else is here: the SessionStart hook

Several sessions in one repo is normal (launch from the main checkout, spawn
worktrees), and the cost is rarely clobbering. It is confusion: a question meant
for one stream lands in another, and the two duplicate or derail, because a
session has no idea its siblings exist. `session_mates.py` fixes that at the
source. Registered for `SessionStart`, it runs `claude-ls who` for the session's
repo and prints the other live sessions into the new context: pane, status,
checkout @ branch, model, topic, last prompt, files touched, and one line asking
the session to say so before acting on a request that looks like another
stream's. A session with no repo-mates gets nothing, so the common case costs
zero tokens; a three-session repo cost about 190 tokens when measured on
2026-09-05.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact|fork",
        "hooks": [
          { "type": "command", "command": "/ABSOLUTE/PATH/TO/claude-ls/session_mates.py", "args": [], "timeout": 15 }
        ]
      }
    ]
  }
}
```

All five sources, because each hands the model a context that has never seen
the fleet; `fork` covers `/branch`, `/fork`, and `--fork-session`. The empty
`args` selects exec form, so an install path with spaces is passed to the hook
whole instead of being split by a shell. The hook finds `claude-ls` on PATH or
beside itself, always exits 0,
and sends any failure to stderr, which Claude Code does not add to context. The
block is a snapshot from session start; it says so, stamped with the time it ran,
and points at `claude-ls who` for the current view. `who`'s own text output opens
with the same stamp, because an agent that runs it reads the result back from
context long after it was true.

Run `--help` or `<command> --help` for full options.
