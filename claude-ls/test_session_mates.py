#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Tests for the SessionStart hook. Run via `uv run claude-ls/test_session_mates.py` (or `just test`).

The hook lists a session's live repo-mates from `claude-ls who`, and prints nothing
when there are none.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "session_mates.py"

_spec = importlib.util.spec_from_file_location("session_mates", HOOK)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def row(sid, pane, **over):
    r = {
        "session_id": sid,
        "pane": pane,
        "status": "idle",
        "status_age_min": 95,
        "worktree": "main",
        "branch": "main",
        "model": "claude-opus-5",
        "topic": "survey the architecture",
        "last_prompt": "ok so what happened here?",
        "touched": ["/w/repo/src/x.cs", "/elsewhere/notes.md"],
        "toplevel": "/w/repo",
        "waiting": False,
    }
    r.update(over)
    return r


def run(stdin: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", **env},
    )


def test_render_lists_the_others_and_not_me():
    rows = [row("me-1", "mine"), row("other-1", "web-app-82f7", waiting=True)]
    out = sm.render(rows, "me-1")
    assert "mine" not in out
    assert (
        "- web-app-82f7 (other-1): idle 1h, last message was a question for the human, "
        "main @ main, opus-5"
    ) in out
    assert "    topic: survey the architecture" in out
    # relative inside the checkout, absolute outside it
    assert "    touching: src/x.cs, /elsewhere/notes.md" in out
    assert out.endswith("leave it there.\n")


def test_render_stamps_the_block_as_a_snapshot():
    # naive, so astimezone() reads it as local time instead of shifting the date
    out = sm.render([row("o-1", "pane")], "me-1", now=datetime(2026, 10, 29, 9, 5))
    head = out.splitlines()[0]
    assert "as of 2026-10-29 09:05" in head
    assert "snapshot from session start and goes stale" in head


def test_render_is_empty_alone_and_caps_a_crowd():
    assert sm.render([row("me-1", "mine")], "me-1") == ""
    assert sm.render([], "me-1") == ""
    crowd = [row(f"s{i}", f"pane-{i}") for i in range(sm.MAX_MATES + 3)]
    out = sm.render(crowd, "none")
    bullets = [ln for ln in out.splitlines() if ln.startswith("- ") and not ln.startswith("- …")]
    assert len(bullets) == sm.MAX_MATES
    assert "- … and 3 more" in out


def test_hook_prints_the_block_from_the_fixture_and_nothing_when_alone(tmp_path):
    fixture = tmp_path / "who.json"
    fixture.write_text(json.dumps([row("me-1", "mine"), row("o-1", "api-server-3828")]))
    r = run(
        json.dumps({"source": "startup", "cwd": "/w/repo", "session_id": "me-1"}),
        {"SESSION_MATES_WHO_JSON": str(fixture)},
    )
    assert r.returncode == 0, r.stderr
    assert "api-server-3828" in r.stdout and "mine" not in r.stdout

    fixture.write_text(json.dumps([row("me-1", "mine")]))
    r = run(
        json.dumps({"source": "resume", "cwd": "/w/repo", "session_id": "me-1"}),
        {"SESSION_MATES_WHO_JSON": str(fixture)},
    )
    assert r.returncode == 0 and r.stdout == ""


def test_hook_never_fails_the_session(tmp_path):
    r = run("not json", {})
    assert r.returncode == 0 and r.stdout == ""
    # claude-ls unavailable: stderr gets the reason, the session gets nothing
    r = run(
        json.dumps({"source": "startup", "cwd": str(tmp_path), "session_id": "x"}),
        {"SESSION_MATES_CLAUDE_LS": str(tmp_path / "missing")},
    )
    assert r.returncode == 0 and r.stdout == ""
    assert "session_mates" in r.stderr


def test_hook_finds_claude_ls_beside_itself_when_path_is_empty(tmp_path):
    # no PATH entry for claude-ls, no override: the sibling claude-ls.py is used,
    # and against an empty registry it reports no mates
    uv_dir = str(Path(shutil.which("uv")).parent)  # claude-ls.py is a uv script
    r = run(
        json.dumps({"source": "startup", "cwd": str(tmp_path), "session_id": "x"}),
        {
            "CLAUDE_LS_SESSIONS_DIR": str(tmp_path / "no-registry"),
            "PATH": f"/usr/bin:/bin:{uv_dir}",
        },
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
