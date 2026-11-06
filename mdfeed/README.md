# mdfeed

Live web viewer for `.md` files Claude writes/edits, with a split-pane UI:
recent files on the left, rendered + navigable preview on the right.
Auto-refreshes via SSE.

Two entry points, both in this directory, plus two sibling modules they load
by path (`feedlib.py`, shared derivation logic; `htmldiff_engine.js`, the diff
view's redline engine), so the directory moves as a unit:

- `mdfeed.py` — the viewer, a `uv` PEP-723 script (deps install automatically).
  Requires [`uv`](https://docs.astral.sh/uv/) on PATH.
- `mdfeed_write.py` — a `PostToolUse` hook that appends `.md` writes to the
  feed. Optional, but it's what makes the feed populate on its own.

The feed lives at `~/.claude/md-feed/index.jsonl` (override with `MD_FEED_PATH`).

## Requirements

- [`uv`](https://docs.astral.sh/uv/) on PATH — runs the viewer and installs its
  one dependency (`mistune`) automatically on first launch. Nothing to `pip install`.
- Python 3.8+ for the hook (uses only the standard library).
- The hook only fires under [Claude Code](https://docs.claude.com/en/docs/claude-code);
  the manual path below needs neither the hook nor Claude Code.

## Setup

The viewer only reads the feed — something has to populate it. Pick one:

**Auto (recommended) — install the hook.** Edit `~/.claude/settings.json` (create
it if it doesn't exist) and register `mdfeed_write.py` as a `PostToolUse` hook,
using its **absolute** path:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          { "type": "command", "command": "/ABSOLUTE/PATH/TO/mdfeed/mdfeed_write.py", "timeout": 5 }
        ]
      }
    ]
  }
}
```

Swap in the real path (run `pwd` in this directory). The script ships with its
executable bit set, so the bare path works; if you stripped it, either
`chmod +x mdfeed_write.py` or set the command to `python3 /ABSOLUTE/PATH/…`.
Restart Claude Code (or start a new session) for the hook to take effect —
every `.md` it then writes or edits lands in the feed automatically. The hook
self-filters to markdown and never blocks Claude (it always exits 0). If you
already have a `PostToolUse` array, add this whole `{ "matcher": …, "hooks": … }`
entry to it. Only merge the command into an existing entry's `hooks` array if
that entry's matcher is also `Write|Edit|MultiEdit` — dropping it into a group
scoped to a different matcher (say `Bash`) means it never fires for `.md` writes.

**Manual — no hook.** Add files to the feed yourself whenever you want them:

```bash
./mdfeed.py add FILE...
```

To add a file *and* look at it, use `open` (below) — the viewer only serves
paths the feed already knows, so `add` is what makes a file viewable at all.

## Run

```bash
./mdfeed.py                 # serve on http://127.0.0.1:8038 and open a browser
./mdfeed.py --port 8050     # use another port
./mdfeed.py --no-browser    # serve only
./mdfeed.py list            # print deduped feed paths, newest first
./mdfeed.py add FILE...     # manually add markdown files to the feed
./mdfeed.py open FILE...    # add, then show it in the viewer (see below)
./mdfeed.py compact         # shrink the append-only feed file (see below)
./mdfeed.py --help          # all options
```

The feed is append-only — every `.md` write adds a line, but the viewer only
ever shows the newest entry per file, capped at 200 files. `compact` rewrites
the file down to exactly that set, so it's lossless for anything visible.
Before rewriting it saves the original alongside as `.bak` (skip with
`--no-backup`); `--dry-run` reports the before → after numbers without
writing. `--keep N` changes the cap and `--prune-missing` also drops entries
whose file is gone from disk. Safe to run while the hook is active (same file
lock).

## Running it in the background

The viewer is a foreground process. To leave one running:

```bash
tmux new-session -d -s agent-tools-mdfeed -c "$PWD" 'bin/mdfeed'
tmux attach -t agent-tools-mdfeed         # look at its log
tmux kill-session -t agent-tools-mdfeed   # stop it
```

Run that from the clone — `-c "$PWD"` is what makes the relative `bin/mdfeed`
resolve. Restarting is kill-then-start, and an open tab does not need
reopening: on reconnect it sees a new boot id and reloads itself. So a restart
usually wants `bin/mdfeed --no-browser`, or you get a second tab alongside the
one that just refreshed.

## Opening one file

```bash
mdview notes.md             # bin/mdview, i.e. `mdfeed open`
./mdfeed.py open notes.md
```

`open` adds the file to the feed and gets it on screen, starting a viewer if
none is running. Given several files it adds them all and lands on the last.

What it does next depends on whether anyone is watching:

- **A viewer tab is connected** — that tab selects the file, and the browser is
  brought to the front. No new tab: browsers cap connections per origin, and
  each tab holds an SSE connection, so extra tabs starve each other. Pass
  `--new-tab` to override, or `--no-raise` to leave your window order alone.
- **Nothing is connected** — it opens `/?path=…`, a deep link that selects that
  file rather than the newest. `--no-browser` prints the URL instead.

A tab you cannot see is a tab that does not count: a backgrounded one drops its
SSE connection on purpose, so `open` treats it as absent and opens a fresh one.
A tab closed a moment ago can still look connected, because the server only
notices when a write to it fails, so steering is confirmed rather than assumed:
the tab reports back once it has selected the file, and if none does within a
few seconds `open` opens a fresh tab after all.

`open` writes to whichever feed the running viewer serves (`/api/status` says
which), since that feed is the viewer's allowlist. If you passed a `--feed`
that disagrees, it stops and says so instead.

The file the viewer should select travels as `focus.json`, written next to the
feed. It is a file rather than a request to the server so that `open` works
whether or not a viewer is up, and because the SSE loop is already polling a
file. Only a request newer than the moment a tab connected moves it — a marker
left over from an hour ago is not a queued instruction.

### From Finder

```bash
just install-finder-app                        # calls this clone's bin/mdview
just install-finder-app ~/Applications ~/bin/mdview   # calls a path you own
```

Then right-click a `.md` file → **Open With** → *Open in mdfeed*. To make it the
default for every `.md`: Get Info (⌘I), set **Open with**, click **Change All…**.
That also covers `open x.md` from a shell. Finder's ⌥ **Always Open With** does
not: it applies to that one file.

The second argument is worth using. The path is compiled into the bundle, so
whatever you name is what the app runs forever; point it at a symlink you
control and switching which checkout answers costs an `ln -sfn` instead of a
rebuild:

```bash
ln -sfn /path/to/agent-tools/bin/mdview ~/bin/mdview
```

The default bakes in the clone you ran it from, which is wrong in one specific
case the recipe warns about: a linked worktree, whose path stops existing when
the worktree is retired.

It is an app bundle rather than a script because Finder gives you no choice.
Open With is built from LaunchServices, which registers bundles only, and the
file arrives as an `odoc` Apple Event rather than as arguments — a bundle
wrapping a shell script launches with an empty `argv` and no idea what it was
asked to open. So the bundle is an AppleScript droplet whose `on open` handler
shells out to `mdview`. The repo ships the droplet's source; `install-finder-app`
compiles it and bakes in absolute paths, because an app launched by Finder
inherits launchd's `PATH` and will find neither `mdview` nor `uv`.

macOS only, and it needs rebuilding if the clone moves.

## Keyboard shortcuts

`/` search · `Tab` Recent/Plans · `j/k` section · `n/p` files · `x` filter (cycle
projects) · `a` show all · `g#` jump to heading · `e/c` expand/collapse all · `b`
copy file path · `s` rendered ⇄ raw source (textarea, select-all friendly) · `y`
copy the raw markdown · `d` diff · `z` zen · `t` theme · `f` size. Refresh is the
`↻` button in the header.

When the file you are reading changes on disk, a pill offers `r` refresh or `d`
diff. Diff redlines what is on disk now against what the tab last showed —
insertions underlined, deletions struck — at word granularity, inside the
rendered document (or the raw markdown in source mode). The baseline is the
version you read, not the last write, so several writes collapse into one diff
until you press `r` to accept. A file that was mostly rewritten says so at the
top instead of pretending a wall of strikethrough is readable.

The list pane has a **Recent | Plans** toggle (the `Tab` key, or click the
segmented control). *Recent* is every file newest-first; *Plans* filters to plan
docs only. It composes with the project filter, so you can view one project's
plans. Plans no longer pin above recent activity — they sit at their real
timestamp in *Recent*.

Tip: keep a single tab open — each tab holds a live SSE connection, and
browsers cap connections per origin, so many tabs can starve each other.
