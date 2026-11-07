import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from workstream import hooklog
from workstream.clock import Instant

H = timedelta(hours=1)
SID_A = "aaaaaaaa-1111-4111-8111-000000000001"
SID_B = "bbbbbbbb-2222-4222-8222-000000000002"
SID_C = "cccccccc-3333-4333-8333-000000000003"


def line(at_local, event, sid, **fields):
    hd = {"session_id": sid, "cwd": "/work/demo", "hook_event_name": event, **fields}
    return json.dumps({"timestamp": at_local.isoformat(), "hook_data": hd}) + "\n"


def zulu(dt_utc):
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Store:
    def __init__(self, root):
        self.logs = root / "logs"
        self.projects = root / "projects"
        self.logs.mkdir()
        (self.projects / "-work-demo").mkdir(parents=True)

    def transcript(self, sid, records):
        p = self.projects / "-work-demo" / f"{sid}.jsonl"
        with open(p, "a") as fh:
            for r in records:
                # Compact, as Claude Code writes them; the peer scan relies on it.
                fh.write(json.dumps({"sessionId": sid, **r}, separators=(",", ":")) + "\n")
        return p

    def write(self, name, lines, gz=False):
        p = self.logs / name
        data = "".join(lines)
        if gz:
            with gzip.open(p, "wt") as fh:
                fh.write(data)
        else:
            p.write_text(data)
        return p

    def start(self, sid, at_local, offset, idle=timedelta(seconds=1), source="startup"):
        """A fresh session: hook lines plus a transcript whose first record is
        the first prompt, `idle` after SessionStart."""
        prompt_local = at_local + idle
        path = self.transcript(
            sid,
            [{"type": "user", "timestamp": zulu(prompt_local - offset), "message": {}}],
        )
        return [
            line(at_local, "SessionStart", sid, source=source, transcript_path=str(path)),
            line(prompt_local, "UserPromptSubmit", sid, prompt="hi", transcript_path=str(path)),
        ]

    def load(self, **kw):
        return hooklog.load(log_dir=self.logs, projects_dir=self.projects, **kw)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_reads_live_plain_and_gz_archives_and_infers_offsets(store):
    # Oldest archive at UTC-7, then UTC-5, then the live log at UTC-8.
    store.write(
        "hook_events.log.2.gz", store.start(SID_A, datetime(2026, 10, 19, 9, 0), -7 * H), gz=True
    )
    store.write(
        "hook_events.log.1",
        store.start(SID_B, datetime(2026, 10, 25, 9, 0), -5 * H, idle=timedelta(minutes=40)),
    )
    store.write("hook_events.log", store.start(SID_C, datetime(2026, 11, 2, 9, 0), -8 * H))
    log = store.load()
    assert [e.session_id for e in log.events if e.event == "SessionStart"] == [SID_A, SID_B, SID_C]
    assert [o for _, o in log.timeline.samples] == [-7 * H, -5 * H, -8 * H]
    b_start = next(e for e in log.events if e.session_id == SID_B and e.event == "SessionStart")
    assert b_start.at.utc == datetime(2026, 10, 25, 14, 0, tzinfo=timezone.utc)
    assert b_start.at.offset == -5 * H


def test_since_stops_at_a_file_wholly_before_the_cut(store):
    # Out of order on purpose: if .2.gz were read, SID_C would show up.
    store.write("hook_events.log.2.gz", [line(datetime(2026, 11, 1, 9, 0), "Stop", SID_C)], gz=True)
    store.write("hook_events.log.1", [line(datetime(2026, 10, 20, 9, 0), "Stop", SID_A)])
    store.write(
        "hook_events.log",
        [
            line(datetime(2026, 10, 30, 9, 0), "Stop", SID_A),
            line(datetime(2026, 11, 1, 9, 0), "Stop", SID_B),
        ],
    )
    events = hooklog.read_events(since=datetime(2026, 10, 31), log_dir=store.logs)
    assert [e.session_id for e in events] == [SID_B]
    assert [p.name for p in hooklog.log_files(store.logs)] == [
        "hook_events.log",
        "hook_events.log.1",
        "hook_events.log.2.gz",
    ]
    assert len(hooklog.read_events(log_dir=store.logs)) == 4


def test_since_as_instant_cuts_exactly_after_offsets_are_known(store):
    lines = store.start(SID_A, datetime(2026, 10, 25, 9, 0), -5 * H)
    lines.append(line(datetime(2026, 10, 25, 9, 30), "Stop", SID_A))
    store.write("hook_events.log", lines)
    since = Instant(datetime(2026, 10, 25, 14, 15, tzinfo=timezone.utc), 0 * H)
    log = store.load(since=since)
    assert [e.event for e in log.events] == ["Stop"]


def test_resume_sessions_are_not_paired(store):
    path = store.transcript(SID_A, [{"type": "user", "timestamp": "2026-10-20T16:00:00.000Z"}])
    store.write(
        "hook_events.log",
        [
            line(
                datetime(2026, 10, 20, 12, 0),
                "SessionStart",
                SID_A,
                source="resume",
                transcript_path=str(path),
            )
        ],
    )
    log = store.load()
    assert not log.timeline
    assert any("host zone" in w for w in log.warnings)


def test_missing_log_dir_degrades_to_empty(tmp_path):
    log = hooklog.load(log_dir=tmp_path / "nope", projects_dir=tmp_path, registry=[])
    assert log.events == []
    assert log.messages() == []
    assert log.warnings


def test_lifecycle_and_segments(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += [
        line(t0 + 10 * H / 60, "PreCompact", SID_A, trigger="auto"),
        line(t0 + 11 * H / 60, "SessionStart", SID_A, source="compact"),
        line(t0 + H, "SessionEnd", SID_A, reason="prompt_input_exit"),
        line(t0 + 2 * H, "SessionStart", SID_A, source="resume", model="opus"),
        line(t0 + 2 * H, "SubagentStop", SID_A, agent_type="Explore"),
    ]
    store.write("hook_events.log", lines)
    log = store.load()
    assert [e.event for e in log.lifecycle(SID_A)] == [
        "SessionStart",
        "PreCompact",
        "SessionStart",
        "SessionEnd",
        "SessionStart",
    ]
    last = log.last_lifecycle(SID_A)
    assert last.source == "resume" and last.model == "opus"
    segs = log.segments(SID_A)
    assert [(s.source, s.reason, s.end is None) for s in segs] == [
        ("startup", None, True),
        ("compact", "prompt_input_exit", False),
        ("resume", None, True),
    ]
    assert segs[1].end.utc == datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
    assert log.last_lifecycle(SID_C) is None
    assert any(e.agent_type == "Explore" for e in log.for_session(SID_A))


def send(at_local, sid, to, body, summary="s", tool_use_id=None):
    ti = {"to": to, "summary": summary, "message": body}
    extra = {"tool_use_id": tool_use_id} if tool_use_id else {}
    return [
        line(at_local, "PreToolUse", sid, tool_name="SendMessage", tool_input=ti, **extra),
        line(
            at_local + timedelta(seconds=1),
            "PostToolUse",
            sid,
            tool_name="SendMessage",
            tool_input=ti,
            tool_response={"ok": True},
            **extra,
        ),
    ]


def peer_record(recipient, at_utc, body, name):
    return {
        "type": "user",
        "isMeta": True,
        "timestamp": zulu(at_utc),
        "origin": {
            "kind": "peer",
            "from": "uds:/tmp/cc-socks/4242.sock",
            "verifiedPeerPid": 4242,
            "msg_id": "m-1",
            "name": name,
            "fromMode": "prompting",
            "body": body,
        },
    }


def test_message_resolved_by_body_match(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += send(t0 + H, SID_A, "uds:/tmp/cc-socks/9999.sock", "please rebase", tool_use_id="tu1")
    store.write("hook_events.log", lines)
    sent_utc = datetime(2026, 10, 25, 15, 0, 2)
    store.transcript(SID_C, [peer_record(SID_C, sent_utc, "please rebase", "lead")])
    # A same-body record far outside the window must not win.
    store.transcript(SID_B, [peer_record(SID_B, sent_utc + H, "please rebase", "lead")])
    registry = [{"sessionId": SID_A, "name": "lead", "formerNames": []}]
    msgs = store.load(registry=registry).messages()
    assert len(msgs) == 1
    m = msgs[0]
    assert (m.from_sid, m.to_sid, m.how, m.body) == (SID_A, SID_C, "body", "please rebase")
    assert m.to_raw == "uds:/tmp/cc-socks/9999.sock"


def test_body_match_respects_sender_name(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H) + send(t0 + H, SID_A, "ghost", "ok")
    store.write("hook_events.log", lines)
    store.transcript(
        SID_C, [peer_record(SID_C, datetime(2026, 10, 25, 15, 0), "ok", "someone-else")]
    )
    registry = [{"sessionId": SID_A, "name": "lead"}]
    m = store.load(registry=registry).messages()[0]
    assert m.to_sid is None and m.how is None and m.to_raw == "ghost"


def test_message_resolved_by_registry_name_former_name_and_socket(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += send(t0 + H, SID_A, "reviewer", "a")
    lines += send(t0 + 2 * H, SID_A, "old-name", "b")
    lines += send(t0 + 3 * H, SID_A, "uds:/tmp/cc-socks/77.sock", "c")
    lines += send(t0 + 4 * H, SID_A, "nobody", "d")
    store.write("hook_events.log", lines)
    after_second = datetime(2026, 10, 25, 16, 30, tzinfo=timezone.utc).timestamp() * 1000
    registry = [
        {
            "sessionId": SID_B,
            "name": "reviewer",
            "formerNames": [],
            "messagingSocketPath": "/tmp/cc-socks/77.sock",
        },
        {
            "sessionId": SID_C,
            "name": "renamed",
            "formerNames": [{"name": "old-name", "until": after_second}],
        },
    ]
    msgs = store.load(registry=registry).messages()
    assert [(m.body, m.to_sid, m.how) for m in msgs] == [
        ("a", SID_B, "registry-name"),
        ("b", SID_C, "registry-former-name"),
        ("c", SID_B, "registry-socket"),
        ("d", None, None),
    ]


def test_former_name_expired_before_send_does_not_resolve(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    store.write(
        "hook_events.log", store.start(SID_A, t0, -5 * H) + send(t0 + H, SID_A, "old-name", "x")
    )
    long_ago = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000
    registry = [
        {
            "sessionId": SID_C,
            "name": "new",
            "formerNames": [{"name": "old-name", "until": long_ago}],
        }
    ]
    assert store.load(registry=registry).messages()[0].to_sid is None


SID_D = "dddddddd-4444-4444-8444-000000000004"


def _outsider_mix(store):
    """SID_A is the only member. Every resolution path, from both sides."""
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += send(t0 + H, SID_A, "uds:/tmp/cc-socks/1.sock", "member to outsider")
    lines += send(t0 + 2 * H, SID_B, "uds:/tmp/cc-socks/2.sock", "outsider to member")
    lines += send(t0 + 3 * H, SID_B, "uds:/tmp/cc-socks/3.sock", "outsider to outsider")
    lines += send(t0 + 4 * H, SID_B, "lead", "outsider to member by name")
    lines += send(t0 + 5 * H, SID_C, "a0123456789abcdef", "outsider to own subagent")
    store.write("hook_events.log", lines)
    # t0 in UTC, plus 30s: peer records on exact quarter hours would compete
    # with the first prompt when the log infers the session's offset.
    utc = datetime(2026, 10, 25, 14, 0, 30)
    store.transcript(SID_C, [peer_record(SID_C, utc + H, "member to outsider", "lead")])
    store.transcript(SID_A, [peer_record(SID_A, utc + 2 * H, "outsider to member", "b")])
    store.transcript(SID_D, [peer_record(SID_D, utc + 3 * H, "outsider to outsider", "b")])
    return [
        {"sessionId": SID_A, "name": "lead"},
        {"sessionId": SID_B, "name": "b"},
    ]


def test_messages_involving_matches_the_unfiltered_answer(store):
    registry = _outsider_mix(store)

    def rows(msgs):
        return [(m.body, m.from_sid, m.to_sid, m.how) for m in msgs]

    every = store.load(registry=registry).messages()
    want = [m for m in every if SID_A in (m.from_sid, m.to_sid)]
    got = store.load(registry=registry).messages(involving={SID_A})
    assert rows(got) == rows(want)
    assert [m.body for m in got] == [
        "member to outsider",
        "outsider to member",
        "outsider to member by name",
    ]


def test_one_member_record_answers_one_outsider_call(store):
    # The same body to the member and then to an outsider: searching only the
    # member's transcript, both calls match its one record. Only the nearer
    # call reached the member.
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += send(t0 + H, SID_B, "uds:/tmp/cc-socks/2.sock", "re-pull")
    lines += send(t0 + H + timedelta(seconds=40), SID_B, "uds:/tmp/cc-socks/4.sock", "re-pull")
    store.write("hook_events.log", lines)
    utc = datetime(2026, 10, 25, 15, 0, 5)  # 5s after the first call, in UTC
    store.transcript(SID_A, [peer_record(SID_A, utc, "re-pull", "b")])
    store.transcript(SID_D, [peer_record(SID_D, utc + timedelta(seconds=40), "re-pull", "b")])
    registry = [{"sessionId": SID_B, "name": "b"}]
    got = store.load(registry=registry).messages(involving={SID_A})
    assert [(m.to_raw, m.to_sid) for m in got] == [("uds:/tmp/cc-socks/2.sock", SID_A)]


def test_messages_involving_reads_only_member_transcripts_for_outsider_calls(store, monkeypatch):
    # The point of `involving`: a call between two outsiders must not cost a
    # scan of every transcript in the store.
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += send(t0 + H, SID_B, "uds:/tmp/cc-socks/3.sock", "outsider to outsider")
    store.write("hook_events.log", lines)
    store.transcript(SID_D, [peer_record(SID_D, datetime(2026, 10, 25, 15, 0), "x", "b")])
    log = store.load(registry=[])
    scanned = []
    real = log._scan_peers
    monkeypatch.setattr(log, "_scan_peers", lambda p: scanned.append(p.stem) or real(p))
    assert log.messages(involving={SID_A}) == []
    assert scanned == [SID_A]


def test_messages_since_and_post_only_calls(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += send(t0 + H, SID_A, "x", "early")
    # A Post with no Pre (log rotated between them) still counts once.
    lines += send(t0 + 2 * H, SID_A, "x", "late")[1:]
    store.write("hook_events.log", lines)
    log = store.load(registry=[])
    assert [m.body for m in log.messages()] == ["early", "late"]
    since = Instant(datetime(2026, 10, 25, 15, 30, tzinfo=timezone.utc), -5 * H)
    assert [m.body for m in log.messages(since)] == ["late"]


def test_list_agents_calls(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines += [
        line(t0 + H, "PreToolUse", SID_A, tool_name="ListAgents", tool_input={}),
        line(
            t0 + H, "PostToolUse", SID_A, tool_name="ListAgents", tool_input={}, tool_response="[]"
        ),
    ]
    store.write("hook_events.log", lines)
    calls = store.load().list_agents_calls()
    assert len(calls) == 1 and calls[0].event == "PreToolUse"


def test_workspace_events_from_tools_and_bash(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)

    def tool(minutes, name, ti, resp=None):
        at = t0 + timedelta(minutes=minutes)
        out = [line(at, "PreToolUse", SID_A, tool_name=name, tool_input=ti)]
        out.append(
            line(at, "PostToolUse", SID_A, tool_name=name, tool_input=ti, tool_response=resp or {})
        )
        return out

    lines += tool(
        1,
        "EnterWorktree",
        {"name": "fix-x"},
        {"worktreePath": "/work/demo/.claude/worktrees/fix-x", "worktreeBranch": "worktree-fix-x"},
    )
    lines += tool(2, "Bash", {"command": "git worktree add -b feat/y ../demo-y origin/main"})
    lines += tool(3, "Bash", {"command": "git -C /work/other worktree remove --force /tmp/scratch"})
    lines += tool(4, "Bash", {"command": "just wt fix/my-thing && ls"})
    lines += tool(5, "Bash", {"command": "wt new spike; wt rm old"})
    lines += tool(6, "Bash", {"command": "git status"})
    lines += tool(7, "ExitWorktree", {"action": "keep"})
    lines += [
        line(t0, "PreToolUse", SID_B, tool_name="EnterWorktree", tool_input={"name": "theirs"})
    ]
    store.write("hook_events.log", lines)

    ws = store.load().workspace_events(SID_A)
    got = [(w.kind, w.via, w.name, w.path, w.branch, w.action) for w in ws]
    assert got == [
        (
            "enter",
            "EnterWorktree",
            "fix-x",
            "/work/demo/.claude/worktrees/fix-x",
            "worktree-fix-x",
            None,
        ),
        ("add", "git", None, "/work/demo-y", None, None),
        ("remove", "git", None, "/tmp/scratch", None, None),
        ("add", "just", "fix/my-thing", None, None, None),
        ("add", "wt", "spike", None, None, None),
        ("remove", "wt", "old", None, None, None),
        ("exit", "ExitWorktree", None, None, None, "keep"),
    ]
    assert ws[0].at.offset == -5 * H


def test_module_level_functions_read_env_dirs(store, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOK_LOG_DIR", str(store.logs))
    monkeypatch.setenv("WORKSTREAM_PROJECTS_DIR", str(store.projects))
    t0 = datetime(2026, 10, 25, 9, 0)
    store.write("hook_events.log", store.start(SID_A, t0, -5 * H))
    assert [e.event for e in hooklog.lifecycle(SID_A)] == ["SessionStart"]
    assert hooklog.segments(SID_A)[0].start.offset == -5 * H


def test_prefilter_drops_irrelevant_tool_lines_before_parsing(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = [
        line(t0, "PostToolUse", SID_A, tool_name="Read", tool_input={"file_path": "/a"}),
        # A worktree path on a non-Bash tool is not a workspace event.
        line(
            t0,
            "PreToolUse",
            SID_A,
            tool_name="Read",
            tool_input={"file_path": "/w/.claude/worktrees/x/f"},
        ),
        line(t0, "PreToolUse", SID_A, tool_name="Bash", tool_input={"command": "ls"}),
        line(
            t0,
            "PreToolUse",
            SID_A,
            cwd="/w/.claude/worktrees/x",
            tool_name="Bash",
            tool_input={"command": "ls"},
        ),
        line(t0, "PreToolUse", SID_A, tool_name="Bash", tool_input={"command": "just wt fix/a"}),
        *send(t0, SID_A, "x", "hello"),
        # A prompt mentioning a tool event name is still a prompt.
        line(t0, "UserPromptSubmit", SID_A, prompt='why does "PreToolUse" fire twice?'),
    ]
    store.write("hook_events.log", lines)
    events = hooklog.read_events(log_dir=store.logs)
    assert [(e.event, e.tool_name) for e in events] == [
        ("PreToolUse", "Bash"),
        ("PreToolUse", "SendMessage"),
        ("UserPromptSubmit", None),
        ("PostToolUse", "SendMessage"),
    ]


def test_kept_tools_can_be_widened(store, monkeypatch):
    t0 = datetime(2026, 10, 25, 9, 0)
    store.write(
        "hook_events.log", [line(t0, "PostToolUse", SID_A, tool_name="Read", tool_input={})]
    )
    assert hooklog.read_events(log_dir=store.logs) == []
    monkeypatch.setattr(hooklog, "KEPT_TOOLS", hooklog.KEPT_TOOLS | {"Read"})
    assert [e.tool_name for e in hooklog.read_events(log_dir=store.logs)] == ["Read"]


def test_peer_scan_reads_each_top_level_transcript_once(store, monkeypatch):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H) + send(t0 + H, SID_A, "x", "ping")
    lines += send(t0 + H, SID_A, "x", "pong")
    store.write("hook_events.log", lines)
    at = datetime(2026, 10, 25, 15, 0)
    store.transcript(SID_C, [peer_record(SID_C, at, "ping", "lead")])
    sub = store.projects / "-work-demo" / SID_B / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text(
        json.dumps(
            {"sessionId": SID_B, **peer_record(SID_B, at, "pong", "lead")}, separators=(",", ":")
        )
        + "\n"
    )
    log = store.load(registry=[])
    scanned = []
    real = log._scan_peers
    monkeypatch.setattr(log, "_scan_peers", lambda p: scanned.append(p.name) or real(p))
    first = [(m.body, m.to_sid) for m in log.messages()]
    log.messages()
    assert first == [("ping", SID_C), ("pong", None)]
    assert sorted(scanned) == sorted({f"{SID_A}.jsonl", f"{SID_C}.jsonl"})


def test_pairing_finds_a_transcript_by_session_id_when_path_is_stale(store):
    t0 = datetime(2026, 10, 25, 9, 0)
    lines = store.start(SID_A, t0, -5 * H)
    lines = [ln.replace(str(store.projects), "/moved/away") for ln in lines]
    store.write("hook_events.log", lines)
    assert [o for _, o in store.load().timeline.samples] == [-5 * H]
