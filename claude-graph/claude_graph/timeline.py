"""The timeline model: lanes, per-session links, concurrency.

The timeline answers "what happened when" — every session in the window is a row,
newest first, and wall-clock concurrency is drawn as parallel lanes (the git
log --graph read). Interdependence rides along as per-session link annotations
derived from the same artifact groups the scan/seed views use.

Everything here is pure over `SessionRecord`s / `SessionGraph` — no filesystem, no
seam — mirroring `build_session_graph`. Timestamps are compared as datetimes (via
`parse_ts`), never as strings: the store mixes `Z` and `+00:00` suffixes.
"""

from .graph import parse_ts


def _span(rec):
    """A session's wall-clock interval as aware datetimes, or None if undatable.
    Falls back to the activity segments when the metadata times are missing."""
    start = parse_ts(rec.start) or (parse_ts(rec.segments[0][0]) if rec.segments else None)
    if start is None:
        return None
    end = parse_ts(rec.end) or (parse_ts(rec.segments[-1][1]) if rec.segments else None)
    return (start, max(end, start) if end else start)


def assign_lanes(records) -> dict:
    """Greedy conflict coloring: id -> lane number. Two sessions need distinct lanes
    only when they were *actively* working at the same moment (activity segments
    overlap) — a session left open-but-idle for days doesn't hog a column, which
    keeps the gutter as narrow as the real concurrency. Freed lanes are reused
    lowest-first; undatable sessions get lane 0 (they draw no bars anyway).

    Sessions of one pane lineage (a root and everything `/clear`ed from it, which
    share `origin`) keep the lane the chain already holds whenever it is free, so
    a pane reads as one thread down the gutter instead of hopping columns at
    every clear. They never overlap in time — one process — so the lane is free
    unless an unrelated session took it meanwhile."""
    order = sorted(
        (r for r in records if _span(r)),
        key=lambda r: _span(r)[0],
    )
    lanes: dict = {r.id: 0 for r in records}
    placed: list = []  # (record, lane)
    chain_lane: dict = {}  # root id -> the lane its members last used
    for r in order:
        taken = {ln for o, ln in placed if segments_overlap(r, o)}
        key = r.origin or r.id
        held = chain_lane.get(key)
        if held is not None and held not in taken:
            lane = held
        else:
            lane = next(i for i in range(len(taken) + 1) if i not in taken)
        lanes[r.id] = chain_lane[key] = lane
        placed.append((r, lane))
    return lanes


def is_active_at(rec, at) -> bool:
    """Was the session inside an activity segment at datetime `at`?"""
    for s, e in rec.segments:
        s, e = parse_ts(s), parse_ts(e)
        if s and e and s <= at <= e:
            return True
    return False


def segments_overlap(a, b) -> bool:
    """True when two sessions were *actively* working at the same wall-clock moment
    (activity segments intersect) — the honest concurrency signal, not merely both
    windows open."""
    bs = [(parse_ts(s), parse_ts(e)) for s, e in b.segments]
    for s1, e1 in ((parse_ts(s), parse_ts(e)) for s, e in a.segments):
        if s1 is None or e1 is None:
            continue
        for s2, e2 in bs:
            if s2 is None or e2 is None:
                continue
            if s1 <= e2 and s2 <= e1:
                return True
    return False


def peak_concurrency(records):
    """(count, datetime) at the moment the most sessions were simultaneously
    *active* — the window's high-water mark. (0, None) when nothing is datable."""
    events = []  # (time, +1/-1) — starts sort before ends at the same instant
    for r in records:
        for s, e in r.segments:
            s, e = parse_ts(s), parse_ts(e)
            if s and e:
                events.append((s, 1))
                events.append((e, -1))
    best, best_at, cur = 0, None, 0
    for t, d in sorted(events, key=lambda ev: (ev[0], -ev[1])):
        cur += d
        if cur > best:
            best, best_at = cur, t
    return best, best_at


def session_links(graph, *, include_scaffold=False) -> dict:
    """id -> [(group, other_ids)] for annotation: which artifacts this session
    shares and with whom, strongest first (PR > branch > file, then bigger groups).
    Scaffold-only file groups are dropped — 'we both touched MEMORY.md' isn't a
    work link (see `group_weight`)."""
    from .graph import group_weight, is_scaffold_group

    links: dict = {r: [] for r in graph.records}
    for grp in graph.groups:
        if not include_scaffold and is_scaffold_group(grp):
            continue
        for m in grp.members:
            others = [o for o in grp.members if o != m]
            if others:
                links.setdefault(m, []).append((grp, others))
    for m in links:
        links[m].sort(key=lambda go: (go[0].precedence, -group_weight(go[0])))
    return links
