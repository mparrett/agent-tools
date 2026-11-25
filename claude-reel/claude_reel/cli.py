"""claude-reel CLI — a session (or the tail you missed) as an HTML slideshow.

claude-reel <id | substr | path.jsonl>   # write a deck, print its path
claude-reel <id> --tail 30 --open        # last 30 beats, open in the browser
claude-reel <id> --since 2026-07-05       # only what landed on/after that date
claude-reel <id> --reel                   # dump the reel (pivot format) instead
claude-reel <id> --subagents brief        # agent chips expand to the agent's run
claude-reel <id> --context none           # hide what the harness injected
claude-reel <id> --live --open            # follow a running session as it grows
"""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import __version__, credits, sessions
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


def _reply_target(path):
    """(sender, note, check): how to deliver a reply to this session, or why there is
    none, and where to look when a reply never shows up."""
    from . import send

    agent = send.find_agent(path.stem)
    if agent is None:
        return (
            None,
            "Replies need a running background session (claude --bg); this one isn't.",
            None,
        )
    if agent.get("kind") != send.ATTACHABLE:
        return (
            None,
            f"Replies need a background session (claude --bg); this one is {agent.get('kind')}.",
            None,
        )
    return (
        lambda text: send.deliver(agent["id"], text, transcript=path),
        None,
        f"`claude attach {agent['id']}`",
    )


def _live(path, build_deck, args) -> int:
    from . import live

    sender, note, check = _reply_target(path) if not args.no_reply else (None, None, None)

    def ready(url):
        print(f"claude-reel: live at {url}  (Ctrl+C to stop)", flush=True)
        print(f"claude-reel: {'replies on' if sender else note or 'replies off'}", flush=True)
        if args.open:
            webbrowser.open(url)

    live.serve(
        path,
        build_deck,
        port=args.port,
        on_ready=ready,
        sender=sender,
        reply_note=note,
        check=check,
    )
    return 0


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
    p.add_argument(
        "--no-credits",
        action="store_true",
        help="skip the closing credits card (its lineage lookup scans the whole store)",
    )
    p.add_argument("-o", "--out", metavar="PATH", help="output file (default: ./<id>.reel.html)")
    p.add_argument("--open", action="store_true", help="open the deck in your browser")
    p.add_argument(
        "--reel", action="store_true", help="dump the reel (pivot JSONL) instead of a deck"
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="serve the deck on 127.0.0.1 and push new slides as the session grows"
        " (no credits card; Ctrl+C stops)",
    )
    p.add_argument("--port", type=int, default=0, help="--live port (default: any free one)")
    p.add_argument(
        "--no-reply",
        action="store_true",
        help="--live without the reply bar (read-only, even for a background session)",
    )
    p.add_argument("--version", action="version", version=f"claude-reel {__version__}")
    args = p.parse_args(argv)

    path = sessions.resolve_session(args.target)
    if path is None:
        print(f"claude-reel: no session matching '{args.target}'", file=sys.stderr)
        return 1

    if args.live and (args.reel or args.out):
        print(
            "claude-reel: --live serves the deck; it can't be combined with --reel or -o",
            file=sys.stderr,
        )
        return 2

    def build_reel():
        reel = extract(path, context=args.context == "chips", subagents=args.subagents)
        reel["beats"] = _filter_beats(reel["beats"], args.tail, args.since)
        return reel

    if args.live:
        return _live(path, lambda: curate(build_reel(), density=args.density), args)

    reel = build_reel()
    if args.reel:
        _dump_reel(reel, args.out)
        return 0

    deck = curate(reel, density=args.density)
    if not args.no_credits:
        lineage = credits.fetch_lineage(path)
        deck["credits"] = {
            "cast": credits.cast(reel["meta"].get("subagents") or [], deck["agent_slides"]),
            "family": credits.family(lineage, reel["meta"]["session_id"]),
            "lineage": lineage is not None,
        }
    out = Path(args.out) if args.out else Path.cwd() / f"{reel['meta']['session_id'][:8]}.reel.html"
    out.write_text(render(deck), encoding="utf-8")
    print(out)
    if args.open:
        webbrowser.open(f"file://{out.resolve()}")
    return 0
