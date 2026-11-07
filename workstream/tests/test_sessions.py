"""sessions.py against a synthetic store: invented ids, repos and paths only."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from workstream import sessions

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)

S_MAIN = "11111111-aaaa-4bbb-8ccc-000000000001"
S_PASTED = "22222222-aaaa-4bbb-8ccc-000000000002"
S_FORK_A = "33333333-aaaa-4bbb-8ccc-000000000003"
S_FORK_B = "44444444-aaaa-4bbb-8ccc-000000000004"
S_LATE = "55555555-aaaa-4bbb-8ccc-000000000005"
S_ROOT = "66666666-aaaa-4bbb-8ccc-000000000006"
S_CLEARED = "77777777-aaaa-4bbb-8ccc-000000000007"
S_AMBIG_1 = "abcdef01-1111-4bbb-8ccc-000000000008"
S_AMBIG_2 = "abcdef01-2222-4bbb-8ccc-000000000009"
AGENT = "a1b2c3d4e5f60718"
CODEX = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeee0001"
CODEX_CHILD = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeee0002"


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


class Rec:
    """Claude-shaped records for one session."""

    def __init__(self, sid: str, cwd: str, origin: str | None = None):
        self.sid, self.cwd, self.origin = sid, cwd, origin

    def _base(self, kind: str, t: float, **extra) -> dict:
        r = {"type": kind, "timestamp": ts(t), "sessionId": self.sid, "cwd": self.cwd}
        if self.origin:
            r["session_id"] = self.origin
        r.update(extra)
        return r

    def user(self, t: float, content, **extra) -> dict:
        return self._base("user", t, message={"role": "user", "content": content}, **extra)

    def assistant(self, t: float, blocks: list[dict], usage: dict | None = None) -> dict:
        msg = {"role": "assistant", "model": "claude-test-1", "content": blocks}
        if usage:
            msg["usage"] = usage
        return self._base("assistant", t, message=msg)

    def tool(self, t: float, tool_name: str, tool_id: str, **inp) -> dict:
        return self.assistant(
            t, [{"type": "tool_use", "id": tool_id, "name": tool_name, "input": inp}]
        )

    def result(self, t: float, tool_id: str, text: str) -> dict:
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
        return self.user(t, [block])


@pytest.fixture
def store(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    codex = tmp_path / "codex"
    projects.mkdir()
    codex.mkdir()
    monkeypatch.setenv("WORKSTREAM_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(codex))

    alpha = tmp_path / "repos" / "alpha"
    beta = tmp_path / "repos" / "beta"
    for r in (alpha, beta):
        (r / ".git").mkdir(parents=True)
        (r / "src").mkdir()
    wt = tmp_path / "wt" / "alpha-feat"
    wt.mkdir(parents=True)
    (alpha / ".git" / "worktrees" / "alpha-feat").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {alpha}/.git/worktrees/alpha-feat\n")
    launch = tmp_path / "launch"
    launch.mkdir()

    proj = projects / "-tmp-launch"
    m = Rec(S_MAIN, str(launch))
    write_jsonl(
        proj / f"{S_MAIN}.jsonl",
        [
            m.user(0, "<command-name>/clear</command-name>"),
            m.user(1, f"pick up from {S_PASTED[:8]}; the sha deadbeef99 is unrelated"),
            m.user(2, "<system-reminder>injected</system-reminder>", isMeta=True),
            m.assistant(
                3,
                [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": f"git -C {beta} status"},
                    }
                ],
                usage={
                    "input_tokens": 10,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 50,
                    "output_tokens": 7,
                },
            ),
            m.result(4, "t1", "clean"),
            m.assistant(
                5,
                [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Edit",
                        "input": {"file_path": f"{alpha}/src/x.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t3",
                        "name": "Write",
                        "input": {"file_path": f"{alpha}/docs/handoff-notes.md"},
                    },
                ],
                usage={
                    "input_tokens": 5,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": 9,
                },
            ),
            m.tool(6, "EnterWorktree", "t4", name="feat"),
            m.result(7, "t4", f"Created worktree at “{wt}” on branch feat"),
            m.tool(8, "Bash", "t5", command="gh pr view 3 --repo acme/widgets"),
            m.tool(9, "Bash", "t6", command=f"cd {wt} && git status"),
            m.user(10, f"now check {S_PASTED}"),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": ts(11),
                "sessionId": S_MAIN,
            },
            m.assistant(12, [{"type": "text", "text": "All done."}], usage={"input_tokens": 3}),
        ],
    )
    p = Rec(S_PASTED, str(launch))
    write_jsonl(proj / f"{S_PASTED}.jsonl", [p.user(-3600, "earlier work")])

    sub_dir = proj / S_MAIN / "subagents"
    sa = Rec(S_MAIN, str(launch))
    write_jsonl(
        sub_dir / f"agent-{AGENT}.jsonl",
        [
            sa.user(4, "review the diff", isSidechain=True),
            sa.assistant(5, [{"type": "text", "text": "Looks fine."}]),
        ],
    )
    (sub_dir / f"agent-{AGENT}.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "review", "name": "code-review"})
    )

    for sid, start in ((S_FORK_A, 100), (S_FORK_B, 130), (S_LATE, 900)):
        f = Rec(sid, str(launch))
        write_jsonl(
            proj / f"{sid}.jsonl",
            [
                f.user(start, "build the widget"),
                f.assistant(start + 1, [{"type": "text", "text": "ok"}]),
            ],
        )

    root = Rec(S_ROOT, str(launch))
    write_jsonl(proj / f"{S_ROOT}.jsonl", [root.user(2000, "first task")])
    cleared = Rec(S_CLEARED, str(launch), origin=S_ROOT)
    write_jsonl(proj / f"{S_CLEARED}.jsonl", [cleared.user(2100, "second task")])

    other = projects / "-tmp-other"
    for sid in (S_AMBIG_1, S_AMBIG_2):
        write_jsonl(other / f"{sid}.jsonl", [Rec(sid, str(launch)).user(3000, "hello")])

    day = codex / "2026" / "10" / "29"
    write_jsonl(
        day / f"rollout-2026-10-29T12-00-00-{CODEX}.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": ts(500),
                "payload": {"id": CODEX, "cwd": str(beta), "timestamp": ts(500)},
            },
            {
                "type": "turn_context",
                "timestamp": ts(501),
                "payload": {"cwd": str(beta), "model": "gpt-test"},
            },
            {
                "type": "event_msg",
                "timestamp": ts(502),
                "payload": {"type": "user_message", "message": f"continue {S_MAIN[:8]}"},
            },
            {
                "type": "response_item",
                "timestamp": ts(503),
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "c1",
                    "arguments": json.dumps({"command": ["bash", "-lc", f"git -C {alpha} log -1"]}),
                },
            },
            {
                "type": "response_item",
                "timestamp": ts(504),
                "payload": {"type": "function_call_output", "call_id": "c1", "output": "commit 0"},
            },
            {
                "type": "event_msg",
                "timestamp": ts(505),
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 5000}},
                },
            },
            {
                "type": "event_msg",
                "timestamp": ts(506),
                "payload": {"type": "agent_message", "message": "done"},
            },
        ],
    )
    write_jsonl(
        day / f"rollout-2026-10-29T12-05-00-{CODEX_CHILD}.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": ts(600),
                "payload": {
                    "id": CODEX_CHILD,
                    "cwd": str(beta),
                    "parent_thread_id": CODEX,
                    "thread_source": "guardian_review",
                },
            },
            {
                "type": "event_msg",
                "timestamp": ts(601),
                "payload": {"type": "user_message", "message": "review"},
            },
        ],
    )
    return {"alpha": str(alpha), "beta": str(beta), "wt": str(wt), "proj": proj}


def test_main_session_facts(store):
    sf = sessions.facts(S_MAIN[:8])
    assert sf.id == S_MAIN and sf.kind == "claude" and sf.complete
    assert sf.ref == f"claude:{S_MAIN}"
    assert sf.peak_context == 2015  # max per turn, not the 3078 cumulative sum
    assert sf.compaction_count == 1
    # A boundary with no summary after it still starts the post-compaction context.
    assert sf.context_after_compaction == 3
    assert sf.model == "claude-test-1"
    assert [t for _, t in sf.human_prompts] == [
        f"pick up from {S_PASTED[:8]}; the sha deadbeef99 is unrelated",
        f"now check {S_PASTED}",
    ]
    # deadbeef99 resolves to nothing; S_PASTED is pasted twice but listed once.
    assert [s for s, _ in sf.pasted_sessions] == [S_PASTED]
    assert sf.pasted_sessions[0][1].utc == T0 + timedelta(seconds=1)
    # beta from `git -C`, alpha from edits and from `cd <worktree>` (canonicalized).
    assert set(sf.repos) == {store["alpha"], store["beta"]}
    assert sf.gh_repos == ["acme/widgets"]
    assert sf.worktrees == [store["wt"]]
    assert sf.handoff_files == [f"{store['alpha']}/docs/handoff-notes.md"]
    assert sf.last_assistant[1] == "All done."
    names, summaries = zip(*[(n, summ) for _, n, summ in sf.last_tools])
    assert names[-2:] == ("Bash", "Bash")
    assert summaries[-2] == "gh pr view 3 --repo acme/widgets"
    assert summaries[-1].startswith("cd ")  # one line, truncated for a long tmp path
    assert sf.subagents == [AGENT]
    assert sf.work is not None and f"{store['alpha']}/src/x.py" in sf.work.edited_paths
    assert sf.started.utc == T0 and sf.last_seen.utc == T0 + timedelta(seconds=12)


def test_subagent(store):
    sf = sessions.facts(AGENT)
    assert sf.kind == "subagent"
    assert sf.parent_id == S_MAIN
    assert sf.ref == f"agent:{S_MAIN}/{AGENT}"
    assert sf.agent_meta["name"] == "code-review"
    # Every subagent record is a sidechain; the delegation prompt still counts.
    assert [t for _, t in sf.human_prompts] == ["review the diff"]
    assert sessions.resolve(f"agent-{AGENT}") == AGENT


def test_fork_siblings(store):
    assert sessions.facts(S_FORK_A).fork_siblings == [S_FORK_B]
    assert sessions.facts(S_FORK_B).fork_siblings == [S_FORK_A]
    assert sessions.facts(S_LATE).fork_siblings == []


def test_clear_chain(store):
    sf = sessions.facts(S_CLEARED)
    assert sf.origin == S_ROOT
    assert sf.chain == [S_ROOT, S_CLEARED]
    assert sessions.facts(S_ROOT).origin is None


def test_codex_rollout(store):
    sf = sessions.facts(CODEX)
    assert sf.kind == "codex" and sf.ref == f"codex:{CODEX}"
    assert sf.parent_id is None
    assert sf.cwd == store["beta"]
    assert sf.model == "gpt-test"
    assert sf.peak_context == 5000
    assert sf.repos == [store["alpha"]]  # where git acted, not the launch cwd
    assert [s for s, _ in sf.pasted_sessions] == [S_MAIN]
    assert sf.last_assistant[1] == "done"
    assert sf.last_tools[0][1] == "shell"
    assert sf.last_tools[0][2].startswith("bash -lc git -C ")

    child = sessions.facts(CODEX_CHILD)
    assert child.kind == "codex" and child.parent_id == CODEX


def test_resolve(store):
    with pytest.raises(sessions.Ambiguous) as exc:
        sessions.resolve("abcdef01")
    assert set(exc.value.matches) == {S_AMBIG_1, S_AMBIG_2}
    assert sessions.resolve("abcdef01-1") == S_AMBIG_1
    assert sessions.resolve(f"claude:{S_MAIN}") == S_MAIN
    with pytest.raises(sessions.NotFound):
        sessions.resolve("ffffffff")


def test_snapshot_lists_the_store_once(store, monkeypatch):
    calls = []
    real = sessions._claude_refs
    monkeypatch.setattr(sessions, "_claude_refs", lambda: calls.append(1) or real())
    sessions.known_session_ids()
    sessions.known_session_ids()
    assert len(calls) == 2  # no snapshot: the store as it is now, every time
    monkeypatch.setattr(sessions, "_snapshot", {})
    sessions.known_session_ids()
    sessions.resolve(S_MAIN)
    assert len(calls) == 3


def test_known_session_ids(store):
    ids = sessions.known_session_ids()
    assert {S_MAIN, AGENT, CODEX, CODEX_CHILD} <= ids
    assert CODEX not in sessions.known_session_ids(include_codex=False)


def test_iter_sessions_cheap_pass(store):
    by_id = {sf.id: sf for sf in sessions.iter_sessions()}
    assert {S_MAIN, AGENT, CODEX, CODEX_CHILD, S_CLEARED} <= set(by_id)
    main = by_id[S_MAIN]
    assert not main.complete and main.repos == [] and main.pasted_sessions == []
    assert main.peak_context == 2015 and main.last_assistant[1] == "All done."
    assert by_id[S_FORK_A].fork_siblings == [S_FORK_B]
    assert by_id[S_CLEARED].chain == [S_ROOT, S_CLEARED]
    assert by_id[AGENT].parent_id == S_MAIN
    assert by_id[CODEX_CHILD].parent_id == CODEX


def test_iter_sessions_since_and_codex_toggle(store):
    old = (T0 - timedelta(days=30)).timestamp()
    os.utime(store["proj"] / f"{S_LATE}.jsonl", (old, old))
    from workstream.clock import Instant

    since = Instant(T0 - timedelta(days=1), timedelta(0))
    ids = {sf.id for sf in sessions.iter_sessions(since=since, include_codex=False)}
    assert S_LATE not in ids and S_MAIN in ids
    assert CODEX not in ids


def test_worktrees_from_bash(store, tmp_path):
    sid = "88888888-aaaa-4bbb-8ccc-00000000000a"
    r = Rec(sid, store["alpha"])
    made = tmp_path / "wt" / "alpha-fix-thing"
    write_jsonl(
        store["proj"] / f"{sid}.jsonl",
        [
            r.user(0, "make worktrees"),
            r.tool(1, "Bash", "b1", command="git worktree add -b topic ../alpha-topic origin/main"),
            r.tool(2, "Bash", "b2", command="just wt fix/thing"),
            r.result(3, "b2", f"Preparing worktree\n{made}\n"),
            r.tool(4, "Bash", "b3", command="just wt quiet-one"),
            r.result(5, "b3", "error: no output path"),
        ],
    )
    sf = sessions.facts(sid)
    assert sf.worktrees == [
        os.path.normpath(os.path.join(store["alpha"], "../alpha-topic")),
        str(made),
        "quiet-one",  # no path printed: the name is all there is
    ]


def test_context_after_compaction_on_a_synthetic_transcript(synthetic_store):
    """Boundary plus summary is one compaction, and the resume cost is the peak
    after the last one, not the whole transcript's."""
    start = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
    cwd = synthetic_store.root / "scratch"
    spec = synthetic_store.session(
        "compacted",
        cwd,
        prompts=[f"step {i}" for i in range(12)],
        start=start,
        usage_peak=320_000,
        compactions=2,
        post_compact_peak=41_000,
    )
    sf = sessions.facts(spec.id)
    assert sf.compaction_count == 2
    assert sf.peak_context == 320_000
    assert sf.context_after_compaction == 41_000

    plain = synthetic_store.session(
        "never-compacted", cwd, prompts=["one", "two"], start=start, usage_peak=70_000
    )
    sf = sessions.facts(plain.id)
    assert (sf.compaction_count, sf.context_after_compaction, sf.peak_context) == (
        0,
        70_000,
        70_000,
    )


def _compacted(r: Rec, boundary_meta: dict | None, summary: bool = True) -> list[dict]:
    usage = {"input_tokens": 3, "cache_read_input_tokens": 200_000}
    recs = [r.user(0, "start"), r.assistant(1, [{"type": "text", "text": "ok"}], usage=usage)]
    if boundary_meta is not None:
        recs.append(
            {"type": "system", "subtype": "compact_boundary", "timestamp": ts(2), **boundary_meta}
        )
    if summary:
        recs.append(r.user(3, "summary of the earlier portion", isCompactSummary=True))
    return recs


def test_bare_compaction_falls_back_to_post_tokens():
    """No assistant turn after the compaction: the boundary's postTokens is the
    context the compaction left, and the only size there is."""
    r = Rec(S_MAIN, "/w")
    meta = {"compactMetadata": {"trigger": "auto", "preTokens": 200_003, "postTokens": 17_500}}
    scan = sessions._scan_claude(_compacted(r, meta), is_subagent=False)
    assert (scan.compaction_count, scan.context_after_compaction) == (1, 17_500)
    assert scan.peak_context == 200_003


@pytest.mark.parametrize("boundary", [{}, None], ids=["boundary-without-size", "summary-only"])
def test_bare_compaction_without_a_size_is_unknown(boundary):
    r = Rec(S_MAIN, "/w")
    scan = sessions._scan_claude(_compacted(r, boundary), is_subagent=False)
    assert (scan.compaction_count, scan.context_after_compaction) == (1, None)


@pytest.mark.parametrize(
    "raw,base",
    [
        ("$WORKSTREAM_TEST_UNSET_VAR/wt/x", "/w"),
        ("wt/x`", "/w"),
        ("wt/x", "/w/~/repo"),
    ],
    ids=["unexpanded-var", "trailing-backtick", "tilde-mid-path-in-base"],
)
def test_abs_path_rejects_paths_the_shell_would_have_rewritten(raw, base):
    assert sessions._abs_path(raw, base) is None


def test_abs_path_keeps_a_leading_tilde():
    assert sessions._abs_path("~/repo/wt/x", "/w") == os.path.expanduser("~/repo/wt/x")
