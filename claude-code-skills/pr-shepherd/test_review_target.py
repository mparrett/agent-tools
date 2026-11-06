"""review_target.py against a local bare "origin" and a stubbed gh."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "scripts" / "review_target.py"


def sh(*args, cwd=None, env=None, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=check)


def git(cwd, *args) -> str:
    return sh("git", "-C", str(cwd), *args).stdout.strip()


def commit(cwd, name, text) -> str:
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", f"add {name}")
    return git(cwd, "rev-parse", "HEAD")


class Repo:
    """A bare origin with a `main` and a PR #1 branch, a clone on main, and a gh stub."""

    def __init__(self, tmp_path: Path):
        self.bare = tmp_path / "origin.git"
        sh("git", "init", "-q", "--bare", str(self.bare))
        # a bare repo's HEAD follows init.defaultBranch, which CI runners leave unset
        # (master); the clone would then have no HEAD at all
        git(self.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        seed = tmp_path / "seed"
        sh("git", "init", "-q", "-b", "main", str(seed))
        commit(seed, "a.txt", "one\n")
        git(seed, "remote", "add", "origin", str(self.bare))
        git(seed, "push", "-q", "origin", "main")
        git(seed, "checkout", "-q", "-b", "feature")
        self.feature_head = commit(seed, "b.txt", "two\n")
        git(seed, "push", "-q", "origin", "feature")
        self.seed = seed
        self.set_pull_ref(self.feature_head)
        self.clone = tmp_path / "clone"
        sh("git", "clone", "-q", str(self.bare), str(self.clone))
        self.gh_file = tmp_path / "gh.json"
        stub = tmp_path / "gh"
        stub.write_text(f"#!/bin/sh\ncat {self.gh_file}\n")
        stub.chmod(0o755)
        self.env = {**os.environ, "REVIEW_TARGET_GH": str(stub)}
        self.set_gh(self.feature_head)

    def set_pull_ref(self, oid: str) -> None:
        git(self.bare, "update-ref", "refs/pull/1/head", oid)

    def set_gh(self, head_oid: str, base_ref: str = "main") -> None:
        base_oid = git(self.bare, "rev-parse", base_ref)
        self.gh_file.write_text(
            json.dumps(
                {
                    "number": 1,
                    "url": "https://example/pull/1",
                    "baseRefName": base_ref,
                    "baseRefOid": base_oid,
                    "headRefOid": head_oid,
                    "headRefName": "feature",
                    "state": "OPEN",
                }
            )
        )

    def advance_feature(self) -> str:
        oid = commit(self.seed, "c.txt", "three\n")
        git(self.seed, "push", "-q", "origin", "feature")
        self.set_pull_ref(oid)
        self.set_gh(oid)
        return oid

    def advance_main(self) -> str:
        """Unrelated work lands on main: the merge base with the feature does not move."""
        git(self.seed, "checkout", "-q", "main")
        oid = commit(self.seed, "d.txt", "four\n")
        git(self.seed, "push", "-q", "origin", "main")
        git(self.seed, "checkout", "-q", "feature")
        self.set_gh(self.feature_head)
        return oid

    def retarget(self, new_base: str) -> None:
        """GitHub now says the PR targets another branch; the head is unchanged."""
        git(self.seed, "branch", new_base, "main")
        git(self.seed, "push", "-q", "origin", new_base)
        self.set_gh(self.feature_head, base_ref=new_base)

    def other_repo(self, tmp_path: Path) -> str:
        """A second repository at <tmp>/o/r.git, registered as remote `upstream`, whose
        PR #1 has a different head, so a fetch from the wrong remote is detectable."""
        bare = tmp_path / "o" / "r.git"
        bare.parent.mkdir()
        sh("git", "init", "-q", "--bare", str(bare))
        git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
        seed = tmp_path / "seed2"
        sh("git", "init", "-q", "-b", "main", str(seed))
        commit(seed, "x.txt", "x\n")
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "-q", "origin", "main")
        git(seed, "checkout", "-q", "-b", "topic")
        head = commit(seed, "y.txt", "y\n")
        git(seed, "push", "-q", "origin", "topic")
        git(bare, "update-ref", "refs/pull/1/head", head)
        git(self.clone, "remote", "add", "upstream", str(bare))
        main_oid = git(bare, "rev-parse", "main")
        self.gh_file.write_text(
            json.dumps(
                {
                    "number": 1,
                    "url": "https://example/o/r/pull/1",
                    "baseRefName": "main",
                    "baseRefOid": main_oid,
                    "headRefOid": head,
                    "headRefName": "topic",
                    "state": "OPEN",
                }
            )
        )
        return head

    def merge_feature_into_main(self) -> None:
        """main fast-forwards onto the feature (e.g. a stacked base PR merged): merge base moves."""
        git(self.seed, "push", "-q", "origin", "feature:main")
        self.set_gh(self.feature_head)

    def rt(self, *args, check=False) -> subprocess.CompletedProcess:
        return sh(sys.executable, str(SCRIPT), *args, cwd=self.clone, env=self.env, check=check)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_begin_pins_explicit_objects(repo):
    r = repo.rt("begin", "--pr", "1")
    assert r.returncode == 0, r.stderr
    main_oid = git(repo.bare, "rev-parse", "main")
    assert f"review with: git diff {main_oid}..{repo.feature_head}" in r.stdout
    snap = json.loads((repo.clone / ".git" / "review-snapshots" / "pr-1.json").read_text())
    assert snap["head_oid"] == repo.feature_head and snap["merge_base"] == main_oid
    assert git(repo.clone, "rev-parse", "refs/review/pr-1/head") == repo.feature_head


def test_begin_refuses_when_fetch_disagrees_with_github(repo):
    repo.set_gh("0" * 40)  # GitHub claims a head the pull ref does not hold
    r = repo.rt("begin", "--pr", "1")
    assert r.returncode == 2 and "refusing to pin" in r.stderr


def test_check_passes_and_stamps_the_head(repo):
    repo.rt("begin", "--pr", "1", check=True)
    r = repo.rt("check", "--pr", "1")
    assert r.returncode == 0 and r.stdout.startswith(f"Reviewed head: {repo.feature_head}")


def test_check_reports_a_moved_head_with_the_delta(repo):
    repo.rt("begin", "--pr", "1", check=True)
    new = repo.advance_feature()
    r = repo.rt("check", "--pr", "1")
    assert r.returncode == 3
    assert "HEAD MOVED" in r.stdout
    assert f"git diff {repo.feature_head}..{new}" in r.stdout
    assert "merge base unchanged" in r.stdout


def test_unrelated_commits_on_main_do_not_invalidate(repo):
    repo.rt("begin", "--pr", "1", check=True)
    repo.advance_main()
    r = repo.rt("check", "--pr", "1")
    assert r.returncode == 0 and "merge base unchanged" in r.stdout


def test_check_reports_a_moved_merge_base(repo):
    repo.rt("begin", "--pr", "1", check=True)
    repo.merge_feature_into_main()
    r = repo.rt("check", "--pr", "1")
    assert r.returncode == 4 and "BASE MOVED" in r.stdout


def test_post_runs_only_against_an_unmoved_target(repo, tmp_path):
    repo.rt("begin", "--pr", "1", check=True)
    marker = tmp_path / "posted"
    r = repo.rt("post", "--pr", "1", "--", "touch", str(marker))
    assert r.returncode == 0 and marker.exists()
    marker.unlink()
    repo.advance_feature()
    r = repo.rt("post", "--pr", "1", "--", "touch", str(marker))
    assert r.returncode == 3 and not marker.exists()
    assert "refusing to post" in r.stderr


def test_worktree_pin_detects_edits(repo):
    git(repo.clone, "checkout", "-q", "-b", "local")
    (repo.clone / "a.txt").write_text("one edited\n")
    r = repo.rt("begin", "--worktree")
    assert r.returncode == 0, r.stderr
    assert "pinned worktree" in r.stdout and "git diff HEAD" in r.stdout
    assert repo.rt("check", "--worktree").returncode == 0
    (repo.clone / "new.txt").write_text("x\n")
    r = repo.rt("check", "--worktree")
    assert r.returncode == 3 and "WORKTREE CHANGED" in r.stdout and "new.txt" in r.stdout


def test_check_without_begin_says_so(repo):
    r = repo.rt("check", "--pr", "1")
    assert r.returncode != 0 and "run `begin` first" in r.stderr


def test_check_reports_a_retargeted_pr(repo):
    repo.rt("begin", "--pr", "1", check=True)
    repo.retarget("release")
    r = repo.rt("check", "--pr", "1")
    assert r.returncode == 4 and "RETARGETED: base was main, now release" in r.stdout


def test_worktree_pin_sees_untracked_content_changes(repo):
    git(repo.clone, "checkout", "-q", "-b", "local")
    (repo.clone / "new.txt").write_text("before\n")
    repo.rt("begin", "--worktree", check=True)
    assert repo.rt("check", "--worktree").returncode == 0
    (repo.clone / "new.txt").write_text("after\n")  # same path, same status line
    r = repo.rt("check", "--worktree")
    assert r.returncode == 3 and "untracked_hash" in r.stdout


def test_worktree_check_revalidates_the_base(repo):
    git(repo.clone, "checkout", "-q", "-b", "local")
    commit(repo.clone, "e.txt", "five\n")
    repo.rt("begin", "--worktree", check=True)
    assert repo.rt("check", "--worktree").returncode == 0
    git(repo.clone, "push", "-q", "origin", "local:main")  # origin/main advances onto HEAD
    r = repo.rt("check", "--worktree")
    assert r.returncode == 4 and "BASE MOVED" in r.stdout


def test_repo_flag_fetches_from_that_repository(repo, tmp_path):
    other_head = repo.other_repo(tmp_path)
    r = repo.rt("begin", "--pr", "1", "--repo", "o/r")
    assert r.returncode == 0, r.stderr
    assert "(fetched from upstream)" in r.stdout
    snap = json.loads((repo.clone / ".git" / "review-snapshots" / "pr-1.json").read_text())
    assert snap["head_oid"] == other_head and snap["source"] == "upstream"
    assert git(repo.clone, "rev-parse", "refs/review/pr-1/head") == other_head


def test_repo_flag_falls_back_to_the_github_url(repo, monkeypatch):
    sys.path.insert(0, str(SCRIPT.parent))
    import review_target as rt

    monkeypatch.chdir(repo.clone)
    assert (
        rt.fetch_source("someone/elsewhere", "origin") == "https://github.com/someone/elsewhere.git"
    )
    assert rt.fetch_source(None, "origin") == "origin"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def _tree_path(repo, pr=1):
    common = git(repo.clone, "rev-parse", "--git-common-dir")
    return (repo.clone / common / "review-worktrees" / f"pr-{pr}").resolve()


def test_tree_materializes_the_pinned_head(repo):
    # Reviewing a diff needs objects; running a check needs a tree, and the
    # checkout the reviewer is sitting in is somebody else's business.
    repo.rt("begin", "--pr", "1", check=True)
    r = repo.rt("tree", "--pr", "1")
    assert r.returncode == 0, r.stderr
    path = _tree_path(repo)
    assert path.is_dir()
    assert git(path, "rev-parse", "HEAD") == repo.feature_head
    # The PR's file is present here and absent from the clone, which is on main.
    assert (path / "b.txt").is_file()
    assert not (repo.clone / "b.txt").exists()


def test_tree_is_reused_when_it_already_sits_at_the_head(repo):
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    r = repo.rt("tree", "--pr", "1")
    assert r.returncode == 0
    assert "reusing" in r.stdout


def test_tree_is_not_reused_when_a_tracked_file_was_modified(repo):
    # HEAD still matches the pin, so an OID-only reuse would keep the edit and
    # the next suite would measure something other than the PR's contents.
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    path = _tree_path(repo)
    (path / "b.txt").write_text("edited\n")
    r = repo.rt("tree", "--pr", "1")
    assert r.returncode == 0, r.stderr
    assert "reusing" not in r.stdout
    assert "b.txt" in r.stdout
    assert (path / "b.txt").read_text() == "two\n"


def test_tree_is_not_reused_when_an_untracked_file_was_added(repo):
    # A stray source or test file changes what a check collects just as surely
    # as an edit does.
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    path = _tree_path(repo)
    (path / "test_stray.py").write_text("raise SystemExit(1)\n")
    r = repo.rt("tree", "--pr", "1")
    assert r.returncode == 0, r.stderr
    assert "reusing" not in r.stdout
    assert not (path / "test_stray.py").exists()


def test_tree_reuse_keeps_ignored_build_artifacts(repo):
    # Deliberate: artifacts are the repo's own declared scratch space, and not
    # rebuilding them on every check is most of what reuse buys.
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    path = _tree_path(repo)
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    exclude = Path(common) / "info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("build/\n")
    (path / "build").mkdir()
    (path / "build" / "artifact").write_text("expensive\n")
    r = repo.rt("tree", "--pr", "1")
    assert r.returncode == 0, r.stderr
    assert "reusing" in r.stdout
    assert (path / "build" / "artifact").is_file()


def test_tree_moves_to_the_new_head_after_a_repin(repo):
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    moved = repo.advance_feature()
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    assert git(_tree_path(repo), "rev-parse", "HEAD") == moved


def test_tree_survives_a_deleted_but_still_registered_directory(repo):
    # A worktree's registration outlives its directory, so `rm -rf` alone leaves
    # the next `worktree add` failing with "missing but already registered".
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    path = _tree_path(repo)
    shutil.rmtree(path)
    assert "pr-1" in git(repo.clone, "worktree", "list")
    r = repo.rt("tree", "--pr", "1")
    assert r.returncode == 0, r.stderr
    assert git(path, "rev-parse", "HEAD") == repo.feature_head


def test_tree_remove_unregisters_it(repo):
    repo.rt("begin", "--pr", "1", check=True)
    repo.rt("tree", "--pr", "1", check=True)
    r = repo.rt("tree", "--pr", "1", "--remove")
    assert r.returncode == 0
    assert not _tree_path(repo).exists()
    assert "pr-1" not in git(repo.clone, "worktree", "list")


def test_tree_without_begin_says_so(repo):
    r = repo.rt("tree", "--pr", "1")
    assert r.returncode != 0
    assert "begin" in r.stderr
