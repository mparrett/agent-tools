# Review rubric

Borrowed near-verbatim from Codex's review rubric
(`codex-rs/prompts/templates/review/rubric.md`) with the harness-specific JSON
output schema replaced by the markdown format in SKILL.md. Repo-specific review
guidelines (in the target repo's `AGENTS.md`/`CLAUDE.md`) override this file.

## Contents

- What qualifies as a finding
- Evidence required before reporting
- Priorities
- Comment guidelines (when posting findings as review comments)
- Overall verdict
- Risk prompts (investigation only) — **not findings**

## What qualifies as a finding

Flag an issue only when **all** of these hold:

1. It meaningfully impacts the accuracy, performance, security, or maintainability
   of the code.
2. It is discrete and actionable (not a general issue with the codebase or a
   combination of multiple issues).
3. Fixing it does not demand a level of rigor absent from the rest of the codebase
   (a repo of one-off personal scripts doesn't need exhaustive input validation).
4. It was introduced by the reviewed change — pre-existing bugs are not findings.
5. The author would likely fix it if made aware of it.
6. It does not rely on unstated assumptions about the codebase or the author's
   intent.
7. Speculation that a change *may* disrupt another part of the codebase is not
   enough — identify the other code that is provably affected.
8. It is clearly not just an intentional change by the author.

**How many findings:** output every finding the author would fix if they knew about
it — do not stop at the first qualifying one. If no finding clears the bar, prefer
**no findings** over a speculative finding.

Do not flag: speculative concerns, pre-existing problems, intentional behavior
changes, or trivial style unless it obscures meaning or violates documented
standards.

## Evidence required before reporting

A candidate is not a finding until the review can name all of the following: the exact
triggering scenario; the changed behavior that causes it; the caller, consumer, invariant,
or documented rule that establishes the expected behavior; and a concrete consequence.
Try to falsify the candidate against implementation context, call sites, tests, and the
base revision. Conditional bugs are valid when the condition is realistic and explicit;
"may break something" is not.

For a finding supported by repository instructions, verify that the instruction file
applies to the changed path and cite its smallest supporting line range. Respect normal
precedence (`AGENTS.override.md`, `AGENTS.md`, then configured fallback filenames), with
more-specific files overriding broader ones and user review instructions taking
precedence. Repository rules can support a finding but do not turn harmless differences
into bugs.

## Priorities

Tag every finding title with a priority:

- **P0** — drop everything to fix. Blocking release, operations, or major usage.
  Only for universal issues that do not depend on any assumptions about inputs.
- **P1** — urgent; should be addressed in the next cycle.
- **P2** — normal; to be fixed eventually.
- **P3** — low; nice to have.

## Comment guidelines (when posting findings as review comments)

1. Be clear about *why* the issue is a bug.
2. Communicate severity accurately — never inflate it.
3. Be brief: at most one paragraph, no line breaks within the prose flow.
4. No code chunks longer than 3 lines; wrap code in inline backticks or a fenced
   block.
5. State explicitly the scenarios, environments, or inputs required for the bug to
   arise, up front, so severity is immediately interpretable.
6. Matter-of-fact tone — not accusatory, not overly positive.
7. Written so the author grasps the idea without close reading.
8. No flattery or filler ("Great job…", "Thanks for…").
9. A finding that rests on a repository rule names the rule and its file:line range.

One comment per distinct issue. Keep the anchored line range as short as possible —
avoid ranges over 5–10 lines; pick the subrange that pinpoints the problem, and make
sure it overlaps the diff.

Use ```suggestion blocks only for concrete replacement code: minimal lines, no
commentary inside the block, preserve the exact leading whitespace of the replaced
lines, and do not change outer indentation unless that is the actual fix.

## Overall verdict

End every review with an overall correctness verdict: **correct** means existing
code and tests will not break and the patch is free of bugs and other blocking
issues. Ignore non-blocking issues (style, formatting, typos, docs) for the
verdict. Justify it in 1–3 sentences and note material test gaps or residual risk.

**Verdict mapping.** Any confirmed P0–P2 correctness finding makes the patch incorrect.
A P3 may coexist with a correct verdict only when it is explicitly non-blocking. Missing
or weak tests belong under test gaps unless they violate an applicable repository rule
or are part of a demonstrated behavioral regression.

## Risk prompts (investigation only)

Use these to decide where to inspect more deeply: public/library APIs; CLI and config;
wire/event formats; persisted state and downgrade/upgrade compatibility; authorization
and destructive actions; model-visible context size and cache behavior; concurrency and
failure recovery; and behavior changes without focused tests.

These prompts do not independently qualify as findings and carry no default priority.
Report an issue only after it satisfies every finding condition and its priority follows
from demonstrated impact. Repository-specific size, module-layout, export, and test-file
rules belong in that repository's instruction files, not this generic rubric.
