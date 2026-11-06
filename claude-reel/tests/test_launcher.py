"""Tests for the `claude-reel` launcher.

The launcher decides which interpreter runs the package, so these run it as a
subprocess rather than importing anything.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "claude-reel"


def _floor(text, key):
    """The (major, minor) in a `requires-python = ">=X.Y"` line."""
    match = re.search(rf'{key}\s*=\s*"(>=\s*(\d+)\.(\d+))"', text)
    assert match, f"no simple >=X.Y {key} bound found"
    return int(match.group(2)), int(match.group(3))


def test_launcher_floor_matches_requires_python():
    """The floor is stated in the script's inline metadata and again in pyproject.

    uv reads the inline block and nothing else when running a script, so the two can
    drift silently — and the launcher would then hand the package an interpreter the
    project metadata already rejects.
    """
    inline = _floor(LAUNCHER.read_text(encoding="utf-8"), "requires-python")
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = metadata["project"]["requires-python"]
    match = re.fullmatch(r">=\s*(\d+)\.(\d+)", declared.strip())
    assert match, f"requires-python {declared!r} is no longer a simple >=X.Y bound"
    assert inline == (int(match.group(1)), int(match.group(2)))


def test_runs_without_a_modern_python_on_path():
    """The bug this replaced: the launcher exec'd whatever `python3` resolved to.

    On macOS that is often 3.9, and the package then dies during import with a
    TypeError about `Path | None` — a message that names a type annotation rather
    than the interpreter. uv has to satisfy requires-python itself, so a PATH
    carrying only an old interpreter must still produce a working run.
    """
    uv_dir = Path(_which("uv")).parent
    proc = subprocess.run(
        [str(LAUNCHER), "--version"],
        capture_output=True,
        text=True,
        # /usr/bin carries macOS's 3.9 and no newer interpreter; uv itself has to
        # stay reachable, since it's what the shebang invokes.
        env={**os.environ, "PATH": f"/usr/bin:/bin:{uv_dir}"},
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip()
    assert "unsupported operand" not in proc.stderr


def _which(name):
    found = shutil.which(name)
    assert found, f"{name} not on PATH; every tool in this repo needs it"
    return found


def test_runs_through_a_symlink(tmp_path):
    """`bin/` exposes the tool by a symlink, so the package root must survive resolve()."""
    link = tmp_path / "claude-reel"
    link.symlink_to(LAUNCHER)
    proc = subprocess.run([str(link), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "usage: claude-reel" in proc.stdout
