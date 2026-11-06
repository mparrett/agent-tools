# claude-reel — POC spec

> Working name (provisional): **claude-reel**. Alternatives: `claude-recap`, `playback`.
> "Reel" doubles as the name of the pivot format below.

**Status: POC BUILT (2026-07-05).** The v0 pipeline below is implemented and validated
end-to-end on a real session — see `claude_reel/` and the README. This doc keeps the
design rationale and the deferred/open items.

## Problem

An operator kicks off semi-autonomous Claude work (a long run, a `/loop`, background
agents), walks away, and comes back to a wall of transcript. Catching up is a *reading*
task, not a search task — but the raw `.jsonl` is unreadable and even the live TUI
scrollback is a firehose. There's no calm, paced way to skim "what happened while I was
gone."

**Persona:** Claude operators catching up on missed semi-auto output.
**Feel:** "less on steroids." Paced, collapsed, sugary. Smooth jazz, not a firehose.

## Hypothesis the POC tests

> Rendering a transcript (or the tail you missed) as paced, collapsed, sugary **slides**
> — tool calls folded to chips, prose rendered, an agenda up front — makes catching up
> fast and pleasant.

Prove that with the cheapest possible build before adding any LLM smarts.

## Architecture: three stages around a pivot format

The two axes you'd otherwise agonize over — *what substrate?* and *code vs LLM curation?*
— are decoupled by a pivot format in the middle. Nothing commits early.

```
transcript.jsonl ──[extract]──▶ reel.jsonl ──[curate]──▶ slides[] ──[render]──▶ deck.html
   (claude-ls)     deterministic   pivot      pluggable    manifest  pluggable   (POC)
```

1. **Extract** (deterministic) — transcript → **reel**: a normalized, ordered list of
   typed *beats*. This is the "lightly prepared nljson." Reuses the claude-ls seam +
   an earlier session viewer's block parsing.
2. **Curate** (pluggable) — reel → slides. POC = rule-based. v1 = a Sonnet skill that
   reads the same reel and emits a richer manifest. **The reel is the contract between
   them**, so code and Sonnet are interchangeable curators.
3. **Render** (pluggable) — slides → output. POC = self-contained HTML deck. Later =
   terminal pager / markdown. Every renderer works regardless of curator.

## POC decisions (locked)

- **Substrate: self-contained HTML deck.** `<details>` gives collapsing for free,
  syntax highlighting + scroll-snap slides + keyboard nav are cheap, and it's
  screenshottable/shareable. Terminal renderer is deferred (the pivot format makes it
  additive).
- **Curator: deterministic first.** Prove the reading UX before spending tokens per run.
- **Tool packaging: separate sibling module reusing the claude-ls seam** — same pattern
  as claude-graph (`importlib`-loads claude-ls, re-exports primitives). Not a claude-ls
  output mode.

## The reel format (pivot)

One JSON object per line. Beats are the transcript's content blocks, normalized and with
tool calls linked to their results. Grounded in the real on-disk shape
(`~/.claude/projects/<flattened-cwd>/<session-id>.jsonl`; records carry
`type`, `timestamp`, `message.{role,content,model,usage}`, `cwd`, `gitBranch`).

```jsonc
// beat kinds (from message.content blocks)
{"kind": "user_say",   "ts": "...", "text": "..."}                       // type:text, role:user
{"kind": "assistant_say","ts":"...", "text": "...", "model": "..."}      // type:text, role:assistant
{"kind": "thinking",   "ts": "...", "text": "..."}                       // type:thinking (collapse)
{"kind": "tool_call",  "ts": "...", "id": "...", "name": "Edit",
                        "input": {...}, "summary": "✎ Edit render.py (+8 −3)"}
{"kind": "tool_result","tool_use_id":"...", "is_error": false,
                        "text": "...", "truncated": false}               // nested under its call
{"kind": "subagent",   "ts": "...", "id": "...", "agentType": "Explore",
                        "description": "..."}                            // from .meta.json sidecar
```

`summary` is a derived one-liner per tool (see "Slide anatomy"). Beats retain `ts` so the
curator can compute per-slide time ranges and the "while you were gone" window.

## Stage 1 — extract (reuse, don't reinvent)

- **Session discovery / selection:** claude-graph's seam (`claude_graph/sessions.py`)
  already re-exports `iter_session_files()`, `list_sessions()`,
  `extract_session_metadata()`, `projects_dir()`. Accept a session id, a substring, or a
  direct `.jsonl` path.
- **Transcript → beats:** port an earlier session viewer's `parse_transcript()`. It already:
  - flattens nested content blocks to render-ready structs,
  - maps each `tool_result` to its `tool_use` by `id` (bidirectional),
  - truncates huge results (`MAX_RESULT_CHARS = 20_000`),
  - emits a `summary` (model, block count, per-tool counts, token usage, ts range).
- **Subagents:** claude-graph's `subagents.py:find_subagent_sessions` (the `.meta.json`
  sidecar traversal) — for the `subagent` beat and optional inline expansion later.
- **First-user text:** an earlier session viewer's `_first_user_text()` (skips injected
  `<task-notification>` / `<command-name>` wrappers) — for slide titles.

## Stage 2 — curate (deterministic rules)

Rule-based chunking of the reel into `slides[]`:

- **Slide boundary:** start a new slide at each `user_say` (a user turn is one beat).
- **Within a slide:** render assistant prose; fold consecutive `tool_call`(+result) into
  one **tool strip**; collapse `thinking` to a chip.
- **Repeat grouping:** 2+ tool beats with identical `summary` collapse to one row with an
  `N×` badge (a session monitor's `maybe_group_repeated_blocks` pattern in its scrollback component).
- **Long-turn split:** if a slide's assistant prose is very long and has multiple `##`/
  `###`, split into sub-slides on heading boundaries — keeps "one idea per slide."
- **Title:** first line of the user prose (trimmed), else the first assistant heading.
- **Badges:** regex-scan the slide for outcomes to surface — `\d+ passed`, PR links,
  `error|Traceback`, files-edited count.

`slides[]` manifest:

```jsonc
{"index": 3, "title": "Fix the cyan/time color collision",
 "ts_range": ["...","..."], "badges": ["54 passed", "1 file"], "beats": [ ... ]}
```

## Stage 3 — render (HTML deck)

Single self-contained `.html` (inline CSS/JS, no CDN — mirrors an earlier session viewer's stdlib,
single-file approach and keeps it Artifact-publishable).

- **Slides:** `<section>` per slide, CSS scroll-snap; `←/→`, space, `j/k` nav; a dot rail
  showing position.
- **Agenda slide (slide 0):** generated TOC of titles + badges — the "here's what
  happened in 8 beats" catch-up view. Click a row to jump.
- **Tool chips:** `<details>` (native collapse); collapsed shows the `summary`, expanded
  shows input + (nested) result. `e` expands all chips on the current slide.
- **Diffs:** Edit/Write expanded view renders a colored add/del/context diff (a session monitor's
  `@update_block_re` add/del/context classification).
- **Markdown + code:** render assistant/user markdown; syntax-highlight code blocks with a
  language label; fold long blocks. (POC: minimal inline highlighter or plain `<pre>` —
  do not pull a CDN.)
- **Theme:** dark, calm. Reuse claude-graph's *semantic* color intent — cyan = time,
  muted grey = secondary metadata, one accent per role. Smooth jazz.

## Slide anatomy (the sugar)

Per-tool `summary` derivation:

| Tool | Chip |
|------|------|
| Edit / Write | `✎ Edit render.py (+8 −3)` |
| Bash | `⚡ pytest -q` → result badge (`54 passed` / exit code) |
| Read | `📖 Read render.py` |
| Grep / Glob | `🔍 Grep "TREE_STYLE"` |
| Task / Agent | `🤖 Explore: scout prior art` |
| other | `🔧 <name>` |

Everything folds by default; the operator opens only what they want. Prose stays
scannable — a session monitor's core insight: don't hide tool calls, *summarize them in collapsible
sections*.

## CLI (POC)

```sh
claude-reel <session-id | substr | path.jsonl>   # → writes deck.html, prints path
claude-reel <id> --tail 20                        # last 20 beats (catch up on the tail)
claude-reel <id> --since 2026-07-05T13:00         # only what landed after you left
claude-reel <id> --density jazz                    # firehose | jazz | tldr(agenda-only)
claude-reel <id> --out deck.html
```

`--tail`/`--since` are the "catch up on what I missed" knob; pair with `claude-ls`
to find the session id.

## POC scope

**In (v0 — the spike):**
- extract → reel (seam + the ported session-viewer parser)
- deterministic curate (turn-boundary slides, tool/thinking/repeat collapse, titles, badges)
- HTML deck render (scroll-snap, `<details>` chips, agenda slide, dark theme, keyboard nav)
- validate on a real recent session; screenshot via playwright/chrome-devtools

**Deferred (v1+):**
- **Sonnet-skill curation** — a skill reads the reel and writes a richer manifest (real
  titles, narrative grouping, per-slide TL;DR, a written agenda). Same reel contract.
- terminal renderer (rich/blessed) and markdown export
- live tail (the earlier viewer does SSE — borrow if wanted)
- inline subagent expansion
- real syntax highlighting

## Reuse map (concrete)

| Need | Source |
|------|--------|
| session discovery / metadata | `claude-graph/claude_graph/sessions.py` (seam) |
| transcript → nested blocks | an earlier session viewer's `parse_transcript` |
| first-user text (skip wrappers) | an earlier session viewer's `_first_user_text` |
| tool_use ↔ tool_result linking | an earlier session viewer (by `id`) |
| collapse heuristics (bash/diff/repeat) | a session monitor's scrollback component |
| subagent sidecars | `claude-graph/claude_graph/subagents.py` |
| terminal styling reference | claude-graph `render.py` palette |

## Open questions

1. **Name** — claude-reel vs claude-recap vs playback.
2. **Syntax highlighting under a strict CSP** (if published as an Artifact): inline a tiny
   highlighter, pre-highlight to spans at build time, or ship plain `<pre>` for the POC.
3. **Slide granularity** when a single assistant turn is enormous — is the heading-split
   rule enough, or do we need beat-count caps per slide?
4. **Seam sharing** — reuse claude-graph's seam module directly, or extract a shared
   `claude_ls` core both import? (Current pattern: each tool carries its own seam.)
5. **Density default** — is `jazz` the right default, or lead everyone with `tldr`
   (agenda-only) since the job is catch-up?
