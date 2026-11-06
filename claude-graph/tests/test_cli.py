"""CLI dispatch + windowing: a seed follows edges past the window; scan stays bounded."""

import io
import time
from pathlib import Path

from claude_graph import cli
from claude_graph.graph import SessionRecord, build_session_graph
from claude_graph.render import render_seed
from rich.console import Console


def _rec(id, **kw):
    return SessionRecord(id=id, **kw)


def test_cutoff_seed_unbounded_by_default():
    assert cli._cutoff_ts(None, None, None, None) is None  # seed: whole store


def test_cutoff_scan_default_and_explicit():
    now = time.time()
    assert cli._cutoff_ts(None, None, None, 7) < now  # scan default window
    assert abs(cli._cutoff_ts(None, 3, None, None) - (now - 3 * 86400)) < 5  # --days
    assert abs(cli._cutoff_ts("5d", None, None, 7) - (now - 5 * 86400)) < 5  # window token


def test_seed_dispatch_scans_whole_store(monkeypatch):
    captured = {}

    def fake_load(cutoff, project, limit, **kw):
        captured.update(cutoff=cutoff, limit=limit)
        return [], 0

    monkeypatch.setattr(cli, "_load_window", fake_load)
    monkeypatch.setattr(cli, "_resolve_seed", lambda idsub: Path("/tmp/deadbeef.jsonl"))
    monkeypatch.setattr(cli, "load_session_record", lambda p: None)
    cli.main(["deadbeef", "--no-color"])
    assert captured["cutoff"] is None  # follows edges past any window
    assert captured["limit"] is None  # no load cap; -n caps display only


def test_default_dispatch_is_timeline_windowed_and_capped(monkeypatch, capsys):
    captured = {}

    def fake_load(cutoff, project, limit, **kw):
        captured.update(cutoff=cutoff, limit=limit)
        return [], 0

    monkeypatch.setattr(cli, "_load_window", fake_load)
    cli.main(["7d", "-n", "5", "--no-color"])
    assert captured["cutoff"] is not None  # windowed
    assert captured["limit"] == 5  # load-capped by -n
    assert "no sessions in this window" in capsys.readouterr().out  # timeline, not scan


def test_scan_mode_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: ([], 0))
    cli.main(["scan", "7d", "--no-color"])
    assert "no linked sessions" in capsys.readouterr().out


def test_json_mode_emits_document(monkeypatch, capsys):
    import json

    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: ([], 0))
    cli.main(["json", "--no-color"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == 1 and doc["sessions"] == []


def _file_fixture():
    return [
        _rec(
            "aaaa1111",
            files=["/x/repo/tools/soundcheck.py"],
            prs=["o/r#1"],
            repos=["/x/repo"],
            start="2026-07-01T09:00:00",
            first_user="build the audio testbed",
        ),
        _rec("bbbb2222", prs=["o/r#1"], repos=["/x/repo"], start="2026-07-02T09:00:00"),
    ]


def test_file_mode_requires_pattern():
    import pytest

    with pytest.raises(SystemExit):
        cli.main(["file", "--no-color"])


def test_file_mode_finds_single_editor_with_cluster_links(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: (_file_fixture(), 2))
    assert cli.main(["file", "soundcheck", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "/x/repo/tools/soundcheck.py" in out  # full canonical path, not basename
    assert "aaaa1111" in out and "bbbb2222" not in out.split("also")[0]
    assert "also PR o/r#1" in out  # cluster link rides along
    assert '"build the audio testbed"' in out


def test_file_mode_miss_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: (_file_fixture(), 2))
    assert cli.main(["file", "no-such", "--no-color"]) == 1
    assert "no session edited" in capsys.readouterr().out


def test_file_mode_hides_temp_paths_unless_asked(monkeypatch, capsys):
    recs = [_rec("aaaa1111", files=["/private/tmp/s/scratchpad/draft.lg"], repos=["/x/repo"])]
    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: (recs, 1))
    assert cli.main(["file", "draft", "--no-color"]) == 1
    assert "scratch/temp matches hidden" in capsys.readouterr().out
    assert cli.main(["file", "scratchpad/draft", "--no-color"]) == 0
    assert "/private/tmp/s/scratchpad/draft.lg" in capsys.readouterr().out


def test_file_mode_json_scopes_to_matches(monkeypatch, capsys):
    import json

    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: (_file_fixture(), 2))
    assert cli.main(["file", "soundcheck", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["query"] == "soundcheck"
    # scope = the editor plus its cluster co-members (shared PR)
    assert {s["id"] for s in doc["sessions"]} == {"aaaa1111", "bbbb2222"}


def test_stale_rewrite_does_not_eat_cap_slot(tmp_path, monkeypatch):
    """Review finding: a session whose file was rewritten recently (mtime fresh)
    but whose activity is outside the window must be skipped BEFORE it counts
    toward -n, or it starves genuinely recent sessions out of a capped load."""
    import os
    import time as _time

    from claude_graph.graph import SessionRecord

    now = _time.time()
    files = []
    # mtime order puts the stale rewrite first — the old code let it fill the cap.
    for name, age in [("stale", 10), ("fresh1", 20), ("fresh2", 30)]:
        f = tmp_path / f"{name}.jsonl"
        f.write_text("")
        os.utime(f, (now - age, now - age))
        files.append(f)
    monkeypatch.setattr(cli.sessions, "iter_session_files", lambda: iter(files))

    def fake_load(path):
        end = "2026-01-01T00:00:00Z" if path.stem == "stale" else "2126-01-01T00:00:00Z"
        return SessionRecord(id=path.stem, start=end, end=end)

    monkeypatch.setattr(cli, "load_session_record", fake_load)
    cutoff = now - 7 * 86400
    recs, total = cli._load_window(cutoff, None, 2, activity_cutoff=cutoff)
    assert [r.id for r in recs] == ["fresh1", "fresh2"]  # stale didn't take a slot
    assert total == 2  # and doesn't inflate the truncation math


def test_file_mode_json_miss_exits_nonzero(monkeypatch, capsys):
    """Review finding: --json must keep the same miss semantics as the rendered
    view — an empty scoped document still exits 1."""
    import json

    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: (_file_fixture(), 2))
    assert cli.main(["file", "no-such", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["sessions"] == []


def test_bare_id_still_seeds(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_window", lambda c, p, n, **kw: ([], 0))
    monkeypatch.setattr(cli, "_resolve_seed", lambda idsub: None)
    assert cli.main(["deadbeef", "--no-color"]) == 1
    assert "no session matching" in capsys.readouterr().out


def test_seed_render_caps_members():
    recs = [_rec("seedaaaa", prs=["o/r#1"], start="2026-06-28T09:00:00")]
    recs += [_rec(f"mate{i:04d}", prs=["o/r#1"], start=f"2026-06-28T1{i}:00:00") for i in range(5)]
    g = build_session_graph(recs)
    buf = io.StringIO()
    render_seed(g, "seedaaaa", Console(file=buf, no_color=True, width=200), limit=2)
    assert "+3 more" in buf.getvalue()  # 5 co-members, 2 shown
