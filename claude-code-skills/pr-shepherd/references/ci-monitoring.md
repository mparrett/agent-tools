# CI monitoring and shepherding loop

Distilled from Codex's `babysit-pr` and `gh-fix-ci` skills. Codex's watcher script
is not bundled here — the loop below uses plain `gh`. (If the sibling `pr-watch`
skill is installed, it can supply change-deltas across many PRs; this loop is for
shepherding one PR to completion.)

## Contents

- Objective — the two monitoring scopes and their terminal outcomes
- The loop, and its polling cadence
- Diagnosing a CI failure, and classification
- Handling review feedback mid-watch
- Stop conditions (strict)
- Final summary

## Objective

Two monitoring scopes share this loop — the user's request picks the terminal
outcome:

- **CI-until-green** ("watch CI and fix what breaks", "monitor CI until green"):
  terminal when every check on the current head SHA passes, or when user help is
  required. Report the green state and stop — don't keep polling for review
  activity the user didn't ask you to watch.
- **Full shepherding** ("babysit this PR", "merge when green"): terminal only
  when the PR is merged or closed, or when user help is genuinely required (CI
  infrastructure issues, flaky-retry budget exhausted, permission problems,
  ambiguity that can't be resolved safely). Here green + mergeable +
  review-clean is a **progress milestone, not a stop** — keep watching while the
  PR is open so late review comments are surfaced promptly.

In both scopes a push is never a terminal outcome — after pushing a fix, return
to the loop until the scope's terminal state is reached. Do not stop because one
snapshot looks idle while checks are still pending, and do not hand back control
just because a new SHA was created.

## The loop

1. Snapshot state:
   ```bash
   gh pr view <n> --json state,mergeable,mergeStateStatus,reviewDecision,headRefOid
   gh pr checks <n>
   ```
   In full-shepherding scope, also run the review-threads GraphQL query in
   SKILL.md for new feedback. In CI-until-green scope, do not read or act on
   review state at all — checks and mergeability only.
2. If the PR is merged/closed: report the terminal state and stop immediately.
   In CI-until-green scope, if every check on the current head SHA passes:
   report green and stop.
3. Check, in priority order: new review feedback (full shepherding only) → CI
   failures → mergeability. Actionable review feedback comes before flaky
   reruns — a fix commit retriggers CI anyway, so don't rerun failed checks on
   a SHA you're about to replace.
4. Diagnose any CI failure (below) and classify branch-related vs flaky/unrelated.
5. Branch-related failure → patch locally, commit, push. Actionable review comment
   (full shepherding only) → patch, commit, push, resolve per the SKILL.md write
   policy. Flaky/unrelated → rerun failed jobs (`gh run rerun <run-id> --failed`),
   at most 3 retries per failure; then escalate to the user.
6. After any push or rerun, resume polling on the new SHA/state. Repeat.

### Polling cadence

- CI pending/running/failing: poll every ~1 minute.
- CI green, PR still open (full shepherding only): keep polling at the base
  cadence — new review comments should surface promptly, not after a long
  green-state backoff. In CI-until-green scope, green is terminal — no further
  polling.
- Reset the cadence immediately on any change (new SHA, check transition, new
  comment, mergeability change).
- During long unchanged stretches, don't emit a full update every poll — report
  status changes plus an occasional heartbeat. When CI first goes all-green for a
  SHA, say so once (e.g. "CI green, 33/33 — still watching for review").

## Diagnosing a CI failure

Don't wait for the whole workflow run to finish — as soon as one job fails, pull
that job's logs directly:

```bash
gh run view <run-id> --json jobs,name,workflowName,conclusion,status,url,headSha
gh api "repos/<owner>/<repo>/actions/jobs/<job-id>/logs"   # per-job, available early
gh run view <run-id> --log-failed                          # fallback once run completes
```

If a failing check's `detailsUrl` is not a GitHub Actions run, treat it as external:
report the URL, don't attempt to debug the other provider.

### Classification

**Branch-related** (fix it): failed-job logs point at changed code —
compile/test/lint/typecheck/snapshot/static-analysis failures in touched areas.

**Flaky/unrelated** (rerun, don't fix): transient infra or external issues —
timeouts, runner provisioning failures, registry/network outages, Actions infra
errors.

Never "fix" a flaky/unrelated failure by changing tests, build scripts, CI config,
dependency pins, or infra-adjacent code just to get green, unless the logs clearly
connect the failure to this branch. If classification is ambiguous, make one manual
diagnosis attempt before choosing rerun.

## Handling review feedback mid-watch

Full-shepherding scope only — a CI-until-green run never reads, patches, or
resolves review feedback.

- Act only on published feedback; ignore reviews in `PENDING` state and their
  inline comments (they surface when submitted).
- Already-resolved threads are non-actionable unless new unresolved follow-up
  appears.
- Actionable and correct → patch, commit (`fix CI failure on PR #<n>` / `address PR
  review feedback (#<n>)` style messages), push, resolve per write policy, resume
  watching.
- Human comment you disagree with, or one needing a written answer → surface to the
  user with a suggested reply; never post without confirmation.
- If a later snapshot surfaces your own approved reply, treat it as handled — don't
  respond again.

## Stop conditions (strict)

Stop only when:

- CI-until-green scope: every check on the current head SHA passes,
- full-shepherding scope: the PR is merged or closed, or
- either scope: user intervention is required and it is unsafe to proceed alone.

Keep going when: checks are still pending, CI is running, or review state is quiet
but CI isn't terminal. In full-shepherding scope, also keep going when CI is green
but mergeability is unknown, or the PR is green but blocked on required approval —
in that last state keep watching and surface new comments as they arrive.

## Final summary

On reaching a terminal state report: final head SHA, CI status summary,
mergeability/conflict status, fixes pushed, flaky retry cycles used, and any
remaining unresolved failures or review comments.
