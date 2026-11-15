"""claude-reel CLI — a session (or the tail you missed) as an HTML slideshow.

claude-reel <id | substr | path.jsonl>   # write a deck, print its path
claude-reel <id> --tail 30 --open        # last 30 beats, open in the browser
claude-reel <id> --since 2026-07-05       # only what landed on/after that date
claude-reel <id> --reel                   # dump the reel (pivot format) instead
claude-reel <id> --subagents brief        # agent chips expand to the agent's run
claude-reel <id> --context none           # hide what the harness injected
"""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import __version__, sessions
from .curate import curate
from .extract import extract
from .render_html import render


def _filter_beats(beats, tail, since):
    out = beats
    if since:
        cut10 = len(since) == 10
        out = [b for b in out if not b.get("ts") or (b["ts"][:10] if cut10 else b["ts"]) >= since]
    if tail:
        # Count the conversation, not the injected context riding along with it: a
        # token-budget beat lands on every request and would crowd out the work.
        kept = 0
        for i in range(len(out) - 1, -1, -1):
            if out[i]["kind"] != "context":
                kept += 1
                if kept == tail:
                    out = out[i:]
                    break
    return out


def _dump_reel(reel, out_path):
    lines = [json.dumps({"meta": reel["meta"]})]
    lines += [json.dumps(b, ensure_ascii=False) for b in reel["beats"]]
    text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        print(out_path)
    else:
        sys.stdout.write(text)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-reel", description="Render a Claude session as a slideshow."
    )
    p.add_argument("target", help="session id / substring, or a path to a transcript .jsonl")
    p.add_argument(
        "--tail", type=int, metavar="N", help="only the last N beats (catch up on the tail)"
    )
    p.add_argument("--since", metavar="DATE", help="only beats on/after DATE (YYYY-MM-DD or ISO)")
    p.add_argument(
        "--density",
        choices=("jazz", "firehose", "tldr"),
        default="jazz",
        help="jazz: chips folded (default) · firehose: chips open · tldr: agenda only",
    )
    p.add_argument(
        "--context",
        choices=("chips", "none"),
        default="chips",
        help="chips: injected context (budget, reminders, system prompt) as muted chips,"
        " repeats pointing back to their first slide (default) · none: hide it",
    )
    p.add_argument(
        "--subagents",
        choices=("none", "brief"),
        default="none",
        help="none: agent chips name the agent (default) · brief: place every subagent at"
        " its launch; its chip shows prompt, tools, result, and the command for its deck",
    )
    p.add_argument("-o", "--out", metavar="PATH", help="output file (default: ./<id>.reel.html)")
    p.add_argument("--open", action="store_true", help="open the deck in your browser")
    p.add_argument(
        "--reel", action="store_true", help="dump the reel (pivot JSONL) instead of a deck"
    )
    p.add_argument("--version", action="version", version=f"claude-reel {__version__}")
    args = p.parse_args(argv)

    path = sessions.resolve_session(args.target)
    if path is None:
        print(f"claude-reel: no session matching '{args.target}'", file=sys.stderr)
        return 1

    reel = extract(path, context=args.context == "chips", subagents=args.subagents)
    reel["beats"] = _filter_beats(reel["beats"], args.tail, args.since)

    if args.reel:
        _dump_reel(reel, args.out)
        return 0

    deck = curate(reel, density=args.density)
    out = Path(args.out) if args.out else Path.cwd() / f"{reel['meta']['session_id'][:8]}.reel.html"
    out.write_text(render(deck), encoding="utf-8")
    print(out)
    if args.open:
        webbrowser.open(f"file://{out.resolve()}")
    return 0
