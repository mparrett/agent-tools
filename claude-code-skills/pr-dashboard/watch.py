#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["rich"]
# ///
"""watch.py — a stateful "what changed on my PRs since I last looked" report.

The pr-dashboard skill's collect.sh is a *stateless snapshot*: it fetches facts
and renders a board. This is its stateful sibling. It caches each PR's review
decision, body, and comment/review state under XDG_STATE_HOME, then on the next
run diffs the live state against that baseline and prints only what moved:
approval flips, a body edit, new/edited/removed comments, new or re-stated
reviews.

Quick start:
    ./watch.py                       # my open PRs in the current repo
    ./watch.py owner/a owner/b       # my open PRs across several repos
    ./watch.py --all owner/repo      # every open PR (maintainer watch)
    ./watch.py --reset               # forget the baseline and re-seed

First run on a repo records a baseline and reports nothing changed (there's no
"before"). Subsequent runs report the delta and re-seed. Comment/review change
detection is "counts + hash": gh exposes no updatedAt on comments, so an edit is
detected as a body-hash change keyed by the comment's stable id — we never cache
the comment text itself.

Inline review comments (the file-and-line kind) are a third collection that
`gh pr list --json` cannot see at all; they come from one batched GraphQL call
per repo. See fetch_review_comments.

Cache lives at $XDG_STATE_HOME/pr-dashboard/watch/<owner>__<repo>.json (durable,
not /tmp). It self-prunes: repo baselines untouched for --ttl-days (default 14)
are dropped, and the newest --max-repos (default 200) are kept as a backstop.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

# --- fields we ask gh for. Heavier than collect.sh's (we need comment/review
# bodies to hash) — that's why this is its own fetch, not a collect.sh add-on. ---
PR_FIELDS = "number,title,url,body,reviewDecision,updatedAt,isDraft,comments,reviews"

# A pull request has THREE comment collections, and the two above are not the
# interesting one. `comments` is the issue conversation; `reviews` is the summary
# body attached to an approve or a request-changes. A comment anchored to a file
# and a line — the form most review feedback actually takes — lives in neither,
# and `gh pr list --json` exposes no field that reaches it. Without the query
# below, a PR in the middle of an active review reports as quiet.
#
# Measured on nooga/let-go#926 (2026-09-20): 1 issue comment and 2 reviews
# visible to `gh pr list`, and 2 inline review comments invisible to it.
#
# One aliased GraphQL call per repo — the same batching shape collect.sh uses
# for lastTouch — rather than one REST call per PR.
REVIEW_THREAD_PAGE = 100
REVIEW_COMMENT_PAGE = 100

# GitHub rejects a query whose *possible* node count exceeds 500,000, and each
# alias above is worth 100 + 100*100 = 10,100 of them. `fetch_prs` passes up to
# --limit 100 PRs, so one query for a busy repo asks for 1,010,000 and is
# refused outright: gh exits non-zero, nothing is measured, and the run still
# prints a normal-looking report. Measured against cli/cli: 49 aliases return
# data, 50 are rejected, and an end-to-end --all run on a 67-PR repo measured 0
# of 67 while saying nothing.
#
# Chunking keeps the feature working on exactly the repos it was written for.
# Lowering the page constants instead would trade the rejection for truncation,
# which is also unmeasured — the same silence by a different route.
REVIEW_PRS_PER_QUERY = 25

# A baseline records the scope it was seeded under, so a later run can tell whether
# it's comparing like with like. ALL_SCOPE is the stored marker for --all; anything
# else is a literal author login. (A GitHub user actually named "ALL" would alias
# onto the maintainer scope — the encoding predates this and isn't worth a cache
# format break, but that's the one input it can't represent.)
ALL_SCOPE = "ALL"
DEFAULT_AUTHOR = "@me"

console = Console()


def _sha(text: str) -> str:
    """Short content hash. 12 hex chars — collision-safe for one PR's comments."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def cache_dir() -> Path:
    """Durable per-user state dir, XDG-style. PR_WATCH_CACHE_DIR overrides (tests)."""
    override = os.environ.get("PR_WATCH_CACHE_DIR")
    if override:
        base = Path(override)
    else:
        state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
        base = Path(state) / "pr-dashboard" / "watch"
    base.mkdir(parents=True, exist_ok=True)
    return base


def repo_key(repo: str) -> str:
    """owner/repo -> owner__repo, safe as a filename stem."""
    return repo.replace("/", "__")


def snapshot_pr(pr: dict, review_comments: dict | None = None, prev: dict | None = None) -> dict:
    """Reduce a gh PR object to the durable state we diff against.

    We store hashes and stable ids, never comment/review bodies — this is the
    "counts + hash" tier: enough to detect add/edit/remove without keeping text.

    `review_comments` is fetch_review_comments' result. The "reviewComments" key
    is written only when this PR's inline comments were actually measured; an
    unmeasured PR never stores {}, because a stored empty is indistinguishable
    from "no inline comments" on the next run and would report every existing
    one as new.

    When this run could not measure, `prev`'s measurement is carried forward
    rather than dropped. Dropping it overwrites the last good baseline with a
    hole, so comments left during a single failed run never surface — not in
    that run, and not in any later one, because the next run has nothing to diff
    against. Carrying it forward still reports nothing now (the two sides are
    equal) but defers the delta instead of destroying it.
    """
    comments = {c["id"]: _sha(c.get("body", "")) for c in pr.get("comments") or []}
    reviews = {
        r["id"]: {
            "state": r.get("state", ""),
            "author": (r.get("author") or {}).get("login", ""),
            "hash": _sha(r.get("body", "")),
        }
        for r in pr.get("reviews") or []
    }
    snap = {
        "number": pr["number"],
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "draft": pr.get("isDraft", False),
        "reviewDecision": pr.get("reviewDecision") or "",
        "bodyHash": _sha(pr.get("body", "")),
        "comments": comments,
        "reviews": reviews,
    }
    inline = (review_comments or {}).get(str(pr["number"]))
    if inline is None:
        inline = (prev or {}).get("reviewComments")
    if inline is not None:
        snap["reviewComments"] = inline
    return snap


def diff_pr(prev: dict | None, cur: dict) -> list[str]:
    """Human-readable change lines for one PR, prev baseline vs current snapshot.

    Empty list means nothing moved. `prev is None` means the PR is newly tracked
    (a first-sight; not itself reported as a change — the caller flags it).
    """
    if prev is None:
        return []

    lines: list[str] = []

    if prev.get("draft", False) != cur["draft"]:
        lines.append("converted to draft" if cur["draft"] else "marked ready for review")

    if prev.get("reviewDecision", "") != cur["reviewDecision"]:
        a = prev.get("reviewDecision") or "none"
        b = cur["reviewDecision"] or "none"
        lines.append(f"approval: {a} → {b}")

    if prev.get("bodyHash") != cur["bodyHash"]:
        lines.append("body edited")

    # Comments — keyed by stable gh id. New = unseen id; edited = same id, new
    # body hash; removed = a previously seen id now gone (deleted comment).
    pc, cc = prev.get("comments", {}), cur["comments"]
    new_c = [i for i in cc if i not in pc]
    edited_c = [i for i in cc if i in pc and pc[i] != cc[i]]
    removed_c = [i for i in pc if i not in cc]
    if new_c or edited_c or removed_c:
        parts = []
        if new_c:
            parts.append(f"+{len(new_c)} new")
        if edited_c:
            parts.append(f"{len(edited_c)} edited")
        if removed_c:
            parts.append(f"{len(removed_c)} removed")
        lines.append(f"comments: {', '.join(parts)} (now {len(cc)})")

    # Inline review comments — same add/edit/remove shape as the conversation
    # thread above, but diffed ONLY when both sides were measured. A baseline
    # written before this field existed, or a run whose GraphQL call failed or
    # paged out, must report nothing here: comparing against an absent side
    # would invent either a wave of additions (old baseline) or a wave of
    # removals (failed fetch), and both land on the PRs under heaviest review.
    pi, ci = prev.get("reviewComments"), cur.get("reviewComments")
    if isinstance(pi, dict) and isinstance(ci, dict):
        new_i = [i for i in ci if i not in pi]
        edited_i = [i for i in ci if i in pi and pi[i] != ci[i]]
        removed_i = [i for i in pi if i not in ci]
        if new_i or edited_i or removed_i:
            parts = []
            if new_i:
                parts.append(f"+{len(new_i)} new")
            if edited_i:
                parts.append(f"{len(edited_i)} edited")
            if removed_i:
                parts.append(f"{len(removed_i)} removed")
            lines.append(f"review comments: {', '.join(parts)} (now {len(ci)})")

    # Reviews — a new review id, the same reviewer re-submitting with a different
    # state (COMMENTED → CHANGES_REQUESTED, etc.), or an edit to the review body
    # (same id + state, changed hash — the same "counts + hash" signal as comments).
    pr_, cr = prev.get("reviews", {}), cur["reviews"]
    for rid, r in cr.items():
        who = r.get("author") or "someone"
        old = pr_.get(rid)
        if old is None:
            lines.append(f"review by @{who}: {r['state'] or 'commented'}")
        elif old.get("state") != r.get("state"):
            lines.append(f"review by @{who}: {old.get('state') or 'none'} → {r['state']}")
        elif old.get("hash") != r.get("hash"):
            lines.append(f"review by @{who}: body edited")

    return lines


def prune(cache: Path, ttl_days: int, max_repos: int, protect: set[str] | None = None) -> None:
    """Bound the cache: drop baselines older than ttl_days, cap total file count.

    Files are rewritten every run, so mtime == last-watched time. A repo you stop
    watching ages out; the cap is a hard backstop against unbounded growth.

    `protect` holds the repo_key stems watched this run — they're exempt from both
    the TTL and the cap so a single invocation watching more than max_repos repos
    can't delete a baseline it just wrote (which would leave those repos forever
    re-seeding, never diffing).
    """
    protect = protect or set()
    files = sorted(cache.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - ttl_days * 86400
    keep = []
    for f in files:
        if f.stem in protect:
            keep.append(f)
        elif f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
        else:
            keep.append(f)
    # Cap the unprotected survivors; never evict a repo we watched this run.
    unprotected = [f for f in keep if f.stem not in protect]
    for f in unprotected[max_repos:]:  # newest-first; anything past the cap is oldest
        f.unlink(missing_ok=True)


def fetch_prs(repo: str, author: str | None) -> list[dict]:
    """gh pr list for one repo. Surfaces gh's real error (auth/404) and aborts."""
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        PR_FIELDS,
    ]
    if author:
        cmd += ["--author", author]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        console.print(f"[red]gh pr list ({repo}) failed:[/red] {proc.stderr.strip()}")
        sys.exit(1)
    return json.loads(proc.stdout or "[]")


def build_review_comments_query(owner: str, name: str, numbers: list[int]) -> str:
    """One aliased `pullRequest(number: N)` field per PR, each asking for its
    review threads' comments. Page info comes back too — see parse_review_comments
    for why truncation has to be visible rather than silently dropped."""
    frag = " ".join(
        f"pr_{n}: pullRequest(number: {n}) {{ "
        f"reviewThreads(first: {REVIEW_THREAD_PAGE}) {{ "
        f"pageInfo {{ hasNextPage }} "
        f"nodes {{ comments(first: {REVIEW_COMMENT_PAGE}) {{ "
        f"pageInfo {{ hasNextPage }} nodes {{ id body }} }} }} }} }}"
        for n in numbers
    )
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {frag} }} }}'


def parse_review_comments(payload: dict) -> dict:
    """GraphQL payload -> {"<pr number>": {comment_id: body_hash}}.

    A PR whose threads or comments were truncated by the page cap maps to None
    — "not measured" — rather than to a partial dict. A partial dict would diff
    as though every comment past the cap had been deleted, inventing a wave of
    removals on precisely the busiest PRs. Same refusal collect.sh makes for
    `mergeable: UNKNOWN`: a missing measurement must not be able to masquerade
    as an answer.
    """
    repo = (payload.get("data") or {}).get("repository") or {}
    out: dict = {}
    for alias, pr in repo.items():
        if not alias.startswith("pr_") or not isinstance(pr, dict):
            continue
        threads = pr.get("reviewThreads") or {}
        truncated = bool((threads.get("pageInfo") or {}).get("hasNextPage"))
        comments: dict = {}
        for node in threads.get("nodes") or []:
            cs = node.get("comments") or {}
            if (cs.get("pageInfo") or {}).get("hasNextPage"):
                truncated = True
            for c in cs.get("nodes") or []:
                comments[c["id"]] = _sha(c.get("body", ""))
        out[alias[3:]] = None if truncated else comments
    return out


def fetch_review_comments(repo: str, numbers: list[int]) -> dict:
    """Inline review comments for a repo's PRs, keyed by PR number as a string.

    Issued in chunks of REVIEW_PRS_PER_QUERY to stay under the node limit; a
    chunk that fails contributes nothing and leaves only its own PRs unmeasured,
    rather than discarding the repo.

    Best-effort by design: a PR that cannot be measured is absent from the
    result, never present-and-empty. Absent means "not measured" and is skipped
    by diff_pr; empty would mean "no inline comments" and would report every
    existing one as removed.
    """
    out: dict = {}
    for i in range(0, len(numbers), REVIEW_PRS_PER_QUERY):
        out.update(_fetch_review_comment_chunk(repo, numbers[i : i + REVIEW_PRS_PER_QUERY]))
    return out


def _fetch_review_comment_chunk(repo: str, numbers: list[int]) -> dict:
    if not numbers:
        return {}
    owner, _, name = repo.partition("/")
    proc = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={build_review_comments_query(owner, name, numbers)}",
        ],
        capture_output=True,
        text=True,
    )
    # Parse stdout even on a non-zero exit. gh exits 1 whenever the response
    # carries an `errors` array, including when that is one unresolvable alias
    # sitting beside complete data for every other PR in the chunk — treating
    # the exit code as the verdict would throw all of it away. A rejected query
    # (node limit, auth) returns no `data`, so this yields {} there anyway, and
    # a nulled alias is skipped by parse_review_comments and stays unmeasured.
    try:
        parsed = parse_review_comments(json.loads(proc.stdout or "{}"))
    except json.JSONDecodeError:
        parsed = {}
    if not parsed and proc.returncode != 0:
        console.print(
            f"[yellow]inline review comments unavailable for {repo}:[/yellow] "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    return parsed


def load_baseline(cache: Path, repo: str) -> dict:
    f = cache / f"{repo_key(repo)}.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return {}  # corrupt baseline → treat as first run, don't crash the report


def save_baseline(cache: Path, repo: str, scope: str, prs: dict) -> None:
    f = cache / f"{repo_key(repo)}.json"
    payload = {
        "repo": repo,
        "scope": scope,
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prs": prs,
    }
    # Write a sibling and rename, rather than truncating in place. load_baseline
    # treats a JSONDecodeError as "first run" and returns {}, so a half-written
    # file never surfaces as an error -- it silently re-seeds, and the delta the
    # next run was supposed to report is gone. os.replace is atomic within a
    # directory, so a reader sees either the old baseline or the new one. The pid
    # in the temp name keeps two concurrent writers off each other's scratch file.
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=0))
        os.replace(tmp, f)
    finally:
        tmp.unlink(missing_ok=True)


def report_repo(
    repo: str, baseline: dict, current: list[dict], review_comments: dict | None = None
) -> bool:
    """Print one repo's change block. Returns True if anything moved."""
    prev_prs = baseline.get("prs", {})
    first_run = not baseline  # no baseline file at all

    cur_by_num = {
        str(pr["number"]): snapshot_pr(pr, review_comments, prev_prs.get(str(pr["number"])))
        for pr in current
    }

    new_prs, changed = [], []
    for num, snap in cur_by_num.items():
        prev = prev_prs.get(num)
        if prev is None:
            new_prs.append(snap)
        else:
            lines = diff_pr(prev, snap)
            if lines:
                changed.append((snap, lines))

    # PRs we tracked before that are no longer open (merged/closed).
    gone = [prev_prs[n] for n in prev_prs if n not in cur_by_num]

    moved = bool(changed or gone or (new_prs and not first_run))

    if first_run:
        console.print(
            f"[bold]{repo}[/bold] — baseline recorded ([cyan]{len(cur_by_num)}[/cyan] open PRs, nothing to diff yet)"
        )
        return False

    unmeasured = [n for n in cur_by_num if "reviewComments" not in cur_by_num[n]]
    if unmeasured:
        console.print(
            f"  [yellow]inline review comments not measured for {len(unmeasured)} PR(s)[/yellow]"
            " [dim](reported as no change, not as none)[/dim]"
        )

    when = baseline.get("fetchedAt", "?")
    if not moved:
        console.print(
            f"[green]✓[/green] [bold]{repo}[/bold] — no changes since {when} ([dim]{len(cur_by_num)} PRs[/dim])"
        )
        return False

    console.print(f"[bold]{repo}[/bold] — changes since {when}:")
    for snap, lines in sorted(changed, key=lambda x: x[0]["number"]):
        console.print(f"  [cyan]#{snap['number']}[/cyan] {snap['title']}")
        for ln in lines:
            console.print(f"      [yellow]●[/yellow] {ln}")
        console.print(f"      [dim]{snap['url']}[/dim]")
    for snap in sorted(new_prs, key=lambda s: s["number"]):
        console.print(
            f"  [green]#{snap['number']}[/green] {snap['title']}  [green](new — now tracking)[/green]"
        )
    for snap in sorted(gone, key=lambda s: s["number"]):
        console.print(
            f"  [magenta]#{snap['number']}[/magenta] {snap.get('title', '')}  [magenta](closed / no longer open)[/magenta]"
        )
    return True


def resolve_repos(repos: list[str]) -> list[str]:
    if repos:
        return repos
    cur = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    name = cur.stdout.strip()
    if cur.returncode != 0 or not name:
        console.print(
            "[red]No repo given and the current dir isn't a GitHub repo.[/red] "
            "Pass owner/repo [owner/repo ...]."
        )
        sys.exit(1)
    return [name]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Report what changed on your open PRs since you last checked.",
    )
    p.add_argument("repos", nargs="*", help="owner/repo (default: current dir's repo)")
    scope = p.add_mutually_exclusive_group()
    # No argparse default: `None` is the sentinel for "not passed", which lets the
    # loop below tell an explicit `--author @me` apart from a bare run and adopt
    # the cached scope for the latter. DEFAULT_AUTHOR still applies with no cache.
    scope.add_argument(
        "--author",
        default=None,
        metavar="LOGIN",
        help="only this author's PRs (default: the watched scope, else @me)",
    )
    scope.add_argument("--all", action="store_true", help="every open PR (maintainer watch)")
    p.add_argument(
        "--reset", action="store_true", help="forget the baseline for the named repos, then re-seed"
    )
    p.add_argument(
        "--ttl-days", type=int, default=14, help="drop baselines older than N days (default 14)"
    )
    p.add_argument(
        "--max-repos", type=int, default=200, help="cap cached repo baselines (default 200)"
    )
    args = p.parse_args(argv)

    # Scope is part of a baseline's identity, so only an *explicit* flag should
    # change it. A bare run adopts whatever scope the baseline was seeded under —
    # otherwise `watch.py` with no flags (what a handoff note says to run, and what
    # habit types) silently re-baselines an --all watch down to @me, destroying the
    # pending delta. Resolved per repo, since each carries its own baseline.
    explicit_scope = args.all or args.author is not None
    cache = cache_dir()
    repos = resolve_repos(args.repos)

    any_moved = False
    for repo in repos:
        baseline = load_baseline(cache, repo)
        if explicit_scope:
            scope_label = ALL_SCOPE if args.all else args.author
        else:
            # Read before --reset drops the file: "forget what I've seen" shouldn't
            # also quietly mean "and switch me to @me".
            scope_label = baseline.get("scope") or DEFAULT_AUTHOR
        author = None if scope_label == ALL_SCOPE else scope_label

        if args.reset:
            (cache / f"{repo_key(repo)}.json").unlink(missing_ok=True)
            baseline = {}
        # A baseline captured under a different scope (--author @me vs --all) was
        # filtered to a different PR set, so diffing it would report out-of-scope
        # PRs as "closed" and in-scope ones as "new". Re-baseline instead. Only
        # reachable via an explicit flag now, i.e. a scope change you asked for.
        if baseline and baseline.get("scope") != scope_label:
            console.print(
                f"[bold]{repo}[/bold] — scope changed "
                f"{baseline.get('scope')} → {scope_label}, re-baselining"
            )
            baseline = {}
        current = fetch_prs(repo, author)
        review_comments = fetch_review_comments(repo, [pr["number"] for pr in current])
        if report_repo(repo, baseline, current, review_comments):
            any_moved = True
        prev_prs = baseline.get("prs", {})
        save_baseline(
            cache,
            repo,
            scope_label,
            {
                str(pr["number"]): snapshot_pr(pr, review_comments, prev_prs.get(str(pr["number"])))
                for pr in current
            },
        )

    prune(cache, args.ttl_days, args.max_repos, protect={repo_key(r) for r in repos})

    if len(repos) > 1 and not any_moved:
        console.print("\n[dim]Nothing moved across any watched repo.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
