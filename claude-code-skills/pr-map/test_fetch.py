#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Unit tests for the offline parts of scripts/fetch.py. Run via
`uv run --with pytest claude-code-skills/pr-map/test_fetch.py` (or `just test`).

No network: the `gh` calls are stubbed. They pin the page splitter, which used
to cut on `][` and lose every comment on a page holding reference-style
Markdown, and the item lookup, which used `gh issue view` and therefore never
noticed when an included number was a pull request.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "fetch", Path(__file__).parent / "scripts" / "fetch.py"
)
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def test_split_pages_survives_brackets_inside_comment_text():
    page1 = json.dumps([{"a": "x", "t": "2026-01-01", "b": "see [docs][ref] and [a][b]"}])
    page2 = json.dumps([{"a": "y", "t": "2026-01-02", "b": "][ literally"}])
    got = fetch.split_pages(page1 + page2)
    assert [c["a"] for c in got] == ["x", "y"]
    assert fetch.split_pages("") == []
    assert fetch.split_pages("  \n" + page2 + "\n") == json.loads(page2)


def test_view_marks_a_pull_request_as_one(monkeypatch):
    calls = []

    def fake_gh(*args, required=True):
        calls.append(args)
        if args[0] == "api":
            return json.dumps(
                {
                    "number": 12,
                    "title": "t",
                    "state": "closed",
                    "pull_request": {"url": "..."},
                    "labels": [{"name": "perf"}],
                    "created_at": "2026-01-01T00:00:00Z",
                    "closed_at": "2026-01-02T00:00:00Z",
                    "updated_at": "",
                    "user": {"login": "alice"},
                    "html_url": "https://example.invalid/12",
                    "body": None,
                }
            )
        return json.dumps({"mergedAt": "2026-01-02T00:00:00Z", "state": "MERGED"})

    monkeypatch.setattr(fetch, "gh", fake_gh)
    monkeypatch.setattr(fetch, "gh_or_missing", lambda *a: (fake_gh(*a), False))
    it = fetch.view("o/r", 12)
    assert it["isPullRequest"] is True and it["state"] == "MERGED"
    assert it["body"] == "" and it["labels"] == [{"name": "perf"}]
    assert calls[0][:2] == ("api", "repos/o/r/issues/12")


def test_view_returns_none_for_a_number_that_does_not_exist(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "gh: Not Found (HTTP 404)"

    monkeypatch.setattr(fetch.subprocess, "run", lambda *a, **k: R())
    assert fetch.view("o/r", 999) is None


# gh 2.97 renders an api error three ways, and two of them carry the request
# URL, so the item number lands inside the message. Anything matching a bare
# "404" reads a 502 on issue #404 as "this item does not exist".
GH_ERRORS = [
    ("no JSON message (5xx edge page)", "HTTP 502 (https://api.github.com/repos/o/r/issues/404)"),
    (
        "multi-line message (secondary rate limit)",
        "HTTP 403: You have exceeded a secondary rate limit\n"
        "(https://api.github.com/repos/o/r/issues/1404)",
    ),
    ("plain transport failure", "dial tcp: lookup api.github.com: no such host"),
]


@pytest.mark.parametrize("name,stderr", GH_ERRORS, ids=[n for n, _ in GH_ERRORS])
def test_an_include_that_fails_for_any_other_reason_aborts(monkeypatch, name, stderr):
    """A 404 is a config error the run reports and carries on past; anything else
    is a transient failure, and swallowing it drops a node from a map that still
    looks complete -- which is the whole reason this path is not `required=False`."""

    class R:
        returncode = 1
        stdout = ""

    R.stderr = stderr
    monkeypatch.setattr(fetch.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(fetch.FetchError):
        fetch.view("o/r", 404)


def test_a_real_404_is_read_from_the_response_body():
    """`gh api` writes the body to stdout even on an error status, so the status
    field settles it without parsing gh's prose."""
    body = '{"message":"Not Found","documentation_url":"https://docs.github.com/x","status":"404"}'
    assert fetch.is_404(body, "gh: Not Found (HTTP 404)")
    assert fetch.is_404("", "gh: Not Found (HTTP 404)"), "stderr suffix is the fallback"
    assert not fetch.is_404("", "HTTP 502 (https://api.github.com/repos/o/r/issues/404)")
    assert not fetch.is_404("not json", "HTTP 500")


def test_preview_survives_a_config_with_no_lanes_yet(monkeypatch, capsys, tmp_path):
    """Step 2 previews before the lanes exist, so `lanes` has to default like the
    other judgment tables rather than KeyError."""
    cfg = fetch.load_config(
        _write_cfg(tmp_path, {"user": "alice", "repos": ["o/main"], "relevance": "perf"})
    )
    monkeypatch.setattr(fetch, "search", lambda repo, user: PREVIEW_ITEMS)
    fetch.preview(cfg)
    assert "lanes: none configured yet" in capsys.readouterr().out


def _write_cfg(tmp_path, obj):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(obj))
    return str(p)


def test_a_failed_required_call_raises_instead_of_returning_empty(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "HTTP 502"

    monkeypatch.setattr(fetch.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(fetch.FetchError):
        fetch.gh("search", "prs")
    assert fetch.gh("api", "x", required=False) == ""


def test_body_cap_makes_a_title_hit_required():
    import re

    rx = re.compile(r"\bshell\w*", re.I)
    body_only = {"title": "fix burning in water", "labels": [], "body": "shell shell shell shell"}
    titled = {"title": "shell: fit on rotate", "labels": [], "body": ""}
    assert fetch.score(body_only, rx) >= 3, "the default cap lets body mentions select"
    assert fetch.score(body_only, rx, body_cap=2) < 3
    assert fetch.score(titled, rx, body_cap=2) >= 3


def test_repo_slug_keeps_the_owner():
    assert fetch.repo_slug("org-a/service") != fetch.repo_slug("org-b/service")


def sitem(n, title, body=""):
    return {
        "number": n,
        "title": title,
        "state": "OPEN",
        "isPullRequest": False,
        "labels": [],
        "createdAt": "2026-06-01T00:00:00Z",
        "closedAt": None,
        "updatedAt": "",
        "author": {"login": "alice"},
        "url": f"https://example.invalid/{n}",
        "body": body,
    }


PREVIEW_CFG = {
    "user": "alice",
    "repos": ["o/main"],
    "relevance": r"\bperf\b",
    "min_score": 3,
    "include": {"o/main": [4]},
    "exclude": {"o/main": [2, 4]},
    "lanes": [{"key": "bench", "name": "Benchmarks", "match": "bench"}],
    "default_lane": {"o/main": "bench"},
}

PREVIEW_ITEMS = {
    1: sitem(1, "perf: bench the loop"),
    2: sitem(2, "perf regression in the parser"),
    3: sitem(3, "unrelated cleanup", body="perf perf"),
    4: sitem(4, "release prep"),
    5: sitem(5, "perf: tighten the allocator"),
}


@pytest.fixture
def previewed(monkeypatch, capsys):
    monkeypatch.setattr(fetch, "search", lambda repo, user: PREVIEW_ITEMS)
    fetch.preview(PREVIEW_CFG)
    return capsys.readouterr().out


def test_preview_selects_what_the_real_fetch_would(previewed):
    """#1 and #5 score on the title; #3's two body hits fall under min_score; #2
    scores but is excluded; #4 is in both tables and `include` wins, because
    main() subtracts exclude and then adds include."""
    assert "1 selected" not in previewed
    assert "5 involved, 3 selected" in previewed
    for want in ("#1", "#4", "#5"):
        assert want in previewed
    assert "1 more scored high enough and are in `exclude`" in previewed


def test_preview_marks_what_no_lane_rule_claimed(previewed):
    """#1 matched `bench`; #5 and #4 fell through to default_lane and carry `d`,
    which is what a lane rule or an override would fix."""
    body = previewed.split("[bench]")[1]
    assert "2 by default_lane" in previewed
    assert " m  perf: bench the loop" in body
    assert " d  perf: tighten the allocator" in body


def test_preview_shows_near_misses_for_the_regex(previewed):
    """#3 scored 2 on body hits alone: the item to look at before widening the regex."""
    assert "near misses (scored 1-2): 1" in previewed
    assert "unrelated cleanup" in previewed


def test_preview_writes_nothing_and_costs_two_calls_per_repo(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_gh(*args, required=True):
        calls.append(args[0])
        return "[]"

    monkeypatch.setattr(fetch, "gh", fake_gh)
    monkeypatch.chdir(tmp_path)
    fetch.preview(dict(PREVIEW_CFG, data_dir=str(tmp_path)))
    assert calls == ["search", "search"], "issues and prs, and no per-node enrichment"
    assert list(tmp_path.iterdir()) == [], "preview must not leave data files behind"
    assert "wrote nothing" in capsys.readouterr().out


def test_fetch_writes_data_dir_beside_the_config_not_the_cwd(monkeypatch, tmp_path):
    cfg_dir, elsewhere = tmp_path / "map", tmp_path / "elsewhere"
    cfg_dir.mkdir()
    elsewhere.mkdir()
    json.dump(dict(PREVIEW_CFG, data_dir="data"), open(cfg_dir / "config.json", "w"))
    monkeypatch.setattr(fetch, "search", lambda repo, user: dict(PREVIEW_ITEMS))
    monkeypatch.setattr(fetch, "enrich_one", lambda repo, n, is_pr: {"comments": [], "pr": None})
    monkeypatch.setattr(sys, "argv", ["fetch.py", str(cfg_dir / "config.json")])
    monkeypatch.chdir(elsewhere)
    fetch.main()
    assert (cfg_dir / "data" / "selected.json").exists()
    assert list(elsewhere.iterdir()) == [], "nothing lands in the cwd"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
