# PR title and body

Adapted from Codex's `codex-pr-body` skill and `yeet`'s body expectations
(Sapling/OpenAI-specific sections dropped).

## Resolving the target PR

The PR may be named explicitly; in the common case infer it from the current
branch: `gh pr view --json number,url`.

## Contents

- **Why before what.** Explain *why* the change is being made first — if the
  current conversation discussed the motivation, capture it.
  What changed comes after, and matters less.
- **Preserve existing content.** Check the existing body for key information before
  rewriting. NEVER remove an image from an existing body — the author may have no
  way to recover it.
- **Net change only.** Discuss the net change of the PR, not the development
  journey. Drop mentions of approaches attempted and undone — they are no longer of
  interest to future readers.
- **Verification.** Say how the change was verified. Skip things CI checks
  automatically (don't list "ran the formatter"); do call out new tests
  purposely introduced to cover the new behavior.
- **No local or confidential detail.** No absolute paths from the local disk (use
  repo-relative paths), no internal codenames or private URLs.
- **Formatting.** Professional Markdown: inline backticks for code things, fenced
  blocks for code or shell transcripts, GitHub permalinks when citing existing code
  relevant to the change. Reference related PRs/issues; never reference the PR in
  its own body.
- For a fix, state the root cause and the user/developer impact.

## Stacks

- A PR composed of a stack of commits gets a body describing the net change of the
  stack as a whole, not commit-by-commit.
- A stacked PR whose base is another PR's head branch gets a body describing only
  the net change between its base and head — not the diff vs main.
