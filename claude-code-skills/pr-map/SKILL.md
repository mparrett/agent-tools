---
name: pr-map
description: >-
  Build a navigable map of a person's work across a complex repo (or a few
  related ones) — every issue and PR they authored, reviewed, or commented on in a
  theme, laid out as lanes × time with the #-references between them as edges, plus a
  "start here" list and a markdown inventory. Use when the user asks for "an inventory
  / map / graph of all my perf (or auth, or CI, ...) issues and PRs", "how does all of
  this connect", "something I can hand the team so they don't get lost", or wants to
  see which epics, trackers, and PRs a theme hangs off. Historical and relational;
  for the live board of what is open and what to review next, use pr-dashboard, and for
  the delta since last look, pr-watch.
---

# PR map

(Formerly `workstream-map`; renamed so "workstream" means only the `workstream` CLI's
entity. With pr-dashboard and pr-watch it covers what is open, what changed, and the
history of a theme.)

Turn months of scattered issues and PRs into one picture a teammate can navigate: lanes
for the sub-themes, time flowing down, every `#`-reference drawn as an edge, hubs
ranked by how often they are cited, and a curated "start here" per lane. The output is a
self-contained HTML file (d3 from cdnjs, nothing else) and a markdown inventory with the
same data, so it can be published anywhere static or pasted into a doc.

The design separates **facts from judgment**, the same way pr-dashboard does:

- `scripts/fetch.py` pulls the facts from `gh`: everything the user is involved in,
  scored for relevance, then comments and PR metadata for the selected set. Never
  hand-transcribe these; the edges come from the comment text.
- The **config file** holds the judgment: relevance regex, lanes and their match rules,
  exclude/include/override tables, and the "start here" hubs with a one-line why each.
- `scripts/build.py` + `template.html` render. Swap data and config, never restyle per run.
- `scripts/laning.py` holds the lane rules and the template's header-fit arithmetic, so
  the preview cannot disagree with the map it previews and the build can check a lane
  name without rendering it.

## The procedure

Budget: 20 to 40 minutes the first time for a 150-node map, most of it reading titles
in the preview. Later refreshes are one `fetch` and one `build`.

The shape to hold onto: **one cheap loop, then one expensive pass.** Scoring reads only
the title, labels and body, all of which the search already returned, so previewing a
config costs two `gh search` calls per repo no matter how many items it selects, against
one to two API calls for every node the real fetch enriches. Settle the judgment in the
preview and the fetch runs once.

1. **Scope the theme in one sentence** and write the `relevance` regex from it. Strong
   words only (for perf: `perf|bench|ratchet|timeline|baseline|noise|regress|alloc|ns/op`),
   because a title hit scores 3× a body hit and `min_score` 3 means "one strong word in
   the title, or three in the body". Wide nets pull in every PR that mentions "fast".
2. **Preview, and keep previewing until the selection is right:**
   `python3 scripts/fetch.py config.json --preview`. It writes nothing, and `lanes` can
   still be missing or empty at this point — it will list the titles in number order
   until you have some. For each repo it
   prints the selected titles grouped by the lane they would land in, a score histogram,
   and the near misses — items that scored something but not enough, which is where a
   regex that is too narrow shows up. Reading these titles is the step that makes the map
   good; the three lists it produces go into the config:
   - `exclude`: keyword false positives (a docs retrofit that says "baseline", a
     release-prep issue).
   - `include`: things the regex missed that you know belong: epics, trackers, the
     design issue a whole lane hangs off. Seed this from the user's own notes if you
     have them (an agent memory directory works; `notes_dir` will also attach the note
     names to nodes). `include` beats `exclude` if a number is in both.
   - **lanes**: 4 to 7 sub-themes with a `match` regex each, evaluated in order, and a
     `default_lane` per repo. Aim for lanes a newcomer would name, not the ones the
     code layout suggests. Two small repos usually earn their own lanes rather than
     sharing the primary repo's. Order the lanes most specific first and make the
     broadest one the default, or the broad regex swallows everything ("shell" caught
     every mobile PR on the second repo this was tried on). Word-boundary short
     tokens: `\bci\b`, not `ci`, which matched "synchronized" and "incremental";
     `\bkey\b`, not `key`, which matched "key lg cache".

   Each line is marked `m` if a lane regex claimed it, `o` for an `override`, and `d`
   if nothing matched and it fell through to `default_lane`. A lane that is mostly `d`
   has no working rule yet.
   - **`body_cap`**: the score is 3 per title or label hit plus 1 per body hit up to
     this cap (default 6), against `min_score` 3. When the theme's words are everyday
     vocabulary in the repo (a game repo where "shell", "tag", and "release" appear in
     most bodies), three body mentions select almost everything: 114 of 154 items on
     that repo. Set `body_cap` to 2 so a title hit is required. The histogram is the
     tell — a pile at exactly 3 with no title hits means the cap is doing the selecting.
3. **Fetch once**, when the preview stops changing:
   `python3 scripts/fetch.py config.json`, which writes to `data_dir`. One to two API calls per
   selected node; 200 nodes is two to three minutes.
4. **Build and read the report.** `python3 scripts/build.py config.json`
   prints per-lane counts and then four lists, each naming the config table that fixes
   it:
   - *`include` entries not in the fetched data* — a wrong number, a deleted item, or
     an `include` you added since the last fetch. The fetch warns about these too, but
     only in the run that first missed them.
   - *no lane rule matched* — these fell through to `default_lane`. Give the lane a
     `match`, or put the node in `override`. Not a cleverer regex; overrides are cheaper
     to read six months later.
   - *cited by the map but not in it* — numbers that two or more of the selected items
     reference and the map does not contain. The honest `include` candidates, ranked by
     how many nodes cite them, not how often they are mentioned. Ones
     already in `exclude` are marked, so a deliberate omission does not come back every
     build. **Adding to `include` means re-running the fetch**, since a new node has no
     comments yet.
   - *lane names that will overflow a narrow column* — a word too long to wrap at the
     template's minimum lane width. Shorten the name.

   Rebuilding is free once the data is fetched, so iterate here.
5. **Pick the hubs.** The page's "most referenced in view" list ranks them. Cross that
   with what you know and write `start_here`: one entry per lane, one clause each, the
   reason a newcomer should open it first. An epic, a tracker issue, or the design issue
   a lane hangs off; rarely a PR.
6. **Publish** where the team already looks: a static branch (`gh-pages` with
   `keep_files`), a docs repo, an artifact. Commit the markdown inventory beside it,
   and commit the config so the next refresh is a two-command job.

**Do not open a browser as a routine step.** `test_template.py` covers the render in CI
— header collision at seven lanes, lane fit, click-to-pin, phone width, dark scheme — and
the template does not change per run, so a per-run browser session re-tests the tool
rather than the data. Open one when the build report flags something, when you edit
`template.html`, or when a map looks wrong in a way the report does not explain. If you
do drive it: the chart labels are `pointer-events: none`, so dispatch a click on `.node`
rather than using a text locator.

## What the page offers

- Two toolbar rows: lanes on the first (chips with counts; hovering a lane header in
  the chart isolates that lane, and only the chips hide one), everything else on the
  second: state, people, start-here, table view, and a panel toggle that hands the
  lanes the full width when seven of them need it.
- Every filter group reads the same way: click toggles a chip, shift-click keeps only
  that chip, and the reset chip at the head of the group ("all", or "everyone" for
  people) clears the restriction.
- **People** are the ten contributors who authored or engaged on the most items;
  the configured `user` is preselected, several can be on at once, and none selected
  means everyone.
- `data.json` and `inventory.md` are written beside the page on every build and linked
  from the header, so an agent can read the map without parsing HTML. Publish all
  three files together.
- The "as of" stamp is to the minute in UTC.

Not built yet, worth doing when the map is refreshed regularly: a "changed since the
previous build" view. Cheap shape: nodes absent from the previous `data.json` get a
marker and a list in the inventory. Fuller shape: a pr-watch-style baseline that also
reports state changes and new references.

## Reading the result

- **Square = PR, circle = issue. Filled = merged or closed, hollow = open.** A slash
  through a square is a PR closed without merging.
- Edges are undirected references; a thick highlight on hover is the neighborhood.
- **"Most referenced in view"** is the honest hub list. The gap between it and your
  `start_here` is usually informative: a PR with 20 references is where the argument
  happened, even if the tracker is the place to start.
- The people filter starts on the configured user. Clearing it shows the items other
  people pulled in through `include` or the relevance regex, useful for seeing what a
  lane looks like from the maintainer's side; picking one other person shows their
  slice of the theme.

## Config reference

```json
{
  "title": "...", "subtitle": "...", "user": "login",
  "repos": ["owner/name", "owner/other"], "primary_repo": "owner/name",
  "relevance": "regex", "min_score": 3, "body_cap": 6,
  "data_dir": "data", "out_html": "path.html", "out_md": "path.md",
  "notes_dir": "optional dir of *.md whose #N mentions attach as notes",
  "lanes": [{"key": "k", "name": "Lane name", "match": "regex", "repos": ["owner/name"]}],
  "default_lane": {"owner/name": "k"},
  "exclude": {"owner/name": [1, 2]}, "include": {"owner/name": [3]},
  "override": {"owner/name#4": "k"},
  "start_here": {"owner/name#5": "why a newcomer opens this first"}
}
```

Relative `data_dir`, `out_html`, `out_md` and `notes_dir` resolve against the config
file's directory, so the scripts can run from any cwd; a `--data` argument resolves
against the cwd. The page's `data.json` and `inventory.md` land beside `out_html`.

Node ids are `owner/name#N`. Labels drop the repo for `primary_repo` and keep the short
repo name for the others (`xsofy#147`). At most 8 lanes; the palette is the validated
categorical set from the dataviz skill.

`--preview` takes `--near N` for how many near misses to list (default 15).

`examples/letgo-perf.json` is the config that produced the first map (nooga/let-go +
nooga/xsofy, 175 nodes, 7 lanes, 2026-09-15); copy it and replace the judgment tables.
`examples/xsofy-shell.json` is the second run, a different theme on a game repo
(69 nodes, 4 lanes): note its `body_cap` of 2, word-bounded lane regexes, and the
specific-first lane order, all of which the first pass on that repo got wrong.

Copy an example next to where the map should live before running it. Its relative
paths resolve against the config's own directory, so running one in place writes
`data/` and the map into the skill directory.

## Gotchas

- `gh search --involves` caps at 1000 per query and returns issues and PRs separately;
  the fetch runs both and merges on number. A person with more than 1000 involvements
  in one repo needs the search split by date.
- `gh api --paginate --jq` on comments emits one JSON array per page back to back; the
  fetcher splits them. Do not `jq -s` the output expecting a single document.
- `gh pr view` returns `closedAt` as `0001-01-01T00:00:00Z` for open PRs. The builder
  blanks it; anything else reading that field must too.
- Edges are only as good as the reference discipline in the repo. A team that writes
  "see the earlier PR" instead of `#123` produces an under-connected map, and the fix is
  in the repo's habits, not the tool.
- The relevance filter is on title, labels, and body of the item itself, not comments,
  so a long discussion that became perf-relevant halfway through needs `include`.
- An `include` number that 404s is dropped and reported; any other failure fetching it
  aborts the run, so a transient error never yields a map silently short a node.
