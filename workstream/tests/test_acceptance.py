"""Spec acceptance (item 5): a fresh session with only a vague prompt and one
lookup reaches the right workstream and the frontier names the loose end, for
each of the four mined cases rebuilt with invented names in tests/synthetic.py.

weft is seeded from each case's ground truth; the matcher and frontier see
only what the stores hold."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from workstream import attribution, frontier, hooklog, seam, sessions, store


def _weft_has_workstream_kind() -> bool:
    exe = shutil.which(seam.weft_bin())
    if not exe:
        return False
    out = subprocess.run([exe, "add", "--help"], capture_output=True, text=True)
    return "workstream" in out.stdout + out.stderr


pytestmark = pytest.mark.skipif(
    not _weft_has_workstream_kind(), reason="installed weft lacks --kind workstream"
)


def seed(case: dict) -> dict[str, store.Workstream]:
    out = {}
    for spec in case["workstreams"]:
        ws = store.create(spec["name"], spec["title"], anchors=list(spec["anchors"]))
        for s in spec["canonical"]:
            store.attach(ws, s.ref, canonical=True, note="seed")
        for s in spec["ancillary"]:
            store.attach(ws, s.ref, canonical=False, note="seed")
        add_tasks(ws, spec.get("tasks", []))
        out[spec["name"]] = store.get(spec["name"])
    return out


def add_tasks(ws: store.Workstream, tasks: list[dict]) -> list[str]:
    """Task children the way a person adds them with weft: a task node in the
    workstreams namespace, `part-of` the workstream. Returns their ids."""
    ids = []
    for t in tasks:
        nid = store._run(["add", t["title"], "--kind", "task", "--ns", store.NS])["nodes"][0]["id"]
        store._run(["link", nid, "part-of", ws.id])
        ids.append(nid)
    for nid, t in zip(ids, tasks):
        if "depends_on" in t:
            store._run(["link", nid, "depends-on", ids[t["depends_on"]]])
        if t.get("state") == "done":
            store._run(["done", nid])
        elif t.get("state") == "dropped":
            store._run(["drop", nid])
    return ids


def gh_runner_for(case: dict):
    def run(argv: list[str]) -> tuple[int, str]:
        if "--repo" not in argv:
            return 1, ""
        repo = argv[argv.index("--repo") + 1]
        num = argv[3]
        data = case["gh"].get(f"{repo}#{num}")
        return (0, json.dumps(data)) if data and argv[1] == "pr" else (1, "")

    return run


def match_for(case: dict, log):
    fresh = sessions.facts(case["fresh"].id, log=log)
    wss = store.list_workstreams()
    recent = []
    for ws in wss:
        for ref in ws.refs:
            scheme, _, value = ref.partition(":")
            if scheme in ("claude", "codex"):
                recent.append(sessions.facts(value, log=log))
    msgs = [m for m in log.messages() if fresh.id in (m.from_sid, m.to_sid)]
    return fresh, attribution.rank(fresh, wss, recent, msgs)


@pytest.fixture
def log(synthetic_store):
    return hooklog.load(registry=[])


@pytest.mark.parametrize(
    "name", ["audio-seam", "pr-ownership", "duplicate-bug", "cross-repo-merge"]
)
def test_fresh_session_reaches_the_right_workstream(acceptance_cases, log, name):
    case = acceptance_cases[name]
    seed(case)
    fresh, ranked = match_for(case, log)
    assert ranked, f"{name}: no candidates for {fresh.ref}"
    top = ranked[0]
    assert top.ws.name == case["expected"], [(c.ws.name, c.score, c.reasons) for c in ranked]
    assert top.reasons, "a match must say why"


def test_audio_seam_frontier_names_the_unaddressed_review(acceptance_cases, log):
    case = acceptance_cases["audio-seam"]
    wss = seed(case)
    fr = frontier.build(wss["mixer-seam"], log=log, gh_runner=gh_runner_for(case))
    assert any("changes requested" in le for le in fr.loose_ends), fr.loose_ends
    # Three-hop pasted-id chain, all canonical, none with a recorded frontier.
    roles = {s.facts.id: s.role for s in fr.sessions}
    assert all(roles[sid] == "canonical" for sid in case["chain"])
    assert all(s.dropped for s in fr.sessions if s.role == "canonical")
    # Two of the mined conventions: UTC timeline, with each event's offset kept.
    assert fr.timeline and all(r[0].utc.tzinfo is not None for r in fr.timeline)
    assert fr.recommendation.action in ("resume", "fresh")


def test_pr_ownership_frontier_names_the_owner(acceptance_cases, log):
    case = acceptance_cases["pr-ownership"]
    wss = seed(case)
    fr = frontier.build(wss["main-tests"], log=log, gh_runner=gh_runner_for(case))
    canon = [s for s in fr.sessions if s.role == "canonical"]
    assert [s.facts.id for s in canon] == [case["loose_end"]["session"]]
    assert any(a.ref == case["loose_end"]["ref"] for a in fr.anchors)
    assert any(a.scheme == "branch" and a.liveness.get("exists_local") for a in fr.anchors)


def test_duplicate_bug_frontier_names_the_stranded_commit(acceptance_cases, log):
    case = acceptance_cases["duplicate-bug"]
    wss = seed(case)
    fr = frontier.build(wss["default-track"], log=log, gh_runner=gh_runner_for(case))
    le = case["loose_end"]
    wt = next(a for a in fr.anchors if a.ref == le["ref"])
    assert wt.liveness["exists"] and wt.liveness["unmerged"] == 1
    assert wt.liveness["unpushed"] == "no remote"
    assert any("commit(s) not on" in x and "no remote" in x for x in fr.loose_ends), fr.loose_ends
    first = next(s for s in fr.sessions if s.role == "canonical")
    assert first.dropped and first.lifecycle == "SessionStart(resume)"


def test_cross_repo_merge_frontier_names_the_foreign_merge(acceptance_cases, log):
    case = acceptance_cases["cross-repo-merge"]
    wss = seed(case)
    recent = list(sessions.iter_sessions(log=log))
    fr = frontier.build(
        wss["origin-readiness"], log=log, gh_runner=gh_runner_for(case), recent=recent
    )
    le = case["loose_end"]
    hits = [x for x in fr.loose_ends if x.startswith(le["ref"]) and le["by"][:8] in x]
    assert hits, fr.loose_ends
    # The handover message resolved to the owner by body match.
    assert any(m.to_sid == le["owner"] and m.how == "body" for m in fr.messages), fr.messages


def test_retire_records_a_capture_and_clears_dropped(acceptance_cases, log, tmp_path, monkeypatch):
    case = acceptance_cases["duplicate-bug"]
    wss = seed(case)
    monkeypatch.setenv("WORKSTREAM_DOCS_ROOT", str(tmp_path / "docs"))
    ws = wss["default-track"]
    fr = frontier.build(ws, log=log, gh_runner=gh_runner_for(case))
    assert all(s.dropped for s in fr.sessions)
    path = frontier.capture(fr)
    assert path.is_file() and "workstreams/default-track/captures" in str(path)
    data = json.loads(path.read_text())
    assert {s["id"] for s in data["sessions"]} == {s.facts.id for s in fr.sessions}
    again = frontier.build(store.get("default-track"), log=log, gh_runner=gh_runner_for(case))
    assert not any(s.dropped for s in again.sessions)
    assert again.captures == [str(path)]
