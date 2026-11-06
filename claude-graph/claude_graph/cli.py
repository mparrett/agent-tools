"""claude-graph CLI — timeline, scan, and seed views over the session graph.

    claude-graph [window]           timeline: what happened when (default ~7d)
    claude-graph <id>               seed: that session's cluster, session-rooted
    claude-graph file <substr>      which sessions edited files matching this
    claude-graph scan [window]      the artifact-group map (the old default)
    claude-graph json [window|id]   the canonical JSON document

window: today | 7d | 2026-06-30   ·   also -d/--days, --since, -p/--project, -n
`--json` on any view dumps the same document (seed-scoped for a seed).
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime

from rich.console import Console

from . import __version__, sessions
from .export import dump_doc, graph_to_doc
from .graph import build_session_graph, load_session_record, parse_ts
from .render import render_file_lookup, render_graph_tree, render_seed, render_timeline

MODES = ("timeline", "scan", "seed", "json", "file")

_WINDOW_RE = re.compile(r"\d+d|today|\d{4}-\d{2}-\d{2}|.+\.\..+")


def _looks_like_window(tok: str) -> bool:
    return bool(tok) and bool(_WINDOW_RE.fullmatch(tok))


def _cutoff_ts(token, days, since, default_days):
    """Unix cutoff for the mtime prefilter. Precedence: --since, --days, a positional
    window token, then the mode's default."""
    now = time.time()
    if since:
        return datetime.fromisoformat(since).timestamp()
    if days:
        return now - days * 86400
    if token:
        if re.fullmatch(r"\d+d", token):
            return now - int(token[:-1]) * 86400
        if token == "today":
            return now - 86400
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            return datetime.fromisoformat(token).timestamp()
    return None if default_days is None else now - default_days * 86400


def _load_window(cutoff, project, limit, *, activity_cutoff=None):
    """Records for sessions modified since `cutoff` (None = whole store), capped at
    `limit` (None = uncapped). The mtime prefilter keeps a scan from parsing the whole
    store; a seed passes cutoff=None to follow edges past any window.

    Candidates are mtime-sorted across ALL projects before the cap so `-n` means "the
    N most recently active sessions" — per-project iteration order used to let one
    busy project starve the rest out of a capped window.

    `activity_cutoff` re-checks the parsed transcript's own last timestamp: session
    files get rewritten (compaction, re-summarization) long after their last real
    activity, so mtime over-admits. The check runs BEFORE a record counts toward
    `limit` — a stale rewrite must not eat a cap slot that a genuinely recent
    session needed. Returns (records, total candidates) so callers can say when the
    cap truncated; discovered-stale candidates are excluded from the total."""
    candidates = []
    for sf in sessions.iter_session_files():
        try:
            mt = sf.stat().st_mtime
        except OSError:
            continue
        if cutoff is not None and mt < cutoff:
            continue
        if project and project.lower() not in sf.parent.name.lower():
            continue
        candidates.append((mt, sf))
    candidates.sort(key=lambda c: c[0], reverse=True)
    recs, stale = [], 0
    for _, sf in candidates:
        rec = load_session_record(sf)
        if not rec:
            continue
        if activity_cutoff is not None:
            t = parse_ts(rec.end) or parse_ts(rec.start)
            if t is not None and t.timestamp() < activity_cutoff:
                stale += 1
                continue
        recs.append(rec)
        if limit and len(recs) >= limit:
            break
    return recs, len(candidates) - stale


def _resolve_seed(idsub):
    for sf in sessions.iter_session_files():
        if idsub in sf.stem:
            return sf
    return None


def _truncation_note(console, shown, total):
    if total > shown:
        console.print(
            f"(showing the {shown} most recent of {total} sessions in window · raise with -n)",
            style="dim",
            markup=False,
        )


def _cmd_timeline(cutoff, project, limit, show_subagents, console, compact=False, as_json=False):
    # The timeline is about when work *happened*, so the load re-checks each
    # transcript's own last timestamp against the window (see _load_window).
    recs, total = _load_window(cutoff, project, limit, activity_cutoff=cutoff)
    graph = build_session_graph(recs)
    if as_json:
        dump_doc(graph_to_doc(graph, window_total=total), console)
    else:
        render_timeline(graph, console, show_subagents=show_subagents, compact=compact)
        _truncation_note(console, len(recs), total)
    return 0


def _cmd_scan(
    cutoff,
    project,
    limit,
    show_subagents,
    show_digest,
    console,
    compact=False,
    as_json=False,
    groups=15,
    scaffold=False,
):
    recs, total = _load_window(cutoff, project, limit)
    graph = build_session_graph(recs)
    if as_json:
        dump_doc(graph_to_doc(graph, window_total=total), console)
    else:
        render_graph_tree(
            graph,
            console,
            show_subagents=show_subagents,
            show_digest=show_digest,
            compact=compact,
            limit=groups,
            show_scaffold=scaffold,
        )
        _truncation_note(console, len(recs), total)
    return 0


def _cmd_file(
    pattern, cutoff, project, limit, show_subagents, console, compact=False, as_json=False
):
    # Like a seed, a file lookup follows the artifact past any window by default —
    # the session that edited it may be months old. Bound with --days/--since.
    scope = "recent sessions" if cutoff else "the whole store"
    print(f"claude-graph: scanning {scope} for editors of '{pattern}'…", file=sys.stderr)
    recs, _ = _load_window(cutoff, project, None)
    graph = build_session_graph(recs)
    pat = pattern.lower()
    hits: dict = {}  # canonical file key -> [records that edited it]
    for r in recs:
        for f in r.files:
            if pat in f.lower():
                hits.setdefault(f, []).append(r)
    # Session scratchpads and /tmp copies mostly duplicate the real artifact's row.
    # Hidden unless the pattern itself reaches for them — no flag to remember.
    wants_temp = "tmp" in pat or "scratch" in pat
    temp_hidden = 0
    if not wants_temp:
        temp = [k for k in hits if k.startswith(("/tmp/", "/private/tmp/", "/var/folders/"))]
        temp_hidden = len(temp)
        for k in temp:
            del hits[k]
    if as_json:
        matched = {r.id for rl in hits.values() for r in rl}
        dump_doc(graph_to_doc(graph, scope=matched, query=pattern), console)
        return 0 if hits else 1  # same miss semantics as the rendered view
    render_file_lookup(
        graph, pattern, hits, console, show_subagents=show_subagents, limit=limit, compact=compact
    )
    if temp_hidden:
        console.print(
            f"+{temp_hidden} scratch/temp matches hidden (put 'tmp' or 'scratch' in the "
            "pattern to include)",
            style="dim",
            markup=False,
        )
    return 0 if hits else 1


def _cmd_seed(idsub, cutoff, project, limit, show_subagents, console, compact=False, as_json=False):
    seed_file = _resolve_seed(idsub)
    if seed_file is None:
        console.print(f"no session matching '{idsub}'", style="dim", markup=False)
        return 1
    # A seed follows shared-artifact edges past any window, so scan the whole store (or
    # the user's --days/--since bound) — older co-members must not be missed. No load
    # cap: -n caps what's shown per group, not what's discovered.
    scope = "recent sessions" if cutoff else "the whole store"
    print(f"claude-graph: scanning {scope} for {seed_file.stem[:8]}'s cluster…", file=sys.stderr)
    recs, _ = _load_window(cutoff, project, None)
    if seed_file.stem not in {r.id for r in recs}:  # e.g. an unparseable window pass
        seed_rec = load_session_record(seed_file)
        if seed_rec:
            recs.append(seed_rec)
    graph = build_session_graph(recs)
    if as_json:
        dump_doc(graph_to_doc(graph, seed=seed_file.stem), console)
        return 0
    render_seed(
        graph, seed_file.stem, console, show_subagents=show_subagents, limit=limit, compact=compact
    )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-graph",
        description="Discover and visualize how Claude Code sessions interconnect.",
    )
    p.add_argument(
        "target",
        nargs="*",
        help="optionally a view (timeline·scan·seed·json), then a session id/substring "
        "to seed on or a window (today, 7d, DATE)",
    )
    p.add_argument("-p", "--project", help="only sessions whose project dir matches this substring")
    p.add_argument("-d", "--days", type=int, help="window size in days")
    p.add_argument("--since", help="window start date (YYYY-MM-DD)")
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=None,
        help="cap loaded sessions to the N most recent (default: no cap; the window bounds it)",
    )
    p.add_argument(
        "--subagents", action="store_true", help="expand subagents instead of collapsing"
    )
    p.add_argument("--no-digest", action="store_true", help="skip the scan's digest header")
    p.add_argument(
        "-g",
        "--groups",
        type=int,
        default=15,
        help="max groups the scan draws (0 = all; default 15)",
    )
    p.add_argument(
        "--scaffold",
        action="store_true",
        help="include scaffolding-file groups (MEMORY.md, handoffs, …) in the scan",
    )
    p.add_argument(
        "--json", action="store_true", help="emit the canonical JSON document instead of rendering"
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="keep each session's quote inline instead of on its own line",
    )
    p.add_argument("--no-color", action="store_true", help="plain output (alias for --color never)")
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize output; 'always' keeps color through a pager (| less -R)",
    )
    p.add_argument("--version", action="version", version=f"claude-graph {__version__}")
    args = p.parse_args(argv)

    # rich strips ANSI when stdout isn't a tty, so a plain `| less` loses color. --color
    # always forces it on (page with `less -R`); --no-color stays as a never alias.
    # highlight=False: we do our own kind/tag coloring; rich's auto-highlighter would
    # otherwise tint every number/quote/paren and fight it (only visible once forced on).
    # width: rich can't size a piped stdout and falls back to 80 — take the real
    # terminal from stderr so `| less -R` wraps at the window, not at 80 columns.
    width = None
    if not sys.stdout.isatty():
        try:
            width = os.get_terminal_size(sys.stderr.fileno()).columns
        except (OSError, ValueError):
            pass
    color_mode = "never" if args.no_color else args.color
    if color_mode == "never":
        console = Console(no_color=True, highlight=False, width=width)
    elif color_mode == "always":
        console = Console(force_terminal=True, highlight=False, width=width)
    else:
        console = Console(highlight=False, width=width)
    # Positional grammar, back-compat with v1's single target: an optional view name,
    # then an optional window-or-id. A bare id still seeds; a bare window still maps
    # to the default view (now the timeline).
    tokens = list(args.target)
    mode = tokens.pop(0) if tokens and tokens[0] in MODES else None
    token = tokens.pop(0) if tokens else None
    if tokens:
        p.error(f"unexpected extra arguments: {' '.join(tokens)}")
    if mode in ("seed", "file") and token is None:
        p.error(f"{mode} needs a {'session id/substring' if mode == 'seed' else 'path substring'}")
    if mode is None:
        seed = token if (token and not _looks_like_window(token)) else None
        mode = "seed" if seed else "timeline"
    elif mode == "json" and token and not _looks_like_window(token):
        mode, args.json = "seed", True  # `json <id>` = the seed-scoped document
    seed = token if mode == "seed" else None
    as_json = args.json or mode == "json"
    if mode == "json":
        mode = "timeline"

    # Seed and file lookups are unbounded by default (they follow the artifact past
    # any window) unless the user bounds them with --days/--since; timeline and scan
    # default to a 7-day window.
    default_days = None if mode in ("seed", "file") else 7
    window_token = None if mode in ("seed", "file") else token
    cutoff = _cutoff_ts(window_token, args.days, args.since, default_days)

    try:
        if mode == "file":
            return _cmd_file(
                token,
                cutoff,
                args.project,
                args.count,
                args.subagents,
                console,
                args.compact,
                as_json,
            )
        if mode == "seed":
            return _cmd_seed(
                seed,
                cutoff,
                args.project,
                args.count,
                args.subagents,
                console,
                args.compact,
                as_json,
            )
        if mode == "scan":
            return _cmd_scan(
                cutoff,
                args.project,
                args.count,
                args.subagents,
                not args.no_digest,
                console,
                args.compact,
                as_json,
                args.groups,
                args.scaffold,
            )
        return _cmd_timeline(
            cutoff, args.project, args.count, args.subagents, console, args.compact, as_json
        )
    except KeyboardInterrupt:
        return 130
