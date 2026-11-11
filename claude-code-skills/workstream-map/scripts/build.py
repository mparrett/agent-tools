#!/usr/bin/env python3
"""Render a workstream map from fetched data.

Usage: build.py <config.json> [--data DIR] [--date YYYY-MM-DD]

Reads <data>/items-*.json, selected.json, enrich.json (from fetch.py) and the
config's judgment tables (lanes, overrides, start_here, notes_dir), then writes
the HTML map (out_html) and the markdown inventory (out_md). Paths in the
config (data_dir, out_html, out_md, notes_dir) are relative to the config file;
--data is relative to the cwd. Node id is
"owner/name#N"; edges are #-references found in bodies and comments, both
directions, restricted to nodes in the map.
"""

import collections
import datetime as dt
import glob
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# by path, not by name: build.py runs as a script and the tests exec it as one.
sys.path.insert(0, HERE)

import laning  # noqa: E402

# Validated categorical palette (dataviz skill, light mode); at most 8 lanes.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7", "#eda100", "#e87ba4", "#008300", "#e34948"]
# `#12`, `owner/name#12`, or the short `name#12`; `/#12` (a URL fragment) is not a reference.
REF = re.compile(r"(?:(?P<repo>[\w.-]+(?:/[\w.-]+)?)#|(?<![\w/])#)(?P<num>\d{1,6})\b")


def make_ref_resolver(repos):
    """Map a reference's repo part (full, short, or absent) onto a configured repo."""
    short = {r.split("/")[-1].lower(): r for r in repos}
    full = {r.lower(): r for r in repos}

    def resolve(m, current):
        part = (m.group("repo") or "").lower()
        if not part:
            return current
        if part in full:
            return full[part]
        if part in short:
            return short[part]
        if "/" in part:
            return None  # a repo we do not map: external/project#12 is not a node here
        return current  # "PR#12", "step#3": some word glued to the hash, read as the current repo

    return resolve


def md_cell(s):
    """Escape a value for one Markdown table cell: pipes split cells, a trailing
    backslash escapes the delimiter, newlines end the row."""
    return html.escape(str(s)).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def script_json(obj):
    """JSON for an inline <script>: `</script>` in a title would end the data script
    and start an attacker's, so escape the characters that can form tags."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def config_relative(cfg_file, path):
    """Paths written in the config resolve against the config's directory, not the
    shell's cwd: a build run from another checkout otherwise writes into that one."""
    return os.path.join(os.path.dirname(os.path.abspath(cfg_file)), os.path.expanduser(path))


def write_text(path, text):
    path = os.path.expanduser(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    open(path, "w").write(text)
    return path


def report(nodes, lane_reason, cited_outside, cfg, lanes, items, top=10):
    """What to fix in the config, printed instead of looked for in a browser.

    Four checks, each pointing at one config table: nodes no lane regex claimed
    (a lane rule or an `override`), numbers the map's own nodes cite but do not
    contain (`include`), `include` entries that never arrived, and lane names
    too long for a column at the template's minimum lane width.
    """
    missing = [
        f"{repo}#{n}"
        for repo, nums in cfg.get("include", {}).items()
        for n in nums
        if n not in items.get(repo, {})
    ]
    if missing:
        print(f"\n`include` entries not in the fetched data ({len(missing)}):")
        for k in missing:
            print(f"  {k}")
        print("  a wrong number, a deleted item, or `include` grew since the last fetch")

    fell = [k for k, r in lane_reason.items() if r == "default"]
    if fell:
        print(f"\nno lane rule matched ({len(fell)}) -- a lane `match` or an `override`:")
        for k in sorted(fell, key=lambda k: nodes[k]["created"]):
            print(f"  {nodes[k]['lane']:10s} {k:22s} {nodes[k]['title'][:70]}")

    excluded = {f"{repo}#{n}" for repo, nums in cfg.get("exclude", {}).items() for n in nums}
    outside = [(k, c) for k, c in cited_outside.most_common() if c > 1]
    if outside:
        print(f"\ncited by the map but not in it ({len(outside)} cited by two or more nodes):")
        for k, c in outside[:top]:
            mark = "  (in `exclude`)" if k in excluded else ""
            print(f"  {k:22s} cited by {c} nodes{mark}")
        if len(outside) > top:
            print(f"  ... {len(outside) - top} more")

    over = laning.overflowing_lane_names(lanes)
    if over:
        cap = laning.header_max_chars()
        print(f"\nlane names that will overflow a narrow column (>{cap} chars, cannot wrap):")
        for key, word in over:
            print(f"  [{key}] {word}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cfg_file = sys.argv[1]
    cfg = json.load(open(cfg_file))
    argv = sys.argv
    # --data is a command-line path, so it stays relative to the cwd like any other
    data = (
        argv[argv.index("--data") + 1]
        if "--data" in argv
        else config_relative(cfg_file, cfg.get("data_dir", "data"))
    )
    # "2026-09-15 04:30 UTC" by default; --date accepts any string (tests pass a bare date)
    today = (
        argv[argv.index("--date") + 1]
        if "--date" in argv
        else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )
    user = cfg["user"]
    repos = cfg["repos"]
    primary = cfg.get("primary_repo", repos[0])
    lanes = cfg["lanes"]
    if len(lanes) > len(PALETTE):
        sys.exit(f"at most {len(PALETTE)} lanes; fold the rest")
    pal = {ln["key"]: PALETTE[i] for i, ln in enumerate(lanes)}
    start_here = cfg.get("start_here", {})

    items = {}
    for repo in repos:
        # owner__name, so org-a/service and org-b/service never share a file
        slug = repo.replace("/", "__")
        items[repo] = {int(k): v for k, v in json.load(open(f"{data}/items-{slug}.json")).items()}
    selected = json.load(open(f"{data}/selected.json"))
    enrich = json.load(open(f"{data}/enrich.json"))

    # optional: notes that mention a node (e.g. an agent memory dir)
    notes = collections.defaultdict(set)
    notes_dir = cfg.get("notes_dir", "") or ""
    notes_dir = notes_dir and config_relative(cfg_file, notes_dir)
    if notes_dir and os.path.isdir(notes_dir):
        res = make_ref_resolver(repos)
        for path in glob.glob(os.path.join(notes_dir, "*.md")):
            name = os.path.basename(path)[:-3]
            if name == "MEMORY":
                continue
            for m in REF.finditer(open(path, errors="ignore").read()):
                # a bare #N in a note means the primary repo; other repos must be named
                notes[f"{res(m, primary)}#{int(m.group('num'))}"].add(name)

    assign_lane = laning.make_lane_assigner(cfg)
    lane_reason = {}

    def label(repo, n):
        return f"#{n}" if repo == primary else f"{repo.split('/')[-1]}#{n}"

    nodes = {}
    for repo, nums in selected.items():
        for n in nums:
            it = items[repo].get(n)
            if not it:
                continue
            key = f"{repo}#{n}"
            e = enrich.get(key, {})
            pr = e.get("pr") or {}
            state = it["state"].lower()
            if it["isPullRequest"] and pr.get("mergedAt"):
                state = "merged"
            closed = it.get("closedAt") or ""
            closed = "" if closed.startswith("0001") else closed[:10]
            labels = " ".join(lb["name"] for lb in it.get("labels", []))
            engaged = {c["a"] for c in e.get("comments", [])} | {
                (r.get("author") or {}).get("login", "") for r in (pr.get("reviews") or [])
            }
            engaged.discard("")
            engaged.discard(it["author"]["login"])
            lane, lane_reason[key] = assign_lane(repo, n, it["title"], labels)
            nodes[key] = dict(
                id=key,
                repo=repo,
                num=n,
                label=label(repo, n),
                pr=it["isPullRequest"],
                state=state,
                title=it["title"],
                author=it["author"]["login"],
                created=it["createdAt"][:10],
                closed=closed,
                merged=(pr.get("mergedAt") or "")[:10],
                url=it["url"],
                lane=lane,
                labels=labels,
                base=pr.get("baseRefName", ""),
                head=pr.get("headRefName", ""),
                notes=sorted(notes.get(key, [])),
                start=start_here.get(key, ""),
                engaged=sorted(engaged),
            )

    resolve = make_ref_resolver(repos)

    def refs_in(text, repo):
        out = set()
        for m in REF.finditer(text or ""):
            r = resolve(m, repo)
            if r:
                out.add(f"{r}#{int(m.group('num'))}")
        return out

    edges = collections.Counter()
    # references that resolve to a configured repo but land outside the map:
    # the honest `include` candidates, since the theme's own nodes cite them.
    cited_outside = collections.Counter()
    for key, nd in nodes.items():
        repo = nd["repo"]
        it = items[repo][nd["num"]]
        e = enrich.get(key, {})
        texts = [it.get("body", "")] + [c.get("b", "") for c in e.get("comments", [])]
        outside_here = set()
        for text in texts:
            for r in refs_in(text, repo):
                if r == key:
                    continue
                if r in nodes:
                    edges[(key, r)] += 1
                else:
                    outside_here.add(r)
        # once per citing node, not once per mention: "more than one item points
        # at this" is the signal, and one node saying #77 in two comments is not it
        for r in outside_here:
            cited_outside[r] += 1
        for ci in (e.get("pr") or {}).get("closingIssuesReferences") or []:
            r = f"{repo}#{ci['number']}"
            if r in nodes:
                edges[(key, r)] += 2
    deg = collections.Counter()
    for a, b in edges:
        deg[a] += 1
        deg[b] += 1
    for k, nd in nodes.items():
        nd["deg"] = deg[k]

    # people who authored or engaged on the most items, for the contributor filter
    people = collections.Counter()

    def is_bot(who):
        return who.endswith("[bot]") or who in ("github-advanced-security", "dependabot")

    for nd in nodes.values():
        for who in [nd["author"], *nd["engaged"]]:
            if not is_bot(who):
                people[who] += 1
    contributors = [dict(login=who, n=c) for who, c in people.most_common(10)]
    out = dict(
        generated=today,
        lanes=[[ln["key"], ln["name"]] for ln in lanes],
        nodes=sorted(nodes.values(), key=lambda n: n["created"]),
        edges=[dict(s=a, t=b, w=w) for (a, b), w in edges.items()],
        contributors=contributors,
    )
    json.dump(out, open(f"{data}/map-data.json", "w"))
    print(f"nodes {len(nodes)} edges {len(edges)}")
    for ln in lanes:
        n_lane = sum(1 for n in nodes.values() if n["lane"] == ln["key"])
        fell = sum(
            1 for k, n in nodes.items() if n["lane"] == ln["key"] and lane_reason[k] == "default"
        )
        tail = f"   {fell} by default_lane" if fell else ""
        print(f"  {ln['name']:40s} {n_lane:3d}{tail}")

    report(nodes, lane_reason, cited_outside, cfg, lanes, items)

    # ---- markdown inventory
    nbrs = collections.defaultdict(set)
    for a, b in edges:
        nbrs[a].add(b)
        nbrs[b].add(a)
    md = [
        f"# {cfg['title']}, as of {today}",
        "",
        cfg.get("subtitle", ""),
        "",
        f"Counts as of {today}: {len(nodes)} nodes, {len(edges)} references. Edges are `#`-references in bodies and comments; the **related** column is the same data. Interactive map: `{cfg.get('out_html', '')}`.",
        "",
    ]
    for lane in lanes:
        ln = [n for n in out["nodes"] if n["lane"] == lane["key"]]
        if not ln:
            continue
        md += [f"## {lane['name']}", ""]
        starts = [n for n in ln if n["start"]]
        if starts:
            md += ["Start here: " + "; ".join(f"{n['label']} ({n['start']})" for n in starts), ""]
        md += [
            "| # | type | state | opened | author | title | related | notes |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for n in ln:
            rel = ", ".join(nodes[k]["label"] for k in sorted(nbrs[n["id"]]))
            md.append(
                f"| [{n['label']}]({n['url']}) | {'PR' if n['pr'] else 'issue'} | {n['state']} | {n['created']} | {md_cell(n['author'])} | {md_cell(n['title'][:90])} | {rel} | {md_cell(', '.join(n['notes'][:3]))} |"
            )
        md.append("")
    out_md = write_text(config_relative(cfg_file, cfg["out_md"]), "\n".join(md))

    # ---- HTML
    tpl = open(os.path.join(HERE, "..", "template.html")).read()
    page = (
        tpl.replace("__DATA__", script_json(out))
        .replace("__PAL__", script_json(pal))
        .replace("__USER_JSON__", script_json(user))
        .replace("__USER__", html.escape(user))
        .replace("__TITLE__", html.escape(cfg["title"]))
        .replace("__SUBTITLE__", html.escape(cfg.get("subtitle", "")))
        .replace("__TODAY__", html.escape(today))
    )
    out_html = write_text(config_relative(cfg_file, cfg["out_html"]), page)
    # agent-consumable copies next to the page, under fixed names the header links to
    side = os.path.dirname(out_html)
    write_text(os.path.join(side, "data.json"), json.dumps(out, indent=1))
    write_text(os.path.join(side, "inventory.md"), "\n".join(md))
    print("wrote", out_html, "and", out_md, "(+ data.json, inventory.md beside the page)")


if __name__ == "__main__":
    main()
