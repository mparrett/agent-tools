"""cli: every subcommand through `cli.main`, on the synthetic store with weft
seeded from the duplicate-bug case, plus the stdlib SessionStart hook through
its fixture seam."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from workstream import cli, seam, store

TESTS = Path(__file__).resolve().parent


def _weft_has_workstream_kind() -> bool:
    exe = shutil.which(seam.weft_bin())
    if not exe:
        return False
    out = subprocess.run([exe, "add", "--help"], capture_output=True, text=True)
    return "workstream" in out.stdout + out.stderr


needs_weft = pytest.mark.skipif(
    not _weft_has_workstream_kind(), reason="installed weft lacks --kind workstream"
)


@pytest.fixture
def env(acceptance_cases, tmp_path, monkeypatch):
    """The duplicate-bug case seeded into weft. Its anchors are a worktree and
    a branch, so nothing here reaches for the real `gh`."""
    from test_acceptance import seed

    # The real registry asks `claude agents`, which would see this machine's sessions.
    monkeypatch.setattr(seam, "live_registry", lambda *a, **k: [])
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("WORKSTREAM_DOCS_ROOT", str(tmp_path / "docs"))
    # Wide enough that rich never wraps a path mid-assertion.
    monkeypatch.setenv("COLUMNS", "400")
    case = acceptance_cases["duplicate-bug"]
    seed(case)
    return case


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def run_json(capsys, *argv: str):
    code, out, err = run(capsys, *argv, "--json")
    assert code == 0, err
    return json.loads(out)


def hook(capsys, monkeypatch, payload) -> dict:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    code, out, err = run(capsys, "hook")
    assert code == 0, err
    return json.loads(out)


# --- match / attach ----------------------------------------------------------


@needs_weft
def test_match_ranks_the_case_workstream_with_reasons(env, capsys):
    d = run_json(capsys, "match", "--session", env["fresh"].id)
    assert d["session"] == env["fresh"].ref
    assert d["unassociated"] is None
    top = d["candidates"][0]
    assert top["name"] == "default-track"
    assert top["reasons"] and top["score"] > 0


@needs_weft
def test_match_text(env, capsys):
    code, out, _ = run(capsys, "match", "--session", env["fresh"].id)
    assert code == 0
    assert "default-track" in out and "attach: workstream attach default-track" in out


@needs_weft
def test_attach_new_creates_and_claims(env, capsys):
    fresh = env["fresh"]
    code, out, _ = run(
        capsys,
        "attach",
        "--new",
        "Clip export audio",
        "--name",
        "clip-audio",
        "--anchor",
        "branch:kiln@wt/fix-default-track",
        "--session",
        fresh.id,
    )
    assert code == 0
    assert "created workstream clip-audio" in out
    assert f"attached {fresh.ref} to clip-audio as canonical" in out
    ws = store.get("clip-audio")
    assert ws.title == "Clip export audio"
    assert {fresh.ref, "branch:kiln@wt/fix-default-track"} <= set(ws.refs)
    assert ws.canonical_holders() == [f"agent:{fresh.ref}"]


@needs_weft
def test_attach_new_needs_a_name(env, capsys):
    with pytest.raises(SystemExit, match="--name"):
        cli.main(["attach", "--new", "Untitled", "--session", env["fresh"].id])


@needs_weft
def test_attach_ancillary_adds_only_a_ref(env, capsys):
    fresh = env["fresh"]
    before = store.get("default-track").canonical_holders()
    code, out, _ = run(capsys, "attach", "default-track", "--ancillary", "--session", fresh.id)
    assert code == 0 and "as ancillary" in out
    ws = store.get("default-track")
    assert fresh.ref in ws.refs
    assert ws.canonical_holders() == before


@needs_weft
def test_attaching_canonical_twice_leaves_one_claim(env, capsys):
    fresh = env["fresh"]
    for _ in range(2):
        assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    ws = store.get("default-track")
    assert ws.canonical_holders().count(f"agent:{fresh.ref}") == 1
    # Refreshing while held is one continuous attachment: no released stint.
    assert [i.released_at for i in ws.intervals() if i.holder == f"agent:{fresh.ref}"] == [None]


@needs_weft
def test_attach_none_is_reported_by_match(env, capsys):
    fresh = env["fresh"]
    code, out, _ = run(capsys, "attach", "--none", "a one-off question", "--session", fresh.id)
    assert code == 0 and f"recorded {fresh.ref} as unassociated" in out
    assert run_json(capsys, "match", "--session", fresh.id)["unassociated"] == "a one-off question"


@needs_weft
def test_attach_without_target_errors(env):
    with pytest.raises(SystemExit, match="needs <ws>"):
        cli.main(["attach", "--session", env["fresh"].id])


# --- show / frontier / retire / ls -------------------------------------------


@needs_weft
def test_show_json(env, capsys):
    first, second = env["sessions"]["first"], env["sessions"]["second"]
    d = run_json(capsys, "show", "default-track")
    assert d["workstream"]["name"] == "default-track"
    fr = d["frontier"]
    assert {s["id"] for s in fr["sessions"]} == {first.id, second.id}
    assert {a["ref"] for a in fr["anchors"]} == set(env["workstreams"][0]["anchors"])


@needs_weft
def test_show_text_utc_and_local(env, capsys):
    first = env["sessions"]["first"]
    wt = str(env["worktrees"]["fix-default-track"])
    code, out, _ = run(capsys, "show", "default-track")
    assert code == 0
    assert first.ref in out and wt in out
    assert "timeline (UTC)" in out and "no frontier recorded" in out
    code, local, _ = run(capsys, "show", "default-track", "--local")
    assert code == 0
    assert "timeline (local)" in local
    # The case spans the DST end: both of its offsets show up in local mode.
    assert "-0700" in local and "-0800" in local
    assert "-0700" not in out


@needs_weft
def test_frontier_record_then_not_dropped(env, capsys, tmp_path):
    d = run_json(capsys, "frontier", "default-track", "--record", "--slug", "x")
    cap = Path(d["capture"])
    assert cap.is_file()
    assert cap.parent == tmp_path / "docs" / "x" / "workstreams" / "default-track" / "captures"
    assert all(s["dropped"] for s in d["sessions"])
    again = run_json(capsys, "frontier", "default-track")
    assert again["capture"] is None
    assert not any(s["dropped"] for s in again["sessions"])


@needs_weft
def test_frontier_text(env, capsys):
    code, out, _ = run(capsys, "frontier", "default-track")
    assert code == 0
    assert "loose ends" in out and "options (default: " in out
    assert "  fresh: start a new session, `workstream attach default-track`" in out
    assert "  resume: " in out and "claude --resume" in out
    assert "commit(s) not on" in out


@needs_weft
def test_frontier_is_concise_unless_full(env, capsys):
    wt = str(env["worktrees"]["fix-default-track"])
    code, out, _ = run(capsys, "frontier", "default-track")
    assert code == 0
    assert "timeline" not in out and "last prompt:" not in out
    # One line per anchor, a status word and no raw liveness.
    assert f"  wt:{wt}  unpushed\n" in out
    assert '"exists"' not in out
    code, full, _ = run(capsys, "frontier", "default-track", "--full")
    assert code == 0
    assert "timeline (UTC)" in full and '"exists": true' in full
    assert "loose ends" in out and "loose ends" in full


@needs_weft
def test_retire_captures_and_releases(env, capsys):
    first = env["sessions"]["first"]
    assert store.get("default-track").canonical_holders() == [f"agent:{first.ref}"]
    d = run_json(capsys, "retire", first.id[:8], "--slug", "x")
    assert d[0]["session"] == first.ref
    [entry] = d[0]["workstreams"]
    assert entry["workstream"] == "default-track" and Path(entry["capture"]).is_file()
    assert json.loads(Path(entry["capture"]).read_text())["recorded_by"] == "retire"
    ws = store.get("default-track")
    assert ws.canonical_holders() == []
    assert first.ref in ws.refs


@needs_weft
def test_retire_capture_lists_the_workstreams_tasks(env, capsys):
    first = env["sessions"]["first"]
    [entry] = run_json(capsys, "retire", first.id, "--slug", "x")[0]["workstreams"]
    cap = json.loads(Path(entry["capture"]).read_text())
    assert [(t["title"], t["state"]) for t in cap["tasks"]] == [
        ("Land the stranded default-track fix", "open"),
        ("Add an export regression test", "open"),
        ("Reproduce the dubbed-track export", "done"),
        ("Re-encode clips as a workaround", "dropped"),
    ]
    assert "2 tasks open (1 blocked)" in cap["closure_blockers"]
    assert "2 tasks open (1 blocked)" in entry["loose_ends"]


@needs_weft
def test_close_succeeds_once_the_worktree_merged_and_is_gone(env, synthetic_store, capsys):
    first, wt = env["sessions"]["first"], env["worktrees"]["fix-default-track"]
    tasks = _tasks_by_title("default-track")
    store._run(["done", tasks["Land the stranded default-track fix"]])
    store._run(["done", tasks["Add an export regression test"]])
    run_json(capsys, "retire", first.id, "--slug", "x")
    synthetic_store.retire_worktree(wt)
    code, out, _ = run(capsys, "show", "default-track")
    assert '"status": "gone (branch wt/fix-default-track merged)"' in out
    d = run_json(capsys, "close", "default-track")
    assert d["closed"] is True and d["forced"] is False and d["loose_ends"] == []


@needs_weft
def test_retire_unattached_session(env, capsys):
    code, out, _ = run(capsys, "retire", env["fresh"].id)
    assert code == 0 and "attached to no workstream" in out


@needs_weft
def test_ls(env, capsys):
    code, out, _ = run(capsys, "ls")
    assert code == 0
    [line] = [x for x in out.splitlines() if x.startswith("default-track")]
    assert "2 sessions" in line and "1 live" in line
    assert [w["name"] for w in run_json(capsys, "ls")] == ["default-track"]


# --- tasks and close ---------------------------------------------------------


def _tasks_by_title(ws_name: str) -> dict[str, str]:
    return {c.title: c.id for c in store.get(ws_name).tasks}


@needs_weft
def test_show_renders_tasks_open_first(env, capsys):
    code, out, _ = run(capsys, "show", "default-track")
    assert code == 0
    assert "[open]" in out.splitlines()[0]
    section = out.split("\ntasks\n")[1].split("\n\n")[0].splitlines()
    assert [line.split()[0] for line in section] == ["open", "open", "done", "dropped"]
    assert "Land the stranded default-track fix" in section[0] and section[0].endswith("ready")
    assert "Add an export regression test" in section[1] and section[1].endswith("blocked")


@needs_weft
def test_frontier_json_carries_tasks_and_the_open_loose_end(env, capsys):
    d = run_json(capsys, "frontier", "default-track")
    assert [(t["state"], t["blocked"]) for t in d["tasks"]] == [
        ("open", False),
        ("open", True),
        ("done", None),
        ("dropped", None),
    ]
    assert "2 tasks open (1 blocked)" in d["loose_ends"]


@needs_weft
def test_close_refuses_then_force_closes(env, capsys):
    first = env["sessions"]["first"]
    code, out, err = run(capsys, "close", "default-track")
    assert code == 1 and out == ""
    assert "not closing default-track; --force" in err
    assert f"live claim: agent:{first.ref}" in err
    assert "Land the stranded default-track fix" in err
    assert "open blocked: Add an export regression test" in err
    assert "loose end: 2 tasks open (1 blocked)" in err
    assert "commit(s) not on" in err
    # A dropped session is not unfinished work: it never blocks closing.
    assert "no recorded frontier" not in err
    assert not store.get("default-track").closed

    # Settling every task still leaves the stranded worktree commit.
    tasks = _tasks_by_title("default-track")
    store._run(["done", tasks["Land the stranded default-track fix"]])
    store._run(["done", tasks["Add an export regression test"]])
    d = json.loads(run(capsys, "close", "default-track", "--json")[1])
    assert d["closed"] is False and d["open_tasks"] == []
    assert d["live_claims"] == [f"agent:{first.ref}"]
    assert any("commit(s) not on" in x for x in d["loose_ends"])

    code, out, _ = run(capsys, "close", "default-track", "--force", "--note", "fixed upstream")
    assert code == 0
    assert out.splitlines()[0] == "closed default-track: fixed upstream"
    assert f"released agent:{first.ref}" in out
    assert "forced past 1 live claim(s), 0 open task(s)" in out
    ws = store.get("default-track")
    assert ws.closed and ws.canonical_holders() == [] and first.ref in ws.refs

    # Closed: hidden from ls, listed and marked with --all, refused by attach.
    assert run_json(capsys, "ls") == []
    code, out, _ = run(capsys, "ls", "--all")
    [line] = [x for x in out.splitlines() if x.startswith("default-track")]
    assert " closed " in line
    with pytest.raises(SystemExit, match="is closed; attach refuses.*out of scope"):
        cli.main(["attach", "default-track", "--session", env["fresh"].id])
    code, out, _ = run(capsys, "close", "default-track")
    assert code == 0 and "already closed" in out
    code, out, _ = run(capsys, "show", "default-track")
    assert "[closed]" in out.splitlines()[0] and "closed: fixed upstream" in out


@needs_weft
def test_live_claim_alone_blocks_close_until_retired(env, capsys):
    from test_acceptance import add_tasks

    sid = env["sessions"]["second"]
    ws = store.create("held-ws", "A held synthetic workstream")
    add_tasks(ws, [{"title": "Only step", "state": "done"}])
    store.attach(ws, sid.ref, canonical=True)
    code, _, err = run(capsys, "close", "held-ws")
    assert code == 1 and f"live claim: agent:{sid.ref}" in err
    run_json(capsys, "retire", sid.id, "--slug", "x")
    assert run_json(capsys, "close", "held-ws")["closed"] is True


@needs_weft
def test_retire_still_captures_a_closed_workstreams_session(env, capsys):
    first = env["sessions"]["first"]
    run(capsys, "close", "default-track", "--force")
    d = run_json(capsys, "retire", first.id, "--slug", "x")
    [entry] = d[0]["workstreams"]
    assert entry["workstream"] == "default-track" and Path(entry["capture"]).is_file()


@needs_weft
def test_close_without_force_when_everything_is_settled(env, capsys):
    from test_acceptance import add_tasks

    ws = store.create("tidy-ws", "A tidy synthetic workstream")
    add_tasks(ws, [{"title": "Only step", "state": "done"}])
    d = run_json(capsys, "frontier", "tidy-ws")
    assert d["loose_ends"] == ["all tasks done; workstream not closed"]
    assert d["closure_blockers"] == []
    d = run_json(capsys, "close", "tidy-ws")
    assert d["closed"] is True and d["forced"] is False
    assert store.get("tidy-ws").closed
    assert run_json(capsys, "frontier", "tidy-ws")["loose_ends"] == []


# --- hook --------------------------------------------------------------------


@needs_weft
def test_hook_names_the_workstream_then_orients(env, capsys, monkeypatch):
    fresh = env["fresh"]
    d = hook(capsys, monkeypatch, {"session_id": fresh.id, "source": "startup"})
    assert "- default-track:" in d["block"]
    assert "workstream attach default-track" in d["block"]
    assert 'workstream attach --none "<reason>"' in d["block"]
    assert d["candidates"] >= 1
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    d = hook(capsys, monkeypatch, {"session_id": fresh.id, "source": "resume"})
    assert d["attached"] == ["default-track"]
    lines = d["block"].splitlines()
    assert lines[0].startswith("This session is attached to these workstreams, as of ")
    # The case's two earlier sessions have no capture; this one is excluded as the starter.
    assert lines[1] == (
        "- default-track (canonical): Exported clips use the dubbed track; 2 tasks open (1 blocked); "
        "2 loose ends before anchor checks; no capture yet. "
        "Full frontier: `workstream frontier default-track`"
    )
    assert "workstream attach" not in d["block"]


@needs_weft
def test_hook_line_counts_capture_age_and_captured_sessions(env, capsys, monkeypatch, tmp_path):
    fresh = env["fresh"]
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    ws = store.get("default-track")
    cap = tmp_path / "cap.json"
    cap.write_text(json.dumps({"sessions": [{"id": s.id} for s in env["sessions"].values()]}))
    store.record_frontier(ws, str(cap))
    d = hook(capsys, monkeypatch, {"session_id": fresh.id})
    line = d["block"].splitlines()[1]
    # The capture names both earlier sessions, so neither is a loose end now.
    assert (
        "2 tasks open (1 blocked); 0 loose ends before anchor checks; newest capture 0m ago" in line
    )


@needs_weft
def test_hook_does_not_probe_anchors_for_an_attached_session(env, capsys, monkeypatch):
    from workstream import anchors

    def boom(*a, **k):
        raise AssertionError("the hook must not probe anchors")

    monkeypatch.setattr(anchors, "liveness_many", boom)
    monkeypatch.setattr(anchors, "liveness", boom)
    fresh = env["fresh"]
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    for name in ("side-one", "side-two"):
        args = ["attach", "--new", f"Side {name}", "--name", name, "--ancillary"]
        assert run(capsys, *args, "--session", fresh.id)[0] == 0

    calls = []
    real_run = store._run

    def counting(args, **k):
        calls.append(args[0])
        return real_run(args, **k)

    monkeypatch.setattr(store, "_run", counting)
    d = hook(capsys, monkeypatch, {"session_id": fresh.id})
    assert sorted(d["attached"]) == ["default-track", "side-one", "side-two"]
    assert len(d["block"].splitlines()) == 4
    # The ranker's ls + export, then the block: one show per workstream, one
    # shared export, and the blocked query because default-track has open tasks.
    assert Counter(calls) == Counter({"show": 3, "export": 2, "ls": 2})


def test_hook_unassociated_with_no_candidates_stays_quiet(capsys, monkeypatch):
    from workstream import sessions

    me = sessions.SessionFacts(id="0" * 8 + "-0000-4000-8000-000000000000", kind="claude", path="")
    monkeypatch.setattr(cli, "_rank_for", lambda *a, **k: (me, []))
    monkeypatch.setattr(store, "unassociated", lambda ref: "a one-off question")
    d = hook(capsys, monkeypatch, {"session_id": me.id})
    assert d == {"block": "", "attached": [], "unassociated": "a one-off question"}


@needs_weft
def test_hook_unknown_session_is_quiet(env, capsys, monkeypatch):
    assert hook(capsys, monkeypatch, {"session_id": "ffffffff-0000-4000-8000-00000000ffff"}) == {
        "block": ""
    }


@pytest.mark.parametrize("payload", ["{}", "not json"])
def test_hook_without_a_session_is_quiet(capsys, monkeypatch, payload):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert hook(capsys, monkeypatch, payload) == {"block": ""}


# --- hook --event SessionEnd -------------------------------------------------


def end_hook(capsys, monkeypatch, sid, reason="prompt_input_exit") -> dict:
    payload = {"session_id": sid, "hook_event_name": "SessionEnd", "reason": reason}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code, out, err = run(capsys, "hook", "--event", "SessionEnd")
    assert code == 0, err
    return json.loads(out)


@needs_weft
def test_session_end_records_per_workstream_then_skips(env, capsys, monkeypatch):
    fresh = env["fresh"]
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    args = ["attach", "--new", "Side work", "--name", "side-one", "--ancillary"]
    assert run(capsys, *args, "--session", fresh.id)[0] == 0

    d = end_hook(capsys, monkeypatch, fresh.id)
    assert d["reason"] == "prompt_input_exit" and d["skipped"] == [] and "error" not in d
    assert len(d["recorded"]) == 2
    names = set()
    for path in d["recorded"]:
        cap = json.loads(Path(path).read_text())
        assert cap["recorded_by"] == "session_end_hook"
        assert fresh.id in [s["id"] for s in cap["sessions"]]
        names.add(cap["workstream"]["name"])
    assert names == {"default-track", "side-one"}
    # The hook's weft writes name the ending session, from the payload.
    log = Path(os.environ["WEFT_HOME"]) / "log.jsonl"
    assert json.loads(log.read_text().splitlines()[-1])["by"] == f"agent:claude:{fresh.id}"
    # Written through a temp file and renamed; nothing half-written is left.
    assert not list(Path(d["recorded"][0]).parent.glob("*.tmp"))
    ws = store.get("default-track")
    assert sum(r.startswith("frontier:") for r in ws.refs) == 1
    # Recorded, never released: the claim outlives the hook.
    assert f"agent:{fresh.ref}" in ws.canonical_holders()

    again = end_hook(capsys, monkeypatch, fresh.id, reason="clear")
    assert again["recorded"] == []
    assert sorted(x["workstream"] for x in again["skipped"]) == ["default-track", "side-one"]
    assert {x["reason"] for x in again["skipped"]} == {"no activity since the newest capture"}


def _shift_facts(monkeypatch, sid, *, seen_after=None, prompt_after=None):
    """Make `sid`'s transcript look as if it continued past `Instant.now()`:
    more records (`seen_after`) or a new human prompt (`prompt_after`)."""
    from dataclasses import replace

    from workstream import sessions
    from workstream.clock import Instant

    base = Instant.now()
    real = sessions.facts

    def shifted(s, log=None):
        f = real(s, log=log)
        if f.id != sid:
            return f
        changes = {}
        if seen_after is not None:
            changes["last_seen"] = Instant(base.utc + seen_after, timedelta(0))
        if prompt_after is not None:
            at = Instant(base.utc + prompt_after, timedelta(0))
            changes["human_prompts"] = [*f.human_prompts, (at, "and one more thing")]
            changes["last_seen"] = at
        return replace(f, **changes)

    monkeypatch.setattr(sessions, "facts", shifted)


@needs_weft
def test_session_end_defers_to_a_deliberate_record_as_the_last_act(env, capsys, monkeypatch):
    fresh = env["fresh"]
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    cap = run_json(capsys, "frontier", "default-track", "--record")["capture"]
    # The record's own tool result and the closing reply land after its as_of.
    _shift_facts(monkeypatch, fresh.id, seen_after=timedelta(seconds=5))
    d = end_hook(capsys, monkeypatch, fresh.id)
    assert d["recorded"] == []
    assert d["skipped"] == [
        {
            "workstream": "default-track",
            "reason": "no prompt since the capture recorded by `frontier --record`",
            "capture": cap,
        }
    ]


@needs_weft
def test_session_end_records_when_a_prompt_follows_the_deliberate_record(env, capsys, monkeypatch):
    fresh = env["fresh"]
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    run_json(capsys, "frontier", "default-track", "--record")
    _shift_facts(monkeypatch, fresh.id, prompt_after=timedelta(seconds=30))
    [path] = end_hook(capsys, monkeypatch, fresh.id)["recorded"]
    assert json.loads(Path(path).read_text())["recorded_by"] == "session_end_hook"


@needs_weft
def test_session_end_out_of_budget_skips_and_still_reports(env, capsys, monkeypatch):
    fresh = env["fresh"]
    args = ["attach", "--new", "Side work", "--name", "side-one", "--ancillary"]
    assert run(capsys, *args, "--session", fresh.id)[0] == 0
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    monkeypatch.setattr(cli, "SESSION_END_BUDGET_S", 0)
    d = end_hook(capsys, monkeypatch, fresh.id)
    assert d["recorded"] == [] and "error" not in d
    # Canonical first, so it is the one a tight budget still reaches.
    assert d["skipped"] == [
        {"workstream": "default-track", "reason": "time budget"},
        {"workstream": "side-one", "reason": "time budget"},
    ]


@needs_weft
def test_session_end_for_an_unattached_session_records_nothing(env, capsys, monkeypatch):
    d = end_hook(capsys, monkeypatch, env["fresh"].id)
    assert d["recorded"] == [] and d["skipped"] == []


@needs_weft
def test_session_end_still_captures_a_closed_workstream(env, capsys, monkeypatch):
    first = env["sessions"]["first"]
    run(capsys, "close", "default-track", "--force")
    [path] = end_hook(capsys, monkeypatch, first.id)["recorded"]
    assert json.loads(Path(path).read_text())["recorded_by"] == "session_end_hook"


@needs_weft
def test_session_end_never_spawns_gh(env, capsys, monkeypatch):
    from workstream import anchors

    def boom(argv):
        raise AssertionError(f"the SessionEnd hook must not call gh: {argv}")

    monkeypatch.setattr(anchors, "_default_gh_runner", boom)
    store.add_anchor(store.get("default-track"), "gh:acme/clipper#7")
    [path] = end_hook(capsys, monkeypatch, env["sessions"]["first"].id)["recorded"]
    [gh] = [a for a in json.loads(Path(path).read_text())["anchors"] if a["scheme"] == "gh"]
    assert gh["liveness"]["not_probed"] == "no cached gh answer"


@needs_weft
def test_start_hook_line_says_the_capture_came_from_the_end_hook(env, capsys, monkeypatch):
    fresh = env["fresh"]
    assert run(capsys, "attach", "default-track", "--session", fresh.id)[0] == 0
    assert len(end_hook(capsys, monkeypatch, fresh.id)["recorded"]) == 1
    line = hook(capsys, monkeypatch, {"session_id": fresh.id})["block"].splitlines()[1]
    assert "newest capture 0m ago (by the SessionEnd hook, not recorded on purpose)." in line


@pytest.mark.parametrize("payload", ["{}", "not json", "[]"])
def test_session_end_without_a_session_is_quiet(capsys, monkeypatch, payload):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code, out, _ = run(capsys, "hook", "--event", "SessionEnd")
    assert code == 0 and json.loads(out) == {"recorded": [], "skipped": []}


def test_session_end_reports_an_error_and_exits_0(capsys, monkeypatch):
    def broken(*a, **k):
        raise store.WeftError("weft is down")

    monkeypatch.setattr(store, "list_workstreams", broken)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "x"})))
    code, out, err = run(capsys, "hook", "--event", "SessionEnd")
    assert code == 0 and json.loads(out)["error"] == "weft is down"
    assert err.startswith("workstream hook:")


# --- exit codes --------------------------------------------------------------


@needs_weft
def test_not_found_exits_2(env, capsys):
    code, _, err = run(capsys, "show", "no-such-workstream")
    assert code == 2 and "not found" in err
    code, _, err = run(capsys, "match", "--session", "ffffffff-0000-4000-8000-00000000ffff")
    assert code == 2


@needs_weft
def test_ambiguous_exits_4(env, capsys):
    # One session ref on two workstreams: `show <ref>` cannot pick.
    fresh = env["fresh"]
    assert run(capsys, "attach", "default-track", "--ancillary", "--session", fresh.id)[0] == 0
    args = ["attach", "--new", "Other", "--name", "other", "--ancillary", "--session", fresh.id]
    assert run(capsys, *args)[0] == 0
    code, _, err = run(capsys, "show", fresh.ref)
    assert code == 4 and "ambiguous" in err


def test_ambiguous_session_prefix_exits_4(capsys, monkeypatch):
    from workstream import sessions

    def boom(prefix):
        raise sessions.Ambiguous(prefix, ["aaaa1111", "aaaa2222"])

    monkeypatch.setattr(sessions, "resolve", boom)
    code, _, err = run(capsys, "match", "--session", "aaaa")
    assert code == 4 and "ambiguous" in err


# --- _window -----------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,delta",
    [("72h", timedelta(hours=72)), ("14d", timedelta(days=14)), ("2w", timedelta(weeks=2))],
)
def test_window(spec, delta):
    got = cli._window(spec)
    assert got.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - delta) - got) < timedelta(seconds=5)


@pytest.mark.parametrize("spec", ["", "7", "3m", "d7", "1.5d"])
def test_window_rejects_bad_specs(spec):
    with pytest.raises(SystemExit, match="bad window"):
        cli._window(spec)


# --- session_start_hook.py ---------------------------------------------------


@pytest.fixture
def start_hook():
    spec = importlib.util.spec_from_file_location(
        "session_start_hook", TESTS.parent / "session_start_hook.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_start_hook(mod, monkeypatch, capsys, fixture: Path, payload='{"session_id": "x"}'):
    monkeypatch.setenv("WORKSTREAM_HOOK_JSON", str(fixture))
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code = mod.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_start_hook_prints_the_block(start_hook, monkeypatch, capsys, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"block": "Workstream candidates: mixer-seam"}))
    code, out, _ = _run_start_hook(start_hook, monkeypatch, capsys, f)
    assert code == 0 and out == "Workstream candidates: mixer-seam\n"


def test_start_hook_prints_nothing_for_an_empty_block(start_hook, monkeypatch, capsys, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"block": "", "attached": ["mixer-seam"]}))
    assert _run_start_hook(start_hook, monkeypatch, capsys, f)[:2] == (0, "")


def test_start_hook_survives_a_broken_fixture(start_hook, monkeypatch, capsys, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text("{not json")
    code, out, err = _run_start_hook(start_hook, monkeypatch, capsys, f)
    assert (code, out) == (0, "") and err.startswith("workstream hook:")


def test_start_hook_ignores_a_non_object_payload(start_hook, monkeypatch, capsys, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"block": "never shown"}))
    assert _run_start_hook(start_hook, monkeypatch, capsys, f, payload="[]")[:2] == (0, "")


# --- session_end_hook.py -----------------------------------------------------


@pytest.fixture
def end_hook_mod():
    spec = importlib.util.spec_from_file_location(
        "session_end_hook", TESTS.parent / "session_end_hook.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_end_hook(mod, monkeypatch, capsys, payload='{"session_id": "x"}'):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code = mod.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_end_hook_prints_nothing_on_success(end_hook_mod, monkeypatch, capsys, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"recorded": ["/tmp/a.json"], "skipped": []}))
    monkeypatch.setenv("WORKSTREAM_HOOK_JSON", str(f))
    assert _run_end_hook(end_hook_mod, monkeypatch, capsys) == (0, "", "")


def test_end_hook_logs_a_reported_error(end_hook_mod, monkeypatch, capsys, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"recorded": [], "skipped": [], "error": "weft is down"}))
    monkeypatch.setenv("WORKSTREAM_HOOK_JSON", str(f))
    code, out, err = _run_end_hook(end_hook_mod, monkeypatch, capsys)
    assert (code, out) == (0, "") and err == "workstream session-end hook: weft is down\n"


def test_end_hook_survives_a_broken_fixture(end_hook_mod, monkeypatch, capsys, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text("{not json")
    monkeypatch.setenv("WORKSTREAM_HOOK_JSON", str(f))
    code, out, err = _run_end_hook(end_hook_mod, monkeypatch, capsys)
    assert (code, out) == (0, "") and err.startswith("workstream session-end hook:")
    assert err.count("\n") == 1


def test_end_hook_ignores_a_non_object_payload(end_hook_mod, monkeypatch, capsys, tmp_path):
    def boom(payload):
        raise AssertionError("a non-object payload must not reach workstream")

    monkeypatch.setattr(end_hook_mod, "fetch", boom)
    assert _run_end_hook(end_hook_mod, monkeypatch, capsys, payload="[]") == (0, "", "")


def test_end_hook_runs_the_session_end_event_through_the_bin_seam(
    end_hook_mod, monkeypatch, capsys, tmp_path
):
    seen = tmp_path / "seen"
    fake = tmp_path / "workstream"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > {seen}\n'
        f"cat >> {seen}\n"
        'echo \'{"recorded": [], "skipped": []}\'\n'
    )
    fake.chmod(0o755)
    monkeypatch.delenv("WORKSTREAM_HOOK_JSON", raising=False)
    monkeypatch.setenv("WORKSTREAM_HOOK_BIN", str(fake))
    payload = '{"session_id": "x", "reason": "clear"}'
    assert _run_end_hook(end_hook_mod, monkeypatch, capsys, payload) == (0, "", "")
    argv, stdin = seen.read_text().split("\n", 1)
    assert argv == "hook --event SessionEnd --json"
    assert json.loads(stdin) == {"session_id": "x", "reason": "clear"}


def test_end_hook_with_a_missing_bin_logs_and_exits_0(end_hook_mod, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("WORKSTREAM_HOOK_JSON", raising=False)
    monkeypatch.setenv("WORKSTREAM_HOOK_BIN", str(tmp_path / "absent"))
    code, out, err = _run_end_hook(end_hook_mod, monkeypatch, capsys)
    assert (code, out) == (0, "") and "not on PATH" in err


# --- hook budget ------------------------------------------------------------


def test_hook_skips_overlap_facts_and_messages(
    synthetic_store, acceptance_cases, monkeypatch, capsys
):
    """The hook's 4 s budget cannot afford the full-facts pass over every
    attached session or the peer-message scan; both feed only weak signals.
    The session resolves, so the call reaches the ranker, and the hooklog is
    still read for worktree events."""
    from workstream import cli, hooklog

    def boom(*a, **k):
        raise AssertionError("hook must not do this")

    monkeypatch.setattr(cli, "_recent_facts", boom)
    monkeypatch.setattr(hooklog.HookLog, "messages", boom)
    seen = {}
    real_load = hooklog.load

    def spy_load(since=None, **k):
        seen["since"] = since
        return real_load(since, **k)

    monkeypatch.setattr(hooklog, "load", spy_load)
    fresh = acceptance_cases["duplicate-bug"]["fresh"].id
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": fresh})))
    assert cli.main(["hook"]) == 0
    assert "block" in json.loads(capsys.readouterr().out)
    assert seen["since"] is not None


def test_message_to_own_subagent_renders_as_such():
    from workstream.clock import Instant
    from workstream.hooklog import MessageEvent

    at = Instant.now()
    own = MessageEvent(
        at=at,
        from_sid="a" * 36,
        to_raw="b" * 17,
        summary="",
        body="",
        to_sid="a" * 36 + "/" + "b" * 17,
        how="subagent",
    )
    peer = MessageEvent(
        at=at, from_sid="a" * 36, to_raw="pane", summary="", body="", to_sid="c" * 36, how="body"
    )
    lost = MessageEvent(at=at, from_sid="a" * 36, to_raw="pane", summary="", body="")
    assert own.to_text() == "own subagent bbbbbbbb"
    assert peer.to_text() == "cccccccc"
    assert lost.to_text() == "pane (unresolved)"


def _skip(ws, ref, reason, sid="claude:aaaaaaaa"):
    return {"workstream": ws, "ref": ref, "reason": reason, "session": sid, "how": "EnterWorktree"}


def _sweep_report():
    from workstream import sync

    gone = "the worktree no longer exists"
    return sync.Report(
        added=[
            {
                "ref": "gh:o/r#1",
                "workstream": "alpha",
                "session": "claude:aaaaaaaa",
                "how": "gh pr create",
                "at": None,
            }
        ],
        skipped=[
            _skip(
                "alpha",
                f"wt:/w/alpha/wt/{i}",
                gone,
                "claude:bbbbbbbb" if i % 2 else "claude:aaaaaaaa",
            )
            for i in range(5)
        ]
        + [
            _skip("beta", "wt:/w/beta/wt/x", gone),
            _skip("alpha", "gh:o/other#2", "it wrote nothing to o/other"),
            _skip("alpha", "gh:o/r#3", "already an anchor"),
        ],
    )


def test_sync_folds_repeated_skips_into_one_line(capsys):
    cli._print_sync(_sweep_report(), dry_run=True)
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "would add gh:o/r#1 to alpha: claude:aaaaaaaa gh pr create",
        "skipped 5 wt: refs for alpha: the worktree no longer exists "
        "(from claude:aaaaaaaa, claude:bbbbbbbb; --verbose lists them)",
        # A group of one keeps its ref: then the ref is the information.
        "skipped wt:/w/beta/wt/x for beta: the worktree no longer exists (claude:aaaaaaaa EnterWorktree)",
        "skipped gh:o/other#2 for alpha: it wrote nothing to o/other (claude:aaaaaaaa EnterWorktree)",
        "1 would add, 1 already anchored",
    ]


def test_folded_skip_names_at_most_three_sessions():
    rows = [_skip("alpha", f"wt:/w/{i}", "gone", f"claude:{i}") for i in range(5)]
    assert cli._fold_skips(rows) == [
        "skipped 5 wt: refs for alpha: gone (from claude:0, claude:1, claude:2 +2 more; "
        "--verbose lists them)"
    ]


def test_folded_skip_shortens_session_ids():
    sid = "claude:ee7781e1-2003-5805-8391-233633d24fd3"
    rows = [_skip("alpha", f"wt:/w/{i}", "gone", sid) for i in range(2)]
    assert "(from claude:ee7781e1; " in cli._fold_skips(rows)[0]


def test_sync_verbose_lists_every_skip(capsys):
    cli._print_sync(_sweep_report(), dry_run=True, verbose=True)
    out = capsys.readouterr().out
    assert out.count("skipped wt:/w/alpha/wt/") == 5
    assert "--verbose lists them" not in out


@needs_weft
@pytest.mark.parametrize("argv", [["ls"], ["sync", "--dry-run"]])
def test_ls_and_sync_say_when_there_is_no_hook_log(env, capsys, monkeypatch, tmp_path, argv):
    monkeypatch.setenv("CLAUDE_HOOK_LOG_DIR", str(tmp_path / "no-log"))
    code, out, err = run(capsys, *argv)
    assert code == 0 and "no hook log under" in err and "no hook log" not in out
    code, out, err = run(capsys, *argv, "--json")
    assert code == 0 and "no hook log" not in err
    json.loads(out)


@needs_weft
def test_no_hook_log_hint_when_the_log_exists(env, capsys):
    code, _, err = run(capsys, "ls")
    assert code == 0 and "no hook log" not in err


def _review_report():
    from workstream import sync

    def add(ws, ref):
        return {
            "workstream": ws,
            "ref": ref,
            "session": "claude:aaaaaaaa",
            "how": "gh pr create",
            "at": None,
        }

    return sync.Report(
        added=[add("alpha", "gh:o/r#1"), add("alpha", "gh:o/r#2"), add("beta", "gh:o/r#3")],
        retracted=[
            {
                "workstream": "alpha",
                "ref": "wt:/w/gone",
                "branch": "b",
                "how": "x",
                "reason": "merged",
            }
        ],
        skipped=[_skip("alpha", "gh:o/r#9", "already an anchor")],
    )


def _keys(*keys):
    it = iter(keys)
    return lambda prompt: next(it)


def _items(report):
    return [("add", r) for r in report.added] + [("retract", r) for r in report.retracted]


@pytest.mark.parametrize(
    "keys, accepted, stopped",
    [
        # One workstream at a time, additions before retractions.
        (("y", "n", "y", "n"), {"gh:o/r#1", "wt:/w/gone"}, False),
        # w accepts the rest of alpha, its retraction included, then asks for beta.
        (("w", "n"), {"gh:o/r#1", "gh:o/r#2", "wt:/w/gone"}, False),
        (("n", "a"), {"gh:o/r#2", "gh:o/r#3", "wt:/w/gone"}, False),
        # q declines everything left.
        (("y", "q"), {"gh:o/r#1"}, True),
        # An unknown key asks again rather than counting as no.
        (("?", "", "y", "y", "y", "y"), {"gh:o/r#1", "gh:o/r#2", "gh:o/r#3", "wt:/w/gone"}, False),
    ],
)
def test_review_sync_keys(keys, accepted, stopped):
    from rich.console import Console

    items = _items(_review_report())
    answers, quit_ = cli._review_sync(items, Console(file=io.StringIO()), _keys(*keys))
    assert {r["ref"] for (_, r), yes in zip(items, answers) if yes} == accepted
    assert quit_ is stopped


def _interactive(monkeypatch, keys, reports=None, fail=()):
    from rich.console import Console

    from workstream import sync

    writes, calls = [], []
    reports = list(reports or [_review_report()])

    def fake_run(targets, **kw):
        kw["progress"]("alpha: s", 0, 1)
        calls.append(set(kw["exclude"]))
        return reports[min(len(calls), len(reports)) - 1]

    def add_anchor(ws, ref, source):
        if ref in fail:
            raise store.WeftError("ref add failed")
        writes.append(("add", ws, ref, source))

    monkeypatch.setattr(sync, "run", fake_run)
    monkeypatch.setattr(store, "add_anchor", add_anchor)
    monkeypatch.setattr(store, "rm_anchor", lambda ws, ref: writes.append(("rm", ws, ref)))
    targets = [store.Workstream.__new__(store.Workstream) for _ in range(2)]
    monkeypatch.setattr(sync, "_label", lambda ws: "alpha" if ws is targets[0] else "beta")
    buf = io.StringIO()
    code = cli._sync_interactive(targets, {}, Console(file=buf, width=200), verbose=False, ask=keys)
    return code, writes, buf.getvalue(), targets, calls


def test_sync_interactive_writes_only_what_was_accepted(monkeypatch):
    code, writes, out, (alpha, beta), calls = _interactive(monkeypatch, _keys("y", "n", "n", "y"))
    assert code == 0
    assert writes == [
        ("add", alpha, "gh:o/r#1", store.SYNC_SOURCE),
        ("add", beta, "gh:o/r#3", store.SYNC_SOURCE),
    ]
    assert "2 added, 1 declined, 1 already anchored, 0 retracted (1 kept)" in out
    # Declining an addition sweeps again without it; nothing new came of it.
    assert calls == [set(), {("alpha", "gh:o/r#2")}]


def test_sync_interactive_offers_what_a_decline_freed(monkeypatch):
    from workstream import sync

    def add(ws, ref):
        return {"workstream": ws, "ref": ref, "session": "claude:a", "how": "git push", "at": None}

    first = sync.Report(
        added=[add("alpha", "gh:o/r#5")], skipped=[_skip("beta", "gh:o/r#5", "an anchor of alpha")]
    )
    second = sync.Report(
        added=[add("beta", "gh:o/r#5")], skipped=[_skip("alpha", "gh:o/r#5", "declined")]
    )
    code, writes, out, (_, beta), calls = _interactive(
        monkeypatch, _keys("n", "y"), reports=[first, second]
    )
    assert code == 0 and writes == [("add", beta, "gh:o/r#5", store.SYNC_SOURCE)]
    assert "declining freed 1 more" in out and "an anchor of alpha" not in out
    assert "1 added, 1 declined" in out


def test_sync_interactive_reports_a_failed_write_and_keeps_going(monkeypatch):
    code, writes, out, (alpha, beta), _ = _interactive(monkeypatch, _keys("a"), fail={"gh:o/r#1"})
    assert code == 1
    assert [w[2] for w in writes] == ["gh:o/r#2", "gh:o/r#3", "wt:/w/gone"]
    assert "failed: add gh:o/r#1 for alpha: ref add failed" in out
    assert "2 added" in out and "1 failed; rerun to retry" in out


def test_sync_interactive_interrupted_writes_nothing(monkeypatch):
    def interrupted(prompt):
        raise EOFError

    code, writes, out, _, _ = _interactive(monkeypatch, interrupted)
    assert (code, writes) == (130, [])
    assert "nothing written" in out


def test_sync_interactive_needs_a_terminal(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit, match="needs a terminal"):
        cli.main(["sync", "--interactive"])
    with pytest.raises(SystemExit, match="neither --json nor --dry-run"):
        cli.main(["sync", "--interactive", "--dry-run"])
