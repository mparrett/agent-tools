#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Tests for the `just link` / `just unlink` / `just link-skills` recipes.

Run via `uv run test_install_recipes.py` (or `just test`).

These are the only shipped shell that takes an argument from the caller, and
they run `ln`/`rm` against a directory that argument names -- so the tests that
matter are about what happens with an argument nobody sanitized. They drive the
real recipes through `just` rather than re-implementing them, because the defect
class here lives in the interpolation `just` does, not in the shell logic.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
TOOLS = sorted(p.name for p in (ROOT / "bin").iterdir())
SKILLS = sorted(p.name for p in (ROOT / "claude-code-skills").iterdir() if p.is_dir())

pytestmark = pytest.mark.skipif(shutil.which("just") is None, reason="just not installed")


def run(recipe: str, dest) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["just", recipe, str(dest)], cwd=ROOT, capture_output=True, text=True, check=True
    )


def _clone(tmp_path: Path) -> Path:
    """A throwaway clone on `main`.

    It inherits whatever branch this tree is on, and `sync` checks the branch
    name before anything else, so name it main explicitly.
    """
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", str(ROOT), str(clone)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "-q", "-B", "main"], cwd=clone, check=True)
    return clone


def _sync(clone: Path) -> subprocess.CompletedProcess:
    """Run `just sync` in `clone` using *this* working tree's justfile.

    A clone carries only committed content, so a recipe still being written
    would not be in it.
    """
    return subprocess.run(
        ["just", "--justfile", str(ROOT / "justfile"), "--working-directory", str(clone), "sync"],
        capture_output=True,
        text=True,
    )


def test_link_installs_every_tool_pointing_at_this_clone(tmp_path):
    dest = tmp_path / "bin"
    run("link", dest)
    for name in TOOLS:
        assert (dest / name).is_symlink()
        assert os.readlink(dest / name) == str(ROOT / "bin" / name)


def test_link_is_idempotent(tmp_path):
    dest = tmp_path / "bin"
    run("link", dest)
    out = run("link", dest).stdout
    assert out.count("ok") == len(TOOLS)
    assert "linked" not in out


def test_link_never_clobbers_a_name_it_does_not_own(tmp_path):
    dest = tmp_path / "bin"
    dest.mkdir()
    (dest / "mdserve").write_text("someone else's script\n")  # a regular file
    (dest / "mdfeed").symlink_to("/usr/bin/true")  # another clone's link
    run("link", dest)
    assert (dest / "mdserve").read_text() == "someone else's script\n"
    assert os.readlink(dest / "mdfeed") == "/usr/bin/true"


def test_unlink_removes_only_this_clone_s_links(tmp_path):
    dest = tmp_path / "bin"
    dest.mkdir()
    (dest / "mdfeed").symlink_to("/usr/bin/true")
    run("link", dest)
    assert (dest / "claude-ls").is_symlink()

    run("unlink", dest)
    assert not (dest / "claude-ls").exists()
    # Not ours, so not ours to remove -- even though the name is one we install.
    assert os.readlink(dest / "mdfeed") == "/usr/bin/true"


@pytest.mark.parametrize(
    "name",
    ["with space", 'has"quote', "has'apostrophe", "semi;colon", "back`tick`", "dollar$var"],
    ids=["space", "dquote", "squote", "semicolon", "backtick", "dollar"],
)
def test_a_directory_name_needing_quoting_is_taken_literally(tmp_path, name):
    dest = tmp_path / name
    run("link", dest)
    assert (dest / "claude-ls").is_symlink(), f"{name!r} did not resolve to a literal directory"
    run("unlink", dest)
    assert not (dest / "claude-ls").exists()


def test_a_substitution_in_the_directory_name_is_not_executed(tmp_path):
    # The reported defect: `dest="{{DIR}}"` interpolates the raw argument into the
    # recipe's script text, so the shell expanded it. `just link '/tmp/x$(id)'`
    # installed into /tmp/x plus the output of `id`; worse payloads run anything.
    canary = tmp_path / "canary"
    dest = tmp_path / f"x$(touch {canary})"
    run("link", dest)
    assert not canary.exists(), "the recipe executed a substitution from its argument"
    assert (dest / "claude-ls").is_symlink()


def test_link_skills_installs_every_skill_pointing_at_this_clone(tmp_path):
    """Every skill, not a hand-maintained subset.

    codex-review shipped with no link for exactly this reason: the links were
    made by hand, and nothing enumerated what the repo actually carries.
    """
    dest = tmp_path / "skills"
    run("link-skills", dest)
    for name in SKILLS:
        assert (dest / name).is_symlink(), f"{name} was not linked"
        assert os.readlink(dest / name) == str(ROOT / "claude-code-skills" / name)


def test_link_skills_is_idempotent(tmp_path):
    dest = tmp_path / "skills"
    run("link-skills", dest)
    out = run("link-skills", dest).stdout
    assert out.count("ok") == len(SKILLS)
    assert "linked" not in out


def test_link_skills_never_clobbers_a_name_it_does_not_own(tmp_path):
    dest = tmp_path / "skills"
    dest.mkdir()
    (dest / "pr-shepherd").symlink_to("/tmp")  # another clone's skill
    (dest / "pr-watch").mkdir()  # a hand-written one
    run("link-skills", dest)
    assert os.readlink(dest / "pr-shepherd") == "/tmp"
    assert (dest / "pr-watch").is_dir() and not (dest / "pr-watch").is_symlink()


def test_unlink_skills_removes_only_this_clone_s_links(tmp_path):
    dest = tmp_path / "skills"
    dest.mkdir()
    (dest / "pr-watch").symlink_to("/tmp")
    run("link-skills", dest)
    assert (dest / "pr-shepherd").is_symlink()

    run("unlink-skills", dest)
    assert not (dest / "pr-shepherd").exists()
    assert os.readlink(dest / "pr-watch") == "/tmp"


def test_link_skills_takes_a_directory_name_needing_quoting_literally(tmp_path):
    # `link-skills` interpolates its argument the same way `link` does; the
    # substitution defect below would be the same defect in a second place.
    dest = tmp_path / "has space$(touch {})".format(tmp_path / "canary")
    run("link-skills", dest)
    assert not (tmp_path / "canary").exists()
    assert (dest / "pr-shepherd").is_symlink()


def test_sync_refuses_to_pull_over_an_out_of_band_change(tmp_path):
    """The tripwire, which is the whole reason a read-only clone exists.

    A skill edited through its ~/.claude path lands in that clone's working
    tree with no remote copy, and nobody cd's there to notice. Pulling over it
    destroys it -- so a dirty tree stops the sync and gets printed instead.
    """
    clone = _clone(tmp_path)
    (clone / "claude-code-skills" / "pr-shepherd" / "SKILL.md").write_text("edited out of band\n")
    proc = _sync(clone)
    assert proc.returncode != 0, "sync pulled over an uncommitted change"
    assert "out-of-band changes" in proc.stderr
    assert "SKILL.md" in proc.stderr
    assert (clone / "claude-code-skills" / "pr-shepherd" / "SKILL.md").read_text() == (
        "edited out of band\n"
    )


def test_sync_refuses_a_clone_that_is_not_on_main(tmp_path):
    clone = _clone(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "wip"], cwd=clone, check=True)
    proc = _sync(clone)
    assert proc.returncode != 0
    assert "not main" in proc.stderr


def _just(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(["just", *args], cwd=ROOT, capture_output=True, text=True, env=env)


def test_ro_lives_under_repos_ro(tmp_path):
    proc = _just("--evaluate", "ro", env_extra={"REPOS_RO": str(tmp_path)})
    assert proc.stdout == str(tmp_path / "agent-tools")


def test_ship_refuses_when_there_is_no_read_only_clone(tmp_path):
    """Checked before anything touches the network or the clone."""
    proc = _just("ship", env_extra={"REPOS_RO": str(tmp_path)})
    assert proc.returncode != 0
    assert "no read-only clone" in proc.stderr and "REPOS_RO" in proc.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
