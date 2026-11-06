#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Tests for claude-watch. Run via `uv run claude-watch/test_claude_watch.py` (or `just test`).

The module filename has a hyphen, so it can't be imported by name — load it from
its path with importlib.
"""

import importlib.util
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "claude_watch", Path(__file__).parent / "claude-watch.py"
)
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def ts(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def fire(t):
    return {
        "type": "system",
        "subtype": "scheduled_task_fire",
        "timestamp": ts(t),
        "cron": "0 12 * * *",
    }


def user(t, text, **kw):
    return {"type": "user", "timestamp": ts(t), "message": {"role": "user", "content": text}, **kw}


def say(t, text):
    return {
        "type": "assistant",
        "timestamp": ts(t),
        "message": {"content": [{"type": "text", "text": text}]},
    }


def tool(t, name, tid, **inp):
    return {
        "type": "assistant",
        "timestamp": ts(t),
        "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
    }


def result(t, tid, text):
    return {
        "type": "user",
        "timestamp": ts(t),
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": [{"type": "text", "text": text}],
                }
            ]
        },
    }


def done(t, ms):
    return {"type": "system", "subtype": "turn_duration", "durationMs": ms, "timestamp": ts(t)}


def noise(t):
    return {"type": "pr-link", "timestamp": ts(t)}


def quiet_tick(t, n, text=None, ms=6000):
    return [
        fire(t),
        user(t, "[1 prior /loop wakeup found nothing actionable]", isMeta=True),
        user(t, "<command-message>loop</command-message>\n<command-name>/loop</command-name>"),
        noise(t + 1),
        tool(t + 2, "Bash", f"b{n}", description="poll the board", command="gh pr list"),
        result(t + 3, f"b{n}", "nothing"),
        say(t + 4, text or f"Quiet tick {n}. Nothing moved."),
        tool(t + 5, "ScheduleWakeup", f"w{n}", delaySeconds=1800, reason="watch"),
        result(t + 5, f"w{n}", "Next wakeup scheduled"),
        done(t + 6, ms),
    ]


def append(path, recs):
    with open(path, "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def store(tmp_path):
    proj = tmp_path / "-Users-x-proj"
    proj.mkdir()
    return proj


def make_session(store, sid="5e55a0de-aaaa", recs=()):
    path = store / f"{sid}.jsonl"
    path.write_text("")
    append(path, recs)
    return path


def watcher(path, **kw):
    out = io.StringIO()
    return cw.Watcher(path, out=out, color=False, width=200, **kw), out


def ticks(n, start=0):
    return [r for i in range(start, start + n) for r in quiet_tick(i * 1800, i)]


# -- resolution and reading ----------------------------------------------------


def test_resolve_by_prefix(store):
    path = make_session(store)
    assert cw.resolve_session("5e55", base=store.parent) == path


def test_resolve_ambiguous_and_missing_exit(store):
    make_session(store, "abc-1")
    make_session(store, "abc-2")
    with pytest.raises(SystemExit, match="ambiguous"):
        cw.resolve_session("abc", base=store.parent)
    with pytest.raises(SystemExit, match="no session"):
        cw.resolve_session("zzz", base=store.parent)


def test_tail_holds_back_partial_line(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\n{"b"')
    t = cw.Tail(p)
    assert t.read() == [{"a": 1}]
    with open(p, "a") as f:
        f.write(": 2}\n")
    assert t.read() == [{"b": 2}]
    assert t.read() == []


def test_start_offset_counts_ticks_not_injected_prompts(store):
    recs = [r for n in range(4) for r in quiet_tick(n * 1800, n)]
    path = make_session(store, recs=recs)
    offset = cw.find_start_offset(path, turns=2)
    first = json.loads(path.read_bytes()[offset:].split(b"\n")[0])
    # The 3rd tick's fire record, not one of the /loop messages injected after it.
    assert first["subtype"] == "scheduled_task_fire"
    assert first["timestamp"] == ts(2 * 1800)


def test_start_offset_since(store):
    recs = [r for n in range(4) for r in quiet_tick(n * 1800, n)]
    path = make_session(store, recs=recs)
    offset = cw.find_start_offset(path, since=T0 + timedelta(seconds=3000))
    first = json.loads(path.read_bytes()[offset:].split(b"\n")[0])
    assert first["timestamp"] == ts(2 * 1800)


# -- history -------------------------------------------------------------------


def test_history_shows_each_turn_as_header_and_summary(store):
    recs = quiet_tick(0, 0) + quiet_tick(1800, 1, text="One PR moved.")
    recs += [user(4000, "what changed overnight?"), say(4001, "Two PRs merged."), done(4002, 2000)]
    path = make_session(store, recs=recs)
    w, out = watcher(path)
    w.replay(0)
    lines = out.getvalue().splitlines()[1:]  # after the day rule
    assert lines == [
        f"── {cw.hhmm(T0)}     6s   2 tools            next {cw.hhmm(T0 + timedelta(seconds=1805))}  tick",
        "     ↳ Quiet tick 0. Nothing moved.",
        lines[2],
        "     ↳ One PR moved.",
        lines[4],
        "     ↳ Two PRs merged.",
    ]
    # Labels start in the same column whatever the stats say.
    assert {ln.index("tick") for ln in lines[0:3:2]} == {lines[4].index("you:")}
    assert "poll the board" not in out.getvalue()


def test_narrow_terminal_does_not_overflow(store):
    path = make_session(store, recs=quiet_tick(0, 0))
    out = io.StringIO()
    w = cw.Watcher(path, out=out, color=False, width=46)
    w.replay(0)
    lines = out.getvalue().splitlines()
    assert all(len(ln) <= 46 for ln in lines), lines
    assert any(ln.startswith("── ") and ln.endswith("tick") for ln in lines)


def test_verbose_history_expands_tool_calls(store):
    path = make_session(store, recs=quiet_tick(0, 0))
    w, out = watcher(path, verbose=True)
    w.replay(0)
    assert "Bash  poll the board" in out.getvalue()


def test_next_wakeup_from_schedule_call(store):
    path = make_session(store, recs=quiet_tick(0, 0))
    w, out = watcher(path)
    w.replay(0)
    assert w.next_wakeup == T0 + timedelta(seconds=5 + 1800)
    assert f"next {cw.hhmm(w.next_wakeup)}" in out.getvalue()


def test_background_agent_completion_prints_in_history(store):
    recs = [
        user(0, "fix it"),
        tool(1, "Agent", "tA", description="Review the parser change", subagent_type="general-purpose", model="opus", run_in_background=True),
        result(2, "tA", "Async agent launched successfully.\nagentId: a01 (internal ID)"),
        done(3, 3000),
        {"type": "queue-operation", "operation": "enqueue", "timestamp": ts(600),
         "content": "<task-notification>\n<tool-use-id>tA</tool-use-id>\n<status>completed</status>\n"
                    "<summary>Agent \"Review the parser change\" finished</summary>"},
    ]  # fmt: skip
    path = make_session(store, recs=recs)
    w, out = watcher(path)
    w.replay(0)
    text = out.getvalue()
    assert "1 agent" in text
    # The agent count has its own column, so this label lines up with others.
    head = next(ln for ln in text.splitlines() if "you: fix it" in ln)
    assert head.index("you:") == len("── 05:00     3s   0 tools  1 agent   next 05:00  ")
    assert "✓ agent Review the parser" in text and "done 9m59s" in text


def test_history_folds_repeat_ticks_into_one_line(store):
    recs = ticks(4) + [user(8000, "what changed?"), say(8001, "Nothing."), done(8002, 1000)]
    path = make_session(store, recs=recs)
    w, out = watcher(path)
    w.replay(0)
    lines = out.getvalue().splitlines()[1:]
    # Digits differ ("Quiet tick 2"), but the report is the same one.
    assert lines[2] == (
        f"   ≡ ×3 same result · {cw.hhmm(T0 + timedelta(seconds=1800))}–"
        f"{cw.hhmm(T0 + timedelta(seconds=5400))} · 6s · next {cw.hhmm(T0 + timedelta(seconds=7205))}"
    )
    assert "you: what changed?" in lines[3] and len(lines) == 5
    w, out = watcher(path, verbose=True)
    w.replay(0)
    assert "same result" not in out.getvalue()


def test_tick_that_did_something_breaks_the_run(store):
    edit = quiet_tick(1800, 1)
    edit.insert(4, tool(1801, "Edit", "e1", file_path="/tmp/x.py"))
    recs = quiet_tick(0, 0) + edit + quiet_tick(3600, 2) + quiet_tick(5400, 3, ms=200_000)
    path = make_session(store, recs=recs)
    w, out = watcher(path)
    w.replay(0)
    text = out.getvalue()
    # The edit tick and the slow tick print in full; the tick after the edit
    # starts a new run rather than joining the first.
    assert "same result" not in text
    assert sum(ln.startswith("── ") and ln.endswith("tick") for ln in text.splitlines()) == 4


# -- live ------------------------------------------------------------------------


def test_live_holds_a_repeat_tick_and_counts_it_in_place(store):
    path = make_session(store, recs=ticks(1))
    w, out = watcher(path, tty=True)
    w.replay(0)
    w.go_live()
    start = len(out.getvalue())
    append(path, quiet_tick(1800, 1))
    w.poll()
    first = out.getvalue()[start:]
    assert "poll the board" not in first and "×1 same result" in first
    assert "\x1b[" not in first  # nothing above it to replace yet
    append(path, quiet_tick(3600, 2))
    w.poll()
    second = out.getvalue()[start + len(first) :]
    assert second.startswith("\x1b[1A\r\x1b[J") and "×2 same result" in second


def test_live_prints_a_new_result_in_full(store):
    path = make_session(store, recs=ticks(1))
    w, out = watcher(path)
    w.replay(0)
    w.go_live()
    start = len(out.getvalue())
    append(path, quiet_tick(1800, 1, text="One PR moved."))
    w.poll()
    live = out.getvalue()[start:]
    assert "── " in live and "Bash  poll the board" in live and "» One PR moved." in live
    assert "same result" not in live


def test_live_held_tick_streams_once_it_runs_long(store):
    now = (datetime.now(timezone.utc) - T0).total_seconds()
    path = make_session(store, recs=ticks(1))
    w, out = watcher(path)
    w.replay(0)
    w.go_live()
    append(path, [fire(now - cw.HOLD_SECONDS - 5), tool(now - 5, "Bash", "b9", description="slow")])
    w.poll()
    assert "Bash  slow" in out.getvalue()


def test_redraw_streams_the_tick_then_replaces_it(store):
    path = make_session(store, recs=ticks(1))
    w, out = watcher(path, tty=True, redraw=True)
    w.replay(0)
    w.go_live()
    append(path, quiet_tick(1800, 1)[:6])
    w.poll()
    assert "Bash  poll the board" in out.getvalue()  # streamed before it ended
    append(path, quiet_tick(1800, 1)[6:])
    w.poll()
    # Header, Bash, », ScheduleWakeup: four lines streamed, four erased.
    assert out.getvalue().endswith(
        "   ScheduleWakeup  in 30m · watch\n\x1b[4A\r\x1b[J   ≡ ×1 same result · "
        f"{cw.hhmm(T0 + timedelta(seconds=1800))} · 6s · next {cw.hhmm(T0 + timedelta(seconds=3605))}\n"
    )


def test_live_follows_subagent_tool_calls(store):
    path = make_session(store, recs=quiet_tick(0, 0))
    w, out = watcher(path)
    w.replay(0)
    w.go_live()
    start = len(out.getvalue())

    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a01.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "Review the parser change",
                "toolUseId": "tA",
                "model": "opus",
            }
        )
    )
    append(path, [
        fire(1800),
        tool(1801, "Agent", "tA", description="Review the parser change", subagent_type="general-purpose", model="opus"),
    ])  # fmt: skip
    w.poll()
    append(sub / "agent-a01.jsonl", [
        user(1801, "Implementer brief", isSidechain=True),
        tool(1802, "Edit", "e1", file_path=str(Path.home() / "src/parser.py")),
        tool(1900, "SubagentHandback", "h1", message="One finding: the empty-input case is skipped.\nMore."),
    ])  # fmt: skip
    w.poll()
    append(
        path,
        [result(1901, "tA", "One finding."), say(1902, "Merged."), done(1903, 103000)],
    )
    w.poll()

    live = out.getvalue()[start:]
    assert "── " in live and "tick" in live
    assert "⧗ agent Review the parser…" in live and "spawned general-purpose/opus" in live
    assert "┊ Review the parser" in live and "· Edit  ~/src/parser.py" in live
    assert (
        "✓ agent Review the parser" in live
        and "One finding: the empty-input case is skipped." in live
    )
    # Handback settled it; the parent's tool_result must not report it twice.
    assert live.count("✓ agent") == 1
    assert "└ 1m43s · 0 tools · 1 agent" in live


def test_handback_and_notification_in_one_poll_report_once(store):
    """The parent's task-notification and the subagent's handback are written
    within milliseconds; whichever is read first, report one completion and no
    phantom resume."""
    path = make_session(store, recs=[user(0, "go")])
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a01.meta.json").write_text(
        json.dumps({"description": "Sample", "toolUseId": "tA"})
    )
    w, out = watcher(path)
    w.replay(0)
    w.go_live()
    append(path, [tool(1, "Agent", "tA", description="Sample", run_in_background=True)])
    w.poll()
    append(sub / "agent-a01.jsonl", [
        tool(2, "Bash", "b1", description="step"),
        tool(10, "SubagentHandback", "h1", message="All three steps ran."),
    ])  # fmt: skip
    append(path, [{"type": "queue-operation", "operation": "enqueue", "timestamp": ts(11),
                   "content": "<task-notification>\n<tool-use-id>tA</tool-use-id>\n"
                              "<summary>Agent \"Sample\" finished</summary>"}])  # fmt: skip
    w.poll()
    text = out.getvalue()
    assert text.count("✓ agent") == 1
    assert "All three steps ran." in text
    assert "resumed" not in text


def test_long_description_tag_cuts_at_word():
    a = cw.Agent("t", "Sample subagent for watcher")
    assert a.tag == "Sample subagent for…"
    assert cw.Agent("t", "Review the parser change").tag == "Review the parser…"


def test_tag_without_spaces_keeps_column_width():
    assert len(cw.Agent("t", "Review-the-parser-change-now").tag) == cw.TAG_WIDTH


def test_interrupted_turn_closes_at_next_prompt(store):
    """An interrupted turn writes no turn_duration; the next typed prompt must
    still open a turn of its own rather than land inside the dead one."""
    recs = [
        user(0, "first ask"),
        tool(1, "Bash", "b1", description="slow thing"),
        {"type": "user", "timestamp": ts(5),
         "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
        user(60, "second ask"),
        say(61, "Answer two."),
        done(62, 2000),
        user(120, "third ask"),
        tool(121, "Bash", "b2", description="another"),
        user(130, "fourth ask, typed with no interrupt record"),
        say(131, "Answer four."),
        done(132, 2000),
    ]  # fmt: skip
    path = make_session(store, recs=recs)
    w, out = watcher(path)
    w.replay(0)
    heads = [ln for ln in out.getvalue().splitlines() if ln.startswith("── ") and "you:" in ln]
    assert [h.split("you: ")[1].split("  ")[0] for h in heads] == [
        "first ask", "second ask", "third ask", "fourth ask, typed with no interrupt record",
    ]  # fmt: skip
    assert w.turn is None
    path2 = make_session(store, "other", recs)
    assert cw.find_start_offset(path2, turns=4) == 0


def test_agent_whose_meta_lands_after_startup_is_followed(store):
    now = (datetime.now(timezone.utc) - T0).total_seconds()
    recs = [
        user(now - 5, "go"),
        tool(now - 4, "Agent", "tA", description="Fresh", run_in_background=True),
    ]
    path = make_session(store, recs=recs)
    w, out = watcher(path)
    w.replay(0)
    w.go_live()
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a01.meta.json").write_text(
        json.dumps({"description": "Fresh", "toolUseId": "tA"})
    )
    append(sub / "agent-a01.jsonl", [tool(now - 2, "Bash", "b1", description="first step")])
    w.poll()
    assert "first step" in out.getvalue()


def test_stale_unfinished_agent_is_not_followed(store):
    recs = [
        user(0, "go"),
        tool(1, "Agent", "tA", description="Old work", run_in_background=True),
        done(2, 2000),
    ]
    path = make_session(store, recs=recs)
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a01.meta.json").write_text(
        json.dumps({"description": "Old work", "toolUseId": "tA"})
    )
    f = sub / "agent-a01.jsonl"
    append(f, [user(1, "brief", isSidechain=True), tool(5, "Bash", "b1", command="ls")])
    old = time.time() - cw.STALE_AGENT_SECONDS - 60
    os.utime(f, (old, old))

    w, out = watcher(path)
    w.replay(0)
    w.go_live()
    w.poll()
    assert "running" not in out.getvalue()
    assert "┊" not in out.getvalue()


def test_recent_unfinished_agent_is_followed_from_now(store):
    recs = [
        user(0, "go"),
        tool(1, "Agent", "tA", description="Live work", run_in_background=True),
        done(2, 2000),
    ]
    path = make_session(store, recs=recs)
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a01.meta.json").write_text(
        json.dumps({"description": "Live work", "toolUseId": "tA"})
    )
    f = sub / "agent-a01.jsonl"
    append(
        f, [user(1, "brief", isSidechain=True), tool(5, "Bash", "b1", description="earlier step")]
    )

    w, out = watcher(path)
    w.replay(0)
    w.go_live()
    append(f, [tool(9, "Bash", "b2", description="later step")])
    w.poll()
    text = out.getvalue()
    assert "⧗ agent Live work" in text and "following" in text
    assert "later step" in text and "earlier step" not in text


def test_foreground_agent_that_finished_before_the_window_is_not_followed(store):
    recs = [user(0, "go"), tool(1, "Agent", "tA", description="Quick look")]
    recs += [result(300, "tA", "Looked."), done(301, 301000)] + ticks(5, start=1)
    path = make_session(store, recs=recs)
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a01.meta.json").write_text(
        json.dumps({"description": "Quick look", "toolUseId": "tA"})
    )
    f = sub / "agent-a01.jsonl"
    append(f, [user(1, "brief", isSidechain=True), tool(5, "Bash", "b1", command="ls"),
               result(6, "b1", "ok"), say(299, "Looked.")])  # fmt: skip
    old = time.time() - 300
    os.utime(f, (old, old))
    w, out = watcher(path)
    w.replay(cw.find_start_offset(path, turns=5))
    w.go_live()
    assert "following" not in out.getvalue()


def test_status_line_fits_the_terminal(store):
    path = make_session(store, recs=ticks(1))
    out = io.StringIO()
    w = cw.Watcher(path, out=out, color=True, width=30)
    w.replay(0)
    w.go_live()
    w.next_wakeup = datetime.now(timezone.utc) + timedelta(minutes=29)
    start = len(out.getvalue())
    w.status()
    shown = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", out.getvalue()[start:])
    assert len(shown) <= 30, shown


def test_echo_off_without_a_terminal(monkeypatch):
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        assert cw._echo_off() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
