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
```

With the project installed, `uv run claude-reel …` works via the `[project.scripts]`
entry; `python -m claude_reel …` also works.

In the deck: `←`/`→` (or `j`/`k`, space) to move, `e` to expand every chip on a slide,
`g` for the agenda. Chips are folded by default (`--density firehose` opens them).
`/` finds text across every slide, folded chips included; Enter or `n` jumps to the next
matching slide, Shift+Enter or `N` back. The URL tracks the slide (`deck.html#s42`), so a
link opens on that slide.

**Context.** What the harness injected into the model's requests (system prompt, token
budget, environment updates, reminders, task notifications) shows as muted `ctx` chips
where it happened. A repeat of identical text is one line pointing back to the slide that
carries it ("unchanged since beat 12"); the budget is one chip per slide. Parts of a
request that merely copy a message already in the transcript are skipped.

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
- `credits.py` — the closing card: subagents from the session's own pool, family and
  compactions from `claude-ls lineage --json` (a subprocess, so no import of claude-ls).
- `cli.py` — entry point.

The `reel` is deliberately a stable, dumpable format (`--reel`): a later Sonnet-skill
curator would read the same beats and emit a richer deck without touching extract or
render.

Stdlib-only, no runtime dependencies.
