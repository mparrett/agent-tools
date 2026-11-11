"""`workstream`: match, attach, sync, show, frontier, retire, close, ls, hook."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from rich.console import Console
from rich.markup import escape

from workstream import attribution, frontier, hooklog, seam, sessions, store, sync
from workstream.clock import Instant

RECENT_WINDOW = "14d"
# The hook still needs the hooklog for one strong signal, worktrees a session
# entered via EnterWorktree that its transcript does not show; one day covers
# the session being started and costs well under a second.
HOOK_WINDOW = "1d"
# The SessionEnd wrapper kills the CLI at 20 s; stop starting captures a few
# seconds before that, so the JSON still gets written and the run exits 0.
SESSION_END_BUDGET_S = 17.0


def _console(args) -> Console:
    return Console(
        stderr=False,
        highlight=False,
        force_terminal=None,
        no_color=bool(getattr(args, "json", False)),
    )


def _emit_json(obj) -> int:
    sys.stdout.write(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    return 0


def _ts(i: Optional[Instant], local: bool) -> str:
    if i is None:
        return "?"
    if local:
        return i.local.strftime("%Y-%m-%d %H:%M %z")
    return i.utc.strftime("%Y-%m-%d %H:%MZ")


def _me(args) -> str:
    """The session to act for: --session, else the caller's own, else error."""
    if getattr(args, "session", None):
        return sessions.resolve(args.session)
    try:
        sid = seam.me_session_id(seam.live_registry())
    except Exception:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise SystemExit("workstream: no session given and none detected; pass --session SID")
    return sid


def _window(spec: str) -> datetime:
    """`14d`, `72h`, `2w` → an aware UTC datetime that far back."""
    m = re.fullmatch(r"(\d+)([hdw])", spec)
    if not m:
        raise SystemExit(f"workstream: bad window {spec!r}; use e.g. 72h, 14d, 2w")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    return datetime.now(timezone.utc) - delta


def _load_log(since=RECENT_WINDOW):
    try:
        return hooklog.load(_window(since))
    except Exception as e:  # the hooklog is optional evidence
        sys.stderr.write(f"workstream: hooklog unavailable: {e}\n")
        return None


HOOKLOG_STALE = timedelta(days=7)


def _hooklog_hint() -> None:
    """One stderr line when the hook log is missing or has stopped growing.
    Nothing fails without it, so the readers stay quiet, which left a person
    never learning that evidence only the log records was being missed. A
    logger that was unregistered leaves its old files behind, so the newest
    file's age is the test, not whether one exists. Only the commands a person
    runs by hand say it, and never into --json."""
    where = seam.hooklog_dir()
    newest = None
    for f in hooklog.log_files():
        try:
            newest = max(newest or 0.0, f.stat().st_mtime)
        except OSError:
            continue
    if newest is None:
        problem = f"no hook log under {where}"
    else:
        age = timedelta(seconds=time.time() - newest)
        if age < HOOKLOG_STALE:
            return
        problem = f"the hook log under {where} was last written {age.days} days ago"
    sys.stderr.write(
        f"workstream: {problem}, so some worktrees and session events go unseen"
        " (README: Setup, claude-hook-logger)\n"
    )


def _recent_facts(log, workstreams) -> list[sessions.SessionFacts]:
    """Full facts for every session attached anywhere, so overlap signals have
    something to compare against; the cheap pass leaves those fields empty."""
    out = []
    seen: set[str] = set()
    for ws in workstreams:
        for ref in ws.refs:
            scheme, _, value = ref.partition(":")
            sid = value.partition("/")[2] if scheme == "agent" else value
            if scheme in ("claude", "codex", "agent") and sid and sid not in seen:
                seen.add(sid)
                try:
                    out.append(sessions.facts(sid, log=log))
                except Exception:
                    continue
    return out


def _rank_for(
    sid: str,
    log=None,
    *,
    window: Optional[str] = RECENT_WINDOW,
    with_messages: bool = True,
    with_overlap: bool = True,
):
    """`window=None` skips the hooklog altogether and `with_overlap=False` skips
    the full-facts pass over every attached session; both feed only the weak
    signals (messages, repo and file overlap), and together they cost more than
    the SessionStart hook's whole budget on a cold cache (measured 2026-11-02:
    3.2 s and 4.9 s for a 16-session workstream)."""
    if log is None and window is not None:
        log = _load_log(window)
    me = sessions.facts(sid, log=log)
    wss = store.list_workstreams()
    msgs = []
    if log and with_messages:
        msgs = log.messages(involving={sid})
    recent = _recent_facts(log, wss) if with_overlap else []
    return me, attribution.rank(me, wss, recent, msgs)


# --- commands -----------------------------------------------------------------


def cmd_match(args) -> int:
    sid = _me(args)
    me, ranked = _rank_for(sid)
    unassoc = store.unassociated(me.ref)
    if args.json:
        return _emit_json(
            {
                "session": me.ref,
                "unassociated": unassoc,
                "candidates": [
                    {
                        "id": c.ws.id,
                        "name": c.ws.name,
                        "title": c.ws.title,
                        "score": round(c.score, 2),
                        "reasons": c.reasons,
                    }
                    for c in ranked
                ],
            }
        )
    con = _console(args)
    con.print(f"[bold]{me.ref}[/bold]  {me.first_prompt[:80]}")
    if unassoc:
        con.print(f"  recorded unassociated: {unassoc}")
    if not ranked:
        con.print(
            '  no candidates. `workstream attach --new "<title>" --name <slug>` or `--none "<reason>"`'
        )
        return 0
    for c in ranked[:5]:
        con.print(f"  [bold]{c.ws.name or c.ws.id[:12]}[/bold]  {c.ws.title}  ({c.score:.2f})")
        for r in c.reasons:
            con.print(f"      - {r}")
    con.print(f"attach: workstream attach {ranked[0].ws.name or ranked[0].ws.id[:12]}")
    return 0


def cmd_attach(args) -> int:
    sid = _me(args)
    me = sessions.facts(sid)
    if args.none:
        nid = store.record_unassociated(me.ref, args.none)
        print(f"recorded {me.ref} as unassociated ({nid[:12]}): {args.none}")
        return 0
    if args.new and not args.name:
        raise SystemExit("workstream attach --new needs --name <slug>")
    # Before the attach: its claims are what the note and the release read.
    before = store.list_workstreams(include_closed=True) if not args.ancillary else []
    if args.new:
        ws = store.create(args.name, args.new, anchors=args.anchor or [])
        print(f"created workstream {ws.name} ({ws.id[:12]}): {ws.title}")
    else:
        if not args.ws:
            raise SystemExit("workstream attach needs <ws>, --new, or --none")
        ws = store.get(args.ws)
        if ws.closed:
            # No --force: attaching would reopen the work without saying so,
            # and reopening is a decision this tool does not make.
            raise SystemExit(
                f"workstream: {ws.name or ws.id[:12]} is closed; attach refuses closed "
                "workstreams and reopening is out of scope for this tool "
                f"(by hand: weft done --undo {ws.id[:12]}). "
                'Start a new one with `workstream attach --new "<title>" --name <slug>`.'
            )
        for ref in args.anchor or []:
            store.add_anchor(ws, ref)
    canonical = not args.ancillary
    holder = f"agent:{me.ref}"
    held = holder in ws.canonical_holders()
    note = args.note or _attach_note(me, ws, before, bool(args.new))
    if canonical:
        # D12: one canonical workstream at a time. A claim left held on the
        # previous one keeps its window open, so its next sync and `show`
        # would take this workstream's work too.
        for w in before:
            if w.id != ws.id and holder in w.canonical_holders():
                store.detach(w, me.ref)
                print(f"released {me.ref}'s canonical claim on {w.name or w.id[:12]}")
    if canonical and not held:
        # One canonical claim per session: drop any expired one before claiming.
        store.detach(ws, me.ref)
    # Re-attaching while held refreshes anchors and subagents, never stacks a
    # second claim (store.attach), so one continuous attachment is one interval.
    store.attach(ws, me.ref, canonical=canonical, note=note)
    # Subagents of this session inherit membership (D9).
    for agent in me.subagents:
        store.attach(ws, f"agent:{me.id}/{agent}", canonical=False, note="subagent of " + me.id[:8])
    print(
        f"attached {me.ref} to {ws.name or ws.id[:12]} as {'canonical' if canonical else 'ancillary'}"
    )
    if canonical and not args.no_sync:
        # Anchors otherwise wait for someone to remember `--anchor`; the
        # attaching session's own writes are the cheapest evidence there is.
        # Not for --ancillary: a helper's other work is not this workstream's.
        # Re-read: the attach and any --anchor changed the refs sync compares.
        try:
            current = store.get(ws.id)
            # Closed ones bound this session's earlier claims (store.session_windows).
            everything = store.list_workstreams(include_closed=True)
            others = [w for w in everything if w.id != current.id and not w.closed]
            # Retracting probes every gone worktree's branch through git and
            # gh; that is `workstream sync`'s job, not an attach's.
            report = sync.run(
                [current],
                others=others,
                only=me.id,
                facts={me.id: me},
                retract=False,
                history=everything,
            )
        except store.WeftError as e:
            # The attach itself landed; a failed sync is a retry, not an error.
            sys.stderr.write(
                f"workstream: attached, but sync failed ({e}); run `workstream sync`\n"
            )
            return 0
        # attach syncs one session, whose skips are few, and has no --verbose
        # to recover a folded line, so it lists every skip.
        _print_sync(report, dry_run=False, quiet_when_empty=True, verbose=True)
    return 0


def _attach_note(
    me: sessions.SessionFacts, ws: store.Workstream, before: list[store.Workstream], new: bool
) -> str:
    """The claim's note when `--note` is not given: the session's latest
    prompt, which is what it is working on now. The first prompt, the old
    default, is yesterday's work in a long-lived session's second attach. For
    `--new`, a latest prompt that predates the session's previous claim
    elsewhere belongs to that work, so the new workstream's title says more."""
    holder = f"agent:{me.ref}"
    prev = [i.at.utc for w in before if w.id != ws.id for i in w.intervals() if i.holder == holder]
    latest = me.human_prompts[-1] if me.human_prompts else None
    recent = latest is not None and (not prev or latest[0].utc > max(prev))
    if latest is not None and (recent or not new):
        return latest[1][:80]
    return ws.title[:80]


# Three full session ids would run a folded line past 200 columns; the first
# eight characters are what claude-ls resolves anyway.
_UUID_TAIL = re.compile(r"\b([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def _fold_skips(rows: list[dict]) -> list[str]:
    """One line per (workstream, ref scheme, reason) that repeats, in first-seen order.

    A sweep over a busy store skips dozens of retired worktrees for one
    workstream, each correct and none worth reading on its own; listed one per
    line they buried the few additions above them. A group of one keeps its full
    line, since then the ref itself is the information.
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["workstream"], r["ref"].partition(":")[0], r["reason"])
        groups.setdefault(key, []).append(r)
    out = []
    for (ws, scheme, reason), rs in groups.items():
        if len(rs) == 1:
            r = rs[0]
            out.append(f"skipped {r['ref']} for {ws}: {reason} ({r['session']} {r['how']})")
        else:
            # The sessions stay visible, so a wrong skip can be traced to the
            # session that produced it without rerunning the sweep.
            seen = list(dict.fromkeys(_UUID_TAIL.sub(r"\1", r["session"]) for r in rs))
            who = ", ".join(seen[:3]) + (f" +{len(seen) - 3} more" if len(seen) > 3 else "")
            out.append(
                f"skipped {len(rs)} {scheme}: refs for {ws}: {reason} "
                f"(from {who}; --verbose lists them)"
            )
    return out


def _print_sync(
    report: sync.Report, dry_run: bool, quiet_when_empty: bool = False, verbose: bool = False
) -> None:
    verb = "would add" if dry_run else "added"
    notable = [r for r in report.skipped if r["reason"] != "already an anchor"]
    if quiet_when_empty and not (report.added or notable or report.warnings or report.retracted):
        return
    for r in report.added:
        when = f"  {r['at']}" if r["at"] else ""
        print(f"{verb} {r['ref']} to {r['workstream']}: {r['session']} {r['how']}{when}")
    gone_verb = "would retract" if dry_run else "retracted"
    for r in report.retracted:
        print(f"{gone_verb} {r['ref']} from {r['workstream']}: {r['reason']}; {r['how']}")
    have = sum(1 for r in report.skipped if r["reason"] == "already an anchor")
    if verbose:
        for r in notable:
            print(
                f"skipped {r['ref']} for {r['workstream']}: {r['reason']} "
                f"({r['session']} {r['how']})"
            )
    else:
        for line in _fold_skips(notable):
            print(line)
    if not quiet_when_empty:
        print(
            f"{len(report.added)} {verb}, {have} already anchored"
            + (f", {len(report.retracted)} {gone_verb}" if report.retracted else "")
        )
        if report.ancillary:
            print(
                f"left out {len(report.ancillary)} ancillary session(s); "
                "--ancillary, or --session SID for one"
            )
    for w in report.warnings:
        print(f"warning: {w}")


def cmd_sync(args) -> int:
    if args.interactive:
        if args.json or args.dry_run:
            raise SystemExit("workstream: --interactive takes neither --json nor --dry-run")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SystemExit("workstream: --interactive needs a terminal; --dry-run shows the same")
    # Closed workstreams only bound sessions' earlier claims (store.session_windows).
    everything = store.list_workstreams(include_closed=True)
    live = [w for w in everything if not w.closed]
    if args.ws:
        targets = [store.get(n) for n in args.ws]
        closed = [t.name or t.id[:12] for t in targets if t.closed]
        if closed:
            raise SystemExit(
                f"workstream: {', '.join(closed)} closed; sync adds anchors only to open work"
            )
    else:
        targets = live
    only = None
    if args.session:
        only = sessions.resolve(args.session)
        targets = [t for t in targets if any(sid == only for _, sid in sync.session_members(t))]
        if not targets:
            where = "the workstreams named" if args.ws else "any open workstream"
            raise SystemExit(
                f"workstream: session {only[:8]} is attached to none of {where}; attach it first"
            )
    ids = {t.id for t in targets}
    run = dict(
        others=[w for w in live if w.id not in ids],
        only=only,
        ancillary=args.ancillary,
        log=_load_log(),
        history=everything,
    )
    if not args.json:
        _hooklog_hint()
    if args.interactive:
        return _sync_interactive(targets, run, _console(args), verbose=args.verbose)
    report = sync.run(targets, dry_run=args.dry_run, **run)
    if args.json:
        return _emit_json({**report.to_dict(), "dry_run": args.dry_run})
    _print_sync(report, dry_run=args.dry_run, verbose=args.verbose)
    return 0


def _sync_interactive(targets, run: dict, con: Console, verbose: bool, ask=input) -> int:
    """Sweep as a dry run, then write only what the person accepts.

    The sweep is read-only, so declining costs nothing and an interrupted
    review writes nothing at all. A dry run counts each proposal as held, so
    a ref proposed for one workstream is skipped for the next; declining it
    therefore sweeps again without it (transcripts are read once), and only
    the changes that appear then are offered. Writes go through the same
    store calls the non-interactive path makes."""
    facts: dict = {}
    decided: dict[tuple, bool] = {}
    declined: set[tuple[str, str]] = set()
    rows: dict[tuple, dict] = {}
    first = True
    while True:
        with con.status("reading sessions") as status:

            def progress(step: str, done: int, total: int) -> None:
                status.update(f"[dim]{done + 1}/{total}[/] {escape(step)}")

            report = sync.run(
                targets, dry_run=True, progress=progress, facts=facts, exclude=declined, **run
            )
        fresh = [
            (kind, r)
            for kind, rs in (("add", report.added), ("retract", report.retracted))
            for r in rs
            if (kind, r["workstream"], r["ref"]) not in decided
        ]
        if not fresh:
            if first:
                con.print("nothing to add or retract")
            break
        if not first:
            con.print(f"\n[bold]declining freed {len(fresh)} more[/]")
        first = False
        try:
            answers, stopped = _review_sync(fresh, con, ask)
        except (EOFError, KeyboardInterrupt):
            con.print("\n[yellow]stopped; nothing written[/]")
            return 130
        for (kind, r), yes in zip(fresh, answers):
            decided[(kind, r["workstream"], r["ref"])] = yes
            rows[(kind, r["workstream"], r["ref"])] = r
            if kind == "add" and not yes:
                declined.add((r["workstream"], r["ref"]))
        # Only a declined addition can free a ref for another workstream.
        if stopped or all(yes or kind != "add" for (kind, _), yes in zip(fresh, answers)):
            break
    by_label = {sync._label(t): t for t in targets}
    written = {"add": 0, "retract": 0}
    failed = []
    for (kind, label, ref), yes in decided.items():
        if not yes:
            continue
        try:
            if kind == "add":
                store.add_anchor(by_label[label], ref, source=store.SYNC_SOURCE)
            else:
                store.rm_anchor(by_label[label], ref)
            written[kind] += 1
        except store.WeftError as e:
            # Keep going: each write stands alone, and the summary says which landed.
            failed.append(f"{kind} {ref} for {label}: {e}")
    notable = [r for r in report.skipped if r["reason"] not in ("already an anchor", "declined")]
    lines = (
        [
            f"skipped {r['ref']} for {r['workstream']}: {r['reason']} ({r['session']} {r['how']})"
            for r in notable
        ]
        if verbose
        else _fold_skips(notable)
    )
    for line in lines:
        con.print(f"[dim]{escape(line)}[/]")
    for w in report.warnings:
        con.print(f"[yellow]warning:[/] {escape(w)}")
    for f in failed:
        con.print(f"[red]failed:[/] {escape(f)}")
    n_gone = sum(1 for k in decided if k[0] == "retract")
    have = sum(1 for r in report.skipped if r["reason"] == "already an anchor")
    parts = [
        f"[green]{written['add']} added[/]",
        f"{sum(1 for k, v in decided.items() if k[0] == 'add' and not v)} declined",
        f"{have} already anchored",
    ]
    if n_gone:
        kept = sum(1 for k, v in decided.items() if k[0] == "retract" and not v)
        parts.append(f"[red]{written['retract']} retracted[/] ({kept} kept)")
    if report.ancillary:
        parts.append(f"{len(report.ancillary)} ancillary session(s) left out")
    if failed:
        parts.append(f"[red]{len(failed)} failed[/]; rerun to retry")
    con.print(", ".join(parts))
    return 1 if failed else 0


_REVIEW_KEYS = "[y]es [n]o [w]orkstream [a]ll [q]uit"


def _review_sync(items: list[tuple[str, dict]], con: Console, ask=input) -> tuple[list[bool], bool]:
    """Walk proposed changes one workstream at a time, additions before
    retractions, and return an answer per item (in `items` order) plus whether
    the person quit. `w` accepts the rest of the current workstream, both
    kinds; `a` everything left; `q` declines everything left."""
    order: dict[str, list[int]] = {}
    for i, (kind, r) in enumerate(items):
        order.setdefault(r["workstream"], []).append(i)
    for idx in order.values():
        idx.sort(key=lambda i: items[i][0] != "add")
    answers = [False] * len(items)
    con.print(f"{len(items)} change(s) to review; {escape(_REVIEW_KEYS)}")
    everything = stop = False
    for ws, idx in order.items():
        if stop:
            break
        con.print(f"\n[bold]{escape(ws)}[/] ({len(idx)})")
        whole = everything
        for i in idx:
            kind, r = items[i]
            # Evidence on its own line: ref plus evidence ran past any terminal width.
            if kind == "add":
                when = f"  {r['at']}" if r["at"] else ""
                con.print(f"  [green]+ add {escape(r['ref'])}[/]")
                sid = _UUID_TAIL.sub(r"\1", r["session"])
                detail = f"{sid} {r['how']}{when}"
            else:
                con.print(f"  [red]- retract {escape(r['ref'])}[/]")
                detail = f"{r['reason']}; {r['how']}"
            con.print(f"      [dim]{escape(detail)}[/]")
            if not whole:
                key = _ask_key(ask)
                if key == "q":
                    stop = True
                    break
                if key == "n":
                    continue
                if key == "a":
                    everything = True
                whole = key in ("w", "a")
            answers[i] = True
    return answers, stop


def _ask_key(ask) -> str:
    while True:
        key = ask("  ? ").strip().lower()[:1]
        if key in ("y", "n", "w", "a", "q"):
            return key
        print(f"  {_REVIEW_KEYS}")


def _interval_end(iv: store.AttachInterval, as_of: Instant, local: bool) -> str:
    if iv.released_at:
        return f"{_ts(iv.released_at, local)} released"
    if iv.expires and iv.expires.utc <= as_of.utc:
        return f"{_ts(iv.expires, local)} expired"
    return "held" + (f" (expires {_ts(iv.expires, local)})" if iv.expires else "")


def _span(s: frontier.SessionFrontier, local: bool) -> str:
    """When the session's counted work ran: the whole transcript, or for a
    scoped one the transcript cut to its windows (store.session_windows)."""
    f = s.facts
    start, end = f.started, f.last_seen
    if s.scope:
        first, last = s.scope[0][0], s.scope[-1][1]
        offset = (start or end).offset if (start or end) else timedelta(0)
        if first is not None and (start is None or first > start.utc):
            start = Instant(first, offset)
        if last is not None and (end is None or last < end.utc):
            end = Instant(last, offset)
    return f"{_ts(start, local)} → {_ts(end, local)}"


def _print_header(con: Console, ws: store.Workstream, local: bool) -> None:
    state = "closed" if ws.closed else ws.state
    con.print(f"[bold]{ws.name or ws.id[:12]}[/bold]  {ws.title}  \\[{state}]")
    con.print(
        f"  id {ws.id[:12]}  created {_ts(ws.created, local)}  touched {_ts(ws.touched, local)}"
    )
    if ws.closed_note:
        con.print(f"  closed: {ws.closed_note}")


def _print_sessions(con: Console, fr: frontier.Frontier, local: bool, full: bool) -> None:
    """Top-level sessions with their canonical intervals; subagents as rows
    (`full`) or a count. Rows and count are the same set."""
    con.print("\n[bold]sessions[/bold]")
    by_parent: dict = {}
    for s in fr.sessions:
        by_parent.setdefault(s.facts.parent_id if s.role == "subagent" else None, []).append(s)
    for s in by_parent.get(None, []):
        f = s.facts
        state = "live" if s.live else (s.lifecycle or "no lifecycle")
        drop = "" if not s.dropped else "  [red]no frontier recorded[/red]"
        con.print(f"  {f.ref}  {s.role}  {state}  {_span(s, local)}  ctx {f.peak_context:,}{drop}")
        for iv in s.intervals:
            con.print(f"      canonical {_ts(iv.at, local)} → {_interval_end(iv, fr.as_of, local)}")
        subs = by_parent.get(f.id, [])
        if not full:
            if subs:
                con.print(f"      ↳ {len(subs)} subagent{'' if len(subs) == 1 else 's'}")
            continue
        if f.human_prompts:
            con.print(f"      last prompt: {f.human_prompts[-1][1][:100]}")
        for sub in subs:
            name = (
                sub.facts.agent_meta.get("name")
                or sub.facts.agent_meta.get("agentType")
                or "subagent"
            )
            con.print(f"      ↳ {sub.facts.ref}  {name}  {_ts(sub.facts.last_seen, local)}")


def _print_anchors(
    con: Console, fr: frontier.Frontier, sources: Optional[dict[str, str]], full: bool
) -> None:
    """One line per anchor with its status word; `full` adds the raw liveness."""
    con.print("\n[bold]anchors[/bold]")
    for a in fr.anchors:
        # Only sync is marked: an asserted anchor is the unremarkable case.
        mark = " (sync)" if (sources or {}).get(a.ref) == "sync" else ""
        word = frontier.anchor_status(a)
        if not full:
            con.print(f"  {a.ref}{mark}  {word}")
            continue
        lv = {
            k: v
            for k, v in a.liveness.items()
            if v not in (None, [], {}, "") and k not in ("fetched_at", "cached")
        }
        # soft_wrap: a hard wrap would split the JSON mid-string for whoever copies it.
        con.print(f"  {a.ref}{mark}  {word}  {json.dumps(lv, default=str)}", soft_wrap=True)
        for w in a.warnings:
            con.print(f"      warning: {w}")


def _print_tasks(con: Console, fr: frontier.Frontier) -> None:
    if fr.tasks:
        con.print("\n[bold]tasks[/bold]")
        for t in fr.tasks:
            mark = {True: "  blocked", False: "  ready"}.get(t.blocked, "")
            kind = "" if t.kind == "task" else f"  ({t.kind})"
            con.print(f"  {t.state:7}  {t.id[:12]}  {t.title}{kind}{mark}")


def _print_show(
    con: Console,
    ws: store.Workstream,
    fr: frontier.Frontier,
    local: bool,
    sources: Optional[dict[str, str]] = None,
) -> None:
    _print_header(con, ws, local)
    _print_sessions(con, fr, local, full=True)
    _print_anchors(con, fr, sources, full=True)
    _print_tasks(con, fr)
    if fr.messages:
        con.print("\n[bold]messages[/bold]")
        for m in fr.messages:
            con.print(
                f"  {_ts(m.at, local)}  {m.from_sid[:8]} → {m.to_text()}: {m.summary or m.body[:80]}"
            )
    con.print(f"\n[bold]timeline[/bold] ({'local' if local else 'UTC'})")
    for at, kind, text in fr.timeline:
        con.print(f"  {_ts(at, local)}  {kind:9}  {text}")


def _recent_sessions(log):
    try:
        return list(
            sessions.iter_sessions(since=Instant(_window(RECENT_WINDOW), timedelta(0)), log=log)
        )
    except Exception as e:  # evidence, not a requirement
        sys.stderr.write(f"workstream: recent sessions unavailable: {e}\n")
        return []


def cmd_show(args) -> int:
    ws = store.get(args.ws)
    log = _load_log()
    fr = frontier.build(ws, log=log, recent=_recent_sessions(log))
    sources = store.ref_sources(ws)
    if args.json:
        return _emit_json(
            {"workstream": asdict(ws), "frontier": frontier.to_dict(fr), "ref_sources": sources}
        )
    _print_show(_console(args), ws, fr, args.local, sources)
    return 0


def _print_frontier(
    con: Console,
    fr: frontier.Frontier,
    local: bool,
    sources: Optional[dict[str, str]] = None,
    full: bool = False,
) -> None:
    """Concise by default: this lands in the next agent's context window.
    `full` is `show` plus the loose ends and options."""
    if full:
        _print_show(con, fr.ws, fr, local, sources)
    else:
        _print_header(con, fr.ws, local)
        _print_sessions(con, fr, local, full=False)
        _print_anchors(con, fr, sources, full=False)
        _print_tasks(con, fr)
    con.print("\n[bold]loose ends[/bold]")
    for le in fr.loose_ends or ["none found"]:
        con.print(f"  - {le}")
    o = fr.options
    con.print(f"\n[bold]options[/bold] (default: {o.default}, because {o.default_why})")
    for kind, opt in (("message", o.message), ("fresh", o.fresh), ("resume", o.resume)):
        if opt is None:
            continue
        con.print(f"  [bold]{kind}[/bold]: {escape(opt.how)}")
        for x in opt.facts:
            con.print(f"      - {escape(x)}")
    if fr.captures:
        con.print(f"\ncaptures: {len(fr.captures)}, newest {fr.captures[-1]}")
    for w in fr.warnings:
        con.print(f"warning: {w}")


def cmd_frontier(args) -> int:
    ws = store.get(args.ws)
    log = _load_log()
    fr = frontier.build(ws, log=log, recent=_recent_sessions(log))
    path = None
    if args.record:
        path = frontier.capture(fr, slug=args.slug, recorded_by=frontier.BY_RECORD)
    if args.json:
        d = frontier.to_dict(fr)
        d["capture"] = str(path) if path else None
        return _emit_json(d)
    _print_frontier(_console(args), fr, args.local, store.ref_sources(ws), full=args.full)
    if path:
        print(f"\nrecorded {path}")
    return 0


def cmd_retire(args) -> int:
    log = _load_log()
    recent = _recent_sessions(log)
    # Closed workstreams too: a session attached to one still needs its frontier recorded.
    wss = store.list_workstreams(include_closed=True)
    done = []
    for raw in args.sid:
        sid = sessions.resolve(raw)
        me = sessions.facts(sid, log=log)
        mine = [ws for ws in wss if any(r.endswith(sid) or r.endswith(me.ref) for r in ws.refs)]
        if not mine:
            done.append(
                {
                    "session": me.ref,
                    "workstreams": [],
                    "note": "attached to no workstream; nothing to capture",
                }
            )
            continue
        caps = []
        # `ls` leaves task children empty; `get` fills them, so the capture
        # carries the tasks and their closure blockers. One export serves this
        # session's workstreams; the next session's sees the claims released here.
        nodes = store.export_nodes()
        for listed in mine:
            ws = store.get(f"ws:{listed.name}" if listed.name else listed.id, nodes)
            fr = frontier.build(ws, log=log, recent=recent, history=mine)
            path = frontier.capture(fr, slug=args.slug, recorded_by=frontier.BY_RETIRE)
            store.detach(ws, me.ref)
            caps.append(
                {
                    "workstream": ws.name or ws.id[:12],
                    "capture": str(path),
                    "loose_ends": fr.loose_ends,
                }
            )
        done.append({"session": me.ref, "workstreams": caps})
    if args.json:
        return _emit_json(done)
    for d in done:
        print(f"{d['session']}")
        if d.get("note"):
            print(f"  {d['note']}")
        for c in d["workstreams"]:
            print(f"  {c['workstream']}: {c['capture']}")
            for le in c["loose_ends"]:
                print(f"      - {le}")
    return 0


def cmd_close(args) -> int:
    ws = store.get(args.ws)
    label = ws.name or ws.id[:12]
    if ws.closed:
        if args.json:
            return _emit_json({"workstream": label, "closed": True, "already": True})
        print(f"{label} is already closed")
        return 0
    log = _load_log()
    fr = frontier.build(ws, log=log, recent=_recent_sessions(log))
    open_tasks = [t for t in fr.tasks if t.state not in store.SETTLED]
    # A held claim means a session still owns the work; closing would release
    # it behind that session's back. `retire` it first, or --force.
    holders = ws.canonical_holders()
    blocked = bool(open_tasks or fr.closure_blockers or holders)
    if blocked and not args.force:
        if args.json:
            _emit_json(
                {
                    "workstream": label,
                    "closed": False,
                    "open_tasks": [asdict(t) for t in open_tasks],
                    "live_claims": holders,
                    "loose_ends": fr.closure_blockers,
                }
            )
            return 1
        sys.stderr.write(f"workstream: not closing {label}; --force to close anyway\n")
        for h in holders:
            sys.stderr.write(f"  live claim: {h} (workstream retire it first)\n")
        for t in open_tasks:
            state = t.state + (" blocked" if t.blocked else "")
            sys.stderr.write(f"  task {t.id[:12]} {state}: {t.title}\n")
        for le in fr.closure_blockers:
            sys.stderr.write(f"  loose end: {le}\n")
        return 1
    closed = store.close(ws, args.note)
    if args.json:
        return _emit_json(
            {
                "workstream": label,
                "closed": closed.closed,
                "forced": blocked,
                "released": holders,
                "open_tasks": [asdict(t) for t in open_tasks],
                "loose_ends": fr.closure_blockers,
                "note": closed.closed_note,
            }
        )
    print(f"closed {label}" + (f": {closed.closed_note}" if closed.closed_note else ""))
    for h in holders:
        print(f"  released {h}")
    if blocked:
        print(
            f"  forced past {len(holders)} live claim(s), {len(open_tasks)} open task(s), "
            f"{len(fr.closure_blockers)} loose end(s)"
        )
    return 0


def cmd_ls(args) -> int:
    wss = store.list_workstreams(args.since, include_closed=args.all)
    if args.session:
        # Membership as `match` counts it ("already attached"), without match's
        # transcript scan: the workstream mod polls this for its status badge.
        # A prefix is matched against member ids, not resolved through the store.
        wss = [
            w
            for w in wss
            if any(s.startswith(args.session) for s in attribution.Membership.of(w).sessions)
        ]
    if args.json:
        return _emit_json([asdict(w) for w in wss])
    _hooklog_hint()
    for ws in wss:
        n = sum(1 for r in ws.refs if r.partition(":")[0] in ("claude", "codex", "agent"))
        live = len(ws.canonical_holders())
        state = "closed" if ws.closed else ws.state
        print(f"{(ws.name or ws.id[:12]):28} {state:7} {n:3} sessions {live:2} live  {ws.title}")
    return 0


def _attached_block(me_ref: str, wss: list[store.Workstream]) -> str:
    """Orientation for a session that is already attached: one frontier line
    per workstream from weft and the capture files alone. The full frontier
    probes every anchor through gh and git, which the hook's budget cannot
    carry, so the line points at it instead."""
    now = Instant.now()
    full = []
    lines = [f"This session is attached to these workstreams, as of {now.iso()}:"]
    # One export shared by every `get`: a show per workstream plus this, not
    # an export each, keeps N attached workstreams at N + 2 weft processes.
    nodes = store.export_nodes()
    for ws in wss:
        try:
            # `ls` leaves children empty; `get` fills them. The `ws:` ref resolves
            # in one show, where a bare id first misses as `ws:<id>`.
            full.append(store.get(f"ws:{ws.name}" if ws.name else ws.id, nodes))
        except store.WeftError as e:
            lines.append(
                f"- {ws.name or ws.id[:12]}: state unavailable ({e}). Full frontier: "
                f"`workstream frontier {ws.name or ws.id[:12]}`"
            )
    blocked = None
    if any(c.state not in store.SETTLED for ws in full for c in ws.tasks):
        try:
            blocked = store.blocked_ids()
        except store.WeftError:
            blocked = None
    for ws in full:
        lines.append(frontier.brief_line(frontier.brief(ws, me_ref, now, blocked)))
    return "\n".join(lines) + "\n"


def _newest_capture_of(
    ws: store.Workstream, sid: str
) -> Optional[tuple[str, datetime, Optional[str]]]:
    """(path, as_of, recorded_by) of the newest capture on `ws` that names
    session `sid`."""
    best = None
    for path in frontier._capture_paths(ws):
        meta = frontier.capture_meta(path)
        if sid not in meta.get("sessions", []) or not meta.get("as_of"):
            continue
        try:
            # Instant.iso(): UTC first, then the wall clock in parentheses.
            at = datetime.fromisoformat(meta["as_of"].split()[0].replace("Z", "+00:00"))
        except (AttributeError, IndexError, ValueError):
            continue
        if best is None or at > best[1]:
            best = (path, at, meta.get("recorded_by"))
    return best


def _covered_by(prior, me: sessions.SessionFacts) -> Optional[str]:
    """Why `prior` (from `_newest_capture_of`) already covers the session, or
    None. Compared at whole seconds: `as_of` is stored truncated to them."""
    if prior is None:
        return None
    as_of = prior[1].replace(microsecond=0)
    if prior[2] in (frontier.BY_RECORD, frontier.BY_RETIRE):
        # Recording on purpose writes to the transcript after `as_of` (the tool
        # result, the closing reply), so `last_seen` always postdates it. Only
        # a new prompt from the human means the work moved on.
        last_prompt = me.human_prompts[-1][0] if me.human_prompts else None
        if last_prompt is None or last_prompt.utc.replace(microsecond=0) <= as_of:
            return f"no prompt since the capture recorded by `{prior[2]}`"
        return None
    if me.last_seen is None or me.last_seen.utc.replace(microsecond=0) <= as_of:
        return "no activity since the newest capture"
    return None


def _session_end(sid: str) -> dict:
    """Record a frontier capture on every workstream `sid` is attached to,
    unless one already covers its last activity. Claims stay held: a /clear or
    a restart usually carries on with the same work, and releasing is what
    `retire` is for."""
    deadline = time.monotonic() + SESSION_END_BUDGET_S
    recorded: list[str] = []
    skipped: list[dict] = []
    out = {"session": sid, "recorded": recorded, "skipped": skipped}
    # Closed workstreams too, as retire does: their sessions still need a capture.
    mine = [
        ws
        for ws in store.list_workstreams(include_closed=True)
        if any(r.endswith(sid) for r in ws.refs)
    ]
    if not mine:
        return out
    # The canonical workstream first: if the budget runs out, it is the one
    # whose capture matters most.
    mine.sort(key=lambda w: not any(h.endswith(sid) for h in w.canonical_holders()))
    try:
        me = sessions.facts(sid)
    except sessions.NotFound:
        me = None
    # One day of hooklog covers this session's lifecycle and messages; older
    # members keep what earlier captures said. `recent` is left empty: the
    # 14-day session scan behind foreign-merge loose ends would not fit the budget.
    log = _load_log(HOOK_WINDOW)
    nodes = store.export_nodes()
    slowest = 0.0
    for listed in mine:
        label = listed.name or listed.id[:12]
        try:
            prior = _newest_capture_of(listed, sid)
            if me is None:
                skipped.append({"workstream": label, "reason": "no transcript for this session"})
                continue
            why = _covered_by(prior, me)
            if why:
                skipped.append({"workstream": label, "reason": why, "capture": prior[0]})
                continue
            # Builds run one after another; start one only if a build as slow
            # as the slowest so far would still finish inside the budget.
            started = time.monotonic()
            if started + slowest >= deadline:
                skipped.append({"workstream": label, "reason": "time budget"})
                continue
            # `ls` leaves tasks empty; the capture should carry them.
            ws = store.get(f"ws:{listed.name}" if listed.name else listed.id, nodes)
            # `mine` holds every claim this session made: all the windows need.
            fr = frontier.build(ws, log=log, recent=(), gh_cache_only=True, history=mine)
            recorded.append(str(frontier.capture(fr, recorded_by=frontier.BY_SESSION_END)))
            slowest = max(slowest, time.monotonic() - started)
        except Exception as e:  # one workstream's failure must not cost the others theirs
            skipped.append({"workstream": label, "reason": f"error: {e}"})
    return out


def cmd_hook(args) -> int:
    """SessionStart: candidates for the starting session as a block to inject.
    SessionEnd: record the frontier for the ending session's workstreams."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    sid = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid and "WEFT_ACTOR" not in os.environ:
        # The payload names the session outright; a hook's environment may not.
        os.environ["WEFT_ACTOR"] = store._holder(f"claude:{sid}")
    if args.event == "SessionEnd":
        if not sid:
            return _emit_json({"recorded": [], "skipped": []})
        try:
            out = _session_end(sid)
        except Exception as e:  # never fail a session's exit; say why on stderr
            sys.stderr.write(f"workstream hook: {e}\n")
            return _emit_json({"recorded": [], "skipped": [], "error": str(e)})
        out["reason"] = payload.get("reason")
        return _emit_json(out)
    if not sid:
        return _emit_json({"block": ""})
    try:
        me, ranked = _rank_for(sid, window=HOOK_WINDOW, with_messages=False, with_overlap=False)
    except sessions.NotFound:
        # A brand-new session has no transcript yet; lineage may still be known.
        return _emit_json({"block": ""})
    except Exception as e:  # the hook degrades to silence; it never fails a session start
        sys.stderr.write(f"workstream hook: {e}\n")
        return _emit_json({"block": "", "error": str(e)})
    attached = [c for c in ranked if "already attached" in c.reasons]
    if attached:
        names = [c.ws.name for c in attached]
        try:
            block = _attached_block(me.ref, [c.ws for c in attached])
        except Exception as e:  # same rule as above: silence, never a failed start
            sys.stderr.write(f"workstream hook: {e}\n")
            return _emit_json({"block": "", "attached": names, "error": str(e)})
        return _emit_json({"block": block, "attached": names})
    unassoc = store.unassociated(me.ref)
    if unassoc and not ranked:
        return _emit_json({"block": "", "attached": [], "unassociated": unassoc})
    lines = [
        f"Workstream candidates for this session from `workstream match` as of {Instant.now().iso()}; "
        "attach before doing work so the next session can find it:"
    ]
    for c in ranked[:3]:
        lines.append(f"- {c.ws.name or c.ws.id[:12]}: {c.ws.title} ({c.score:.2f})")
        for r in c.reasons[:3]:
            lines.append(f"    - {r}")
        lines.append(
            f"    attach: `workstream attach {c.ws.name or c.ws.id[:12]}` (add --ancillary if helping, not owning)"
        )
    if not ranked:
        lines.append("- no candidate matched this session's evidence yet")
    lines.append(
        'Otherwise: `workstream attach --new "<title>" --name <slug> --anchor gh:o/r#N`, '
        'or `workstream attach --none "<reason>"` if this session is not a workstream.'
    )
    return _emit_json({"block": "\n".join(lines) + "\n", "candidates": len(ranked)})


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="workstream", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, session=False, local=False):
        sp.add_argument("--json", action="store_true")
        if session:
            sp.add_argument("--session", help="session id or prefix (default: the caller's own)")
        if local:
            sp.add_argument(
                "--local", action="store_true", help="render each event in its own zone"
            )

    sp = sub.add_parser("match", help="ranked workstream candidates for a session")
    common(sp, session=True)
    sp.set_defaults(fn=cmd_match)

    sp = sub.add_parser(
        "attach", help="attach a session to a workstream, create one, or record none"
    )
    sp.add_argument("ws", nargs="?")
    sp.add_argument("--new", metavar="TITLE")
    sp.add_argument("--name", metavar="SLUG")
    sp.add_argument("--none", metavar="REASON")
    sp.add_argument("--ancillary", action="store_true")
    sp.add_argument("--anchor", action="append", metavar="REF")
    sp.add_argument("--note")
    sp.add_argument(
        "--no-sync",
        action="store_true",
        help="do not add anchors from this session's evidence (--ancillary never does)",
    )
    common(sp, session=True)
    sp.set_defaults(fn=cmd_attach)

    sp = sub.add_parser(
        "sync", help="add anchors from the PRs, branches and worktrees attached sessions acted on"
    )
    sp.add_argument("ws", nargs="*", help="workstreams to sync (default: every open one)")
    sp.add_argument(
        "--dry-run", action="store_true", help="print what would be added; write nothing"
    )
    sp.add_argument(
        "--ancillary",
        action="store_true",
        help="also sync ancillary sessions (default: sessions that hold or held a claim)",
    )
    sp.add_argument(
        "--verbose",
        action="store_true",
        help="list every skipped ref (default: repeats are folded into one line per workstream)",
    )
    sp.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="review each addition and retraction before anything is written (needs a terminal)",
    )
    common(sp, session=True)
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("show", help="a workstream's sessions, anchors, messages and timeline")
    sp.add_argument("ws")
    common(sp, local=True)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("frontier", help="where the work stopped, loose ends, resume-or-fresh")
    sp.add_argument("ws")
    sp.add_argument("--record", action="store_true", help="write a capture under project-docs")
    sp.add_argument("--slug", help="project-docs slug for the capture")
    sp.add_argument(
        "--full",
        action="store_true",
        help="also subagent rows, last prompts, messages, the timeline and raw anchor liveness",
    )
    common(sp, local=True)
    sp.set_defaults(fn=cmd_frontier)

    sp = sub.add_parser(
        "retire", help="capture the frontier for each session's workstreams and release its claims"
    )
    sp.add_argument("sid", nargs="+")
    sp.add_argument("--slug")
    common(sp)
    sp.set_defaults(fn=cmd_retire)

    sp = sub.add_parser(
        "close",
        help="mark a workstream done once its tasks are settled and nothing is left loose",
    )
    sp.add_argument("ws")
    sp.add_argument("--note", help="why it closed; kept as a note derived from the workstream")
    sp.add_argument("--force", action="store_true", help="close despite open tasks or loose ends")
    common(sp)
    sp.set_defaults(fn=cmd_close)

    sp = sub.add_parser("ls", help="list workstreams (closed ones only with --all)")
    sp.add_argument("--since")
    sp.add_argument("--all", action="store_true", help="include closed workstreams")
    sp.add_argument("--session", help="only workstreams this session id (or prefix) is attached to")
    common(sp)
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser(
        "hook",
        help="hook body: stdin payload; SessionStart prints JSON {block}, "
        "SessionEnd records captures and prints JSON {recorded, skipped}",
    )
    sp.add_argument("--event", choices=("SessionStart", "SessionEnd"), default="SessionStart")
    sp.add_argument("--json", action="store_true", default=True)
    sp.set_defaults(fn=cmd_hook)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # One weft listing for every frontier this invocation builds.
    frontier._history_cache = {}
    sessions._snapshot = {}
    try:
        return args.fn(args)
    except (store.NotFound, sessions.NotFound) as e:
        sys.stderr.write(f"workstream: not found: {e}\n")
        return 2
    except (store.Ambiguous, sessions.Ambiguous) as e:
        sys.stderr.write(f"workstream: ambiguous: {e}\n")
        return 4
    except store.WeftError as e:
        sys.stderr.write(f"workstream: weft: {e}\n")
        return 6
    finally:
        frontier._history_cache = None
        sessions._snapshot = None


if __name__ == "__main__":
    sys.exit(main())
