"""The canonical JSON document: shape, seed scoping, concurrency flags."""

import io
import json

from claude_graph.export import dump_doc, graph_to_doc
from claude_graph.graph import SessionRecord, build_session_graph
from rich.console import Console


def _rec(id, **kw):
    return SessionRecord(id=id, **kw)


def _graph():
    return build_session_graph(
        [
            _rec(
                "aaaa1111",
                prs=["o/r#1"],
                start="2026-07-01T09:00:00Z",
                end="2026-07-01T10:00:00Z",
                segments=[("2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z")],
                repos=["/x/repo"],
                turns=5,
            ),
            _rec(
                "bbbb2222",
                prs=["o/r#1"],
                start="2026-07-01T09:30:00Z",
                end="2026-07-01T11:00:00Z",
                segments=[("2026-07-01T09:30:00Z", "2026-07-01T11:00:00Z")],
                repos=["/x/repo"],
                turns=3,
            ),
            _rec("cccc3333", branches=["feat/z"], start="2026-07-02T09:00:00Z"),
            _rec("dddd4444", branches=["feat/z"], start="2026-07-02T10:00:00Z"),
        ]
    )


def test_doc_shape_and_concurrency_edge():
    doc = graph_to_doc(_graph(), window_total=9)
    assert doc["version"] == 1
    assert doc["window_total"] == 9
    assert {s["id"] for s in doc["sessions"]} == {"aaaa1111", "bbbb2222", "cccc3333", "dddd4444"}
    pr_edge = next(e for e in doc["edges"] if e["a"] == "aaaa1111")
    assert pr_edge["b"] == "bbbb2222"
    assert ["pr", "o/r#1"] in pr_edge["artifacts"]
    assert pr_edge["concurrent"] is True  # active segments overlap
    br_edge = next(e for e in doc["edges"] if e["a"] == "cccc3333")
    assert br_edge["concurrent"] is False  # no segments at all
    assert doc["peak_concurrency"]["count"] == 2
    assert doc["linked_sessions"] == 4
    # lanes: the two concurrent sessions split, the rest pack to 0
    lanes = {s["id"]: s["lane"] for s in doc["sessions"]}
    assert lanes["aaaa1111"] != lanes["bbbb2222"]


def test_seed_scoping():
    doc = graph_to_doc(_graph(), seed="aaaa1111")
    assert doc["seed"] == "aaaa1111"
    assert {s["id"] for s in doc["sessions"]} == {"aaaa1111", "bbbb2222"}
    assert all("cccc3333" not in (e["a"], e["b"]) for e in doc["edges"])


def test_dump_doc_is_plain_parseable_json():
    buf = io.StringIO()
    dump_doc(graph_to_doc(_graph()), Console(file=buf, force_terminal=True))
    parsed = json.loads(buf.getvalue())
    assert parsed["version"] == 1
    assert "\x1b" not in buf.getvalue()  # no ANSI even on a color console


def test_doc_carries_origin_and_lineage():
    g = build_session_graph(
        [
            _rec("root0000", start="2026-07-03T09:00:00Z", end="2026-07-03T10:00:00Z"),
            _rec("kid00001", start="2026-07-03T11:00:00Z", origin="root0000"),
            _rec("kid00002", start="2026-07-03T12:00:00Z", origin="root0000"),
        ]
    )
    doc = graph_to_doc(g)
    by_id = {s["id"]: s for s in doc["sessions"]}
    assert by_id["root0000"]["origin"] is None and by_id["kid00001"]["origin"] == "root0000"
    assert doc["lineage"] == [
        {"session": "kid00001", "cleared_from": "root0000"},
        {"session": "kid00002", "cleared_from": "root0000"},
    ]
    # the root may be outside the window; the pointer stays
    doc = graph_to_doc(build_session_graph([_rec("kid00003", origin="gone0000")]))
    assert doc["lineage"] == [{"session": "kid00003", "cleared_from": "gone0000"}]
