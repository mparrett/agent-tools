import json

from claude_graph.subagents import find_subagent_sessions, subagent_dir


def _write_agent(sub_dir, agent_id, tid, agent_type, desc, transcript=True):
    (sub_dir / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"toolUseId": tid, "agentType": agent_type, "description": desc})
    )
    if transcript:
        (sub_dir / f"agent-{agent_id}.jsonl").write_text('{"type":"assistant"}\n')


def test_maps_by_tooluseid_and_strips_description(tmp_path):
    session = tmp_path / "sess.jsonl"
    session.write_text("{}\n")
    sub = subagent_dir(session)
    sub.mkdir(parents=True)
    _write_agent(sub, "a1", "toolu_1", "Explore", "  audit X  ")
    _write_agent(sub, "a2", "toolu_2", "general-purpose", "do Y")
    out = find_subagent_sessions(session)
    assert set(out) == {"toolu_1", "toolu_2"}
    assert out["toolu_1"]["agentType"] == "Explore"
    assert out["toolu_1"]["description"] == "audit X"  # stripped


def test_empty_when_no_subagent_dir(tmp_path):
    session = tmp_path / "sess.jsonl"
    session.write_text("{}\n")
    assert find_subagent_sessions(session) == {}


def test_skips_malformed_sidecar_with_warning(tmp_path, capsys):
    session = tmp_path / "sess.jsonl"
    session.write_text("{}\n")
    sub = subagent_dir(session)
    sub.mkdir(parents=True)
    _write_agent(sub, "good", "toolu_ok", "Explore", "ok")
    (sub / "agent-bad.meta.json").write_text("{ not json")
    (sub / "agent-bad.jsonl").write_text("{}\n")
    out = find_subagent_sessions(session)
    assert set(out) == {"toolu_ok"}  # malformed skipped, good one kept
    assert "skipping unreadable sidecar" in capsys.readouterr().err


def test_sidecar_without_transcript_is_dropped(tmp_path):
    session = tmp_path / "sess.jsonl"
    session.write_text("{}\n")
    sub = subagent_dir(session)
    sub.mkdir(parents=True)
    _write_agent(sub, "orphan", "toolu_x", "Explore", "no transcript", transcript=False)
    assert find_subagent_sessions(session) == {}  # requires the matching .jsonl
