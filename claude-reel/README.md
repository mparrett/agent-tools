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
```

With the project installed, `uv run claude-reel …` works via the `[project.scripts]`
entry; `python -m claude_reel …` also works.

In the deck: `←`/`→` (or `j`/`k`, space) to move, `e` to expand every chip on a slide,
`g` for the agenda. Chips are folded by default (`--density firehose` opens them).

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
- `cli.py` — entry point.

The `reel` is deliberately a stable, dumpable format (`--reel`): a later Sonnet-skill
curator would read the same beats and emit a richer deck without touching extract or
render.

Stdlib-only, no runtime dependencies.
