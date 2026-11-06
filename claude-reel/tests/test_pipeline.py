"""Pipeline tests over synthetic transcripts — no store, no seam (sessions.py isn't
imported, so claude-ls isn't required to run these)."""

import json

from claude_reel.curate import curate
from claude_reel.extract import derive_tool_summary, extract
from claude_reel.markdown import render_markdown
from claude_reel.render_html import render


def _write(tmp_path, records):
    f = tmp_path / "sess1234-abcd-0000.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return f


def _u(text, ts):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def _a_blocks(blocks, ts, model="claude-opus-4-8"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "model": model, "content": blocks},
    }


def _result(tid, text, ts, is_error=False):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error}
            ],
        },
    }


def _transcript(tmp_path):
    recs = [
        {
            **_u("Please add the widget", "2026-07-05T10:00:00Z"),
            "cwd": "/x/myrepo",
            "gitBranch": "main",
            "sessionId": "sess1234",
        },
        _a_blocks(
            [
                {"type": "text", "text": "On it. **Editing** now."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Edit",
                    "input": {
                        "file_path": "/x/myrepo/app.py",
                        "old_string": "a\nb",
                        "new_string": "a\nb\nc\nd",
                    },
                },
            ],
            "2026-07-05T10:00:01Z",
        ),
        _result("t1", "ok", "2026-07-05T10:00:02Z"),
        _a_blocks(
            [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}],
            "2026-07-05T10:00:03Z",
        ),
        _result("t2", "..... 54 passed in 1.2s", "2026-07-05T10:00:04Z"),
        _u(
            "<system-reminder>ignore me</system-reminder>", "2026-07-05T10:00:05Z"
        ),  # injected -> dropped
        _u("now write the docs", "2026-07-05T10:01:00Z"),
        _a_blocks(
            [
                {
                    "type": "tool_use",
                    "id": "t3",
                    "name": "Edit",
                    "input": {
                        "file_path": "/x/myrepo/README.md",
                        "old_string": "x",
                        "new_string": "y",
                    },
                },
                {
                    "type": "tool_use",
                    "id": "t4",
                    "name": "Edit",
                    "input": {
                        "file_path": "/x/myrepo/README.md",
                        "old_string": "p",
                        "new_string": "q",
                    },
                },
            ],
            "2026-07-05T10:01:01Z",
        ),
    ]
    return _write(tmp_path, recs)


def test_extract_beats_and_meta(tmp_path):
    reel = extract(_transcript(tmp_path))
    kinds = [b["kind"] for b in reel["beats"]]
    assert kinds.count("user_say") == 2  # injected system-reminder dropped
    assert "assistant_say" in kinds and "tool_call" in kinds and "tool_result" in kinds
    m = reel["meta"]
    assert m["project"] == "myrepo"
    assert m["branch"] == "main"
    assert m["model"] == "claude-opus-4-8"
    assert m["tool_calls"]["Edit"] == 3 and m["tool_calls"]["Bash"] == 1


def test_extract_drops_synthetic_user_turns(tmp_path):
    recs = [
        {**_u("real prompt one", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
        {
            **_u("Approach this as the design lead …", "2026-07-05T10:00:01Z"),
            "isMeta": True,
        },  # skill inject
        _u(
            "[Request interrupted by user for tool use]", "2026-07-05T10:00:02Z"
        ),  # interrupt marker
        _u("real prompt two", "2026-07-05T10:00:03Z"),
    ]
    reel = extract(_write(tmp_path, recs))
    says = [b["text"] for b in reel["beats"] if b["kind"] == "user_say"]
    assert says == ["real prompt one", "real prompt two"]


def test_tool_summary_diff_stat():
    label, arg, stat = derive_tool_summary(
        "Edit", {"file_path": "/x/app.py", "old_string": "a\nb", "new_string": "a\nb\nc\nd"}
    )
    assert label == "edit" and arg == "app.py"
    assert stat == "+4 −2"
    assert derive_tool_summary("Bash", {"command": "pytest -q\nsecond line"})[0] == "run"
    assert derive_tool_summary("Read", {"file_path": "/a/b/render.py"}) == (
        "read",
        "render.py",
        None,
    )


def test_curate_slides_badges_and_repeat(tmp_path):
    deck = curate(extract(_transcript(tmp_path)))
    assert len(deck["slides"]) == 2  # one per real user turn
    s0, s1 = deck["slides"]
    assert s0["title"] == "Please add the widget"
    assert any(b["text"] == "54 passed" for b in s0["badges"])
    # second slide: two identical adjacent Edits collapse to one N× chip
    tools = [e for e in s1["elements"] if e["type"] == "tool"]
    assert len(tools) == 1 and tools[0]["count"] == 2
    # tool elements carry ts, so the slide's range extends past the prompt to the work
    assert s1["ts_range"] == ["2026-07-05T10:01:00Z", "2026-07-05T10:01:01Z"]


def test_repeat_collapse_surfaces_later_failure(tmp_path):
    # two identical Bash calls; the second fails. The collapsed chip must reflect the
    # failure, not the first call's pass.
    recs = [
        {**_u("run the tests twice", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
        _a_blocks(
            [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}],
            "2026-07-05T10:00:01Z",
        ),
        _result("t1", "1 passed", "2026-07-05T10:00:02Z"),
        _a_blocks(
            [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}],
            "2026-07-05T10:00:03Z",
        ),
        _result("t2", "boom: 1 failed", "2026-07-05T10:00:04Z", is_error=True),
    ]
    s = curate(extract(_write(tmp_path, recs)))["slides"][0]
    tools = [e for e in s["elements"] if e["type"] == "tool"]
    assert len(tools) == 1 and tools[0]["count"] == 2
    assert tools[0]["is_error"] is True
    badges = [b["text"] for b in s["badges"]]
    assert any("failed" in b for b in badges) and "1 passed" not in badges


def test_render_html_is_escaped(tmp_path):
    recs = [
        {
            **_u("danger <script>alert(1)</script> and **bold**", "2026-07-05T10:00:00Z"),
            "cwd": "/x/repo",
        },
        _a_blocks([{"type": "text", "text": "done"}], "2026-07-05T10:00:01Z"),
    ]
    html = render(curate(extract(_write(tmp_path, recs))))
    assert "&lt;script&gt;alert(1)" in html  # user content escaped
    assert "<script>alert(1)" not in html  # ...never live
    assert "claude" in html and "recap" in html


def test_markdown_safe_and_formatted():
    out = render_markdown("**bold** and `co<de`\n\n- one\n- two")
    assert "<strong>bold</strong>" in out
    assert "<code>co&lt;de</code>" in out  # escaped inside code span
    assert "<li>one</li>" in out and "<ul>" in out
    assert "<script>" not in render_markdown("<script>evil()</script>")


def test_markdown_link_href_cannot_break_out():
    out = render_markdown('[x](https://evil"onmouseover=alert)')
    assert '"onmouseover' not in out  # the quote can't close the href attribute
    assert "&quot;onmouseover" in out
