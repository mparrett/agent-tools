#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["rich"]
# ///
"""Render claude-graph's timeline + scan + seed views over SYNTHETIC data — a showcase
that needs no real session store (and leaks nothing). Handy for docs/screenshots and for
eyeballing the renderer without a populated `~/.claude/projects`.

    uv run claude-graph/tools/demo.py            # print the demo tree
    uv run claude-graph/tools/demo.py --svg out.svg   # also export a terminal-style SVG

All repo/PR/branch/session names below are invented.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make claude_graph importable

from claude_graph.graph import SessionRecord, build_session_graph  # noqa: E402
from claude_graph.render import render_graph_tree, render_seed, render_timeline  # noqa: E402
from rich.console import Console  # noqa: E402


def _rec(id, **kw):
    # One continuous activity segment per session keeps the demo simple; the
    # timeline's lanes and durations come from these.
    if "start" in kw and "end" in kw and "segments" not in kw:
        kw["segments"] = [(kw["start"], kw["end"])]
    return SessionRecord(id=id, **kw)


def demo_graph():
    """A checkout-redesign effort (a PR + a branch, overlapping) plus a separate
    design-system file cluster — enough to show every rendered signal."""
    records = [
        _rec(
            "a1b2c3d4",
            prs=["acme/webapp#412"],
            branches=["feat/checkout-redesign"],
            files=["/w/webapp/src/checkout.tsx"],
            repos=["/w/webapp"],
            start="2026-06-28T14:02:00",
            end="2026-06-28T15:40:00",
            turns=22,
            first_user="wire up the new checkout grid",
            subagents={
                "t1": {"agentType": "Explore", "description": "audit checkout usage across webapp"}
            },
        ),
        _rec(
            "e5f6a7b8",
            prs=["acme/webapp#412"],
            repos=["/w/webapp", "/w/webapp-wt"],
            start="2026-06-29T09:14:00",
            end="2026-06-29T10:02:00",
            turns=8,
            first_user="fix the failing checkout snapshot test",
        ),
        _rec(
            "c9d0e1f2",
            prs=["acme/webapp#412"],
            branches=["feat/checkout-redesign"],
            repos=["/w/webapp"],
            start="2026-06-30T06:32:00",
            end="2026-06-30T07:10:00",
            turns=15,
            first_user="polish the grid spacing",
        ),
        _rec(
            "b7c8d9e0",
            branches=["feat/checkout-redesign"],
            repos=["/w/webapp"],
            # overlaps e5f6a7b8 — the timeline draws them in parallel lanes
            start="2026-06-29T09:40:00",
            end="2026-06-29T10:30:00",
            turns=6,
            first_user="rebase the redesign branch onto main",
        ),
        _rec(
            "11aa22bb",
            files=[
                "/w/design-system/api.ts",
                "/w/design-system/api.test.ts",
                "/w/design-system/README.md",
            ],
            repos=["/w/design-system"],
            start="2026-06-27T08:00:00",
            end="2026-06-27T09:30:00",
            turns=19,
            first_user="document the design-system api surface",
        ),
        _rec(
            "33cc44dd",
            files=[
                "/w/design-system/api.ts",
                "/w/design-system/api.test.ts",
                "/w/design-system/README.md",
            ],
            repos=["/w/design-system"],
            start="2026-06-27T13:00:00",
            end="2026-06-27T14:15:00",
            turns=11,
            first_user="add types to the api module",
            subagents={
                "t2": {"agentType": "general-purpose", "description": "find all api.ts importers"}
            },
        ),
    ]
    return build_session_graph(records)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render claude-graph over synthetic demo data.")
    ap.add_argument("--svg", metavar="PATH", help="also write a terminal-style SVG here")
    args = ap.parse_args(argv)

    graph = demo_graph()
    console = Console(record=True, width=128, highlight=False)
    console.print("$ claude-graph 30d", style="bold green")
    console.print("")
    render_timeline(graph, console)
    console.print("")
    console.print("─" * 124, style="dim")
    console.print("")
    console.print("$ claude-graph scan 30d", style="bold green")
    console.print("")
    render_graph_tree(graph, console, show_subagents=True)
    console.print("")
    console.print("─" * 124, style="dim")
    console.print("")
    console.print("$ claude-graph a1b2c3d4  --subagents", style="bold green")
    console.print("")
    render_seed(graph, "a1b2c3d4", console, show_subagents=True)

    if args.svg:
        Path(args.svg).write_text(console.export_svg(title="claude-graph — demo (synthetic data)"))
        print(f"wrote {args.svg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
