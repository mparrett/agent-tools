#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest"]
# ///
"""Tests for collect.sh. Run via `uv run claude-code-skills/pr-dashboard/test_collect.py`
(or `just test`).

collect.sh is all gh calls, so the only way to test it is to put a fake `gh` first
on PATH. The stub answers by pattern (`pr list`, `issue list`, `api graphql`, …) from
canned JSON, which is enough to exercise the parts that are actually ours: the jq
normalizers, the issue<->PR linking, and the argument plumbing between them.

Needs `jq` on PATH, like collect.sh itself — it is not stubbed.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

COLLECT = Path(__file__).parent / "collect.sh"


def _bash_version(path):
    """(major, minor) of a bash binary, or None if it won't report one."""
    try:
        out = subprocess.run(
            [path, "-c", 'printf "%s %s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        # Something on PATH named `bash` that isn't one. This runs at import, so
        # letting it raise would be a collection error rather than a skip.
        return None


def _oldest_bash():
    """The oldest bash on this machine, as (path, version).

    Pin the interpreter rather than trusting collect.sh's `#!/usr/bin/env bash`:
    "${arr[@]}" on an empty array is an unset-variable error under `set -u` only
    before bash 4.4. macOS still ships 3.2, which is where the bug this file was
    written for actually bites, so the strictest available bash is the one that
    tests it. Picking `/bin/bash` alone would be wrong off macOS, where it's 5.x.
    """
    seen = {}
    for path in (
        "/bin/bash",
        shutil.which("bash"),
        "/usr/local/bin/bash",
        "/opt/homebrew/bin/bash",
    ):
        if path and path not in seen and Path(path).exists():
            version = _bash_version(path)
            if version:
                seen[path] = version
    if not seen:
        return None, None
    return min(seen.items(), key=lambda kv: kv[1])


BASH, BASH_VERSION = _oldest_bash()

# Below 4.4 the empty-array expansion is fatal, so the regression test can prove
# the guard is load-bearing. At or above it, that test skips *loudly* rather than
# passing for free — a green run on a modern bash is not evidence of the guard.
GUARD_IS_TESTABLE = bool(BASH_VERSION) and BASH_VERSION < (4, 4)

# pytest evaluates a function-level skipif before the module's, so this reason has
# to cover the no-bash case too or the skip line blames the wrong thing.
GUARD_SKIP_REASON = (
    "no bash on PATH to run collect.sh under"
    if BASH is None
    else f"oldest bash here is {BASH_VERSION}; the empty-array expansion is only fatal "
    "before 4.4, so this box cannot gate the regression"
)

pytestmark = pytest.mark.skipif(BASH is None, reason="no bash on PATH to run collect.sh under")

# A gh stub. Dispatches on the joined argv, echoing whichever canned file matches;
# an unmatched call exits nonzero on purpose, so a new gh call in collect.sh shows
# up as a test failure rather than being silently answered with an empty list.
GH_STUB = r"""#!/usr/bin/env bash
argv="$*"
d="$GH_STUB_DIR"
case "$argv" in
  *"repo view"*"defaultBranchRef"*) printf 'main' ;;
  *"repo view"*"nameWithOwner"*)    printf 'owner/repo' ;;   # the no-repo-argument default
  *"api graphql"*)    cat "$d/graphql.json"; exit "$(cat "$d/graphql_exit" 2>/dev/null || echo 0)" ;;
  *"api repos/"*)     printf 'abc1234' ;;
  *"pr list"*"--state merged"*) cat "$d/merged.json" ;;
  *"pr list"*)        cat "$d/prs.json" ;;
  *"issue list"*"--state closed"*) cat "$d/closed_issues.json" ;;
  *"issue list"*)     cat "$d/issues.json" ;;
  *) echo "gh stub: unhandled call: $argv" >&2; exit 1 ;;
esac
"""


def run_collect(
    tmp_path,
    *args,
    prs=(),
    issues=(),
    merged=(),
    closed=(),
    graphql=None,
    graphql_exit=0,
    repo="owner/repo",
    script=None,
    expect_ok=True,
):
    """Run collect.sh against a stubbed gh; return its parsed JSON stdout.

    `repo=None` omits the repo argument, exercising the default-repo detection
    path. `script` runs a modified copy, and `expect_ok=False` returns the failed
    CompletedProcess instead of parsed JSON — both for the guard regression test.
    """
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "prs.json").write_text(json.dumps(list(prs)))
    (stub_dir / "issues.json").write_text(json.dumps(list(issues)))
    (stub_dir / "merged.json").write_text(json.dumps(list(merged)))
    (stub_dir / "closed_issues.json").write_text(json.dumps(list(closed)))
    (stub_dir / "graphql.json").write_text(json.dumps(graphql or {"data": {"repository": {}}}))
    (stub_dir / "graphql_exit").write_text(str(graphql_exit))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_STUB_DIR": str(stub_dir),
    }
    argv = [BASH, str(script or COLLECT), "--all", *([repo] if repo else []), *args]
    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    if not expect_ok:
        return proc
    assert proc.returncode == 0, f"collect.sh failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def pr(
    number=1,
    head="feat/x",
    base="main",
    body="",
    closes=(),
    draft=False,
    review="REVIEW_REQUIRED",
    mergeable="MERGEABLE",
    author="pr-author",
    cross=False,
):
    return {
        "number": number,
        "title": f"pr {number}",
        "headRefName": head,
        "baseRefName": base,
        "isCrossRepository": cross,
        "isDraft": draft,
        "mergeable": mergeable,
        "reviewDecision": review,
        "labels": [],
        "url": f"https://x/pull/{number}",
        "updatedAt": "2026-08-01T00:00:00Z",
        "additions": 10,
        "deletions": 2,
        "author": {"login": author},
        "body": body,
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "closingIssuesReferences": [{"number": n} for n in closes],
    }


def issue(number=5, title="an issue"):
    return {
        "number": number,
        "title": title,
        "labels": [],
        "url": f"https://x/issues/{number}",
        "updatedAt": "2026-08-01T00:00:00Z",
    }


# The regression this file was written for: with no open PRs, the lastTouch
# enrichment expanded an empty array under `set -u`, which bash 3.2 (macOS)
# treats as unset — collect.sh aborted instead of emitting an empty board.
def test_zero_open_prs_still_emits_a_board(tmp_path):
    out = run_collect(tmp_path, prs=[], issues=[issue(5)])
    repo = out["repos"][0]
    assert repo["prs"] == []
    assert repo["prCount"] == 0
    assert [i["number"] for i in repo["openIssues"]] == [5]


def test_zero_open_prs_leaves_issues_unclaimed(tmp_path):
    out = run_collect(tmp_path, prs=[], issues=[issue(5)])
    assert out["repos"][0]["openIssues"][0]["linkedPrs"] == []


@pytest.mark.skipif(not GUARD_IS_TESTABLE, reason=GUARD_SKIP_REASON)
def test_the_empty_array_guard_is_what_keeps_zero_prs_working(tmp_path):
    """Strip the guard and the zero-PR run must die, or the tests above prove nothing.

    Without this, the whole file passes on a modern bash whether or not the fix is
    present — the tests above assert good output, not that anything protects it.
    """
    guard = '${pr_nums[@]+"${pr_nums[@]}"}'
    original = COLLECT.read_text()
    # Assert on the pre-replacement text: checking the result can't fail, so a
    # reworded guard would silently leave this testing an unmodified script.
    assert guard in original, "guard not found — did it move or get reworded?"

    patched = tmp_path / "collect-unguarded.sh"
    patched.write_text(original.replace(guard, '"${pr_nums[@]}"'))

    proc = run_collect(tmp_path, prs=[], issues=[], script=patched, expect_ok=False)
    assert proc.returncode != 0
    assert "pr_nums[@]: unbound variable" in proc.stderr


def test_repo_defaults_to_the_current_directory(tmp_path):
    # SKILL.md's primary usage passes no repo at all, so the detection call has to
    # work; the stub answers it from a different `gh repo view` arm than the rest.
    out = run_collect(tmp_path, repo=None, prs=[], issues=[issue(5)])
    assert out["repos"][0]["repo"] == "owner/repo"


def test_last_touch_is_attached_per_pr_and_null_without_events(tmp_path):
    # Both halves in one case on purpose: asserting only the null half would pass
    # for any breakage at all, since fetch_pr_graph degrades to {} on error.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {
                    "timelineItems": {
                        "nodes": [
                            {
                                "__typename": "IssueComment",
                                "author": {"login": "reviewer", "__typename": "User"},
                                "createdAt": "2026-08-01T12:00:00Z",
                            }
                        ]
                    }
                },
                "pr_2": {"timelineItems": {"nodes": []}},
            }
        }
    }
    out = run_collect(tmp_path, prs=[pr(1), pr(2, head="feat/y")], issues=[], graphql=graphql)
    touches = {p["number"]: p["lastTouch"] for p in out["repos"][0]["prs"]}
    assert touches == {
        1: {"kind": "comment", "by": "reviewer", "at": "2026-08-01T12:00:00Z"},
        2: None,
    }


def _touch(kind, by, at="2026-08-01T12:00:00Z"):
    """One timelineItems node of the given kind, shaped as the GraphQL query returns it."""
    if kind == "commit":
        return {
            "__typename": "PullRequestCommit",
            "commit": {"committedDate": at, "author": {"user": {"login": by}}},
        }
    if kind.startswith("review:"):
        return {
            "__typename": "PullRequestReview",
            "author": {"login": by, "__typename": "User"},
            "createdAt": at,
            "state": kind.split(":", 1)[1].upper(),
        }
    return {
        "__typename": "IssueComment",
        "author": {"login": by, "__typename": "User"},
        "createdAt": at,
    }


def _owed(out):
    return {p["number"]: (p["owedBy"], p["owedWhy"]) for p in out["repos"][0]["prs"]}


def test_owed_by_distinguishes_a_stale_changes_requested_badge(tmp_path):
    # The three CHANGES_REQUESTED shapes that reviewDecision alone cannot tell apart.
    # #3 is the one that matters: a *second* reviewer approving leaves the badge set
    # forever, and reading the badge alone reports the author as blocking work they
    # finished days ago.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {
                    "timelineItems": {"nodes": [_touch("review:changes_requested", "reviewer")]}
                },
                "pr_2": {"timelineItems": {"nodes": [_touch("commit", "pr-author")]}},
                "pr_3": {"timelineItems": {"nodes": [_touch("review:approved", "other-reviewer")]}},
            }
        }
    }
    out = run_collect(
        tmp_path,
        prs=[
            pr(1, review="CHANGES_REQUESTED"),
            pr(2, head="feat/y", review="CHANGES_REQUESTED"),
            pr(3, head="feat/z", review="CHANGES_REQUESTED"),
        ],
        issues=[],
        graphql=graphql,
    )
    assert _owed(out) == {
        1: ("author", "changes-requested"),
        2: ("reviewer", "author-responded"),
        3: ("reviewer", "peer-approved"),
    }


def test_owed_by_ignores_a_touch_that_is_not_the_authors(tmp_path):
    # "author-responded" means the author moved, not that *something* happened since
    # the request. A reviewer's own follow-up — a comment, a drive-by review:commented,
    # or a third party chiming in — is a post-request touch too, and crediting it to
    # the author hands the ball back to a reviewer while the author has not moved.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {"timelineItems": {"nodes": [_touch("comment", "reviewer")]}},
                "pr_2": {"timelineItems": {"nodes": [_touch("review:commented", "reviewer")]}},
                "pr_3": {"timelineItems": {"nodes": [_touch("comment", "a-bystander")]}},
            }
        }
    }
    out = run_collect(
        tmp_path,
        prs=[
            pr(1, review="CHANGES_REQUESTED"),
            pr(2, head="feat/y", review="CHANGES_REQUESTED"),
            pr(3, head="feat/z", review="CHANGES_REQUESTED"),
        ],
        issues=[],
        graphql=graphql,
    )
    assert _owed(out) == {
        1: ("author", "changes-requested"),
        2: ("author", "changes-requested"),
        3: ("author", "changes-requested"),
    }


def test_owed_by_credits_the_author_whatever_the_touch_kind(tmp_path):
    # The author can respond by pushing, commenting, or replying in a review; all three
    # put the ball back with the reviewer. The gate is who touched it, not how.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {"timelineItems": {"nodes": [_touch("commit", "pr-author")]}},
                "pr_2": {"timelineItems": {"nodes": [_touch("comment", "pr-author")]}},
                "pr_3": {"timelineItems": {"nodes": [_touch("review:commented", "pr-author")]}},
            }
        }
    }
    out = run_collect(
        tmp_path,
        prs=[
            pr(1, review="CHANGES_REQUESTED"),
            pr(2, head="feat/y", review="CHANGES_REQUESTED"),
            pr(3, head="feat/z", review="CHANGES_REQUESTED"),
        ],
        issues=[],
        graphql=graphql,
    )
    assert _owed(out) == {
        1: ("reviewer", "author-responded"),
        2: ("reviewer", "author-responded"),
        3: ("reviewer", "author-responded"),
    }


def test_owed_by_reports_unknown_when_last_touch_is_unavailable(tmp_path):
    # fetch_pr_graph degrades to {} on a GraphQL failure. A missing measurement
    # must not be able to masquerade as a verdict, so this reports unknown rather
    # than defaulting to either party.
    out = run_collect(tmp_path, prs=[pr(1, review="CHANGES_REQUESTED")], issues=[], graphql=None)
    assert _owed(out) == {1: (None, "unknown")}


def test_owed_by_covers_approved_conflicting_unreviewed_and_draft(tmp_path):
    graphql = {
        "data": {
            "repository": {
                "pr_1": {"timelineItems": {"nodes": [_touch("review:approved", "reviewer")]}},
                "pr_2": {"timelineItems": {"nodes": [_touch("review:approved", "reviewer")]}},
                "pr_3": {"timelineItems": {"nodes": [_touch("comment", "someone")]}},
                "pr_4": {"timelineItems": {"nodes": [_touch("commit", "author")]}},
            }
        }
    }
    out = run_collect(
        tmp_path,
        prs=[
            pr(1, review="APPROVED", mergeable="CONFLICTING"),
            pr(2, head="feat/y", review="APPROVED"),
            pr(3, head="feat/z", review="REVIEW_REQUIRED"),
            pr(4, head="feat/w", draft=True, review="CHANGES_REQUESTED"),
        ],
        issues=[],
        graphql=graphql,
    )
    assert _owed(out) == {
        1: ("author", "needs-rebase"),  # review loop done; only the author can act
        2: ("maintainer", "ready-to-merge"),  # nothing blocks it but the merge button
        3: ("reviewer", "unreviewed"),
        4: (None, None),  # drafts are not in the review loop
    }


def test_owed_by_does_not_call_an_uncomputed_merge_state_ready(tmp_path):
    # GitHub reports mergeable=UNKNOWN while it recomputes, which it does for every
    # open PR whenever the base branch moves — so a whole board reads UNKNOWN right
    # after any merge. Testing `!= CONFLICTING` here reported three genuinely DIRTY
    # PRs as ready-to-merge.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {"timelineItems": {"nodes": [_touch("review:approved", "reviewer")]}},
            }
        }
    }
    out = run_collect(
        tmp_path,
        prs=[pr(1, review="APPROVED", mergeable="UNKNOWN")],
        issues=[],
        graphql=graphql,
    )
    assert _owed(out) == {1: (None, "unknown")}


def _review(state, by, oid):
    return {"state": state, "author": {"login": by}, "commit": {"oid": oid}}


def _signals(out):
    return {p["number"]: (p["approvals"], p["threads"]) for p in out["repos"][0]["prs"]}


def test_approvals_split_at_head_from_stale_and_threads_count_unresolved(tmp_path):
    # reviewDecision stays APPROVED after a push, so "approved" alone can describe
    # code nobody has seen. Only an approval submitted on headRefOid is at-head; a
    # reviewer whose latest opinion is CHANGES_REQUESTED is not an approver at all.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {
                    "timelineItems": {"nodes": []},
                    "headRefOid": "new",
                    "latestOpinionatedReviews": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            _review("APPROVED", "fresh", "new"),
                            _review("APPROVED", "earlier", "old"),
                            _review("CHANGES_REQUESTED", "objector", "new"),
                        ],
                    },
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {"isResolved": False, "isOutdated": False},
                            {"isResolved": False, "isOutdated": True},
                            {"isResolved": True, "isOutdated": True},
                        ],
                    },
                },
            }
        }
    }
    out = run_collect(tmp_path, prs=[pr(1, review="APPROVED")], issues=[], graphql=graphql)
    assert _signals(out) == {
        1: (
            {"atHead": ["fresh"], "stale": ["earlier"]},
            # No comment data on these threads, so who spoke last is unmeasured.
            {
                "unresolved": 2,
                "outdated": 1,
                "replied": None,
                "repliedTo": None,
                "resolvedUnanswered": None,
            },
        )
    }


def _thread(resolved, last_by, n=1, outdated=False):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"totalCount": n, "nodes": [{"author": {"login": last_by}}]},
    }


def test_threads_split_on_who_spoke_last_and_reviewers_union_requests(tmp_path):
    # A thread whose last word is the PR author's is the reviewer's move; one
    # the reviewer spoke last on is back with the author, even if the author
    # replied earlier in it. Reviewers: GitHub drops a request once the
    # reviewer reviews, so the pending list alone undercounts.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {
                    "timelineItems": {"nodes": []},
                    "author": {"login": "author"},
                    "comments": {"totalCount": 3},
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            _thread(False, "author", n=2),
                            _thread(False, "reviewer", n=3),
                            _thread(True, "reviewer", n=1),
                        ],
                    },
                    "reviewRequests": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{"requestedReviewer": {"login": "pending"}}],
                    },
                    "latestReviews": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {"state": "COMMENTED", "author": {"login": "reviewer"}},
                            {"state": "PENDING", "author": {"login": "drafting"}},
                            {"state": "COMMENTED", "author": {"login": "author"}},
                        ],
                    },
                },
            }
        }
    }
    out = run_collect(tmp_path, prs=[pr(1)], issues=[], graphql=graphql)
    got = out["repos"][0]["prs"][0]
    assert got["threads"] == {
        "unresolved": 2,
        "outdated": 0,
        "replied": 1,
        "repliedTo": {},  # the answered thread's fixture has only the author in it
        "resolvedUnanswered": 1,  # the resolved thread only the reviewer spoke in
    }
    assert got["reviewers"] == {
        "requested": ["pending"],
        "reviewed": ["reviewer"],
        "lastRequestedAt": None,
    }
    assert got["comments"] == 3 + 2 + 3 + 1


def test_merge_ready_blocks_on_any_failed_check_and_carries_warnings(tmp_path):
    graphql = {
        "data": {
            "repository": {
                "pr_1": {
                    "timelineItems": {"nodes": []},
                    "author": {"login": "pr-author"},
                    "headRefOid": "h",
                    "comments": {"totalCount": 0},
                    "latestOpinionatedReviews": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [_review("APPROVED", "ok", "h")],
                    },
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        # resolved, and only the reviewer ever spoke: closed without a reply
                        "nodes": [_thread(True, "reviewer")],
                    },
                    "reviewRequests": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{"requestedReviewer": {"login": "late"}}],
                    },
                    "latestReviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                    "reviewRequestedAt": {"nodes": [{"createdAt": "2026-08-01T00:00:00Z"}]},
                },
                "pr_2": {
                    "timelineItems": {"nodes": []},
                    "author": {"login": "pr-author"},
                    "headRefOid": "h",
                    "latestOpinionatedReviews": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            _review("APPROVED", "ok", "h"),
                            _review("CHANGES_REQUESTED", "no", "h"),
                        ],
                    },
                    "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                },
            }
        }
    }
    out = run_collect(
        tmp_path,
        prs=[pr(1, review="APPROVED"), pr(2, head="feat/y", review="CHANGES_REQUESTED")],
        issues=[],
        graphql=graphql,
    )
    got = {p["number"]: p for p in out["repos"][0]["prs"]}
    m1 = got[1]["mergeReady"]
    assert m1["verdict"] == "ready"
    warns = {c["check"]: c["note"] for c in m1["checks"] if c["state"] == "warn"}
    assert warns == {
        "resolved-unanswered": "1 resolved without a reply",
        "pending-reviews": "awaiting late",
    }
    assert got[1]["reviewers"]["lastRequestedAt"] == "2026-08-01T00:00:00Z"
    m2 = got[2]["mergeReady"]
    assert m2["verdict"] == "blocked"
    assert [c["note"] for c in m2["checks"] if c["state"] == "fail"] == ["changes requested by no"]


def test_merge_ready_is_unknown_not_ready_when_unmeasured(tmp_path):
    # Every check passes that can be measured, but the approval cannot. A
    # missing fact must not read as "ready".
    out = run_collect(tmp_path, prs=[pr(1, review="APPROVED")], issues=[], graphql=None)
    assert out["repos"][0]["prs"][0]["mergeReady"]["verdict"] == "unknown"


def test_a_failed_graphql_call_degrades_to_unmeasured(tmp_path):
    # gh prints GitHub's JSON error body to stdout and exits non-zero. Appending
    # a fallback {} to that output made two JSON documents, which --argjson
    # rejected, so one failed enrichment call aborted the whole board.
    out = run_collect(
        tmp_path,
        prs=[pr(1, review="APPROVED")],
        issues=[],
        graphql={"errors": [{"message": "rate limited"}]},
        graphql_exit=1,
    )
    got = out["repos"][0]["prs"][0]
    assert (got["lastTouch"], got["approvals"], got["threads"]) == (None, None, None)
    assert got["mergeReady"]["verdict"] == "unknown"


def test_waiting_on_keeps_only_prs_that_owe_that_login_a_move(tmp_path):
    def node(**kw):
        base = {
            "timelineItems": {"nodes": []},
            "author": {"login": "pr-author"},
            "headRefOid": "h",
            "latestOpinionatedReviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            "reviewRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            "latestReviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        }
        base.update(kw)
        return base

    def thread(*by):
        return {
            "isResolved": False,
            "isOutdated": False,
            "comments": {"totalCount": len(by), "nodes": [{"author": {"login": b}} for b in by]},
        }

    graphql = {
        "data": {
            "repository": {
                "pr_1": node(
                    reviewRequests={
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{"requestedReviewer": {"login": "me"}}],
                    }
                ),
                "pr_2": node(
                    latestOpinionatedReviews={
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [_review("APPROVED", "me", "old")],
                    }
                ),
                "pr_3": node(
                    reviewThreads={
                        "pageInfo": {"hasNextPage": False},
                        # me spoke, the author answered: my move. The second
                        # thread waits on the author, not on me.
                        "nodes": [thread("me", "pr-author"), thread("pr-author", "me")],
                    }
                ),
                "pr_4": node(),  # nothing owed
                "pr_5": node(),  # my own PR is never waiting on me
            }
        }
    }
    out = run_collect(
        tmp_path,
        "--waiting-on",
        "me",
        prs=[
            pr(1),
            pr(2, head="b"),
            pr(3, head="c"),
            pr(4, head="d"),
            pr(5, head="e", author="me"),
        ],
        issues=[],
        graphql=graphql,
    )
    assert out["waitingOn"] == "me"
    assert {p["number"]: p["waitingOn"] for p in out["repos"][0]["prs"]} == {
        1: ["review requested"],
        2: ["your approval predates the head"],
        3: ["1 thread(s) answered, your move"],
    }


def test_review_signals_are_null_when_not_measured(tmp_path):
    # A truncated page or a failed query must not read as "no approvals" or
    # "nothing outstanding" — those are the answers that let a PR through.
    graphql = {
        "data": {
            "repository": {
                "pr_1": {
                    "timelineItems": {"nodes": []},
                    "headRefOid": "h",
                    "latestOpinionatedReviews": {"pageInfo": {"hasNextPage": True}, "nodes": []},
                    "reviewThreads": {"pageInfo": {"hasNextPage": True}, "nodes": []},
                },
                "pr_2": {"timelineItems": {"nodes": []}},
            }
        }
    }
    out = run_collect(tmp_path, prs=[pr(1), pr(2, head="feat/y")], issues=[], graphql=graphql)
    assert _signals(out) == {1: (None, None), 2: (None, None)}
    assert [(p["reviewers"], p["comments"]) for p in out["repos"][0]["prs"]] == [
        (None, None),
        (None, None),
    ]
    (tmp_path / "empty").mkdir()
    out = run_collect(tmp_path / "empty", prs=[pr(1)], issues=[], graphql=None)
    assert _signals(out) == {1: (None, None)}


def test_issue_links_come_from_refs_and_body_keywords(tmp_path):
    prs = [pr(1, closes=[5]), pr(2, head="feat/y", body="Fixes #6")]
    out = run_collect(tmp_path, prs=prs, issues=[issue(5), issue(6)])
    linked = {
        i["number"]: [p["number"] for p in i["linkedPrs"]] for i in out["repos"][0]["openIssues"]
    }
    assert linked == {5: [1], 6: [2]}


def test_stacked_on_resolves_to_the_parent_pr(tmp_path):
    prs = [pr(1, head="feat/base"), pr(2, head="feat/top", base="feat/base")]
    out = run_collect(tmp_path, prs=prs, issues=[])
    stacked = {p["number"]: p["stackedOn"] for p in out["repos"][0]["prs"]}
    assert stacked == {1: None, 2: 1}


def test_stacked_on_ignores_a_fork_head_named_like_the_base(tmp_path):
    # A fork PR opened from the fork's own `main` has head "main"; it must not
    # become the parent of every PR into the upstream `main`, itself included.
    prs = [
        pr(1, head="main", cross=True),
        pr(2, head="feat/x"),
        pr(3, head="feat/top", base="feat/x"),
    ]
    out = run_collect(tmp_path, prs=prs, issues=[])
    stacked = {p["number"]: p["stackedOn"] for p in out["repos"][0]["prs"]}
    assert stacked == {1: None, 2: None, 3: 2}


def test_merged_since_adds_recap_lanes(tmp_path):
    merged = [
        {
            "number": 9,
            "title": "landed",
            "headRefName": "feat/done",
            "mergedAt": "2026-08-01T00:00:00Z",
            "url": "https://x/pull/9",
            "additions": 1,
            "deletions": 0,
            "author": {"login": "someone"},
            "body": "Closes #5",
            "labels": [],
            "closingIssuesReferences": [],
        }
    ]
    closed = [
        {
            "number": 5,
            "title": "an issue",
            "labels": [],
            "url": "https://x/issues/5",
            "closedAt": "2026-08-01T00:00:00Z",
        }
    ]
    out = run_collect(
        tmp_path, "--merged-since", "7d", prs=[], merged=merged, closed=closed, issues=[]
    )
    repo = out["repos"][0]
    assert repo["mergedCount"] == 1
    # the "just landed" row: a closed issue whose linked PR is merged
    assert repo["closedIssues"][0]["linkedPrs"] == [
        {"number": 9, "url": "https://x/pull/9", "state": "merged"}
    ]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
