# claude-graph — dogfooding findings (2026-07-04)

First run of the shipped v1 tool against the live session store. Recorded
here so the observations aren't lost between sessions.

## What works well

- **Seed view is the standout.** `claude-graph <id>` reconstructs the work cluster
  around a session — the connected PRs, branches, files, and the handoff→resume→review
  chain — in a compact, readable form. It answers "how does this session connect to my
  other work?" in a way `claude-ls` can't.
- **Concentration is legible.** On the live store the seed surfaced the busiest PR
  as the current hot spot (7 sessions) with a clear resume/review lineage.
- **Subagent nesting and the first-user label** (post the review fix) make each row
  self-describing.

## Bugs

### 1. `<task-notification>` leaks into the first-user label  (real; dogfooding caught it)

Sessions driven by background-task completions render as
`"<task-notification> <task-id>… "` instead of the human's prompt. Confirmed in the
raw transcript for one task-driven session: the leading "user" messages are
`<local-command-caveat>` → `/clear` → a run of `<task-notification>` blocks.
`_first_user_text` skips the first two (via `_WRAPPER_PREFIXES`) but not
`<task-notification>`. It's the same class of injected synthetic message as the
caveat/command wrappers already handled.

**Fix:** add `<task-notification>` to `_WRAPPER_PREFIXES` (and, for robustness, the
tags it wraps — `<task-id>`, `<tool-use-id>`, `<output-file>`). Fixture tests can't
catch this class; only live data does. Consider a small live-store smoke check.

## UX / signal-quality observations (scan view)

The scan view degrades on a busy single-project store (here: 40 sessions, 38 groups,
all one workspace). Specific issues:

- **Same-filename groups are indistinguishable.** `file shell.html` appears as
  ~5 separate groups with identical labels (different member-sets / worktree-path
  copies of the same basename). The label needs to disambiguate — path hint, or merge.
- **Scaffolding files dominate.** `MEMORY.md`, `key_facts.md`, `justfile`,
  `decisions.md`, and handoff docs form large "shared file" groups that signal
  "same project" more than "related work." `key_facts.md` / `MEMORY.md` each hit 7
  sessions but `hub=8` doesn't suppress them — the hub threshold is a blunt instrument
  and project-scaffolding files slip under it.
- **No global ranking / cap on scan.** All 38 groups print. On a busy store this buries
  the few high-signal clusters.

## Takeaway

Seed view delivers today. The scan view needs a signal pass (dedup same-basename
groups, down-weight scaffolding files, rank + cap output) before it's the default way
in. The `<task-notification>` fix is a quick correctness win.
