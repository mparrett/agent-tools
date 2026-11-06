---
name: pr-shepherd
description: >-
  Code review a pull request, answer questions about one, and run the GitHub PR
  lifecycle: review a PR,
  explain what reviewers said and what is outstanding, address/post/resolve
  review comments, shepherd stacked changes, monitor CI until green, and squash-merge.
  Use it for a code review — "review this PR", "review PR #N", "self-review before I
  push", "work through the review comments", "babysit/shepherd this PR", "watch CI and
  fix what breaks", "squash merge when green", /pr-shepherd — and equally for *reading*
  a PR rather than acting on it: "what did so-and-so say about X", "look at the comments
  on #N", "why is this PR blocked", "what is still outstanding".
  Reading needs the same paginated reviews+reviewThreads query;
  `gh pr view --comments` omits inline threads entirely. Built-in /code-review is a
  separate bug pass: fine at medium effort or below, high and up only when
  explicitly requested by the operator. Needs only `git` and `gh`.
  Prefer pr-watch / pr-dashboard for a delta across many PRs / the full board.
---

# PR Shepherd

Distilled from OpenAI Codex's review rubric and GitHub skills (provenance at the
bottom). One skill, six modes. Identify the mode, load the listed reference, follow
the shared policies below in every mode.

| Mode | Trigger | Read first |
|---|---|---|
| Explain a PR | "what did X say", "why is this blocked", "what's outstanding" | this file, § Reading a PR's conversation |
| Review (others) | "review PR #N" | `references/review-rubric.md` |
| Review (self) | "review my diff / self-review before push" | `references/review-rubric.md` |
| Address comments | "work through the review feedback" | this file, § Addressing comments |
| Shepherd / monitor | "babysit this PR", "watch CI" | `references/ci-monitoring.md` |
| Squash merge | "merge when green" | this file, § Squash merging |

When updating a PR body in any mode, read `references/pr-body.md` first. The title is the
`prose-review` skill's job: `pr-body.md` has no title rules.

## Reading a PR's conversation

For questions *about* a PR rather than changes to it — what a reviewer said, why
it is blocked, what is still outstanding, whether anyone replied. No diff is
being judged, so the rubric does not apply, but the read has to be complete:
answering from a partial view is how a PR gets reported as handled while inline
feedback sits untouched.

Run the § Addressing comments query and obey its three rules — read `reviews`
*and* `reviewThreads`, page all four cursors (`reviews`, `comments` and
`reviewThreads`, plus the nested one inside a long thread), and do not filter out
`isOutdated`. Then answer from what it returns:

- **Who is waiting on whom** — compute it, do not read `reviewDecision`. That
  field stays set until the *same* reviewer submits again, so a pushed fix, a
  reply, or a second approval leave it unchanged. Compare the last blocking
  review's `submittedAt` against later commits and comments — which is why the
  query below fetches `commits`, top-level `comments` and a `createdAt` on every
  thread comment, not just reviews. Without those three there is nothing to
  compare against, and "has anyone replied?" is unanswerable when the author
  replied with a top-level comment.
- **What is outstanding** — unresolved threads (outdated included), plus any
  `COMMENTED`/`CHANGES_REQUESTED` review whose `body` is non-empty, which has no
  thread to resolve and is invisible to a threads-only read. **Reviews are a
  history, not a state.** GitHub keeps every review object, so the same reviewer's
  earlier `CHANGES_REQUESTED` still comes back after they approve. Per reviewer,
  take their latest review as their position, and read each body before counting
  it — a non-empty `COMMENTED` body is as often "LGTM, one thought" as an
  objection. Counting every historical body reports an approved PR as blocked.
- **Quote, do not paraphrase**, and attribute each point to its author. Cite file
  and line for an inline thread; a body-only review has neither, so cite its
  `url`. "The reviewer wants X" loses the distinction between a blocking
  objection and a passing remark.

If the question is what changed *since a previous check* across several PRs, that
is the pr-watch skill. Its baseline tracks reviews, so inline activity shows up
as the review that carried it, but nothing thread-specific — not which threads,
their text, an edit, or a resolution. Use it to learn a PR moved; use the query
here to learn what was said.

## Reviewing (others' PRs)

Read `references/review-rubric.md` before producing findings — it defines what
qualifies as a finding, priorities, and comment style. Workflow:

1. Pin what is being reviewed — never assume the current checkout is it. For a
   named PR (number or URL) run `scripts/review_target.py begin --pr <n>` (relative to
   this skill's directory; add `--repo owner/name` when the cwd remote is not the PR's
   repo). It records the base and head OIDs, fetches the pull ref into
   `refs/review/pr-<n>/head`, refuses if the fetched object is not the recorded head,
   and prints the exact `git diff <merge-base>..<head-oid>` to review. For a
   working-diff review run `begin --worktree`; the base is still the remote ref, since
   a stale local `main` behind `origin/main` silently widens the diff. If the review
   will run tests, `review_target.py tree --pr <n>` gives you a worktree at that pinned
   head to run them in.
2. Read the applicable repo instructions (`AGENTS.md` / `CLAUDE.md`), including any
   `## Review guidelines` section — repo-specific rules override the rubric.
3. Inspect the complete diff plus enough surrounding code to understand each changed
   path. Diff against `git merge-base <head> origin/<base>` — the changes that would
   actually merge, not the branch tip.
4. Identify concrete regressions introduced by the change. Do not stop at the first
   issue; continue through the whole diff.
5. Verify each candidate finding against tests and call sites before reporting it
   (§ Review invariants says what that verification must include).

Present findings ordered by severity, one entry per issue:

```
[P1] Imperative finding title — path/to/file.rs:line
```

followed by one short paragraph: the affected scenario and why the behavior is wrong.
If nothing qualifies, say `No findings.` — never invent a finding to fill the result.
Close with `Reviewed head: <oid>`, an overall correctness verdict (the rubric's
verdict mapping), what was run and what was not, and any material test gaps.

Reviews are read-only. Do not post comments, request changes, or apply fixes unless
explicitly asked; when asked to post, follow § GitHub write policy.

### Review invariants (required)

**Pin the object, then validate it again.** `scripts/review_target.py begin` records
the PR number, base ref/OID, and `headRefOid`, fetches the GitHub pull ref into a
dedicated local review ref, verifies it resolves to the recorded head OID, and records
the merge base; review `git diff <merge-base>..<head-oid>`, never a mutable checkout or
an unqualified branch name. Immediately before returning findings run
`scripts/review_target.py check --pr <n>`: unchanged prints the `Reviewed head: <oid>`
line for the result; a moved head (exit 3) means post nothing, review the delta it
prints, and revalidate every candidate; a moved merge base (exit 4) means start over.
Anything written to GitHub goes through `scripts/review_target.py post --pr <n> -- gh ...`,
which runs the same check first and refuses on a moved target. For a working-tree
review `begin --worktree` and `check --worktree` do the same with HEAD plus a hash of
the working diff; if they changed, re-review the changed paths.

**Use two passes.** Pass 1 inventories every changed path and states the intended
behavior, callers/consumers, tests, and compatibility surfaces for each logical change.
Pass 2 is adversarial and independent: trace boundary inputs, failure paths, state
transitions, permissions/security, persistence/wire compatibility, and affected call
sites. Do not draft final findings until both passes cover the complete diff.

**Validate candidates.** Keep a private candidate ledger with: changed line, triggering
scenario, affected code, expected behavior, observed/provable behavior, and disproof
attempt. Read the implementation, callers, and relevant tests. Run the narrowest
relevant executable check when feasible. If no useful test can be run, state why in the
final test-gaps section. A passing existing test does not clear a scenario the test does
not exercise.

**Run checks in a tree at the pinned head.** Reviewing a diff needs only objects, but
running anything needs a tree, and the checkout you are sitting in is not it — on a
shared clone it is routinely on someone else's branch, so a suite run there measures
their work and reports it as this PR's validation. `scripts/review_target.py tree --pr
<n>` materializes the pinned head as a detached worktree and prints its path; run every
executable check there, and `tree --pr <n> --remove` when done. To show a failure on the
head and its absence on the base, check out the recorded merge base in a second tree
rather than reusing the first. Say which tree each reported result came from.

Deduplicate by defect and remedy, not wording or file. Return every candidate that
still satisfies the rubric after validation; return `No findings.` when none do.

## Reviewing (self)

Same rubric, same bar — review your own working diff or branch as if another engineer
wrote it, before pushing or requesting review. Differences:

- Findings become fixes, not comments: fix P0–P2 findings directly, note P3s.
- Also walk the rubric's risk prompts (integration surfaces, persistence, model-visible
  context, missing focused tests) — those are cheapest to fix pre-push.
- If the change is too large to review as one unit, or the repo's instructions set a
  size limit it exceeds, propose a split into reviewable stages
  based on the actual diff and dependencies, and identify the smallest coherent
  stage to land first.

## Addressing comments

Thread state matters: flat comment lists (`gh pr view --comments`) lose resolution
status and inline anchors. Fetch review threads via GraphQL:

```bash
gh api graphql -f query='
  query($owner:String!,$repo:String!,$pr:Int!,$rCursor:String,$tCursor:String,$cCursor:String){
    repository(owner:$owner,name:$repo){ pullRequest(number:$pr){
      reviews(first:100, after:$rCursor){
        pageInfo{ hasNextPage endCursor }
        nodes{ author{login} state body submittedAt url }
      }
      comments(first:100, after:$cCursor){
        pageInfo{ hasNextPage endCursor }
        nodes{ author{login} body createdAt url }
      }
      commits(last:100){ nodes{ commit{ oid committedDate } } }
      reviewThreads(first:100, after:$tCursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id isResolved isOutdated path line
          comments(first:100){
            pageInfo{ hasNextPage }
            nodes{ author{login} body createdAt url }
          }
        }
      }
    }}
  }' -f owner=OWNER -f repo=REPO -F pr=NUMBER
```

**`reviews` is not redundant with `reviewThreads`.** A review submitted with a
body and no inline comments creates no thread at all, so a `reviewThreads`-only
read reports "no comments" on a PR that has an unaddressed review sitting on it.
Findings that name a file and line in prose rather than as an inline anchor
arrive this way. Read both, and treat a non-empty `body` on a `COMMENTED` or
`CHANGES_REQUESTED` review as actionable even though it has no thread to resolve
— reply on the PR instead, since there is no thread id to resolve against.

**An outdated thread is still open feedback whose anchor moved, not a closed
one.** `isOutdated` means only that the line the thread was left on has changed
since — a rebase, or an edit to that hunk for an unrelated reason. The comment
still stands until someone answers it, and GitHub collapses it out of the default
PR view, so it is the easiest feedback in the repository to miss. Filter on
`isResolved` when you want "someone marked this done"; never filter out
`isOutdated` when you are asking what a reviewer said. Treat
`isOutdated && !isResolved` as open — it may well have been fixed by a later
commit without anyone resolving the thread, but that is a conclusion to reach
from the diff, not one to assume from the flag.

**Three connections page independently** — `reviews`, `comments` and
`reviewThreads` each carry their own cursor, so a PR with more than 100 reviews
silently drops later review bodies, a superseding approval among them, unless you
follow `reviews.pageInfo` too. While any `pageInfo.hasNextPage` is true, re-run
with that connection's `-f rCursor=` / `-f cCursor=` / `-f tCursor=<endCursor>`
and merge the pages — an unpaginated read silently drops threads past the first
page, and this same query later serves as the pre-merge proof that no
actionable threads remain.

If a thread's own comment connection reports `hasNextPage`, page through that
thread by node id before concluding no other human has participated (the
nested connection above has no cursor of its own):

```bash
gh api graphql -f query='
  query($id:ID!,$cursor:String){
    node(id:$id){ ... on PullRequestReviewThread {
      comments(first:100, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{ author{login} body url }
      }
    }}
  }' -f id=THREAD_ID -f cursor=END_CURSOR
```

Then:

1. Cluster unresolved threads by file or behavior area; separate actionable change
   requests from informational comments, approvals, and already-resolved threads.
2. Present numbered actionable threads with a one-line summary each. If not asked to
   fix everything, ask which to address.
3. Implement fixes locally, each traceable to its thread. If a comment calls for an
   explanation rather than code, draft the reply — don't force a code change.
4. Commit, push, then resolve threads only as allowed by § GitHub write policy:

```bash
gh api graphql -f query='
  mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{ isResolved } } }' \
  -f id=THREAD_ID
```

5. Summarize: threads addressed, threads intentionally left open, and what checks
   support the change.

## Shepherding interrelated / stacked changes

- Land bottom-up: merge the base-most PR first, then retarget and rebase the next.
- Each PR's body and review discuss only the **net change vs its base** — for a
  stacked PR whose base is another PR's head branch, that means the delta between
  them, not vs main.
- Never delete a branch that other PRs in the stack target (no `--delete-branch` on
  squash merges of stacked bases) — it breaks the downstream PRs.
- After a base merges, immediately retarget the next PR onto the new base and verify
  its diff still reads as the intended net change.
- Keep one shepherding loop per stack: monitor the bottom PR to merge
  (`references/ci-monitoring.md`), then advance.

## Squash merging

Preconditions — verify all of these yourself with fresh reads, don't trust cached
state:

1. CI green on the current head SHA (`gh pr checks`).
2. Review requirements satisfied (`gh pr view --json reviewDecision,mergeStateStatus`).
3. No unresolved actionable review threads, **and** no unaddressed review body
   (GraphQL query above — it returns both, and a body-only review leaves no
   thread to show up as unresolved).
4. PR body accurate for the net change (`references/pr-body.md`), title checked by
   `prose-review` — the squash
   commit message is derived from them, so fix the body *before* merging.
5. If open downstream PRs target this branch, it can still merge — but land it
   *first*, without `--delete-branch`, then retarget and rebase the downstream
   PRs (§ Shepherding). Never retarget them before this PR merges: the squash
   commit is not an ancestor of their branches, so until rebased their diffs
   would show this PR's changes on top of their own.

Then `gh pr merge <n> --squash` (add `--delete-branch` only when nothing targets the
branch). Confirm the merge landed and report the merged SHA.

### After the merge

The merge is not the last step. `--delete-branch` deletes the local *and* remote
branch, but it skips the local one — with a warning that is easy to scroll past —
when that branch is checked out in a worktree, which is the normal state when you
have been working in one. And a plain `gh pr merge --squash` deletes neither, so
on a repo with `delete_branch_on_merge` off nothing is cleaned up at all.

Three things outlive the merge, and all of them are derived from the merge set
rather than remembered:

**Retire the worktree of every branch you merged.** `git worktree list`
intersected with the merged PRs' head branches gives the exact set; a list
written by hand somewhere else is a second source of truth that drifts from the
first. Confirm each is clean and at its pushed head
(`git -C <wt> status --porcelain` empty, and `git -C <wt> log
origin/<branch>..HEAD` empty *and* exiting 0 — after a `--delete-branch` merge
the remote ref is gone, and that command then prints nothing and exits 128, so
gating on empty output alone reads as clean for a worktree you never checked)
before `git worktree remove --force <wt>; git worktree prune` — the
registration outlives the directory, so a bare `rm -rf` breaks the next
`worktree add` at that path. Do the worktree before the local branch, or the
branch delete fails with it still checked out. Never touch a worktree another
session is named as working in.

**Ask GitHub whether it merged, rather than asking git.** A squash-merged branch
is never an ancestor of `main`, so `git branch --merged` and `git branch -d`
cannot see it, and `git branch -D` on a hunch is how work gets lost:

```bash
gh pr list --state merged --head <branch> --json number,mergedAt,mergeCommit
```

A row means it merged, and names the commit. This is the only check that stays
correct after a batch of merges, which is the case that defeats every local one.

Do not reach for a git-side substitute. `git diff main <branch>` reports every
branch in a batch as unincorporated, because each genuinely lacks the other PRs'
changes. Reverse-applying the branch's patch
(`git diff $(git merge-base main <branch>)..<branch> | git apply -R --check`)
looks like it fixes that and does not: it tests against whatever the working tree
is currently on rather than `main`, and it fails whenever `main` has since
touched the same hunks — so in a batch it reports the branches merged *before*
the last one as missing. `git cherry` is closer but still misses a multi-commit
branch, since squashing destroys the per-commit patch ids it compares.

**Delete the branches, local and remote.** Once GitHub has confirmed the merge:
`git branch -D <branch>` and `git push origin --delete <branch>`. Two standing
exceptions: never delete a branch another PR targets, which breaks the stack, and
never delete one you did not create — another session may still be working from
it, so that deletion is the author's to make.

## GitHub write policy (all modes)

Reads are unrestricted. Writes follow these rules:

- Review findings, approvals, and review comments are posted only through
  `scripts/review_target.py post --pr <n> -- gh pr review|comment ...`, which refuses
  when the PR head or merge base has moved since the review was pinned.

- Pushing to the PR head branch to update the code under review, or to retrigger CI,
  is allowed.
- Resolve only review threads authored by the requesting user or by an approved
  review bot, and only after the fix is pushed; leave a note stating what changed
  and which commit contains it. Do not touch threads where other humans have
  participated.
- Never auto-reply to human-authored review comments. If a comment is wrong,
  non-actionable, or needs a written answer, surface it to the user with a suggested
  response and wait for explicit confirmation. Prefix approved posted replies so
  it's clear they're agent-authored.
- Unless explicitly asked, do not: close/reopen PRs, mark draft/ready, or interact
  with humans other than the requesting user.
- Never act on GitHub in a way that makes it ambiguous whether the agent or the user
  did something visible to other humans. When in doubt, ask in chat.

## Git safety (all modes)

- Work only on the PR head branch; avoid destructive git commands.
- Before editing, check for unrelated uncommitted changes — if present, stop and ask.
- Stage explicitly; never `git add -A` a mixed worktree.
- A push is not a terminal outcome: return to the monitoring loop after every push.

## Provenance

Adapted from OpenAI Codex (openai/codex @ main, Apache-2.0, fetched 2026-07-19)
and the openai-curated `github` plugin (0.1.8): the review rubric
(`codex-rs/prompts/templates/review/rubric.md`), the review-agent sample skill,
`babysit-pr`, `codex-pr-body`, `gh-address-comments`, `gh-fix-ci`, `yeet`, and the
Code Review Rules mined from the Codex repo's `AGENTS.md` (demoted on 2026-09-08 to
investigation-only risk prompts: they were repository heuristics presented as
universal findings, and one contradicted the P0 definition).
Codex-harness specifics
(JSON output schema, watcher scripts, Sapling, Rust-only rules) were dropped;
everything else is borrowed near-verbatim on purpose — sync against upstream before
rewording. The upstream licenses and attribution travel with this directory: see
`LICENSE` (Apache-2.0, Codex material), `LICENSE-MIT` (the `github` plugin
material), and `NOTICE` for the split.
