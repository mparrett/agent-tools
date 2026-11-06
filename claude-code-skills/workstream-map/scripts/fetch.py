#!/usr/bin/env python3
"""Pull the facts for a workstream map.

Usage: fetch.py <config.json> [--data DIR]
       fetch.py <config.json> --preview [--near N]

For every repo in the config: search issues and PRs the user is involved in
(author, commenter, mentioned, assigned), score each against the relevance
regex, then enrich the selected set with comments and PR metadata. Writes:

  <data>/items-<owner>__<name>.json  every item the search returned (unfiltered)
  <data>/selected.json               {"owner/name": [numbers]} that made the cut
  <data>/enrich.json                 per node: comments (author, date, body), PR
                                     base/head/merged/reviews/closing issues

`--preview` stops after the search and writes nothing. Scoring reads only the
title, labels and body, all of which the search already returned, so a preview
costs two `gh search` calls per repo whatever the theme selects (each paginates
at 100 results, so a few HTTP requests). Tune
`relevance`, `min_score`, `body_cap` and the lane regexes against its output,
then run the real fetch once.

Needs `gh` authenticated. Roughly one API call per selected node (two for a
PR); a 200-node map takes two to three minutes. Every `gh` call that feeds the
map is required: a failure aborts before any data file is replaced, so a
transient API error cannot leave a valid-looking map with a repo's PRs missing.
"""

import collections
import json
import os
import re
import subprocess
import sys

# Loaded by path rather than by name: fetch.py runs as a script, and the tests
# load it through importlib, so scripts/ is not on sys.path either way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import laning  # noqa: E402


class FetchError(RuntimeError):
    pass


def gh(*args, required=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        if required:
            raise FetchError(f"gh {' '.join(args[:3])}... failed: {r.stderr.strip()[:300]}")
        return ""
    return r.stdout


def is_404(stdout, stderr):
    """Did `gh` fail because the item does not exist, or for some other reason?

    Read the response body, which `gh api` writes to stdout even on an error
    status. Do NOT look for "404" anywhere in the rendered error: two of gh's
    three error formats embed the request URL, so a 502 on issue #404 read as
    "missing" and the node was dropped from a map that still looked complete —
    the exact failure this function exists to prevent.
    """
    try:
        body = json.loads(stdout)
        if isinstance(body, dict) and str(body.get("status")) == "404":
            return True
    except ValueError:
        pass
    return bool(re.search(r"\(HTTP 404\)\s*$", stderr.strip()))


def gh_or_missing(*args):
    """-> (stdout, missing). Raises on any failure that is not a 404.

    A 404 is the caller's config to fix; anything else aborts the run like the
    rest of the fetch does.
    """
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout, False
    if is_404(r.stdout, r.stderr):
        return "", True
    raise FetchError(f"gh {' '.join(args[:3])}... failed: {r.stderr.strip()[:300]}")


def split_pages(text):
    """Parse the back-to-back JSON documents `gh api --paginate` emits.

    One array per page, no separator. A regex on `][` mis-splits any page whose
    comment text contains `[a][b]` (reference-style Markdown), so decode
    document by document instead.
    """
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        doc, end = dec.raw_decode(text, i)
        out.extend(doc if isinstance(doc, list) else [doc])
        i = end
    return out


def repo_slug(repo):
    return repo.replace("/", "__")


def load_config(path):
    cfg = json.load(open(path))
    cfg.setdefault("min_score", 3)
    cfg.setdefault("lanes", [])  # preview runs before lanes exist
    cfg.setdefault("include", {})
    cfg.setdefault("exclude", {})
    return cfg


def search(repo, user):
    fields = "number,title,state,isPullRequest,labels,createdAt,closedAt,updatedAt,author,url,body"
    items = {}
    for kind in ("issues", "prs"):
        out = gh(
            "search", kind, "--repo", repo, "--involves", user, "--limit", "1000", "--json", fields
        )
        for it in json.loads(out):
            items[it["number"]] = it
    return items


def score(it, rx, body_cap=6):
    """3 per hit in the title or labels, 1 per hit in the body up to body_cap.

    With min_score 3 the default lets three body mentions select an item with
    no title hit. On a repo where the theme's words are everyday vocabulary
    that over-selects badly; set `body_cap` to 2 in the config so a title hit
    is required and the body only breaks ties.
    """
    labels = " ".join(lb["name"] for lb in it.get("labels", []))
    return len(rx.findall(it["title"] + " " + labels)) * 3 + min(
        len(rx.findall(it.get("body") or "")), body_cap
    )


def view(repo, n):
    """Fetch one item we did not get from search (an include entry).

    The issues endpoint serves PRs too and says which is which through the
    `pull_request` key; `gh issue view` accepts a PR number silently, so it
    cannot be used to tell them apart.
    """
    raw, missing = gh_or_missing("api", f"repos/{repo}/issues/{n}")
    if missing:
        return None
    d = json.loads(raw)
    it = {
        "number": d["number"],
        "title": d["title"],
        "state": d["state"].upper(),
        "isPullRequest": d.get("pull_request") is not None,
        "labels": [{"name": lb["name"]} for lb in d.get("labels", [])],
        "createdAt": d["created_at"],
        "closedAt": d.get("closed_at"),
        "updatedAt": d.get("updated_at"),
        "author": {"login": (d.get("user") or {}).get("login", "")},
        "url": d["html_url"],
        "body": d.get("body") or "",
    }
    if it["isPullRequest"]:
        pr = json.loads(gh("pr", "view", str(n), "--repo", repo, "--json", "mergedAt,state"))
        if pr.get("mergedAt"):
            it["state"] = "MERGED"
    return it


def enrich_one(repo, n, is_pr):
    rec = {"comments": [], "pr": None}
    c = gh(
        "api",
        f"repos/{repo}/issues/{n}/comments",
        "--paginate",
        "--jq",
        "[.[] | {a:.user.login,t:.created_at[0:10],b:.body}]",
    )
    rec["comments"] = split_pages(c)
    if is_pr:
        p = gh(
            "pr",
            "view",
            str(n),
            "--repo",
            repo,
            "--json",
            "baseRefName,headRefName,mergedAt,reviews,closingIssuesReferences,additions,deletions",
        )
        rec["pr"] = json.loads(p)
    return rec


REASON_MARK = {"override": "o", "match": "m", "default": "d"}


def selection(cfg, items, repo):
    """Split one repo's search results the way the real fetch would.

    Returns (selected, near_miss, dropped): selected is [(number, score,
    reason, lane)], near_miss is [(number, score)] for items that scored
    something but not enough, dropped is the count removed by `exclude`.
    """
    rx = re.compile(cfg["relevance"], re.I)
    cap = cfg.get("body_cap", 6)
    floor = cfg["min_score"]
    assign = laning.make_lane_assigner(cfg)
    excluded = set(cfg["exclude"].get(repo, []))
    included = set(cfg["include"].get(repo, []))

    selected, near_miss, dropped = [], [], 0
    for n, it in sorted(items.items()):
        sc = score(it, rx, cap)
        # main() subtracts `exclude` and then adds `include`, so an id in both
        # is selected; preview has to resolve them in the same order.
        keep = (sc >= floor and n not in excluded) or n in included
        if keep:
            labels = " ".join(lb["name"] for lb in it.get("labels", []))
            lane, reason = assign(repo, n, it["title"], labels)
            selected.append((n, sc, reason, lane))
        elif sc >= floor:
            dropped += 1
        elif sc > 0:
            near_miss.append((n, sc))
    near_miss.sort(key=lambda r: (-r[1], r[0]))
    return selected, near_miss, dropped


def preview(cfg, near=15):
    """Search only, report what the config would select, write nothing."""
    lane_name = {ln["key"]: ln["name"] for ln in cfg["lanes"]}
    total, calls = 0, 0
    for repo in cfg["repos"]:
        items = search(repo, cfg["user"])
        calls += 2
        selected, near_miss, dropped = selection(cfg, items, repo)
        total += len(selected)
        cap = cfg.get("body_cap", 6)
        print(
            f"\n{repo}: {len(items)} involved, {len(selected)} selected"
            f"  (min_score {cfg['min_score']}, body_cap {cap})"
        )
        if dropped:
            print(f"  {dropped} more scored high enough and are in `exclude`")

        hist = collections.Counter(sc for _, sc, _, _ in selected)
        print("  scores: " + "  ".join(f"{sc}x{c}" for sc, c in sorted(hist.items())))

        by_lane = collections.defaultdict(list)
        for row in selected:
            by_lane[row[3]].append(row)
        if not cfg["lanes"]:
            print("  lanes: none configured yet; titles in number order")
            for n, sc, _, _ in selected:
                print(f"    #{n:<6} {sc:>2}  {items[n]['title']}")
        else:
            # configured order first, then any key only `default_lane` or an
            # override names -- a typo there must show up, not vanish.
            keys = [ln["key"] for ln in cfg["lanes"]]
            keys += [k for k in sorted(by_lane) if k not in keys]
            for key in keys:
                rows = by_lane.get(key)
                if not rows:
                    continue
                if key not in lane_name:
                    print(f"  [{key}] NOT A CONFIGURED LANE  {len(rows)}")
                fell = sum(1 for r in rows if r[2] == "default")
                tail = f"  ({fell} by default_lane)" if fell else ""
                print(f"  [{key}] {lane_name.get(key, key)}  {len(rows)}{tail}")
                for n, sc, reason, _ in rows:
                    print(f"    #{n:<6} {sc:>2} {REASON_MARK[reason]}  {items[n]['title']}")

        missing = [n for n in cfg["include"].get(repo, []) if n not in items]
        if missing:
            print(f"  include, not in the search results (fetched one call each): {missing}")
        if near_miss:
            shown = near_miss[:near]
            print(
                f"  near misses (scored 1-{cfg['min_score'] - 1}): {len(near_miss)}, top {len(shown)}"
            )
            for n, sc in shown:
                print(f"    #{n:<6} {sc:>2}     {items[n]['title']}")

    print(f"\npreview made {calls} `gh search` calls and wrote nothing.")
    print(f"{total} nodes selected; the full fetch will make roughly {total} to {total * 2} more.")
    print("`m` matched a lane regex, `o` an override, `d` fell through to default_lane.")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cfg = load_config(sys.argv[1])
    if "--preview" in sys.argv:
        i = sys.argv.index("--near") + 1 if "--near" in sys.argv else 0
        if i >= len(sys.argv):
            sys.exit("--near needs a number")
        near = int(sys.argv[i]) if i else 15
        try:
            preview(cfg, near)
        except FetchError as e:
            sys.exit(str(e))
        return
    data = (
        sys.argv[sys.argv.index("--data") + 1]
        if "--data" in sys.argv
        else cfg.get("data_dir", "data")
    )
    os.makedirs(data, exist_ok=True)
    rx = re.compile(cfg["relevance"], re.I)
    all_items, selected, enrich, unresolved = {}, {}, {}, {}
    try:
        for repo in cfg["repos"]:
            items = search(repo, cfg["user"])
            cap = cfg.get("body_cap", 6)
            nums = {n for n, it in items.items() if score(it, rx, cap) >= cfg["min_score"]}
            nums -= set(cfg["exclude"].get(repo, []))
            for n in cfg["include"].get(repo, []):
                if n not in items:
                    it = view(repo, n)
                    if it:
                        items[n] = it
                    else:
                        unresolved.setdefault(repo, []).append(n)
                if n in items:
                    nums.add(n)
            all_items[repo] = items
            selected[repo] = sorted(nums)
            print(f"{repo}: {len(items)} involved, {len(nums)} selected", flush=True)
            for n in sorted(nums):
                key = f"{repo}#{n}"
                enrich[key] = enrich_one(repo, n, items[n]["isPullRequest"])
                print(f"  {key} comments={len(enrich[key]['comments'])}", flush=True)
    except FetchError as e:
        sys.exit(f"aborting before writing any data file: {e}")
    # everything succeeded; only now replace the data files
    for repo, items in all_items.items():
        json.dump(items, open(f"{data}/items-{repo_slug(repo)}.json", "w"))
    json.dump(selected, open(f"{data}/selected.json", "w"))
    json.dump(enrich, open(f"{data}/enrich.json", "w"))
    for repo, nums in unresolved.items():
        print(f"warning: `include` numbers not found in {repo}: {sorted(nums)}")
    print("done")


if __name__ == "__main__":
    main()
