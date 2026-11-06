# usage-viz

Standalone HTML reports of coding-agent usage, built on [ccusage](https://github.com/ryoppippi/ccusage).

```
usage-viz daily [--breakdown] [--open] [--out-dir DIR] [--no-commit]
usage-viz codex [ROLLOUT | --id PREFIX | --date YYYY-MM-DD] [--open] [--no-cost] [--out-dir DIR] [--no-commit]
```

**daily** is token and cost history across every agent, one bar per day, with
Claude Code `/loop` wakeups overlaid. **codex** covers one Codex run: prompts and
progress notes, commands by category, context fill, plan rate limits, and commits
and PRs. With no rollout given, it picks the day's largest top-level session.
Stdlib Python; `npx` must be on PATH for the ccusage calls.

## Where reports go

Output goes under `daily/` and `codex/` in the first of these that is set:
`--out-dir`, `$USAGE_VIZ_DIR`, then `~/.local/share/usage-viz`. Each page sits
beside the JSON it was built from, named by date (codex reports by date and the
first 13 characters of the session id).

When the output directory is inside a git repo, usage-viz commits the files it just
wrote, and only those, so other staged or untracked work there is left alone.
`--no-commit` skips that. Pushing stays manual.

Codex reports carry prompt text and commands from other repos, so point
`USAGE_VIZ_DIR` at a private repo if the output is committed anywhere, and export
it in your shell profile to get the same destination from anywhere.

## What the reports work out for themselves

**daily** runs `npx ccusage@20.0.26 daily --json`, then counts `ScheduleWakeup` and
`CronCreate` tool calls in `~/.claude/projects/**/*.jsonl` by local date. The ccusage
pin matters: the globally installed 15.x reports Claude only, while v20 reports every
agent. The page derives two markers:

- the /loop start: the first day with at least 10 wakeups (stray calls predate real use)
- the model switch: the first day the Claude model carrying the latest week's spend appeared

Its comparison baseline is the three weeks before whichever came first, so neither
change leaks into it. Every run covers the full history, so there is no separate
long-range report to keep current.

Below those, cache misses and output share are 7-day trailing lines. With
`--breakdown`, a Cost / Tokens / Requests toggle adds two stacked breakdowns:

- **By model**, all agents. Cost and tokens come from ccusage's `modelBreakdowns`.
  Requests are counted from the logs: Claude assistant messages deduped by message
  and request id, and Codex `token_count` events where the running total grew.
- **By project**, Claude only, from `ccusage claude daily --instances` (saved beside
  the daily JSON as `ccusage-projects-<date>.json`). ccusage keys projects by the
  directory a session started in; the report folds worktrees, `-wt-` siblings and
  subdirectories into the `~/projects-new` repo whose name prefixes them.

Both fix their five series by spend over the last nine weeks and put the rest under
Other, so changing the range never recolors a series. The breakdowns are opt-in
because they about double the run time (16s to 34s here, as of 2026-11-02): request
counts parse nearly every transcript line, where the wakeup count alone parses only
the lines that mention a wakeup tool, and the project split is a second ccusage call.

**codex** reads one `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and folds in the
guardian or subagent rollouts that name it as `parent_thread_id`. Cost comes from
`ccusage codex session`. Asking for a side session directly exits and names the parent
to run instead, since its tokens already appear there. `--id` takes any prefix; an
8-character one can match a guardian started in the same minute, so top-level
sessions win.

## Checking a change

`test_usage_viz.py` runs in `just test`: it renders the daily page from a synthetic
payload, with and without `--breakdown`, and fails on a console error or horizontal
overflow, plus unit tests for the project folding and model naming.

`shot.py` is the render check for real output: it screenshots each page in light, dark
and phone widths, and exits non-zero on console errors or horizontal overflow.

```sh
uv run usage-viz/shot.py /tmp/shots "$USAGE_VIZ_DIR"/*/*.html
```

Color slots come from the dataviz skill's reference palette, each set validated with
its `validate_palette.js`; rerun that if you add a series.
