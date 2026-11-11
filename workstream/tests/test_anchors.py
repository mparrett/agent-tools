"""anchors: liveness against real git repos built in tmp_path, a fake gh, and
an injected registry. Every name here is invented."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workstream import anchors
from workstream.anchors import liveness, liveness_many, parse_ref


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    # The user's global config (rebase.updateRefs, signing, hooks) must not
    # reach these repos, and nothing may land in the real cache dir.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test Person")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")
    monkeypatch.setenv("WORKSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def commit(repo: Path, name: str, msg: str) -> None:
    (repo / name).write_text(msg + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def world(tmp_path):
    """A bare origin, its clone `lanternfish` with origin/HEAD set, and a
    linked worktree on `feat/gill` carrying one unmerged commit and a dirty file."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare")
    repo = tmp_path / "lanternfish"
    repo.mkdir()
    git(repo, "init", "-q")
    commit(repo, "a.txt", "first")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")

    wt = tmp_path / "wt-gill"
    git(repo, "worktree", "add", "-q", "-b", "feat/gill", str(wt))
    commit(wt, "g.txt", "gill work")
    (wt / "scratch.txt").write_text("uncommitted\n")
    return {"origin": origin, "repo": repo, "wt": wt, "tmp": tmp_path}


def test_parse_ref():
    assert parse_ref("gh:acme/widget#12") == ("gh", "acme/widget#12")
    assert parse_ref("wt:/a/b:c") == ("wt", "/a/b:c")
    assert parse_ref("nocolon") == ("", "nocolon")


def test_wt_linked_unmerged_dirty_no_upstream(world):
    a = liveness(f"wt:{world['wt']}")
    lv = a.liveness
    assert lv["exists"] and lv["dirty"] and lv["linked"]
    assert lv["branch"] == "feat/gill" and not lv["detached"]
    assert lv["upstream"] is None
    # Never pushed must not read as zero unpushed.
    assert lv["unpushed"] == "no upstream"
    assert lv["unmerged"] == 1
    assert lv["unmerged_commits"][0].endswith("gill work")
    assert lv["default_branch"] == "main"
    assert Path(lv["main_checkout"]).resolve() == world["repo"].resolve()
    assert a.warnings == []


def test_wt_unpushed_count_against_upstream(world):
    wt = world["wt"]
    git(wt, "push", "-q", "-u", "origin", "feat/gill")
    commit(wt, "h.txt", "second gill")
    commit(wt, "i.txt", "third gill")
    lv = liveness(f"wt:{wt}").liveness
    assert lv["upstream"] == "origin/feat/gill"
    assert lv["unpushed"] == 2
    assert lv["unmerged"] == 3


def test_wt_upstream_ref_deleted_is_not_zero(world):
    wt = world["wt"]
    git(wt, "push", "-q", "-u", "origin", "feat/gill")
    git(wt, "push", "-q", "origin", "--delete", "feat/gill")
    lv = liveness(f"wt:{wt}").liveness
    assert lv["upstream"] == "origin/feat/gill"
    assert lv["unpushed"] == "no upstream"


def test_wt_pushed_without_tracking(world):
    wt = world["wt"]
    git(wt, "push", "-q", "origin", "feat/gill")
    commit(wt, "h.txt", "after push")
    lv = liveness(f"wt:{wt}").liveness
    assert lv["upstream"] is None
    assert lv["unpushed"] == 1


def test_wt_canonical_checkout_clean(world):
    lv = liveness(f"wt:{world['repo']}").liveness
    assert lv["linked"] is False
    assert lv["dirty"] is False
    assert lv["unpushed"] == 0
    assert lv["unmerged"] == 0


def test_wt_no_remote_and_master_fallback(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    commit(repo, "a.txt", "only")
    git(repo, "switch", "-q", "-c", "side")
    commit(repo, "b.txt", "side work")
    lv = liveness(f"wt:{repo}").liveness
    assert lv["unpushed"] == "no remote"
    assert lv["default_branch"] == "master"
    assert lv["unmerged"] == 1


def test_wt_missing_and_non_git(tmp_path):
    missing = liveness(f"wt:{tmp_path / 'gone'}")
    assert missing.liveness["exists"] is False and missing.warnings == []
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    a = liveness(f"wt:{scratch}")
    assert a.liveness["exists"] is True and a.liveness["dirty"] is None
    assert any("not a git work tree" in w for w in a.warnings)


def test_branch_via_hint(world):
    repo = world["repo"]
    git(repo, "switch", "-q", "-c", "feat/merged")
    commit(repo, "m.txt", "merged work")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "feat/merged")
    hints = {"lanternfish": str(repo)}

    merged = liveness("branch:lanternfish@feat/merged", repo_hints=hints).liveness
    assert merged["exists_local"] and not merged["exists_remote"]
    assert merged["merged_into_default"] is True

    open_ = liveness("branch:lanternfish@feat/gill", repo_hints=hints).liveness
    assert open_["exists_local"] and open_["merged_into_default"] is False

    absent = liveness("branch:lanternfish@nope", repo_hints=hints).liveness
    assert absent["exists_local"] is False and absent["merged_into_default"] is None


def test_branch_found_under_home_and_missing_repo(world, tmp_path):
    home_repo = tmp_path / "home" / "projects-new" / "3p" / "lanternfish"
    home_repo.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", str(world["origin"]), str(home_repo))
    lv = liveness("branch:lanternfish@main").liveness
    assert Path(lv["repo"]) == home_repo
    assert lv["exists_local"] and lv["exists_remote"] and lv["merged_into_default"]

    a = liveness("branch:no-such-repo@main")
    assert a.liveness["repo"] is None
    assert any("not found" in w for w in a.warnings)


PR_JSON = {
    "state": "OPEN",
    "isDraft": False,
    "reviewDecision": "CHANGES_REQUESTED",
    "updatedAt": "2026-10-29T12:00:00Z",
    "mergedAt": None,
    "headRefName": "feat/gill",
    "reviews": [
        {
            "author": {"login": f"rev{i}"},
            "state": "COMMENTED",
            "submittedAt": f"2026-10-2{i}T00:00:00Z",
        }
        for i in range(5)
    ],
}


class FakeGh:
    def __init__(self, pr=None, issue=None):
        self.pr, self.issue, self.calls = pr, issue, []

    def __call__(self, argv):
        self.calls.append(argv)
        payload = self.pr if argv[1] == "pr" else self.issue
        if payload is None:
            return 1, ""
        return 0, json.dumps(payload)


def test_gh_pr_last_three_reviews():
    gh = FakeGh(pr=PR_JSON)
    a = liveness("gh:acme/widget#12", gh_runner=gh, now=1_000_000.0)
    lv = a.liveness
    assert lv["kind"] == "pr" and lv["state"] == "OPEN"
    assert lv["review_decision"] == "CHANGES_REQUESTED"
    assert [r["author"] for r in lv["reviews"]] == ["rev2", "rev3", "rev4"]
    assert lv["cached"] is False
    assert gh.calls[0][:5] == ["gh", "pr", "view", "12", "--repo"]


def test_gh_falls_back_to_issue():
    gh = FakeGh(issue={"state": "CLOSED", "updatedAt": "x", "closedAt": "y"})
    lv = liveness("gh:acme/widget#7", gh_runner=gh, now=1.0).liveness
    assert lv["kind"] == "issue" and lv["state"] == "CLOSED"
    assert [c[1] for c in gh.calls] == ["pr", "issue"]


def test_gh_both_fail_warns_and_does_not_cache(tmp_path):
    gh = FakeGh()
    a = liveness("gh:acme/widget#404", gh_runner=gh, now=1.0)
    assert a.liveness["kind"] is None and a.warnings
    assert not (tmp_path / "cache" / "gh" / "acme__widget__404.json").exists()


def test_gh_cache_put_from_many_threads_on_one_key(tmp_path):
    # `workstream timeline` builds frontiers on threads; two can write one key.
    from concurrent.futures import ThreadPoolExecutor

    def put(i: int):
        return anchors._cache_put("acme__widget__7", float(i), {"state": "OPEN", "pad": "x" * 4096})

    with ThreadPoolExecutor(max_workers=8) as pool:
        warnings = list(pool.map(put, range(200)))
    assert warnings == [None] * 200
    cached = json.loads((tmp_path / "cache" / "gh" / "acme__widget__7.json").read_text())
    assert cached["state"] == "OPEN"
    assert not list((tmp_path / "cache" / "gh").glob("*.tmp"))


def test_gh_cache_put_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    def fail(self, target):
        raise OSError("no room")

    monkeypatch.setattr(Path, "replace", fail)
    assert anchors._cache_put("acme__widget__8", 1.0, {"state": "OPEN"}) == (
        "cache write failed (OSError)"
    )
    assert not list((tmp_path / "cache" / "gh").glob("*.tmp"))


def test_gh_cache_hit_then_expiry(tmp_path):
    gh = FakeGh(pr=PR_JSON)
    t0 = 2_000_000.0
    liveness("gh:acme/widget#12", gh_runner=gh, now=t0)
    assert (tmp_path / "cache" / "gh" / "acme__widget__12.json").is_file()

    hit = liveness("gh:acme/widget#12", gh_runner=gh, now=t0 + 599).liveness
    assert hit["cached"] is True and len(gh.calls) == 1

    miss = liveness("gh:acme/widget#12", gh_runner=gh, now=lambda: t0 + 601).liveness
    assert miss["cached"] is False and len(gh.calls) == 2


def test_gh_malformed_ref():
    a = liveness("gh:widget-12", gh_runner=FakeGh())
    assert a.warnings and "expected" in a.warnings[0]


def test_file_and_frontier(tmp_path):
    f = tmp_path / "capture.json"
    f.write_text("{}")
    present = liveness(f"frontier:{f}").liveness
    assert present["exists"] is True and present["mtime"].endswith("Z")
    absent = liveness(f"file:{tmp_path / 'nope.md'}").liveness
    assert absent == {"exists": False, "mtime": None}


def test_sessions_against_injected_registry():
    reg = [{"sessionId": "0a1b2c3d-1111-2222-3333-444455556666", "pid": 4242}]
    out = liveness_many(
        [
            "claude:0a1b2c3d",
            "claude:ffffffff",
            "agent:0a1b2c3d-1111-2222-3333-444455556666/agent-x1",
            "codex:rollout-zzz",
        ],
        registry=reg,
    )
    assert out[0].liveness["live"] is True and out[0].liveness["pid"] == 4242
    assert out[1].liveness["live"] is False
    assert out[2].liveness["live"] is True
    assert out[3].liveness["live"] is None and out[3].warnings


def test_registry_read_failure_is_a_warning(monkeypatch):
    def boom():
        raise RuntimeError("bad registry")

    monkeypatch.setattr(anchors.seam, "live_registry", boom)
    a = liveness("claude:0a1b2c3d")
    assert a.liveness["live"] is None
    assert any("registry" in w for w in a.warnings)


def test_unknown_scheme_and_no_scheme_warn():
    out = liveness_many(["jira:ABC-1", "plain-text", "ws:some-stream"])
    assert "unknown anchor scheme" in out[0].warnings[0]
    assert out[1].warnings
    assert out[2].warnings == [] and out[2].liveness == {}


def test_probe_exception_never_raises(monkeypatch):
    def explode(*_a, **_k):
        raise ValueError("kaboom")

    monkeypatch.setattr(anchors, "_probe_file", explode)
    a = liveness("file:/x")
    assert any("kaboom" in w for w in a.warnings)


# --- squash merges -----------------------------------------------------------


def squash_onto_main(repo: Path, branch: str) -> None:
    """Land `branch` the way GitHub's squash button does: one new commit on
    main with the branch's changes, leaving the branch tip unreachable."""
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--squash", branch)
    git(repo, "commit", "-q", "-m", f"{branch} (#41)")


@pytest.fixture
def squashed(world):
    """`feat/fin` squash-merged into main; `feat/gill` (from `world`) still open."""
    repo = world["repo"]
    git(repo, "switch", "-q", "-c", "feat/fin")
    commit(repo, "f.txt", "fin work")
    commit(repo, "f2.txt", "more fin work")
    squash_onto_main(repo, "feat/fin")
    return world


MERGED_ROW = {
    "number": 41,
    "mergedAt": "2026-10-30T09:00:00Z",
    "mergeCommit": {"oid": "c0ffee0000000000000000000000000000000041"},
}


class FakeHeadGh:
    """`gh pr list --head` answering from a {branch: rows} map; anything else fails."""

    def __init__(self, by_head: dict[str, list[dict]], rc: int = 0):
        self.by_head, self.rc, self.calls = by_head, rc, []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[1:3] != ["pr", "list"] or self.rc:
            return self.rc or 1, ""
        return 0, json.dumps(self.by_head.get(argv[argv.index("--head") + 1], []))


def on_github(repo: Path) -> None:
    git(repo, "remote", "set-url", "origin", "git@github.com:acme/lanternfish.git")


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("git@github.com:acme/lanternfish.git", "acme/lanternfish"),
        ("https://github.com/acme/lanternfish.git", "acme/lanternfish"),
        ("https://github.com/acme/lanternfish", "acme/lanternfish"),
        ("ssh://git@github.com/acme/lanternfish.git", "acme/lanternfish"),
        (
            "https://ghe.example.invalid/acme/lanternfish.git",
            "ghe.example.invalid/acme/lanternfish",
        ),
        ("/tmp/origin.git", None),
        ("file:///tmp/origin.git", None),
    ],
)
def test_gh_repo_from_url(url, want):
    assert anchors.gh_repo_from_url(url) == want


def test_squash_merged_branch_reads_merged_via_github(squashed):
    repo = squashed["repo"]
    on_github(repo)
    head = git(repo, "rev-parse", "feat/fin")
    gh = FakeHeadGh({"feat/fin": [{**MERGED_ROW, "headRefOid": head}]})
    hints = {"lanternfish": str(repo)}

    fin = liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh, now=1.0)
    assert fin.liveness["merged_into_default"] is True
    assert fin.liveness["merged_via"] == {
        "pr": 41,
        "merged_at": "2026-10-30T09:00:00Z",
        "commit": MERGED_ROW["mergeCommit"]["oid"],
    }
    assert gh.calls[0][:5] == ["gh", "pr", "list", "--repo", "acme/lanternfish"]
    assert gh.calls[0][gh.calls[0].index("--head") + 1] == "feat/fin"

    gill = liveness("branch:lanternfish@feat/gill", repo_hints=hints, gh_runner=gh, now=1.0)
    assert gill.liveness["merged_into_default"] is False
    assert gill.liveness["merged_via"] is None and gill.warnings == []


def test_squash_merged_without_github_falls_back_to_tree(squashed):
    # `world`'s origin is a local path: no owner/name, so gh is never asked.
    gh = FakeHeadGh({})
    hints = {"lanternfish": str(squashed["repo"])}
    fin = liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh).liveness
    assert fin["merged_into_default"] is True and fin["merged_via"] == {"tree": True}
    gill = liveness("branch:lanternfish@feat/gill", repo_hints=hints, gh_runner=gh).liveness
    assert gill["merged_into_default"] is False and gill["merged_via"] is None
    assert gh.calls == []


def test_gh_failure_records_nothing_and_tree_still_decides(squashed):
    on_github(squashed["repo"])
    gh = FakeHeadGh({}, rc=4)
    hints = {"lanternfish": str(squashed["repo"])}
    a = liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh, now=1.0)
    assert a.liveness["merged_via"] == {"tree": True} and a.warnings == []
    assert not any((Path(anchors.gh_cache_dir())).glob("head__*"))


def test_conflicting_tree_is_inconclusive(world):
    repo = world["repo"]
    git(repo, "switch", "-q", "-c", "feat/eel")
    commit(repo, "a.txt", "eel's version")
    git(repo, "switch", "-q", "main")
    commit(repo, "a.txt", "main's version")
    lv = liveness("branch:lanternfish@feat/eel", repo_hints={"lanternfish": str(repo)}).liveness
    assert lv["merged_into_default"] is False and lv["merged_via"] is None


def test_reused_head_branch_is_not_merged_by_its_old_pr(squashed):
    repo = squashed["repo"]
    on_github(repo)
    old_head = git(repo, "rev-parse", "feat/fin")
    git(repo, "switch", "-q", "feat/fin")
    commit(repo, "f3.txt", "fin follow-up after the merge")
    gh = FakeHeadGh({"feat/fin": [{**MERGED_ROW, "headRefOid": old_head}]})
    lv = liveness(
        "branch:lanternfish@feat/fin", repo_hints={"lanternfish": str(repo)}, gh_runner=gh, now=1.0
    ).liveness
    assert lv["merged_into_default"] is False and lv["merged_via"] is None


def test_a_deleted_branch_reads_merged_from_its_head_alone(squashed):
    repo = squashed["repo"]
    on_github(repo)
    git(repo, "branch", "-q", "-D", "feat/fin")
    gh = FakeHeadGh({"feat/fin": [{**MERGED_ROW, "headRefOid": "0" * 40}]})
    hints = {"lanternfish": str(repo)}
    lv = liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh, now=1.0).liveness
    assert lv["exists_local"] is False and lv["merged_into_default"] is True
    assert lv["merged_via"] == {
        "pr": 41,
        "merged_at": "2026-10-30T09:00:00Z",
        "commit": MERGED_ROW["mergeCommit"]["oid"],
        "head_only": True,
    }
    # No merged PR from that head: still unknown, never "unmerged".
    gone = liveness("branch:lanternfish@feat/none", repo_hints=hints, gh_runner=gh, now=1.0)
    assert gone.liveness["merged_into_default"] is None and gone.liveness["merged_via"] is None


def test_head_lookup_is_cached(squashed):
    repo = squashed["repo"]
    on_github(repo)
    head = git(repo, "rev-parse", "feat/fin")
    gh = FakeHeadGh({"feat/fin": [{**MERGED_ROW, "headRefOid": head}]})
    hints = {"lanternfish": str(repo)}
    t0 = 3_000_000.0
    first = liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh, now=t0)
    second = liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh, now=t0 + 599)
    assert len(gh.calls) == 1
    assert first.liveness["merged_via"] == second.liveness["merged_via"]
    assert second.liveness["merged_via"]["pr"] == 41

    # "no merged PR" is an answer too, and is cached the same way.
    liveness("branch:lanternfish@feat/gill", repo_hints=hints, gh_runner=gh, now=t0)
    liveness("branch:lanternfish@feat/gill", repo_hints=hints, gh_runner=gh, now=t0 + 1)
    assert len(gh.calls) == 2

    liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh, now=t0 + 601)
    assert len(gh.calls) == 3


def test_squash_merged_worktree(world):
    repo, wt = world["repo"], world["wt"]
    (wt / "scratch.txt").unlink()
    squash_onto_main(repo, "feat/gill")
    on_github(repo)
    gh = FakeHeadGh({"feat/gill": [{**MERGED_ROW, "headRefOid": git(wt, "rev-parse", "HEAD")}]})
    lv = liveness(f"wt:{wt}", gh_runner=gh, now=1.0).liveness
    # The count is still reachability's; the verdict is the squash-aware one.
    assert lv["unmerged"] == 1 and lv["dirty"] is False
    assert lv["merged_into_default"] is True and lv["merged_via"]["pr"] == 41


def test_unmerged_worktree_and_tree_fallback(world):
    open_ = liveness(f"wt:{world['wt']}", gh_runner=FakeHeadGh({})).liveness
    assert open_["merged_into_default"] is False and open_["merged_via"] is None

    squash_onto_main(world["repo"], "feat/gill")
    landed = liveness(f"wt:{world['wt']}", gh_runner=FakeHeadGh({})).liveness
    assert landed["merged_into_default"] is True and landed["merged_via"] == {"tree": True}


UNKNOWN_OID = "deadbeef" * 5


def test_unknown_pr_head_is_not_credited_and_tree_decides(squashed):
    # A fresh branch named like an old merged PR whose commits were never
    # fetched (or were pruned): the name match alone proves nothing.
    repo = squashed["repo"]
    on_github(repo)
    rows = [{**MERGED_ROW, "headRefOid": UNKNOWN_OID}]
    gh = FakeHeadGh({"feat/fin": rows, "feat/gill": rows})
    hints = {"lanternfish": str(repo)}

    # feat/fin's content is on main, so the tree test still finds it landed.
    fin = liveness("branch:lanternfish@feat/fin", repo_hints=hints, gh_runner=gh, now=1.0)
    assert fin.liveness["merged_via"] == {"tree": True}

    # feat/gill's tree differs from main: unmerged, not credited to PR 41.
    gill = liveness("branch:lanternfish@feat/gill", repo_hints=hints, gh_runner=gh, now=1.0)
    assert gill.liveness["merged_into_default"] is False
    assert gill.liveness["merged_via"] is None
    assert len(gh.calls) == 2


def test_pr_row_without_head_oid_is_not_credited(world):
    on_github(world["repo"])
    gh = FakeHeadGh({"feat/gill": [MERGED_ROW]})
    lv = liveness(f"wt:{world['wt']}", gh_runner=gh, now=1.0).liveness
    assert lv["merged_into_default"] is False and lv["merged_via"] is None
