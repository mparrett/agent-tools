---
name: pr-watch
description: >-
  Report only what MOVED on open PRs since the last check — approval flips,
  new/edited/removed comments (conversation and inline review), body edits, new
  reviews, and PRs that opened or closed.
  Use when the user asks "what changed on my PRs", "any new approvals or comments",
  "catch me up on my PRs", "what moved since I last looked", or invokes /pr-watch.
  This is the stateful delta sibling of pr-dashboard: reach for pr-dashboard when
  the user wants the whole picture (lanes, stack DAG, review queue); reach for THIS
  when they want just the movement. Works author-scoped (my PRs) or repo-wide
  (--all, every open PR), across one or many repos.
---

# PR watch — what changed since last time

A stateful "what moved on my PRs" report. It caches each PR's review decision, body,
and comment/review state under `$XDG_STATE_HOME`, then on the next run diffs live state
against that baseline and prints **only the delta** — never the whole board. The board
(lanes, DAG, ranked queue) is the `pr-dashboard` skill's job; this one answers "tell me
what moved."

The engine is `watch.py`, which lives in the **sibling `pr-dashboard` skill** and is
shared between the two skills — there is no separate copy here.

## Run it

Resolve the shared script, then invoke it and relay the output. `watch.py` is executable
(a `uv run --script` shebang pulls its own deps), so call it directly:

```bash
# Locate the shared script in the sibling skill dir.
# (If this skill's injected "Base directory" is known, use "<base>/../pr-dashboard/watch.py".)
WATCH="$(dirname "$(readlink -f ~/.claude/skills/pr-watch)")/pr-dashboard/watch.py"

"$WATCH"                          # current repo, at whatever scope it's watched
"$WATCH" owner/a owner/b          # several repos, each at its own watched scope
"$WATCH" --all owner/repo         # every open PR (maintainer watch)
"$WATCH" --author someone owner/repo   # a specific author's PRs
"$WATCH" --reset owner/repo       # forget the baseline and re-seed
```

Flags: `--all` (maintainer scope) xor `--author LOGIN`; with neither, each repo keeps
whatever scope its baseline was seeded under, falling back to `@me` when there's no
baseline yet. `--reset`
(drop the named repos' baseline, then re-seed); `--ttl-days N` (prune baselines older
than N days, default 14); `--max-repos N` (cap cached baselines, default 200).

**Just run it and relay the output verbatim** — it's a finished terminal report, not
facts to reshape into a dashboard. If `gh` needs auth or a repo 404s, say so and stop;
don't invent movement.

## Behavior notes

- **First run on a repo seeds a baseline and reports nothing** ("no before"). The delta
  shows from the *second* run onward, then re-seeds. So catching up needs two runs over
  time, not two back-to-back — tell the user that on a first/seed run.
- **Scope is sticky, and only an explicit flag changes it.** A flag-less run adopts the
  scope each repo's baseline was seeded under, so a repo you watch with `--all` stays
  maintainer-scoped when you later just run `watch.py`. This matters because the scope
  change below is destructive — without stickiness, the bare command a handoff note tells
  you to run would quietly narrow an `--all` watch to `@me` and eat the pending delta.
- **An explicit scope change re-baselines.** Passing `--all` or `--author LOGIN` for a repo
  baselined at the other scope filters a different PR set, so watch discards the old
  baseline and re-seeds rather than reporting out-of-scope PRs as "closed" and in-scope
  ones as "new." Expect a "scope changed … re-baselining" line and no delta that run.
  `--reset` re-seeds under the watched scope, so it forgets history without switching you.
- **Comment detection is counts + hash, not text.** `gh` exposes no `updatedAt` on
  comments, so an edit surfaces as a body-hash change keyed by the comment's stable id;
  the text itself is never cached. It can say *"1 edited"* but not show what changed — if
  the user wants the content, pull that comment with `gh` separately.
- **Inline review comments get their own line.** They are a third collection —
  `comments` is the issue conversation, `reviews` is the summary body on an
  approve or a request-changes, and a comment anchored to a file and a line is in
  neither, which is why `gh pr list --json` cannot see them. A `reviewThreads`
  query fetches them, so a reviewer leaving four inline comments reports as
  `review comments: +4 new`, separately from the review that carried them.
  Edits and deletions are detected the same counts-and-hash way as the
  conversation thread. What it still cannot show is which files, the text, or a
  thread being resolved — for one PR's threads use pr-shepherd's
  reviews+reviewThreads query.
- **"Not measured" is not "none".** If the inline query fails or a PR has more
  threads than one page holds, that PR is reported as unmeasured and its inline
  comments are left out of the delta rather than counted as zero — counting zero
  would report every existing comment as removed. You will see
  `inline review comments not measured for N PR(s)`; the rest of the report is
  unaffected.
- **Reports:** approval flips (e.g. `REVIEW_REQUIRED → APPROVED`), body edits, comment
  add/edit/remove counts, inline review-comment add/edit/remove counts, new or re-stated
  reviews, newly-tracked PRs, and PRs that closed.
  A tracked PR that disappears is reported as "closed / no longer open" — the engine only
  lists open PRs, so it cannot tell merged from manually closed, and a PR opened and
  merged between polls is never seen. Check `gh pr view <n>` when the distinction matters.
- **Durable, self-pruning cache** at `$XDG_STATE_HOME/pr-dashboard/watch/<owner>__<repo>.json`
  (shared with pr-dashboard's watch mode — same baseline, so the two don't double-seed).
  Baselines untouched past `--ttl-days` age out; `--max-repos` is the backstop.

## After relaying

- On a **delta**, offer to drill into any moved PR (e.g. `gh pr view <n>`, or pull an
  edited comment's text) or to build the full board with `pr-dashboard`.
- For **hands-off polling**, suggest wrapping this in `/loop` (e.g. `/loop 30m /pr-watch`)
  rather than re-running by hand — but only if the user wants recurring checks.
