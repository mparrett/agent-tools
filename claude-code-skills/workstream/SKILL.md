---
name: workstream
description: >-
  Attach the current session to the workstream it belongs to, resume work from
  a workstream's frontier, and record the frontier before a session is retired.
  Use when a SessionStart block lists workstream candidates, when the user says
  "what's the state of X", "who worked on this PR / branch / worktree",
  "where did we stop", "pick up where the other session left off", "which
  session owns #N", or "retire these sessions", or invokes /workstream. Also use
  before taking an action on an anchor another workstream holds (merging its
  PR, touching its worktree). Backed by the `workstream` CLI over weft; the
  prose frontier lives in project-docs.
---

# workstream

A workstream is the durable identity of a piece of work: usually one project,
sometimes several, spanning Claude sessions, their subagents, Codex rollouts,
PRs, branches, worktrees, specs and handoffs. `workstream` is the CLI; weft is
the store; this skill is how a session uses them.

## When to attach (and to what)

A session has **one canonical workstream at a time** plus any number of
ancillary ones. Attach early, before the work makes the session's identity
obvious to nobody but itself.

1. At session start, a hook prints ranked candidates with reasons. Read the
   reasons, not the scores: a pasted session id or a shared PR is strong,
   repo overlap alone is weak.
2. If a candidate is right: `workstream attach <ws>`. Add `--ancillary` if
   this session is helping, not owning (reviewing a PR another session
   shepherds, say).
3. If none fits and the work is real: `workstream attach --new "<title>"
   --name <slug> --anchor gh:o/r#N --anchor wt:<path>`. Name the anchor that
   *is* the work; a PR beats a branch beats a directory.
4. If this session is not a workstream (a question, a one-off lookup):
   `workstream attach --none "<reason>"`. That is a recorded answer, not a
   skipped step; the hook stops asking.
5. Say it. After any attach, tell the user in one line which workstream you
   attached to and whether as owner or helper (`--ancillary`), or that you
   recorded `--none` and why. The attach is otherwise invisible to them, and
   which session owns what is what they need to know when several run at once.

A canonical attach also runs `workstream sync` for this session: the PRs it opened,
merged, reviewed or commented on, the branches it committed or pushed, and
the worktrees it worked in become anchors, each printed with the evidence.
Later in the session, after opening a PR or pushing a new branch, run
`workstream sync <ws> --session <your sid>` (or just `workstream sync`) so
the next session finds them. It adds strong evidence only: a repo you merely
read, or a PR you only looked at, stays out. `--dry-run` shows what it would
add. A synced anchor shows as `(sync)` in `workstream show`; `--anchor` is
still the way to name something you did not touch yourself.

Sync takes evidence only from sessions that own the workstream (hold or held
its canonical claim). Ancillary means referenced, not owned; its evidence is
not the workstream's: `attach --ancillary` adds no anchors, and a helper's
other PRs from the same afternoon stay out. Name the one session with
`--session SID`, or pass `--ancillary`, when a helper's work does belong.

Re-run `workstream match` when the work changes shape: a session that drifts
from repo A into repo B's PRs should attach to B's workstream (ancillary, or
canonical if it took the work over). The attach is timestamped, so attribution
is per interval.

Before merging, rebasing, or deleting an anchor you did not create, run
`workstream show <ws>` for the workstream that holds it and look at
**sessions**: a live canonical session there owns that anchor. Message it or
ask the user before acting.

## Resuming from a frontier

Run `workstream frontier <ws>` for orientation; it is concise (subagent
counts, one status word per anchor), and `frontier --full` is only for when a
loose end needs the timeline. With `--full` it prints, for the humans and for you:

- each session (canonical first, subagents under their parent, Codex marked)
  with its last human prompt, last assistant text, last tools, lifecycle
  (how it ended, or that it was dropped), peak context, and the context
  since its last compaction;
- anchors with **read-time liveness**: a worktree that still exists with an
  unmerged commit, a branch with no remote, a PR whose review changed after
  the last session saw it;
- cross-session messages in and out;
- a UTC timeline (`--local` for each event's own zone);
- loose ends, and the **options**: message the newest canonical session
  (only while it runs), start fresh, or resume its transcript, each with the
  facts that bear on it, and one marked default with its reason.

The choice is yours; the default is a lean, not an instruction. The house
style, which the default follows:

- **Start fresh about 90% of the time.** Attach, read the prose frontier the
  fresh option names (if it exists) and the newest capture, then the loose
  ends, and read the previous session selectively (`claude-ls summary
  <sid8>`, or the lines the frontier cites) instead of loading all of it. Do
  not re-derive state by searching transcripts when the frontier already
  names the session and line.
- **Resume only a session whose post-compaction context is small and whose
  cache is still warm**: the resume option says whether its context since the
  last compaction is within `RESUME_MAX_CONTEXT` and whether it is idle less
  than `CACHE_TTL`. Past either, a resume pays to re-read what a fresh start
  would skip.
- **A live session is messaged, never resumed**: resuming it would drive one
  transcript from two processes. The message option gives its name.

A session that starts already attached gets a one-line frontier per
workstream from the SessionStart hook; run `workstream frontier <name>` for
the anchors, options and the rest.

## Writing the prose frontier (D3c)

The CLI records evidence; a person steering the team reads prose. When you
finish a stretch of work, or when the user asks "where are we", write or
update `project-docs/docs/<slug>/workstreams/<name>.md`:

- **State** in two or three sentences: what is done, what is in flight, what
  is blocked and on whom.
- **Loose ends** as a list, each with its anchor (`gh:o/r#N`, `wt:<path>`)
  and the session that left it (`claude:<sid8>`), citing the capture or the
  transcript line it came from.
- **Next** one line: the single next action, and who should take it.

Cite the machine evidence (`workstream frontier --record` writes a capture and
prints its path) rather than restating it. Commit project-docs on `main` and
push, per that repo's rule.

## Retiring sessions

Before a batch shutdown: `workstream retire <sid>...`. It captures a frontier
for every workstream each session is attached to and releases the session's
claims. A session with no recorded frontier counts as **dropped** no matter
how it ended, so this is the step that makes the shutdown clean. Then write
the prose frontier if anything changed that a reader would need.

A SessionEnd hook also records a capture for every workstream an ending
session is attached to, marked `recorded_by: session_end_hook`, when nothing
newer covers the session's last activity. It is a backstop, not a hand-off:
it never releases claims, never calls gh, and writes no prose. When the
SessionStart line says the newest capture came "by the SessionEnd hook", the
prose frontier may be behind it; run `workstream frontier <name>` before
trusting the prose. To hand work off deliberately, still `retire`.

## Tasks and closing

Tasks are weft nodes `part-of` the workstream, in the `workstreams`
namespace (`weft add "<title>" --ns workstreams`, then `weft link <task>
part-of <ws>`). Anchors stay refs, never part-of. `weft ls --ns workstreams
--ready` is the task frontier across every workstream; `workstream show`
lists one workstream's tasks with state and blocked-ness.

When the frontier says "all tasks done; workstream not closed", run
`workstream close <ws> --note "<why>"`. It refuses while a session still
holds a claim (`workstream retire` it first), tasks are open, or loose ends
remain, and lists them; fix those, or pass `--force` only when the
user agrees the rest does not matter. A closed workstream cannot be attached
to; start a new one rather than reopening.

## Commands

```
workstream match [--session SID]           ranked candidates with reasons
workstream attach <ws> [--ancillary] [--anchor REF]... [--no-sync]
workstream attach --new "<title>" --name <slug> [--anchor REF]...
workstream attach --none "<reason>"
workstream sync [ws...] [--session SID] [--ancillary] [--dry-run]
workstream show <ws> [--local]
workstream frontier <ws> [--record] [--full] [--local]
workstream retire <sid>...
workstream close <ws> [--note TEXT] [--force]
workstream ls [--since 7d] [--all]
```

Refs: `claude:<sid>` · `agent:<parent>/<id>` · `codex:<id>` · `gh:o/r#N` ·
`branch:<repo>@<name>` · `wt:<path>` · `file:<path>`. Every command takes
`--json`.

## Traps

- **cwd and project slug lie.** A session launched in one repo can do all its
  work in another. Location evidence comes from where tools acted; do not
  attach by directory.
- **`gh` says every agent's PR is the user's.** Ownership comes from the
  workstream's sessions, never from the PR author.
- **A session is a lineage.** Forks, `/clear`, and compaction mint new ids.
  `show` folds them; attach the id you are running as, and the chain follows.
- **Subagents inherit.** They attach under their parent automatically; they
  matter for "who changed this file", not for messaging.
- **Three clocks.** Transcripts are UTC, hooklog is naive local across zone
  changes, claude-ls prints local. The CLI normalizes to UTC with each event's
  offset kept; quote its timestamps, not the raw ones.
