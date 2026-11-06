---
title: "claude-ps — resting-state activity glance"
status: partially shipped
author: Matt (via Claude)
created: 2026-06-30
kind: feature
---

# `claude-ps` — at-a-glance activity across the fleet

> **Status.** This is the origin design note. What shipped: `claude-ls`
> `read_session_header` + `pending --json {cwd, git_branch}`, `last --json` /
> `--cwd`, and the `claude-ps --status` waiting-flag v1. The join was
> implemented on the real transcript `cwd` (path-based), not the folder slug this
> note first sketched. The last-response column and the "last recap" idea remain
> unbuilt. Treat the design discussion below as the record of how
> we got here, not a to-do list.

## Summary

Extend `claude-ps` to optionally surface each session's **resting state** — the
last assistant response, the last recap, and/or whether the session is waiting
on input — so a single `claude-ps` run answers "what is each of my Claude
sessions actually doing right now?" without opening any of them.

Today `claude-ps` is a pure process view: PID, age, CPU, tmux session, CWD,
command. It tells you a session *exists* and is *busy* (CPU), but nothing about
*what it's working on* or whether it's stuck waiting on you. This feature adds a
thin content layer on top of the existing process rows.

## Why

The fleet is the unit of attention now — several Claude sessions running across
tmux panes and worktrees. Scanning them one by one (switch pane, read scrollback,
switch back) doesn't scale. CPU tells you "hot vs idle" but conflates "idle and
done" with "idle and blocked on a permission prompt." A one-line activity glance
per session turns `claude-ps` into a fleet dashboard.

## What counts as "resting state"

In priority order, cheapest/most-useful first:

- **Last assistant response** — the tail of the most recent assistant turn,
  truncated to one line. Best single signal of "what it's on."
- **Waiting/pending** — session is blocked on a question or a permission prompt.
  High-value because it's *actionable* (it wants you).
- **Last recap** — if the session emitted a recap/summary, show it instead of the
  raw response tail (more durable "where we are" signal).

## Reuse, don't reinvent

`claude-ls` already does the hard part, but is only **half-ready** for this — see
"Prerequisite work in `claude-ls`" below:

- `claude-ls last` extracts the last assistant response and **auto-detects the
  session from `os.getcwd()`** (most-recent session in the project). But it
  prints human text only — **no `--json`** — and has **no `--cwd` flag**, so it
  reads the real working directory.
- `claude-ls pending --json` already classifies sessions waiting on user input
  and emits structured rows: `{session_id, project, minutes_waiting, preview,
  path}`. It scans all projects (keyed by collapsed project label, not CWD) and
  returns only waiting sessions.
- It already maps a CWD → `~/.claude/projects/<encoded>/` → most-recently-active
  session transcript, including worktree handling.
- There is **no "recap" concept** in `claude-ls` today; that field is net-new
  transcript parsing.

## Prerequisite work in `claude-ls`

The two signals have very different readiness, which splits the work into two
PRs:

1. **`claude-ls` (do first).**
   - Add `--json` to `last` so `claude-ps` consumes structured output instead of
     scraping `[assistant] …` lines.
   - Add CWD addressing that doesn't rely on `os.getcwd()` — either a `--cwd
     <path>` flag on `last`, or (better for a fleet) a batch `resolve --json`
     that takes many CWDs and returns one resting-state row each. Batch avoids
     spawning one subprocess **per process row** from inside each directory.
   - (Optional) define and emit "last recap" if it earns its parse cost.
2. **`claude-ps` (do second).** Consume the above; render the column/line.

The **waiting glance is buildable today** against `pending --json` with a single
fleet-wide call (see below). The **last-response glance needs PR 1 first.**

`claude-ps` already resolves, per process, the **CWD** and **tmux session**. The
missing link is joining those two: take each `claude-ps` row's CWD, ask the
`claude-ls` transcript layer for that session's resting state, and render it.

**Decision: shell out to `claude-ls --json`, do not share a module.** Both tools
are distributed as single self-contained scripts (inline `uv` deps in the
shebang) — you `cat` one file over the network and it runs. A shared importable
helper breaks that: `claude-ps` would need a third file present at a known path
plus packaging to keep them in sync. Shelling out keeps both files standalone and
matches how `claude-ps` already depends on sibling binaries (`ps`, `lsof`,
`tmux`, `pgrep`); `claude-ls` is just one more, looked up on PATH and degrading
to blank cells when absent. The cost given up is subprocess + double-parse, which
is gated behind `--status`, bounded by tail-reads, and collapsible to **two
subprocesses total** (one `pending --json`, one batch `resolve --json`) rather
than one per row — acceptable for a glance tool, revisit only if profiling says
so.

Two alternatives considered and rejected:

- **Shared importable module.** Cleanest code, but breaks single-file
  cat-distribution — `claude-ps` would need a third file at a known path plus
  packaging to keep it in sync.
- **Build/graft step** — a compile pass that splices a shared "library" block
  into both scripts at build time, keeping the *shipped* artifacts single-file.
  This solves distribution but reintroduces what the toolbox avoids: a build
  step, a source-vs-artifact split, and a "did you regenerate?" failure mode for
  a one-file tool you currently just edit and run. Not worth it to dedupe a thin
  join layer; reconsider only if real shared logic accumulates across several
  tools.
- **Duplicating the transcript-reading logic into `claude-ps`** — rejected: it's
  a non-trivial slice of a large file, maintained twice.

## UX

Keep the default `claude-ps` table lean and fast. Gate the content layer behind
a flag — e.g. `--status` (or `--activity`):

```
claude-ps --status
```

Open question — rendering (pick in review):
- An extra right-hand **Activity** column, truncated to terminal width.
- A wrapped **second line** under each row (dim), so the response tail gets room
  without crushing the process columns.

Decorate the actionable state: sessions **waiting on input** get a marker/color
(reuse the existing `◀` / yellow-warning vocabulary) so they pop in a `watch`
loop. `--json` gains the same fields (`last_response`, `last_recap`, `waiting`)
so the data is scriptable, mirroring the existing JSON path.

## Performance

The process listing must stay cheap. Reading a transcript per process is the new
cost, so:

- Only do transcript reads when `--status`/`--json`-with-status is requested —
  never on the default path.
- **Batch, don't fan out per row.** Two subprocess calls total: `claude-ls
  pending --json` once for the fleet-wide waiting set, and one batch `resolve
  --json` for the last-response rows (see prerequisite work). Avoid spawning
  `claude-ls` once per process from inside each CWD.
- Read **tail-first** (last assistant turn / recap) rather than parsing whole
  JSONL files.
- Join key: `pending --json` returns the **collapsed project label**, so
  `claude-ps` derives the same label from each row's CWD to match. Confirm both
  tools compute that label identically (a real coupling risk if either drifts).
- Degrade gracefully: no transcript found, unreadable, `claude-ls` not on PATH,
  or no tmux → blank cell, same way the Session column already tolerates a
  missing tmux.

## Acceptance

- `claude-ps --status` shows, per Claude process, a one-line resting-state cell
  (last response, or recap when present).
- Sessions waiting on input are visibly flagged.
- Default `claude-ps` (no flag) is unchanged in output and speed.
- `--json` carries the new fields when status is requested.
- Missing/unreadable transcript or missing tmux degrades to a blank cell, never
  an error.

## Open questions

- Column vs. wrapped second line (above).
- Recap source — what exactly marks a "recap" in the transcript, and is it worth
  the extra parse over just the last-response tail in v1?
- Truncation budget under narrow terminals / `watch`.
- Join key: is the collapsed project label stable enough to join on, or should
  `last --json` / `resolve --json` echo the matched session's real CWD so
  `claude-ps` joins on path instead of label?
- Scope check: ship the **waiting glance alone** as a v1 (buildable today against
  `pending --json`), and defer the last-response column to a follow-up once
  `last --json` lands?
