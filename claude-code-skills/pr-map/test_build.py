#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Unit tests for scripts/build.py, on a synthetic data directory. Run via
`uv run --with pytest claude-code-skills/pr-map/test_build.py` (or `just test`).

They pin the parts that went wrong while the first map was built: an open PR's
`closedAt` of 0001-01-01 leaking into the panel, a `#N` inside a URL path
counted as a reference, a comment on one repo's issue referring to the other
repo by short name, and lane overrides losing to the regex.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

BUILD = Path(__file__).parent / "scripts" / "build.py"
TEMPLATE = Path(__file__).parent / "template.html"

spec = importlib.util.spec_from_file_location(
    "laning", Path(__file__).parent / "scripts" / "laning.py"
)
laning = importlib.util.module_from_spec(spec)
spec.loader.exec_module(laning)

CONFIG = {
    "title": "T",
    "subtitle": "S",
    "user": "alice",
    "repos": ["o/main", "o/side"],
    "primary_repo": "o/main",
    "relevance": "perf",
    "min_score": 3,
    "lanes": [
        {"key": "a", "name": "Lane A", "match": "alpha", "repos": ["o/main"]},
        {"key": "b", "name": "Lane B", "match": "beta"},
    ],
    "default_lane": {"o/main": "a", "o/side": "b"},
    "override": {"o/main#3": "b"},
    "start_here": {"o/main#1": "the tracker"},
    # 999 does not exist in the fixture data: the build has to say so
    "include": {"o/main": [1, 999]},
}


def item(n, title, pr=False, state="OPEN", closed=None, body="", author="alice"):
    return {
        "number": n,
        "title": title,
        "state": state,
        "isPullRequest": pr,
        "labels": [],
        "createdAt": f"2026-06-{n:02d}T00:00:00Z",
        "closedAt": closed,
        "updatedAt": "",
        "author": {"login": author},
        "url": f"https://example.invalid/{n}",
        "body": body,
    }


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    main = {
        1: item(
            1,
            "alpha tracker",
            body="see #2 and side#9, plus https://x/y/#4 which is not a ref, "
            "and external/project#4 which is another repo entirely, and #77 "
            "which is real but outside the map",
        ),
        2: item(2, "alpha PR", pr=True, state="OPEN", closed="0001-01-01T00:00:00Z"),
        3: item(3, "alpha but overridden to b", body="closes #1, follows #77"),
        4: item(4, "unrelated | with a pipe </script><script>alert(1)</script>"),
    }
    side = {9: item(9, "beta thing on the side", body="relates to main#1")}
    json.dump(main, open(d / "items-o__main.json", "w"))
    json.dump(side, open(d / "items-o__side.json", "w"))
    json.dump({"o/main": [1, 2, 3, 4], "o/side": [9]}, open(d / "selected.json", "w"))
    json.dump(
        {
            "o/main#1": {
                "comments": [
                    {"a": "bob", "t": "2026-06-10", "b": "and #3 too"},
                    {"a": "github-actions[bot]", "t": "2026-06-11", "b": "CI report"},
                ],
                "pr": None,
            },
            "o/main#2": {
                "comments": [],
                "pr": {
                    "baseRefName": "main",
                    "headRefName": "wt/2",
                    "mergedAt": None,
                    "reviews": [{"author": {"login": "carol"}, "state": "APPROVED"}],
                    "closingIssuesReferences": [{"number": 1}],
                },
            },
            "o/main#3": {"comments": [], "pr": None},
            "o/main#4": {
                "comments": [
                    {"a": "bob", "t": "2026-06-12", "b": "see #78"},
                    {"a": "bob", "t": "2026-06-13", "b": "still #78"},
                ],
                "pr": None,
            },
            "o/side#9": {"comments": [], "pr": None},
        },
        open(d / "enrich.json", "w"),
    )
    cfg = dict(CONFIG, out_html=str(d / "map.html"), out_md=str(d / "map.md"))
    json.dump(cfg, open(d / "config.json", "w"))
    r = subprocess.run(
        [
            sys.executable,
            str(BUILD),
            str(d / "config.json"),
            "--data",
            str(d),
            "--date",
            "2026-09-15",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    # stdout is the config report; several tests read it rather than the page
    return d, json.load(open(d / "map-data.json")), r.stdout


def nodes_by_id(data):
    return {n["id"]: n for n in data["nodes"]}


def test_open_pr_closed_date_is_blank_not_year_one(built):
    _, data, _ = built
    assert nodes_by_id(data)["o/main#2"]["closed"] == ""


def test_references_skip_url_fragments_and_span_repos(built):
    _, data, _ = built
    pairs = {(e["s"], e["t"]) for e in data["edges"]}
    assert ("o/main#1", "o/main#2") in pairs
    assert ("o/main#1", "o/side#9") in pairs
    assert ("o/side#9", "o/main#1") in pairs
    assert ("o/main#1", "o/main#3") in pairs, "comment references count"
    assert not any(t == "o/main#4" for _, t in pairs), "#4 inside a URL is not a reference"


def test_closing_reference_and_override_and_labels(built):
    _, data, _ = built
    n = nodes_by_id(data)
    assert n["o/main#3"]["lane"] == "b", "override beats the lane regex"
    assert n["o/side#9"]["lane"] == "b"
    assert n["o/side#9"]["label"] == "side#9" and n["o/main#1"]["label"] == "#1"
    assert ("o/main#2", "o/main#1") in {(e["s"], e["t"]) for e in data["edges"]}, (
        "closingIssuesReferences is an edge"
    )
    assert n["o/main#2"]["engaged"] == ["carol"]
    assert n["o/main#1"]["start"] == "the tracker"


def test_outputs_exist_and_carry_the_counts(built):
    d, data, _ = built
    html = (d / "map.html").read_text()
    md = (d / "map.md").read_text()
    assert "__DATA__" not in html and "__TITLE__" not in html
    assert (
        f"Counts as of 2026-09-15: {len(data['nodes'])} nodes, {len(data['edges'])} references"
        in md
    )
    assert "Start here: #1 (the tracker)" in md


def test_a_reference_to_an_unconfigured_repo_is_not_an_edge(built):
    """`external/project#4` must not become an edge to o/main#4 just because #4 exists."""
    _, data, _ = built
    pairs = {(e["s"], e["t"]) for e in data["edges"]}
    assert ("o/main#1", "o/main#4") not in pairs


def test_hostile_titles_cannot_break_the_page_or_the_table(built):
    """A title with `</script>` must not end the data script; a title with `|` must
    stay in its Markdown cell."""
    d, _, _ = built
    html = (d / "map.html").read_text()
    assert "</script><script>alert" not in html
    assert "\\u003c/script\\u003e" in html
    row = next(
        line for line in (d / "map.md").read_text().splitlines() if "example.invalid/4" in line
    )
    assert "\\|" in row and row.count("|") == 9 + 1, row


def test_agent_readable_copies_sit_beside_the_page(built):
    d, data, _ = built
    side = json.loads((d / "data.json").read_text())
    assert side["nodes"] == data["nodes"] and side["edges"] == data["edges"]
    assert (d / "inventory.md").read_text() == (d / "map.md").read_text()
    assert data["contributors"][0]["login"] == "alice"
    logins = {c["login"] for c in data["contributors"]}
    assert logins >= {"alice", "bob", "carol"}
    assert "github-actions[bot]" not in logins, "bots are not people"


def test_output_paths_without_a_directory_are_accepted(tmp_path):
    """The documented `out_md: "map.md"` form has no parent dir to create."""
    d, _ = _build_into(tmp_path, out_html="map.html", out_md="map.md")
    assert (tmp_path / "map.html").exists() and (tmp_path / "map.md").exists()


def _build_into(d, **out):
    json.dump({1: item(1, "alpha only")}, open(d / "items-o__main.json", "w"))
    json.dump({9: item(9, "beta")}, open(d / "items-o__side.json", "w"))
    json.dump({"o/main": [1], "o/side": [9]}, open(d / "selected.json", "w"))
    json.dump(
        {"o/main#1": {"comments": [], "pr": None}, "o/side#9": {"comments": [], "pr": None}},
        open(d / "enrich.json", "w"),
    )
    json.dump(dict(CONFIG, **out), open(d / "config.json", "w"))
    r = subprocess.run(
        [sys.executable, str(BUILD), "config.json", "--data", ".", "--date", "2026-09-15"],
        capture_output=True,
        text=True,
        cwd=d,
    )
    assert r.returncode == 0, r.stderr
    return d, r


def test_config_paths_resolve_against_the_config_not_the_cwd(tmp_path):
    """A build run from another checkout wrote the page, inventory and data.json into
    that checkout. Paths in the config belong to the config's directory."""
    cfg_dir, elsewhere = tmp_path / "map", tmp_path / "elsewhere"
    (cfg_dir / "data").mkdir(parents=True)
    elsewhere.mkdir()
    d = cfg_dir / "data"
    json.dump({1: item(1, "alpha only")}, open(d / "items-o__main.json", "w"))
    json.dump({9: item(9, "beta")}, open(d / "items-o__side.json", "w"))
    json.dump({"o/main": [1], "o/side": [9]}, open(d / "selected.json", "w"))
    json.dump(
        {"o/main#1": {"comments": [], "pr": None}, "o/side#9": {"comments": [], "pr": None}},
        open(d / "enrich.json", "w"),
    )
    cfg = dict(CONFIG, data_dir="data", out_html="out/map.html", out_md="out/map.md")
    json.dump(cfg, open(cfg_dir / "config.json", "w"))
    r = subprocess.run(
        [sys.executable, str(BUILD), str(cfg_dir / "config.json"), "--date", "2026-09-15"],
        capture_output=True,
        text=True,
        cwd=elsewhere,
    )
    assert r.returncode == 0, r.stderr
    out = cfg_dir / "out"
    assert {p.name for p in out.iterdir()} == {"map.html", "map.md", "data.json", "inventory.md"}
    assert list(elsewhere.iterdir()) == [], "nothing lands in the cwd"


def test_the_report_names_the_nodes_no_lane_rule_matched(built):
    """#4 matches no lane regex and lands in default_lane; #1 matched and #3 is an
    override, so neither is a config question and neither belongs in the list."""
    _, _, out = built
    head, _, body = out.partition("no lane rule matched")
    assert body, out
    listed = body.split("cited by the map")[0]
    assert "o/main#4" in listed
    assert "o/main#1" not in listed and "o/main#3" not in listed
    assert "1 by default_lane" in head


def test_the_report_lists_numbers_the_map_cites_but_does_not_contain(built):
    """#77 is referenced by two mapped items and is in none of them: an `include`
    candidate. #4-in-a-URL and external/project#4 are not references at all, and
    #78 is cited twice by the same node, which is one citer."""
    _, _, out = built
    _, _, body = out.partition("cited by the map but not in it")
    assert body, out
    assert "o/main#77" in body and "cited by 2 nodes" in body
    assert "external/project" not in body
    assert "o/main#78" not in body, "one node citing #78 twice is one citer, not two"


def test_the_report_names_include_entries_that_never_arrived(built):
    """#1 was included and is in the data; #999 is not, and silently dropping it
    is how a map loses a node the config asked for."""
    _, _, out = built
    _, _, body = out.partition("`include` entries not in the fetched data")
    assert body, out
    assert "o/main#999" in body.split("lane names")[0]
    assert "o/main#1" not in body.split("lane names")[0]


def test_lane_header_limits_still_match_the_template():
    """build.py cannot measure the rendered page, so it mirrors the template's wrap
    arithmetic. If the template's numbers move, this fails instead of the check
    silently going wrong."""
    js = TEMPLATE.read_text()
    min_w = int(re.search(r"minLaneW\s*=\s*(\d+)", js).group(1))
    m = re.search(r"Math\.max\((\d+),\s*Math\.floor\(\(laneW - (\d+)\) / ([\d.]+)\)\)", js)
    assert (min_w, int(m.group(1)), int(m.group(2)), float(m.group(3))) == (
        laning.MIN_LANE_W,
        laning.MIN_CHARS,
        laning.HEADER_PAD,
        laning.CHAR_PX,
    ), "template.html's lane-header wrapping changed; update scripts/laning.py"


def test_a_lane_name_word_too_long_to_wrap_is_reported():
    """Wrapping splits on spaces, so two short words are fine and one long one is not."""
    cap = laning.header_max_chars()
    assert laning.overflowing_lane_names([{"key": "a", "name": "Benchmarks and ratchets"}]) == []
    over = laning.overflowing_lane_names([{"key": "b", "name": "x" * (cap + 1)}])
    assert over == [("b", "x" * (cap + 1))]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
