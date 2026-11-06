#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Render a pr-dashboard model into the template, and say what changed since last time.

    render.py model.json facts.json [--out PATH] [--state-dir DIR] [--no-open]

Writes the board to <state>/boards/<scope>.html, where <state> is
$XDG_STATE_HOME/pr-dashboard (~/.local/state/pr-dashboard), next to watch.py's
baselines. The path is stable on purpose: the board's "reviewed" ticks live in
the browser's localStorage, which is keyed by the file's location, so a board
regenerated somewhere new starts with none.

Next to it, <scope>.facts.json keeps this run's collect.sh output. The next run
diffs against it and puts the result in the model's `changes`, which the board
shows as "since last scan". It is the dashboard's own snapshot, not watch.py's
baseline: sharing one meant running either tool reset the other's delta.

<scope> is the collect.sh scope plus the repos, e.g. mparrett__nooga-let-go, so
boards for different authors or repo sets don't overwrite each other.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TEMPLATE = Path(__file__).parent / "template.html"
ISLAND = re.compile(r'(<script id="dash-data"[^>]*>)[\s\S]*?(</script>)')


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "pr-dashboard"


def scope_slug(facts):
    who = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(facts.get("scope") or "ALL").lstrip("@"))
    if facts.get("waitingOn"):
        who = "waiting-on-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", facts["waitingOn"])
    repos = "+".join(r["repo"].replace("/", "-") for r in facts.get("repos", []))
    return f"{who}__{repos}"


def _by_number(facts):
    out = {}
    for repo in facts.get("repos", []):
        for p in repo.get("prs", []):
            out[(repo["repo"], p["number"])] = p
    return out


def _merged(facts):
    return {
        (repo["repo"], m["number"]): m
        for repo in facts.get("repos", [])
        for m in repo.get("mergedPrs", [])
    }


def _get(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _threads(t):
    if not t:
        return None
    if t.get("replied") is None:
        return f"{t['unresolved']} open"
    return f"{t['unresolved']} open, {t['replied']} replied"


def diff(prev, cur):
    """What moved between two collect.sh outputs, as display-ready rows.

    One row per change, each {ref, url, kind, tone, text}. tone is "good",
    "bad" or "info" -- the board colours it, so the call is made here, once.
    A field that is null on either side was not measured that run, and a
    change to or from "not measured" is not news, so it is skipped.
    """
    rows = []
    before, after = _by_number(prev), _by_number(cur)
    merged = _merged(cur)

    def add(key, p, kind, tone, text):
        repo, n = key
        rows.append(
            {
                "ref": f"{repo}#{n}",
                "url": (p or {}).get("url"),
                "kind": kind,
                "tone": tone,
                "text": text,
            }
        )

    for key, p in after.items():
        if key not in before:
            add(key, p, "opened", "info", f"opened: {p.get('title', '')}")
    for key, p in before.items():
        if key in after:
            continue
        if key in merged:
            add(key, merged[key], "merged", "good", "merged")
        else:
            add(key, p, "gone", "info", "closed, or no longer in scope")

    for key in sorted(after.keys() & before.keys()):
        a, b = before[key], after[key]

        va, vb = _get(a, "mergeReady", "verdict"), _get(b, "mergeReady", "verdict")
        if va and vb and va != vb:
            add(key, b, "verdict", "good" if vb == "ready" else "bad", f"merge: {va} → {vb}")

        ha, hb = _get(a, "approvals", "atHead"), _get(b, "approvals", "atHead")
        if ha is not None and hb is not None:
            for who in sorted(set(hb) - set(ha)):
                add(key, b, "approved", "good", f"approved by {who}")
            for who in sorted(set(ha) - set(hb)):
                stale = who in (_get(b, "approvals", "stale") or [])
                add(
                    key,
                    b,
                    "approval",
                    "bad",
                    f"{who}'s approval {'went stale (new push)' if stale else 'withdrawn'}",
                )

        ca, cb = a.get("changesRequested"), b.get("changesRequested")
        if ca is not None and cb is not None:
            for who in sorted(set(cb) - set(ca)):
                add(key, b, "changes", "bad", f"changes requested by {who}")

        ra, rb = _get(a, "reviewers", "reviewed"), _get(b, "reviewers", "reviewed")
        if ra is not None and rb is not None:
            for who in sorted(set(rb) - set(ra) - set(hb or []) - set(cb or [])):
                add(key, b, "reviewed", "info", f"reviewed by {who}")

        ta, tb = _threads(a.get("threads")), _threads(b.get("threads"))
        if ta and tb and ta != tb:
            worse = (b["threads"]["unresolved"] - (b["threads"].get("replied") or 0)) > (
                a["threads"]["unresolved"] - (a["threads"].get("replied") or 0)
            )
            add(key, b, "threads", "bad" if worse else "info", f"threads: {ta} → {tb}")

        na, nb = a.get("comments"), b.get("comments")
        if na is not None and nb is not None and nb > na:
            add(key, b, "comments", "info", f"+{nb - na} comment{'s' if nb - na > 1 else ''}")

        if None not in (a.get("ci"), b.get("ci")) and a.get("ci") != b.get("ci"):
            add(
                key,
                b,
                "ci",
                "good" if b.get("ci") == "pass" else "bad",
                f"CI: {a.get('ci')} → {b.get('ci')}",
            )

        ma, mb = a.get("mergeable"), b.get("mergeable")
        if "UNKNOWN" not in (ma, mb) and ma != mb:
            if mb == "CONFLICTING":
                add(key, b, "conflicts", "bad", "now conflicts with its base")
            elif ma == "CONFLICTING":
                add(key, b, "conflicts", "good", "conflict cleared")

        la, lb = a.get("lastTouch") or {}, b.get("lastTouch") or {}
        if (
            lb.get("at")
            and lb.get("at") != la.get("at")
            and lb.get("kind") in ("commit", "force-push")
        ):
            add(
                key,
                b,
                "push",
                "info",
                f"new {'force-' if lb['kind'] == 'force-push' else ''}push by {lb.get('by')}",
            )

    return rows


def changes_block(prev_snapshot, facts):
    if prev_snapshot is None:
        return {"first": True}
    return {
        "since": prev_snapshot.get("savedAt"),
        "items": diff(prev_snapshot.get("facts", {}), facts),
    }


def island_json(model):
    # "</" inside the island would close the <script> early: a PR title or
    # body excerpt containing "</script>" broke the page out of its data.
    return json.dumps(model, indent=2, ensure_ascii=False).replace("</", "<\\/")


def render(model, facts, out=None, state=None, now=None):
    state = Path(state) if state else state_dir()
    boards = state / "boards"
    boards.mkdir(parents=True, exist_ok=True)
    slug = scope_slug(facts)
    snap_path = boards / f"{slug}.facts.json"
    prev = json.loads(snap_path.read_text()) if snap_path.exists() else None

    now = now or datetime.datetime.now().astimezone()
    model = dict(model)
    model.setdefault("generatedAt", now.strftime("%Y-%m-%d %H:%M:%S %Z"))
    # Same facts as the snapshot means a re-render (say, after rewording the
    # queue), not a new scan: keep the snapshot and its diff, or the second
    # render would report "no changes" and lose what the first one found.
    if prev is not None and prev.get("facts") == facts and "changes" in prev:
        model["changes"] = prev["changes"]
        rescan = False
    else:
        model["changes"] = changes_block(prev, facts)
        rescan = True

    tpl = TEMPLATE.read_text()
    html, n = ISLAND.subn(lambda m: m.group(1) + "\n" + island_json(model) + "\n" + m.group(2), tpl)
    if n != 1:
        raise SystemExit("render.py: template has no dash-data island")
    out = Path(out) if out else boards / f"{slug}.html"
    out.write_text(html)
    if rescan:
        snap_path.write_text(
            json.dumps({"savedAt": now.isoformat(), "facts": facts, "changes": model["changes"]})
        )
    return out, model["changes"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model")
    ap.add_argument("facts")
    ap.add_argument("--out", help="write the board here instead of the state dir")
    ap.add_argument("--state-dir", help="override $XDG_STATE_HOME/pr-dashboard")
    ap.add_argument("--no-open", action="store_true", help="don't open the board")
    args = ap.parse_args(argv)
    model = json.loads(Path(args.model).read_text())
    facts = json.loads(Path(args.facts).read_text())
    out, changes = render(model, facts, args.out, args.state_dir)
    if changes.get("first"):
        print(f"wrote {out}  (first scan for this scope: baseline saved)")
    else:
        print(f"wrote {out}  ({len(changes['items'])} change(s) since {changes['since']})")
    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    main()
