#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "pyyaml>=6"]
# ///
"""Tests for skill-lint. Run via `uv run skill-lint/test_skill_lint.py` (or `just test`).

The module filename has a hyphen, so it can't be imported by name — load it
from its path with importlib.

Two kinds of test here. Most build a fixture skill and assert one check fires
or stays quiet. The last one runs the linter over this repo's own five skills,
which is the assertion that actually protects anything: a gate nothing is held
to goes stale.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "skill_lint", Path(__file__).parent / "skill-lint.py"
)
sl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl)

REPO = Path(__file__).resolve().parent.parent


def write_skill(tmp_path, frontmatter, body="# Title\n\nSome instructions.\n", name="demo"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(f"---\n{frontmatter}\n---\n{body}")
    return p


def messages(path):
    return [p.message for p in sl.check_skill(path)]


GOOD = "name: demo\ndescription: Does a thing. Use when the user asks for the thing."


def test_a_well_formed_skill_has_no_problems(tmp_path):
    assert messages(write_skill(tmp_path, GOOD)) == []


def test_missing_frontmatter_fence_is_reported(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("# No frontmatter\n")
    assert "must start with a `---` frontmatter fence" in messages(p)[0]


def test_unclosed_frontmatter_fence_is_reported(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: demo\ndescription: x\n")
    assert "never closed" in messages(p)[0]


def test_frontmatter_that_is_not_yaml_says_the_skill_never_fires(tmp_path):
    # The failure this tool exists for: the harness falls back silently, so the
    # description never reaches Claude. An unquoted `: ` inside a value does it.
    p = write_skill(tmp_path, "name: demo\ndescription: Use when: the user asks")
    assert "not YAML" in messages(p)[0]


def test_name_rules(tmp_path):
    assert any(
        "lowercase" in m
        for m in messages(write_skill(tmp_path, "name: Demo_Skill\ndescription: x"))
    )
    assert any(
        "reserved word" in m
        for m in messages(write_skill(tmp_path, "name: claude-helper\ndescription: x"))
    )
    assert any(
        "64" in m for m in messages(write_skill(tmp_path, f"name: {'a' * 65}\ndescription: x"))
    )


def test_empty_description_is_reported(tmp_path):
    assert any(
        "description is empty" in m
        for m in messages(write_skill(tmp_path, 'name: demo\ndescription: ""'))
    )


def test_description_over_the_limit_is_reported(tmp_path):
    p = write_skill(tmp_path, f"name: demo\ndescription: {'x' * 1025}")
    assert any("the limit is 1024" in m for m in messages(p))


def test_first_and_second_person_openers_are_reported(tmp_path):
    for opener in ("I can help you process files.", "You can use this to process files."):
        p = write_skill(tmp_path, f"name: demo\ndescription: {opener}")
        assert any("third" in m for m in messages(p)), opener


def test_a_quoted_pronoun_mid_description_is_not_flagged(tmp_path):
    # find-session's triggers quote the user verbatim: "I lost the chat about X".
    p = write_skill(
        tmp_path,
        'name: demo\ndescription: >-\n  Finds a session. Use when the user says "I lost the chat about X".',
    )
    assert messages(p) == []


def test_placeholder_angle_brackets_are_not_xml_tags(tmp_path):
    # find-session ships `<uuid>` and `<file/repo>` in its description.
    p = write_skill(
        tmp_path,
        "name: demo\ndescription: Finds a session by <uuid> or <file/repo>. Use when asked.",
    )
    assert messages(p) == []


def test_real_markup_in_a_description_is_reported(tmp_path):
    for desc in ("Does <b>things</b>.", "Does things.<br/>", 'Does <a href="x">things</a>.'):
        p = write_skill(tmp_path, f"name: demo\ndescription: '{desc}'")
        assert any("XML tag" in m for m in messages(p)), desc


def test_body_over_500_lines_is_reported(tmp_path):
    p = write_skill(tmp_path, GOOD, body="x\n" * 501)
    assert any("keep it under 500" in m for m in messages(p))


def test_a_missing_reference_is_reported(tmp_path):
    p = write_skill(tmp_path, GOOD, body="See [the guide](references/guide.md).\n")
    assert any("does not exist" in m for m in messages(p))


def test_a_long_reference_without_a_contents_list_is_reported(tmp_path):
    p = write_skill(tmp_path, GOOD, body="See [the guide](guide.md).\n")
    (p.parent / "guide.md").write_text("# Guide\n" + "line\n" * 120)
    assert any("has no contents list" in m for m in messages(p))


def test_a_long_reference_with_a_contents_list_is_accepted(tmp_path):
    p = write_skill(tmp_path, GOOD, body="See [the guide](guide.md).\n")
    (p.parent / "guide.md").write_text(
        "# Guide\n\n## Contents\n\n- Setup\n- Running it\n\n"
        "## Setup\n" + "line\n" * 60 + "\n## Running it\n" + "line\n" * 60
    )
    assert messages(p) == []


def test_a_contents_section_that_does_not_navigate_the_file_is_not_a_toc(tmp_path):
    # pr-body.md's "## Contents" lists what a PR body must contain, not where
    # things are in the file. Accepting it would pass a long file that still
    # gets read in part.
    p = write_skill(tmp_path, GOOD, body="See [the guide](guide.md).\n")
    (p.parent / "guide.md").write_text(
        "# Guide\n\n## Contents\n\n- Why before what\n- Net change only\n\n"
        "## Setup\n" + "line\n" * 60 + "\n## Running it\n" + "line\n" * 60
    )
    assert any("has no contents list" in m for m in messages(p))


def test_a_short_reference_needs_no_contents_list(tmp_path):
    p = write_skill(tmp_path, GOOD, body="See [the guide](guide.md).\n")
    (p.parent / "guide.md").write_text("# Guide\n" + "line\n" * 10)
    assert messages(p) == []


def test_a_backticked_reference_is_followed_too(tmp_path):
    # pr-shepherd names every reference in backticks and none as a link.
    p = write_skill(tmp_path, GOOD, body="Read `references/guide.md` first.\n")
    (p.parent / "references").mkdir()
    (p.parent / "references" / "guide.md").write_text("# Guide\n" + "line\n" * 120)
    assert any("has no contents list" in m for m in messages(p))


def test_backticked_prose_that_is_not_a_file_is_ignored(tmp_path):
    p = write_skill(tmp_path, GOOD, body="Run `gh pr view` and read `nope.md`.\n")
    assert messages(p) == []


def test_a_nested_reference_is_reported(tmp_path):
    p = write_skill(tmp_path, GOOD, body="See [the guide](guide.md).\n")
    (p.parent / "guide.md").write_text("# Guide\n\nSee [details](details.md).\n")
    (p.parent / "details.md").write_text("# Details\n")
    assert any("one level deep" in m for m in messages(p))


def test_naming_the_skill_file_in_its_own_body_is_not_a_reference(tmp_path):
    # Counting it would measure the body against the reference-length rule and
    # report the skill's real references as nested a level too deep.
    p = write_skill(tmp_path, GOOD, body="The policy in `SKILL.md` applies.\n" + "x\n" * 120)
    assert messages(p) == []


def test_several_rules_in_one_space_separated_string_are_accepted(tmp_path):
    # This used to be reported as a folded-scalar accident. It is a documented
    # form, and a rule pattern holds spaces of its own, so the string is
    # tokenized on rule shape instead and only a leftover is a problem.
    p = write_skill(
        tmp_path, GOOD.replace("\n", "\nallowed-tools: Bash(git status) Bash(gh pr view)\n", 1)
    )
    assert messages(p) == []


def test_allowed_tools_as_a_list_is_accepted(tmp_path):
    p = write_skill(
        tmp_path,
        "name: demo\nallowed-tools:\n  - Bash(git status)\n  - Read\ndescription: Does a thing.",
    )
    assert messages(p) == []


def test_find_skills_searches_a_directory(tmp_path):
    write_skill(tmp_path, GOOD, name="one")
    write_skill(tmp_path, GOOD, name="two")
    assert len(sl.find_skills([tmp_path])) == 2


def test_main_exits_nonzero_on_a_problem(tmp_path, capsys):
    p = write_skill(tmp_path, "name: Bad_Name\ndescription: x")
    assert sl.main([str(p)]) == 1
    assert "lowercase" in capsys.readouterr().out


def test_main_json_output_lists_problems(tmp_path, capsys):
    import json

    p = write_skill(tmp_path, "name: Bad_Name\ndescription: x")
    sl.main([str(p), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["checked"] == [str(p)]
    assert any("lowercase" in item["message"] for item in payload["problems"])


def test_this_repos_own_skills_pass():
    """The assertion that keeps the gate honest — a linter nothing is held to
    goes stale. If this fails, fix the skill rather than loosening the rule."""
    skills = sl.find_skills([REPO / "claude-code-skills"])
    assert skills, "no skills found; the path convention changed"
    problems = [str(p) for s in skills for p in sl.check_skill(s)]
    assert problems == []


def test_a_deleted_backticked_reference_is_reported(tmp_path):
    # The gap the existence guard used to leave: delete a reference that is
    # named in backticks and nothing notices. pr-shepherd names all three of
    # its references that way.
    p = write_skill(tmp_path, GOOD, body="Read `references/guide.md` first.\n")
    (p.parent / "references").mkdir()
    assert any("does not exist" in m for m in messages(p))


def test_prose_paths_that_are_not_siblings_stay_quiet(tmp_path):
    # codex-review names `meta.json`, `stderr.log` and `codex-run/README.md`;
    # none is a file beside the skill and none is a broken reference.
    p = write_skill(tmp_path, GOOD, body="Read `meta.json` and `codex-run/README.md`.\n")
    assert messages(p) == []


def test_space_separated_allowed_tools_is_accepted(tmp_path):
    for value in ("Read Grep", "Bash(git:*) Bash(jq:*) Read", "Bash(git status) Read"):
        p = write_skill(tmp_path, f"name: demo\nallowed-tools: {value}\ndescription: Does a thing.")
        assert messages(p) == [], value


def test_allowed_tools_that_is_not_rules_is_reported(tmp_path):
    p = write_skill(
        tmp_path,
        'name: demo\nallowed-tools: "Bash(git status) -- and then some prose"\ndescription: Does a thing.',
    )
    assert any("does not read as tool rules" in m for m in messages(p))


def test_non_string_metadata_is_reported(tmp_path):
    assert any(
        "name must be a string" in m
        for m in messages(write_skill(tmp_path, "name: 123\ndescription: Does a thing."))
    )
    assert any(
        "description must be a string" in m
        for m in messages(write_skill(tmp_path, "name: demo\ndescription: [Does, thing]"))
    )


def test_hyphen_edges_and_doubles_are_reported(tmp_path):
    for bad in ("-demo", "demo-", "demo--skill"):
        d = tmp_path / bad
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {bad}\ndescription: Does a thing.\n---\n# T\n")
        assert any("lowercase letters" in m for m in messages(d / "SKILL.md")), bad


def test_a_name_that_does_not_match_its_directory_is_reported(tmp_path):
    d = tmp_path / "actual-dir"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: other-name\ndescription: Does a thing.\n---\n# T\n")
    assert any("does not match its directory" in m for m in messages(d / "SKILL.md"))


def test_the_contents_threshold_counts_lines_not_newlines(tmp_path):
    # 101 lines with no trailing newline is 100 newline characters.
    p = write_skill(tmp_path, GOOD, body="See [the guide](guide.md).\n")
    (p.parent / "guide.md").write_text("\n".join(f"line {i}" for i in range(101)))
    assert any("101 lines" in m for m in messages(p))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
