#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "rich"]
# ///
"""Tests for watch.py. Run via `uv run claude-code-skills/pr-dashboard/test_watch.py`
(or `just test`).

The module filename has no hyphen but lives beside a SKILL, so load it by path
to stay consistent with the repo's other tool tests. The gh-fetch layer isn't
exercised here — the value is in the pure snapshot/diff/prune functions.
"""

import importlib.util
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("watch", Path(__file__).parent / "watch.py")
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)


def _pr(number=1, body="hi", decision="", comments=(), reviews=(), title="t", draft=False):
    return {
        "number": number,
        "title": title,
        "url": f"https://x/pull/{number}",
        "body": body,
        "isDraft": draft,
        "reviewDecision": decision,
        "comments": [{"id": cid, "body": cbody} for cid, cbody in comments],
        "reviews": [
            {"id": rid, "state": st, "body": rb, "author": {"login": who}}
            for rid, st, rb, who in reviews
        ],
    }


def test_snapshot_reduces_to_hashes_not_text():
    snap = w.snapshot_pr(_pr(body="secret body", comments=[("c1", "hello")]))
    assert snap["bodyHash"] != "secret body"
    assert snap["comments"]["c1"] != "hello"  # stored as a hash, never the text
    assert snap["comments"]["c1"] == w._sha("hello")


def test_first_sight_has_no_diff():
    assert w.diff_pr(None, w.snapshot_pr(_pr())) == []


def test_unchanged_pr_reports_nothing():
    pr = _pr(decision="APPROVED", comments=[("c1", "a")])
    snap = w.snapshot_pr(pr)
    assert w.diff_pr(snap, w.snapshot_pr(pr)) == []


def test_approval_flip():
    prev = w.snapshot_pr(_pr(decision="REVIEW_REQUIRED"))
    cur = w.snapshot_pr(_pr(decision="APPROVED"))
    assert w.diff_pr(prev, cur) == ["approval: REVIEW_REQUIRED → APPROVED"]


def test_body_edit_detected_via_hash():
    prev = w.snapshot_pr(_pr(body="v1"))
    cur = w.snapshot_pr(_pr(body="v2"))
    assert "body edited" in w.diff_pr(prev, cur)


def test_new_edited_removed_comments():
    prev = w.snapshot_pr(_pr(comments=[("c1", "a"), ("c2", "b")]))
    cur = w.snapshot_pr(
        _pr(comments=[("c1", "a-edited"), ("c3", "c")])
    )  # c2 removed, c3 new, c1 edited
    line = next(x for x in w.diff_pr(prev, cur) if x.startswith("comments:"))
    assert "+1 new" in line and "1 edited" in line and "1 removed" in line and "now 2" in line


def test_new_review_and_state_change():
    prev = w.snapshot_pr(_pr(reviews=[("r1", "COMMENTED", "x", "alice")]))
    cur = w.snapshot_pr(
        _pr(reviews=[("r1", "CHANGES_REQUESTED", "x", "alice"), ("r2", "APPROVED", "", "bob")])
    )
    lines = w.diff_pr(prev, cur)
    assert "review by @alice: COMMENTED → CHANGES_REQUESTED" in lines
    assert "review by @bob: APPROVED" in lines


def test_review_body_edit_detected():
    # Same review id and state, edited body — a "counts + hash" signal, like comments.
    prev = w.snapshot_pr(_pr(reviews=[("r1", "COMMENTED", "orig", "alice")]))
    cur = w.snapshot_pr(_pr(reviews=[("r1", "COMMENTED", "edited", "alice")]))
    assert w.diff_pr(prev, cur) == ["review by @alice: body edited"]


def test_sha_handles_unicode():
    assert w._sha("café") == w._sha("café")  # stable
    assert w._sha("café") != w._sha("cafe")  # accent is a real difference


def test_prune_drops_stale_and_caps(tmp_path):
    cache = tmp_path
    fresh = cache / "o__fresh.json"
    stale = cache / "o__stale.json"
    fresh.write_text("{}")
    stale.write_text("{}")
    old = time.time() - 20 * 86400
    import os

    os.utime(stale, (old, old))
    w.prune(cache, ttl_days=14, max_repos=200)
    assert fresh.exists() and not stale.exists()  # stale aged out by TTL

    # cap: exactly 3 files present, keep newest 2
    fresh.unlink()
    for i in range(3):
        f = cache / f"o__r{i}.json"
        f.write_text("{}")
        t = time.time() - i  # r0 newest, r2 oldest
        os.utime(f, (t, t))
    w.prune(cache, ttl_days=14, max_repos=2)
    remaining = {p.name for p in cache.glob("o__r*.json")}
    assert "o__r2.json" not in remaining and len(remaining) == 2


def test_prune_protects_current_run(tmp_path):
    # A repo watched this run must survive the cap even when it's the oldest file
    # and the run exceeds --max-repos — otherwise it re-seeds forever, never diffs.
    import os

    cache = tmp_path
    for i in range(3):
        f = cache / f"o__r{i}.json"
        f.write_text("{}")
        t = time.time() - (10 - i)  # r0 oldest, r2 newest
        os.utime(f, (t, t))
    # cap of 1, but all three are protected (watched this run) -> none deleted
    w.prune(cache, ttl_days=14, max_repos=1, protect={"o__r0", "o__r1", "o__r2"})
    assert len({p.name for p in cache.glob("o__r*.json")}) == 3
    # protected oldest survives even when an unprotected newer file exists
    (cache / "o__other.json").write_text("{}")
    w.prune(cache, ttl_days=14, max_repos=0, protect={"o__r0"})
    assert (cache / "o__r0.json").exists() and not (cache / "o__other.json").exists()


def test_prune_protects_from_ttl(tmp_path):
    import os

    cache = tmp_path
    watched = cache / "o__watched.json"
    watched.write_text("{}")
    old = time.time() - 30 * 86400
    os.utime(watched, (old, old))  # ancient mtime, but watched this run
    w.prune(cache, ttl_days=14, max_repos=200, protect={"o__watched"})
    assert watched.exists()


# --- report_repo: the first-run / re-run / closed-PR branches ---------------


def _baseline(prs):
    """Wrap raw PRs into a loaded-baseline payload keyed by number."""
    return {
        "repo": "o/r",
        "fetchedAt": "2026-07-14T09:00:00+00:00",
        "prs": {str(p["number"]): w.snapshot_pr(p) for p in prs},
    }


def test_report_first_run_seeds_no_diff(capsys):
    moved = w.report_repo("o/r", {}, [_pr(1), _pr(2)])
    out = capsys.readouterr().out
    assert moved is False
    assert "baseline recorded" in out and "2 open PRs" in out


def test_report_no_changes(capsys):
    prs = [_pr(1, decision="APPROVED"), _pr(2)]
    moved = w.report_repo("o/r", _baseline(prs), prs)
    out = capsys.readouterr().out
    assert moved is False
    assert "no changes" in out


def test_report_detects_change_new_and_closed(capsys):
    base = _baseline([_pr(1, decision="REVIEW_REQUIRED"), _pr(2, title="Old one")])
    # #1 approved, #2 gone (closed/merged), #3 brand new
    current = [_pr(1, decision="APPROVED"), _pr(3, title="Fresh PR")]
    moved = w.report_repo("o/r", base, current)
    out = capsys.readouterr().out
    assert moved is True
    assert "approval: REVIEW_REQUIRED → APPROVED" in out
    assert "#3" in out and "new — now tracking" in out
    assert "#2" in out and "closed / no longer open" in out


def test_draft_ready_transitions():
    ready = w.diff_pr(w.snapshot_pr(_pr(draft=True)), w.snapshot_pr(_pr(draft=False)))
    assert ready == ["marked ready for review"]
    back = w.diff_pr(w.snapshot_pr(_pr(draft=False)), w.snapshot_pr(_pr(draft=True)))
    assert back == ["converted to draft"]


def test_scope_change_rebaselines_instead_of_reporting_closed(tmp_path, monkeypatch, capsys):
    # Baseline seeded under @me; re-running --all must re-baseline, not report the
    # now-out-of-scope @me PR as "closed" or every --all PR as "new".
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    w.save_baseline(cache, "o/r", "@me", {"1": w.snapshot_pr(_pr(1, decision="APPROVED"))})

    monkeypatch.setattr(w, "resolve_repos", lambda repos: ["o/r"])
    monkeypatch.setattr(
        w, "fetch_prs", lambda repo, author: [_pr(1), _pr(9, title="Someone else's")]
    )

    w.main(["--all", "o/r"])
    out = capsys.readouterr().out
    assert "scope changed @me → ALL, re-baselining" in out
    assert "closed / no longer open" not in out
    assert "new — now tracking" not in out
    # and the baseline is now stored under the new scope
    assert w.load_baseline(cache, "o/r")["scope"] == "ALL"


def _spy_fetch(monkeypatch, prs):
    """Patch fetch_prs, recording the `author` each call was scoped to."""
    seen = []

    def fake(repo, author):
        seen.append(author)
        return prs

    monkeypatch.setattr(w, "fetch_prs", fake)
    return seen


def test_flagless_run_adopts_cached_all_scope(tmp_path, monkeypatch, capsys):
    # The bug: a bare `watch.py` defaulted to @me, mismatched the --all baseline,
    # and re-baselined — destroying the pending delta before printing it. A handoff
    # that says "run /pr-watch" must not be a destructive command.
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    w.save_baseline(cache, "o/r", "ALL", {"1": w.snapshot_pr(_pr(1, decision="REVIEW_REQUIRED"))})

    monkeypatch.setattr(w, "resolve_repos", lambda repos: ["o/r"])
    seen = _spy_fetch(monkeypatch, [_pr(1, decision="APPROVED")])

    w.main(["o/r"])
    out = capsys.readouterr().out

    assert seen == [None], "adopted scope must fetch unscoped (--all), not --author @me"
    assert "re-baselining" not in out
    # the delta the old code threw away actually gets reported
    assert "approval: REVIEW_REQUIRED → APPROVED" in out
    assert w.load_baseline(cache, "o/r")["scope"] == "ALL"


def test_flagless_run_falls_back_to_me_with_no_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(w, "resolve_repos", lambda repos: ["o/r"])
    seen = _spy_fetch(monkeypatch, [_pr(1)])

    w.main(["o/r"])

    assert seen == ["@me"]
    assert w.load_baseline(w.cache_dir(), "o/r")["scope"] == "@me"


def test_explicit_author_still_rebaselines_off_an_all_baseline(tmp_path, monkeypatch, capsys):
    # Narrowing scope on purpose must keep today's behavior — diffing across scopes
    # would misreport, so the re-baseline is correct when you asked for it.
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    w.save_baseline(cache, "o/r", "ALL", {"1": w.snapshot_pr(_pr(1))})

    monkeypatch.setattr(w, "resolve_repos", lambda repos: ["o/r"])
    seen = _spy_fetch(monkeypatch, [_pr(1)])

    w.main(["--author", "@me", "o/r"])
    out = capsys.readouterr().out

    assert seen == ["@me"]
    assert "scope changed ALL → @me, re-baselining" in out
    assert w.load_baseline(cache, "o/r")["scope"] == "@me"


def test_reset_reseeds_under_the_cached_scope(tmp_path, monkeypatch):
    # --reset means "forget what I've seen", not "and also switch me to @me".
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    w.save_baseline(cache, "o/r", "ALL", {"1": w.snapshot_pr(_pr(1))})

    monkeypatch.setattr(w, "resolve_repos", lambda repos: ["o/r"])
    seen = _spy_fetch(monkeypatch, [_pr(1), _pr(9)])

    w.main(["--reset", "o/r"])

    assert seen == [None]
    assert w.load_baseline(cache, "o/r")["scope"] == "ALL"


def test_scope_resolved_per_repo(tmp_path, monkeypatch):
    # Baselines are per repo, so a bare run across two repos watched at different
    # scopes must honor each one — not resolve a single scope for the whole run.
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    w.save_baseline(cache, "o/all", "ALL", {"1": w.snapshot_pr(_pr(1))})
    w.save_baseline(cache, "o/mine", "@me", {"1": w.snapshot_pr(_pr(1))})

    monkeypatch.setattr(w, "resolve_repos", lambda repos: ["o/all", "o/mine"])
    seen = _spy_fetch(monkeypatch, [_pr(1)])

    w.main(["o/all", "o/mine"])

    assert seen == [None, "@me"]
    assert w.load_baseline(cache, "o/all")["scope"] == "ALL"
    assert w.load_baseline(cache, "o/mine")["scope"] == "@me"


def test_roundtrip_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    snaps = {"1": w.snapshot_pr(_pr(1, decision="APPROVED"))}
    w.save_baseline(cache, "owner/repo", "@me", snaps)
    loaded = w.load_baseline(cache, "owner/repo")
    assert loaded["prs"]["1"]["reviewDecision"] == "APPROVED"
    assert loaded["repo"] == "owner/repo"


def test_corrupt_baseline_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    (cache / "owner__repo.json").write_text("{ not json")
    assert w.load_baseline(cache, "owner/repo") == {}


def test_baseline_is_replaced_atomically_not_truncated(tmp_path, monkeypatch):
    # The failure this prevents is silent: load_baseline swallows a
    # JSONDecodeError as "first run", so a half-written baseline re-seeds and
    # takes the pending delta with it instead of raising.
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    w.save_baseline(cache, "owner/repo", "@me", {"1": w.snapshot_pr(_pr(1))})
    before = (cache / "owner__repo.json").read_text()

    seen = {}
    real_replace = w.os.replace

    def spy(src, dst):
        # The destination must still hold the old baseline whole at this point:
        # nothing has been written through it, only alongside it.
        seen["dst_at_replace"] = Path(dst).read_text()
        real_replace(src, dst)

    monkeypatch.setattr(w.os, "replace", spy)
    w.save_baseline(cache, "owner/repo", "@me", {"2": w.snapshot_pr(_pr(2))})

    assert seen["dst_at_replace"] == before
    assert list(w.load_baseline(cache, "owner/repo")["prs"]) == ["2"]


def test_a_failed_baseline_write_leaves_the_previous_one_intact(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_WATCH_CACHE_DIR", str(tmp_path))
    cache = w.cache_dir()
    w.save_baseline(cache, "owner/repo", "@me", {"1": w.snapshot_pr(_pr(1))})

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(w.os, "replace", boom)
    with pytest.raises(OSError):
        w.save_baseline(cache, "owner/repo", "@me", {"2": w.snapshot_pr(_pr(2))})

    assert list(w.load_baseline(cache, "owner/repo")["prs"]) == ["1"]
    assert list(cache.glob("*.tmp")) == []  # no scratch file left behind


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))


# --- inline review comments (the third collection) ---------------------------


def _threads(*threads, threads_truncated=False):
    """GraphQL-shaped payload for one PR: each thread is a list of (id, body)."""
    return {
        "reviewThreads": {
            "pageInfo": {"hasNextPage": threads_truncated},
            "nodes": [
                {
                    "comments": {
                        "pageInfo": {"hasNextPage": trunc},
                        "nodes": [{"id": cid, "body": body} for cid, body in cs],
                    }
                }
                for cs, trunc in threads
            ],
        }
    }


def _payload(**prs):
    return {"data": {"repository": prs}}


def test_parse_flattens_threads_to_id_hash_map():
    got = w.parse_review_comments(
        _payload(pr_7=_threads(([("c1", "a")], False), ([("c2", "b"), ("c3", "c")], False)))
    )
    assert set(got["7"]) == {"c1", "c2", "c3"}
    # Hashes, never the text — same rule as the conversation thread.
    assert "a" not in got["7"].values()
    assert got["7"]["c1"] == w._sha("a")


def test_parse_marks_truncated_pr_as_unmeasured():
    """A partial dict would diff as deletions on the busiest PRs, so a capped
    page must come back as None instead."""
    assert w.parse_review_comments(_payload(pr_1=_threads(([("c1", "a")], True)))) == {"1": None}
    assert w.parse_review_comments(
        _payload(pr_2=_threads(([("c1", "a")], False), threads_truncated=True))
    ) == {"2": None}


def test_snapshot_omits_key_when_unmeasured():
    """Storing {} for an unmeasured PR is indistinguishable from 'none', and the
    next run would report every existing inline comment as new."""
    assert "reviewComments" not in w.snapshot_pr(_pr(number=1), {"1": None})
    assert "reviewComments" not in w.snapshot_pr(_pr(number=1), {})
    assert "reviewComments" not in w.snapshot_pr(_pr(number=1))
    assert w.snapshot_pr(_pr(number=1), {"1": {"c1": "h"}})["reviewComments"] == {"c1": "h"}


def test_inline_comment_added_edited_removed():
    prev = w.snapshot_pr(_pr(number=1), {"1": {"c1": w._sha("a"), "c2": w._sha("b")}})
    cur = w.snapshot_pr(_pr(number=1), {"1": {"c1": w._sha("EDITED"), "c3": w._sha("c")}})
    line = next(x for x in w.diff_pr(prev, cur) if x.startswith("review comments:"))
    assert "+1 new" in line and "1 edited" in line and "1 removed" in line
    assert "(now 2)" in line


def test_inline_diff_skipped_against_a_pre_upgrade_baseline():
    """A baseline written before this field existed has no key. Diffing it would
    report every inline comment on every PR as new, all at once."""
    prev = w.snapshot_pr(_pr(number=1))  # no reviewComments key
    cur = w.snapshot_pr(_pr(number=1), {"1": {"c1": w._sha("a")}})
    assert not any(x.startswith("review comments:") for x in w.diff_pr(prev, cur))


def test_inline_diff_skipped_when_this_run_failed_to_measure():
    """The mirror case: a failed GraphQL call must not read as 'all removed'."""
    prev = w.snapshot_pr(_pr(number=1), {"1": {"c1": w._sha("a")}})
    cur = w.snapshot_pr(_pr(number=1), {})  # fetch failed → unmeasured
    assert not any(x.startswith("review comments:") for x in w.diff_pr(prev, cur))


def test_query_is_aliased_per_pr_and_asks_for_page_info():
    q = w.build_review_comments_query("owner", "repo", [1, 22])
    assert 'repository(owner: "owner", name: "repo")' in q
    assert "pr_1: pullRequest(number: 1)" in q
    assert "pr_22: pullRequest(number: 22)" in q
    assert q.count("hasNextPage") == 4  # threads + comments, per PR


def test_fetch_is_a_noop_without_prs():
    assert w.fetch_review_comments("o/r", []) == {}


# --- query size, the failure the suite could not previously see ---------------

NODE_LIMIT = 500_000


def _possible_nodes(pr_count: int) -> int:
    """GitHub's accounting: per alias, the thread page plus a comment page each."""
    per_pr = w.REVIEW_THREAD_PAGE + w.REVIEW_THREAD_PAGE * w.REVIEW_COMMENT_PAGE
    return pr_count * per_pr


def test_a_single_query_stays_under_the_node_limit():
    """At --limit 100 the unchunked query asked for 1,010,000 nodes against a
    500,000 cap; GitHub rejected it, gh exited 1, and nothing was measured while
    the run still printed a normal report. Measured live: 49 aliases fine, 50
    rejected."""
    assert _possible_nodes(w.REVIEW_PRS_PER_QUERY) < NODE_LIMIT


def test_chunking_covers_every_pr_at_the_fetch_limit():
    """--limit 100 is what fetch_prs can return, so the chunks must tile it."""
    numbers = list(range(1, 101))
    chunks = [
        numbers[i : i + w.REVIEW_PRS_PER_QUERY]
        for i in range(0, len(numbers), w.REVIEW_PRS_PER_QUERY)
    ]
    assert [n for c in chunks for n in c] == numbers
    assert all(_possible_nodes(len(c)) < NODE_LIMIT for c in chunks)


# --- an unmeasured run must defer the delta, not destroy it ------------------


def test_unmeasured_run_carries_the_previous_measurement_forward():
    """Overwriting the baseline with a hole means comments left during a single
    failed run never surface — not that run, and not any later one."""
    measured = w.snapshot_pr(_pr(number=1), {"1": {"c1": w._sha("a")}})

    # The failed run: no measurement available for this PR.
    carried = w.snapshot_pr(_pr(number=1), {}, measured)
    assert carried["reviewComments"] == {"c1": w._sha("a")}


def test_a_recovered_run_still_reports_what_arrived_during_the_outage():
    run1 = w.snapshot_pr(_pr(number=1), {"1": {"c1": w._sha("a")}})
    run2 = w.snapshot_pr(_pr(number=1), {}, run1)  # measurement failed
    run3 = w.snapshot_pr(_pr(number=1), {"1": {"c1": w._sha("a"), "c2": w._sha("b")}}, run2)

    line = next(x for x in w.diff_pr(run2, run3) if x.startswith("review comments:"))
    assert "+1 new" in line


def test_nothing_to_carry_leaves_the_key_absent():
    """A PR never measured stays unmeasured rather than becoming empty."""
    assert "reviewComments" not in w.snapshot_pr(_pr(number=1), {}, None)
    assert "reviewComments" not in w.snapshot_pr(_pr(number=1), {}, {"number": 1})


# --- partial GraphQL results are usable ---------------------------------------


def test_parse_keeps_the_resolvable_aliases_when_one_is_null():
    """gh exits 1 whenever an `errors` array is present, including when it is one
    unresolvable alias beside complete data for every other PR in the chunk."""
    payload = {"data": {"repository": {"pr_1": None, "pr_2": _threads(([("c1", "a")], False))}}}

    got = w.parse_review_comments(payload)
    assert "1" not in got  # nulled alias contributes nothing
    assert got["2"] == {"c1": w._sha("a")}


def test_a_rejected_query_yields_nothing_rather_than_empty_measurements():
    """No `data` at all — the node-limit and auth shapes — must not read as
    'no inline comments' for every PR in the chunk."""
    assert w.parse_review_comments({"errors": [{"type": "MAX_NODE_LIMIT_EXCEEDED"}]}) == {}
