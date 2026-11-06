---
name: codex-review
description: >-
  Get a second-opinion code review from OpenAI Codex via the `codex-run` CLI —
  dispatch it, wait on it correctly, verify what it found, and post it only when
  a human says so. Use ONLY when the user names Codex explicitly: "review this
  with codex", "codex review", "codex-review", "get a second opinion from
  codex", "what does codex think of this PR". Handles both a GitHub PR
  (`codex-run review`) and a local change with no PR (`codex-run review-local`,
  rendered through preview-diff). A plain "review this PR", with no mention of
  Codex, belongs to pr-shepherd instead — as does round two, once the author has
  addressed the findings.
---

# Codex review — a second opinion, verified before it lands

`codex-run` drives the Codex CLI non-interactively and leaves the review on disk,
so this session can dispatch it, wait, read it, check it, and decide what to do.
Full CLI surface: `codex-run/README.md`. Machine-local setup, the sandbox
details, and the running "things that bit" list live in
`~/projects-new/project-docs/docs/agent-tools/2026-09-09-codex-run-local-ops.md`
— read that rather than re-deriving them here.

## When this skill is the wrong one

- **"Review PR #N"** with no mention of Codex → `pr-shepherd`. Don't offer a
  Codex run as a substitute; it costs 5–8 minutes and a big token window.
- **Round two** — the author pushed fixes and wants them checked → `pr-shepherd`,
  verified against the saved `review.md`. That file on disk is the point. Do not
  spend a second Codex run re-reviewing what you already have a review of.

## Dispatch

```bash
codex-run review <project> <pr>                    # a GitHub PR
codex-run review-local <project> <base> <head>     # no PR; rendered via preview-diff
```

`<project>` is a path, a bare directory name resolved under `$CODEX_RUN_ROOTS`, or
a GitHub `owner/name`. Both commands go to the background and print a run id.

Hand a PR review a **clean worktree** rather than the canonical checkout, which is
routinely on someone else's dirty branch — concurrent reviews need one each.

## Waiting: background, always

```bash
codex-run wait <id> --timeout 1500     # run this as a BACKGROUND Bash call
```

Two failure modes to avoid:

- **Never poll in a loop.** Dispatch, then one background `wait`. The harness
  completion notification is what wakes the session.
- **Never run `wait` in the foreground.** Reviews outlast the foreground tool
  timeout, so the call is killed and the exit code — the whole point — is lost.

Exit codes: **0** done, **1** failed (read `stderr.log`), **124** timed out with
the run still going (it is not dead; `wait` again or check `status`).

## The run directory

`~/.local/share/codex-run/runs/<id>/` — `meta.json`, `prompt.md`, `events.jsonl`,
`stderr.log`, `status`, and the output file.

**`status` is the completion signal, not the output file.** It holds
`created` → `running` → `done` | `failed`, and is written after codex exits;
`done` requires both a zero exit and a non-empty output. `codex-run wait` polls
exactly this. Watching the output file instead cannot separate a finished run
from a failed one, and codex writes that file itself, before the status settles.

```bash
RD=~/.local/share/codex-run/runs/<id>
cat "$RD/status"
cat "$RD/$(jq -r .output "$RD/meta.json")"   # review.md for reviews, result.md otherwise
```

## Verify every finding before acting on it

Codex is good and it is not always right: true findings and a confidently wrong
one can arrive in the same review.

**First check, every time: does the diff actually contain the file the finding
names?** A false positive can name a file the branch never touches.

```bash
git diff --name-only <base>...<head>     # three dots: merge base, what really changed
```

A finding can also be wrong about code that is genuinely in the diff.

Then confirm the mechanism in the code before you repeat the finding as fact.
Report what survives, and say plainly which findings you discarded and why.

## Posting needs an explicit go-ahead

```bash
codex-run post <id>              # posts review.md as a PR review via gh
codex-run post <id> --force      # only with a stated reason
```

Never post unprompted. `post` reads `review.md` from disk, so the pause is there
for a human to edit the review first — that is the design, not an oversight. It
resolves the verdict before dispatch and downgrades to `--comment` on the user's
own PRs. It refuses when the PR head has moved since the review, because a
verdict would then be attached to code nobody read; `--force` overrides that and
needs a reason you can state.

## What the checkout is, and is not

The directory you pass is **context and caches, not the thing under review.**
Codex works with cwd set to that checkout (`codex exec -C "$dir"`) and reads the
PR over the network with `gh` — description, diff, CI, existing comments. Nothing
is checked out into your tree, which is why the checkout stays on whatever branch
it was on with no modifications.

When Codex wants to *run* something it materializes the head elsewhere — a
temporary clone under `/tmp`, or a `git archive` — so **it will not mutate your
working tree.** Verified: it does this in roughly a quarter of review runs, and
never issues `git worktree add`.

One thing to know when you hand it a linked worktree: that worktree's git
metadata lives in the main repo's `.git`, outside the workspace-write sandbox.
Dispatching commands pass `--add-dir "$(git rev-parse --git-common-dir)"` so a
handed worktree can check out the branch it is reviewing (without it, checkout
dies on `index.lock`). That flag does not reach `post`, which resumes
the thread rather than starting one: `codex exec resume` accepts neither
`--add-dir` nor `-s/--sandbox`, only `-c`.

When Codex does hit a sandbox block it routes around rather than asking, because
`codex exec` has **no approval mode at all** — `-a/--ask-for-approval` exists
only on the interactive `codex`, so there is nobody for a non-interactive run to
ask. The Codex Desktop app runs `on-request` ("Approve for me"), which is why the
same operation can succeed there after a prompt and look like a codex-run-only
problem.

## Budget

Reviews take **5–8 minutes** and **0.6M–1.4M input tokens**, overwhelmingly
cached. `codex-run limits` prints the 5h and weekly windows; check it before
dispatching a batch.

## Stopping to ask

Stop and ask rather than guessing when: the project or base/head is ambiguous;
`wait` returns 124 twice; the run is `failed` and `stderr.log` is not
self-explanatory; a finding contradicts something you verified; or posting would
be the first GitHub write of the session.
