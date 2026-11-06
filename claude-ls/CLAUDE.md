# claude-ls

## Transcript formats

`claude-ls` reads two on-disk formats. Claude sessions live under
`~/.claude/projects`; OpenAI Codex CLI rollouts under `~/.codex/sessions`. A format
sniff (`_file_is_codex` / `_entries_are_codex`, exploiting disjoint type
vocabularies) dispatches four entry points to `_codex_*` counterparts that
normalize into the same types, so modes don't branch.

`sessions --codex` (or `CLAUDE_LS_CODEX=1`) lists rollouts alongside Claude
sessions, grouped by the cwd in their `session_meta` header — encoded to the same
slug the Claude store uses, so one repo worked in both agents is one project.
Subagent threads are excluded, as on the Claude side. Off by default: mixing two
agents' sessions isn't always wanted, and `resume` is Claude-only.

Resolution is *not* opt-in the way listing is: `summary`/`get`/`last -s` take a
rollout's id, because an id typed by hand is unambiguous intent. `resume` resolves
rollouts too, then refuses them by name — `claude -r` only speaks Claude's store.

`search --codex` scans rollouts too, opt-in on the same flag and env var as
listing. It hands `rg` the rollout store as one more directory, then does the
per-file work on the hits: a rollout records its project inside the file, so the
project key and the subagent flag each cost a bounded head-read, and the tuple
carries a synthetic project path holding the key.

Narrow first, then read: the first cut enumerated every rollout up front via
`iter_codex_session_refs`, which quietly inherited that function's `per_project=20`
listing cap and made **35% of a real store unsearchable**. A listing cap is
meaningless for a search, and no test saw it.

What that shape costs is a second caller asking "is this file a session, and whose
project is it?" — so both routes go through **`codex_project_key`**. Two copies of
that predicate would drift the day Codex adds a `thread_source` worth excluding:
listing would filter, search wouldn't, and nothing would fail.
