# memory-ls

Scan Claude Code's auto-memory store for memories that have gone wrong — paths
that no longer exist, work described as in flight that has already landed, and
entries missing from the index that loads them.

```sh
./memory-ls.py                     # scan every project's memory store, locally
./memory-ls.py --online            # also check issue/PR state via `gh`
./memory-ls.py --project agent     # only projects whose slug contains "agent"
./memory-ls.py --strict            # also report frontmatter name vs filename
./memory-ls.py --search worktree   # find which memories mention something
./memory-ls.py --json              # machine-readable
```

Read-only. Nothing is written, moved, or fixed.

First run and unsure what you're looking at? Skip to
[Reading the output](#reading-the-output) — it explains each finding and what to
do about it. The sections before it are background.

## What "memory store" means here

The term is overloaded. This reads **Claude Code's auto-memory**: the
per-project files the agent writes to carry facts across sessions, plus the
index that gets loaded into context at the start of every session in that
project. Not the context window, not conversation history, not a vector store,
not `CLAUDE.md`, not a repo's `docs/`.

The schema is Claude Code's, not this tool's — undocumented, and free to change
underneath it. Everything the checks assume is here so it can be re-checked
against a real store:

```
~/.claude/projects/                     store root
  <flattened-cwd>/                      one dir per project; '/' becomes '-'
    sessions-index.json                 carries `originalPath` — the real cwd
    <session-uuid>.jsonl                transcripts; each line carries `cwd`
    memory/
      MEMORY.md                         index, loaded into context each session
      <slug>.md                         one entry, one fact
```

An entry opens with frontmatter carrying `name`, `description`, and a nested
`metadata.type` of `user`, `feedback`, `project`, or `reference`. The body links
to other entries as `[[name]]`, resolved by the `name` field rather than by the
filename — the two are allowed to disagree. The index lists entries as
`- [Title](file.md) — hook`.

Two properties of that layout the checks lean on:

- **The directory name is lossy.** A '-' is either a path separator or a literal
  hyphen in a directory name, and nothing in the slug says which. So the
  working directory is taken from `originalPath` or a transcript's `cwd` where
  either survives, and reconstructed by walking the filesystem otherwise.
- **`MEMORY.md` lives inside `memory/`, not beside it.** Looking in the wrong
  place reports every entry in the store as unlinked, which is how the original
  survey briefly measured 326 unlinked entries instead of 3.

## What it checks, and why only these

The checks were picked by running candidates against a real store (50 projects,
327 entries) and keeping the ones that found something worth acting on.

**Dead paths.** A fifth of the absolute paths that memories cite no longer
exist, clustered in deleted worktrees and a docs migration. The memory write
rules already say to verify a named path before recommending it; this runs that
check over the whole store at once.

**Stale references.** A third of entries cite a `#N`. A `project`-type memory
describing work as in flight, when it merged weeks ago, is wrong in a way no
amount of tidiness catches — so `project` entries are what the check reads.
Pass `--all-types` to include the rest, where a landed `#N` is usually just a
citation.

This is the only check that leaves your machine, so it is opt-in behind
`--online`. A local run still counts the references it *would* have looked up
and says so in the footer:

```
284 issue/PR references across 13 projects not checked — re-run with --online
to see which have already landed
```

Silence there would be the worse default: a scan that skips a third of the
store without saying so reads like a clean bill of health.

**Index drift.** Entries with no line in `MEMORY.md`, and index lines pointing at
files that are gone. Near-zero yield today (3 unlinked, 0 dangling), but it is
cheap, and an entry that never got an index line is one nothing will recall.

**Facts in the index.** `MEMORY.md` is meant to hold pointers; the entries hold
the facts. The split exists because the index is loaded into context at the
start of every session in its project, while an entry is read only when
something recalls it — so a fact written into the index is paid for
continuously rather than on demand.

Reported only for projects that *also* have entry files, which is where the two
have genuinely mixed. Eight of the fifty surveyed keep everything in the index
and have no entries at all; that is a different convention rather than drift,
and flagging it would bury the four projects this finds.

Every run also prints what the indexes cost, findings or not:

```
indexes total ~24,274 tokens, each loaded every session in its own project
```

A crude `chars / 4` estimate — enough to show the bill exists, not to settle it.

`--strict` adds frontmatter `name` vs filename divergence. It is off by default
because 57% of a real store diverges and nothing is broken by it — links resolve
under either spelling. Turn it on for a deliberate convention sweep.

### Deliberately not checked

Measured, found empty, and cut: duplicate detection (zero exact-duplicate
descriptions; nothing to say without fuzzy matching), broken `[[wikilinks]]`
(91% resolve, and the rest are forward references the write rules explicitly
allow), and frontmatter validation (one entry storewide). Each is cheap to add
back — the argument against them is the current data, not the idea.

## Reading the output

```
MEMORY STORE — 50 projects, 327 entries        ← what was scanned, not what's wrong

~/src/example                                  ← the project, by working directory
dead path  reference_daily_worktree.md  ~/src/worktrees/shell-merge
stale ref  project_perf_baseline.md     #80  merged PR — bytecode v2 format
unlinked   dev-server-restart-sop.md    no line in MEMORY.md
└─ finding  └─ the entry file            └─ what the finding is about

3 findings: 1 dead path, 1 stale ref, 1 unlinked
  284 issue/PR references across 13 projects not checked — …    ← what wasn't done
```

Only projects with findings are printed. A silent project is a clean one.

Entry files live in `~/.claude/projects/<slug>/memory/`, so a finding names a
file you can open directly. Nothing here is written by the tool — every fix is a
manual edit, or a job to hand to Claude in that project.

### What each finding means, and what to do

| Finding | What it means | Usual fix |
|---|---|---|
| **dead path** | The entry recommends a path that no longer exists. A future session will act on it and find nothing. | Open the entry. Update the path if the thing moved; delete the claim if it's gone. |
| **stale ref** | The entry cites `#N`, and that issue or PR has since closed or merged — so a memory describing it as in flight is now wrong. | Re-read the entry. If it says "in progress", either update it to what shipped or delete it as spent. |
| **unlinked** | An entry file exists with no line in `MEMORY.md`. The index is what loads each session, so nothing will ever recall this entry. | Add a `- [Title](file.md) — hook` line to `MEMORY.md`, or delete the entry if it's obsolete. |
| **dangling** | `MEMORY.md` points at a file that isn't there — or there is no `MEMORY.md` at all while entries exist. | Remove the stale index line; or create the index, if that's what's missing. |
| **index prose** | `MEMORY.md` holds facts inline in a project that also has entry files. The index is loaded every session, so those facts are paid for continuously. | Move each fact to its own entry with a pointer line, if it earns the standing cost. Leave it if the project is small. |
| **name/slug** | `--strict` only. Frontmatter `name` differs from the filename. | Nothing is broken — links resolve either way. Ignore unless you're doing a convention sweep. |

Roughly in priority order. The first two are wrong *advice*, which is what makes
them worth acting on: they will be read as fact by a session that has no way to
check them. The next two are structural and cheap. The last is cosmetic.

### What to ignore

The checks are deliberately literal, so a few findings are noise rather than
defects:

- **A dead path that was always illustrative.** If the entry was describing a
  path as an example rather than recommending it, nothing is wrong.
- **A stale ref in a `reference` entry.** "Shipped in #46" is a citation, not a
  claim about work in flight. That's why only `project` entries are read by
  default; `--all-types` deliberately surfaces the rest.
- **A number that was never a reference.** `Open Question #1` is looked up as
  `#1` and may resolve to a real, merged PR. The surveyed store had two of these
  against 767 references, so they're left as reported noise rather than guessed
  at.

If a finding survives that filter, the entry needs a human decision — which is
why the tool has no `--fix`.

### The footer

Everything under the count is about *coverage*, not defects: references not
checked because the run was local, projects skipped for want of a GitHub remote,
and checks not enabled. It's there so a partial scan can't read as a clean bill
of health.

### How projects are named

By the working directory their memories are about, taken from the path Claude
Code recorded where one survives and reconstructed from the flattened folder
name otherwise. A project whose directory was deleted or renamed shows its slug
and `(unresolved)`, and skips the checks that need a git remote rather than
guessing at one.

## Finding a memory

`--search PATTERN` lists the memories mentioning something, instead of checking
them. The pattern is a case-insensitive regex.

```
MEMORY STORE — 'worktree' in 2 entries

~/src/example
  reference_daily_worktree.md  reference  — daily worktree hygiene for the fork
    12: always `git worktree remove --force` before re-adding
  branch-discipline.md  project  — keep the design branch local-only
    34: concurrent sessions use a git worktree

2 matches in 2 entries across 1 project
```

Exits 1 when nothing matches, so it composes in a shell conditional.

`rg ~/.claude/projects/*/memory/` does the raw grep perfectly well. What this
adds is the context needed to judge a hit without opening the file: the project
keyed by working directory rather than flattened slug, and each entry's `type`
and `description` above its matching lines. A match in the frontmatter is
reported as the entry itself rather than as a line, since the header already
shows what it would say.

`MEMORY.md` is searched alongside the entry files — eight of the fifty projects
surveyed keep every fact in the index and have no entry files at all, so
skipping it would return nothing for them.

Entry bodies, `name` and `description` are searched; `metadata.type` is not, so
`--search reference` won't find `reference`-typed entries even though the type
is shown beside every hit. Filtering by type would be a different flag.

Search runs no checks, so `--online`, `--strict` and `--all-types` are rejected
rather than ignored.

### Exit codes

`0` found something, `1` looked and found nothing, `2` the run never happened —
a bad pattern, a flag combination that doesn't mean anything, a `--base` with no
store under it, or a `--project` matching no project. The third case is separate
on purpose: a mistyped scope reported as "nothing found" reads as an answer
about your memories when the store was never asked.

## Requirements and limits

Everything but the reference check is local: file reads and `stat`. A default
run over 50 projects finishes in about a second.

`--online` shells out to `gh` — one `gh auth status`, then one GraphQL request
per repo per 50 references. If `gh` cannot be used, the run **fails before
scanning** and exits 2, quoting `gh`'s own diagnosis:

```
memory-ls: --online needs `gh`, and `gh auth status` failed: Failed to log in
to github.com account (default) — To re-authenticate, run: gh auth login
Re-run without --online for the local checks.
```

It does not fall back to a local scan. `--online` asks for a specific check, and
answering with a full report whose footer admits the check never ran hides the
failure under the findings — which is where it is easiest to miss.

A project's own repository is read from its GitHub remote, `origin` preferred
but any of them will do. Without one, `owner/repo#123` references are still
checked — they name their own repository — and only bare `#N` go unresolved,
reported per project with a count:

```
some-project: stale-ref: no GitHub remote — 209 bare references not checked
```

Two things it can misread, both rare enough to leave as reported noise:

- An ordinal written like a reference (`Open Question #1`) is looked up as
  `#1`. Two such spellings against 767 references in the surveyed store.
- Cross-repo references (`owner/repo#123`) are resolved against the repo they
  name, not the project's own. A bare `#N` inside prose about another project
  still resolves against the project's own remote.
