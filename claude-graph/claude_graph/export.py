"""One canonical JSON document for the whole graph.

Both a `--json` terminal dump and any future frontend consume this same shape — the
renderers and this exporter are two views over one model, so a UI never needs to
re-derive lanes, links, or concurrency from transcripts. Session content (quotes,
descriptions) is data here, not markup; consumers do their own escaping.
"""

import json

from .graph import group_weight, is_scaffold_group
from .timeline import assign_lanes, peak_concurrency, segments_overlap


def _session_dict(rec, lane):
    return {
        "id": rec.id,
        "project": rec.repos[0].rsplit("/", 1)[-1] if rec.repos else None,
        "repos": rec.repos,
        "prs": rec.prs,
        "branches": rec.branches,
        "files": rec.files,
        "start": rec.start or None,
        "end": rec.end or None,
        "segments": [list(s) for s in rec.segments],
        "turns": rec.turns,
        "first_user": rec.first_user or None,
        "lane": lane,
        "origin": rec.origin or None,
        "subagents": [
            {"agent_type": s.get("agentType"), "description": s.get("description")}
            for s in rec.subagents.values()
        ],
    }


def graph_to_doc(graph, *, seed=None, scope=None, query=None, window_total=None) -> dict:
    """The export document. With `seed` (one id) or `scope` (a set of ids), scoped to
    those sessions' cluster — them plus every session sharing a group with them; a
    `file <substr>` lookup passes its matches as `scope` and echoes the pattern as
    `query`. `window_total` reports how many sessions matched the window before any
    `-n` cap, so consumers can tell a full picture from a truncated one."""
    records = dict(graph.records)
    groups = list(graph.groups)
    if seed is not None:
        scope = {seed}
    if scope is not None:
        groups = [g for g in groups if scope & set(g.members)]
        keep = set(scope) | {m for g in groups for m in g.members}
        records = {i: r for i, r in records.items() if i in keep}

    recs = list(records.values())
    lanes = assign_lanes(recs)

    # Pairwise session edges, artifacts accumulated across every shared group.
    pair_artifacts: dict = {}  # (a, b) sorted -> {(kind, value), ...}
    for grp in groups:
        members = [m for m in grp.members if m in records]
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                pair_artifacts.setdefault((a, b), set()).update(grp.keys)
    edges = [
        {
            "a": a,
            "b": b,
            "artifacts": sorted([k, v] for k, v in arts),
            "concurrent": segments_overlap(records[a], records[b]),
        }
        for (a, b), arts in sorted(pair_artifacts.items())
    ]

    peak, peak_at = peak_concurrency(recs)
    return {
        "version": 1,
        "seed": seed,
        "query": query,
        "window_total": window_total,
        "sessions": [_session_dict(r, lanes.get(r.id, 0)) for r in recs],
        "groups": [
            {
                "keys": [[k, v] for k, v in g.keys],
                "members": g.members,
                "weight": round(group_weight(g), 2),
                "scaffold": is_scaffold_group(g),
            }
            for g in groups
        ],
        "edges": edges,
        # One pane's sessions across /clear: child -> root. The root may sit
        # outside the window, or be gone from disk; the pointer is still true.
        "lineage": [{"session": r.id, "cleared_from": r.origin} for r in recs if r.origin],
        "peak_concurrency": {
            "count": peak,
            "at": peak_at.isoformat() if peak_at else None,
        },
        "linked_sessions": len({i for pair in pair_artifacts for i in pair}),
    }


def dump_doc(doc, console) -> None:
    """Print the doc as JSON. Uses the raw stream (not rich) so output is clean for
    pipes regardless of color mode."""
    console.file.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
