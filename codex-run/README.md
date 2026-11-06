# codex-run

Drive the Codex CLI non-interactively: review a PR, review a local change,
implement an issue, or send any prompt, with the prompt built from a template
and the result written to a file. Made for calling from a shell or from
another agent.

Requires `codex` (logged in) and `jq`. `review` and `post` need `gh`;
`review-local` needs the sibling `preview-diff`; `tui` needs `tmux`.

## Quick start

```bash
codex-run review owner/repo 42          # background; prints the run id
codex-run wait <run-id>                 # or watch for <run>/review.md to appear
codex-run post <run-id>                 # posts review.md as a PR review via gh

codex-run review-local myproj origin/main HEAD   # no PR: renders with preview-diff
codex-run build myproj 197              # implement an issue on codex/issue-197; commits, never pushes
codex-run task myproj "explain how the scheduler picks the next job"
codex-run limits                        # 5h / weekly usage
```

A project is a path, a directory name looked up under `$CODEX_RUN_ROOTS`
(colon-separated; default: the parent of the current directory), or a GitHub
`owner/name`, which resolves by its repo name and also sets the repo. Otherwise
the repo is parsed from the checkout's `origin`, or given with `--repo`.

## Runs

Every dispatching command creates a run directory under `$CODEX_RUN_HOME`
(default `$XDG_DATA_HOME/codex-run/runs`, i.e. `~/.local/share/codex-run/runs`):

| File | What |
|---|---|
| `meta.json` | id, kind, dir, repo, pr/issue, model, effort, status, thread id, timestamps |
| `prompt.md` | the rendered prompt that was sent |
| `events.jsonl` | the `codex exec --json` event stream |
| `stderr.log` | codex's stderr |
| `status` | `created` → `running` → `done` or `failed` |
| `review.md` / `result.md` | the final message. Written only when the turn is complete. |
| `preview.html`, `change.diff` | review-local only: the rendered page and the raw diff |

`status` is the completion signal, and `codex-run wait <id>` polls it. A watcher
on the output file cannot distinguish a finished run from a failed one: `done`
requires a zero exit *and* a non-empty output, and codex writes the output file
itself, before the status is settled. `ls` lists recent runs; `show <id>` prints
one with its result.

`post` and `resume` continue the original thread (`codex exec resume`), so
Codex still has the review in context. `post` reads `review.md` from disk, so
edits made before posting are what gets posted.

## Commands

```
review <project> <pr>            Review a GitHub PR. Findings land in review.md; nothing is posted.
review-local <project> [base] [head] [-- preview-diff args]
                                 Review a local change via a preview-diff page. No GitHub involved.
post <run-id>                    Post a review run's review.md on its PR.
build <project> <issue>          Implement an issue on branch codex/issue-N. Commits, never pushes.
task <project> "<prompt>"        Free-form prompt in a project.
resume <run-id> "<prompt>"       Continue a run's thread with a new prompt.
wait <run-id> [--timeout <s>]    Block until a run finishes. Exit 0 done, 1 failed, 124 timeout.
ls | show <run-id>               Recent runs; one run with its result.
limits [--json]                  5h / weekly usage from the newest Codex rollout.
tui <project> ["prompt"]         Interactive codex in a detached tmux session.
init                             Write the default prompt templates for editing.
```

Options for the dispatching commands: `--model`, `--effort` (low … max),
`--repo`, `--fg` (foreground, streams a compact event log), `--full-access`
(danger-full-access sandbox), `--dry-run` (print the exact `codex` argv and
prompt, run nothing).

`review-local` forwards anything after `--` to preview-diff, e.g.
`-- --body pr-body.md --title "the change"`.

## Templates

Prompts are rendered from `$CODEX_RUN_TEMPLATES/<kind>.md` (default
`$XDG_CONFIG_HOME/codex-run/templates`), falling back to the defaults built
into the script. `codex-run init` writes the defaults out for editing.
Placeholders are replaced literally: `{{slug}}`, `{{dir}}`, `{{repo}}`,
`{{pr}}`, `{{issue}}`, `{{prompt}}`, plus `{{review_file}}`, `{{review_flag}}`
and `{{downgrade_note}}` for `post`, and `{{preview}}`, `{{diff_file}}`,
`{{base}}`, `{{head}}` for `review-local`, and `{{rubric_note}}` for both
review kinds.

The review templates ask for a `Verdict:` first line and nothing else about
form. What counts as a finding, its priority and the verdict come from one
rubric, `claude-code-skills/pr-shepherd/references/review-rubric.md`, which
`{{rubric_note}}` names by absolute path — located next to the script, or set
`CODEX_RUN_RUBRIC`. Codex's own review rubric is not in play: it loads for
`codex exec review`, and `codex-run` runs a plain `codex exec` turn.

`post` resolves that verdict **before dispatching** and hands the turn one
complete command — flag and body already decided — rather than a rule to apply.
It writes `review.body.md` next to `review.md` (the review minus its verdict
line, which is what `--body-file` reads, since `gh pr review` refuses a blank
body on `--comment` and `--request-changes`), and maps the verdict to
`--request-changes`, `--approve` or `--comment`.

GitHub rejects `--approve` and `--request-changes` on a pull request you
authored, so `post` asks GraphQL whether the PR is yours and downgrades to
`--comment` itself, writing that fact into the body — a request-changes review
that lands silently as a comment reads as a weaker verdict than the reviewer
reached. Deciding this in the shell is deliberate: a turn told to recover from
GitHub's rejection on its own has instead reported the rejection and stopped,
leaving the review unposted and the run marked done. If the authorship probe
cannot answer — no network, no auth — the verdict's own flag is used and GitHub
remains the backstop.

`review` records the PR's head SHA, and `post` refuses when the head has moved
since: a review reads one specific tree, and posting it against a later one
submits a verdict for code nobody looked at. `--force` posts anyway. A head that
cannot be read is treated as unknown, never as unchanged, so a probe failure
does not block a post.

## Sandbox

`codex exec` defaults to a read-only sandbox that also blocks the network, so
`gh` inside it cannot even read a PR. Runs use:

```
-c sandbox_mode="workspace-write" -c sandbox_workspace_write.network_access=true -c approval_policy="never"
```

`--full-access` swaps in `danger-full-access`. The `-c` forms are used rather
than `--sandbox` because `codex exec resume` accepts only the former.

A linked worktree keeps its git metadata in the main repo's `.git`, outside the
workspace, so `git checkout` inside one fails on `index.lock` with `Operation
not permitted` and Codex falls back to reviewing an archive of the head.
Dispatching commands therefore pass that directory as an extra writable root:

```
--add-dir "$(git rev-parse --git-common-dir)"
```

An ordinary checkout keeps its common dir inside the workspace already, so
nothing is added for one. `post` and `resume` never pass it — `codex exec
resume` rejects `--add-dir`.

## Limits

The `--json` stream carries per-turn token usage only. The rate-limit windows
are recorded, every turn, in the rollout file under `~/.codex/sessions/` as a
`token_count` event with `rate_limits`. `codex-run limits` reads the newest
such record, from any Codex frontend.
