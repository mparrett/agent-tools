<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo.png" alt="agent-tools" width="320">
  </picture>
</p>

Standalone, self-contained tools for working with Claude Code and agent
workflows. Each tool is a single executable script with inline
[uv](https://docs.astral.sh/uv/) dependency metadata — no install step, just run it.

Most tools started life in sibling repos and were collected here.

## Tools

| Tool | What it does |
|------|--------------|
| [`mdfeed`](mdfeed/README.md) | Live web viewer for `.md` files written/edited by Claude. Split-pane UI with SSE auto-refresh. Includes the `mdfeed_write` PostToolUse hook that feeds it, and `mdview FILE.md` to open one file in it from a shell or from Finder. |
| [`mdserve`](mdserve/README.md) | Convert and serve a markdown/code directory (or single file) as a browsable HTML site; export to Google-Docs-compatible markdown. |
| [`claude-ls`](claude-ls/README.md) | View, search, and resume Claude Code session logs from the CLI; `who` shows the live fleet by repo, and a SessionStart hook tells each session who else is working beside it. |
| [`claude-ps`](claude-ps/README.md) | List running Claude processes with TTY, CWD, tmux session, and resource usage — a `ps` tuned for Claude Code. |
| [`claude-watch`](claude-watch/README.md) | Follow one session live — its turns or `/loop` ticks, the tools it calls, and the subagents it spawns, with each subagent's tool calls inline while it runs. History shows each past turn as a header and its last message. |
| [`preview-diff`](preview-diff/README.md) | Render a `git diff` (optionally with a PR description) to a self-contained HTML file and open it in the browser. No server, no deps. |
| [`htmldiff`](htmldiff/README.md) | Compare two web pages as a rendered redline — changes marked up in the page itself — or as a side-by-side/unified source diff. One HTML file, no build step. Run it standalone, or `node htmldiff/cli.mjs` to type two URLs into the page and re-diff them as they change. |
| [`claude-reel`](claude-reel/README.md) | Render a Claude Code session (or the tail you missed) as a paced, collapsed HTML slideshow — catch up on semi-autonomous output without scrubbing raw JSONL. |
| [`harness-ls`](harness-ls/README.md) | Survey what lands in Claude's effective context — the `CLAUDE.md` chain, memory, skill/command/agent catalogs, plugins, MCP servers, hooks — ranked by token cost, with provenance. |
| [`memory-ls`](memory-ls/README.md) | Scan the auto-memory store for memories that have gone wrong — dead paths, work described as in flight that already landed, index drift. Read-only. |
| [`claude-graph`](claude-graph/README.md) | An overview of what happened when across your sessions — a timeline with wall-clock concurrency lanes, plus artifact-group and seed views, all emittable as one canonical JSON document. The historical counterpart to `claude-ps`'s live view. |
| [`codex-run`](codex-run/README.md) | Drive the Codex CLI non-interactively: review a PR (or a local change via `preview-diff`), implement an issue, or send any templated prompt; each run leaves a directory with the prompt, event stream, and the final message, so an agent can dispatch Codex and wait on a file. |
| [`usage-viz`](usage-viz/README.md) | Standalone HTML reports of coding-agent usage from ccusage: daily tokens and cost across every agent with `/loop` wakeups overlaid (and, opt-in, by model and by project), or one Codex run with its commands, context fill and cost. |
| [`codex-review-scan`](codex-review-scan/README.md) | Measure what Codex did when asked to review a PR, from its session rollouts: which sessions were reviews, and whether each fetched the PR, ran tests, used the web, or posted to GitHub. Also prints one session's command trail, or the system prompt it ran under. |
| [`skill-lint`](skill-lint/README.md) | Check `SKILL.md` files against Anthropic's published skill contract — frontmatter that silently fails to parse, the name/description limits, and the partial-read rules that decide whether a reference file is read in full. |
| [`board`](board/README.md) | A work board for people and agents sharing a repo: one markdown card per owner per piece of work on a branch that is never merged, with the branch's generated `README.md` as the board. Writes push straight to the branch and survive concurrent writers; contested and stale cards are marked. |
| [`workstream`](workstream/README.md) | A durable identity for a piece of work across Claude Code sessions, subagents, Codex rollouts and the PRs, branches and worktrees they touch, held in weft. A SessionStart hook matches a new session to its workstream; `frontier` says where the last session stopped and what to resume; `sync` accrues anchors from the evidence sessions acted in; `close` is the human assertion that the work is done. |

A companion hook, [`claude-hook-logger`](https://github.com/mparrett/claude-hook-logger), lives in its own repo because it is a compiled Go binary: it logs every Claude Code hook event to a rotating JSONL file in ~3 ms, and installs with `go install`.

`claude-graph` is the exception to the single-script rule above: it reads
`claude-ls.py` through an import seam, so it ships as a package with its own
`pyproject.toml`. `bin/claude-graph` is a wrapper rather than a symlink, so it
still runs as a bare word once `bin/` is on your `PATH`; from the clone root
without one, it is:

```sh
uv run --directory claude-graph claude-graph
```

## Usage

The scripts are self-executing via `uv`:

```sh
./mdfeed/mdfeed.py
./mdserve/mdserve.py <dir-or-file>
./claude-ls/claude-ls.py last
./claude-ps/claude-ps.py
./claude-watch/claude-watch.py <session-id>
./harness-ls/harness-ls.py
./memory-ls/memory-ls.py
./htmldiff/htmldiff              # or just open htmldiff/htmldiff.html
```

### Put the tools on your `PATH`

`bin/` holds relative symlinks to each tool under clean command names, so a
fresh clone is runnable with one `PATH` entry:

```sh
export PATH="$PWD/bin:$PATH"
mdfeed
mdview <file.md>
mdserve <dir-or-file>
claude-ls last
claude-ps
claude-watch <session-id>
claude-graph
harness-ls
memory-ls
htmldiff
board
```

To install into a directory already on your `PATH` instead:

```sh
just link ~/bin      # symlinks every tool, reports created vs skipped
just unlink ~/bin    # removes only the links pointing at this clone
```

On macOS, `just install-finder-app` additionally builds an app bundle that hands
`.md` files from Finder to `mdview` — see
[mdfeed's README](mdfeed/README.md#from-finder).

`link` never overwrites: a name already taken by another clone's tool, or by an
unrelated command, is reported and left alone. `unlink` is the same rule in
reverse — it removes a symlink only when it points into this clone.

## Skills

`claude-code-skills/` holds Claude Code skills; symlink them into
`~/.claude/skills/` to activate.

```sh
just link-skills ~/.claude/skills    # symlinks every skill, reports created vs skipped
just unlink-skills ~/.claude/skills  # removes only the links pointing at this clone
```

Same never-clobber rule as `link`. Prefer it to linking by hand: a skill added
to the repo and never linked is invisible, with nothing to fail.

### Reading from a separate clone

Skills and tools can be loaded from a clone kept apart from the one you commit
in, so a half-finished edit does not reach live sessions. Nothing is authored in
such a clone, which makes its `git status` a tripwire — anything modified there
arrived out of band, from a plugin, a script, or a session that edited a skill
through its `~/.claude` path without knowing it was writing into a working tree.

```sh
just sync    # fast-forwards it, and refuses if that tripwire has fired
just ship    # from the working clone after a merge: sync it, then link anything new
```

`ship` looks for the clone at `$REPOS_RO/agent-tools`, with `REPOS_RO` defaulting to
`~/repos-read-only`.

A change caught this way has no remote copy, so `sync` prints it and stops
rather than pulling over it. Commit it from a working clone first.

| Skill | What it does |
|-------|--------------|
| [`codex-review`](claude-code-skills/codex-review/SKILL.md) | Get a second-opinion review from OpenAI Codex through the `codex-run` CLI: dispatch a PR or local-diff review, wait on it as a background job, verify each finding against the real diff before repeating it, and post only on an explicit go-ahead. Triggers only on an explicit mention of Codex — a plain "review this PR", and round two afterwards, stay with `pr-shepherd`. |
| [`find-session`](claude-code-skills/find-session/SKILL.md) | Locate a past Claude Code session by ticket, PR, file, or topic — and report where the work actually landed. Pairs with `claude-ls`. |
| [`pr-dashboard`](claude-code-skills/pr-dashboard/SKILL.md) | Build a heads-up dashboard of open PRs — workstream lanes, the stack DAG, and a prioritized "what to review next and why" queue. Pulls facts from `gh`; renders a self-contained local HTML file with a light/dark/system switch. Includes `watch.py`, a stateful "what changed since I last looked" mode that caches PR review/comment state under `$XDG_STATE_HOME` and diffs on re-run. |
| [`pr-shepherd`](claude-code-skills/pr-shepherd/SKILL.md) | Code review a PR, then the rest of its lifecycle, adapted from OpenAI Codex's review rubric and GitHub skills (Apache-2.0): review a PR or self-review a diff, address review comments, shepherd stacked changes, babysit CI until green, squash-merge; `scripts/review_target.py` pins the reviewed head, gives you a worktree at it to run checks in, and refuses to post against a moved one. Agent-agnostic — needs only `git` and `gh`. |
| [`workstream-map`](claude-code-skills/workstream-map/SKILL.md) | Map a person's work in a theme across one or more repos — every issue and PR they touched, as lanes × time with the `#`-references between them as edges, hub ranking, a curated "start here" per lane, and a markdown inventory. The historical, relational sibling of `pr-dashboard`. |
| [`pr-watch`](claude-code-skills/pr-watch/SKILL.md) | Report only what moved on open PRs since the last check — approval flips, comment/body changes (conversation and inline review), new reviews, opened/closed PRs. Stateful delta sibling of `pr-dashboard`: the engine (`watch.py`) and baseline cache live in the `pr-dashboard` skill, so installing pr-watch requires pr-dashboard alongside it. |
| [`smolspec`](claude-code-skills/smolspec/SKILL.md) | Sharpen a task with three critical questions — why, what's undecided, what done means and where new scope goes — into a spec of half a page to a page. |

## Roadmap

- [x] `claude-code-skills/` — skills live here (`codex-review`, `find-session`, `pr-dashboard`, `pr-shepherd`, `pr-watch`). More to come.

## License

MIT — see [LICENSE](LICENSE).

One directory is carved out. `claude-code-skills/pr-shepherd/` adapts prompt and
workflow text from OpenAI Codex (Apache-2.0) and the openai-curated `github`
plugin (MIT), and is governed by the `LICENSE`, `LICENSE-MIT`, and `NOTICE`
files in that directory rather than by the root LICENSE. Everything else in the
tree is original work under the root MIT grant.
