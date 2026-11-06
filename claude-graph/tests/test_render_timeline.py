"""Timeline renderer, string-asserted over synthetic graphs. Assertions avoid
exact clock times — rendering is in local time, and CI/dev machines differ."""

import io

from claude_graph.graph import SessionRecord, build_session_graph
from claude_graph.render import render_timeline
from rich.console import Console


def _console():
    return Console(file=io.StringIO(), no_color=True, width=200)


def _out(c):
    return c.file.getvalue()


def _rec(id, **kw):
    return SessionRecord(id=id, **kw)


def _burst(id, start, end, **kw):
    return _rec(id, start=start, end=end, segments=[(start, end)], **kw)


def test_timeline_rows_day_separators_and_legend():
    g = build_session_graph(
        [
            _burst(
                "aaaa1111",
                "2026-07-01T09:00:00Z",
                "2026-07-01T10:00:00Z",
                repos=["/x/webapp"],
                turns=5,
                first_user="fix the login flow",
            ),
            _burst("bbbb2222", "2026-07-04T09:00:00Z", "2026-07-04T09:30:00Z", repos=["/x/webapp"]),
        ]
    )
    c = _console()
    render_timeline(g, c)
    out = _out(c)
    assert "aaaa1111" in out and "bbbb2222" in out
    assert out.index("bbbb2222") < out.index("aaaa1111")  # newest first
    assert out.count("── ") == 2  # one separator per distinct day
    assert '"fix the login flow"' in out
    assert "● last active" in out  # legend
    assert "2 sessions" in out


def test_timeline_concurrent_sessions_get_parallel_lanes():
    g = build_session_graph(
        [
            _burst("aaaa1111", "2026-07-01T09:00:00Z", "2026-07-01T11:00:00Z", repos=["/x/one"]),
            _burst("bbbb2222", "2026-07-01T09:30:00Z", "2026-07-01T10:30:00Z", repos=["/x/two"]),
        ]
    )
    c = _console()
    render_timeline(g, c)
    # conflicting sessions occupy different columns, and the one still running
    # at the other's anchor moment draws its bar there
    dot_rows = [ln for ln in _out(c).splitlines() if "●" in ln and "last active" not in ln]
    assert len(dot_rows) == 2
    assert dot_rows[0].index("●") != dot_rows[1].index("●")
    assert any("│" in ln for ln in dot_rows)


def test_ten_concurrent_sessions_get_ten_distinct_columns():
    """Review finding: the gutter used to cap at 8 lanes and clamp overflow into
    the last column — misdrawing exactly the high-concurrency bursts the view is
    for. Every concurrent session gets its own column."""
    g = build_session_graph(
        [
            _burst(
                f"cc{i:02d}aaaa",
                "2026-07-01T09:00:00Z",
                "2026-07-01T12:00:00Z",
                repos=[f"/x/p{i}"],
            )
            for i in range(10)
        ]
    )
    c = _console()
    render_timeline(g, c)
    dot_rows = [ln for ln in _out(c).splitlines() if "●" in ln and "last active" not in ln]
    assert len(dot_rows) == 10
    assert len({ln.index("●") for ln in dot_rows}) == 10


def test_timeline_multiday_session_tagged_since():
    g = build_session_graph(
        [
            _rec(
                "aaaa1111",
                start="2026-07-01T09:00:00Z",
                end="2026-07-04T09:00:00Z",  # resumed days later — 'since' under any tz
                segments=[
                    ("2026-07-01T09:00:00Z", "2026-07-01T09:30:00Z"),
                    ("2026-07-04T08:00:00Z", "2026-07-04T09:00:00Z"),
                ],
                repos=["/x/webapp"],
            )
        ]
    )
    c = _console()
    render_timeline(g, c)
    assert "since " in _out(c)


def test_timeline_links_annotation_and_no_scaffold_links():
    g = build_session_graph(
        [
            _rec("aaaa1111", prs=["o/r#7"], files=["/x/r/MEMORY.md"], start="2026-07-01T09:00:00Z"),
            _rec("bbbb2222", prs=["o/r#7"], files=["/x/r/MEMORY.md"], start="2026-07-01T10:00:00Z"),
            _rec("cccc3333", files=["/x/r/MEMORY.md"], start="2026-07-01T11:00:00Z"),
        ]
    )
    c = _console()
    render_timeline(g, c)
    out = _out(c)
    assert "↔ PR o/r#7 with bbbb2222" in out
    # cccc3333 shares only MEMORY.md — scaffolding, so no link line for it
    ccc_lines = [ln for ln in out.splitlines() if "cccc3333" in ln]
    assert ccc_lines and all("↔" not in ln for ln in ccc_lines)


def test_timeline_empty():
    c = _console()
    render_timeline(build_session_graph([]), c)
    assert "no sessions" in _out(c)


def test_timeline_tags_a_cleared_session_and_explains_the_glyph():
    g = build_session_graph(
        [
            _burst("root0000", "2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z", repos=["/x/webapp"]),
            _burst(
                "kid00001",
                "2026-07-01T11:00:00Z",
                "2026-07-01T12:00:00Z",
                repos=["/x/webapp"],
                origin="root0000",
            ),
        ]
    )
    c = _console()
    render_timeline(g, c)
    out = _out(c)
    assert "↺ cleared from root0000" in out
    assert "↺ /cleared: same pane, same lane" in out
    # a store with no clears keeps the legend short
    c = _console()
    render_timeline(
        build_session_graph([_burst("solo0000", "2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z")]), c
    )
    assert "↺" not in _out(c)
