#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Tests for claude-closed. Run via `uv run claude-closed/test_claude_closed.py` (or `just test`).

A fake ~/.claude (daemon log plus transcripts) and a fake `claude` on PATH stand
in for the real ones.
"""

import importlib.util
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "claude_closed", Path(__file__).parent / "claude-closed.py"
)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

FAKE_CLAUDE = """#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_FAIL") or ("--all" in sys.argv and os.environ.get("FAKE_FAIL_ALL")):
    sys.exit(1)
which = "FAKE_ALL" if "--all" in sys.argv else "FAKE_LIVE"
print(open(os.environ[which]).read())
"""


def sid(short):
    return f"{short}-0000-0000-0000-000000000000"


def at(minutes_ago):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def log_line(minutes_ago, text):
    stamp = at(minutes_ago).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"[{stamp}] [bg] bg {text}"


def bg(short, name=None, live=True):
    row = {
        "id": short,
        "sessionId": sid(short),
        "kind": "background",
        "name": name or f"session {short}",
        "cwd": "/work/repo",
        "state": "done",
    }
    if live:
        row["pid"] = 1
        row["status"] = "idle"
    return row


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects").mkdir(parents=True)
    live = tmp_path / "live.json"
    listed = tmp_path / "all.json"
    live.write_text("[]")
    listed.write_text("[]")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LIVE", str(live))
    monkeypatch.setenv("FAKE_ALL", str(listed))
    monkeypatch.setattr(cc, "CLAUDE_DIR", claude_dir)

    class Fake:
        def log(self, *lines, name="daemon.log"):
            (claude_dir / name).write_text("\n".join(lines) + "\n")

        def sessions(self, live_rows=(), closed_rows=()):
            live.write_text(json.dumps(list(live_rows)))
            listed.write_text(json.dumps([*live_rows, *closed_rows]))

        def transcript(self, short, *entries):
            d = claude_dir / "projects" / "-work-repo"
            d.mkdir(exist_ok=True)
            (d / f"{sid(short)}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))

        def rows(self, *argv):
            return json.loads(self.out(*argv, "--json"))

        def out(self, *argv):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                assert cc.main(list(argv)) == 0
            return buf.getvalue()

    return Fake()


def test_stopped_sessions_newest_first_with_resume_ids(fake):
    fake.log(
        log_line(50, "claimed-spare aaaa0000 (fleet)"),
        log_line(40, "settled aaaa0000 (killed)"),
        log_line(30, "claimed-spare bbbb0000 (shell)"),
        log_line(5, "settled bbbb0000 (killed)"),
    )
    fake.sessions(closed_rows=[bg("aaaa0000", live=False), bg("bbbb0000", live=False)])
    rows = fake.rows()
    assert [(r["id"], r["how"], r["removed"]) for r in rows] == [
        ("bbbb0000", "stopped", False),
        ("aaaa0000", "stopped", False),
    ]
    assert "claude --bg --resume bbbb0000-0000-0000-0000-000000000000" in fake.out()


def test_removed_session_is_described_from_its_transcript(fake):
    fake.log(log_line(5, "settled cccc0000 (killed)"))
    fake.transcript(
        "cccc0000",
        {"type": "user", "cwd": "/work/first"},
        {"type": "custom-title", "customTitle": "old title"},
        {"type": "agent-name", "agentName": "generated name"},
        {"type": "assistant", "cwd": "/work/later"},
        {"type": "custom-title", "customTitle": "renamed"},
    )
    [row] = fake.rows()
    assert row["removed"] is True
    assert row["sessionId"] == sid("cccc0000")
    assert row["name"] == "renamed"
    assert row["cwd"] == "/work/later"
    assert "removed from list" in fake.out()


def test_agent_name_stands_in_when_never_titled(fake):
    fake.log(log_line(5, "settled cccc0000 (killed)"))
    fake.transcript("cccc0000", {"type": "agent-name", "agentName": "generated name"})
    assert fake.rows()[0]["name"] == "generated name"


def test_retire_then_settle_reads_as_retired_with_idle_time(fake):
    fake.log(
        log_line(10, "retire dddd0000: idle-prompt, idle 61m"),
        log_line(10, "settled dddd0000 (done)"),
        log_line(8, "settled eeee0000 (done)"),
        log_line(6, "settled ffff0000 (crashed)"),
    )
    fake.sessions(closed_rows=[bg(s, live=False) for s in ("dddd0000", "eeee0000", "ffff0000")])
    hows = {r["id"]: (r["how"], r["idle"]) for r in fake.rows()}
    assert hows == {
        "dddd0000": ("retired", "idle 61m"),
        "eeee0000": ("exited", ""),
        "ffff0000": ("crashed", ""),
    }
    assert "retired (idle 61m)" in fake.out()


def test_a_retire_long_before_the_settle_is_not_its_cause(fake):
    fake.log(
        log_line(120, "retire dddd0000: idle-prompt, idle 60m"),
        log_line(5, "settled dddd0000 (killed)"),
    )
    fake.sessions(closed_rows=[bg("dddd0000", live=False)])
    assert fake.rows()[0]["how"] == "stopped"


def test_reopened_session_counts_only_its_latest_close(fake):
    fake.log(
        log_line(60, "settled aaaa0000 (killed)"),
        log_line(30, "claimed-spare aaaa0000 (fleet)"),
        log_line(10, "settled aaaa0000 (done)"),
    )
    fake.sessions(closed_rows=[bg("aaaa0000", live=False)])
    [row] = fake.rows()
    assert row["how"] == "exited"
    closed = datetime.fromisoformat(row["closedAt"])
    assert cc.ago(closed, datetime.now(timezone.utc)) == "10m"


def test_session_running_again_is_not_listed(fake):
    fake.log(
        log_line(10, "settled aaaa0000 (killed)"), log_line(2, "claimed-spare aaaa0000 (fleet)")
    )
    fake.sessions(live_rows=[bg("aaaa0000")])
    assert fake.rows() == []


def test_live_session_with_a_stale_settle_is_not_listed(fake):
    """Reopened without a claim line (e.g. the log rotated between them)."""
    fake.log(log_line(10, "settled aaaa0000 (killed)"))
    fake.sessions(live_rows=[bg("aaaa0000")])
    assert fake.rows() == []


def test_spares_are_not_sessions(fake):
    fake.log(
        log_line(10, "retire 99990000: stale-spare, idle 60m"),
        log_line(10, "settled 99990000 (done)"),
        log_line(5, "settled 88880000 (done)"),  # no listing, no transcript
    )
    assert fake.rows() == []


def test_since_window(fake):
    fake.log(
        log_line(3 * 60, "settled aaaa0000 (killed)"), log_line(30, "settled bbbb0000 (killed)")
    )
    fake.sessions(closed_rows=[bg("aaaa0000", live=False), bg("bbbb0000", live=False)])
    assert [r["id"] for r in fake.rows()] == ["bbbb0000", "aaaa0000"]
    assert [r["id"] for r in fake.rows("--since", "1h")] == ["bbbb0000"]


def test_stopped_filter_and_limit(fake):
    fake.log(
        log_line(30, "settled aaaa0000 (killed)"),
        log_line(20, "settled bbbb0000 (done)"),
        log_line(10, "settled cccc0000 (killed)"),
    )
    fake.sessions(closed_rows=[bg(s, live=False) for s in ("aaaa0000", "bbbb0000", "cccc0000")])
    assert [r["id"] for r in fake.rows("--stopped")] == ["cccc0000", "aaaa0000"]
    assert [r["id"] for r in fake.rows("-n", "1")] == ["cccc0000"]


def test_rotated_logs_are_read_too(fake):
    fake.log(log_line(30, "settled aaaa0000 (killed)"), name="daemon.log.1")
    fake.log(log_line(10, "settled bbbb0000 (killed)"))
    fake.sessions(closed_rows=[bg("aaaa0000", live=False), bg("bbbb0000", live=False)])
    assert {r["id"] for r in fake.rows()} == {"aaaa0000", "bbbb0000"}


def test_failed_listings_warn_but_still_list(fake, monkeypatch, capsys):
    fake.log(log_line(5, "settled cccc0000 (killed)"))
    fake.transcript("cccc0000", {"type": "custom-title", "customTitle": "t"})
    monkeypatch.setenv("FAKE_FAIL", "1")
    out = fake.out()
    assert "claude --bg --resume" in out
    err = capsys.readouterr().err
    assert "may be listed too" in err
    assert "removed from the list is unknown" in err


def test_failed_full_listing_leaves_removed_unknown(fake, monkeypatch, capsys):
    """Not every session gets marked removed just because --all couldn't be read."""
    fake.log(log_line(5, "settled cccc0000 (killed)"))
    fake.transcript("cccc0000", {"type": "custom-title", "customTitle": "t"})
    fake.sessions(closed_rows=[bg("cccc0000", live=False)])
    monkeypatch.setenv("FAKE_FAIL_ALL", "1")
    [row] = fake.rows()
    assert row["removed"] is None
    assert "removed from list" not in fake.out()


def test_json_output_still_warns_on_stderr(fake, monkeypatch, capsys):
    fake.log(log_line(5, "settled cccc0000 (killed)"))
    fake.transcript("cccc0000", {"type": "custom-title", "customTitle": "t"})
    monkeypatch.setenv("FAKE_FAIL", "1")
    rows = fake.rows()  # stdout must still parse as JSON
    assert rows[0]["id"] == "cccc0000"
    assert "may be listed too" in capsys.readouterr().err


def test_transcript_removed_mid_run_is_skipped(fake, monkeypatch):
    fake.transcript("cccc0000", {"type": "custom-title", "customTitle": "t"})
    real_glob = Path.glob

    def glob_then_vanish(self, pattern):
        hits = list(real_glob(self, pattern))
        for h in hits:
            h.unlink()
        return iter(hits)

    monkeypatch.setattr(Path, "glob", glob_then_vanish)
    assert cc.transcript_meta("cccc0000") is None


@pytest.mark.parametrize("bad", ["0", "-1", "x"])
def test_limit_must_be_positive(bad):
    with pytest.raises(SystemExit):
        cc.parse_args(["-n", bad])


def test_nothing_closed(fake):
    fake.log("unrelated line", log_line(5, "spare spawned host pid=1"))
    assert "no background sessions closed" in fake.out()


def test_missing_log_is_not_fatal(fake):
    assert fake.rows() == []


@pytest.mark.parametrize(
    "text, expected",
    [("30m", timedelta(minutes=30)), ("6h", timedelta(hours=6)), ("3d", timedelta(days=3))],
)
def test_duration(text, expected):
    assert cc.duration(text) == expected


@pytest.mark.parametrize("bad", ["6", "h", "1w", "-1h"])
def test_duration_rejects(bad):
    with pytest.raises(Exception):
        cc.duration(bad)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
