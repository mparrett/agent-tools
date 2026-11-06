"""Timeline model: segments, lanes, concurrency, links — pure, no seam."""

from claude_graph.graph import SessionRecord, activity_segments, build_session_graph
from claude_graph.timeline import (
    assign_lanes,
    is_active_at,
    parse_ts,
    peak_concurrency,
    segments_overlap,
    session_links,
)


def _rec(id, **kw):
    return SessionRecord(id=id, **kw)


def _seg_rec(id, *segs, **kw):
    """Record whose start/end bracket its segments."""
    return _rec(id, start=segs[0][0], end=segs[-1][1], segments=list(segs), **kw)


def test_activity_segments_split_on_idle_gap():
    stamps = [
        "2026-07-01T09:00:00Z",
        "2026-07-01T09:10:00Z",
        # 50min idle — walked away
        "2026-07-01T10:00:00Z",
        "2026-07-01T10:05:00Z",
    ]
    segs = activity_segments(stamps, gap_minutes=30)
    assert segs == [
        ("2026-07-01T09:00:00Z", "2026-07-01T09:10:00Z"),
        ("2026-07-01T10:00:00Z", "2026-07-01T10:05:00Z"),
    ]


def test_activity_segments_tolerates_disorder_and_garbage():
    segs = activity_segments(["2026-07-01T09:05:00Z", "not-a-date", "2026-07-01T09:00:00Z"])
    assert segs == [("2026-07-01T09:00:00Z", "2026-07-01T09:05:00Z")]
    assert activity_segments([]) == []


def test_lanes_split_only_on_active_overlap():
    a = _seg_rec("aaaa", ("2026-07-01T09:00:00Z", "2026-07-01T11:00:00Z"))
    b = _seg_rec("bbbb", ("2026-07-01T10:00:00Z", "2026-07-01T12:00:00Z"))  # overlaps a
    c = _seg_rec("cccc", ("2026-07-01T13:00:00Z", "2026-07-01T14:00:00Z"))  # after both
    lanes = assign_lanes([a, b, c])
    assert lanes["aaaa"] != lanes["bbbb"]
    assert lanes["cccc"] == 0  # freed lanes are reused lowest-first


def test_idle_open_session_does_not_hog_a_lane():
    # Two bursts days apart: idle in between must not conflict with midday work.
    long = _seg_rec(
        "long",
        ("2026-07-01T09:00:00Z", "2026-07-01T09:30:00Z"),
        ("2026-07-03T09:00:00Z", "2026-07-03T09:30:00Z"),
    )
    mid = _seg_rec("mid1", ("2026-07-02T09:00:00Z", "2026-07-02T10:00:00Z"))
    lanes = assign_lanes([long, mid])
    assert lanes["long"] == lanes["mid1"] == 0


def test_is_active_at_between_segments_is_false():
    r = _seg_rec(
        "rrrr",
        ("2026-07-01T09:00:00Z", "2026-07-01T09:30:00Z"),
        ("2026-07-01T11:00:00Z", "2026-07-01T11:30:00Z"),
    )
    assert is_active_at(r, parse_ts("2026-07-01T09:15:00Z"))
    assert not is_active_at(r, parse_ts("2026-07-01T10:00:00Z"))  # idle-open


def test_segments_overlap_mixed_tz_suffixes():
    a = _seg_rec("aaaa", ("2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z"))
    b = _seg_rec("bbbb", ("2026-07-01T09:30:00+00:00", "2026-07-01T09:45:00+00:00"))
    assert segments_overlap(a, b)


def test_peak_concurrency():
    a = _seg_rec("aaaa", ("2026-07-01T09:00:00Z", "2026-07-01T11:00:00Z"))
    b = _seg_rec("bbbb", ("2026-07-01T10:00:00Z", "2026-07-01T12:00:00Z"))
    c = _seg_rec("cccc", ("2026-07-01T10:30:00Z", "2026-07-01T10:45:00Z"))
    peak, at = peak_concurrency([a, b, c])
    assert peak == 3
    assert at == parse_ts("2026-07-01T10:30:00Z")
    assert peak_concurrency([]) == (0, None)


def test_session_links_rank_pr_first_and_drop_scaffold():
    recs = [
        _rec("aaaa1111", prs=["o/r#1"], files=["/x/repo/MEMORY.md", "/x/repo/app.py"]),
        _rec("bbbb2222", prs=["o/r#1"], files=["/x/repo/MEMORY.md", "/x/repo/app.py"]),
        _rec("cccc3333", files=["/x/repo/MEMORY.md"]),
    ]
    g = build_session_graph(recs)
    links = session_links(g)
    kinds = [grp.kind for grp, _ in links["aaaa1111"]]
    assert kinds[0] == "pr"
    # the MEMORY.md-only tie to cccc3333 is scaffolding, not a work link
    assert all("cccc3333" not in others for _, others in links["aaaa1111"])
    assert links["cccc3333"] == []


def test_cleared_sessions_keep_their_pane_lane():
    # b holds lane 0 all morning; the root a overlaps it and lands in lane 1
    b = _seg_rec("bbbb", ("2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z"))
    a = _seg_rec("aaaa", ("2026-07-01T09:30:00Z", "2026-07-01T10:00:00Z"))
    # c was /cleared from a, later and alone: greedy would drop it into lane 0,
    # but the pane's lane is free, so it stays with its chain
    c = _seg_rec("cccc", ("2026-07-01T11:00:00Z", "2026-07-01T12:00:00Z"), origin="aaaa")
    lanes = assign_lanes([a, b, c])
    assert lanes["bbbb"] == 0 and lanes["aaaa"] == 1 and lanes["cccc"] == 1
    # when an unrelated session took the chain's lane meanwhile, the chain moves
    d = _seg_rec("dddd", ("2026-07-01T10:30:00Z", "2026-07-01T11:30:00Z"))
    lanes = assign_lanes([a, b, c, d])
    assert lanes["dddd"] == 0 and lanes["cccc"] == 1
    e = _seg_rec("eeee", ("2026-07-01T10:30:00Z", "2026-07-01T11:30:00Z"))
    lanes = assign_lanes([a, b, c, d, e])
    assert {lanes["dddd"], lanes["eeee"]} == {0, 1} and lanes["cccc"] == 2
