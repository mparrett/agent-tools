# codex-review-scan

What Codex does when asked to "review this PR", read from its rollouts.

Codex CLI rollouts under `~/.codex/sessions/` record the system prompt each session
ran under, every tool call with its input, and every message, so a review process
can be measured after the fact. `codex-review-scan` (stdlib, same
`CLAUDE_LS_CODEX_DIR` override as `claude-ls --codex`) reads them:

```bash
codex-review-scan/codex-review-scan.py reviews              # PR-review sessions: what each one ran
codex-review-scan/codex-review-scan.py reviews --since 2026-08-01 --json
codex-review-scan/codex-review-scan.py trail 0123abcd      # one session's shell commands, then its final answer
codex-review-scan/codex-review-scan.py prompt --out /tmp/codex-prompt.md   # the system prompt, by heading
```

`prompt --out` writes the vendor's own system instructions to the file you name.
Send it to a scratch path, not into a repository, so it is not committed.

`reviews` keeps sessions whose first human line asks to review a PR, drops the
`codex-auto-review` side sessions (a second model that assesses the agent's own
actions) and sessions with no tool calls, and counts tests, fetch/checkout, `gh pr`
reads, web use, and GitHub posts per session. The read counts are substring
heuristics over tool-call inputs; `posted` is classified from the tool name and
the command's HTTP method, because a read of a comments endpoint is spelled like
a write of one. They are meant for tallying across many sessions rather than
judging one. Scanned 2026-09-10, over 960 rollouts: 297 review sessions with tool
calls since 2025-11, median 45 tool calls, 68% ran a test command, 61 (20%)
posted; the system prompt carried no review rubric, only "inspect the task and
provide an evidence-backed response".

Built as research for `pr-shepherd`, to separate what Codex's review quality
owes to its prompt, its harness, and its model. It lived in `claude-ls/` until it
moved here: it doesn't use claude-ls, and a PR-review analysis is outside that
tool's job. If a second Codex-only analysis appears, this becomes a subcommand of
a `codex-ls` rather than the start of a grab-bag directory.
