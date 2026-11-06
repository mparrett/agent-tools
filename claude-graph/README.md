# claude-graph

An overview of what happened when across your Claude Code sessions — and a map of the
periods of high concurrency and inter-dependence. The historical/relationship
counterpart to `claude-ps`'s live view: same session-identity model, different time
axis.

**Status: v2.** The timeline is the default view; the v1 artifact-group scan lives on
as `scan`; the seed view is unchanged. Everything can emit one canonical JSON document
(`--json`) for piping or a future frontend.

## Run

```sh
python -m claude_graph                 # timeline: what happened when (~7d)
python -m claude_graph 30d            # a wider window
python -m claude_graph <session-id>   # seed: that session's cluster, session-rooted
python -m claude_graph file <substr>  # which sessions EDITED files matching this
python -m claude_graph scan           # the artifact-group map (v1's default)
python -m claude_graph json           # the canonical JSON document
python -m claude_graph --json <id>    # same document, scoped to a seed's cluster
```

With the project installed, `uv run claude-graph …` works via the `[project.scripts]`
entry.

### Timeline (default)

Every session in the window is a row, newest first, bucketed by day — a
`git log --graph` for your session store:

- The **lane gutter** draws wall-clock concurrency: `●` is a session at its last
  activity, `│` is another session actively working at that same moment. Sessions
  that were genuinely concurrent occupy parallel lanes; lanes are colored by
  **project**, so interleaved work reads as threads.
- Concurrency is *activity*-based, not window-open-based: a transcript's timestamps
  are gap-split (30 min) into activity segments, so a session left open overnight
  doesn't read as an all-night worker. A session resumed across days is anchored at
  its last activity and tagged `since <date>`.
- **Links** (`↔ PR org/repo#7 with ab12cd34 …`) annotate each session with the
  artifacts it shares — PRs, branches, files — and the co-sessions, strongest first.
  Scaffolding files (MEMORY.md, handoff notes, …) never count as links.
- The header digest names the window's **peak concurrency** moment.
- A session `/clear`ed from another is tagged `↺ cleared from <id>` and keeps its
  pane's lane, so a pane cleared five times reads as one thread rather than five
  unrelated sessions. The link is the `session_id` Claude Code writes on every
  record after a clear (see claude-ls's README, "Lineage across `/clear`").

### File lookup

`file <substr>` answers "who touched this?": it matches against *edited* canonical
file paths (mentions in conversation don't count — that's what makes it precise when
a filename leaks into neighboring transcripts via git-status output), shows every
editing session under the full path — including single-editor files the scan would
never group — and carries each session's `↔` cluster links so the lineage is visible
without a second command. Scratch/temp copies are hidden unless the pattern itself
says `tmp` or `scratch`. Unbounded by default, like a seed; exits non-zero on a miss.

### Scan and seed

`scan` is the artifact-anchored group map (PR green · branch magenta · file cyan),
ranked by shared-session weight. It now shows the top 15 groups (`-g N`, `0` = all)
and hides scaffolding-file groups by default (`--scaffold` restores them). `seed <id>`
roots the graph at one session and shows the cluster it belongs to; it scans the whole
store by default so old co-members aren't missed (bound it with `--days`/`--since`).

### JSON

`json` / `--json` emit one document — `sessions` (with lanes and activity segments),
`groups` (with weights and scaffold flags), pairwise `edges` (with shared artifacts
and a wall-clock `concurrent` flag), `lineage` (each `/clear`ed session and the root
it came from; sessions also carry `origin`), and the peak-concurrency stat. The terminal
renderers and this exporter are two views over the same model, so a frontend consumes
exactly what the terminal shows.

## Flags

`-p/--project <substr>` · `-d/--days N` · `--since DATE` · `-n/--count N` (cap loading
to the N most recently active; default uncapped) · `-g/--groups N` and `--scaffold`
(scan) · `--subagents` (expand instead of `+N subagents`) · `--no-digest` (scan) ·
`--compact` (quote inline vs its own line) · `--color {auto,always,never}`
(`--no-color` = `never`; `--color always | less -R` keeps color through a pager).

### Colors

Each color carries one meaning (see `docs/render-improvements.md`):

- **Artifact kind** — PR green · branch magenta · file cyan.
- **Project identity** — timeline lanes, dots, and project names: one stable color
  per project.
- **Structure** — tree glyphs `├── └── │` and the `⇄` / `↔` tag symbols: yellow.
- **Time** — timestamps, durations, and `→` date-range spans: cyan.
- **Muted** — tag bodies (`N repos`, `also …`), lifted project, and quotes recede.

No populated store handy? `just graph-demo` (or `uv run claude-graph/tools/demo.py`)
renders the timeline, scan, and seed views over synthetic data — useful for a quick
look or a screenshot (`--svg out.svg` exports a terminal-style image).

## Architecture

claude-graph reuses claude-ls's transcript→work-signal parsing rather than duplicating
it. `claude_graph/sessions.py` is the **seam**: it loads claude-ls (a sibling single-file
script) with `importlib` and re-exports the primitives. If that parsing layer is ever
extracted into a shared core, only that one file changes.

- `sessions.py` — the seam: session enumeration, work signals, canonical repo identity.
- `subagents.py` — `find_subagent_sessions`: the on-disk `.meta.json` sidecar traversal.
- `graph.py` — the model + `build_session_graph` (artifact-anchored, hub-suppressed,
  fileset-merged) + activity segments.
- `timeline.py` — lanes (active-overlap coloring), per-session links, peak concurrency.
- `render.py` — the timeline, the scan tree, and the session-rooted seed.
- `export.py` — the canonical JSON document.
- `cli.py` — entry point: timeline/scan/seed/json dispatch (v1 invocations still work).

Requires claude-ls beside it in the agent-tools monorepo, or `$CLAUDE_LS_PATH` pointing
at `claude-ls.py`.
