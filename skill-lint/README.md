# skill-lint

Check `SKILL.md` files against the contract the harness and Claude actually
apply — Anthropic's published limits, plus the partial-read rules that decide
whether a reference file is read in full.

```bash
skill-lint claude-code-skills/              # every SKILL.md under a directory
skill-lint path/to/SKILL.md                 # one skill
skill-lint --json claude-code-skills/       # machine-readable
```

Exit 0 when every skill is clean, 1 when anything is found. `just test` runs it
over this repo's own skills, so a regression fails the suite.

## What it checks

| Check | Why it is here |
|---|---|
| Frontmatter is YAML and a mapping | The harness falls back **silently** when it isn't: the skill is listed under its first heading and the description never reaches Claude, so it never fires. Two skills had exactly this and were fixed |
| `name` ≤ 64, lowercase/digits/hyphens, no reserved word, no markup | Published limits |
| `name` has no leading, trailing or doubled hyphen, and matches its directory | The directory is how a skill is installed and invoked; a `name` that disagrees is the version nobody sees |
| `name` and `description` are strings | `name: 123` and `description: [a, b]` load as an int and a list, which string conversion would launder into something that passes every other rule |
| `description` non-empty, ≤ 1,024, no markup | Published limits; the description is the only thing Claude sees before loading the skill |
| `description` does not open in the first or second person | Published warning — it is injected into the system prompt |
| Body under 500 lines | Published guidance |
| Referenced files exist | A dangling reference is a dead end mid-task |
| Reference files over 100 lines carry a contents list | Claude previews long files rather than reading them, so the tail goes missing |
| References are one level deep | Same reason: a reference reached from a reference gets read in part |
| `allowed-tools` reads as discrete rules | Accepts a YAML list, a comma-separated string, and a space-separated one (tokenized on rule shape, since a pattern holds spaces of its own); only a leftover that is not a rule is reported |

Both spellings of a reference count — a markdown link and a backticked path —
because skills name them either way, and a link-only reader finds no references
at all in a skill that names all of its own in backticks. A backticked path
counts when it resolves, **or** when it sits under a directory that exists
beside the skill: otherwise deleting `references/guide.md` would delete the
finding along with the file. Prose like `meta.json` or `codex-run/README.md`,
which names neither a sibling file nor a sibling directory, stays quiet.

## What it deliberately does not check

- **Evaluations.** Their absence is real (see `project-docs/agent-tools/2026-09-14-skills-guidance-audit.md`)
  but no linter can tell a good evaluation from an empty file.
- **Third person vs imperative.** The published warning targets first and second
  person, which is checked. Failing every imperative opener would fail all five
  of this repo's skills against a prose example rather than a stated rule.
- **Windows-style paths.** Every backslash in these skills has been a regex or a
  shell escape inside a fenced block. The check produced only false positives.
- **Flag drift** — whether a backticked `--flag` still exists in the tool
  it names. Worth doing, and not here: attribution is the hard part, and a
  checker that guesses which command a flag belongs to is noise. Noise in a doc
  gate gets it switched off.

The general rule: a check earns its place by being a published limit or a
failure that has already shipped in this repo. Style opinions do not qualify.

## Related

Another project has its own skill checker covering the same published limits.
The rules live in two repos on purpose — that one belongs to a different project
and is not ours to depend on.
