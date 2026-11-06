"""Render a SessionGraph as an ASCII tree.

- timeline (default): every session in the window as a chronological row, newest
  first, with a git-log-graph lane gutter showing wall-clock concurrency (● last
  active, │ also-working-then) and shared-artifact links as row annotations.
- scan: artifact-anchored groups (PR/branch/file), ranked by shared-session weight, each member showing
  multi-membership (`↔ also …`) and cross-repo (`⇄`) annotations, with subagents
  nested (or collapsed to `+N subagents`).
- seed: session-rooted — the seed session is the subject, and the groups it belongs
  to organize underneath it (it is never renamed to "PR #…").

Output is plain text printed through a rich Console with per-line styles and
`markup=False`, so session content can't inject console markup. Pass a
`Console(no_color=True, file=…)` to capture plain text in tests.
"""

from rich.text import Text

from .graph import (
    disambiguate_basenames,
    group_label,
    group_label_segments,
    group_weight,
    sprawl_score,
)

# Color by artifact kind so group boundaries pop out of the wall; tag colors so the
# cross-cutting rows (multi-repo, shared-with) stand out. Kept here (a render concern),
# not in graph.py which only knows structure.
KIND_STYLE = {"pr": "bright_green", "branch": "bright_magenta", "file": "bright_cyan"}
TREE_STYLE = "yellow"  # ├── └── │ box-drawing glyphs + the ⇄ / ↔ tag symbols
TAG_STYLE = "grey62"  # muted body text of the ⇄ repos / ↔ also tags
TIME_STYLE = "cyan"  # timestamps and date-range spans


def _common_groups(others_per):
    """Groups present (by identity) in *every* member's other-group list — the shared
    `↔ also` edges that lift to the header instead of repeating on each row."""
    if not others_per:
        return []
    common = []
    for o in others_per[0]:
        if any(o is x for x in common):  # dedup within the first member's list
            continue
        if all(any(o is x for x in lst) for lst in others_per[1:]):
            common.append(o)
    return common


def _header_text(grp, file_display):
    """Group header artifact, each kind segment colored by KIND_STYLE."""
    t = Text()
    for i, (kind, seg) in enumerate(group_label_segments(grp, file_display=file_display)):
        if i:
            t.append(" · ")
        t.append(seg, style=KIND_STYLE.get(kind, ""))
    return t


def _file_display(graph):
    """Disambiguation map for file labels: same basename in different repos gets a
    distinguishing path prefix. Computed over the whole graph so a file reads the same
    in every view."""
    return disambiguate_basenames(v for grp in graph.groups for k, v in grp.keys if k == "file")


BR_MID, BR_END, VERT, GAP = "├──", "└──", "│   ", "    "


def _base(p):
    return p.rsplit("/", 1)[-1] if p else "?"


def _when(iso):
    return iso[5:16].replace("T", " ") if iso else "?"  # MM-DD HH:MM


def _trunc(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def _project(rec):
    return _base(rec.repos[0]) if rec.repos else "?"


def _date_range(records):
    starts = [r.start[:10] for r in records if r.start]
    if not starts:
        return ""
    ends = [r.end[:10] for r in records if r.end] or starts
    return f"{min(starts)[5:]} → {max(ends)[5:]}"  # MM-DD → MM-DD


def _member_text(rec, glyph, *, also=(), hide_project=False):
    t = Text()
    t.append(glyph, style=TREE_STYLE)
    t.append(f" {rec.id[:8]}  ")
    t.append(_when(rec.start), style=TIME_STYLE)
    if not hide_project:  # dropped when the whole group shares one project (lifted to header)
        t.append(f"  ({_project(rec)})")
    t.append(f"  ~{rec.turns}t")
    if len(rec.repos) > 1:
        t.append("   ")
        t.append("⇄", style=TREE_STYLE)
        t.append(f" {len(rec.repos)} repos", style=TAG_STYLE)
    if also:
        body = "also " + "; ".join(also[:2]) + (" …" if len(also) > 2 else "")
        t.append("   ")
        t.append("↔", style=TREE_STYLE)
        t.append(" " + body, style=TAG_STYLE)
    return t


def _emit_member(
    console,
    rec,
    glyph,
    cont,
    *,
    prefix="",
    also=(),
    show_subagents=False,
    compact=False,
    hide_project=False,
):
    """A member row plus its subagents (nested if show_subagents, else '+N' inline).
    The first-user quote goes on its own indented line (rows nearly always wrap
    otherwise); --compact keeps it inline on the member row."""
    line = _member_text(rec, glyph, also=also, hide_project=hide_project)
    if compact and rec.first_user:
        line.append(f'   "{_trunc(rec.first_user, 48)}"')
    subs = list(rec.subagents.values())
    if subs and not show_subagents:
        line.append(f"   +{len(subs)} subagents")
    if prefix:  # prefix is tree bars + spaces (seed view) — color only the bars
        p = Text()
        p.append(prefix, style=TREE_STYLE)
        p.append_text(line)
        line = p
    console.print(line, markup=False)
    if not compact and rec.first_user:
        q = Text()
        q.append(f"{prefix}{cont}", style=TREE_STYLE)
        q.append(f'"{_trunc(rec.first_user, 84)}"', style="dim")
        console.print(q, markup=False)
    if subs and show_subagents:
        for i, s in enumerate(subs):
            g = BR_END if i == len(subs) - 1 else BR_MID
            atype = s.get("agentType") or "agent"
            desc = _trunc(s.get("description") or "", 44)
            tail = f': "{desc}"' if desc else ""
            st = Text()
            st.append(f"{prefix}{cont}{g}", style=TREE_STYLE)
            st.append(f" ⌁ {atype}{tail}", style="dim")
            console.print(st, markup=False)


def _footer(console, graph, n_groups):
    console.print("")
    console.print(f"{len(graph.records)} sessions · {n_groups} groups", style="dim", markup=False)
    console.print(
        "narrow with  -p <project>  ·  --since <date>  ·  -n <count>", style="dim", markup=False
    )


def render_digest(graph, ordered, console, file_display=None):
    """A short TL;DR above the scan. The ranked list already shows the hub clusters,
    so the digest adds the two lenses it doesn't: the session that *connects* the most
    clusters (degree), and the most cross-cutting one (sprawl). Lead cluster is named
    for orientation."""
    recs = graph.records
    degree = {}  # session id -> how many groups it belongs to (over the whole graph)
    for grp in graph.groups:
        for m in grp.members:
            degree[m] = degree.get(m, 0) + 1

    def row(tag, body):
        console.print(f"  {tag:15}{body}", style="dim", markup=False)

    def quote(rec):
        return f'   "{_trunc(rec.first_user, 40)}"' if rec.first_user else ""

    console.print("digest", style="bold", markup=False)

    lead = ordered[0]
    lrecs = [recs[m] for m in lead.members if m in recs]
    dr = _date_range(lrecs)
    row(
        "lead cluster",
        f"{group_label(lead, file_display=file_display)} · {len(lead.members)} sessions"
        + (f" · {dr}" if dr else ""),
    )

    # top connector: the session in the most groups (ties -> most recent)
    if degree:
        cid = max(degree, key=lambda i: (degree[i], recs[i].start if i in recs else ""))
        r = recs.get(cid)
        if r and degree[cid] > 1:
            row("top connector", f"{cid[:8]} in {degree[cid]} clusters ({_project(r)}){quote(r)}")

    # widest session: highest sprawl (cross-repo breadth dominant)
    if recs:
        w = max(recs.values(), key=lambda r: (sprawl_score(r), len(r.repos), len(r.files)))
        if sprawl_score(w) > 0:
            row(
                "widest session",
                f"{w.id[:8]} {len(w.repos)} repos · {len(w.files)} files ({_project(w)}){quote(w)}",
            )
    console.print("")


def render_graph_tree(
    graph,
    console,
    *,
    show_subagents=False,
    limit=15,
    show_digest=True,
    compact=False,
    show_scaffold=False,
):
    """Scan view: artifact groups, ranked by group_weight (shared sessions × kind).
    Scaffold-only file groups ("we both touched MEMORY.md") are hidden unless
    `show_scaffold`; `limit` caps the groups drawn (0/None = all) — on a busy store
    the tail buries the few clusters that matter."""
    from .graph import is_scaffold_group

    groups_of = {}  # session id -> [groups it's in] (for '↔ also')
    for grp in graph.groups:
        for m in grp.members:
            groups_of.setdefault(m, []).append(grp)

    def gsort(grp):
        recs = [graph.records[m] for m in grp.members if m in graph.records]
        newest = max((r.start for r in recs if r.start), default="")
        return (-group_weight(grp), newest)  # weight already folds in session count

    candidates = [g for g in graph.groups if show_scaffold or not is_scaffold_group(g)]
    n_scaffold = len(graph.groups) - len(candidates)
    ordered = sorted(candidates, key=gsort)
    hidden = max(0, len(ordered) - limit) if limit else 0
    if limit:
        ordered = ordered[:limit]
    if not ordered:
        console.print("no linked sessions in this window.", style="dim", markup=False)
        _footer(console, graph, 0)
        return

    fd = _file_display(graph)
    if show_digest and len(ordered) >= 2:
        render_digest(graph, ordered, console, file_display=fd)

    for gi, grp in enumerate(ordered):
        recs = sorted(
            (graph.records[m] for m in grp.members if m in graph.records),
            key=lambda r: r.start or "",
        )
        # Fields shared by *every* member lift to the header instead of repeating per
        # row: a single project, and the `↔ also` edges common to all members.
        others_per = [[o for o in groups_of.get(r.id, []) if o is not grp] for r in recs]
        common = _common_groups(others_per)
        projects = {_project(r) for r in recs}
        lift_project = next(iter(projects)) if len(projects) == 1 else None

        header = _header_text(grp, fd)
        header.append(f"  ·  {len(grp.members)} sessions")
        dr = _date_range(recs)
        if dr:
            header.append("  ·  ")
            header.append(dr, style=TIME_STYLE)
        if lift_project:
            header.append("  ·  ")
            header.append(lift_project, style="dim")
        if common:
            labels = [group_label(o, file_display=fd) for o in common]
            header.append("  ·  ")
            header.append(
                "also " + "; ".join(labels[:3]) + (" …" if len(labels) > 3 else ""),
                style=TAG_STYLE,
            )
        console.print(header, style="bold", markup=False)
        for i, rec in enumerate(recs):
            last = i == len(recs) - 1
            also = [
                group_label(o, file_display=fd)
                for o in groups_of.get(rec.id, [])
                if o is not grp and not any(o is c for c in common)
            ]
            _emit_member(
                console,
                rec,
                BR_END if last else BR_MID,
                GAP if last else VERT,
                also=also,
                show_subagents=show_subagents,
                compact=compact,
                hide_project=bool(lift_project),
            )
        if gi != len(ordered) - 1:
            console.print("")
    _footer(console, graph, len(ordered))
    tail = []
    if hidden:
        tail.append(f"+{hidden} more groups (-g 0 shows all)")
    if n_scaffold:
        tail.append(f"{n_scaffold} scaffolding-file groups hidden (--scaffold)")
    if tail:
        console.print(" · ".join(tail), style="dim", markup=False)


# Timeline lanes are colored by *project identity* (a fourth color meaning, beside
# kind/structure/time): one stable color per project so interleaved work reads as
# threads. 256-color names chosen to stay clear of the kind colors above.
PROJECT_PALETTE = (
    "bright_blue",
    "orange1",
    "medium_purple1",
    "gold3",
    "turquoise2",
    "light_coral",
    "chartreuse3",
    "plum2",
)


def _local(iso):
    """Store timestamps are UTC; the timeline is about the human's day, so display
    and day-bucketing use local time."""
    from .graph import parse_ts

    t = parse_ts(iso)
    return t.astimezone() if t else None


def _dur_text(td) -> str:
    m = int(td.total_seconds() // 60)
    if m < 1:
        return "<1m"
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if h else f"{d}d"


def _active_dur(rec) -> str:
    """Summed activity-segment time — 'how much work', not wall span (a session
    resumed across three days isn't a 3-day session)."""
    from datetime import timedelta

    from .graph import parse_ts

    total = timedelta()
    for s, e in rec.segments:
        s, e = parse_ts(s), parse_ts(e)
        if s and e:
            total += e - s
    return _dur_text(total) if rec.segments else "?"


def _anchor(rec):
    """A row's place on the timeline: last activity (matches the window's
    modified-since semantics, so a June session resumed yesterday reads as
    yesterday's work), falling back to start."""
    return _local(rec.end) or _local(rec.start)


def _gutter(rec, others, lanes, styles, width, *, own_glyph="●"):
    """One row's lane cells: `own_glyph` in this session's lane, │ for every other
    session actively working at this row's anchor moment, colored per project."""
    from .timeline import is_active_at

    at = _anchor(rec)
    cells = [(" ", "")] * width
    if at is not None:
        for o in others:
            if is_active_at(o, at):
                cells[lanes.get(o.id, 0)] = ("│", styles.get(_project(o), ""))
    cells[lanes.get(rec.id, 0)] = (own_glyph, styles.get(_project(rec), ""))
    t = Text()
    for ch, style in cells:
        t.append(ch + " ", style=style)
    return t


def _emit_timeline_row(
    console, rec, recs, lanes, styles, width, links, fd, show_subagents, compact
):
    """One session's timeline row plus its continuation lines (quote, links,
    expanded subagents), all behind the lane gutter."""
    at = _anchor(rec)
    others = [o for o in recs if o.id != rec.id]

    # Same-day sessions show their wall span (start–end); a session resumed
    # across days is timed by its last activity and tagged `since <start>`.
    started = _local(rec.start)
    multiday = started and at and started.date() != at.date()
    line = _gutter(rec, others, lanes, styles, width)
    line.append(f" {rec.id[:8]}  ")
    if at is None:
        line.append("?", style=TIME_STYLE)
    elif multiday:
        line.append(f"{at:%H:%M}", style=TIME_STYLE)
    elif started and (at - started).total_seconds() >= 60:
        line.append(f"{started:%H:%M}–{at:%H:%M}", style=TIME_STYLE)
    else:
        line.append(f"{(started or at):%H:%M}", style=TIME_STYLE)
    line.append(f"  {_active_dur(rec)}", style=TIME_STYLE)
    if multiday:
        line.append(f"  since {started:%m-%d}", style="dim")
    line.append("  ")
    line.append(_project(rec), style=styles.get(_project(rec), ""))
    line.append(f"  ~{rec.turns}t")
    if len(rec.repos) > 1:
        line.append("   ")
        line.append("⇄", style=TREE_STYLE)
        line.append(f" {len(rec.repos)} repos", style=TAG_STYLE)
    if rec.origin:
        line.append("   ")
        line.append("↺", style=TREE_STYLE)
        line.append(f" cleared from {rec.origin[:8]}", style=TAG_STYLE)
    subs = list(rec.subagents.values())
    if subs and not show_subagents:
        line.append(f"   +{len(subs)} subagents")
    if compact and rec.first_user:
        line.append(f'   "{_trunc(rec.first_user, 48)}"')
    console.print(line, markup=False)

    def cont_line():
        return _gutter(rec, others, lanes, styles, width, own_glyph="│")

    if not compact and rec.first_user:
        q = cont_line()
        q.append(f'   "{_trunc(rec.first_user, 84)}"', style="dim")
        console.print(q, markup=False)
    my_links = links.get(rec.id) or []
    if my_links:
        lt = cont_line()
        lt.append("   ")
        lt.append("↔ ", style=TREE_STYLE)
        for i, (grp, others_ids) in enumerate(my_links[:2]):
            if i:
                lt.append(" · ", style=TAG_STYLE)
            for j, (kind, seg) in enumerate(group_label_segments(grp, file_display=fd, max_vals=2)):
                if j:
                    lt.append(" · ")
                lt.append(seg, style=KIND_STYLE.get(kind, ""))
            shown = ", ".join(o[:8] for o in others_ids[:2])
            more = f" +{len(others_ids) - 2}" if len(others_ids) > 2 else ""
            lt.append(f" with {shown}{more}", style=TAG_STYLE)
        if len(my_links) > 2:
            lt.append(f"  +{len(my_links) - 2} more", style=TAG_STYLE)
        console.print(lt, markup=False)
    if subs and show_subagents:
        for s in subs:
            st = cont_line()
            atype = s.get("agentType") or "agent"
            desc = _trunc(s.get("description") or "", 44)
            st.append(f"   ⌁ {atype}" + (f': "{desc}"' if desc else ""), style="dim")
            console.print(st, markup=False)


def render_timeline(graph, console, *, show_subagents=False, compact=False):
    """Timeline view: every loaded session, newest first, day-bucketed, with the
    concurrency lane gutter and shared-artifact link annotations."""
    from .timeline import assign_lanes, peak_concurrency, session_links

    recs = list(graph.records.values())
    if not recs:
        console.print("no sessions in this window.", style="dim", markup=False)
        return
    dated = sorted((r for r in recs if _anchor(r)), key=_anchor)
    rows = list(reversed(dated)) + [r for r in recs if not _anchor(r)]

    lanes = assign_lanes(recs)
    # One column per lane, uncapped: collapsing overflow lanes into a shared column
    # would misdraw exactly the high-concurrency bursts the view exists to show.
    width = max(lanes.values(), default=0) + 1
    # Stable project colors, assigned in row (newest-first) order.
    styles: dict = {}
    for r in rows:
        p = _project(r)
        if p not in styles:
            styles[p] = PROJECT_PALETTE[len(styles) % len(PROJECT_PALETTE)]
    links = session_links(graph)
    fd = _file_display(graph)

    # Digest line: the window at a glance, incl. the concurrency high-water mark.
    peak, peak_at = peak_concurrency(recs)
    head = Text()
    if dated:
        lo, hi = _anchor(dated[0]), _anchor(dated[-1])
        head.append(f"{lo:%m-%d} → {hi:%m-%d}", style=TIME_STYLE)
        head.append(f" · {len(rows)} sessions · {len(styles)} projects")
    else:
        head.append(f"{len(rows)} sessions")
    if peak >= 2 and peak_at:
        head.append(f" · peak {peak} active ")
        head.append(f"({peak_at.astimezone():%m-%d %H:%M})", style=TIME_STYLE)
    console.print(head, style="bold", markup=False)

    day = None
    for rec in rows:
        at = _anchor(rec)
        if at and at.date() != day:
            day = at.date()
            sep = Text()
            sep.append("── ", style=TREE_STYLE)
            sep.append(f"{at:%a %m-%d} ", style="bold cyan")
            sep.append("─" * max(4, 40 - width * 2), style=TREE_STYLE)
            console.print(sep, markup=False)
        _emit_timeline_row(
            console, rec, recs, lanes, styles, width, links, fd, show_subagents, compact
        )

    console.print("")
    legend = Text()
    legend.append("● last active · │ also working then · lanes = wall-clock overlap", style="dim")
    if any(r.origin for r in recs):
        legend.append(" · ↺ /cleared: same pane, same lane", style="dim")
    console.print(legend, markup=False)
    linked = sum(1 for r in rows if links.get(r.id))
    console.print(
        f"{len(rows)} sessions · {linked} linked · seed a session with  claude-graph <id>",
        style="dim",
        markup=False,
    )


def render_file_lookup(
    graph, pattern, hits, console, *, show_subagents=False, limit=None, compact=False
):
    """Artifact lookup: which sessions *edited* files matching `pattern`. `hits` maps
    each matched canonical file key to its editing records. Unlike the scan, a
    single-editor file still prints — finding the one session that touched a file is
    the whole point. Headers show the full canonical path (the store may hold
    same-named files in many repos); members carry their usual `↔` cluster links so
    lineage is visible without a second command."""
    if not hits:
        console.print(f"no session edited a file matching '{pattern}'.", style="dim", markup=False)
        console.print(
            "(matches are against edited paths — mentions in conversation don't count)",
            style="dim",
            markup=False,
        )
        return

    groups_of = {}  # session id -> [groups it's in] (for '↔ also')
    for grp in graph.groups:
        for m in grp.members:
            groups_of.setdefault(m, []).append(grp)
    fd = _file_display(graph)

    def newest(recs):
        return max((r.end or r.start for r in recs), default="")

    n_sessions = len({r.id for recs in hits.values() for r in recs})
    head = Text()
    head.append(f"file '{pattern}'", style=KIND_STYLE["file"])
    head.append(f" · {len(hits)} files · {n_sessions} sessions", style="")
    console.print(head, style="bold", markup=False)
    console.print("")

    for key in sorted(hits, key=lambda k: newest(hits[k]), reverse=True):
        recs = sorted(hits[key], key=lambda r: r.start or "")
        header = Text()
        header.append(key, style=KIND_STYLE["file"])
        header.append(f"  ·  {len(recs)} session{'s' if len(recs) != 1 else ''}")
        console.print(header, style="bold", markup=False)
        shown = recs[:limit] if limit else recs
        for i, rec in enumerate(shown):
            last = i == len(shown) - 1 and len(shown) == len(recs)
            also = [group_label(o, file_display=fd) for o in groups_of.get(rec.id, [])]
            _emit_member(
                console,
                rec,
                BR_END if last else BR_MID,
                GAP if last else VERT,
                also=also,
                show_subagents=show_subagents,
                compact=compact,
            )
        if len(shown) < len(recs):
            mt = Text()
            mt.append(BR_END, style=TREE_STYLE)
            mt.append(f" +{len(recs) - len(shown)} more", style="dim")
            console.print(mt, markup=False)
        console.print("")
    console.print(
        "matches are against edited paths · seed a session with  claude-graph <id>",
        style="dim",
        markup=False,
    )


def render_seed(graph, seed_id, console, *, show_subagents=False, limit=None, compact=False):
    """Session-rooted view: the seed is the subject; its groups organize underneath.
    `limit` caps co-members shown per group (with a `+N more` note), not discovery."""
    seed = graph.records.get(seed_id)
    if seed is None:
        console.print(
            f"seed {seed_id[:8]} not found among loaded sessions.", style="dim", markup=False
        )
        return

    head = Text()
    head.append(f"◆ {seed.id[:8]}  ")
    head.append(_when(seed.start), style=TIME_STYLE)
    head.append(f"  ({_project(seed)})  ~{seed.turns}t")
    if len(seed.repos) > 1:
        head.append("   ")
        head.append("⇄", style=TREE_STYLE)
        head.append(f" {len(seed.repos)} repos", style=TAG_STYLE)
    if seed.first_user:
        head.append(f'   "{_trunc(seed.first_user, 56)}"')
    console.print(head, style="bold", markup=False)
    if seed.subagents:
        console.print(f"  ⌁ {len(seed.subagents)} subagents spawned", style="dim", markup=False)

    my_groups = sorted(
        (g for g in graph.groups if seed_id in g.members),
        key=lambda g: (g.precedence, -len(g.members)),
    )
    if not my_groups:
        console.print(
            "  no shared PR / branch / file with other sessions.", style="dim", markup=False
        )
        _footer(console, graph, 0)
        return

    fd = _file_display(graph)
    console.print("  shares work with:", style="dim", markup=False)
    for gi, grp in enumerate(my_groups):
        glast = gi == len(my_groups) - 1
        gpre = "  " + (GAP if glast else VERT)
        others = sorted(
            (graph.records[m] for m in grp.members if m != seed_id and m in graph.records),
            key=lambda r: r.start or "",
        )
        gh = Text("  ")
        gh.append(BR_END if glast else BR_MID, style=TREE_STYLE)
        gh.append(" ")
        gh.append_text(_header_text(grp, fd))
        gh.append(f"  ·  {len(grp.members)} sessions")
        console.print(gh, markup=False)
        shown = others[:limit] if limit else others
        hidden = len(others) - len(shown)
        for i, rec in enumerate(shown):
            last = i == len(shown) - 1 and hidden == 0
            _emit_member(
                console,
                rec,
                BR_END if last else BR_MID,
                GAP if last else VERT,
                prefix=gpre,
                show_subagents=show_subagents,
                compact=compact,
            )
        if hidden:
            mt = Text()
            mt.append(f"{gpre}{BR_END}", style=TREE_STYLE)
            mt.append(f" +{hidden} more", style="dim")
            console.print(mt, markup=False)
    _footer(console, graph, len(my_groups))
