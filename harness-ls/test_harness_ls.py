#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "rich"]
# ///
"""Tests for harness-ls. Run via `uv run harness-ls/test_harness_ls.py` (or `just test`).

The module filename has a hyphen, so it can't be imported by name — load it from
its path with importlib.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "harness_ls", Path(__file__).parent / "harness-ls.py"
)
hl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hl)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A throwaway $HOME with a ~/.claude inside it.

    The instruction walk stops at Path.home(), so both the config dir and the
    home anchor have to move or the test would climb the real filesystem.
    """
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr(hl.Path, "home", staticmethod(lambda: home))
    return home


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- frontmatter and catalog accounting ------------------------------------


def test_frontmatter_joins_folded_descriptions():
    meta = hl.parse_frontmatter("---\nname: x\ndescription: one\n  two\n---\nbody\n")
    assert meta["description"] == "one two"


def test_frontmatter_block_scalar_marker_is_not_content():
    meta = hl.parse_frontmatter("---\ndescription: |\n  real text\n---\n")
    assert meta["description"] == "real text"


def test_frontmatter_absent_is_empty():
    assert hl.parse_frontmatter("# just a heading\n") == {}


def test_body_summary_strips_heading_and_frontmatter():
    assert hl.body_summary("---\nname: x\n---\n\n# Orient\n\nmore\n") == "Orient"


def test_body_summary_falls_through_to_first_prose_line():
    assert hl.body_summary("Let's check in the changes.\n\nmore\n") == "Let's check in the changes."


def test_catalog_bills_name_and_description_only(fake_home):
    # Frontmatter carries fields that never reach the context (allowed-tools,
    # model); billing the whole block would overstate every catalog.
    skill = fake_home / ".claude" / "skills" / "demo" / "SKILL.md"
    write(
        skill,
        "---\nname: demo\ndescription: does a thing\nallowed-tools: "
        + "x, " * 200
        + "\n---\nbody\n",
    )
    catalog = hl.collect_catalog(None, "skills", "*/SKILL.md", "skills")
    assert catalog.chars == hl.catalog_entry_chars("demo", "does a thing")


def test_skill_name_defaults_to_its_directory(fake_home):
    write(fake_home / ".claude" / "skills" / "from-dir" / "SKILL.md", "no frontmatter here\n")
    catalog = hl.collect_catalog(None, "skills", "*/SKILL.md", "skills")
    assert catalog.items[0]["name"] == "from-dir"


# --- instruction chain -----------------------------------------------------


def test_user_and_project_claude_md_are_both_found(fake_home, tmp_path):
    write(fake_home / ".claude" / "CLAUDE.md", "user guidance\n")
    project = write(tmp_path / "home" / "proj" / "CLAUDE.md", "project guidance\n").parent
    names = [s.name for s in hl.instruction_sources(project)]
    assert any(n.endswith(".claude/CLAUDE.md") for n in names)
    assert any(n.endswith("proj/CLAUDE.md") for n in names)


def test_ancestor_claude_md_is_labeled_as_ancestor(fake_home, tmp_path):
    write(tmp_path / "home" / "parent" / "CLAUDE.md", "inherited\n")
    project = (tmp_path / "home" / "parent" / "child").resolve()
    project.mkdir(parents=True)
    origins = {s.name.split("/")[-2]: s.origin for s in hl.instruction_sources(project)}
    assert origins["parent"] == "ancestor"


def test_walk_stops_at_home(fake_home, tmp_path):
    # A CLAUDE.md above $HOME belongs to no session and must not be billed.
    write(tmp_path / "CLAUDE.md", "should not be read\n")
    project = tmp_path / "home" / "proj"
    project.mkdir(parents=True)
    assert hl.instruction_sources(project) == []


def test_imports_are_followed_and_billed(fake_home, tmp_path):
    write(fake_home / "shared.md", "x" * 100)
    write(fake_home / ".claude" / "CLAUDE.md", "@~/shared.md\n")
    source = hl.instruction_sources(None)[0]
    assert source.chars == len("@~/shared.md\n") + 100
    assert source.detail == "1 import(s)"


def test_import_cycles_terminate(fake_home):
    write(fake_home / ".claude" / "a.md", "@./b.md\n")
    write(fake_home / ".claude" / "b.md", "@./a.md\n")
    imports = hl.resolve_imports(fake_home / ".claude" / "a.md", fake_home / ".claude", set())
    assert [p.name for p in (i[0] for i in imports)] == ["b.md"]


def test_mid_sentence_at_is_not_an_import(fake_home):
    write(fake_home / ".claude" / "CLAUDE.md", "mail me at user@example.com\n")
    assert hl.instruction_sources(None)[0].detail == ""


# --- memory ----------------------------------------------------------------


def test_project_slug_dashes_the_path():
    assert hl.project_slug("/Users/x/projects/y") == "-Users-x-projects-y"


def test_memory_index_is_always_on_bodies_are_on_demand(fake_home, tmp_path):
    project = tmp_path / "home" / "proj"
    project.mkdir(parents=True)
    memory = fake_home / ".claude" / "projects" / hl.project_slug(project) / "memory"
    write(memory / "MEMORY.md", "- [a](a.md)\n")
    write(memory / "a.md", "body\n")
    when = {s.name: s.when for s in hl.memory_sources(project)}
    assert when == {"memory/MEMORY.md": "always", "memory bodies": "on-demand"}


# --- plugins ---------------------------------------------------------------


def _install_plugin(home, plugin_id, scope="user", project_path=None):
    root = home / ".claude" / "plugins" / "cache" / plugin_id
    root.mkdir(parents=True, exist_ok=True)
    manifest = home / ".claude" / "plugins" / "installed_plugins.json"
    data = json.loads(manifest.read_text()) if manifest.exists() else {"plugins": {}}
    entry = {"scope": scope, "installPath": str(root)}
    if project_path:
        entry["projectPath"] = str(project_path)
    data["plugins"][plugin_id] = [entry]
    write(manifest, json.dumps(data))
    return root


def _enable(home, **plugins):
    write(home / ".claude" / "settings.json", json.dumps({"enabledPlugins": plugins}))


def test_disabled_plugins_are_not_counted(fake_home):
    _install_plugin(fake_home, "on@mkt")
    _install_plugin(fake_home, "off@mkt")
    _enable(fake_home, **{"on@mkt": True, "off@mkt": False})
    assert [p for p, _ in hl.enabled_plugin_roots(None)] == ["on@mkt"]


def test_project_scoped_plugin_only_applies_in_its_project(fake_home, tmp_path):
    mine = tmp_path / "home" / "mine"
    other = tmp_path / "home" / "other"
    for path in (mine, other):
        path.mkdir(parents=True)
    _install_plugin(fake_home, "scoped@mkt", scope="project", project_path=mine)
    _enable(fake_home, **{"scoped@mkt": True})
    assert [p for p, _ in hl.enabled_plugin_roots(mine)] == ["scoped@mkt"]
    assert hl.enabled_plugin_roots(other) == []


def test_plugin_skills_join_the_catalog(fake_home):
    root = _install_plugin(fake_home, "pack@mkt")
    _enable(fake_home, **{"pack@mkt": True})
    write(root / "skills" / "thing" / "SKILL.md", "---\nname: thing\ndescription: d\n---\n")
    catalog = hl.collect_catalog(None, "skills", "*/SKILL.md", "skills")
    assert catalog.items[0]["origin"] == "plugin:pack@mkt"
    assert "1 from plugins" in catalog.detail


# --- MCP, hooks, settings --------------------------------------------------


def test_mcp_found_in_project_file_and_plugin_manifest(fake_home, tmp_path):
    project = tmp_path / "home" / "proj"
    project.mkdir(parents=True)
    write(project / ".mcp.json", json.dumps({"mcpServers": {"local": {}}}))
    root = _install_plugin(fake_home, "pack@mkt")
    _enable(fake_home, **{"pack@mkt": True})
    write(root / ".claude-plugin" / "plugin.json", json.dumps({"mcpServers": {"from-plugin": {}}}))
    names = {i["name"] for i in hl.mcp_sources(project)[0].items}
    assert names == {"local", "from-plugin"}


def test_mcp_cost_is_unknown_not_zero(fake_home, tmp_path):
    project = tmp_path / "home" / "proj"
    project.mkdir(parents=True)
    write(project / ".mcp.json", json.dumps({"mcpServers": {"local": {}}}))
    source = hl.mcp_sources(project)[0]
    assert source.chars == 0
    assert "unmeasurable" in source.detail
    # Zero chars renders as "?" so an unmeasured cost never reads as free.
    assert hl.fmt_tokens(source.tokens) == "?"


def test_user_prompt_submit_hook_is_per_turn(fake_home):
    write(
        fake_home / ".claude" / "settings.json",
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"matcher": "", "hooks": [{"command": "enrich.py"}]}],
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "guard.py"}]}],
                }
            }
        ),
    )
    by_name = {s.name: s for s in hl.hook_sources(None)}
    assert by_name["hook: UserPromptSubmit"].when == "per-turn"
    assert by_name["hooks (non-injecting)"].when == "behavioral"
    assert by_name["hook: UserPromptSubmit"].items[0]["command"] == "enrich.py"


def test_local_settings_layer_wins(fake_home, tmp_path):
    project = tmp_path / "home" / "proj"
    write(fake_home / ".claude" / "settings.json", json.dumps({"model": "user-model"}))
    write(project / ".claude" / "settings.local.json", json.dumps({"model": "local-model"}))
    assert hl.merged_settings(project)["model"] == "local-model"


# --- survey and rendering --------------------------------------------------


def test_no_project_survey_skips_project_scoped_sources(fake_home, tmp_path):
    write(fake_home / ".claude" / "CLAUDE.md", "user\n")
    kinds = {s.kind for s in hl.survey(None)}
    assert "instructions" in kinds
    assert "env" not in kinds


def test_json_payload_totals_match_sources(fake_home):
    write(fake_home / ".claude" / "CLAUDE.md", "x" * 400)
    sources = hl.survey(None)
    always = sum(s.tokens for s in sources if s.when == "always")
    assert always == 100


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
