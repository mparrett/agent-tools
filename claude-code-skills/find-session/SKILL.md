---
name: find-session
description: >-
  Locate a past Claude Code session by ticket, PR, file, or topic — and report
  where the work actually happened. Use when the user says "find the convo where
  I worked on PROJ-1234", "which session touched <file/repo>", "I lost the chat
  about X", "where did I do the PR for <ticket>", or invokes /find-session. Also
  use when the user supplies a bare session id or UUID prefix — "tail session
  <uuid>", "resume <uuid>", "get context on <uuid>", "what was I doing in
  <uuid>" — since `claude-ls summary`/`export`/`resume` already resolve a
  partial id. The payoff is the key gotcha: sessions are filed under the
  directory they were STARTED in, not where the edits/PR landed — so content
  search beats guessing the project folder. Returns the session file path, its
  date and starting dir, the real work location (repo/PR), and a ready-to-run
  resume command.
---

# find-session

Find the past Claude Code session where some work happened, given a ticket, PR number, filename, repo, or topic.

## The core gotcha (this is the whole point)

Sessions are stored under `~/.claude/projects/<slug-of-starting-cwd>/<session-id>.jsonl`, where the slug encodes the directory Claude was **launched from**. The work itself often touches a *different* repo. So a session that built a PR in `~/src/project-b/` can be filed under the `project-a` project because that's where the terminal happened to be.

**Therefore: search by content, not by which project folder you'd expect.** Don't conclude a session doesn't exist just because it's missing from the obvious project dir.

## Procedure

**If you were handed a session id or prefix, start at step 2.** `claude-ls summary <prefix>` resolves a partial id directly, and step 1 is actively wrong here: a content search for a uuid ranks by recency and matches every session that merely *mentions* the id — a scratchpad path, another session quoting it — so the session you were given is rarely the top hit.

1. **Search content across all projects.**
   ```bash
   claude-ls search 'PROJ-1234'
   ```
   `claude-ls search` ranks sessions by recency and, for each, prints a `↳` work-line showing **where edits & PRs actually landed** (repo + edit count + PR refs) — derived from the session's tool calls, not its project folder. That work-line usually answers the question outright. Narrow with `--since yesterday` / `--until YYYY-MM-DD` when a ticket spans several days, and `-o user` to match only what *you* typed. Add a raw backstop if a search comes up empty (catches hits `claude-ls` filters as noise):
   ```bash
   rg -l 'PROJ-1234' ~/.claude/projects 2>/dev/null
   ```
   Search a few terms if one is sparse: the ticket, the repo name, a distinctive file or function.

2. **Confirm the candidate.** The longest session with the most edits in the target repo is almost always the real work; brief mentions show no edits or only a stray PR ref. Get the full picture with:
   ```bash
   claude-ls summary <session-id>
   ```
   `summary` prints a **Worked in / PRs / Branches** block and, when they differ, an explicit `↳ launched from X, but edits landed in Y` line — the exact mismatch that usually hides a session from where you'd expect it.

3. **Report concisely:**
   - Session file path (full `.jsonl` path) and date
   - **Where the work actually happened** (repo / PR / branch) — call out a launched-from-vs-landed-in mismatch explicitly
   - Resume command:
     ```bash
     claude-ls resume <session-id-or-prefix>
     ```
     `resume` resolves a partial id, `cd`s into the session's anchoring dir (following a removed worktree back to its main repo), and hands off to `claude -r`. It hard-stops if a tmux session named after the project is live, so you don't double-drive an in-progress session (`--attach` to jump to it, `--force` to override).
   - Note any other sessions that matched but are just passing references.

## claude-ls cheat-sheet

`claude-ls` (at `~/bin/claude-ls`) browses `~/.claude/projects`:

- `search <text>` — sessions containing text, with a `↳` work-line (repo/edits/PRs), snippets, dates, starting dirs. Flags: `--since`/`--until DATE` (YYYY-MM-DD, `today`, `yesterday`, `Nd`), `-o/--only-type user`, `-m/--max-matches N`, `-w/--width N`, `--no-work` (skip the work-line for speed), `-x/--exclude-type`, `-b/--brief`
- `summary <session-id>` — first/last message, turn count, duration, and the Worked-in / PRs / Branches block
- `export <session-id|path>` — full session as markdown; accepts a bare session id or a `.jsonl` path
- `sessions` — recent sessions with IDs across all projects
- `last` — recent messages from the current dir's session
- `resume <id-or-prefix>` — cd to the session's anchoring dir (follows a gone worktree to its main repo) and run `claude -r`; tmux safety check, `--dry-run` to print the command instead, `-a` attach, `-f` force

## Notes

- `rg -M 320 --max-columns-preview '<query>' ~/.claude` is the general raw-grep form across all of `~/.claude` when you want surrounding context, not just a file list.
