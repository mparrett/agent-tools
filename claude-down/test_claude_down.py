#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Tests for claude-down. Run via `uv run claude-down/test_claude_down.py` (or `just test`).

Fake `claude` and `claude-ps` executables on PATH stand in for the real ones:
they serve fixture JSON and log every invocation, so the tests check what would
be stopped, and in what order, without touching a live session.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "claude_down", Path(__file__).parent / "claude-down.py"
)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["agents", "--json"]:
    print(open(os.environ["FAKE_AGENTS"]).read())
elif sys.argv[1] == "stop" and sys.argv[2] in os.environ.get("FAKE_STOP_FAIL", "").split(","):
    print("No session matching", file=sys.stderr)
    sys.exit(1)
"""

FAKE_CLAUDE_PS = """#!/usr/bin/env python3
import os
print(open(os.environ["FAKE_SHELLS"]).read())
"""


def session(short, status="idle", kind="background", name=None):
    return {
        "id": short,
        "sessionId": f"{short}-0000-0000-0000-000000000000",
        "kind": kind,
        "status": status,
        "name": name or f"session {short}",
        "cwd": "/work/repo",
    }


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("claude", FAKE_CLAUDE), ("claude-ps", FAKE_CLAUDE_PS)):
        exe = bin_dir / name
        exe.write_text(body)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.touch()
    agents = tmp_path / "agents.json"
    shells = tmp_path / "shells.json"
    shells.write_text("[]")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_AGENTS", str(agents))
    monkeypatch.setenv("FAKE_SHELLS", str(shells))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(cd, "POLL_SECONDS", 0.01)

    class Fake:
        snapshots = tmp_path / "snap"

        def set_sessions(self, rows):
            agents.write_text(json.dumps(rows))

        def set_shells(self, rows):
            shells.write_text(json.dumps(rows))

        def calls(self):
            return [c for c in log.read_text().splitlines() if c != "agents --json"]

        def run(self, *argv):
            return cd.main([*argv, "--snapshot-dir", str(self.snapshots)])

    return Fake()


def test_plan_only_changes_nothing(fake, capsys):
    fake.set_sessions([session("aaaa"), session("bbbb", "busy")])
    assert fake.run() == 0
    out = capsys.readouterr().out
    assert "2 background session(s) to stop: 1 idle, 1 busy" in out
    assert "plan only" in out
    assert fake.calls() == []
    assert not fake.snapshots.exists()


def test_go_stops_idle_then_busy_then_daemon(fake):
    fake.set_sessions([session("busy1", "busy"), session("idle1"), session("idle2")])
    assert fake.run("--go", "--grace", "0") == 0
    assert fake.calls() == [
        "stop idle1",
        "stop idle2",
        "stop busy1",
        "daemon stop --any",
    ]


def test_interactive_sessions_are_left_alone(fake):
    fake.set_sessions([session("bg01"), session("tty1", kind="interactive")])
    fake.run("--go", "--grace", "0")
    assert "stop tty1" not in fake.calls()


def test_own_session_is_skipped_and_daemon_kept(fake, monkeypatch, capsys):
    fake.set_sessions([session("self"), session("othr")])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session("self")["sessionId"])
    fake.run("--go", "--grace", "0")
    assert fake.calls() == ["stop othr"]
    assert "skipping this session (self)" in capsys.readouterr().out


def test_include_self_hands_its_own_stop_and_the_daemon_to_a_detached_shell(fake, monkeypatch):
    fake.set_sessions([session("self"), session("othr")])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session("self")["sessionId"])
    handed = []
    monkeypatch.setattr(cd, "hand_off", handed.append)
    fake.run("--go", "--grace", "0", "--include-self")
    # Stopping this session or the daemon would kill the run mid-call, so
    # neither happens in-process.
    assert fake.calls() == ["stop othr"]
    assert handed == [["claude stop self", "claude daemon stop --any"]]


def test_session_that_turned_busy_after_the_plan_gets_the_grace(fake, monkeypatch):
    fake.set_sessions([session("aaaa")])
    listings = iter([[session("aaaa")], [session("aaaa", "busy")], [session("aaaa")]])
    monkeypatch.setattr(cd, "fetch_background", lambda: (next(listings), None))
    waited = []
    real_wait = cd.wait_for_idle
    monkeypatch.setattr(cd, "wait_for_idle", lambda ids, g: waited.append(ids) or real_wait(ids, g))
    fake.run("--go", "--keep-daemon")
    assert waited == [["aaaa"]]
    assert fake.calls() == ["stop aaaa"]


def test_sessions_that_appeared_after_the_plan_are_stopped(fake, monkeypatch):
    listings = iter([[session("aaaa")], [session("aaaa"), session("newb")]])
    monkeypatch.setattr(cd, "fetch_background", lambda: (next(listings), None))
    fake.run("--go", "--keep-daemon")
    assert fake.calls() == ["stop aaaa", "stop newb"]


def test_failed_poll_during_grace_still_stops_the_rest(fake, monkeypatch):
    listings = iter(
        [
            ([session("idle1"), session("busy1", "busy")], None),
            ([session("idle1"), session("busy1", "busy")], None),
            (None, "claude agents --json failed: timed out"),
        ]
    )
    monkeypatch.setattr(cd, "fetch_background", lambda: next(listings))
    assert fake.run("--go", "--grace", "30") == 0
    assert fake.calls() == ["stop idle1", "stop busy1", "daemon stop --any"]


def test_only_stops_the_named_session_and_keeps_daemon(fake):
    fake.set_sessions([session("aaaa"), session("bbbb")])
    fake.run("--go", "--only", "bbbb")
    assert fake.calls() == ["stop bbbb"]


def test_only_with_unknown_id_exits(fake):
    fake.set_sessions([session("aaaa")])
    with pytest.raises(SystemExit, match="no background session zzzz"):
        fake.run("--go", "--only", "zzzz")


def test_keep_daemon(fake):
    fake.set_sessions([session("aaaa")])
    fake.run("--go", "--keep-daemon")
    assert fake.calls() == ["stop aaaa"]


def test_failed_stop_is_reported_in_exit_status(fake, monkeypatch):
    fake.set_sessions([session("aaaa"), session("bbbb")])
    monkeypatch.setenv("FAKE_STOP_FAIL", "aaaa")
    assert fake.run("--go", "--keep-daemon") == 1
    # One failure doesn't stop the rest.
    assert fake.calls() == ["stop aaaa", "stop bbbb"]


def test_snapshot_has_attach_lines(fake):
    fake.set_sessions([session("aaaa", name="my task")])
    fake.run("--go", "--keep-daemon")
    latest = (fake.snapshots / "latest.md").read_text()
    assert "**my task**" in latest
    assert "`claude attach aaaa`" in latest
    [js] = fake.snapshots.glob("*.json")
    assert json.loads(js.read_text())[0]["id"] == "aaaa"


def test_plan_shows_what_holds_a_busy_session(fake, capsys):
    s = session("busy1", "busy")
    fake.set_sessions([s])
    fake.set_shells(
        [
            {
                "session_id": s["sessionId"],
                "kind": "background",
                "command": "python -m http.server 8000\nmore",
            },
            {"session_id": s["sessionId"], "kind": "foreground", "command": "ls"},
        ]
    )
    fake.run()
    out = capsys.readouterr().out
    assert "holds: python -m http.server 8000" in out
    assert "holds: ls" not in out


def test_missing_claude_ps_is_not_fatal(fake, monkeypatch, capsys):
    fake.set_sessions([session("aaaa")])
    monkeypatch.setattr(cd, "holders_by_session", lambda: {})
    assert fake.run() == 0


def test_wait_for_idle_returns_once_sessions_settle(fake):
    fake.set_sessions([session("busy1")])  # already idle by the first poll
    assert cd.wait_for_idle(["busy1"], grace=5) == set()


def test_wait_for_idle_gives_up_after_grace(fake):
    fake.set_sessions([session("busy1", "busy")])
    assert cd.wait_for_idle(["busy1"], grace=0.05) == {"busy1"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
