# workstream

A durable identity for a piece of work across Claude Code sessions, their
subagents, Codex rollouts, and the PRs, branches, worktrees and docs they
touch. It answers "what is the state of W, who worked on X and when, where did
it stop" from what the tools already record, so a fresh session does not
rebuild that by searching transcripts and two sessions do not take the same
PR.

weft holds the identity: a `workstream` node whose refs are its sessions and
anchors. This tool reads the transcript store, git and, when present, the hook
log for the evidence, and writes prose nowhere: the frontier a person steers by
lives in project-docs and is written by an agent following the `workstream`
skill.

```
workstream match   [--session SID]                 ranked candidates, with the evidence
workstream attach  <ws> [--ancillary] [--anchor REF]... [--no-sync]
workstream attach  --new "<title>" --name <slug> [--anchor REF]...
workstream attach  --none "<reason>"               this session is not a workstream
workstream sync    [ws...] [--session SID] [--ancillary] [--dry-run]   anchors from what owning sessions wrote to
workstream sync    --interactive [ws...] [--verbose]   review each change before it is written
workstream show    <ws> [--local]                  sessions, anchors, messages, UTC timeline
workstream frontier <ws> [--record] [--full] [--local]   concise state, loose ends, ways to pick it up
workstream retire  <sid>...                        capture the frontier, release the claims
workstream hook    [--event SessionStart|SessionEnd]   hook bodies (below); stdin payload, JSON out
workstream close   <ws> [--note TEXT] [--force]    mark it done once nothing is left open
workstream ls      [--since 7d] [--all]            --all includes closed workstreams
```

Every command takes `--json`. Refs: `claude:<sid>` · `agent:<parent>/<id>` ·
`codex:<id>` · `gh:o/r#N` · `branch:<repo>@<name>` · `wt:<path>` ·
`file:<path>` · `frontier:<path>`.

## Setup

Four pieces, and an optional fifth, from the agent-tools checkout you run tools
from (the read-only clone, where there is one):

1. **`weft` on PATH**, built from [mparrett/weft](https://github.com/mparrett/weft).
   Everything here reads and writes the weft store. Every build of the public
   repo exports released claims; a weft from before it went public is not
   refused, but its export carries none, so attach intervals have no end and
   the claim-window scoping of `sync`, `show` and `frontier` silently covers
   whole sessions.
   Install with `make install` from a clone, which stamps the version that
   `weft help | head -1` prints (`go install` leaves it at `0.0.0-dev`).
2. **The command**: `just link ~/bin` symlinks `bin/workstream` (and every
   other tool in `bin/`) into `~/bin`; it skips what is already linked.
3. **The hooks**, in `~/.claude/settings.json`. Both are stdlib-only
   scripts run by whatever `python3` the hook's PATH finds (no uv), and both
   exit 0 on any error so a broken store never blocks a session:

   ```json
   "SessionStart": [{"matcher": "*", "hooks": [{"type": "command",
     "command": "<checkout>/workstream/session_start_hook.py", "timeout": 5}]}],
   "SessionEnd":   [{"matcher": "*", "hooks": [{"type": "command",
     "command": "<checkout>/workstream/session_end_hook.py", "timeout": 25}]}]
   ```

   SessionStart prints the candidate block the skill acts on; SessionEnd
   records a frontier capture for a session that forgot to.
4. **The skill**: `just link-skills ~/.claude/skills` links
   `claude-code-skills/workstream`, which tells a session when to attach and
   how to resume from a frontier.
5. **Optional: a hook log**, from
   [mparrett/claude-hook-logger](https://github.com/mparrett/claude-hook-logger),
   registered on every hook event. It appends each event to
   `~/logs/claude-hooks/hook_events.log`, which workstream reads for evidence
   the transcripts don't hold: worktrees a session entered, its starts, ends
   and compactions, and messages it exchanged with other sessions.

   Without it nothing fails and nothing warns, but the evidence is thinner.
   `sync` misses worktree and branch anchors that only the log recorded: a
   worktree made by `just wt`, or an EnterWorktree whose path the transcript
   does not carry. `match` ranks on fewer reasons, and `frontier` lists fewer
   loose ends.

Check it with `workstream ls` (lists open workstreams, or nothing on an empty
store) and by starting a session: the candidate block appears under
SessionStart. Stores and their overrides are listed below.

## How a session gets attached

A SessionStart hook runs `workstream hook`, which prints the top candidates
for the starting session with the reasons and the exact attach lines. A
session that is already attached (resumed, or continuing after a compaction)
gets one line per workstream instead: open and blocked tasks, loose ends
known without probing anchors, the newest capture's age, and a pointer to
`workstream frontier <name>` for the rest. A session recorded as
unassociated with no candidates gets nothing.
Register it in `~/.claude/settings.json` with an absolute path:

```json
"SessionStart": [{"matcher": "*", "hooks": [{"type": "command",
  "command": "/abs/path/to/agent-tools/workstream/session_start_hook.py", "timeout": 5}]}]
```

A SessionEnd hook records the frontier the session forgot to. For each
workstream the ending session is attached to, closed ones included, it writes
the same capture `frontier --record` would, marked `"recorded_by":
"session_end_hook"`, unless a capture already covers the session: one recorded
on purpose with no prompt after it, or the hook's own with no activity after
it. It stops starting captures after 17 s, canonical workstream first, and
lists the rest as skipped. It records and never releases: a `/clear` or a
restart usually continues the same work, so claims stay held. `retire` is
still how a session hands off on purpose. The hook never calls gh (PR anchors
come from the gh cache or are marked `not_probed`) and skips the 14-day scan
for foreign merges, so a manual `frontier --record` captures more. The start
hook's line says when the newest capture came from this hook.

```json
"SessionEnd": [{"matcher": "*", "hooks": [{"type": "command",
  "command": "/abs/path/to/agent-tools/workstream/session_end_hook.py", "timeout": 25}]}]
```

The ranking is evidence-first. A pasted session id, a shared PR, a branch or
a worktree name the work directly. Where tools acted (`git -C`, edited
paths, `gh --repo`) says which repos a session touched. Shared files count
for almost nothing, because scaffolding files bridge every session on the
machine into one blob, and the launch directory counts for nothing on its
own: a session launched in one repo can do all its work in another.

A session has one **canonical** workstream at a time (a live weft claim held
by `agent:<session ref>`) and any number of **ancillary** ones (a ref only).
Subagents attach under their parent. Attaches are timestamped, so attribution
is per interval; a session switches canonical by attaching again, which
releases its claim on the previous workstream and says so. `show`
lists each canonical interval under its session, ending where weft recorded
the release (any build of the public weft repo; pre-public builds drop released claims).

**A session counts only inside its canonical interval.** When a session
releases one workstream and attaches to another, each gets what the session
did while attached there: `sync` takes only the writes timestamped inside the
interval, and `show`, `frontier`, the SessionEnd hook's capture and `retire`
list only the prompts, tools, subagents, messages and timeline rows inside
it. The session row keeps its full id and peak context, and spans the
interval rather than the whole transcript. An interval starts at the claim but
reaches back over the gap before it: to where the session's previous claim
on any workstream ended (its release, or its expiry if it lapsed), or to the
session's start if it never claimed one, since what a session did before `attach --new` is usually the
work it then names. An ancillary session (ref only, no claim) has no
interval, so all of it still counts where it shows up.

## How anchors accrue

`--anchor` names an anchor by hand; `sync` adds the ones a session's own tools
wrote to, so a workstream does not miss the PR its session opened an hour
after attaching. For each session that owns a workstream (every open
workstream by default, or those named) it adds:

- `gh:o/r#N` for a PR the session opened, merged, reviewed, commented on,
  edited or checked out with `gh pr`, or reviewed or commented on with a
  writing `gh api .../pulls/N/...` call. A bare number resolves through the
  directory's `origin`.
- `gh:o/r#N` for a PR named (URL or `o/r#N`) in a prompt, assistant text or
  shell command in the transcript tail, only when the session also wrote to
  `o/r`. Tool output does not count (`gh pr list` prints PRs nobody touched),
  nor do file bodies written by Edit or Write (a CHANGELOG names every PR).
- `branch:<repo>@<name>` for a branch it committed on or pushed, read from
  git's own output (`[branch sha]`, `a -> b`), or from the push's refspec
  when the output is quiet or piped away. Default branches are skipped,
  and so is a push git rejected or that only deleted a branch.

Commands are read one simple command at a time: a Bash call is split on
`;`, `&&`, `||`, `|` and newlines, heredoc bodies are dropped (a `cat >
notes <<EOF` that mentions `git push` is a file, not a push), and
redirections such as `2>&1` are not arguments. Wrappers (`do`, `!`, `time`,
`timeout 60`, `env VAR=x`) and gh's global `--repo X` before the subcommand
do not hide the command. When a push's output was piped through something
that can cut it (`| tail -1`), the refspec counts only if what survived is
empty or the `set up to track` message; anything else is inconclusive and
adds nothing. Every branch and repo name
must look like one (`git check-ref-format --branch` rules; `owner/name`)
or it is dropped, so a misread fragment never becomes an anchor.

**Ancillary means referenced, not owned; its evidence is not the
workstream's.** A session owns a workstream when it holds or held a
canonical claim on it, released claims included; its subagents count with
it. An ancillary session helped here, and the rest of what it did that
afternoon is other work, so sync leaves it out and says how many it left
out. `--ancillary` includes every ancillary session; `--session SID` syncs
that one session alone, whatever its role.
- `wt:<path>` for a worktree it created, entered, edited in, committed or
  pushed from, while the worktree still exists.

Repo overlap, shared files and the launch directory rank candidates in
`match` and never become anchors. A ref the workstream already has is
skipped, `wt:` compared by real path. So is a ref another open workstream
holds: the session acted on it for that work. A session attached to
several workstreams syncs into its canonical one first. Each added anchor
prints with the session and the writes that justified it; `--dry-run`
writes nothing, and `--json` gives `{added, skipped, by_session, ancillary,
retracted}`.

Sync also retracts a `wt:` anchor whose directory is gone and whose branch
merged: a slice retires its worktree after the merge, so the worktree is
history, and the captures already written keep it. The branch comes from
what the sessions did there (the last commit, the `EnterWorktree` or `just
wt` that made it, a lone push), else from a branch or PR anchor named like
the directory. `frontier` and `show` read the same way: such a worktree
shows `gone (branch <b> merged)` and is not a loose end, so it no longer
stops `close`. A gone worktree whose branch did not merge, or whose branch
nothing names, stays a loose end. A PR whose head shares the name counts only
if it merged after the last work in the worktree; with no session evidence
to date that work, the name alone settles it for `frontier` (`merged, by
branch name only`) but sync keeps the anchor, since a reused branch name
could be hiding work that never landed.

`attach` runs the same sync for the attaching session once a canonical
attach lands, unless `--no-sync`. `attach --ancillary` never syncs. The SessionStart hook
does not: it has a 4 s budget and sync reads whole transcripts.

`show` and `frontier` mark a synced anchor `(sync)`. weft's ref `source`
allows only `observed`, `asserted` and `inferred`, so sync writes
`inferred`, which nothing else here writes, and `show` reads it back from
weft's event log, since no weft command returns a ref's source. A synced
anchor stays marked until it is removed and asserted again; asserting a ref
a node already has records nothing in weft.

## The frontier

`frontier` is concise by default, since its output usually lands in the next
agent's context: sessions with their canonical intervals and a subagent
count, one line per anchor with a status word, tasks, loose ends and options.
A sample, with paths shortened:

```
$ workstream frontier spool-cache
spool-cache  Cache spools between runs  [open]
  id 85c5202ea029  created 2026-11-03 20:57Z  touched 2026-11-03 20:57Z

sessions
  claude:72a6b5a1-…  canonical  SessionStart(startup)  2026-11-03 20:57Z → 2026-11-03 20:58Z  ctx 118,000  no frontier recorded
      canonical 2026-11-03 20:57Z → held (expires 2026-11-10 20:57Z)
      ↳ 4 subagents
  claude:f91a9130-…  ancillary  SessionStart(startup)  2026-11-03 18:07Z → 2026-11-03 20:58Z  ctx 60,000  no frontier recorded

anchors
  branch:spindle@spool-cache  unpushed
  gh:acme/spindle#52 (sync)  unknown
  wt:/work/spindle/.claude/worktrees/spool-cache  dirty

loose ends
  - branch spindle@spool-cache has no remote copy
  - gh:acme/spindle#52: neither gh pr view nor gh issue view answered
  - worktree /work/spindle/.claude/worktrees/spool-cache: 1 commit(s) not on main, uncommitted changes, no upstream
  - 72a6b5a1 (canonical) has no recorded frontier; last SessionStart(startup)
  - f91a9130 (ancillary) has no recorded frontier; last SessionStart(startup)

options (default: resume, because its context is within the resume cap and the cache is still warm)
  fresh: start a new session, `workstream attach spool-cache`, read the prose frontier and the newest capture, …
  resume: cd /work/spindle && claude --resume 72a6b5a1-…
      - peak context 118,000 (never compacted)
      - within the resume cap (RESUME_MAX_CONTEXT 150,000)
      - idle 0m, within CACHE_TTL 1h00m: the cache is warm
```

The status words are `merged`, `open`, `draft`, `closed` for a PR;
`gone (merged)`, `gone`, `dirty`, `unpushed`, `unmerged`, `merged`, `exists`
for a worktree or branch; `unknown` when nothing was probed or gh did not
answer. Where several apply, the worse news wins.

`frontier --full` adds what `show` prints: per-subagent rows, last prompts,
messages, the UTC timeline and each anchor's raw liveness. Reach for it when a
loose end needs the timeline. In full, `frontier` gives, per session: role, how it ended or that it is live, peak
context, compactions and the context since the last one, last prompt, last
assistant text, last tools. Per anchor, liveness
computed now: a worktree's unmerged or unpushed commits, a branch with no
remote copy, a PR's state and last reviews. Cross-session messages with
recipients resolved to session ids. A merged timeline in UTC, with each
event's own zone kept (`--local` to see it). Loose ends. And the
**options** for picking it up, with the facts for each: **message** the newest
canonical session while it runs, start **fresh** from the prose frontier and
newest capture, or **resume** its transcript, with the context a resume would
load (after the last compaction, since that is where Claude Code resumes
from), its idle time against `CACHE_TTL`, and whether it is within
`RESUME_MAX_CONTEXT`. One option is marked the default, with a one-clause
reason; the reader decides. `--json` keeps the older `recommendation` key,
derived from that default.

`--record` writes the evidence as JSON under
`project-docs/docs/<slug>/workstreams/<name>/captures/` and binds it to the
workstream as a `frontier:` ref. A session with no capture naming it is
**dropped**, regardless of how it ended; `retire <sid>...` is what makes a
batch shutdown clean, and the SessionEnd hook is the backstop for a session
that ends without either. Each capture's `recorded_by` says which path wrote
it.

## Tasks and closure

A workstream's tasks are weft nodes `part-of` it, in the `workstreams`
namespace; add them with weft (`weft add "<title>" --ns workstreams`, then
`weft link <task> part-of <ws>`). Anchors stay refs. weft's `ls --ready`
skips workstreams because they are containers, so
`weft ls --ns workstreams --ready` is the task frontier across every
workstream. `show` and `frontier` list each task with its state, open first,
and whether weft reports it blocked.

`frontier` adds a loose end while any task is open ("2 tasks open (1
blocked)"), and "all tasks done; workstream not closed" once every task is
done or dropped. `close` marks the workstream done and releases its claims.
It refuses while a session still holds a canonical claim (`retire` it
first), a task is open, or any loose end other than a session with no
recorded frontier remains; `--force` closes anyway. `--note` is kept as a
note derived from the workstream and shown by `show`. A closed workstream
keeps its refs, drops out of `ls`, `match` and the hook, and `attach` refuses
it; `retire` still finds it, so its sessions can record a frontier. Reopening is not a verb here: `weft done --undo <id>` does it by hand.

## Three clocks

Transcripts are UTC. The hooklog is naive local time, and the machine's zone
changed four times in the two weeks that were mined. claude-ls prints local.
Everything here is an `Instant`: UTC plus the offset in force when the event
happened, inferred per hooklog event by pairing session starts with their
transcript records and rounding to 15 minutes.

## Stores and overrides

| What | Default | Override |
|---|---|---|
| Claude transcripts | `~/.claude/projects` | `WORKSTREAM_PROJECTS_DIR` |
| Codex rollouts | `~/.codex/sessions` | `CLAUDE_LS_CODEX_DIR` |
| Session registry | `~/.claude/sessions` | `CLAUDE_LS_SESSIONS_DIR` |
| hooklog | `~/logs/claude-hooks` | `CLAUDE_HOOK_LOG_DIR` |
| weft | `~/.weft` | `WEFT_HOME`, binary via `WORKSTREAM_WEFT_BIN` |
| project-docs | `~/projects-new/project-docs/docs` | `WORKSTREAM_DOCS_ROOT` |
| gh cache | `$XDG_CACHE_HOME/workstream` | `WORKSTREAM_CACHE_DIR` |
| weft actor | `agent:claude:<sid>` inside a Claude Code session (`CLAUDE_CODE_SESSION_ID`), else weft's git email | `WEFT_ACTOR` |

Missing stores degrade to warnings. Only the hook swallows errors outright.

## Known limits (v1)

- Pulse claims are not read. Ownership comes from workstream membership only.
- `branch:` and `wt:` liveness never fetch. "Merged" is reachability first;
  failing that, GitHub is asked for a merged PR by head branch (`gh pr list
  --head`), and failing that, a branch whose merge into the default branch
  would change nothing counts as landed. So a squash-merged branch reads as
  merged once GitHub or the tree test says so.
- The design contract is `DESIGN.md`; the spec it implements lives in
  project-docs.

## Development

```
uv run --directory workstream pytest -q
```

Tests build a synthetic store (`tests/synthetic.py`) with invented names;
nothing from a real session store is committed. Tests that need weft skip
when the installed `weft` does not accept `--kind workstream`, and the
released-claim round trip skips when its export has no `released_claims`.
