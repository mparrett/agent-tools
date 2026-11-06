"""Self-checks: the synthetic store is readable by the real primitives."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from synthetic import SyntheticStore, case_duplicate_bug, slug_for

from workstream import seam


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_slug_matches_claude_ls():
    for p in ("/tmp/x/repos/orchard", "/a/b.c_d/e f"):
        assert slug_for(p) == seam._cl.encode_path_to_folder_name(p)


def test_transcripts_parse_through_seam(acceptance_cases):
    seam_case = acceptance_cases["audio-seam"]
    root = seam_case["sessions"]["root"]
    entries = seam.load_raw_entries(str(root.path))
    meta = seam.extract_session_metadata(entries)
    assert meta.session_id == root.id
    assert meta.cwd == str(root.cwd)
    assert meta.compaction_count == 1
    work = seam.derive_work_signals(entries)
    assert "acme/trellis#57" in work.prs
    assert any("trellis" in r for r in work.repo_edits)
    peak = max(
        sum(
            e["message"]["usage"][k]
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        for e in entries
        if e.get("type") == "assistant"
    )
    assert peak == root.usage_peak


def test_lineage_fork_and_subagent(acceptance_cases):
    s = acceptance_cases["audio-seam"]["sessions"]
    cleared, shepherd = s["shepherd_cleared"], s["shepherd"]
    assert seam.session_origin(cleared.path) == shepherd.id
    assert [m.session_id for m in seam.session_chain(cleared.path)] == [shepherd.id, cleared.id]

    fork, base = s["conventions_fork"], s["conventions"]

    def first_prompt(spec):
        return next(e["message"]["content"] for e in _lines(spec.path) if e["type"] == "user")

    assert first_prompt(fork) == first_prompt(base)

    sub = s["pickup_review"]
    assert sub.path.parent.name == "subagents" and sub.path.parent.parent.name == s["pickup"].id
    meta = json.loads(sub.path.with_name(sub.path.stem + ".meta.json").read_text())
    assert meta["name"] == "code-review" and meta["finished"] is True
    assert all(e["isSidechain"] for e in _lines(sub.path))


def test_list_projects_finds_slug_dirs(acceptance_cases, synthetic_store):
    names = {p.name for p in seam.list_projects(str(seam.projects_dir()))}
    for repo in ("orchard", "trellis", "grove", "kiln", "meadow", "bramble"):
        assert slug_for(synthetic_store.repos[repo]) in names


def test_codex_rollouts_found(acceptance_cases):
    s = acceptance_cases["cross-repo-merge"]["sessions"]
    main, guard = s["codex"], s["codex_guardian"]
    assert seam.file_is_codex(main.path)
    default = {r.path for r in seam.iter_codex_session_refs()}
    assert main.path in default and guard.path not in default
    everything = {r.path for r in seam.iter_codex_session_refs(include_subagents=True)}
    assert guard.path in everything
    assert seam.codex_project_key(main.path) == slug_for(main.cwd)


def test_hooklog_spans_zone_change(acceptance_cases, synthetic_store):
    lines = _lines(synthetic_store.hooklog_path)
    offsets = set()
    for line in lines:
        data = line["hook_data"]
        assert set(data) >= {"session_id", "transcript_path", "cwd", "hook_event_name"}
        if data["hook_event_name"] != "SessionStart" or data.get("source") != "startup":
            continue
        local = datetime.fromisoformat(line["timestamp"])
        assert local.tzinfo is None
        first = _lines(Path(data["transcript_path"]))[0]
        first_utc = seam.parse_ts(first["timestamp"]).replace(tzinfo=None)
        offsets.add(round((local - first_utc) / timedelta(minutes=15)) * timedelta(minutes=15))
    assert {timedelta(hours=-7), timedelta(hours=-5), timedelta(hours=-8)} <= offsets


def test_send_message_reaches_recipient(acceptance_cases, synthetic_store):
    case = acceptance_cases["cross-repo-merge"]
    owner, merger = case["sessions"]["owner"], case["sessions"]["merger"]
    pre = [
        h["hook_data"]
        for h in _lines(synthetic_store.hooklog_path)
        if h["hook_data"].get("tool_name") == "SendMessage"
        and h["hook_data"]["hook_event_name"] == "PreToolUse"
        and h["hook_data"]["session_id"] == merger.id
    ]
    assert len(pre) == 1 and pre[0]["tool_input"]["to"] == "meadow-b2"
    peers = [
        e
        for e in _lines(owner.path)
        if e.get("isMeta") and e.get("origin", {}).get("kind") == "peer"
    ]
    assert len(peers) == 1
    assert peers[0]["origin"]["body"] == pre[0]["tool_input"]["message"]
    assert peers[0]["origin"]["name"] == "bramble-bb"
    reg = json.loads((synthetic_store.sessions_dir / "41101.json").read_text())
    assert reg["sessionId"] == owner.id and reg["name"] == "meadow-b2"


def test_git_shapes(acceptance_cases, synthetic_store):
    dup = acceptance_cases["duplicate-bug"]
    kiln, wt = dup["repos"]["kiln"], dup["worktrees"]["fix-default-track"]
    git = synthetic_store.git
    assert git(kiln, "remote") == ""
    assert git(kiln, "rev-list", "--count", "main..wt/fix-default-track") == "1"
    assert git(wt, "status", "--porcelain") == ""
    own = acceptance_cases["pr-ownership"]
    grove = own["repos"]["grove"]
    assert git(grove, "rev-parse", "wt/fix-main-tests") == git(
        grove, "rev-parse", "origin/wt/fix-main-tests"
    )
    assert git(grove, "status", "--porcelain") == ""


def test_gh_canned_and_loose_ends(acceptance_cases):
    seam_case = acceptance_cases["audio-seam"]
    pr = seam_case["gh"]["acme/trellis#57"]
    assert pr["reviews"][-1]["state"] == "CHANGES_REQUESTED"
    for case in acceptance_cases.values():
        assert case["expected"] in {w["name"] for w in case["workstreams"]}
        assert case["loose_end"]["kind"]
        fresh_prompts = [
            e
            for e in _lines(case["fresh"].path)
            if e["type"] == "user" and isinstance(e["message"]["content"], str)
        ]
        assert len(fresh_prompts) == 1


def test_ids_are_stable(tmp_path):
    a = case_duplicate_bug(SyntheticStore(tmp_path / "a"))
    b = case_duplicate_bug(SyntheticStore(tmp_path / "b"))
    assert a["sessions"]["first"].id == b["sessions"]["first"].id
    assert a["loose_end"]["commit"] == b["loose_end"]["commit"]


def test_no_home_paths(acceptance_cases, synthetic_store):
    root = synthetic_store.root
    if shutil.which("rg"):
        out = subprocess.run(
            ["rg", "--no-ignore", "--hidden", "-c", "-F", "/Users/", str(root)],
            capture_output=True,
            text=True,
        )
        assert out.stdout == "", out.stdout
    else:
        hits = [p for p in root.rglob("*") if p.is_file() and b"/Users/" in p.read_bytes()]
        assert hits == []
