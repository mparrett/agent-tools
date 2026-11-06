# harness-ls

Survey what actually lands in Claude Code's effective context, ranked by token
cost — `du -sh` for the context window, with provenance.

Run it in a project (or with `--no-project` for the user-global picture) and it
resolves the same config the harness reads: the `CLAUDE.md` chain and its
`@`-imports, memory, the skill/command/agent catalogs, enabled plugins, MCP
servers, hooks, and the environment block.

## Usage

```bash
./harness-ls.py                  # ranked survey of the current project
./harness-ls.py --no-project     # user-global sources only
./harness-ls.py --project DIR    # survey somewhere else
./harness-ls.py skills           # drill into one source
./harness-ls.py --json           # machine-readable
./harness-ls.py --exact          # real token counts instead of chars/4
```

Drill-downs: `skills`, `commands`, `agents`, `instructions`, `hooks`, `mcp`,
`settings`, `memory`.

## What the output means

```
EFFECTIVE CONTEXT — ~/src/example-project

ALWAYS-ON — every session pays this before you type
   5.0k  skills catalog                mixed    40 entries (30 from plugins)
   1.2k  ~/.claude/CLAUDE.md           user
    800  ~/src/example-project/CLAUDE.md  project
    200  environment block             harness  3 changed file(s) in git status
      ?  hook: SessionStart            user     1 command(s) — injects once, size unmeasured
      ?  MCP servers                   mixed    2 server(s) — schemas unmeasurable offline

PER-TURN — paid again on every message
      ?  hook: UserPromptSubmit  user  1 command(s) — injects EVERY turn
```

(Illustrative numbers — yours will differ.)

Sources are grouped by *when* you pay for them:

| group | meaning |
|---|---|
| `ALWAYS-ON` | in context before you type a word |
| `PER-TURN` | re-injected on every message — `UserPromptSubmit` hooks |
| `ON-DEMAND` | not loaded yet; the number is what it costs when it is (skill bodies, recalled memories) |
| `BEHAVIORAL` | no context cost, but it changes what happens (permissions, non-injecting hooks) |

A `?` is an honest unknown, not a zero. Hook output can't be measured without
running the hook, and MCP tool schemas only exist once a server connects.

## Accuracy

Two things to keep in mind:

**It predicts, it doesn't observe.** The numbers come from statically resolving
config the same way the harness does. When Claude Code changes its loading rules,
this drifts. Comparing the total against a real session's first-turn `usage` is
the intended calibration and is not built yet.

**Tokens are estimated at ~4 chars/token** — good enough to rank sources against
each other, which is the job. `--exact` counts through the API instead, and needs
the `anthropic` package plus a key:

```bash
uv run --with rich --with anthropic ./harness-ls.py --exact
```

Catalogs are billed for the name and description only, since that is all that
reaches the context — a `SKILL.md` body costs nothing until the skill is invoked,
which is what the `ON-DEMAND` row is for.

## What it can't see

The server-side system prompt, built-in tool schemas, and built-in agent types are
not derivable from local config. They are called out as unattributed rather than
folded into the total.
