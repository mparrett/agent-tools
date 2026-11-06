---
name: pr-dashboard
description: Build a heads-up dashboard of open pull requests — grouped by workstream,
  with stack/DAG relationships and a prioritized "what to review next and why" queue.
  Use when the user asks to "show me a dag of the PRs", "make sense of all the open
  PRs/issues", "what should I review", "give me an overview of what's in flight",
  "PR dashboard/heads-up/status board", or is drowning in stacked PRs across one or
  more repos and wants the picture. Works author-scoped (my PRs) or maintainer-scoped
  (everything awaiting my review). Produces a self-contained HTML file that renders as
  a claude.ai Artifact or opens locally. For just the delta since last check ("what
  changed on my PRs", "any new approvals or comments") rather than the whole board, use
  the sibling pr-watch skill, which shares this skill's watch.py and baseline cache.
---

# PR dashboard

Turn a pile of open PRs (often stacked, with non-obvious dependencies) into one glanceable
picture: workstream lanes, stack DAG, and a ranked review queue with a reason per item.

The design separates **facts from judgment**:

- `collect.sh` pulls the facts from `gh` — numbers, branches, **stack links** (base→head),
  CI state, review decision, diff size, **issue↔PR links**, and (opt-in) recently-merged
  PRs. Never hand-transcribe these.
- You do the **judgment** — group PRs into workstreams, rank the review queue, write the "why".
- `template.html` is a fixed, data-driven renderer. You only swap its JSON data island;
  never restyle it per run. That's what makes successive dashboards look consistent.

## Watch mode — what changed since last check

The board above is a **stateless snapshot**. When the user instead asks "what changed on
my PRs since I last looked", "any new approvals/comments", or wants to be caught up on
movement (not the whole picture), use `watch.py` — the stateful sibling. It caches each
PR's review decision, body, and comment/review state under `$XDG_STATE_HOME/pr-dashboard/watch/`
(durable, not `/tmp`), then on the next run diffs live state against that baseline and prints
**only what moved**.

```bash
SKILL="$(dirname "$(readlink -f ~/.claude/skills/pr-dashboard)")/pr-dashboard"  # or wherever this skill lives
"$SKILL/watch.py"                         # current repo, at whatever scope it's watched
"$SKILL/watch.py" owner/a owner/b         # several repos, each at its own watched scope
"$SKILL/watch.py" --all owner/repo        # every open PR (maintainer watch)
"$SKILL/watch.py" --reset owner/repo      # forget the baseline and re-seed
```

Just run it and relay the output — it's a finished terminal report, not facts to reshape.
Behavior worth knowing:

- **First run on a repo records a baseline and reports nothing** (there's no "before").
  The *second* run onward shows the delta, then re-seeds. So catching up needs two runs
  over time, not two back-to-back.
- **Scope is sticky.** With neither `--all` nor `--author`, each repo keeps the scope its
  baseline was seeded under (`@me` when there's no baseline yet); only an explicit flag
  changes it, and that change re-baselines. So a bare `watch.py` is always safe to run —
  it can't quietly narrow an `--all` watch and discard the pending delta.
- **Reports**: approval flips (`REVIEW_REQUIRED → APPROVED`), body edits, comment
  add/edit/remove counts, inline review-comment add/edit/remove counts, new or re-stated
  reviews, newly-tracked PRs, and PRs that closed.
- **All three comment collections are covered.** A PR has three, and `gh pr list --json`
  reaches only two: `comments` is the issue conversation and `reviews` is the summary body
  on an approve/request-changes. File-and-line review comments — the form most review
  feedback takes — live in neither, so they come from one batched GraphQL call per repo.
  Without it a PR under active review reads as quiet. Inline comments are reported on their
  own `review comments:` line, separate from the conversation's `comments:` line.
- **An unmeasured collection reports nothing rather than guessing.** If the GraphQL call
  fails, or a PR has more threads/comments than one page, that PR's inline comments are
  recorded as *not measured* and skipped — never as an empty set. The same applies to a
  baseline written before inline support existed. A partial measurement diffed against a
  full one invents a wave of additions or removals, and always on the PRs under the
  heaviest review.
- **Comment detection is "counts + hash".** `gh` exposes no `updatedAt` on comments, so an
  edit is a body-hash change keyed by the comment's stable id; the text itself is never
  cached. That means it can say *"1 edited"* but not show the edited text — pull the comment
  with `gh` if the user wants the content.
- **Self-pruning cache**: baselines untouched for `--ttl-days` (default 14) age out; a
  `--max-repos` cap (default 200) is the backstop. Nothing accumulates forever.

This mode is independent of the HTML board — different output, different question. Use the
board for "give me the whole picture"; use watch for "tell me what moved."

## Steps

### 1. Collect the facts

`collect.sh` and `template.html` live alongside this `SKILL.md`. Set `SKILL` to that
directory (when installed, it's the path the skill was loaded from):

```bash
SKILL="$(dirname "$(readlink -f ~/.claude/skills/pr-dashboard)")/pr-dashboard"  # or wherever this skill lives
# author view (my work) — default:
"$SKILL/collect.sh" > /tmp/pr-facts.json
# maintainer view (everything awaiting my review):
"$SKILL/collect.sh" --all > /tmp/pr-facts.json
# add a "what landed" recap (merged PRs on/after a date):
"$SKILL/collect.sh" --all --merged-since today > /tmp/pr-facts.json
# other repos / user:
"$SKILL/collect.sh" --author someone owner/repoA owner/repoB > /tmp/pr-facts.json
# other people's PRs where I owe the next move:
"$SKILL/collect.sh" --waiting-on @me owner/repo > /tmp/pr-facts.json
```

Defaults to **the current directory's repo** and `--author @me`; pass `owner/repo` args
for other repos, `--all` for the maintainer view. `--merged-since <date>` (YYYY-MM-DD, or
`today`/`yesterday`/`Nd` like `7d`) adds a `mergedPrs` list per repo for the recap lane.
`--waiting-on <login|@me>` looks at everyone's open PRs and keeps the ones where that login
owes the next move. That covers a pending review request, an approval the head has since
moved past, changes requested that the author has answered, and an open thread the login
is in where the author spoke last. Each kept PR carries `waitingOn`, a list of those
reasons. To see what is waiting on nooga, pass `--waiting-on nooga`.
Read the JSON. Key per-PR fields:

- `stackedOn` — the PR number this one is based on, or `null` if it branches off the
  default branch. This is your DAG. A chain (`#145 ← #146 ← #149`) is one stack; bottom
  merges first.
- `base` — the branch this PR merges **into** (normalized from `baseRefName`; that raw key
  is not in the output). Always surface it on open-PR rows: it's the edge that makes a stack
  legible (see "Show the base branch" below). Note: `mergedPrs` do not carry `base`.
- `draft` — the PR is a GitHub draft. A **stronger not-ready gate than CI or approval**:
  its author has not offered it for review, so it is never a queue candidate no matter how
  green it is. Orthogonal to `status`, not a value of it — a draft can also be parked,
  conflicting, or approved. Carry it through to the model as its own boolean.
- `review` — `APPROVED` / `REVIEW_REQUIRED` / `CHANGES_REQUESTED` / `""` (none requested).
- `ci` — `pass` / `fail` / `pending` / `none`.
- `mergeable` — `MERGEABLE` / `CONFLICTING` / `UNKNOWN` (UNKNOWN = GitHub still computing).
- `closes` — issue numbers this PR would close (GitHub's `closingIssuesReferences` **∪**
  `close/fix/resolve #N` keywords scraped from the body). Drives the coverage view.
- `lastTouch` — `{kind, by, at}` or `null`: the most recent comment, review (state change),
  commit, or force-push on an open PR, from one extra batched GraphQL call (bot actors —
  `__typename "Bot"`, e.g. a CI comment bot — are excluded so they can't mask the last real
  human activity). `kind` is `comment` / `commit` / `force-push` / `review:approved` /
  `review:changes_requested` / `review:commented` / `review:dismissed`. Not present on
  `mergedPrs`. Carry it through to the model verbatim — the template computes the "how long
  ago" phrasing and the redundancy suppression itself (see the data model notes below);
  don't pre-render either.
- `owedBy` / `owedWhy` — whose turn it is, derived from `review` **and** `lastTouch`.
  `owedBy` is `author` / `reviewer` / `maintainer` / `null` (drafts). `owedWhy` gives the
  evidence: `changes-requested`, `author-responded`, `peer-approved`, `needs-rebase`,
  `ready-to-merge`, `unreviewed`, or `unknown`.

  **Sort the review queue on this, not on `review`.** GitHub holds `reviewDecision` at
  `CHANGES_REQUESTED` until the *same* reviewer submits a new review — the author pushing a
  fix, replying, or a second reviewer approving all leave it set — so on a repo whose
  reviewers don't habitually re-review it decays into "someone once objected", a fact about
  the past rather than about whose turn it is. `author-responded` and `peer-approved` are
  exactly the cases where the badge still reads `CHANGES_REQUESTED` but the ball is back
  with a reviewer. `author-responded` requires the last touch to be *by the author* — a
  reviewer's own follow-up comment leaves the PR owed by the author. `unknown` means
  `lastTouch` was unavailable, so no verdict was guessed.
- `approvals` — `{atHead: [logins], stale: [logins]}` or `null`: each reviewer whose latest
  approve/request-changes review is an approval, split by whether it was submitted on the
  current head commit. `review` stays `APPROVED` after the author pushes again, so an
  approval in `stale` covers code that reviewer hasn't seen. A server-side "Update branch"
  moves the head too.
- `threads` — `{unresolved, outdated, replied}` or `null`: unresolved inline review threads,
  with `outdated` the subset whose anchor lines have since changed. `replied` counts the
  open threads where the PR author commented last, so the reviewer is next. The rest are
  still waiting on the author. An `APPROVED` PR can still have open threads.
- `reviewers` — `{requested, reviewed}` or `null`: review requests still pending, plus
  everyone else who has submitted a review. GitHub drops a request once that reviewer
  reviews, so neither list alone says who is on the PR.
- `comments` — integer or `null`: conversation comments plus all inline review comments.

  `reviewers.lastRequestedAt` is the newest review request or re-request.
- `changesRequested` — logins whose latest opinionated review requests changes, or `null`.
- `threads.resolvedUnanswered` — resolved threads where someone else commented and the
  PR author never did: a comment closed without a reply.
- `mergeState` — GitHub's `mergeStateStatus` (`CLEAN`, `BEHIND`, `BLOCKED`, `DIRTY`, …).
- `mergeReady` — `{verdict, checks[]}`, the answer to "can I even think about merging
  this". Each check is `{check, level, state, note}`. `level` is `block` or `warn`, and
  `state` is `pass`, `fail`, `warn` or `unknown`. Blocking checks: not a draft, CI green,
  an approval on the current head, no outstanding changes requested, no conflicts, no
  unresolved threads, and a base that is the default branch rather than an unmerged
  parent. Warnings: threads resolved without a reply, pending review requests, branch
  behind the base. `verdict` is `blocked` on any failed blocking check, `unknown` when a
  blocking input wasn't measured, and otherwise `ready`. Conflicts are reported as GitHub
  reports them. A generated file that a local merge driver would resolve still reads as a
  conflict.

  All of these come from the same batched GraphQL call as `lastTouch`. `null` means *not measured*
  (call failed, or more than 100 reviews/threads), never zero — don't present it as "none".
- `adds`/`dels`, `labels`, `bodyExcerpt`.

Per repo you also get:

- `openIssues[]` — each with `linkedPrs`: the open/merged PRs whose `closes` include it.
  Empty `linkedPrs` ⇒ **unclaimed**; an open PR ⇒ **in-flight**. (Repos that neither link
  via the sidebar nor write `Closes #N` will show everything unclaimed — that's honest,
  not a bug.)
- `closedIssues[]` — present only with `--merged-since`: issues closed on/after that date,
  same shape plus `closedAt`. A closed issue whose `linkedPrs` contains a merged PR is
  **just landed**. (It has to work this way: a PR that genuinely closes an issue closes it
  at merge time, so the landed pair never appears in `openIssues`.)
- `mergedPrs[]` — present only with `--merged-since`; each has `mergedAt` and `closes`.

If `gh` needs auth or a repo 404s, say so and stop — don't invent data.

### 2. Group into workstreams (judgment)

A **lane** = one workstream. Assign each PR using, in order of signal strength:

1. **Stack chains** (`stackedOn`) — a chain is always one lane, ordered bottom→top.
2. **Branch prefix** — `perf/*`, `mobile/*`, `ui/*`, `console/*` usually cluster.
3. **Labels / linked issue / epic** — same issue or epic ⇒ same lane.
4. Leftover unrelated singletons → one **"Standalone"** grid lane (`kind:"grid"`, `span:true`).

Mark the lane the user is actively driving with `accent:"active"`; a lane parked on a
decision with `accent:"defer"` and its items `status:"deferred"`.

### 3. Build the review queue (judgment — this is the payoff)

Rank what to review/act on next.

**Drafts are excluded first, before any of the rules below.** `draft:true` means the author
hasn't offered the PR for review, so it is never a queue candidate — green CI, an approval,
and a clean merge don't override it. A draft that otherwise looks finished isn't a review,
it's an author decision: put it in the blocked/deferred bucket with the action framed as
*"undraft"*, not *"review"*. (This is the one gate that's a fact rather than a judgment
call, which is exactly why it's easy to skip past.)

Rank what's left on `owedBy` / `owedWhy`, not on `review` — those are the fields that know
whose turn it is. Highest first:

1. **`owedBy:"maintainer"`** (`ready-to-merge`), bottom-of-stack first → *merge now* — unblocks
   its stack.
2. **`owedBy:"reviewer"` with `owedWhy:"peer-approved"` or `"author-responded"`, `ci:pass`** →
   *review now*. These still carry a `CHANGES_REQUESTED` badge, so a queue keyed on `review`
   buries them — they are the cases the field exists to surface.
3. **`owedBy:"reviewer"` with `owedWhy:"unreviewed"`, `ci:pass`**, bottom-of-stack first →
   *review first* — gates everything above it.
4. **Standalone, small (`adds` low), `ci:pass`** → *quick win*.
5. **Large or foundational standalone** → *review, budget time*.
6. **`owedBy:"author"`** (`changes-requested`, `needs-rebase`) **or `ci:fail`** → *needs author
   action* — surface as `blocked`, not "review".
7. **Deferred / parked / draft** → bottom, or omit from the queue.

Two review signals adjust that order. An approved PR whose `approvals.atHead` is empty, with
every approval in `stale`, is not *merge now*: it needs a re-review of the latest push, so
treat it as rule 2. Open `threads.unresolved` on a PR otherwise ready to merge are owed by the
author, unless `threads.replied` covers them all, in which case the reviewer is next. Name
the signal in the `why`.

`owedWhy:"unknown"` means an input was missing — `lastTouch` unavailable, or `mergeable` still
`UNKNOWN` while GitHub recomputes the merge. Rank those on their remaining signals and say the
turn is unverified; don't promote one to *merge now* on the strength of an approval alone.

Give every queue row a one-line `why` naming the actual signal ("bottom of the perf stack,
CI green — clears #146/#149 behind it"), not a restatement of the status. Keep the queue
to the ~5–8 that matter; the lanes already show everything.

### 3b. Optional lanes — issue coverage and "what landed" (when the data warrants)

Two supplementary lanes, added only when they'd say something:

- **Issue coverage** (`kind:"issues"`) — turn `openIssues` (+ `closedIssues` when present)
  into a coverage map. Each row is an issue → the PR covering it (in-flight or merged) or
  **unclaimed**. Open issues supply the unclaimed/in-flight rows; **just landed** rows come
  from `closedIssues` entries whose `linkedPrs` has a merged PR (skip closed issues with no
  linked PR — closed by hand isn't coverage news). Use the lane when a repo tracks real
  issues; skip it when there are none, or when every issue is unclaimed and that adds
  nothing. Pick the most relevant PR per row: an open one over a merged one.
- **Merged recap** (a `grid` lane of `status:"merged"` cards) — when you ran `--merged-since`,
  group `mergedPrs` by workstream into "merged today/this week" lanes. This is the answer to
  "what shipped." If a merged PR has `closes`, mention the issue it resolved.

Neither is required; a normal review board has just the stack/standalone lanes and the queue.

### 4. Render — local file, always

**Local-first is the default; don't publish to a claude.ai Artifact unless the user
explicitly asks.** The output is a single self-contained HTML file with no dependencies.
Build the JSON model (schema below), then hand it and the facts to `render.py`:

```bash
"$SKILL/render.py" model.json /tmp/pr-facts.json      # writes, saves the snapshot, opens
```

`render.py` does four things:

- **Writes the board to a stable path**, `~/.local/state/pr-dashboard/boards/<scope>.html`
  (`$XDG_STATE_HOME`). `<scope>` is the author plus the repos, e.g.
  `mparrett__nooga-let-go`, so boards for other authors or repo sets don't overwrite this
  one. The path has to stay put, because the board's "reviewed" ticks live in the browser's
  localStorage, which is keyed by file location.
- **Fills in `changes`**, the "since last scan" strip, by diffing these facts against
  the previous run's snapshot (`<scope>.facts.json` next to the board). It then saves the
  new snapshot. Re-rendering identical facts, say after rewording the queue, keeps the
  earlier diff rather than reporting "nothing moved". This snapshot belongs to the board;
  `watch.py` keeps its own baseline, so neither tool resets the other.
- **Stamps `generatedAt`** from the real clock, unless the model already sets it. The page
  shows it as a badge, so a board left open across a break still shows its age.
- **Escapes the island.** A PR title or body containing `</script>` can't break the page.

Pass `--out PATH` for a one-off file elsewhere (the snapshot is still saved), and
`--no-open` to skip opening it. Don't write `changes` into the model yourself; render.py
owns it.

The template ships a **light / dark / system** switch (top-right, persisted in
`localStorage`); it defaults to `system`. Nothing to configure per run.

**Artifact publishing is opt-in only.** If the user explicitly asks to publish/share, call
the `Artifact` tool with `file_path`=the filled HTML, `icon` "chart" (load `artifact-design`
first). The template already satisfies the Artifact CSP. Default path is local — don't offer
or auto-publish.

### 5. Validate before declaring done

Confirm it renders before saying it's done: `open` it, or headless-check with Playwright
(assert `.qrow` count > 0 and no `pageerror`; lanes are on their own tabs, so open
`<board>#tab=work` to check `.lane`/`.slice`). A JSON typo in the island
shows as "Bad data island: …" text; fix and re-render.

## Data model (the JSON island)

The `subtitle` says what the board **is**, not what it isn't. Counts, grouping, and what
the queue is ranked on — no contrastive clause defending the choice ("ranked on whose turn
it is, *not the review badge*"). The `owedBy` field's rationale belongs in this file, not on
every board; on a phone that clause costs a line of a header that already has too many.
Two lines is the ceiling.

```jsonc
{
  "title": "Open PRs — <who>, <repos>",
  "subtitle": "37 open · grouped by stack · queue ranked by next turn",
  "generatedAt": "2026-07-23 14:32:07 PDT",  // render.py stamps it from the clock; never guess one
  "nudgeAfterHours": 48,                       // optional: when "no approval" turns amber
  // "changes": { ... }                        // written by render.py, never by hand
  "legend": true,                       // false to hide the legend row
  "queue": [
    { "rank": 1, "ref": "owner/repo#45", "url": "https://github.com/owner/repo/pull/45",
      "author": "someone",               // straight from collect.sh's per-PR `author` — copy, don't drop
      "lastTouch": { "kind": "review:changes_requested", "by": "someone-else", "at": "2026-07-23T13:00:00Z" },
      "action": "review",               // pill: review|approved|active|blocked|merged|deferred
      "draft": false,                   // straight from collect.sh — adds a separate outlined
                                        // "draft" pill *next to* the action pill (see step 3:
                                        // a draft shouldn't be in the queue in the first place)
      "why": "bottom of the perf stack, CI green — clears #46/#49 behind it" }
  ],
  "sections": [
    {
      "repo": "owner/repo",             // rail label
      "base": "main 42e5e20",           // default branch + short sha (from collect.sh baseSha)
      "heading": null,                  // optional uppercase divider above the rail
      "lanes": [
        {
          "title": "Perf stack",
          "meta": "stacked · off main", // small right-aligned note
          "accent": "active",           // active | defer | block | normal (default)
          "kind": "stack",              // "stack" (vertical slices) | "grid" (cards)
          "span": false,                // true = full-width lane
          "items": [
            { "pr": "#49", "branch": "perf/sync-output", "desc": "synchronized-output frames",
              "sub": "↳ merges into perf/sgr-elision (#45)", "status": "review", "draft": false,
              "author": "someone",
              "lastTouch": { "kind": "comment", "by": "someone-else", "at": "2026-07-23T13:00:00Z" },
              "approvals": { "atHead": [], "stale": ["someone-else"] },   // from collect.sh, verbatim
              "threads": { "unresolved": 2, "outdated": 1, "replied": 1 }, // from collect.sh, verbatim
              "reviewers": { "requested": ["a"], "reviewed": ["b"] },     // queue only
              "comments": 7,                                               // queue only
              "url": "https://github.com/owner/repo/pull/49" },
            { "pr": "#45", "branch": "perf/sgr-elision", "desc": "elide redundant SGR",
              "sub": "↳ merges into main", "status": "review", "author": "someone-else" }
          ],
          "note": "Touches only the renderer — disjoint from the other lanes."   // dashed footer
        },

        {                               // --- issue coverage lane ---
          "title": "Issue coverage", "kind": "issues", "span": true,
          "items": [
            { "issue": "#27", "title": "flaky retry logic", "pr": "#60",
              "prStatus": "review", "prState": "open",     // -> shows a "review" pill + "→ #60"
              "prDraft": false,                            // true -> also shows a "draft" pill:
                                                           // covered, but not landing soon
              "url": "https://github.com/owner/repo/issues/27" },
            { "issue": "#25", "title": "session roaming", "pr": "#52", "prState": "merged",
              "url": "https://github.com/owner/repo/issues/25" },   // -> "merged" pill + "→ #52"
            { "issue": "#6", "title": "ergonomics",
              "url": "https://github.com/owner/repo/issues/6" }     // no pr -> grey "unclaimed"
          ]
        },

        {                               // --- merged recap lane (needs --merged-since) ---
          "title": "Merged today", "meta": "off main", "kind": "grid", "span": true,
          "items": [
            { "pr": "#51", "branch": "feat/tests", "desc": "test suite (+368)", "status": "merged", "author": "someone",
              "url": "https://github.com/owner/repo/pull/51" }
          ]
        }
      ]
    }
  ],
  "footnotes": [ "Nothing blocks on conflicts.", "Only #45 is on the critical path." ]
}
```

`status` / `action` values map to pills: `active` (blue), `approved` (green), `review`
(amber), `blocked` (red — CI-fail/conflicting), `merged` (purple), `deferred` (grey).
`draft` is a **separate boolean**, not one of those values: it renders as an outlined pill
to the left of the status pill, so a PR can be both draft *and* parked/blocked/approved.
Order stack `items` top-of-stack first so the visual reads like a stack (newest on top),
but say "bottom merges first" in a footnote.

An **issues** lane (`kind:"issues"`) renders coverage rows: each item is `{issue, title, url}`
plus, when covered, `{pr, prState:"open"|"merged", prStatus, prDraft}` — no `pr` renders a
grey `unclaimed` pill. A draft still counts as coverage (the work exists), but set `prDraft`
so the row doesn't read as landing soon. A **merged recap** is just a `grid`/`stack` lane whose items carry
`status:"merged"`.

## Notes

- **Carry `author` through.** `collect.sh` already emits it per PR (and per merged PR) —
  copy it onto every queue row and lane item verbatim, never omit it to save space. In the
  **queue**, the template renders it as its own compact column, left of the action pill
  (one line per row — don't fold it into `why`). In **stack/grid** lanes it renders under
  the description/branch — *unless* every item in that lane shares one author, in which
  case the renderer automatically hoists it to the lane header once and skips the per-item
  badge, so don't hand-write the author into the lane `title` yourself (e.g. don't write
  `"lgbgen / IR lowering (nnunley)"` — write `"lgbgen / IR lowering"` and let a uniform
  `author` across its items do the rest; the hoist only fires at 2+ items, so a lane with a
  single item always shows its badge inline). Deliberately uncolored, unlike the status
  pill, so attribution reads as identity, not another status signal.
- **Carry `draft` through.** `collect.sh` emits it per PR; copy it onto every queue row and
  lane item (and as `prDraft` on issue-coverage rows). Don't hand-encode it instead — marking
  a draft `deferred` or appending "— DRAFT" to `desc` reads acceptably for one run but throws
  away the distinction the pill exists to keep: `deferred` is *your* call about priority,
  `draft` is the PR's own state, and the two disagree often enough to matter (a draft that's
  parked wants a different reaction than finished work that never got undrafted).
- **Carry `lastTouch` through, don't pre-render it.** Pass the raw `{kind, by, at}` object
  onto every item — the template turns `at` into "3h ago" against the real clock at *page
  load* (unlike `generatedAt`, which is stamped once at render time), so opening the
  dashboard well after generation still shows a true elapsed time; it's a load-time
  snapshot, not a live ticker, so the text won't tick over while the tab sits open — and
  turns `kind`/`by` into a phrase like
  `"nnunley reviewed 3h ago"`. It also suppresses the single least-informative case itself —
  `kind:"commit"` by the PR's own `author` (a routine self-push, the default state of any
  active PR) — so don't pre-filter that case in the model either; just pass the field through.
- **Carry `approvals` and `threads` through, including `null`.** Copy both onto every queue
  row and lane item. The template renders a chip for every state, zero included: green
  "approved on head", amber "approval predates head" / threads with any
  "unanswered", blue when every open thread is "replied", amber "no approval on head" (a merge needs one), grey "no open threads", and
  grey "?" for `null`. Open-thread chips link to the item's `url`. Only an absent key
  renders nothing, so an item missing the fields silently loses both chips.
- **Queue rows also carry `reviewers`, `comments` and `mergeReady`.** They fill the queue's
  Reviewers ("1/2 reviewed"), Comments and Merge columns; lane items don't render them. A
  zero reads "none" in every queue column. The Merge chip shows "✓ ready" or "✗" with the
  first failing check, and its hover text lists every check that isn't a pass. Base the
  row's `action` on `mergeReady`: only a `ready` PR is *merge now*.
- **`nudgeAfterHours`** (top level, default 48) sets when "no approval" turns from grey to
  amber: a review requested more recently than that isn't late yet.
- **Show the base branch.** Every **open-PR** row should say where it merges *into*, not just
  its head branch — the `base` field is what makes a stack readable at a glance. In **stack**
  lanes put it in `sub` as `↳ merges into <base> (#parent)` (the stack renderer shows `sub`;
  the **grid** renderer does not, so for grid/standalone cards fold it into the `branch` label
  as `head → base`, usually `→ main`). Lane `meta` tags like `bottom → main` reinforce it.
  This applies to open PRs only — `mergedPrs` have no `base` (they've landed on the default
  branch), so merged-recap cards keep a bare head-branch label.
- **The board is tabbed.** **Now** holds the since-last-scan strip, the queue and the
  footnotes. **Workstreams** holds stack and grid lanes, **Issues** the `kind:"issues"`
  lanes, and **Shipped** any lane whose items are all `status:"merged"`. A tab with nothing
  in it isn't drawn. The tab comes from a `#tab=` link if there is one, otherwise from the
  viewer's last choice, otherwise Now.
- **Each queue row has a "looked at it" tick.** It is stored per viewer in localStorage,
  keyed by PR url. A ticked row dims. If the PR's `lastTouch` is newer than the tick, the
  row returns to full strength marked "changed since you looked". This needs nothing in
  the model beyond `url` and `lastTouch`.
- **Carry `closes` and `waitingOn` through.** Copy `closes` (issue numbers) onto queue
  rows and lane items; the board links each one as "closes #N". On a `--waiting-on` board,
  copy `waitingOn` onto queue rows, where each reason leads the row as a "your move" chip.
- **Live data drifts.** The dashboard is a snapshot; re-run `collect.sh` each time. Don't
  reuse a prior model's PR numbers. The `generatedAt` badge is how a returning reader tells
  a fresh dashboard from one they left open; render.py stamps it.
- **Merged/closed PRs** won't appear in `--state open`. Use `--merged-since <date>` for the
  recap; for a single merged base referenced by a live stack, add a `status:"merged"` slice by hand.
- **One repo or ten** — pass any repos to `collect.sh`; add one `section` per repo.
- Keep the whole thing to a single screen where possible; the queue is the part people read.
