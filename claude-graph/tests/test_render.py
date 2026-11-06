"""Renderer output, string-asserted over synthetic graphs (no seam, no store)."""

import io

from claude_graph.graph import SessionRecord, build_session_graph
from claude_graph.render import render_graph_tree, render_seed
from rich.console import Console


def _console():
    return Console(file=io.StringIO(), no_color=True, width=200)


def _out(c):
    return c.file.getvalue()


def _rec(id, **kw):
    return SessionRecord(id=id, **kw)


def test_scan_renders_group_header_and_members():
    g = build_session_graph(
        [
            _rec(
                "aaaa1111",
                prs=["acme/webapp#174"],
                start="2026-06-28T14:02:00",
                turns=22,
                repos=["/x/webapp"],
            ),
            _rec(
                "bbbb2222",
                prs=["acme/webapp#174"],
                start="2026-06-29T09:14:00",
                turns=8,
                repos=["/x/webapp"],
            ),
        ]
    )
    c = _console()
    render_graph_tree(g, c)
    out = _out(c)
    assert "PR acme/webapp#174" in out
    assert "2 sessions" in out
    assert "aaaa1111" in out and "bbbb2222" in out
    assert "└──" in out  # tree glyphs
    # both members share one project, so it lifts to the header (dropped from rows)
    assert "·  webapp" in out
    assert "(webapp)" not in out


def test_scan_multi_membership_annotation():
    g = build_session_graph(
        [
            _rec(
                "s1aaaaaa",
                prs=["o/r#1"],
                branches=["feat/x"],
                start="2026-06-28T10:00:00",
                repos=["/x/r"],
            ),
            _rec("s2bbbbbb", prs=["o/r#1"], start="2026-06-28T11:00:00", repos=["/x/r"]),
            _rec("s3cccccc", branches=["feat/x"], start="2026-06-28T12:00:00", repos=["/x/r"]),
        ]
    )
    c = _console()
    render_graph_tree(g, c)
    assert "↔ also" in _out(c)  # s1 belongs to both the PR and branch groups


def test_scan_lifts_shared_fields_to_header():
    # PR group {s1,s2,s3} is strictly larger than file group {s1,s2} (distinct
    # membership -> not merged). For the file group, the PR edge is common to both
    # members, so it lifts to the header rather than repeating per row. The file edge
    # is NOT common to the larger PR group, so it stays inline there.
    g = build_session_graph(
        [
            _rec(
                "s1aaaaaa",
                prs=["o/r#9"],
                files=["/x/a.py"],
                repos=["/x/r"],
                start="2026-06-28T10:00:00",
            ),
            _rec(
                "s2bbbbbb",
                prs=["o/r#9"],
                files=["/x/a.py"],
                repos=["/x/r"],
                start="2026-06-28T11:00:00",
            ),
            _rec("s3cccccc", prs=["o/r#9"], repos=["/x/r"], start="2026-06-28T12:00:00"),
        ]
    )
    c = _console()
    render_graph_tree(g, c, show_digest=False)  # digest has its own (project) format
    out = _out(c)
    assert "also PR o/r#9" in out  # PR edge lifted onto the file group's header
    assert "↔ also PR" not in out  # ...and never repeated as a per-row annotation
    assert "↔ also file a.py" in out  # file edge not common to PR group -> stays inline
    assert "(r)" not in out  # single shared project lifted off the rows too


def test_scan_subagents_collapsed_then_expanded():
    subs = {"tool_1": {"agentType": "Explore", "description": "audit X"}}
    recs = [
        _rec("s1aaaaaa", prs=["o/r#1"], start="2026-06-28T10:00:00", subagents=subs),
        _rec("s2bbbbbb", prs=["o/r#1"], start="2026-06-28T11:00:00"),
    ]
    g = build_session_graph(recs)
    c = _console()
    render_graph_tree(g, c, show_subagents=False)
    assert "+1 subagents" in _out(c)
    c2 = _console()
    render_graph_tree(g, c2, show_subagents=True)
    out2 = _out(c2)
    assert "⌁ Explore" in out2 and "audit X" in out2
    assert "+1 subagents" not in out2


def test_scan_empty_graph():
    g = build_session_graph([_rec("solo1111", prs=["o/r#1"])])  # nothing shared -> no group
    c = _console()
    render_graph_tree(g, c)
    assert "no linked sessions" in _out(c)


def _two_group_graph():
    # conn1111 shares a PR with mate2222 and a branch with mate3333 -> in two groups,
    # and touches two repos -> the widest session.
    return build_session_graph(
        [
            _rec(
                "conn1111",
                prs=["o/r#1"],
                branches=["feat/x"],
                repos=["/a", "/b"],
                start="2026-06-28T10:00:00",
                first_user="the connector",
            ),
            _rec("mate2222", prs=["o/r#1"], start="2026-06-28T11:00:00", repos=["/a"]),
            _rec("mate3333", branches=["feat/x"], start="2026-06-28T12:00:00", repos=["/a"]),
        ]
    )


def test_scan_digest_surfaces_connector_and_widest():
    c = _console()
    render_graph_tree(_two_group_graph(), c)
    out = _out(c)
    assert "digest" in out
    assert "lead cluster" in out
    assert "top connector" in out and "conn1111" in out and "2 clusters" in out
    assert "widest session" in out


def test_scan_digest_suppressed_by_flag():
    c = _console()
    render_graph_tree(_two_group_graph(), c, show_digest=False)
    assert "digest" not in _out(c)


def test_scan_digest_skipped_for_single_group():
    g = build_session_graph(
        [
            _rec("aaaa1111", prs=["o/r#1"], start="2026-06-28T10:00:00"),
            _rec("bbbb2222", prs=["o/r#1"], start="2026-06-29T11:00:00"),
        ]
    )  # exactly one group -> nothing to summarize
    c = _console()
    render_graph_tree(g, c)
    assert "digest" not in _out(c)


def test_scan_disambiguates_same_basename_across_repos():
    # same filename shared within two different repos -> two groups; labels must differ
    g = build_session_graph(
        [
            _rec("a1aaaaaa", files=["/root/xsofy/shared.txt"], start="2026-06-28T10:00:00"),
            _rec("a2aaaaaa", files=["/root/xsofy/shared.txt"], start="2026-06-28T11:00:00"),
            _rec("b1bbbbbb", files=["/root/letgo/shared.txt"], start="2026-06-28T12:00:00"),
            _rec("b2bbbbbb", files=["/root/letgo/shared.txt"], start="2026-06-28T13:00:00"),
        ]
    )
    c = _console()
    render_graph_tree(g, c, show_digest=False)
    out = _out(c)
    assert "file xsofy/shared.txt" in out
    assert "file letgo/shared.txt" in out


def test_no_color_output_has_no_ansi():
    g = build_session_graph(
        [
            _rec("aaaa1111", prs=["o/r#1"], start="2026-06-28T14:02:00"),
            _rec("bbbb2222", prs=["o/r#1"], start="2026-06-29T09:14:00"),
        ]
    )
    c = _console()
    render_graph_tree(g, c)
    assert "\x1b[" not in _out(c)


def test_seed_is_session_rooted():
    g = build_session_graph(
        [
            _rec(
                "seedaaaa",
                prs=["o/r#1"],
                start="2026-06-28T10:00:00",
                turns=22,
                repos=["/x/r"],
                first_user="wire up the grid",
            ),
            _rec("mate1111", prs=["o/r#1"], start="2026-06-28T11:00:00", repos=["/x/r"]),
        ]
    )
    c = _console()
    render_seed(g, "seedaaaa", c)
    out = _out(c)
    assert "◆ seedaaaa" in out  # the seed is the subject/root
    assert "shares work with" in out
    assert "PR o/r#1" in out and "mate1111" in out
    assert "wire up the grid" in out
    # session-rooted: the seed heads the tree; it is NOT renamed to the PR
    assert out.index("seedaaaa") < out.index("PR o/r#1")


def test_seed_not_found():
    g = build_session_graph([_rec("aaaa1111", prs=["o/r#1"]), _rec("bbbb2222", prs=["o/r#1"])])
    c = _console()
    render_seed(g, "zzzznope", c)
    assert "not found" in _out(c)
