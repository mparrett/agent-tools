# agent-tools

Standalone tools for Claude Code and agent workflows. Each is a single executable
script with inline uv metadata — no install step. See README.md for what each tool
does; this file is only the things that will bite you.

Gotchas scoped to one tool live in `claude-ls/CLAUDE.md`, `mdserve/CLAUDE.md`, and
`preview-diff/CLAUDE.md`, which load when you work in those directories.

## Run `just test`, not one suite

```
just test     # every suite (~2 min)
just check    # lint + format check
just fix      # ruff autofixes
```

`just test` is slow mostly because of preview-diff's Playwright suite (~85s). Run
it anyway. Running a single suite is how a cross-package break gets merged — see
below.

## CI runs these same recipes, and gates `main`

`.github/workflows/ci.yml` calls `just check` and `just test` on every PR. It
deliberately does not restate the suite list: a second copy is how a suite goes
missing from one side without anyone noticing, which is the bug it exists to
catch. Add a suite to the justfile and CI picks it up.

Two things to know:

- **It blocks a merge, and nobody bypasses it.** A ruleset on `main` requires
  the `lint + tests` check and forbids force-pushes and deleting `main`, with no
  bypass actors. There is no PR rule, but a direct push is refused all the same:
  the pushed commit has no passing check yet. Config edits go through a PR too.
- **A cancelled run on `main` is not a failure.** The workflow sets
  `cancel-in-progress`, and merges land close enough together here that one run
  supersedes another. Check the run for the newer SHA.

CI runs on ubuntu; every tool here is written and used on macOS, and that gap is
real. `mktemp -t` means "prefix" to BSD and "template, and it needs XXX" to GNU,
so `preview-diff --serve` and `--review` aborted before printing anything on
Linux — for as long as those modes had existed, with no test able to see it.
Shell that has only ever run on a Mac is the first place to look when CI
disagrees with your laptop.

## claude-graph reaches into claude-ls

`claude_graph/sessions.py` locates `claude-ls.py` **by path** and loads it with
importlib, then re-exports its underscore-prefixed functions as claude-graph's
public API. It has to: the filename has a hyphen, so it can't be imported by name,
and importing it as a module would drag `rich` + `blessed` into a stdlib-only tool.

Consequences, all of which have already caused a bug:

- **A claude-ls function with no in-tree caller may not be dead.** `vulture` and
  any claude-ls-scoped analysis cannot see across the seam. `_canonical_repo_root`
  was deleted as dead code in an earlier cleanup and broke claude-graph; it now
  carries a docstring saying why it must stay. Grep `claude-graph/` before
  deleting anything private from `claude-ls.py`.
- **Renaming or changing the return type of a private claude-ls function is a
  breaking change** for claude-graph, and nothing in claude-ls will fail.
- **The gate only works if you use it.** claude-graph's suite wasn't in `just test`
  until a later change added it, so the standard check went green while
  claude-graph was broken. It's in there now — which is the whole reason to run
  the full target.

**`workstream/workstream/seam.py` is a second seam of the same kind.** It
re-exports another dozen underscore-private claude-ls names (`_tail_entries`,
`_me_session_id`, `_live_registry`, `_codex_session_meta`, `_EDIT_TOOLS`, …), so
grep `workstream/` as well as `claude-graph/` before deleting or reshaping
anything private in `claude-ls.py`. Both seams are in `just test`.

The dependency is one-directional: claude-ls never imports or calls claude-graph
or workstream.
The string `claude-graph <id>` in claude-ls's summary footer is printed text, not a
call.

## Coverage gaps

- `claude-conversation/` is untracked stale bytecode, not a tool. The command of
  that name was an alias for `claude-ls` and is retired. Ignore the directory;
  deleting it is still to do.

## Wrapper tags: summarizing vs fidelity

Claude Code injects synthetic "user" messages — slash-command scaffolding, `!`-bash
echoes, hook output, task notifications — that open with a known tag. More than half
the transcripts in a real store start with one, so anything that treats the first
user message as the topic shows boilerplate instead.

The vocabulary lives in **`_WRAPPER_TAGS` in `claude-ls.py`** and nowhere else worth
copying. claude-graph re-exports it through the seam. claude-reel keeps a hand-synced
copy because it is stdlib-only by design; a drift test in claude-ls's suite fails if
the two diverge. **Don't restate the list in prose** — a fourth copy is the bug this
arrangement exists to prevent.

The rule that matters is *which callers filter*:

- **Summarizing views** — listing topic, `activity` topic and turn count, `summary` —
  want "prose the human typed" and call `is_wrapper_text`.
- **Fidelity views** — `get`, `last`, `messages`, `select`, `browse`, `export` — keep
  everything. Several wrappers carry text the human typed: a slash command with
  arguments is one message opening with `<command-message>`, and `<bash-input>` is
  typed by definition.

Filtering in `is_system_message` (which gates `load_messages`, feeding the fidelity
views) silently deleted 1,056 messages across 280 sessions and emptied 44, breaking
`last --json`. Keep the filter at the summarizing call site.

The tag set is an external fact about Claude Code's wire format, not a design
decision, so re-derive it from a real store rather than reasoning about it:

```bash
# leading <tag> on user messages, most common first (--no-filename or it groups by file)
rg --no-ignore --hidden --no-filename -o '"content":"<([a-z][a-z0-9-]*)>' -r '$1' \
  ~/.claude/projects | sort | uniq -c | sort -rn | head -20
```

Read a sample before adding anything it turns up — the census can't tell an injection
from pasted HTML. That run found `persisted-output` ("Output too large, saved to …",
a real injection, now in the set) alongside `title` (someone's pasted web page,
correctly not in it).

## Work in a worktree; this checkout stays on main

More than one session works in this repo at once, so `HEAD` can move between your
own commands, and `git status` shows modified files rather than which branch you
are on — a clean status is not evidence you are on `main`.

```
just wt fix/my-thing     # worktree off a freshly-fetched origin/main; prints the path
just wt-ls               # every worktree: branch, dirty/unpushed, merged PR
just wt-rm fix/my-thing  # retire one (refuses unless clean and pushed)
just mine                # commits your branch adds over origin/main
```

**`~/projects-new/agent-tools-public` is the canonical checkout: clean `main`,
fast-forward only.** Nothing is authored there. That is the whole rule, and it is
load-bearing rather than tidiness — `git checkout -b` branches from **`HEAD`, not
`main`**, so running it while a session has left `HEAD` on their branch silently
adopts their commits. That is how one PR's commit shipped inside another's,
under an unrelated commit message. If HEAD is always `main`, that cannot happen;
it is not a hazard to be caught by habit, it is one that stops existing.

Worktrees live in `~/projects-new/worktrees/agent-tools-<name>`, outside the repo,
so the canonical checkout's `git status` keeps meaning something — the same
property the read-only clone gets from nobody authoring in it. `just branch` is
retired and errors with a pointer; it was what parked this checkout on feature
branches in the first place.

**Deleting a branch stays a human decision.** `just wt-rm` prints the command and
does not run it, because nothing can tell a branch abandoned on purpose from one
whose PR is about to be reopened. Don't delete a branch you didn't create, even a
merged one — a session may still be working from it.

## This repo is checked out twice

You commit in a worktree of `~/projects-new/agent-tools-public` (`just wt`).
Claude Code *loads this repo's skills* from
`~/projects-new/read-only-clones/agent-tools`, a second clone pinned to `main`.
Both see this file, so the rules above apply in either — but only one of them is
yours to write in.

The read-only clone exists so that a modification under it is visibly out of band.
Nothing is authored there, so its `git status` is a tripwire; a working clone's is
not, because a stray edit hides among real work.

```
just stale                           # in the read-only clone: how far behind is it?
just sync                            # in the read-only clone: ff only from a clean tree
just link-skills ~/.claude/skills    # after adding a skill; skips what is linked
just ship                            # in this clone, after a merge: sync, then link
```

`ship` finds the read-only clone at `$REPOS_RO/agent-tools` (`REPOS_RO` defaults to
`~/repos-read-only`). It refuses unless the work in front of you is on `origin/main`:
no unpushed commits on `main`, or a merged PR for the current branch.

**A skill edited through `~/.claude/skills/<name>/` lands in that clone, not this
one.** The path does not look like a working tree, the editing session is usually
in a different repo, and nobody `cd`s there — so the change sits uncommitted with
no copy on any remote. `pr-shepherd`'s review-body fix lived that way for
three days and was found by accident; an unattended `git pull` would have
destroyed it, since a later PR had rewritten neighbouring lines. Port such a
change here by hand rather than as a patch, for that same reason.

**The clone cannot tell you it is stale, so ask it.** Nothing fetches in that
clone on its own, so it reports its own months-old `origin/main` as the truth and
looks current. It sat six commits behind that way while every session loaded
skills from it. `just stale` fetches first and is the only reliable answer to
"which version are my agents actually running".

`just sync` tags the pre-pull HEAD as `synced/<timestamp>` before fast-forwarding,
then prints what landed. The tag is the rollback point — the version the agents
were running is the only one there is evidence about, which is why it is taken
before the pull and not after. `git reset --hard synced/<stamp>` goes back;
`git bisect start main synced/<stamp>` finds which commit changed a behaviour.
The tags are local to the machine and pruned to the newest 20.

## Conventions

- Never mix formatting with feature work; `just fix` / `just fmt` get their own commit.
- `aislop-dev scan` is advisory here. Its comment rules ("trivial restating comment",
  "narrative comment block") conflict with this codebase's deliberate WHY-comments —
  do not run `aislop fix` unattended.
