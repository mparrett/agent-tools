# board

A work board for people and agents sharing a repository: one markdown card per
owner per piece of work, kept on a git branch that is never merged. `README.md`
on that branch is generated from the cards, so the branch's page on the git host
is the board, behind the host's own access, with nothing to serve.

## Use

```sh
board init --title "Our work board"     # once: create the `board` branch
export BOARD_OWNER=sam                    # or `git config board.owner sam`

board ls login-page                       # before starting; exit 3 if someone has it
board set login-page --status in-progress --title "Login page" --link pr=42 --note "Form drawn."
board set login-page --status in-review   # later updates keep every field you don't pass
board done login-page                     # when it merges or is dropped
```

`board ls` prints every card, `board render` prints the README (`--html` gives a
standalone page via `gh api markdown`, light or dark with the system, with a
Theme button to pin either), `board prune` removes cards long past
their `ttl`, and `board squash` replaces the branch's history with one commit.
`--remote`, `--branch` and `-C` pick where it runs.

## The branch

```
board.yml                        # what is particular to this repository
cards/<subject>--<owner>.md      # one card per owner per piece of work
README.md                        # generated; never edited or merged
```

A card is YAML frontmatter and a short note:

```markdown
---
title: Login page
subject: login-page
owner: sam
session: claude-1234     # optional: the agent working for the owner
status: in-progress
since: 2026-10-24T09:12:40Z
updated: 2026-10-25T16:40:05Z
ttl: 3d                  # optional: the board's default otherwise
links:
  pr: 42
---

Form drawn; validation next.
```

`title`, `subject`, `owner`, `status` and `updated` are required. Other keys are
kept as they are, so a card can carry fields a later version will read, such as
`depends-on` or `parent`.

`board.yml`:

```yaml
title: Our work board
statuses: [claimed, in-progress, in-review, blocked, parked]   # the columns, in order
ttl: 3d                 # a card older than this is shown as stale
prune_after: 14d        # `prune` removes cards this far past their ttl
subject_url: https://github.com/OWNER/REPO/blob/main/docs/proposals/{subject}.md   # linked only where subject_check finds the file in the writer's checkout
subject_check:          # refuse a subject with no such file, so everyone spells it alike
  ref: origin/main
  path: docs/proposals/{subject}.md
links:                  # how a link key becomes a URL
  pr: https://github.com/OWNER/REPO/pull/{value}
```

## How writes stay safe

Every write builds its commit on a temporary index from a fresh fetch and pushes
it straight to the branch; the checkout and working tree are never touched. A
writer only ever changes its own card, and the README is derived, so when a push
is rejected because the board moved, the write is replayed on the new tip with
jittered backoff. Each fetch lands on a ref of its own, so several agents in one
checkout don't race on the remote-tracking ref's lock.

After a write lands, `set` reads the board again and warns, with exit 2, if
someone else has a card on the same subject. The board shows that subject as
contested; the earlier `since` keeps the work. `squash` pushes with
`--force-with-lease`, so it changes nothing if a card landed after its fetch.
