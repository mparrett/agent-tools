#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Tests for render.py: the since-last-scan diff, the snapshot it keeps, and the island."""

import datetime
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import render  # noqa: E402


def facts(*prs, merged=(), repo="owner/repo", scope="@me"):
    return {
        "scope": scope,
        "repos": [{"repo": repo, "prs": list(prs), "mergedPrs": list(merged)}],
    }


def pr(n, **kw):
    base = {
        "number": n,
        "title": f"pr {n}",
        "url": f"https://x/pull/{n}",
        "ci": "pass",
        "mergeable": "MERGEABLE",
        "approvals": {"atHead": [], "stale": []},
        "changesRequested": [],
        "reviewers": {"requested": [], "reviewed": []},
        "threads": {"unresolved": 0, "outdated": 0, "replied": 0},
        "comments": 0,
        "mergeReady": {"verdict": "blocked"},
        "lastTouch": None,
    }
    base.update(kw)
    return base


def texts(rows):
    return [(r["ref"].split("#")[1], r["text"]) for r in rows]


def test_diff_reports_each_kind_of_movement():
    before = facts(
        pr(1),
        pr(2, approvals={"atHead": ["a"], "stale": []}, mergeReady={"verdict": "ready"}),
        pr(3),
        pr(4, threads={"unresolved": 2, "outdated": 0, "replied": 0}, comments=4),
        pr(5),
    )
    after = facts(
        pr(
            1,
            approvals={"atHead": ["a"], "stale": []},
            reviewers={"requested": [], "reviewed": ["a"]},
            mergeReady={"verdict": "ready"},
        ),
        pr(2, approvals={"atHead": [], "stale": ["a"]}),
        pr(4, threads={"unresolved": 2, "outdated": 0, "replied": 2}, comments=6, ci="fail"),
        pr(5, mergeable="CONFLICTING"),
        pr(6),
        merged=[{"number": 3, "url": "https://x/pull/3"}],
    )
    assert texts(render.diff(before, after)) == [
        ("6", "opened: pr 6"),
        ("3", "merged"),
        ("1", "merge: blocked → ready"),
        ("1", "approved by a"),  # not also "reviewed by a": the approval says it
        ("2", "merge: ready → blocked"),
        ("2", "a's approval went stale (new push)"),
        ("4", "threads: 2 open, 0 replied → 2 open, 2 replied"),
        ("4", "+2 comments"),
        ("4", "CI: pass → fail"),
        ("5", "now conflicts with its base"),
    ]


def test_unmeasured_on_either_side_is_not_news():
    # null means "could not measure" -- a flip to or from it is a GraphQL
    # hiccup, not something that happened on the PR.
    before = facts(pr(1, approvals=None, threads=None, comments=None))
    after = facts(pr(1, approvals={"atHead": ["a"], "stale": []}, comments=9))
    assert render.diff(before, after) == []


def test_scope_slug_separates_authors_and_repo_sets():
    assert render.scope_slug(facts(scope="@me")) == "me__owner-repo"
    assert render.scope_slug(facts(scope="nooga")) == "nooga__owner-repo"
    two = {"scope": "ALL", "repos": [{"repo": "a/b"}, {"repo": "c/d"}]}
    assert render.scope_slug(two) == "ALL__a-b+c-d"
    assert render.scope_slug({**two, "waitingOn": "mparrett"}) == "waiting-on-mparrett__a-b+c-d"


def _island(path):
    html = Path(path).read_text()
    body = re.search(r'<script id="dash-data"[^>]*>([\s\S]*?)</script>', html).group(1)
    return json.loads(body)


def test_first_render_saves_a_baseline_and_says_so(tmp_path):
    out, changes = render.render({"title": "t"}, facts(pr(1)), state=tmp_path)
    assert out == tmp_path / "boards" / "me__owner-repo.html"
    assert changes == {"first": True}
    assert _island(out)["changes"] == {"first": True}
    assert "generatedAt" in _island(out)


def test_second_scan_diffs_and_a_rerender_keeps_the_diff(tmp_path):
    t0 = datetime.datetime(2026, 10, 19, 9, 0, tzinfo=datetime.timezone.utc)
    render.render({}, facts(pr(1)), state=tmp_path, now=t0)
    after = facts(pr(1, ci="fail"))
    _, changes = render.render({}, after, state=tmp_path, now=t0 + datetime.timedelta(hours=3))
    assert changes["since"] == t0.isoformat()
    assert texts(changes["items"]) == [("1", "CI: pass → fail")]
    # Re-rendering the same facts (rewording the queue, say) is not a new scan.
    out, again = render.render({"title": "v2"}, after, state=tmp_path)
    assert again == changes
    assert _island(out)["changes"] == changes


def test_the_island_cannot_be_closed_by_pr_text(tmp_path):
    model = {"queue": [{"why": "body says </script><script>alert(1)</script>"}]}
    out, _ = render.render(model, facts(), state=tmp_path)
    assert _island(out)["queue"][0]["why"] == model["queue"][0]["why"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
