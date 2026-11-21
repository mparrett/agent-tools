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
import time
from datetime import datetime, timedelta, timezone
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
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(cd, "PROJECTS_DIR", projects)

    class Fake:
        snapshots = tmp_path / "snap"

        def set_sessions(self, rows):
            agents.write_text(json.dumps(rows))

        def transcript(self, short, *entries, project="-work-repo"):
            d = projects / project
            d.mkdir(exist_ok=True)
            path = d / f"{session(short)['sessionId']}.jsonl"
            path.write_text("".join(json.dumps(e) + "\n" for e in entries))
            return path

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


def stamp(minutes_ago):
    then = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return then.isoformat().replace("+00:00", "Z")


def test_last_activity_is_the_last_timestamped_entry(fake):
    fake.transcript(
        "aaaa",
        {"type": "user", "timestamp": stamp(90)},
        {"type": "system", "timestamp": stamp(30)},
        {"type": "summary"},  # no timestamp: skipped, not mistaken for the end
    )
    got = cd.last_activity(session("aaaa")["sessionId"])
    assert cd.ago(got, datetime.now(timezone.utc)) == "30m"


def test_last_activity_skips_the_fragment_a_tail_read_starts_in(fake, monkeypatch):
    path = fake.transcript(
        "aaaa",
        {"type": "user", "timestamp": stamp(10), "pad": "x" * 200},
        {"type": "user", "timestamp": stamp(5)},
    )
    monkeypatch.setattr(cd, "TAIL_BYTES", path.stat().st_size - 20)
    got = cd.last_activity(session("aaaa")["sessionId"])
    assert cd.ago(got, datetime.now(timezone.utc)) == "5m"


def test_last_activity_survives_a_tail_read_that_starts_mid_character(fake, monkeypatch):
    path = fake.transcript(
        "aaaa",
        {"type": "user", "timestamp": stamp(10)},
        {"type": "tool_result", "content": "\u00e9" * 100},
    )
    path.write_text(path.read_text().replace("\\u00e9", "\u00e9"), encoding="utf-8")
    # Land on the second byte of a two-byte character, with no complete line after.
    size = path.stat().st_size
    monkeypatch.setattr(cd, "TAIL_BYTES", size - path.read_bytes().index(b"\xc3\xa9") - 1)
    got = cd.last_activity(session("aaaa")["sessionId"])
    assert got is not None  # fell back to the mtime instead of raising


@pytest.mark.parametrize(
    "line, expected",
    [
        (b'{"timestamp": 1732000000}', None),
        (b'{"timestamp": "not a date"}', None),
        (b"[1, 2]", None),
        (b'\xa9"}', None),
        (b'{"timestamp": "2026-11-20T10:00:00"}', datetime(2026, 11, 20, 10, tzinfo=timezone.utc)),
        (b'{"timestamp": "2026-11-20T10:00:00Z"}', datetime(2026, 11, 20, 10, tzinfo=timezone.utc)),
    ],
)
def test_entry_time_skips_what_is_not_a_usable_timestamp(line, expected):
    assert cd.entry_time(line) == expected


def test_last_activity_falls_back_to_mtime(fake):
    path = fake.transcript("aaaa", {"type": "summary"})
    old = time.time() - 3 * 3600
    os.utime(path, (old, old))
    got = cd.last_activity(session("aaaa")["sessionId"])
    assert cd.ago(got, datetime.now(timezone.utc)) == "3h"


def test_last_activity_takes_the_newest_of_several_transcripts(fake):
    fake.transcript("aaaa", {"timestamp": stamp(300)}, project="-started-here")
    fake.transcript("aaaa", {"timestamp": stamp(15)}, project="-moved-here")
    got = cd.last_activity(session("aaaa")["sessionId"])
    assert cd.ago(got, datetime.now(timezone.utc)) == "15m"


def test_last_activity_without_a_transcript_is_unknown(fake):
    assert cd.last_activity(session("none")["sessionId"]) is None
    assert cd.ago(None, datetime.now(timezone.utc)) == "?"


@pytest.mark.parametrize(
    "minutes, shown",
    [(0, "<1m"), (59, "59m"), (119, "119m"), (120, "2h"), (47 * 60, "47h"), (72 * 60, "3d")],
)
def test_ago(minutes, shown):
    now = datetime.now(timezone.utc)
    assert cd.ago(now - timedelta(minutes=minutes, seconds=1), now) == shown


def test_plan_shows_quiet_time_longest_first(fake, capsys):
    fake.set_sessions([session("recent"), session("stale"), session("nolog")])
    fake.transcript("recent", {"timestamp": stamp(5)})
    fake.transcript("stale", {"timestamp": stamp(600)})
    fake.run()
    lines = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ") and "idle" in ln
    ]
    assert [ln.split()[0] for ln in lines] == ["stale", "recent", "nolog"]
    assert lines[0].split()[2] == "10h"
    assert lines[2].split()[2] == "?"


def test_snapshot_records_last_activity(fake):
    fake.set_sessions([session("aaaa"), session("bbbb")])
    fake.transcript("aaaa", {"timestamp": stamp(5)})
    fake.run("--go", "--keep-daemon")
    [js] = fake.snapshots.glob("*.json")
    by_id = {r["id"]: r for r in json.loads(js.read_text())}
    assert by_id["aaaa"]["lastActivity"].endswith("+00:00")
    assert "lastActivity" not in by_id["bbbb"]
    assert "_last" not in by_id["aaaa"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
