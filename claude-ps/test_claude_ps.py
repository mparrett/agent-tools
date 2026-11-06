#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "rich"]
# ///
"""Tests for claude-ps. Run via `uv run claude-ps/test_claude_ps.py` (or `just test`).

The module filename has a hyphen, so it can't be imported by name — load it from
its path with importlib.
"""

import importlib.util
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

_spec = importlib.util.spec_from_file_location("claude_ps", Path(__file__).parent / "claude-ps.py")
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _fake_run(stdout, returncode=0):
    return lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=returncode)


def _lsof_fixture():
    # lsof -F output: one tagged field per line. A real session holds its cwd and a
    # `txt` handle on its executable under the versions dir; the impostors below are
    # the shapes actually observed in the wild — a shell matched by `pgrep -f`, and
    # a binary named `claude` sitting in a scratchpad with no version handle.
    return "\n".join(
        [
            "p1058",
            "fcwd",
            "n/Users/x/projects/real",
            "ftxt",
            "n/Users/x/.local/share/claude/versions/2.1.220",
            "ftxt",
            "n/usr/lib/dyld",
            "p61442",  # /bin/zsh matched only because its args contain "claude"
            "fcwd",
            "n/Users/x/projects/real",
            "ftxt",
            "n/bin/zsh",
            "p35214",  # a fake `claude` in a scratchpad fakebin/
            "fcwd",
            "n/Users/x/projects/real",
            "ftxt",
            "n/private/tmp/scratchpad/fakebin/claude",
        ]
    )


def test_lsof_facts_extracts_cwd_and_version(monkeypatch):
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(_lsof_fixture()))
    facts = cp.get_lsof_facts(["1058", "61442", "35214"])
    assert facts["1058"] == {"cwd": "/Users/x/projects/real", "version": "2.1.220"}
    # Both impostors are located, and both are distinguishable: no version handle.
    assert facts["61442"]["version"] is None
    assert facts["35214"]["version"] is None


def test_lsof_facts_survives_paths_with_spaces(monkeypatch):
    # Why -F rather than the human table: the NAME column is whitespace-separated
    # from the fields before it, so a path with a space cannot be split back out.
    out = "\n".join(
        [
            "p99",
            "fcwd",
            "n/Users/x/my projects/a b",
            "ftxt",
            "n/Users/x/.local/share/claude/versions/2.1.220",
        ]
    )
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(out))
    assert cp.get_lsof_facts(["99"])["99"]["cwd"] == "/Users/x/my projects/a b"


def test_lsof_facts_degrades_when_lsof_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(cp.subprocess, "run", boom)
    # Empty means "no opinion" — main() falls back to the args match rather than
    # filtering every process away and reporting an empty fleet.
    assert cp.get_lsof_facts(["1"]) == {}


def test_lsof_facts_ignores_nonzero_exit(monkeypatch):
    # lsof exits non-zero when any requested pid has already gone away, which is
    # routine here; the output for the surviving pids is still good.
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(_lsof_fixture(), returncode=1))
    assert cp.get_lsof_facts(["1058"])["1058"]["version"] == "2.1.220"


def test_extract_claude_version():
    v = "/Users/x/.local/share/claude/versions/2.1.219"
    assert cp.extract_claude_version(v) == "2.1.219"
    assert cp.extract_claude_version(v + "/inner/file") == "2.1.219"
    assert cp.extract_claude_version("/private/tmp/scratchpad/fakebin/claude") is None
    assert cp.extract_claude_version("/bin/zsh") is None


def test_pids_union_recovers_what_pgrep_misses(monkeypatch):
    # The reproducible gap: pgrep omits the caller's own ancestor, so a session
    # running claude-ps cannot see itself. The ps scan supplies it.
    def fake_run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return SimpleNamespace(stdout="1058\n61442\n", returncode=0)
        return SimpleNamespace(stdout="1058 claude\n37833 claude\n900 /bin/zsh\n", returncode=0)

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    # 37833 only ps knows about, 61442 only pgrep does; both are candidates, and
    # precision is the version handle's job, not this function's.
    assert cp.get_claude_pids() == ["1058", "37833", "61442"]


def test_pids_survive_one_enumeration_failing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return SimpleNamespace(stdout="", returncode=1)  # pgrep finds nothing
        return SimpleNamespace(stdout="37833 claude\n", returncode=0)

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    assert cp.get_claude_pids() == ["37833"]


def test_waiting_keyed_by_cwd(monkeypatch):
    # pending --json carries each session's real cwd; the map keys on that path.
    rows = [
        {"cwd": "/Users/x/foo", "minutes_waiting": 5},
        {"cwd": "/Users/x/bar", "minutes_waiting": 12},
    ]
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(cp.json.dumps(rows)))
    assert cp.waiting_by_cwd(cp.fetch_pending_rows()) == {"/Users/x/foo": 5, "/Users/x/bar": 12}


def test_waiting_skips_rows_without_cwd(monkeypatch):
    # An older claude-ls (or a session missing the header) yields no cwd — skip it.
    rows = [{"minutes_waiting": 9}, {"cwd": "/Users/x/foo", "minutes_waiting": 3}]
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(cp.json.dumps(rows)))
    assert cp.waiting_by_cwd(cp.fetch_pending_rows()) == {"/Users/x/foo": 3}


def test_waiting_keeps_freshest_per_cwd(monkeypatch):
    # Two waiting sessions share a directory; the smaller staleness wins.
    rows = [
        {"cwd": "/Users/x/foo", "minutes_waiting": 30},
        {"cwd": "/Users/x/foo", "minutes_waiting": 4},
    ]
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(cp.json.dumps(rows)))
    assert cp.waiting_by_cwd(cp.fetch_pending_rows()) == {"/Users/x/foo": 4}


def test_waiting_degrades_when_claude_ls_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("claude-ls")

    monkeypatch.setattr(cp.subprocess, "run", boom)
    assert cp.fetch_pending_rows() is None


def test_waiting_degrades_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(cp.subprocess, "run", _fake_run("", returncode=1))
    assert cp.fetch_pending_rows() is None


def test_waiting_degrades_on_bad_json(monkeypatch):
    monkeypatch.setattr(cp.subprocess, "run", _fake_run("not json"))
    assert cp.fetch_pending_rows() is None


def test_waiting_degrades_on_non_list_json(monkeypatch):
    # Valid JSON that isn't the expected list (a bare object) must not crash the
    # loop on row.get() — it degrades to unknown like every other bad output.
    monkeypatch.setattr(cp.subprocess, "run", _fake_run('{"cwd": "/tmp"}'))
    assert cp.fetch_pending_rows() is None


def test_waiting_skips_non_dict_rows(monkeypatch):
    # A list with a stray non-object entry keeps the valid rows, skips the junk.
    rows = ["oops", {"cwd": "/Users/x/foo", "minutes_waiting": 3}, 42]
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(cp.json.dumps(rows)))
    assert cp.waiting_by_cwd(cp.fetch_pending_rows()) == {"/Users/x/foo": 3}


def test_waiting_degrades_on_timeout(monkeypatch):
    # A stuck `claude-ls pending` scan must not wedge the listing under `watch`.
    def slow(*a, **k):
        raise cp.subprocess.TimeoutExpired(cmd="claude-ls", timeout=cp.PENDING_TIMEOUT_SECONDS)

    monkeypatch.setattr(cp.subprocess, "run", slow)
    assert cp.fetch_pending_rows() is None


def test_empty_scan_is_not_unknown(monkeypatch):
    # The distinction the None contract exists for: claude-ls answered and nothing
    # was waiting. That is an empty map, never None — conflating the two is what
    # made a failed scan render as a confident fleet-wide "not waiting".
    monkeypatch.setattr(cp.subprocess, "run", _fake_run("[]"))
    result = cp.waiting_by_cwd(cp.fetch_pending_rows())
    assert result == {}
    assert result is not None


def test_waiting_by_session_does_not_collapse(monkeypatch):
    # The cwd map keeps one entry per directory; the session map keeps every
    # session, so a row reports its own wait rather than a directory minimum.
    rows = [
        {"cwd": "/Users/x/foo", "session_id": "aaa", "minutes_waiting": 36003},
        {"cwd": "/Users/x/foo", "session_id": "bbb", "minutes_waiting": 41078},
    ]
    monkeypatch.setattr(cp.subprocess, "run", _fake_run(cp.json.dumps(rows)))
    fetched = cp.fetch_pending_rows()
    assert cp.waiting_by_session(fetched) == {"aaa": 36003, "bbb": 41078}
    # ...while the fallback still collapses to the freshest, as it must.
    assert cp.waiting_by_cwd(fetched) == {"/Users/x/foo": 36003}


def _local_lstart(utc_iso, shift_s=0):
    """`ps -o lstart=` output for an instant the registry would record as UTC.

    The fixtures have to differ between the two clocks or they cannot catch the
    bug they exist for: a naive string or wall-clock compare passes whenever both
    sides are written in the same timezone, which is what every fixture here used
    to do.
    """
    inst = datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
    return (inst.astimezone() + timedelta(seconds=shift_s)).strftime(cp.LSTART_FMT)


def _local_started(utc_iso, shift_s=0):
    """The naive local datetime `get_proc_info` parses out of that same output."""
    return datetime.strptime(_local_lstart(utc_iso, shift_s), cp.LSTART_FMT)


def _registry_dir(tmp_path, *records):
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    for rec in records:
        (d / f"{rec['pid']}.json").write_text(cp.json.dumps(rec))
    return d


def test_registry_reads_claude_codes_session_records(monkeypatch, tmp_path):
    d = _registry_dir(
        tmp_path,
        {"pid": 500, "sessionId": "aaa", "procStart": "Mon Aug  3 09:00:00 2026"},
        {"pid": 501, "sessionId": "bbb", "procStart": "Mon Aug  3 09:30:00 2026"},
    )
    monkeypatch.setattr(cp, "SESSIONS_DIR", d)
    reg = cp.load_session_registry()
    assert reg["500"]["session_id"] == "aaa"
    assert reg["501"]["proc_start"] == "Mon Aug  3 09:30:00 2026"


def test_registry_skips_unusable_records(monkeypatch, tmp_path):
    # Another program's output directory: partial writes, records from a newer
    # schema, and files nothing here wrote are all normal. Skip them individually
    # rather than losing the whole join to one bad file.
    d = _registry_dir(tmp_path, {"pid": 500, "sessionId": "aaa"})
    (d / "junk.json").write_text("{not json")
    (d / "601.json").write_text(cp.json.dumps({"pid": "601", "sessionId": "str-pid"}))
    (d / "602.json").write_text(cp.json.dumps({"pid": 602}))  # no session id
    (d / "603.json").write_text(cp.json.dumps(["not", "a", "dict"]))
    monkeypatch.setattr(cp, "SESSIONS_DIR", d)
    assert sorted(cp.load_session_registry()) == ["500"]


def test_registry_missing_dir_is_empty(monkeypatch, tmp_path):
    # An older CLI writes no records at all; that is a fallback, not an error.
    monkeypatch.setattr(cp, "SESSIONS_DIR", tmp_path / "absent")
    assert cp.load_session_registry() == {}


@pytest.fixture
def pacific_tz():
    """Force a non-UTC local zone for the duration of a test.

    Without this the UTC/local case does not exist in CI: `_local_lstart` derives
    "local" from the runner's own clock, GitHub's ubuntu runner is UTC, and the
    two sides of the compare come out byte-identical — so a regression to a naive
    wall-clock compare passes CI and fails only for users in a real timezone.
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_registry_matches_a_utc_record_against_local_ps_output(pacific_tz):
    # The trap this join rests on. Claude Code writes `procStart` in UTC; `started`
    # is parsed from local `ps -o lstart=`. Comparing them as text — or as naive
    # wall clocks — rejects every live session anywhere but UTC, and the failure is
    # invisible: every row silently falls back to the cwd join, which is exactly
    # what the old un-installed hook looked like.
    recorded = "Wed Sep  2 21:16:00 2026"
    registry = {"500": {"session_id": "aaa", "proc_start": recorded}}
    proc = {"pid": "500", "started": _local_started("2026-09-02T21:16:00")}
    # Guard the guard: if these two spellings ever coincide the test is inert and
    # proves nothing, which is precisely how it read on a UTC runner.
    assert proc["started"].strftime(cp.LSTART_FMT) != recorded
    assert cp.registry_session_for(proc, registry) == "aaa"

    # ...and the guard still has teeth: an hour out is a different process.
    recycled = {"pid": "500", "started": _local_started("2026-09-02T21:16:00", 3600)}
    assert cp.registry_session_for(recycled, registry) is None


def test_registry_rejects_recycled_pid():
    # The guard that makes the pid join safe: same pid, different start time means
    # the OS handed the number to someone else, so the record must not be trusted.
    registry = {"500": {"session_id": "aaa", "proc_start": "Mon Aug  3 09:00:00 2026"}}
    proc = {"pid": "500", "started": _local_started("2026-08-06T11:00:00")}
    assert cp.registry_session_for(proc, registry) is None

    proc_same = {"pid": "500", "started": _local_started("2026-08-03T09:00:00")}
    assert cp.registry_session_for(proc_same, registry) == "aaa"


def test_registry_unconfirmable_record_is_not_a_join():
    # An unparseable procStart leaves the pid unconfirmed, and an unconfirmed pid
    # is the recycled-pid hazard again. Fall back rather than guess.
    registry = {"500": {"session_id": "aaa", "proc_start": "yesterday-ish"}}
    assert cp.registry_session_for({"pid": "500", "started": datetime.now()}, registry) is None
    # No opinion from either side: the record is trusted, as it was before.
    bare = {"500": {"session_id": "aaa", "proc_start": None}}
    assert cp.registry_session_for({"pid": "500", "started": datetime.now()}, bare) == "aaa"


def test_session_join_flags_only_the_waiting_process():
    # The case that exposed the shared-directory bug, inverted. Two registered sessions share a
    # directory and only one is waiting: exactly one row is flagged, and it carries
    # its own wait rather than the directory's freshest.
    started = _local_started("2026-08-06T11:00:00")
    proc_start = "Thu Aug  6 11:00:00 2026"
    procs = [
        {"pid": "100", "cwd": "~/foo", "started": started},
        {"pid": "200", "cwd": "~/foo", "started": started},
    ]
    registry = {
        "100": {"session_id": "waiting-one", "proc_start": proc_start},
        "200": {"session_id": "busy-one", "proc_start": proc_start},
    }
    cp.annotate_waiting(
        procs,
        by_cwd={"/Users/x/foo": 41078},
        by_session={"waiting-one": 41078},
        registry=registry,
    )
    assert procs[0]["waiting"] is True
    assert procs[0]["waiting_minutes"] == 41078
    assert procs[1]["waiting"] is False
    assert [p["waiting_source"] for p in procs] == ["session", "session"]


def test_exact_join_uses_the_id_shapes_both_sides_really_produce(monkeypatch, tmp_path):
    # Both sides through their real parsers, with the ids the two stores really hold:
    # the hook writes the whole uuid, and `pending --json` emits that same uuid. The
    # tests above pair a synthetic id with itself, which cannot catch a shape
    # mismatch — and a mismatch is invisible, because an id absent from the map is
    # indistinguishable from a session that simply is not waiting.
    uuid = "0b0b2922-78a8-453e-891b-77911740dfaf"
    started = _local_started("2026-08-06T11:00:00")
    d = _registry_dir(
        tmp_path, {"pid": 100, "sessionId": uuid, "procStart": "Thu Aug  6 11:00:00 2026"}
    )
    monkeypatch.setattr(cp, "SESSIONS_DIR", d)
    monkeypatch.setattr(
        cp.subprocess,
        "run",
        _fake_run(
            cp.json.dumps([{"cwd": "/Users/x/foo", "session_id": uuid, "minutes_waiting": 7}])
        ),
    )

    rows = cp.fetch_pending_rows()
    procs = [{"pid": "100", "cwd": "~/foo", "started": started}]
    cp.annotate_waiting(
        procs,
        by_cwd=cp.waiting_by_cwd(rows),
        by_session=cp.waiting_by_session(rows),
        registry=cp.load_session_registry(),
    )
    assert procs[0]["waiting"] is True
    assert procs[0]["waiting_minutes"] == 7
    assert procs[0]["waiting_source"] == "session"


def test_unregistered_process_falls_back_to_cwd(monkeypatch):
    # A session started outside tmux, or under a CLI too old to write a record, has
    # no exact join — so the two have to coexist per process, and the source has to
    # say which one ran.
    monkeypatch.setattr(cp.os.path, "expanduser", lambda p: p.replace("~", "/Users/x", 1))
    procs = [{"pid": "100", "cwd": "~/foo", "started": None}]
    cp.annotate_waiting(procs, by_cwd={"/Users/x/foo": 12}, by_session={}, registry={})
    assert procs[0]["waiting"] is True
    assert procs[0]["waiting_source"] == "cwd"


def test_annotate_waiting_propagates_unknown():
    # A failed scan must reach the renderer as None, not False. False claims the
    # session is not waiting; nothing established that.
    procs = [{"cwd": "~/foo"}, {"cwd": "~/bar"}]
    cp.annotate_waiting(procs, None)
    assert [p["waiting"] for p in procs] == [None, None]
    assert [p["waiting_minutes"] for p in procs] == [None, None]


def test_annotate_waiting_marks_by_cwd(monkeypatch):
    # Drive the real join: the process in the waiting directory is flagged with its
    # staleness; a sibling directory is not. get_lsof_facts() tilde-trims under $HOME,
    # so annotate_waiting expands ~ back to an absolute path before matching.
    monkeypatch.setattr(cp.os.path, "expanduser", lambda p: p.replace("~", "/Users/x", 1))
    procs = [{"cwd": "~/foo"}, {"cwd": "~/bar"}]
    cp.annotate_waiting(procs, {"/Users/x/foo": 7})
    assert procs[0]["waiting"] is True
    assert procs[0]["waiting_minutes"] == 7
    assert procs[1]["waiting"] is False
    assert procs[1]["waiting_minutes"] is None


def test_versions_sort_numerically():
    # The stale-build footer names versions[-1] as newest. Sorted as strings,
    # "2.1.100" < "2.1.99", so the footer would name the older build and count the
    # up-to-date sessions as the stale ones. Patch numbers are already three digits,
    # so this boundary is crossed in normal use.
    versions = sorted({"2.1.99", "2.1.100", "2.1.9"}, key=cp.version_key)
    assert versions == ["2.1.9", "2.1.99", "2.1.100"]
    assert versions[-1] == "2.1.100"


def test_version_key_tolerates_non_numeric_parts():
    # An unparseable version must not take the listing down; it sorts after the
    # numeric ones instead.
    assert sorted({"2.1.100", "2.1.99-rc1"}, key=cp.version_key) == ["2.1.100", "2.1.99-rc1"]


def test_confirmed_candidates_are_kept(monkeypatch):
    # The confirming pass drops the candidate with no version handle and keeps the one
    # that has it — the impostor filter this stack's earlier PR exists for.
    monkeypatch.setattr(cp, "get_proc_info", lambda pid: {"pid": pid})
    facts = {"1": {"cwd": "~/a", "version": "2.1.220"}, "2": {"cwd": "~/b", "version": None}}
    procs = cp.collect_procs(["1", "2"], facts, confirm=True)
    assert [p["pid"] for p in procs] == ["1"]
    assert procs[0]["version"] == "2.1.220"


def test_listing_survives_lsof_confirming_nothing(monkeypatch):
    # lsof answers, but no candidate holds a handle under the versions dir — the npm
    # install layout, where the txt handle is the node binary. Confirming would drop
    # every row and print "No Claude processes found." on a live fleet, so the
    # unconfirmed pass has to keep them.
    monkeypatch.setattr(cp, "get_proc_info", lambda pid: {"pid": pid})
    facts = {"1": {"cwd": "~/a", "version": None}, "2": {"cwd": "~/b", "version": None}}
    assert cp.collect_procs(["1", "2"], facts, confirm=True) == []
    fallback = cp.collect_procs(["1", "2"], facts, confirm=False)
    assert [p["pid"] for p in fallback] == ["1", "2"]
    assert [p["cwd"] for p in fallback] == ["~/a", "~/b"]


# ── live store ───────────────────────────────────────────────────────────────
#
# Everything above is mocked, so nothing there proves the real records parse or
# that the UTC/local compare holds against real processes. The registry is another
# program's output and its format can change under us; these two catch that on a
# real machine and skip in CI, which has no sessions.

needs_live_store = pytest.mark.skipif(
    not cp.SESSIONS_DIR.is_dir() or not any(cp.SESSIONS_DIR.glob("*.json")),
    reason="no live Claude Code session records on this machine",
)


@needs_live_store
def test_live_records_parse_into_joins():
    registry = cp.load_session_registry()
    on_disk = len(list(cp.SESSIONS_DIR.glob("*.json")))
    assert registry, f"{on_disk} records on disk, none usable — the format moved"
    for pid, entry in registry.items():
        assert pid.isdigit()
        assert entry["session_id"]


@needs_live_store
def test_live_records_confirm_against_ps():
    # The end-to-end assertion: a record whose process is still the Claude that
    # wrote it must confirm. A regression in the start-time compare shows up here
    # as zero joins while every unit test above stays green.
    #
    # Scoped to pids claude-ps itself calls Claude, because the one record that is
    # *supposed* to fail this is a stale file whose pid the OS has since handed to
    # something else — which is the guard working, not a regression.
    registry = cp.load_session_registry()
    live = {}
    for pid in registry.keys() & set(cp.get_claude_pids()):
        info = cp.get_proc_info(pid)
        if info and info.get("started"):
            live[pid] = info
    if not live:
        pytest.skip("no recorded process still running as claude")
    failed = [
        pid
        for pid, info in live.items()
        if not cp.registry_session_for({"pid": pid, "started": info["started"]}, registry)
    ]
    assert not failed, (
        f"{len(failed)} of {len(live)} live records failed to confirm ({failed}); "
        "a start-time compare that ignores the UTC/local split does exactly this"
    )


# ── command line ─────────────────────────────────────────────────────────────


def test_help_exits_zero_and_names_the_documented_flags(capsys):
    # It used to print the process table: `--help` was just another string the
    # membership tests didn't recognize, so the tool's own flags were
    # undiscoverable from the CLI.
    with pytest.raises(SystemExit) as exc:
        cp.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--status", "--json", "--color", "--project-dir"):
        assert flag in out


def test_a_misspelled_flag_is_rejected(capsys):
    # The bug worth pinning: `--jsonn` used to be ignored with a zero exit, so a
    # script asking for JSON silently got the human table instead.
    with pytest.raises(SystemExit) as exc:
        cp.parse_args(["--jsonn"])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_flags_default_off_and_turn_on():
    off = cp.parse_args([])
    assert (off.status, off.json, off.color, off.project_dir) == (False, False, False, [])
    on = cp.parse_args(["--status", "--json", "--color"])
    assert (on.status, on.json, on.color) == (True, True, True)


@pytest.mark.parametrize("argv", [["--project-dir", "~/src/"], ["--project-dir=~/src/"]])
def test_project_dir_accepts_both_spellings(argv):
    assert cp.parse_args(argv).project_dir == ["~/src/"]


def test_project_dir_is_repeatable():
    assert cp.parse_args(["--project-dir", "~/a/", "--project-dir", "~/b/"]).project_dir == [
        "~/a/",
        "~/b/",
    ]


def test_prefixes_prefer_flags_then_env_then_default(monkeypatch):
    monkeypatch.setenv("PROJECT_DIRS", "~/from-env/")
    assert cp.project_dir_prefixes(["~/from-flag"]) == ("~/from-flag/",)
    assert cp.project_dir_prefixes([]) == ("~/from-env/",)
    monkeypatch.delenv("PROJECT_DIRS")
    assert cp.project_dir_prefixes([]) == cp.DEFAULT_PROJECT_DIRS


def test_prefixes_are_retilded_and_slash_terminated(monkeypatch):
    monkeypatch.delenv("PROJECT_DIRS", raising=False)
    home = cp.os.path.expanduser("~")
    # A shell expands ~ before the flag is seen, so the absolute form has to come
    # back to ~-relative or it will never match the table's tilde-form CWDs.
    assert cp.project_dir_prefixes([f"{home}/work"]) == ("~/work/",)


def test_shorten_cwd_trims_a_resolved_prefix():
    prefixes = ("~/projects/", "~/work/")
    assert cp.shorten_cwd("~/work/thing", prefixes) == "thing"
    assert cp.shorten_cwd("~/elsewhere/thing", prefixes) == "~/elsewhere/thing"


# ── held shells ──────────────────────────────────────────────────────────────


def _wrapper(command):
    """A Bash tool call's real command line, as the harness builds it."""
    return (
        "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1788756231717-bey77p.sh "
        "2>/dev/null || true && setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL 2>/dev/null || true "
        "&& { \\builtin unalias -- 'unsetenv'; } >/dev/null 2>&1 || true "
        f"&& eval '{command}' < /dev/null && pwd -P >| /tmp/claude-71c7-cwd"
    )


def test_shell_command_recovers_the_command_verbatim():
    # The eval payload is the only place the command survives; everything around
    # it is harness scaffolding.
    assert cp.shell_command(_wrapper('echo "hi"; sleep 240')) == 'echo "hi"; sleep 240'


def test_shell_command_ignores_shells_claude_does_not_own():
    assert cp.shell_command("/bin/zsh -c 'sleep 900'") is None
    assert cp.shell_command("npm exec chrome-devtools-mcp@1.8.0") is None


def test_an_unrecognized_wrapper_is_still_reported():
    # If the harness changes the wrapper, a held shell must not vanish from the
    # listing: "nothing running" is the one answer that must not be wrong.
    odd = "/bin/zsh -c source ~/.claude/shell-snapshots/snapshot-zsh-1.sh && exec something-new"
    assert cp.shell_command(odd) is not None


def test_parse_task_owners_reads_the_project_session_and_task_from_the_path():
    sid = "0f0f0f0f-1111-4222-8333-444444444444"
    out = "\n".join(
        [
            "p2794",
            f"n/private/tmp/claude-501/-Users-x-proj/{sid}/tasks/bc9454l6h.output",
            "p2798",
            "n/dev/ttys004",
        ]
    )
    # The slug matters as much as the id: it names the transcript directory, and
    # the transcript is the only thing that knows if the call was backgrounded.
    assert cp.parse_task_owners(out) == {"2794": ("-Users-x-proj", sid, "bc9454l6h")}


def test_parse_task_owners_ignores_unrelated_open_files():
    out = "p100\nn/Users/x/some/file.txt\nn/dev/null"
    assert cp.parse_task_owners(out) == {}


def _procs(rows):
    return {pid: (ppid, etime, cmd) for pid, ppid, etime, cmd in rows}


def test_collect_shells_finds_a_shell_under_a_claude_process():
    procs = _procs(
        [
            ("100", "1", "10:00", "claude --model opus"),
            ("200", "100", "08:27:46", _wrapper("while true; do sleep 90; done")),
            ("300", "200", "00:21", "sleep 90"),
            ("400", "1", "99:00", _wrapper("not under claude")),
        ]
    )
    rows = cp.collect_shells(procs, ["100"])
    assert len(rows) == 1
    row = rows[0]
    assert (row["pid"], row["claude_pid"], row["age"]) == ("200", "100", "08:27:46")
    assert row["command"] == "while true; do sleep 90; done"
    # The leaf is the diagnosis: this shell is parked in `sleep`.
    assert row["blocking"] == ["sleep"]


def test_collect_shells_skips_our_own_shell():
    # claude-ps runs inside exactly the kind of shell it reports.
    procs = _procs(
        [
            ("100", "1", "10:00", "claude"),
            ("200", "100", "00:01", _wrapper("claude-ps --shells")),
        ]
    )
    assert cp.collect_shells(procs, ["100"], ancestry={"200"}) == []


def test_own_ancestry_walks_to_the_root():
    procs = _procs(
        [("1", "0", "1:00", "init"), ("10", "1", "1:00", "a"), ("20", "10", "1:00", "b")]
    )
    assert cp.own_ancestry(procs, pid="20") == {"20", "10", "1"}


def test_own_ancestry_survives_a_parent_cycle():
    # ps is sampled, not atomic, so a self-referential ppid is possible; the walk
    # has to terminate rather than spin.
    procs = _procs([("10", "20", "1:00", "a"), ("20", "10", "1:00", "b")])
    assert cp.own_ancestry(procs, pid="10") == {"10", "20"}


@pytest.mark.parametrize(
    "result_text",
    [
        "Command running in background with ID: bc9454l6h. Output is being written",
        "did not complete within its 420s timeout and was moved to the background (ID: bc9454l6h).",
    ],
)
def test_backgrounded_ids_catch_both_ways_the_harness_announces_one(tmp_path, result_text):
    # The timeout case carries no marker in the tool *input*, so a scan keyed on
    # run_in_background misses it entirely.
    sid = "0f0f0f0f-1111-4222-8333-444444444444"
    (tmp_path / "slug").mkdir()
    (tmp_path / "slug" / f"{sid}.jsonl").write_text(json.dumps({"text": result_text}))
    assert cp.backgrounded_task_ids("slug", sid, root=tmp_path) == {"bc9454l6h"}


def test_backgrounded_ids_are_none_when_the_transcript_cannot_be_read(tmp_path):
    # Distinct from the empty set: "no opinion", not "nothing was backgrounded".
    assert cp.backgrounded_task_ids("slug", "missing", root=tmp_path) is None


def test_classify_marks_a_shell_the_transcript_announced_as_background():
    rows = [{"pid": "200"}]
    owners = {"200": ("slug", "sid", "bc9454l6h")}
    cp.classify_shells(rows, owners, {("slug", "sid"): {"bc9454l6h"}})
    assert rows[0]["kind"] == "background"
    assert rows[0]["task_id"] == "bc9454l6h"


def test_classify_marks_an_unannounced_task_foreground():
    # Every in-flight call holds a task file, so holding one proves nothing on its
    # own -- the transcript not naming it is what makes this a foreground call.
    rows = [{"pid": "200"}]
    owners = {"200": ("slug", "sid", "bifhvy6ln")}
    cp.classify_shells(rows, owners, {("slug", "sid"): {"bc9454l6h"}})
    assert rows[0]["kind"] == "foreground"


@pytest.mark.parametrize(
    "owners,backgrounded",
    [
        ({}, {}),
        ({"200": ("slug", "sid", "b1")}, {("slug", "sid"): None}),
    ],
)
def test_classify_says_unknown_rather_than_guessing(owners, backgrounded):
    # lsof unavailable, or a transcript that could not be read. Calling either
    # "foreground" is what would flag a healthy monitor as an abandoned shell.
    rows = [{"pid": "200"}]
    cp.classify_shells(rows, owners, backgrounded)
    assert rows[0]["kind"] == "unknown"


def test_blocking_leaves_descends_through_a_forked_subshell():
    # A pipeline or a ( ... ) group puts another shell under the wrapper; naming
    # it `zsh` reports the scaffolding instead of the blocker.
    procs = _procs(
        [
            ("200", "100", "1:00", _wrapper("( sleep 90 )")),
            ("300", "200", "1:00", "/bin/zsh"),
            ("400", "300", "1:00", "sleep 90"),
        ]
    )
    children = {"200": ["300"], "300": ["400"]}
    assert cp.blocking_leaves(procs, children, "200") == ["sleep"]


@pytest.mark.parametrize(
    "age,expected",
    [("00:21", False), ("59:59", False), ("01:00:00", True), ("2-03:04:05", True)],
)
def test_held_for_an_hour_reads_the_ps_elapsed_format(age, expected):
    assert cp.held_for_an_hour(age) is expected


def test_shell_command_keeps_the_payload_verbatim():
    # Collapsing whitespace here would rewrite the program: these are two
    # different commands, and --json promises the one that was issued.
    payload = 'printf "%s\\n" "a  b"'
    assert cp.shell_command(_wrapper(payload)) == payload


def test_shell_command_keeps_a_multi_line_payload():
    payload = "for f in *; do\n  echo $f\ndone"
    assert cp.shell_command(_wrapper(payload)) == payload


@pytest.mark.parametrize(
    "command,expected",
    [
        ("echo   a    b", "echo a b"),
        ("line one\nline two", "line one line two"),
    ],
)
def test_display_command_flattens_for_the_table(command, expected):
    assert cp.display_command(command) == expected


def test_display_command_truncates_an_unbroken_command():
    # textwrap.shorten would return only the placeholder here, hiding the very
    # command the row exists to show.
    out = cp.display_command("x" * 500, width=20)
    assert len(out) == 20
    assert out.startswith("xxx") and out.endswith("…")


def test_a_command_containing_rich_markup_does_not_break_the_listing():
    # A held command is arbitrary shell input. Passed as a str, `[/red]` makes
    # rich raise MarkupError and take the whole table down.
    rows = [
        {
            "session": "pane",
            "claude_pid": "100",
            "pid": "200",
            "age": "00:10",
            "kind": "foreground",
            "task_id": None,
            "blocking": ["sleep"],
            "command": "echo [/red] [bold]",
        }
    ]
    console = Console(file=io.StringIO(), width=200)
    console.print(cp.build_shell_table(rows))
    assert "[/red]" in console.file.getvalue()


def _proc(**over):
    """One row of the default listing, with every field main() reads."""
    row = {
        "pid": "200",
        "ppid": "100",
        "started": None,
        "cpu_pct": "0.0",
        "session": "pane",
        "cwd": "/tmp/x",
        "args": "claude",
        "waiting": None,
        "waiting_minutes": None,
        "waiting_source": None,
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    "field,value",
    [
        ("args", "claude --resume [/red]"),
        ("session", "[/red]"),
        ("cwd", "/tmp/[/red]"),
    ],
)
def test_rich_markup_in_process_text_does_not_break_the_listing(field, value):
    # Same defect as the shells table's, in the view everyone actually runs: a
    # `claude` command line reaches the cell verbatim via classify_command, and
    # as a str `[/red]` makes rich raise MarkupError and drop the whole table.
    console = Console(file=io.StringIO(), width=200)
    console.print(cp.build_process_table([_proc(**{field: value})], "1", [], status=False))
    assert "[/red]" in console.file.getvalue()


def test_the_self_marker_still_renders_next_to_an_escaped_command():
    # The marker is our own markup appended to untrusted text, so it is styled
    # rather than concatenated -- escaping the command must not eat it.
    console = Console(file=io.StringIO(), width=200)
    console.print(cp.build_process_table([_proc(args="claude [/red]")], "200", [], status=False))
    out = console.file.getvalue()
    assert "[/red]" in out and "\u25c0" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_parse_top_mem_units_and_trend_markers():
    out = "Processes: 3 total\n\nPID    MEM  \n99853  15M  \n99775  4304K\n12  1024M+\n13  2G-\n14  512B\n"
    assert cp.parse_top_mem(out) == {
        "99853": 15 * 1024,
        "99775": 4304,
        "12": 1024 * 1024,
        "13": 2 * 1024 * 1024,
        "14": 0,
    }


def test_session_memory_counts_descendants_but_not_other_sessions():
    procs = {
        "10": ("1", "", "claude"),
        "11": ("10", "", "pyright-langserver"),
        "12": ("11", "", "node worker"),
        "20": ("10", "", "claude subagent"),
        "21": ("20", "", "mcp server"),
    }
    mem = {"10": 300, "11": 1000, "12": 50, "20": 200, "21": 70}
    assert cp.session_memory("10", procs, mem, stop={"20"}) == 1350
    assert cp.session_memory("20", procs, mem, stop={"10"}) == 270


def test_format_mem():
    assert cp.format_mem(512 * 1024) == "512M"
    assert cp.format_mem(int(1.5 * 1048576)) == "1.5G"


def _render(procs, width, my_pid="1"):
    out = io.StringIO()
    Console(file=out, width=width, force_terminal=False).print(
        cp.build_process_table(procs, my_pid, [], status=False, width=width)
    )
    return out.getvalue()


@pytest.mark.parametrize("width", [80, 150])
def test_table_keeps_every_column_readable_with_a_long_command(width):
    # A long one-line command used to squeeze PID, Age, CPU and Mem to "…".
    procs = [
        _proc(pid="33409", args="claude --model opus " + "word " * 40, mem_kb=1460000),
        _proc(pid="201", session="usage-probe", cwd="/Users/user/projects-new/tack"),
    ]
    text = _render(procs, width)
    assert "33409" in text and "1.4G" in text
    assert ("PPID" in text) == (width >= 100)
    assert all(len(line) <= width for line in text.splitlines())
    assert len([line for line in text.splitlines() if line.startswith("│")]) == len(procs)
    assert "/tack" in text  # a cut CWD keeps its end, the part that differs


def test_orphan_excludes_the_daemon_and_is_marked_in_narrow_tables():
    host = _proc(pid="86263", ppid="1", args="claude --bg-pty-host /tmp/x.sock")
    daemon = _proc(
        pid="21405", ppid="1", args="/Users/m/.local/bin/claude daemon run --json-path x"
    )
    child = _proc(pid="86717", ppid="86263")
    assert [cp.is_orphan(p) for p in (host, daemon, child)] == [True, False, False]
    out = io.StringIO()
    Console(file=out, width=80, force_terminal=True, color_system="truecolor").print(
        cp.build_process_table([host, child], "1", [], status=False, width=80)
    )
    rendered = out.getvalue()
    assert "PPID" not in rendered
    marked = [line for line in rendered.splitlines() if "86263" in line]
    assert marked and "\x1b[" in marked[0].split("86263")[0][-12:]


def test_color_is_detected_without_the_flag(monkeypatch):
    # Console(force_terminal=False) means "never a terminal", so without
    # --color the listing printed no color even in a terminal.
    seen = []
    monkeypatch.setattr(cp, "Console", lambda **kw: seen.append(kw) or Console(file=io.StringIO()))
    monkeypatch.setattr(cp, "get_claude_pids", lambda: ["200"])
    monkeypatch.setattr(cp, "get_lsof_facts", lambda pids: {})
    monkeypatch.setattr(cp, "get_proc_info", lambda pid: _proc(tty="ttys001"))
    monkeypatch.setattr(cp, "get_tmux_tty_map", lambda: {})
    monkeypatch.setattr(cp, "memory_by_pid", lambda: {})
    cp.main([])
    cp.main(["--color"])
    assert [kw["force_terminal"] for kw in seen] == [None, True]
