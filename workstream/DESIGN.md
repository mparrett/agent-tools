# workstream — design contract (v1)

Spec: `project-docs/docs/agent-tools/2026-11-02-workstream-continuity-spec.md`
(decisions D0–D14). This file is the build contract: module boundaries, data
shapes, and the rules every module shares. Where this file and the spec
disagree, the spec wins and this file is wrong.

## Shape

`workstream/` is a package with a `pyproject.toml`, like `claude-graph`: it
reads `claude-ls.py` through an import seam, so it cannot be a single script.
`bin/workstream` is a wrapper. Dependencies: `rich` only. Python ≥ 3.11.

```
workstream/
  pyproject.toml
  workstream/
    __init__.py
    __main__.py
    seam.py          # the ONLY module that knows how to reach claude-ls
    clock.py         # D0: UTC-with-offset instants; hooklog offset inference
    hooklog.py       # hook_events.log reader: lifecycle, SendMessage/ListAgents
    sessions.py      # SessionFacts for Claude main, subagent, Codex transcripts
    anchors.py       # D7: anchor refs and read-time liveness
    store.py         # D2: weft adapter (shell out to `weft --json`)
    attribution.py   # D4/D9/D12: candidate ranking, attach intervals
    sync.py          # anchors from the writes a session's tools made
    frontier.py      # D3a/b + D11: evidence, timeline, options, capture, hook brief
    cli.py           # match / attach / sync / show / frontier / retire / close / ls / hook
  session_start_hook.py   # stdlib-only SessionStart hook (D8); execs `workstream hook`
  session_end_hook.py     # stdlib-only SessionEnd hook; execs `workstream hook --event SessionEnd`
  _hook_common.py         # what both wrappers share: find `workstream`, run it, the test seams
  tests/
    conftest.py      # synthetic store builder (see Fixtures)
    test_*.py
  README.md
```

## Shared rules

- **Time (D0).** Every timestamp crossing a module boundary is a
  `clock.Instant`: `utc: datetime` (tz-aware, UTC) plus `offset: timedelta`
  (the local offset in force when the event happened). Transcript records carry
  `Z` timestamps: offset is unknown from the record alone and is taken from the
  hooklog offset timeline when one exists, else from the host's current zone.
  hooklog timestamps are naive local; `clock.OffsetTimeline` infers the offset
  per event (below). Rendering is UTC by default; `--local` renders with each
  event's own offset.
- **Session identity.** A session id is a Claude session uuid, a Codex rollout
  id, or a subagent id. A session is a lineage, not an id: `sessions` exposes
  `origin` (the root it was `/clear`ed from) and `fork_of` when known, but
  every fact is recorded against the concrete id. Prefixes resolve like
  claude-ls (8+ hex chars).
- **Refs.** `scheme:value`, as weft takes them:
  `claude:<sid>` · `agent:<parent-sid>/<agent-id>` · `codex:<rollout-id>` ·
  `gh:<owner>/<repo>#<n>` · `branch:<repo-root-basename>@<branch>` ·
  `wt:<abs-path>` · `file:<abs-path>` · `frontier:<abs-path>` ·
  `ws:<name>`. `wt:` is any workspace: linked worktree, temp clone, scratch dir.
- **Nothing real in the tree (D14).** Tests and fixtures use the synthetic
  builder. No real session ids, repo names, people, or home paths in any
  committed file, PR body, or issue.
- **Degrade, never fail the session.** Missing weft, gh, hooklog, Codex store,
  or Pulse: every reader returns empty with a one-line `warnings` entry. Only
  the `hook` subcommand swallows exceptions entirely (exit 0, stderr).
- **Read-time, not stored (D7).** Liveness and external state are computed
  when asked. weft holds identity and membership; project-docs holds prose and
  captures.

## Modules

### seam.py
Loads `claude-ls.py` by path (`CLAUDE_LS_PATH`, then `../../claude-ls/claude-ls.py`,
then walking up from cwd) exactly like `claude_graph/sessions.py`. Re-exports:
`load_raw_entries, extract_session_metadata, derive_work_signals, WorkSignals,
session_chain, session_origin, is_wrapper_text, user_prompt_text,
_extract_text as extract_text, _tail_entries as tail_entries,
_repo_root_for as repo_root_for, _canonical_repo_root as canonical_repo_root,
_git_linked_worktree_main as linked_worktree_main, list_projects, list_sessions,
_file_is_codex as file_is_codex, codex_sessions_dir, iter_codex_session_refs,
codex_project_key, _live_registry as live_registry, _EDIT_TOOLS as EDIT_TOOLS,
_PR_URL_RE as PR_URL_RE, _parse_ts as parse_ts`.
Store locations: `projects_dir()` honours `WORKSTREAM_PROJECTS_DIR` then
`~/.claude/projects`; Codex via `CLAUDE_LS_CODEX_DIR`; registry via
`CLAUDE_LS_SESSIONS_DIR`; hooklog via `CLAUDE_HOOK_LOG_DIR`; weft via
`WEFT_HOME`. Tests set all five.

### clock.py (D0)
```
@dataclass(frozen=True) class Instant: utc: datetime; offset: timedelta
  .local -> datetime (utc + offset, tz=timezone(offset))
  .iso() -> "2026-10-29T12:58:02Z+-05:00"?  NO: render as "2026-10-29T12:58:02Z (07:58 -05:00)"
parse_transcript_ts(s: str, offset_hint: Instant|None) -> Instant
class OffsetTimeline:
    samples: list[(naive_local: datetime, offset: timedelta)]   # sorted
    @classmethod from_pairs(pairs: Iterable[(naive_local, utc)])  # offset = round((naive - utc_naive) to 15 min)
    offset_at(naive_local) -> timedelta   # nearest sample; host offset when empty
    instant(naive_local) -> Instant
```
Pairs come from `hooklog.pair_with_transcripts()`: for each hooklog
`SessionStart` (and `UserPromptSubmit`) event, the transcript record of the
same session with the nearest timestamp within 120 s of some candidate offset
in {-12h..+14h step 15m}. One pair per session segment is enough; the mining
doc measured 117 such samples exactly to the minute.

### hooklog.py
Reads `$CLAUDE_HOOK_LOG_DIR/hook_events.log`, `.1`, and `.N.gz` (newest
first, stop when a file is wholly before `since`). Yields
```
HookEvent(at_local: datetime(naive), at: Instant|None, session_id, cwd, event,
          source|reason, tool_name, tool_input: dict|None, tool_response: dict|str|None,
          agent_type, transcript_path)
```
Derived views:
- `lifecycle(session_id) -> list[HookEvent]` SessionStart/SessionEnd/PreCompact,
  and `last_lifecycle(session_id)`.
- `messages(since) -> list[MessageEvent]` from PreToolUse+PostToolUse
  SendMessage: `(at, from_sid, to_raw, summary, body, to_sid|None, how)`.
  Recipient resolution (D5, at read time): exact body match against incoming
  peer records in any transcript (`type=user, isMeta, origin.kind=="peer"`,
  `origin.body == body`, `origin.name == sender's name`), within ±10 min; else
  the live registry (`name`, `formerNames[].name` with `until`, or
  `messagingSocketPath == uds:…`) ; else unresolved with `to_raw` kept.
- `list_agents_calls(since)` — PreToolUse ListAgents, one event each.
- `workspace_events(session_id)` — EnterWorktree/ExitWorktree tool_input +
  tool_response (`worktreePath`, `worktreeBranch`), and Bash commands matching
  `git worktree add|remove`, `just wt `, `wt new|rm`.
`segments(session_id)` — (start, end|None, source, reason) per SessionStart.

### sessions.py
```
SessionFacts(
  id, kind: "claude"|"subagent"|"codex", path, parent_id|None,
  project_dir, cwd, started: Instant, last_seen: Instant,
  origin|None, chain: list[str],           # lineage via seam.session_chain
  fork_siblings: list[str],                # same first prompt within 60 s, same project
  model, peak_context: int,                # max over assistant usage of in+cache_read+cache_create
  compaction_count: int,                   # compact_boundary records, plus any isCompactSummary not right after one
  context_after_compaction: int|None,      # peak over assistant records after the last compaction, else the boundary's
                                           # compactMetadata.postTokens; None if neither; = peak_context if never compacted
  work: WorkSignals,                       # repos by edit, prs, branches, edited_paths
  repos: list[str],                        # canonical roots from edits AND `git -C`, `gh --repo`, `cd` in Bash (D4 location)
  pasted_sessions: list[(sid, Instant)],   # uuids/8+hex prefixes in HUMAN prompts that resolve to a known session
  worktrees: list[str],                    # paths from hook workspace_events + Bash
  worktree_branches: dict[str, str],       # worktree path (or bare `just wt` name) -> the branch its
                                           # EnterWorktree / `just wt` creation named
  handoff_files: list[str],                # edited paths with "handoff" in the name
  human_prompts: list[(Instant, str)],     # wrapper-filtered, last 12
  last_assistant: (Instant, str)|None,     # last text block, 600 chars
  last_tools: list[(Instant, name, one-line summary)]   # last 10 tool uses
  subagents: list[str],                    # agent ids under <project>/<sid>/subagents
  acted: list[Acted(kind, value, how, at, dir, repo)],   # writes only, full pass only (see sync.py)
)
iter_sessions(since: Instant|None, include_codex=True) -> Iterator[SessionFacts]   # cheap head/tail pass
facts(sid_or_prefix) -> SessionFacts                                             # full pass
resolve(prefix) -> str  (raises Ambiguous/NotFound)
```
Subagent transcripts: `<project>/<parent-sid>/subagents/agent-<id>.jsonl` with
`.meta.json` beside it. Codex rollouts via the seam's codex helpers; a rollout's
`parent_thread_id` (guardian subagents) sets `parent_id`.

### anchors.py (D7)
```
Anchor(ref: str, scheme, value, liveness: dict, warnings: list[str])
liveness(ref, gh_cache_only=False) -> Anchor   # computed now; each probe has a 5 s timeout and never raises
```
`gh_cache_only` (also on `liveness_many` and `frontier.build`) answers `gh:`
refs and the merged-PR lookup from the cache at any age and never spawns gh;
a miss leaves `liveness.not_probed` set rather than a warning, since warnings
become loose ends. The SessionEnd hook uses it.
- `wt:` → `exists`, `dirty`, `branch`, `upstream`, `unpushed` (count, or
  "no remote"/"no upstream"), `unmerged` (commits not in the main checkout's
  default branch), `merged_into_default`, `merged_via`, `main_checkout`.
- `branch:` → `exists_local`, `exists_remote`, `merged_into_default`, `merged_via`.
- `merged_into_default` for both: reachability; else `gh pr list --state merged
  --head <branch>` on the `origin` repo (cached like `gh:`), giving
  `merged_via: {pr, merged_at, commit}`; else `git merge-tree --write-tree`
  against the default branch, where an unchanged tree gives `merged_via:
  {tree: true}`. A PR is credited only when its `headRefOid` is a local commit
  the branch tip is at or before; otherwise, and when gh fails or there is no
  GitHub remote, the tree test still decides. A merge conflict or a differing
  tree leaves it false. The frontier drops a merged worktree's commit and push
  loose ends. A `branch:` whose tip is gone locally and on the remote (deleted
  after its merge) has nothing to check a PR's head against, so a merged PR
  from that head alone decides, marked `merged_via.head_only`; none leaves it
  None.
- `gh:` → via `gh pr view --json state,isDraft,reviewDecision,updatedAt,reviews(last 3: author,state,submittedAt),mergedAt,headRefName`
  cached for 10 min under `$XDG_CACHE_HOME/workstream/gh/`; `kind: issue` when
  pr view 404s and `gh issue view` succeeds.
- `file:`/`frontier:` → `exists`, `mtime`.
- `claude:`/`codex:`/`agent:` → `live` (registry), `last_seen`, `peak_context`.

### store.py (D2)
Shells out to `weft` with `--json` (`WORKSTREAM_WEFT_BIN` overrides the binary; `WEFT_HOME` passes through). Every call
has a 10 s timeout. Exit codes map: 2 → NotFound, 4 → Ambiguous, 7 →
ExternalFailed, 6 → StoreError (raise), else WeftError.
```
NS = "workstreams"
create(name, title, anchors: list[str], source="asserted") -> Workstream
   # weft add "<title>" --kind workstream --ns workstreams --ref ws:<name> [--ref anchor]...
get(name_or_prefix, nodes=None) -> Workstream   # resolves ws:<name>, prefix, or id; nodes: a shared export_nodes()
export_nodes() -> dict[id, node]   # one `weft export --include claims`, for callers resolving several workstreams
list(since=None, include_closed=False) -> list[Workstream]   # closed = weft state done; retire passes True, match/hook do not
attach(ws, session_ref, canonical: bool, note) :
   # membership: weft ref add <ws> <session_ref>
   # canonical:  weft claim <ws> --holder agent:<session_ref> --note "<note>" --source observed
   # ancillary:  ref only
detach(ws, session_ref): weft release <ws> (holder match) ; keep the ref
add_anchor(ws, ref, source="asserted") / rm_anchor   # weft ref add|rm; anchors are refs (weft's part-of is a single-parent forest)
SYNC_SOURCE = "inferred"   # how sync writes; weft's source enum has no `sync`
ref_sources(ws) -> dict[ref, "sync"|source]   # read from weft's event log (EVENTS.md), the only place a ref's source survives
record_unassociated(session_ref, reason):
   # weft add "unassociated: <reason>" --kind note --ns workstreams --ref <session_ref> --source asserted; weft done <note>
   # (done at once, like the closure note: an open note is listed by `weft ls --ready` as work)
unassociated(session_ref) -> str|None
record_frontier(ws, capture_path): weft ref add <ws> frontier:<path> --source observed
blocked_ids() -> set[str]   # weft ls --all-ns --blocked
close(ws, note=None) -> Workstream   # a resolved Workstream is used as given; no re-resolve
   # release every live claim; note → weft add "closed: <note>" --kind note, link derived-from <ws>, done;
   # then weft done <ws>
Workstream(id, name, title, state, created: Instant, touched: Instant,
           refs: list[str], claims: list[Claim(holder, at, expires, note, released_at)],
           children: list[Child(id, title, kind, state)],   # every part-of child, kind/state from one export; anchors are refs, never part-of
           closed_note: str|None,
           released: list[Claim])   # released claims, oldest first; `claims` stays the held and expired ones
   .tasks (children whose kind is not workstream) · .sub_workstreams · .closed (state == done)
   .intervals() -> list[AttachInterval(holder, at, expires, released_at, note)]
attach_intervals(ws) -> list[AttachInterval]   # weft export --include all
Window = (start: datetime|None, end: datetime|None)   # UTC; None start = session start, None end = open
session_windows(session_ref, ws, history) -> list[Window]|None   # None: never canonical on ws
in_windows(at, windows) -> bool   # None windows: everything; at None: outside a scoped session
```
Canonical-vs-ancillary (D12): canonical = a live weft claim held by
`agent:<session_ref>`; ancillary = ref only. Attach intervals are claims:
held and expired ones from export's `claims`, released ones from its
`released_claims` (every build of the public weft repo), each ending at its
`released_at`. `show` never lists released claims, so `get` reads them from
the same export that answers its children's kinds. A weft without the array
loses released claims entirely; their intervals are missing, the only case
where a caller must close an interval itself. `frontier` gives each session
the intervals whose holder is `agent:<its ref>`, the same match `role` uses.

**Canonical intervals scope what a session contributes.** A session that
holds or held a canonical claim on a workstream counts there only inside its
windows (`session_windows`), one per claim: from the claim to its release,
or to its expiry when it lapsed without one, or open while held. A window
reaches back over the gap before its claim, to where the session's previous
claim on any workstream in `history` (closed ones included) ended, or to the
session's start when it never claimed before. A canonical `attach` releases
the session's other held canonical claims first (D12: one canonical
workstream at a time), so the previous claim has always ended; a held one
can only be old data, and then the window starts at its own claim. The
work before an attach is usually what the attach names (a session explores,
then runs `attach --new`), and the earlier claim already accounts for
everything before it. Without the reach-back, `attach`'s own sync would see
nothing, since it runs right after the claim. A subagent counts in its
parent's windows. An **ancillary** session (ref only, no claim) has no
interval and counts whole, as before: there is nothing to scope it by, so a
helper's unrelated work from the same transcript still shows in `show` and
`frontier` (not in `sync`, which leaves ancillary sessions out by default).

### attribution.py (D4, D9, D12)
```
Candidate(ws: Workstream, score: float, reasons: list[str])
rank(session: SessionFacts, workstreams: list[Workstream], recent: list[SessionFacts]) -> list[Candidate]
```
Signals and weights (highest first): pasted session id that is attached to a
ws (1.0 per hit, max 3); own origin/chain/fork-sibling attached (0.9); PR or
issue ref shared with ws anchors (0.9); branch ref (0.8); worktree path (0.8);
parent session attached, for subagents (inherit: 0.95, D9); a SendMessage to
or from an attached session (0.5); repo location overlap, from where tools
acted (0.3 per repo, max 0.6); shared edited files with attached sessions
(0.05 per file, max 0.2; scaffolding paths excluded: `CLAUDE.md`, `MEMORY.md`,
`*/memory/*`, `README.md`, `justfile`, `*.lock`). Never use cwd or project
slug as a signal on their own. Reasons are human sentences citing the evidence
(`"pasted 3f2a9c1e at 2026-01-15T12:00Z"`).

### sync.py
```
evidence(facts) -> list[Found(ref, session, how: list[str], at, unusable: str)]
run(targets, others=(), only=None, facts=None, log=None, dry_run=False, retract=True)
  -> Report(added, skipped, by_session, warnings, ancillary, retracted)
```
Strong evidence only, from `SessionFacts.acted` (what the tools wrote) and
`worktrees`: a PR acted on with a writing `gh pr` verb (create, merge,
review, comment, edit, ready, close, reopen, checkout) or a writing `gh api
.../pulls/N`; a branch from `git commit`'s `[branch sha]` or `git push`'s
`a -> b` lines, or its refspec when the output has no status lines (default
branches dropped; rejected, deleted and failed pushes are no evidence, and
only an accepted ref marks the repo as written to); a linked worktree created,
entered, edited, committed or pushed in, if it still exists. A bare PR
number resolves through the command directory's `origin`. A PR named in the
tail's prompts, assistant text or shell commands (never tool output or file
bodies) counts
when the session wrote to that GitHub repo. Repo overlap, shared files and
cwd never become anchors. Skips: already an anchor (`wt:` by realpath), an
anchor of another open workstream, unresolvable or gone. Writes go through
`store.add_anchor(..., source=SYNC_SOURCE)`.

Shell commands are parsed per simple command (`sessions.shell_segments`):
heredoc bodies stripped, split on `;` `&&` `||` `|` `&` newlines and parens
outside quotes, shlex'd, redirections dropped, `cd` tracked across
segments. `_strip_prefixes` removes `do`/`then`/`else`/`!`/`time`/`command`/
`exec`, `timeout [opts] N`, `env [opts] VAR=x` and leading assignments, and
moves gh's global `--repo`/`-R` after the subcommand (dropping `--hostname`).
A segment piped into anything but `tee`/`cat` is `filtered`: its push output
may be cut, so the refspec fallback needs empty output or the tracking
message, and `hint:` counts as a failure line like `error:`. `git` takes `-C` for its dir; `gh pr` skips the
values of its value-taking options before taking the PR. Every branch must
pass `valid_branch` (`[A-Za-z0-9][A-Za-z0-9._/-]*`, no `..`, `//`, `/.`,
trailing `.lock`, `/` or `.`) and every repo `valid_repo` (`owner/name`), or
the evidence is dropped.

Retraction: a `wt:` anchor whose directory is gone and whose branch merged
(`frontier.settle_gone_worktrees`, below) is removed with `store.rm_anchor`
and listed in `Report.retracted` (`would retract` under `--dry-run`): every
slice retires its worktree after the merge, so it is history, not an anchor.
A merge known by branch name alone (`merged_via.head_only`) is not retracted:
a reused name can fake it, so the row goes to `skipped` instead.
Captures already written keep it. Only targets with a gone `wt:` ref pay for
this, which reads every attached session's facts and probes the
workstream's branch and PR anchors. `attach` passes `retract=False`.

Ancillary means referenced, not owned; its evidence is not the
workstream's. `owners(ws)` is every session that holds or held a canonical
claim (`ws.intervals()`, released included) and those sessions' subagents.
`run` syncs owners only, unless `ancillary=True` (all attached sessions) or
`only=<sid>` (that session, whatever its role); the ancillary sessions left
out are listed in `Report.ancillary`. Canonical attachments still go first,
so a session's evidence goes to the workstream it holds now.

Each session's evidence is cut to its windows for the workstream being synced
(`frontier.session_scope`): `acted` entries by their `at`, and the tail
prose by record timestamp, so a session that released one workstream and
claimed another gives each what it did while attached there. An act with no
time cannot be placed and is left out of a scoped session. `edited_paths`
carry no times, so the "it wrote to this repo" half of the tail-mention rule
still reads the whole session. `run(history=...)` takes every workstream the
windows need, closed ones included; the CLI passes `list_workstreams(include_closed=True)`.

### frontier.py (D3a, D3b, D11)
```
Frontier(ws, as_of: Instant,
  sessions: list[SessionFrontier(facts, role: canonical|ancillary|subagent|codex, lifecycle: last hook event, dropped: bool)],
  anchors: list[Anchor],
  messages: list[MessageEvent],
  timeline: list[(Instant, kind, text)],   # merged, UTC
  loose_ends: list[str],                   # unmerged worktree commits, open review requests, waiting-on-human, unpushed branches
  options: Options(session_id, default: "message"|"fresh"|"resume", default_why: str,
                    message: Option|None, fresh: Option, resume: Option|None),   # Option(how, facts: list[str], data: dict)
  recommendation -> Recommendation(action, session_id, why)   # property, derived from options.default
  tasks: list[TaskStatus(id, title, kind, state, blocked: bool|None)],   # open (ready, then blocked), done, dropped
  closure_blockers: list[str])             # loose ends minus dropped sessions and the not-closed nudge
build(ws, history=None) -> Frontier   # history: every workstream for session_windows; None lists them when needed
session_scope(ref, ws, history) -> list[Window]|None   # own windows, else a subagent's parent's
scoped(facts, windows) -> SessionFacts   # acted, human_prompts, last_assistant, last_tools, subagents cut to the windows
anchor_status(anchor) -> str   # merged|open|draft|closed|gone (merged)|gone|dirty|unpushed|unmerged|exists|unknown
capture(frontier, docs_root, recorded_by) -> Path   # project-docs/docs/<slug>/workstreams/<name>/captures/<utc>-<sid8>.json
```
A capture's JSON carries `recorded_by`: `frontier --record`, `retire`, or
`session_end_hook`. Older captures have none.
Gone worktrees: a `wt:` probe of a missing directory cannot say what it held,
so `settle_gone_worktrees` derives the branch: from session evidence first
(the branch of the last `git commit` inside it, else `worktree_branches`, a
bare `just wt` name matching only as exactly `<known repo>-<name>`, else
the single branch pushed from it), then from a `branch:` or PR anchor whose
name matches the directory under the `just wt` convention
(`<repo>-<branch with / as ->`) or `<repo>/.claude/worktrees/<name>`. A
derived branch is merged when a PR anchor with that `head_ref` is MERGED, a
`branch:` anchor for it is merged, or a `branch:<repo>@<branch>` probe says
so. A PR is credited only if it merged no earlier than the last session act
inside the worktree (created, entered, edited, committed, pushed): work after
an old same-named PR merged is not in it. With no session time to compare,
the credit is `head_only` and the status reads `merged, by branch name only`.
The probe asks only the repo the evidence ran in and the repo the directory
name gives, never every repo the sessions touched, and is skipped once a
`branch:` anchor decided. Merged: liveness gets `status: "gone (branch <b> merged)"`, `branch`,
`branch_from`, `merged_via`, and there is no loose end. Known but unmerged:
"worktree <p> is gone; its branch <b> is not merged". Nothing derivable: the
old "worktree <p> is gone". A `.claude/worktrees` ref also names its repo
for `branch:` probes, since the sessions' repos came from paths inside it.
Task loose ends: "N tasks open (M blocked)" while any task is open; "all
tasks done; workstream not closed" once every task is done or dropped and the
node is not. A workstream with no tasks gets neither.
Scoping: each `SessionFrontier` carries `scope` (its windows, or None) and
`facts` already cut to them. Identity, lineage, peak context, compactions,
locations, `started` and `last_seen` stay whole: they describe the
transcript, and a resume loads all of it. A subagent that started outside
its parent's windows is not listed; a scoped parent's `subagents` list keeps
the attached subagents that started inside its windows, using the starts
`build` already read (each member's transcript is read once per build).
`history`, when not given, comes from one weft listing per CLI invocation. A message counts unless a scoped member
it involves sent or got it outside its windows. Timeline rows (start,
lifecycle, last record) outside a session's windows are dropped, and each
claim and release gets a row. `settle_gone_worktrees` still reads the uncut
facts: what a directory held is not a question of whose work it was. The
SessionEnd hook and `retire` call `build` with the workstreams the session
is attached to as `history`, so captures are scoped the same way; `to_dict`
adds `scope` per session and `status` (the `anchor_status` word) per anchor.
`brief` reads no anchors and no transcripts, so it has nothing to scope and
no status words.

Status words, worst news first where several apply: `gh:` → `merged`,
`draft`, `open`, `closed` from `state`/`is_draft`, else `unknown` (e.g.
`not_probed`). `wt:` gone → `gone (merged)` or `gone`; present → `dirty`,
then `merged` (on the default branch), `unpushed` (count > 0, no remote, no
upstream), `unmerged`, `exists`; not probed → `unknown`. `branch:` →
`gone (merged)`/`gone` when on neither side, else `merged`, `unpushed`
(local only), `unmerged`, `exists`. `file:`/`frontier:` → `exists`/`gone`.

A session is **dropped** (D10) when the ws has no `frontier:` capture naming
it, regardless of how it ended.

`options` (D11) states the ways to pick the work up, each with the facts that
bear on it, and leaves the choice to the reader. All three are about the
newest canonical session:

- `message`, only while it is live: its registry `name` (for SendMessage) and
  tmux pane.
- `fresh`: attach a new session, read the prose frontier (path given, and
  whether it exists yet) and the newest capture (path and age), then read the
  old session selectively.
- `resume`: the `claude --resume` command from its cwd; the context a resume
  would load (`context_after_compaction`, which is `peak_context` when it never
  compacted, since Claude Code resumes from the last compaction summary on;
  when no turn follows the compaction and no size was recorded it is unknown,
  said in words, and never leads to a resume default);
  whether that is within or above `RESUME_MAX_CONTEXT` (150_000; from the
  mining doc, 5 of 14 dropped sessions sat at ≥150k and the median was ~110k);
  its idle time against `CACHE_TTL` (60 min, the prompt cache TTL in force);
  and its compaction count.

`default` is one soft lean with a one-clause reason: `message` while live,
`resume` when the context is within the cap and the cache is warm, else
`fresh`. The constants are facts on the resume option, not a verdict.
`to_dict` carries `options` and keeps the old `recommendation` key, derived
from `default` (`message` maps to `fresh`, as the old verdict did for a live
session), so readers of the pre-options shape keep working.

### cli.py
```
workstream match   [--session SID] [--json]        # ranked candidates for a session (default: $CLAUDE_SESSION_ID or newest in cwd)
workstream attach  <ws> [--session SID] [--ancillary] [--anchor REF]... [--note TEXT] [--no-sync]
workstream attach  --new "<title>" --name <slug> [--anchor REF]... [--session SID]
workstream attach  --none "<reason>" [--session SID]
workstream sync    [ws...] [--session SID] [--ancillary] [--dry-run] [--json]   # default: owning sessions of every open ws
workstream show    <ws> [--json] [--local]
workstream frontier <ws> [--record] [--full] [--json] [--local]
workstream retire  <sid>... [--json]              # capture frontier for every ws each sid is attached to, release its claims
                                                   # (each ws resolved with `get` over one shared export, so captures carry tasks)
workstream close   <ws> [--note TEXT] [--force] [--json]   # exit 1 listing live claims, open tasks and closure_blockers unless --force
workstream ls      [--since 7d] [--all] [--json]   # --all includes closed workstreams
workstream hook [--event SessionStart]             # stdin: SessionStart payload; stdout: {block}
workstream hook --event SessionEnd                 # stdin: SessionEnd payload; records captures; stdout: {recorded, skipped, reason, error?}
```
A canonical `attach` first releases the session's held canonical claims on
other workstreams and prints each one. The claim note, without `--note`, is
the session's latest prompt; for `--new`, the workstream's title when that
prompt predates the session's previous claim. A canonical `attach` then runs `sync` for the attaching session unless
`--no-sync`; `attach --ancillary` and `hook` never do.
`show` sections: header (name, title, state, created/touched), sessions
(canonical first, subagents indented under parents, Codex marked, each
session's canonical intervals beneath it with their release or expiry), anchors
with the status word, then the raw liveness (a synced one marked `(sync)`),
tasks, messages, timeline (UTC, `--local` for each event's own zone). A scoped
session's row spans its transcript cut to its windows, not the whole of it.

`frontier` is concise by default, because its output lands in the next
agent's context window: header; sessions with their canonical intervals and
`↳ N subagents` instead of rows (the count is the rows `--full` would print);
one line per anchor, `ref (sync)  <status word>`; tasks; loose ends; options.
No last prompts, messages, timeline or raw liveness. `--full` prints `show`'s
sections followed by the loose ends and options. `--json` is the same in
both modes.

### session_start_hook.py (D8)
Stdlib only, mirrors `claude-ls/session_mates.py`: reads the payload, runs
`workstream hook` with a 4 s timeout, prints its stdout, exit 0 always. The
block it prints: the top 3 candidates with reasons, the exact `workstream
attach` lines, and `workstream attach --none "<reason>"`. For a session that is already
attached it prints one frontier line per workstream instead (`frontier.brief`):
name and role, open tasks with the blocked count, the loose ends knowable
without anchors (claude sessions other than this one that no capture names,
and the not-closed nudge), the newest capture's age and who wrote it (the
SessionEnd hook, or the command that recorded it on purpose), and the pointer
`workstream frontier <name>`. It reads only `store.get`, `blocked_ids` when a
task is open, and the capture files: no anchor probes, nothing that spawns gh
or git, so it fits the 4 s budget. One `export_nodes()` is shared across the
attached workstreams, so N of them cost N `show`s plus at most two more weft
processes. Prints nothing when `match` has no
candidates and the session is marked unassociated. Env seams for tests:
`WORKSTREAM_HOOK_BIN`, `WORKSTREAM_HOOK_JSON`.

### session_end_hook.py
Stdlib only, same seams as the start hook. A session with no capture naming it
is dropped (D10), and `retire` / `frontier --record` run only when a session
remembers them, so the end hook records for it. It runs `workstream hook
--event SessionEnd` with a 20 s timeout (25 s in settings.json), never writes
stdout, logs one stderr line on failure, exits 0.

`hook --event SessionEnd` finds every workstream whose refs name the session,
closed ones included (as `retire` does), and for each writes a capture through
`frontier.build` + `capture(recorded_by="session_end_hook")`, the
`frontier --record` path. It skips a workstream when the newest capture naming
the session already covers it, compared at whole seconds since `as_of` is
stored truncated: for a capture recorded on purpose (`retire`, `frontier
--record`), when no human prompt follows its `as_of`, because the record's own
tool result and closing reply always land after it; for anything else (the
hook's own captures, older ones), when the transcript's `last_seen` does not
pass it, so a `/clear` does not write a duplicate. Workstreams where the
session is canonical go first, and the run has a 17 s deadline: a capture
starts only if one as slow as the slowest so far would still finish, and the
rest are listed in `skipped` as "time budget". `capture` writes to a `.tmp`
beside the target and renames it before adding the `frontier:` ref, so a kill
leaves a whole file or none. It
never releases a claim: SessionEnd fires on `/clear` and on restarts that
carry on the same work, and `retire` stays the deliberate hand-off.

Budget: `gh_cache_only`, one day of hooklog (the start hook's `HOOK_WINDOW`),
and no 14-day `recent` session scan, so a capture from the hook lacks the
foreign-merge loose ends a manual `frontier --record` would find. Git probes
still run. Measured 2026-11-02 against the real store, read-only (capture
stubbed out), for a 32-session workstream: 9.7 s on a cold transcript cache,
~5 s warm, 0.5 s when the skip applies; CLI startup adds 0.1–0.7 s. Per
workstream errors land in `skipped` with the reason, so one failure does not
cost the others their capture.

## Fixtures (tests/conftest.py)
`SyntheticStore(tmp_path)` builds, under one temp root: a Claude projects dir
with transcripts (main, subagent, forks, /clear chains), a Codex rollout, a
hooklog with naive local timestamps across a TZ change, a registry dir, two git
repos with a linked worktree carrying an unmerged commit, and a `WEFT_HOME`
with an empty log. Helpers: `store.session(project, prompts=[...], tools=[...],
start=..., tz=...)`, `store.hook(event, sid, at_local, **fields)`,
`store.env()` -> dict of the five env vars. The four acceptance cases are
rebuilt on it with invented names: `audio-seam`, `pr-ownership`,
`duplicate-bug`, `cross-repo-merge`.

## Acceptance (spec item 5)
`tests/test_acceptance.py`: for each case, `match` on the fresh session's
facts ranks the right workstream first, and `frontier` names the loose end
the case is about (the unaddressed review; the owning session of the PR; the
unmerged commit in the dropped session's worktree; the merge in another
workstream's anchor by a session rooted elsewhere).
