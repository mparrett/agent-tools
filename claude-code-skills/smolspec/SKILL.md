---
name: smolspec
description: >-
  Three critical questions that make a task sharper and leave fewer loose ends:
  why it matters, what is still undecided, and what done means (including where
  new scope goes). Produces a spec of half a page to a page, not a plan. Use
  when the user invokes /smolspec, says "smolspec this", "spec this out
  quickly", "interview me before we start", or opens a moderate task with a
  one-line prompt and asks to tighten it first. Lighter than plan mode: it
  settles what, why, and done, never how.
---

# smolspec

Three questions, one per message, a check on what went unanswered, then a
spec. The point is fewer loose ends, not completeness — if the spec is getting
long, cut it.

Open the first question you ask (question 1 unless the opening prompt already
answered it) with one line: *"Say 'park it' to defer a thread or 'let's move
on' to skip ahead."* Honor either at any point.

## Conventions ride along with the first question

`<slug>` is the project's project-docs directory, resolved the way the
project-memory skill does; if the project has none, ask where specs go as part
of the first question rather than inventing a directory.

Read the saved defaults, if any:

- machine: `~/projects-new/project-docs/docs/_shared/smolspec-defaults.md`
- project: `~/projects-new/project-docs/docs/<slug>/smolspec-defaults.md`

Append them to the first question's message as one line the user can accept with
"same": *"Conventions as last time: standard PR flow on this machine, this
repo's procedures, defer extra scope."* Explicit beats implicit even when it
goes without saying. It costs no extra round trip.

If none exist, append instead your proposed conventions for the user to
correct, and save what they settle on with the spec: machine-wide conventions
to `_shared`, ones specific to this project to `<slug>`. Record only what the
user said; CLAUDE.md already covers the rest.

## The three questions

**Lead every question with your own answer and a recommendation with its
rationale**, even when the question is open-ended. The user can always reply
"go with your recommendation"; never leave them a blank to fill.

Ask one question per message, so the discussion of one can reshape the next;
the user may answer ahead, all at once. Skip any the opening prompt already
answered.

1. **Why?** What it's for, what it connects to, the cost of not doing it.
   Propose your best reading of the why from context.
2. **What's undecided?** The real forks, each with two or more viable options
   and a recommendation; anything with only one sensible answer goes on a
   single "assuming …" line instead. Don't list options the user has already
   ruled out. Add any risk, dependency, or person to notify you can see.
3. **What's done, and where does new scope go?** Draft both. The scope draft
   says what happens to new bugs, features, and housekeeping found along the
   way: fix now, record for later (and where), or stop and ask.

## Close the gaps

Before writing the spec, list everything the user left unanswered — forks,
the conventions line, a skipped question — each with the option you'll take
(your recommendation or otherwise) and why. A reply confirms every item it
doesn't correct — "ok, go" confirms the lot, conventions included. If the user
moves on without answering the list, mark those items "(unconfirmed)" in the
spec and don't save them as defaults.

## Done always includes

Unless the user removes it: **every loose end, follow-up, and housekeeping
task has a named sink and survives the session ending.**

## The spec

Half a page typical, one page max. Save it to
`~/projects-new/project-docs/docs/<slug>/YYYY-MM-DD-<topic>-spec.md`, show it
in the conversation, and commit and push it (project-docs commits land on
`main`; if it's on another branch, stop and say so). Commit any new or changed
defaults files in the same commit.

State each decision in words, numbered so amendments can point at it.

```markdown
# <topic> — spec (YYYY-MM-DD)
**Why:** …
**Decisions:** D1 … · D2 … · open: …
**Risks / deps / notify:** …
**Done:** …
**Scope:** new bugs → … · new features → … · housekeeping → …
**Exceptions to defaults:** <only if any>
## Amendments
```

## While working

- When new scope turns up, apply the spec's scope line instead of deciding
  afresh.
- When a decision changes, append one line to Amendments: which D, what
  changed and why, and commit and push it.
- Before calling the task done, list every loose end with its sink.
