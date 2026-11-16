"""Credits: the closing card. Who else was in this session, and which sessions it
belongs among.

Two sources. The **cast** (subagents) comes from the transcript's own subagent pool, so
it needs nothing else. The **family** (sessions before and after this one, linked by
/clear, handoffs, and mentions) and the compaction history come from
`claude-ls lineage --json`, run as a subprocess: claude-reel stays stdlib-only, and
lineage's store-wide mention scan is not worth re-implementing. Without claude-ls on
PATH, or if it fails, the card shows the cast alone.
"""

import json
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path

# The mention scan reads the whole store; it took ~13s on one machine as of 2026-11-15.
LINEAGE_TIMEOUT_S = 90


def fetch_lineage(path: Path) -> dict | None:
    exe = shutil.which("claude-ls")
    if not exe:
        return None
    print("claude-reel: reading lineage for the credits (--no-credits skips)…", file=sys.stderr)
    try:
        proc = subprocess.run(
            [exe, "lineage", str(path), "--json", "--no-subagents"],
            capture_output=True,
            text=True,
            timeout=LINEAGE_TIMEOUT_S,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        data = None
    if data is None:
        print("claude-reel: lineage unavailable; credits show the cast only", file=sys.stderr)
    return data if isinstance(data, dict) else None


def _walk(start: str, adj: dict) -> dict:
    """sid -> (hops from start, the kind of the edge on the hop nearest start)."""
    out = {}
    q = deque([(start, 0, None)])
    seen = {start}
    while q:
        node, hops, kind = q.popleft()
        for nxt, k in adj.get(node, ()):
            if nxt in seen:
                continue
            seen.add(nxt)
            first = kind or k
            out[nxt] = (hops + 1, first)
            q.append((nxt, hops + 1, first))
    return out


def family(lineage: dict | None, seed_sid: str) -> dict:
    """Lineage -> {"previously", "sequels", "related", "compactions"}.

    Previously: sessions this one descends from. Sequels: sessions descending from
    it. Related: the rest of the family (a predecessor's other sequels). Each row
    is labelled with the link nearest this session."""
    empty = {"previously": [], "sequels": [], "related": [], "compactions": []}
    if not lineage:
        return empty
    sessions = {s["sid"]: s for s in lineage.get("sessions") or [] if s.get("sid")}
    # lineage names its own seed (the file it was given); a transcript's sessionId can
    # be another node of the family, e.g. in a resumed file, so it's only the fallback.
    seed = lineage.get("seed")
    if seed not in sessions:
        seed = next((sid for sid in sessions if sid.startswith(seed_sid)), None)
    if seed not in sessions:
        return empty
    fwd, back = {}, {}
    for e in lineage.get("edges") or []:
        fwd.setdefault(e["src"], []).append((e["dst"], e.get("kind") or "link"))
        back.setdefault(e["dst"], []).append((e["src"], e.get("kind") or "link"))
    up, down = _walk(seed, back), _walk(seed, fwd)
    rest = [s for s in sessions if s != seed and s not in up and s not in down]

    def row(sid, hops, kind):
        s = sessions[sid]
        return {
            "sid": sid,
            "hops": hops,
            "kind": kind,
            "topic": s.get("topic") or "",
            "start": s.get("start"),
            "end": s.get("end"),
            "where": Path(s.get("cwd") or "").name,
            "branch": s.get("branch"),
        }

    def ordered(rows):
        return sorted(rows, key=lambda r: r["start"] or "")

    return {
        "previously": ordered(row(s, *up[s]) for s in up),
        "sequels": ordered(row(s, *down[s]) for s in down),
        "related": ordered(_related(seed, rest, fwd, back, row)),
        "compactions": sessions[seed].get("compactions") or [],
    }


def _related(seed, rest, fwd, back, row):
    """Family members that are neither ancestors nor descendants (a predecessor's other
    sequel, a sequel's other parent). An undirected walk from this session gives each
    its real distance and the link that reaches it: `via` is the neighbour on that path,
    and `direction` says whether the link runs from it ("from") or to it ("to")."""
    rest = set(rest)
    adj = {}
    for a, outs in fwd.items():
        for b, k in outs:
            adj.setdefault(a, []).append((b, k, "from"))  # b was reached from a
            adj.setdefault(b, []).append((a, k, "to"))  # a links to b
    out, seen, q = [], {seed}, deque([(seed, 0)])
    while q:
        node, hops = q.popleft()
        for nxt, kind, direction in adj.get(node, ()):
            if nxt in seen:
                continue
            seen.add(nxt)
            q.append((nxt, hops + 1))
            if nxt in rest:
                r = row(nxt, hops + 1, kind)
                r["via"], r["direction"] = node, direction
                out.append(r)
    return out


def cast(pool_meta: list, agent_slides: dict) -> list:
    """Subagents grouped by type, then by what they were asked, most frequent first.
    Each group lists the slides its agents appear on (empty without --subagents brief
    for forked skills, which have no tool call to sit on)."""
    groups: dict = {}
    for a in pool_meta:
        key = (a.get("type") or "agent", _role(a.get("description") or ""))
        g = groups.setdefault(key, {"type": key[0], "role": key[1], "count": 0, "slides": []})
        g["count"] += 1
        if a["id"] in agent_slides:
            g["slides"].append(agent_slides[a["id"]])
    out = sorted(groups.values(), key=lambda g: (-g["count"], g["type"], g["role"]))
    for g in out:
        g["slides"] = sorted(set(g["slides"]))
    return out


def _role(description: str) -> str:
    """What an agent was asked, coarse enough that repeats group: a slash command
    keeps only its name (`/code-review medium 15` -> `/code-review`)."""
    d = description.strip()
    if d.startswith("/"):
        return d.split()[0]
    return d
