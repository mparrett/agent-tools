# claude-graph — render / readability improvements (2026-07-05)

Backlog from a human dogfooding pass on the scan view. The output reads as a
near-monochrome wall: every member row repeats its project and `↔ also` edge, quotes
wrap mid-word, and nothing is colored by kind. These items break up that wall.

Source command that surfaced this: `claude-graph -d 1 --project myrepo`.

## Shared technical constraint

`render.py` currently emits **plain strings with a single per-line `style=`** and
`markup=False` (session content can't inject console markup). Coloring a *span within*
a line — kind color, tag color, a hyperlink — means switching those lines to rich
`Text` objects with explicit appended spans. A `Text` built by `.append(s, style=…)`
does **not** parse markup, so this stays injection-safe. It's a one-time refactor that
items 2, 5, and 6 all share; once it's in, those three are cheap.

## The items, ranked

| # | Idea | Impact | Effort | Status |
|---|------|--------|--------|--------|
| 7 | Lift common `(project)` + `↔ also` to group header | ★★★ | ●●● | ✅ done 2026-07-05 |
| 5 | Color header artifact by kind (pr/branch/file) | ★★★ | ● | ✅ done 2026-07-05 |
| 3 | Quote on its own line (unless `--compact`) | ★★★ | ●● | ✅ done 2026-07-05 |
| 1 | Paging preserves color (`\| less -R`) | ★★ | ● | ✅ done 2026-07-05 |
| 6 | Color inline tags (`⇄ N repos`, `↔ also`) | ★★ | ● (after Text refactor) | ✅ done 2026-07-05 |
| 2 | OSC-8 hyperlink on PR refs | ★ | ●● | last / optional |
| 4 | Absolute-path anchor (selective) | ★ | ●● | fold into 7 |

### Landed in the 1 / 5 / 6 pass

- **Text-span refactor** (the shared dependency): `render.py` header + member rows now
  build `rich.Text` with explicit spans instead of single-style plain strings. Still
  injection-safe (`.append` doesn't parse markup). `graph.group_label` split into
  `group_label_segments` → `[(kind, text)]` so the renderer can color per kind.
- **Colors** (current): `KIND_STYLE` = pr `bright_green` / branch `bright_magenta`
  / file `bright_cyan`; tree glyphs (`├── └── │`) **and** the `⇄` / `↔` tag symbols
  = `yellow` (`TREE_STYLE`); tag body text (`N repos`, `also …`) = muted `grey62`
  (`TAG_STYLE`); timestamps + date-range spans = `cyan` (`TIME_STYLE`); lifted
  project = `dim`; quote = `dim`. Red/blue were dropped in favor of the muted grey.
  Style only the glyph span, never the whole `Text` — a base style bleeds into
  appended content (yellowed the quote/ids until fixed).
- **`--color {auto,always,never}`** (`--no-color` aliases `never`); `always` forces
  ANSI through a non-tty for `| less -R`.
- **Gotcha found:** forcing color exposed rich's auto-highlighter tinting every
  number/quote/paren (notably in the digest). Fixed by `Console(highlight=False)` in
  both `cli.py` and `tools/demo.py` — we now own all coloring.

## Item notes

### 1 — paging preserves color
Rich detects a non-tty stdout and strips ANSI, so `claude-graph … | less` is plain
today. Fix: force color when the user asks. Page with `less -R` (raw control chars),
**not** `less --use-color` (that colors less's own chrome, not our content).
Implementation: a `--color {auto,always,never}` knob (or reuse/extend `--no-color`)
that sets `Console(force_terminal=True)` for `always`. Cheapest item; do first so the
rest is reviewable under a pager.

### 5 — color header by kind
`graph.py` already partitions kinds via `PRECEDENCE`/`TAG`/`KIND_PRIOR`
(`pr` / `branch` / `file`). Pick one distinct color per kind and apply it to the
header artifact segment. Pulls in the `Text`-span refactor.

### 6 — color inline tags
Once 5's refactor lands, color `⇄ N repos` and `↔ also …` distinctly so cross-cutting
sessions pop out of the wall. Rides the same change.

### 3 — quote on its own line  ✅
Rows almost always wrap because of the trailing `"quote"`. Done: the quote now prints on
its own continuation line indented under the id (`prefix + cont`), styled dim, cap 84.
`--compact` restores the old single-line form (quote inline, cap 48). Note the member
row itself can still wrap when the `↔ also …` tag is long — item 7 removes that by
hoisting the shared tag to the header.

### 7 — dedup: lift shared fields to the header  ✅
Biggest clutter win. **Decided rule: all-or-nothing** — a field lifts to the group
header only if *every* member shares it; divergent rows keep their inline annotation.
Landed:
- Uniform `(project)` lifts to the header (dim) and drops from all rows
  (`hide_project`). Chosen over keeping it per-row.
- `↔ also` edges common to *all* members lift to the header as `· also <labels>`
  (`_common_groups`, identity-based since `ArtifactGroup` isn't hashable). Rows show
  only their divergent edges.

Real result — a busy file cluster, before vs after:
```
before: ├── a1b2c3d4  (myrepo)  ~375t  ↔ also PR org/repo#42; file notes.md …
after:  file shell.html · 5 sessions · … · myrepo · also PR org/repo#42
        ├── a1b2c3d4  ~375t  ↔ also file notes.md; 3 files (app.py, …)
```
Scope: scan view only. Digest keeps its own `(project)` format. Seed view unchanged —
possible follow-up.

### 4 — absolute-path anchor  (still open)
7 lifts the project *basename* to the header, not an absolute path. A single absolute
repo path on the lifted header is still a cheap, non-redundant add — not yet done.
Per-row anchors would undo 7, so keep it header-only.

## Sequence

1. Paging/color flag (1) — so the rest is reviewable in `less`.
2. Color header by kind (5) — brings in the `Text` refactor.
3. Color inline tags (6) — free once 5 is in.
4. Quote on its own line + `--compact` (3).
5. Dedup lift (7), folding the abs-path anchor (4) into the lifted header.
6. OSC-8 PR links (2) — optional polish.

**This pass: 1, then 5 + 6.**
