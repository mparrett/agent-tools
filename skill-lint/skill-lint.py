#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = ["pyyaml>=6"]
# ///
"""Check a SKILL.md against the contract the harness and Claude actually apply.

Not a style linter. Every rule here is either a limit Anthropic publishes or a
failure mode that has already shipped in this repo:

- **Frontmatter that is not YAML fails silently.** The harness falls back to
  listing the skill under its first heading and the `description` never reaches
  Claude, so the skill simply never fires. Two of these skills had exactly that
  and were fixed, and nothing would have caught it.
- **A reference file over 100 lines with no contents list gets read in part.**
  Claude previews long files (`head -100` and friends), so the tail goes
  missing. In `pr-shepherd/references/review-rubric.md` the tail is the note
  demoting the mined Code Review Rules to investigation-only — a preview reads
  the rules without the demotion.
- **A reference that links to another reference is read partially too.** The
  guidance asks for one level deep from SKILL.md for the same reason.

Rules the guidance states that are deliberately *not* here:

- **Evaluations.** Their absence is real (see the 2026-09-14 skills audit) but a
  linter cannot tell a good evaluation from an empty file.
- **Third person vs imperative.** The published warning targets first and second
  person ("I can help you…", "You can use this to…"), which is checked. Flagging
  every imperative opener would fail all five of this repo's skills over a prose
  example rather than a stated rule.
- **Windows-style paths.** Every backslash in these skills so far has been a
  regex or a shell escape inside a fenced block, never a path, so the check
  produced only false positives on a repo that has never run on Windows.
- **Flag drift** — whether a backticked `--flag` in a skill body still
  exists in the tool it names. Worth doing and not here: attribution is the hard
  part, and a checker that guesses which command a flag belongs to is noise, and
  noise in a doc gate gets switched off.

Usage:

    skill-lint <path>...        # SKILL.md files, or directories to search
    skill-lint --json <path>    # machine-readable

Exit 0 when every skill is clean, 1 when any problem is found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

# Anthropic's published limits (skill authoring best practices, read 2026-09-14).
NAME_MAX = 64
# Hyphens separate segments; they do not lead, trail, or double up. `-demo`,
# `demo-` and `demo--skill` are slugs nobody meant to write.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED = ("anthropic", "claude")
DESC_MAX = 1024
BODY_MAX_LINES = 500
# The guidance asks for a contents list "for reference files longer than 100
# lines" so a partial read still shows the file's full scope.
REF_TOC_MIN_LINES = 100

# "Cannot contain XML tags" is about markup riding into the system prompt, so
# only real markup shapes count: a closing tag, a self-closing tag, or a tag
# carrying attributes. A bare `<uuid>` or `<file/repo>` is a placeholder — the
# convention find-session's triggers use, verified routing correctly for bare session UUIDs —
# and failing a shipped skill over it would be the noise that gets a gate
# switched off.
XML_TAG_RE = re.compile(
    r"</[a-zA-Z][^>]*>|<[a-zA-Z][a-zA-Z0-9-]*\s*/>|<[a-zA-Z][a-zA-Z0-9-]*\s+[^>]*=[^>]*>"
)
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TOC_RE = re.compile(r"^#+\s*(contents|table of contents|in this file)\b", re.I | re.M)
HEADING_RE = re.compile(r"^#{2,}\s+(.+?)\s*$", re.M)
# A heading named "Contents" is not proof of a table of contents. pr-body.md's
# "## Contents" lists what a PR body must contain, not where to find things in
# the file — accepting it would pass a long file that still gets read in part.
# A real one names the file's own sections, so require that it name at least two.
TOC_MIN_HEADINGS_NAMED = 2
# A rule is `Name` or `Name(pattern)`; the pattern itself may hold anything.
TOOL_RULE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.*\))?$")
# One rule inside a space-separated string: a name, optionally with a pattern
# whose own parentheses may contain spaces.
RULE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\([^)]*\))?")
# Only a first- or second-person *opener* is flagged: quoted trigger phrases
# ("I lost the chat about X") legitimately carry these pronouns mid-description.
PERSON_RE = re.compile(r"\s*(I|I'm|I can|We|We can|You|You can|You should|You'll)\b")


class Problem:
    """One failed check, with the file it belongs to."""

    def __init__(self, path: Path, message: str) -> None:
        self.path, self.message = path, message

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def split_frontmatter(text: str) -> tuple[str | None, str, str | None]:
    """Return (frontmatter, body, error). Mirrors how the harness reads the file:
    a fence that is absent or unclosed means there is no frontmatter at all."""
    if not text.startswith("---\n"):
        return None, text, "file must start with a `---` frontmatter fence"
    end = text.find("\n---", 4)
    if end < 0:
        return None, text, "frontmatter fence is never closed"
    return text[4:end], text[end + 4 :], None


def check_name(name: str, skill_dir: Path | None = None) -> list[str]:
    problems = []
    if not name:
        problems.append("name is missing")
        return problems
    if len(name) > NAME_MAX:
        problems.append(f"name is {len(name)} chars; the limit is {NAME_MAX}")
    if not NAME_RE.match(name):
        problems.append(f"name {name!r} must be lowercase letters, digits or hyphens only")
    for word in RESERVED:
        if word in name.lower():
            problems.append(f"name {name!r} contains the reserved word {word!r}")
    if XML_TAG_RE.search(name):
        problems.append("name contains an XML tag")
    # The directory is how a skill is installed and invoked; a `name` that
    # disagrees with it is the version nobody sees.
    if skill_dir is not None and name and skill_dir.name != name:
        problems.append(f"name {name!r} does not match its directory {skill_dir.name!r}")
    return problems


def check_description(desc: str) -> list[str]:
    problems = []
    if not desc.strip():
        # Worth its own message: this is the field that decides whether the
        # skill is ever selected, and an empty one fails silently.
        problems.append("description is empty; it is all Claude sees before loading the skill")
        return problems
    if len(desc) > DESC_MAX:
        problems.append(f"description is {len(desc)} chars; the limit is {DESC_MAX}")
    if XML_TAG_RE.search(desc):
        problems.append("description contains an XML tag")
    if PERSON_RE.match(desc):
        problems.append("description opens in the first or second person; write it in the third")
    return problems


def check_allowed_tools(value: object) -> list[str]:
    """`allowed-tools` must read as discrete rules. A YAML list or a
    comma-separated string is unambiguous; one space-separated string is not,
    because the patterns themselves hold spaces — which is what a folded block
    scalar silently turns a list of rules into."""
    if value is None:
        return []
    if isinstance(value, list):
        entries = [str(v).strip() for v in value]
    elif isinstance(value, str):
        if "," in value:
            entries = [e.strip() for e in value.split(",") if e.strip()]
        else:
            # Space-separated is tokenized rather than rejected. A rule's own
            # pattern can hold spaces (`Bash(git status)`), so split on rule
            # shape instead: if the tokens account for the whole string it is a
            # list of rules, and only what is left over is a problem.
            entries = RULE_TOKEN_RE.findall(value)
            if RULE_TOKEN_RE.sub("", value).strip():
                return [
                    f"allowed-tools {value!r} does not read as tool rules; write it as a "
                    "YAML list (one `- Bash(...)` per line) or comma-separate them"
                ]
    else:
        return [f"allowed-tools must be a list or a string, not {type(value).__name__}"]
    return [
        f"allowed-tools entry {e!r} does not read as a tool rule (Name or Name(pattern))"
        for e in entries
        if not TOOL_RULE_RE.match(e)
    ]


def local_links(body: str, skill_dir: Path | None = None) -> list[str]:
    """Files beside the skill that the body points at, ignoring URLs and anchors.

    Both spellings count. The guidance's examples use markdown links, but a
    skill is as likely to name a reference in prose or a table — pr-shepherd
    names all three of its reference files in backticks and none as a link, so
    a link-only reader reports the one skill with references as having none.
    A backticked path is only followed when it resolves, which keeps prose like
    `gh pr view` from being read as a filename.
    """
    out = []
    for target in MD_LINK_RE.findall(body):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue
        out.append(target.split("#", 1)[0])
    if skill_dir is not None:
        for target in re.findall(r"`([^`\s]+\.(?:md|py|sh|html|json))`", body):
            if target.startswith(("/", "~", "http")):
                continue
            # Existence cannot be the test on its own, or deleting a reference
            # deletes the finding with it. A path under a directory that exists
            # beside the skill is a reference whether or not the file is still
            # there — which is what catches a deleted `references/foo.md`.
            # Without that, a bare `meta.json` or `codex-run/README.md` (both
            # real prose in these skills, neither a sibling file) would be
            # reported as missing.
            parent = target.split("/", 1)[0] if "/" in target else ""
            if (skill_dir / target).exists() or (parent and (skill_dir / parent).is_dir()):
                out.append(target)
    return list(dict.fromkeys(t for t in out if t))


def has_contents_list(text: str) -> bool:
    """True when the file opens a section that actually navigates it.

    Matching the heading alone is not enough: a "Contents" section may be prose
    about the subject rather than a map of the file. A navigational one names
    the file's own sections, so that is what is checked.
    """
    match = TOC_RE.search(text)
    if not match:
        return False
    headings = [h.strip() for h in HEADING_RE.findall(text)]
    # Stop at the next heading: reading past it would find the file's own
    # section titles in the body and count them as if the list had named them.
    rest = text[match.end() :]
    nxt = re.search(r"^#{1,6}\s", rest, re.M)
    block = rest[: nxt.start()] if nxt else rest
    named = sum(
        1
        for h in headings
        if h.lower() not in ("contents", "table of contents", "in this file")
        and h.lower() in block.lower()
    )
    return named >= TOC_MIN_HEADINGS_NAMED


def check_references(skill_dir: Path, body: str, skill_file: Path) -> list[str]:
    """Referenced files must exist, sit one level deep, and carry a contents
    list once they are long enough to be previewed rather than read.

    A skill that names `SKILL.md` in its own prose is not referencing anything,
    so it is skipped: counting it makes the skill its own reference file, which
    reads its body against the reference-length rule and reports its real
    references as nested one level too deep.
    """
    problems = []
    for target in local_links(body, skill_dir):
        ref = skill_dir / target
        if ref.resolve() == skill_file.resolve():
            continue
        if not ref.exists():
            problems.append(f"referenced file {target} does not exist beside SKILL.md")
            continue
        if ref.suffix != ".md":
            continue
        ref_text = ref.read_text()
        ref_lines = len(ref_text.splitlines())
        if ref_lines > REF_TOC_MIN_LINES and not has_contents_list(ref_text):
            problems.append(
                f"{target} is {ref_lines} lines and has no contents list; "
                f"over {REF_TOC_MIN_LINES} lines Claude may read it in part"
            )
        for nested in local_links(ref_text, ref.parent):
            if (ref.parent / nested).exists():
                problems.append(
                    f"{target} links to {nested}; keep references one level deep "
                    "from SKILL.md so they are read in full"
                )
    return problems


def check_skill(path: Path) -> list[Problem]:
    text = path.read_text()
    front, body, err = split_frontmatter(text)
    if err:
        return [Problem(path, err)]
    try:
        meta = yaml.safe_load(front) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at frontmatter line {mark.line + 1}" if mark else ""
        return [
            Problem(
                path,
                f"frontmatter is not YAML{where}: {getattr(e, 'problem', e)}. The harness "
                "falls back silently when this happens and the description never reaches "
                "Claude, so the skill never fires. A multi-line field holding `: ` does "
                "it; write the field as a `>-` block scalar.",
            )
        ]
    if not isinstance(meta, dict):
        return [Problem(path, "frontmatter must be a mapping")]

    # Type first: `name: 123` and `description: [a, b]` load as an int and a
    # list, and str() would launder both into something that passes every rule
    # below. The contract defines them as strings.
    messages = []
    raw_name, raw_desc = meta.get("name"), meta.get("description")
    if raw_name is not None and not isinstance(raw_name, str):
        messages.append(f"name must be a string, not {type(raw_name).__name__}")
        raw_name = ""
    if raw_desc is not None and not isinstance(raw_desc, str):
        messages.append(f"description must be a string, not {type(raw_desc).__name__}")
        raw_desc = ""
    messages += check_name(str(raw_name or ""), path.parent)
    messages += check_description(str(raw_desc or ""))
    messages += check_allowed_tools(meta.get("allowed-tools"))
    body_lines = len(body.splitlines())
    if body_lines >= BODY_MAX_LINES:
        messages.append(
            f"body is {body_lines} lines; keep it under {BODY_MAX_LINES} by moving "
            "detail into reference files"
        )
    messages += check_references(path.parent, body, path)
    return [Problem(path, m) for m in messages]


def find_skills(targets: list[Path]) -> list[Path]:
    """A path may be a SKILL.md or a directory to search. Sorted so output is
    stable across filesystems."""
    found: list[Path] = []
    for target in targets:
        if target.is_dir():
            found.extend(sorted(target.rglob("SKILL.md")))
        else:
            found.append(target)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check SKILL.md files against Anthropic's published skill contract."
    )
    parser.add_argument("paths", nargs="+", type=Path, help="SKILL.md files, or dirs to search")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    skills = find_skills(args.paths)
    missing = [p for p in skills if not p.exists()]
    problems: list[Problem] = []
    for path in skills:
        if path.exists():
            problems.extend(check_skill(path))
    for path in missing:
        problems.append(Problem(path, "no such file"))

    if args.json:
        print(
            json.dumps(
                {
                    "checked": [str(p) for p in skills],
                    "problems": [{"path": str(p.path), "message": p.message} for p in problems],
                },
                indent=2,
            )
        )
    elif problems:
        by_path: dict[Path, list[str]] = {}
        for p in problems:
            by_path.setdefault(p.path, []).append(p.message)
        for path, messages in by_path.items():
            print(f"{path}: {len(messages)} problem(s)")
            for m in messages:
                print(f"  - {m}")
    else:
        print(f"{len(skills)} skill(s) checked; frontmatter parses and every limit holds")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
