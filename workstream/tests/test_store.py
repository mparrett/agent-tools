"""store.py against a real weft binary in a throwaway WEFT_HOME.

The binary is `seam.weft_bin()`: `weft` on PATH unless WORKSTREAM_WEFT_BIN
overrides it. The round-trip tests skip when that weft predates
`--kind workstream`; the fake-binary tests always run.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import replace
from datetime import timedelta
from functools import lru_cache

import pytest

from workstream import seam, store
from workstream.clock import Instant


def _supports_workstream(path: str) -> bool:
    try:
        out = subprocess.run([path, "add", "--help"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "workstream" in (out.stdout + out.stderr)


def _find_weft() -> str | None:
    path = shutil.which(seam.weft_bin())
    return path if path and _supports_workstream(path) else None


WEFT = _find_weft()


@lru_cache(maxsize=None)
def _keeps_released_claims(path: str | None) -> bool:
    """`weft --version` does not say, so ask: claim, release, export, in a
    throwaway WEFT_HOME (never the real store). Cached, and called only by
    the fixture, so it costs four processes once and only when needed."""
    if path is None:
        return False
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "WEFT_HOME": home, "WEFT_ACTOR": "agent:probe"}
        env.pop("WEFT_NS", None)

        def run(*args: str) -> dict:
            out = subprocess.run(
                [path, *args, "--json"], capture_output=True, text=True, timeout=5, env=env
            )
            return json.loads(out.stdout) if out.returncode == 0 else {}

        try:
            nid = run("add", "probe", "--kind", "task")["nodes"][0]["id"]
            run("claim", nid, "--holder", "agent:probe")
            run("release", nid, "--holder", "agent:probe")
            nodes = run("export", "--include", "claims").get("nodes") or []
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError):
            return False
    return any(n.get("released_claims") for n in nodes)


@pytest.fixture
def weft_store(tmp_path, monkeypatch):
    if WEFT is None:
        pytest.skip(
            f"weft ({seam.weft_bin()}) is missing or predates `--kind workstream`; "
            "install a newer weft or point WORKSTREAM_WEFT_BIN at one"
        )
    home = tmp_path / "weft-home"
    home.mkdir()
    monkeypatch.setenv("WEFT_HOME", str(home))
    monkeypatch.setenv("WORKSTREAM_WEFT_BIN", WEFT)
    monkeypatch.setenv("WEFT_ACTOR", "agent:test-runner")
    monkeypatch.delenv("WEFT_NS", raising=False)
    return home


def _fake_weft(tmp_path, monkeypatch, body: str) -> None:
    script = tmp_path / "fake-weft"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("WORKSTREAM_WEFT_BIN", str(script))


# --- create / get / list ----------------------------------------------------


def test_create_get_roundtrip(weft_store):
    ws = store.create(
        "gizmo-port", "Port the gizmo", anchors=["gh:acme/gizmo#12", "branch:gizmo@port"]
    )
    assert ws.name == "gizmo-port"
    assert ws.title == "Port the gizmo"
    assert ws.state == "open"
    assert {"ws:gizmo-port", "gh:acme/gizmo#12", "branch:gizmo@port"} <= set(ws.refs)
    assert isinstance(ws.created, Instant) and isinstance(ws.touched, Instant)
    assert ws.created.utc.tzinfo is not None
    assert ws.claims == [] and ws.children == []

    by_name = store.get("gizmo-port")
    by_ref = store.get("ws:gizmo-port")
    by_prefix = store.get(ws.id[:10])
    assert by_name.id == by_ref.id == by_prefix.id == ws.id


def test_instant_keeps_offset():
    i = store._instant("2026-10-29T07:58:02.123456789-05:00")
    assert i.offset == timedelta(hours=-5)
    assert i.utc.isoformat().startswith("2026-10-29T12:58:02.123456")


def test_list_scoped_to_namespace_and_kind(weft_store):
    a = store.create("alpha-ws", "Alpha")
    b = store.create("beta-ws", "Beta")
    # a workstream in another namespace and a task in ours: neither is listed
    subprocess.run(
        [WEFT, "add", "Elsewhere", "--kind", "workstream", "--ns", "other", "--json"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [WEFT, "add", "A task", "--kind", "task", "--ns", store.NS, "--json"],
        check=True,
        capture_output=True,
    )
    store.attach(a, "claude:1111aaaa", canonical=True, note="driving")

    got = {w.name: w for w in store.list_workstreams()}
    assert set(got) == {"alpha-ws", "beta-ws"}
    assert got["alpha-ws"].canonical_holders() == ["agent:claude:1111aaaa"]
    assert got["beta-ws"].claims == []
    assert {w.id for w in store.list_workstreams(since="7d")} == {a.id, b.id}
    assert len(store.list_workstreams(since=timedelta(hours=2))) == 2


def test_list_empty_store(weft_store):
    assert store.list_workstreams() == []


def test_get_rejects_non_workstream(weft_store):
    note_id = store.record_unassociated("claude:2222bbbb", "scratch poking")
    with pytest.raises(store.NotFound):
        store.get(note_id)


def _weft(*args: str) -> dict:
    out = subprocess.run([WEFT, *args, "--json"], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def _task(title: str, parent: store.Workstream) -> str:
    nid = _weft("add", title, "--kind", "task", "--ns", store.NS)["nodes"][0]["id"]
    _weft("link", nid, "part-of", parent.id)
    return nid


def test_children_are_part_of_sub_workstreams_and_tasks(weft_store):
    parent = store.create("big-ws", "Big")
    child = store.create("small-ws", "Small")
    _weft("link", child.id, "part-of", parent.id)
    t = _task("Wire the gizmo", parent)
    _weft("done", t)
    got = store.get("big-ws")
    assert got.children == [
        (child.id, "Small", "workstream", "open"),
        (t, "Wire the gizmo", "task", "done"),
    ]
    assert got.sub_workstreams == [got.children[0]]
    assert got.tasks == [got.children[1]]


def test_blocked_ids(weft_store):
    ws = store.create("blk-ws", "Blocked")
    first = _task("First", ws)
    second = _task("Second", ws)
    _weft("link", second, "depends-on", first)
    # part-of never blocks: only the depends-on edge does.
    assert store.blocked_ids() == {second}
    _weft("done", first)
    assert store.blocked_ids() == set()


# --- close ------------------------------------------------------------------


def test_close_releases_claims_marks_done_and_hides_from_ls(weft_store):
    ws = store.create("fin-ws", "Finished")
    other = store.create("live-ws", "Still going")
    store.attach(ws, "claude:c1c1c1c1", canonical=True)
    store.attach(ws, "claude:c1c1c1c1", canonical=True)
    store.attach(ws, "claude:c2c2c2c2", canonical=True)
    got = store.close("fin-ws", "shipped in the synthetic release")
    assert got.closed and got.state == "done"
    assert got.canonical_holders() == []
    assert {"claude:c1c1c1c1", "claude:c2c2c2c2"} <= set(got.refs)
    assert got.closed_note == "shipped in the synthetic release"
    assert got.children == []  # the note is derived-from, not part-of
    assert [w.id for w in store.list_workstreams()] == [other.id]
    assert {w.id for w in store.list_workstreams(include_closed=True)} == {ws.id, other.id}
    # The closure note must not surface as work in the cross-workstream frontier.
    ready = _weft("ls", "--ns", store.NS, "--ready")["nodes"] or []
    assert not [n for n in ready if n["title"].startswith(store.CLOSED_PREFIX)]
    # ...nor read as an unassociated-session record.
    assert store.unassociated("claude:c1c1c1c1") is None


def test_notes_never_surface_as_ready_work(weft_store):
    ws = store.create("notes-ws", "Notes")
    store.record_unassociated("claude:d1d1d1d1", "one-off lookup")
    store.close(ws, "wrapped up")
    titles = [n["title"] for n in _weft("ls", "--ns", store.NS, "--ready")["nodes"] or []]
    assert not [t for t in titles if t.startswith((store.UNASSOCIATED_PREFIX, store.CLOSED_PREFIX))]
    assert store.unassociated("claude:d1d1d1d1") == "one-off lookup"


def test_close_resolved_workstream_needs_no_show(weft_store, monkeypatch):
    ws = store.create("cheap-ws", "Cheap")
    store.attach(ws, "claude:e1e1e1e1", canonical=True)
    ws = store.get("cheap-ws")
    calls = []
    real = store._run

    def spy(args, **kw):
        calls.append(args[0])
        return real(args, **kw)

    monkeypatch.setattr(store, "_run", spy)
    got = store.close(ws, "done here")
    assert "show" not in calls and "export" not in calls
    assert got.closed and got.canonical_holders() == [] and got.closed_note == "done here"
    monkeypatch.setattr(store, "_run", real)
    fresh = store.get("cheap-ws")
    assert (fresh.state, fresh.canonical_holders(), fresh.closed_note) == ("done", [], "done here")


def test_children_cost_one_export(weft_store, monkeypatch):
    ws = store.create("many-ws", "Many")
    for i in range(4):
        _task(f"Step {i}", ws)
    calls = []
    real = store._run

    def spy(args, **kw):
        calls.append(args[0])
        return real(args, **kw)

    monkeypatch.setattr(store, "_run", spy)
    assert len(store.get("many-ws").tasks) == 4
    assert calls.count("show") == 1 and calls.count("export") == 1


def test_close_without_note(weft_store):
    store.create("bare-ws", "Bare")
    got = store.close("bare-ws")
    assert got.closed and got.closed_note is None


# --- attach / detach --------------------------------------------------------


def test_attach_canonical_claims(weft_store):
    ws = store.create("canon-ws", "Canon")
    store.attach(ws, "claude:3333cccc", canonical=True, note="main driver")
    got = store.get("canon-ws")
    assert "claude:3333cccc" in got.refs
    (c,) = got.claims
    assert c.holder == "agent:claude:3333cccc"
    assert c.note == "main driver" and c.live and c.source == "observed"
    assert isinstance(c.at, Instant) and c.expires.utc > c.at.utc


def test_attach_ancillary_is_ref_only(weft_store):
    store.create("anc-ws", "Anc")
    store.attach("anc-ws", "codex:rollout-4444", canonical=False)
    got = store.get("anc-ws")
    assert "codex:rollout-4444" in got.refs
    assert got.claims == []


def test_detach_releases_claim_keeps_ref(weft_store):
    ws = store.create("det-ws", "Det")
    store.attach(ws, "claude:5555dddd", canonical=True)
    # attach never stacks a held claim; a raw second claim makes two to release.
    _weft("claim", ws.id, "--holder", "agent:claude:5555dddd")
    store.attach(ws, "claude:6666eeee", canonical=True)
    assert store.detach(ws, "claude:5555dddd") is True
    got = store.get("det-ws")
    assert "claude:5555dddd" in got.refs
    assert got.canonical_holders() == ["agent:claude:6666eeee"]
    assert store.detach(ws, "claude:5555dddd") is False


# --- anchors / frontier -----------------------------------------------------


def test_anchor_add_rm(weft_store):
    store.create("anc2-ws", "Anchors")
    store.add_anchor("anc2-ws", "wt:/work/scratch-tree")
    store.add_anchor("anc2-ws", "wt:/work/scratch-tree")  # idempotent
    assert store.get("anc2-ws").refs.count("wt:/work/scratch-tree") == 1
    store.rm_anchor("anc2-ws", "wt:/work/scratch-tree")
    store.rm_anchor("anc2-ws", "wt:/work/scratch-tree")  # already gone: still fine
    assert "wt:/work/scratch-tree" not in store.get("anc2-ws").refs


def test_wt_anchor_is_canonicalized(weft_store, tmp_path, monkeypatch):
    real = tmp_path / "real-tree"
    real.mkdir()
    link = tmp_path / "link-tree"
    link.symlink_to(real)
    monkeypatch.chdir(tmp_path)
    ws = store.create("canon-wt", "Canon wt", anchors=["wt:link-tree"])
    want = f"wt:{real.resolve()}"
    assert want in ws.refs
    store.add_anchor(ws, f"wt:{link}")  # same workspace, other spelling: no new ref
    assert [r for r in store.get(ws.id).refs if r.startswith("wt:")] == [want]
    store.rm_anchor(ws, "wt:./link-tree")
    assert not [r for r in store.get(ws.id).refs if r.startswith("wt:")]


def test_record_frontier(weft_store):
    ws = store.create("fr-ws", "Frontier")
    store.record_frontier(ws, "/docs/fr-ws/captures/20261102T000000Z-abcd1234.json")
    assert "frontier:/docs/fr-ws/captures/20261102T000000Z-abcd1234.json" in store.get(ws.id).refs


# --- unassociated -----------------------------------------------------------


def test_unassociated_record_and_lookup(weft_store):
    assert store.unassociated("claude:7777ffff") is None
    store.record_unassociated("claude:7777ffff", "one-off question")
    assert store.unassociated("claude:7777ffff") == "one-off question"
    assert store.unassociated("claude:8888aaaa") is None


def test_unassociated_survives_session_also_attached(weft_store):
    # The session ref then sits on a workstream too; lookup must not go ambiguous.
    store.create("both-ws", "Both")
    store.attach("both-ws", "claude:9999bbbb", canonical=False)
    store.record_unassociated("claude:9999bbbb", "changed my mind")
    assert store.unassociated("claude:9999bbbb") == "changed my mind"


# --- attach intervals -------------------------------------------------------


@pytest.fixture
def released_store(weft_store):
    if not _keeps_released_claims(WEFT):
        pytest.skip(
            f"weft ({WEFT}) does not export released_claims; any build of the public weft repo keeps them"
        )
    return weft_store


def test_attach_intervals_end_at_release(released_store):
    ws = store.create("iv-ws", "Intervals")
    store.attach(ws, "claude:aaaa0001", canonical=True, note="first")
    store.attach(ws, "claude:aaaa0003", canonical=False)  # ancillary: no interval
    store.detach(ws, "claude:aaaa0001")
    store.attach(ws, "claude:aaaa0001", canonical=True, note="second")
    store.detach(ws, "claude:aaaa0001")
    store.attach(ws, "claude:aaaa0001", canonical=True, note="third")

    first, second, third = store.attach_intervals(ws)
    assert {i.holder for i in (first, second, third)} == {"agent:claude:aaaa0001"}
    assert [i.note for i in (first, second, third)] == ["first", "second", "third"]
    assert first.at.utc <= first.released_at.utc <= second.at.utc
    assert second.at.utc <= second.released_at.utc <= third.at.utc
    assert third.released_at is None and third.expires.utc > third.at.utc

    got = store.get(ws.id)
    (live,) = got.claims
    assert (live.note, live.live, live.released_at) == ("third", True, None)
    assert [(c.note, c.live) for c in got.released] == [("first", False), ("second", False)]
    assert got.released[0].released_at == first.released_at
    assert got.intervals() == [first, second, third]
    (listed,) = store.list_workstreams()
    assert listed.released == got.released


def test_reattach_while_held_is_one_claim(weft_store):
    ws = store.create("re-ws", "Reattach")
    store.attach(ws, "claude:dddd0001", canonical=True, note="taken")
    store.attach("re-ws", "claude:dddd0001", canonical=True, note="refreshed")
    got = store.get("re-ws")
    (c,) = got.claims
    assert (c.holder, c.note, c.live) == ("agent:claude:dddd0001", "taken", True)
    assert got.released == []


def test_close_keeps_released_intervals(released_store):
    ws = store.create("cl-iv-ws", "Close intervals")
    store.attach(ws, "claude:eeee0001", canonical=True, note="earlier")
    store.detach(ws, "claude:eeee0001")
    store.attach(ws, "claude:eeee0001", canonical=True, note="last")
    store.attach(ws, "claude:eeee0002", canonical=True, note="other")
    closed = store.close(store.get(ws.id))
    ivs, fresh = closed.intervals(), store.get(ws.id).intervals()
    assert [i.note for i in ivs] == ["earlier", "last", "other"]
    # close reads no export, so the claims it released carry the close time,
    # a moment after weft's release; everything else matches a fresh get.
    assert [replace(i, released_at=None) for i in ivs] == [
        replace(i, released_at=None) for i in fresh
    ]
    assert ivs[0].released_at == fresh[0].released_at
    for mine, weft in zip(ivs[1:], fresh[1:]):
        assert timedelta(0) <= mine.released_at.utc - weft.released_at.utc < timedelta(seconds=5)


def test_attach_intervals_without_released_claims(monkeypatch):
    # An older weft: export has no released_claims, so a released
    # attach is simply missing and nothing carries an end.
    node = {
        "id": "ws-0000aaaa",
        "kind": "workstream",
        "title": "Older weft",
        "state": "open",
        "refs": [{"ref": "ws:older-ws"}, {"ref": "claude:cccc0001"}],
        "claims": [
            {
                "holder": "agent:claude:cccc0002",
                "note": "held",
                "at": "2026-11-02T09:00:00-08:00",
                "expires": "2099-11-09T09:00:00-08:00",
            }
        ],
    }
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args[0] == "show":
            return {"node": node, "claims": [{**node["claims"][0], "live": True}]}
        return {"nodes": [node], "edges": []}

    monkeypatch.setattr(store, "_run", fake_run)
    got = store.get("older-ws")
    assert got.released == [] and got.canonical_holders() == ["agent:claude:cccc0002"]
    (iv,) = store.attach_intervals(got)
    assert (iv.holder, iv.note, iv.released_at) == ("agent:claude:cccc0002", "held", None)
    assert calls[-1] == ["export", "--include", "all"]


def test_attach_intervals_keep_expired_claims(weft_store):
    ws = store.create("exp-ws", "Expired")
    subprocess.run(
        [WEFT, "claim", ws.id, "--holder", "agent:claude:bbbb0001", "--ttl", "1ms", "--json"],
        check=True,
        capture_output=True,
    )
    (iv,) = store.attach_intervals(ws)
    assert iv.holder == "agent:claude:bbbb0001"
    (c,) = store.get(ws.id).claims
    assert c.live is False


# --- error mapping ----------------------------------------------------------


def test_not_found_from_real_weft(weft_store):
    with pytest.raises(store.NotFound) as e:
        store.get("no-such-ws")
    assert e.value.code == 2


def test_ambiguous_from_real_weft(weft_store):
    a = store.create("dup-a", "Dup A")
    b = store.create("dup-b", "Dup B")
    store.add_anchor(a, "ws:shared-name")
    store.add_anchor(b, "ws:shared-name")
    with pytest.raises(store.Ambiguous) as e:
        store.get("shared-name")
    assert e.value.code == 4
    assert set(e.value.candidates) == {a.id, b.id}


@pytest.mark.parametrize(
    "code,exc",
    [
        (2, store.NotFound),
        (4, store.Ambiguous),
        (6, store.StoreError),
        (7, store.ExternalFailed),
        (5, store.WeftError),
    ],
)
def test_exit_code_mapping(tmp_path, monkeypatch, code, exc):
    _fake_weft(
        tmp_path,
        monkeypatch,
        f'echo \'{{"error": "synthetic failure", "code": {code}, "candidates": ["c1", "c2"]}}\'; exit {code}',
    )
    with pytest.raises(exc) as e:
        store._run(["show", "x"])
    assert type(e.value) is exc
    assert e.value.code == code
    assert "synthetic failure" in str(e.value)
    if exc is store.Ambiguous:
        assert e.value.candidates == ["c1", "c2"]


def test_usage_error_uses_stderr(tmp_path, monkeypatch):
    _fake_weft(tmp_path, monkeypatch, "echo 'weft add: unknown kind' >&2; exit 1")
    with pytest.raises(store.WeftError) as e:
        store._run(["add", "x"])
    assert e.value.code == 1 and "unknown kind" in str(e.value)


def test_timeout(tmp_path, monkeypatch):
    _fake_weft(tmp_path, monkeypatch, "sleep 5")
    with pytest.raises(store.WeftError) as e:
        store._run(["ls"], timeout=0.3)
    assert e.value.code is None and "timed out" in str(e.value)
    monkeypatch.setattr(store, "TIMEOUT", 0.3)
    with pytest.raises(store.WeftError, match="timed out"):
        store.get("anything")


def test_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSTREAM_WEFT_BIN", str(tmp_path / "absent-weft"))
    with pytest.raises(store.WeftError, match="not found") as e:
        store.list_workstreams()
    assert e.value.code is None


def test_actor_and_home_pass_through(tmp_path, monkeypatch):
    _fake_weft(
        tmp_path,
        monkeypatch,
        'printf \'{"nodes": [], "actor": "%s", "home": "%s", "argv": "%s"}\' "$WEFT_ACTOR" "$WEFT_HOME" "$*"',
    )
    monkeypatch.setenv("WEFT_HOME", "/synthetic/home")
    monkeypatch.setenv("WEFT_ACTOR", "agent:from-env")
    out = store._run(["ls"])
    assert out["actor"] == "agent:from-env" and out["home"] == "/synthetic/home"
    assert out["argv"] == "ls --json"
    assert store._run(["ls"], actor="agent:override")["actor"] == "agent:override"
