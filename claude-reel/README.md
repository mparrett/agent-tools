# claude-reel

Render a Claude Code session — or just the tail you missed — as a paced, collapsed
**slideshow** so you can catch up on semi-autonomous output without scrubbing raw JSONL.
Tool calls fold to chips, prose renders as Markdown, and an agenda up front shows the
shape of what happened. "less on steroids." Smooth jazz.

**Status: POC / spike.** Deterministic curation + self-contained HTML deck. Design and
open questions in [`POC_SPEC.md`](POC_SPEC.md).

## Run

```sh
claude-reel <session-id | substr | path.jsonl>   # write a deck, print its path
claude-reel <id> --open                           # ...and open it in your browser
claude-reel <id> --tail 30                        # only the last 30 beats
claude-reel <id> --since 2026-07-05               # only what landed on/after a date
claude-reel <id> --density tldr                    # agenda only (jazz | firehose | tldr)
claude-reel <id> --reel                            # dump the reel (pivot JSONL) instead
claude-reel <id> --subagents brief                 # agent chips expand to the agent's run
claude-reel <id> --context none                    # hide injected context
claude-reel <id> --no-credits                      # skip the closing credits card
claude-reel <id> --live --open                     # follow a running session; reply if it's --bg
```

With the project installed, `uv run claude-reel …` works via the `[project.scripts]`
entry; `python -m claude_reel …` also works.

In the deck: `←`/`→` (or `j`/`k`, `l`/`h`, `]`/`[`, space) to move, `»`, End or `G` for
the last slide, `$` to the end of the current slide and `0` back to its top, `e` to expand every chip on a slide,
`g` for the agenda. Chips are folded by default (`--density firehose` opens them).
`c` (or the `chips:` button) cycles how chips show, `C` backwards: rows, one per line; inline, side by
side with a short argument; compact, labels only. Clicking a chip in a denser strip
unfolds that strip one level, down to rows, where a click opens the chip. The choice is
remembered in the browser. `?` (or the rail's `?` button) shows each control's keys beside it, and a card of the
keys that have no control, for a few seconds. `t` (or the button in the top bar) cycles the theme: auto
(follows the OS), light, dark; also remembered. `/` finds text across every slide,
folded chips included; Enter or `n` jumps to the next
matching slide, Shift+Enter or `N` back. The URL tracks the slide (`deck.html#s42`), so a
link opens on that slide.

**Live.** `--live` serves the deck from `127.0.0.1` instead of writing a file, and
pushes slides as the transcript grows: the turn in progress grows in place, a new turn
appends a slide. The page follows the tail while you are on the last slide and leaves
you alone anywhere else. It prints a URL carrying a random token, and the server
refuses requests without it. The port is any free one unless `--port` says otherwise,
so several sessions can be followed at once. No credits card, since its lineage scan
would run on every change. Ctrl+C stops it.

**Replying.** For a background session (`claude --bg`, listed by `claude agents`), the
live page has a reply bar: Enter sends, Shift+Enter starts a new line. The server types
the text into the session through `claude attach`, so Claude Code records it as yours,
typed, and queues it if a turn is running. The page says "sent" once the transcript
records it. While the session is blocked on something else, such as a `!` command in
its terminal, Claude Code holds the text unrecorded: the page says it is waiting, turns
to "sent" when your message shows up in the deck, and after 3 minutes without it says
to check the session with `claude attach <id>`. Other sessions get a line saying why there is
no bar, and `--no-reply` turns it off. `attach` is a terminal UI rather than an API, so a
Claude Code update can break this; `send.py` says how it works.

**Context.** What the harness injected into the model's requests (system prompt, token
budget, environment updates, reminders, task notifications) shows as muted `ctx` chips
where it happened. A repeat of identical text is one line pointing back to the slide that
carries it ("unchanged since beat 12"); the budget is one chip per slide. Parts of a
request that merely copy a message already in the transcript are skipped. A compaction's
summary of the earlier conversation is a `compaction` chip, not a turn of yours. Text you
pasted folds into a `pasted · N lines` block inside your message, line breaks kept.

**Credits.** The last slide is a closing card. *Featuring* lists the subagents, grouped
by type and by what they were asked (a slash command by its name), each linking to the
slides it appears on. *Previously*, *Sequels* and *Also in the family* list the sessions
linked to this one by /clear, handoffs, and mentions, each with a copyable command for its
own deck; the session's compactions sit between them. The agenda's cast line
("42 subagents · 3 related sessions → credits") links there. Sessions and compactions
come from `claude-ls lineage --json`, which scans the whole store and adds seconds to a
build; without claude-ls on PATH the card shows the subagents alone. `--no-credits` skips
the card.

**Subagents.** By default an agent chip names the agent. `--subagents brief` places every
subagent at its launch (including forked skills, which have no tool call) and its chip
expands to the agent's prompt, tool counts, final result, and a copyable command for its
own deck.

## How it works

A three-stage pipeline around a **reel** pivot format, so code or an LLM can curate and
any renderer can consume:

```
transcript.jsonl ──[extract]──▶ reel (beats) ──[curate]──▶ deck (slides) ──[render]──▶ deck.html
```

- `sessions.py` — session discovery over `~/.claude/projects` (stdlib glob; top-level
  transcripts only, subagents excluded).
- `extract.py` — transcript → an ordered list of typed **beats** (user_say,
  assistant_say, thinking, tool_call, tool_result, subagent-linked calls). Each tool call
  carries a derived chip summary. Parsing is ported from an earlier session viewer's `parse_transcript`.
- `curate.py` — beats → **slides** (one per user turn), folding tool results into their
  call chips, collapsing repeated calls to `N×`, deriving titles and outcome badges.
- `markdown.py` — a small, escape-first Markdown → HTML renderer (untrusted transcript
  text can't inject markup).
- `render_html.py` — slides → a single self-contained HTML deck (no CDN, inline CSS/JS).
- `live.py` — `--live`: a stdlib HTTP server that polls the transcript, rebuilds on change,
  and streams the slides that changed over server-sent events.
- `send.py` — the reply bar's delivery: `claude attach` driven under a pty.
- `credits.py` — the closing card: subagents from the session's own pool, family and
  compactions from `claude-ls lineage --json` (a subprocess, so no import of claude-ls).
- `cli.py` — entry point.

The `reel` is deliberately a stable, dumpable format (`--reel`): a later Sonnet-skill
curator would read the same beats and emit a richer deck without touching extract or
render.

Stdlib-only, no runtime dependencies.
