"""codex-review-scan against a synthetic rollout store.

The module filename has a hyphen, so it can't be imported by name — load it from
its path with importlib. It goes into sys.modules before it runs because
@dataclass resolves the module's lazy annotations through it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "codex_review_scan", Path(__file__).resolve().parent / "codex-review-scan.py"
)
crs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = crs
_spec.loader.exec_module(crs)

PROMPT_TEXT = (
    "# Personality\nBe brief.\n# Autonomy and persistence\n- review: evidence-backed response\n"
)


def _rollout(root: Path, day: str, uid: str, model: str, first: str, inputs, final="Done."):
    d = root / day.replace("-", "/")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-{day}T10-00-00-{uid}.jsonl"
    recs = [
        {
            "type": "session_meta",
            "payload": {
                "id": uid,
                "cli_version": "0.144.4",
                "base_instructions": {"text": PROMPT_TEXT, "provenance": {"type": "model"}},
            },
        },
        {"type": "turn_context", "payload": {"model": model}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>x</environment_context>"}
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": first}],
            },
        },
    ]
    for inp in inputs:
        # a (name, arguments) pair is the direct function_call encoding; a bare
        # string is the unified tools.exec_command({cmd:"..."}) spelling
        if isinstance(inp, tuple):
            name, arguments = inp
            recs.append(
                {
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": name, "arguments": arguments},
                }
            )
        else:
            recs.append(
                {
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call", "name": "exec", "input": inp},
                }
            )
    recs.append(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": final}],
            },
        }
    )
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return p


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path))
    _rollout(
        tmp_path,
        "2026-09-05",
        "01a0aaaa-0000-7000-8000-000000000001",
        "gpt-5.6-sol",
        "please review https://github.com/o/r/pull/97",
        [
            'const r = await tools.exec_command({cmd:"gh pr view 97 --repo o/r --json headRefOid"});',
            'const r = await tools.exec_command({cmd:"git fetch origin pull/97/head:pr-97"});',
            'const r = await tools.exec_command({cmd:"go test ./... -count=1"});',
            'const r = await tools.exec_command({cmd:"gh pr review 97 --approve --body \\"Reviewed abc. No findings.\\""});',
        ],
        final="Reviewed abc. No findings.",
    )
    _rollout(
        tmp_path,
        "2026-09-06",
        "01a0bbbb-0000-7000-8000-000000000002",
        "codex-auto-review",
        "The following is the Codex agent history whose request action you are assessing. review PR",
        ['const r = await tools.exec_command({cmd:"true"});'],
    )
    _rollout(
        tmp_path,
        "2026-09-07",
        "01a0cccc-0000-7000-8000-000000000003",
        "gpt-5.6-sol",
        "please fix the flaky test",
        ['const r = await tools.exec_command({cmd:"pytest -x"});'],
    )
    _rollout(
        tmp_path,
        "2026-08-01",
        "01a0dddd-0000-7000-8000-000000000004",
        "gpt-5.5",
        "review PR 12 please",
        [],  # no tool calls: dropped
    )
    return tmp_path


def test_selects_review_asks_and_drops_side_sessions(store):
    rows = crs.select_reviews(store)
    assert [r.id for r in rows] == ["01a0aaaa"]
    assert len(crs.select_reviews(store, everything=True)) == 2  # + the fix session
    assert crs.select_reviews(store, since="2026-09-06") == []


def test_counts_and_commands(store):
    r = crs.select_reviews(store)[0]
    assert r.counts() == {"tests": 1, "fetch": 1, "gh_pr": 1, "web": 0, "posted": 1}
    assert r.commands[0] == "gh pr view 97 --repo o/r --json headRefOid"
    assert r.commands[-1] == 'gh pr review 97 --approve --body "Reviewed abc. No findings."'
    assert r.date == "2026-09-05" and r.model == "gpt-5.6-sol" and r.cli_version == "0.144.4"
    assert r.final == "Reviewed abc. No findings."


def test_summary(store):
    s = crs.summarize(crs.select_reviews(store, everything=True))
    assert s["sessions"] == 2 and s["with"]["tests"] == 2 and s["with"]["posted"] == 1
    assert s["models"] == {"gpt-5.6-sol": 2}
    assert (s["first"], s["last"]) == ("2026-09-05", "2026-09-07")


def test_prompt_and_trail_modes(store, capsys):
    crs.main(["prompt", "01a0aaaa"])
    out = capsys.readouterr().out
    assert "# Personality" in out and "# Autonomy and persistence" in out
    crs.main(["trail", "01a0aaaa"])
    out = capsys.readouterr().out
    assert "$ go test ./... -count=1" in out and "Reviewed abc. No findings." in out
    crs.main(["reviews"])
    out = capsys.readouterr().out
    assert out.startswith("1 PR-review sessions with tool calls, 2026-09-05 → 2026-09-05")
    assert "tfg.p" in out  # flags: tests, fetch, gh_pr, no web, posted


def test_direct_shell_calls_are_read(store):
    """A function_call named exec_command carries its command in JSON arguments.

    Only the unified `cmd:"..."` spelling was parsed, so a third of a real store
    produced an empty trail.
    """
    _rollout(
        store,
        "2026-09-08",
        "01a0eeee-0000-7000-8000-000000000005",
        "gpt-5.6-sol",
        "review PR 5 in o/r",
        [
            ("exec_command", json.dumps({"cmd": "git fetch origin pull/5/head", "workdir": "/x"})),
            ("shell_command", json.dumps({"cmd": "pytest -q"})),
            ("shell", json.dumps({"command": ["gh", "pr", "diff", "5"]})),
            ("update_plan", json.dumps({"plan": "not a shell call"})),
        ],
    )
    r = [x for x in crs.select_reviews(store) if x.id == "01a0eeee"][0]
    assert r.commands == ["git fetch origin pull/5/head", "pytest -q", "gh pr diff 5"]
    assert r.counts()["tests"] == 1 and r.counts()["fetch"] == 1


def test_posted_separates_reads_from_writes(store):
    """Reading a comments endpoint is not posting; a named posting tool is.

    The old pattern matched the endpoint path alone, so `gh api .../comments`
    (a GET) counted as a post, while `_add_review_to_pr` did not.
    """
    _rollout(
        store,
        "2026-09-09",
        "01a0ffff-0000-7000-8000-000000000006",
        "gpt-5.6-sol",
        "review PR 6 in o/r",
        [("exec_command", json.dumps({"cmd": "gh api repos/o/r/pulls/6/comments --paginate"}))],
    )
    reader = [x for x in crs.select_reviews(store) if x.id == "01a0ffff"][0]
    assert reader.counts()["posted"] == 0

    _rollout(
        store,
        "2026-09-09",
        "01a01111-0000-7000-8000-000000000007",
        "gpt-5.6-sol",
        "review PR 7 in o/r",
        [
            ("_fetch_pr_comments", json.dumps({"pr": 7})),
            ("_add_review_to_pr", json.dumps({"pr": 7, "body": "looks good"})),
        ],
    )
    writer = [x for x in crs.select_reviews(store) if x.id == "01a01111"][0]
    assert writer.counts()["posted"] == 1

    _rollout(
        store,
        "2026-09-09",
        "01a02222-0000-7000-8000-000000000008",
        "gpt-5.6-sol",
        "review PR 8 in o/r",
        [
            (
                "exec_command",
                json.dumps({"cmd": "gh api repos/o/r/pulls/8/reviews --method POST -f body=x"}),
            )
        ],
    )
    poster = [x for x in crs.select_reviews(store) if x.id == "01a02222"][0]
    assert poster.counts()["posted"] == 1


def test_prompt_on_empty_store_exits_cleanly(tmp_path, monkeypatch):
    """`prompt` with no reference used max() on an empty iterator: ValueError."""
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        crs.main(["prompt"])
    assert "no rollouts" in str(e.value)


def test_n_zero_prints_no_rows(store, capsys):
    """`rows[-0:]` is `rows[:]`, so -n 0 printed every row instead of none."""
    crs.main(["reviews", "--all", "-n", "0"])
    out = capsys.readouterr().out
    assert "sessions with tool calls" in out  # summary still printed
    assert "01a0aaaa" not in out


def test_malformed_since_is_rejected(store):
    with pytest.raises(SystemExit):
        crs.main(["reviews", "--since", "09/05/2026"])


def test_ambiguous_prefix_exits(store):
    with pytest.raises(SystemExit):
        crs.resolve("01a0", store)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
