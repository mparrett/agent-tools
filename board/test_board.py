#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "pyyaml"]
# ///
"""Tests for board. Run via `uv run board/test_board.py` (or `just test`).

Each test gets a bare repository as the remote and clones of it, so pushes,
races and force-with-lease run against real git.
"""

import datetime as dt
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "board.py"
_spec = importlib.util.spec_from_file_location("board", SCRIPT)
board = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board)

CONFIG = """\
title: Test board
statuses: [claimed, in-progress, blocked]
ttl: 3d
prune_after: 14d
subject_url: https://example.test/proposals/{subject}.md
subject_check:
  ref: origin/main
  path: docs/proposals/{subject}.md
links:
  pr: https://example.test/pull/{value}
"""


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.test")
    monkeypatch.delenv("BOARD_OWNER", raising=False)
    monkeypatch.delenv("BOARD_SESSION", raising=False)
    # The operator's global git config (init.defaultBranch, commit.gpgsign,
    # core.hooksPath, ...) must not reach the fixture repos: a test here was green on
    # one machine and red on CI for exactly that reason.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")


@pytest.fixture
def repos(tmp_path):
    """A remote with a main branch of proposals and a configured board, and two clones."""
    remote = tmp_path / "remote.git"
    # The seed pushes `main`; the remote's HEAD has to name it or the clones
    # below come up with an unborn HEAD.
    git("init", "-q", "--bare", "--initial-branch=main", str(remote), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(remote), str(seed), cwd=tmp_path)
    git("checkout", "-q", "-b", "main", cwd=seed)
    (seed / "docs/proposals").mkdir(parents=True)
    for subject in ("login-page", "search", "billing"):
        (seed / f"docs/proposals/{subject}.md").write_text(f"# {subject}\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "proposals", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    assert run(seed, "init", "--title", "Test board") == 0
    git("fetch", "-q", "origin", "board", cwd=seed)
    git("checkout", "-q", "-b", "board", "origin/board", cwd=seed)
    (seed / "board.yml").write_text(CONFIG)
    git("commit", "-qam", "config", cwd=seed)
    git("push", "-q", "origin", "board", cwd=seed)
    clones = {}
    for name in ("a", "b"):
        clones[name] = tmp_path / name
        git("clone", "-q", str(remote), str(clones[name]), cwd=tmp_path)
    return seed, clones["a"], clones["b"]


def run(repo, *args, owner=None):
    argv = ["-C", str(repo), *args]
    if owner:
        argv = argv[:2] + [args[0], "--owner", owner, *args[1:]]
    return board.main(argv)


def card(repo, path):
    git("fetch", "-q", "origin", "board", cwd=repo)
    return board.parse_card(git("show", f"origin/board:{path}", cwd=repo).stdout)


def remote_files(repo):
    git("fetch", "-q", "origin", "board", cwd=repo)
    out = git("ls-tree", "-r", "--name-only", "origin/board", cwd=repo).stdout
    return sorted(out.split())


def test_set_writes_card_and_readme(repos):
    _, a, _ = repos
    assert run(a, "set", "login-page", "--status", "claimed", "--link", "pr=12", owner="sam") == 0
    assert remote_files(a) == ["README.md", "board.yml", "cards/login-page--sam.md"]
    written = card(a, "cards/login-page--sam.md")
    assert {k: written[k] for k in ("subject", "owner", "status", "links")} == {
        "subject": "login-page",
        "owner": "sam",
        "status": "claimed",
        "links": {"pr": 12},
    }
    readme = git("show", "origin/board:README.md", cwd=a).stdout
    assert "[#12](https://example.test/pull/12)" in readme


def test_set_never_touches_the_checkout(repos):
    _, a, _ = repos
    head = git("rev-parse", "HEAD", cwd=a).stdout
    assert run(a, "set", "login-page", "--status", "claimed", owner="sam") == 0
    assert git("rev-parse", "HEAD", cwd=a).stdout == head
    assert git("status", "--porcelain", cwd=a).stdout == ""


def test_misspelled_subject_is_refused(repos):
    _, a, _ = repos
    assert run(a, "set", "login-paeg", "--status", "claimed", owner="sam") == 1
    assert run(a, "set", "login-paeg", "--status", "claimed", "--new-subject", owner="sam") == 0


def test_only_a_subject_with_a_proposal_is_linked(repos):
    _, a, _ = repos
    assert run(a, "set", "login-page", "--status", "claimed", owner="sam") == 0
    assert run(a, "set", "dark-mode", "--status", "claimed", "--new-subject", owner="sam") == 0
    readme = git("show", "origin/board:README.md", cwd=a).stdout
    assert "[`login-page`](https://example.test/proposals/login-page.md)" in readme
    assert "<br>`dark-mode` |" in readme
    assert "proposals/dark-mode.md" not in readme


def test_no_links_when_the_checked_ref_is_unreadable(repos):
    _, a, _ = repos
    git("update-ref", "-d", "refs/remotes/origin/main", cwd=a)
    assert run(a, "set", "dark-mode", "--status", "claimed", "--new-subject", owner="sam") == 0
    readme = git("show", "origin/board:README.md", cwd=a).stdout
    assert "example.test/proposals" not in readme


def test_unknown_status_is_refused(repos):
    _, a, _ = repos
    assert run(a, "set", "login-page", "--status", "doing", owner="sam") == 1


def test_update_keeps_fields_and_unknown_keys(repos):
    seed, a, _ = repos
    (seed / "cards").mkdir()
    (seed / "cards/login-page--sam.md").write_text(
        "---\ntitle: Login page\nsubject: login-page\nowner: sam\nstatus: claimed\n"
        "since: 2026-10-01T10:00Z\nupdated: 2026-10-01T10:00Z\n"
        "depends-on: [search]\nlinks:\n  pr: 12\n---\n\nFirst note.\n"
    )
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "hand card", cwd=seed)
    git("push", "-q", "origin", "board", cwd=seed)
    assert run(a, "set", "login-page", "--status", "blocked", owner="sam") == 0
    updated = card(a, "cards/login-page--sam.md")
    assert {k: updated[k] for k in ("title", "status", "since", "depends-on", "links", "note")} == {
        "title": "Login page",
        "status": "blocked",
        "since": "2026-10-01T10:00Z",
        "depends-on": ["search"],
        "links": {"pr": 12},
        "note": "First note.",
    }
    assert run(a, "set", "login-page", "--link", "pr=", owner="sam") == 0
    assert "links" not in card(a, "cards/login-page--sam.md")


def test_second_claim_is_contested(repos, capsys):
    _, a, b = repos
    assert run(a, "set", "login-page", "--status", "claimed", owner="sam") == 0
    time.sleep(1.1)
    assert run(b, "set", "login-page", "--status", "claimed", owner="alex") == 2
    assert "sam also has login-page" in capsys.readouterr().err
    assert run(b, "ls", "login-page") == 3
    readme = git("show", "origin/board:README.md", cwd=b).stdout
    assert "contested, keeps it" in readme and "contested, after sam" in readme


def test_ls_free_subject(repos):
    _, a, _ = repos
    assert run(a, "ls", "login-page") == 0


def test_done_removes_only_your_card(repos):
    _, a, b = repos
    run(a, "set", "login-page", "--status", "claimed", owner="sam")
    run(b, "set", "login-page", "--status", "claimed", owner="alex")
    assert run(a, "done", "login-page", owner="sam") == 0
    assert remote_files(a) == ["README.md", "board.yml", "cards/login-page--alex.md"]
    assert run(a, "done", "login-page", owner="sam") == 1


def test_concurrent_writers_in_one_checkout_all_land(repos):
    _, a, _ = repos
    owners = [f"agent-{i}" for i in range(6)]
    procs = [
        subprocess.Popen(
            [sys.executable, str(SCRIPT), "-C", str(a), "set", "login-page", "--status", "claimed"],
            env={**os.environ, "BOARD_OWNER": owner},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for owner in owners
    ]
    codes = [p.wait(timeout=120) for p in procs]
    assert set(codes) <= {0, 2}
    assert remote_files(a) == sorted(
        ["README.md", "board.yml"] + [f"cards/login-page--{o}.md" for o in owners]
    )
    assert git("for-each-ref", "refs/board-fetch", cwd=a).stdout == ""


def test_render_marks_stale_and_groups_by_status():
    at = dt.datetime(2026, 10, 25, 12, tzinfo=dt.timezone.utc)
    config = {**board.DEFAULT_CONFIG, "statuses": ["claimed", "blocked"]}
    cards = [
        {
            "title": "Fresh",
            "subject": "a",
            "owner": "x",
            "status": "claimed",
            "updated": "2026-10-25T11:00Z",
        },
        {
            "title": "Old",
            "subject": "b",
            "owner": "y",
            "status": "blocked",
            "updated": "2026-10-19T12:00Z",
        },
        {
            "title": "Odd",
            "subject": "c",
            "owner": "z",
            "status": "mystery",
            "updated": "2026-10-25T11:00Z",
        },
    ]
    text = board.render(config, cards, at=at)
    assert "**3 in flight · 1 stale**" in text
    assert "stale 6d" in text
    assert "## mystery (not a board status) (1)" in text
    assert text.index("## claimed") < text.index("## blocked") < text.index("## mystery")


def test_contested_tie_names_no_keeper():
    at = dt.datetime(2026, 10, 25, 12, tzinfo=dt.timezone.utc)
    same = "2026-10-25T11:00:00Z"
    cards = [
        {
            "title": "A",
            "subject": "s",
            "owner": o,
            "status": "claimed",
            "since": same,
            "updated": same,
        }
        for o in ("zed", "amy")
    ]
    cards.append(
        {
            "title": "B",
            "subject": "s",
            "owner": "bo",
            "status": "claimed",
            "updated": "2026-10-25T11:30:00Z",
        }
    )
    text = board.render(board.DEFAULT_CONFIG, cards, at=at)
    assert text.count("contested, tied") == 2
    assert "contested, after amy and zed" in text


def test_prune_removes_cards_past_ttl_and_grace(repos):
    seed, a, _ = repos
    (seed / "cards").mkdir()
    (seed / "cards/billing--alex.md").write_text(
        "---\ntitle: Old\nsubject: billing\nowner: alex\nstatus: claimed\n"
        "updated: 2026-01-01T00:00Z\n---\n"
    )
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "old card", cwd=seed)
    git("push", "-q", "origin", "board", cwd=seed)
    run(a, "set", "login-page", "--status", "claimed", owner="sam")
    assert run(a, "prune") == 0
    assert remote_files(a) == ["README.md", "board.yml", "cards/login-page--sam.md"]


def test_squash_keeps_the_tree_and_refuses_a_moved_board(repos):
    _, a, b = repos
    run(a, "set", "login-page", "--status", "claimed", owner="sam")
    git("fetch", "-q", "origin", "board", cwd=a)
    tree = git("rev-parse", "origin/board^{tree}", cwd=a).stdout
    assert run(a, "squash") == 0
    git("fetch", "-q", "origin", "board", cwd=a)
    assert git("rev-list", "--count", "origin/board", cwd=a).stdout.strip() == "1"
    assert git("rev-parse", "origin/board^{tree}", cwd=a).stdout == tree

    stale = board.Board(repo=str(a)).fetch()
    run(b, "set", "search", "--status", "claimed", owner="alex")
    squashed = git("commit-tree", f"{stale}^{{tree}}", "-m", "x", cwd=a).stdout.strip()
    ok, _ = board.Board(repo=str(a)).push(squashed, lease=stale)
    assert not ok
    assert "cards/search--alex.md" in remote_files(a)


def test_html_page_follows_the_system_theme_with_a_toggle():
    page = board.page("<h1>Board</h1>")
    assert '<link id=light rel=stylesheet href="' in page
    assert 'media="(prefers-color-scheme: light)"' in page
    assert 'media="(prefers-color-scheme: dark)"' in page
    assert "<button id=theme type=button>Theme: Auto</button>" in page
    assert "<main><h1>Board</h1></main>" in page


def test_a_card_with_a_bad_value_never_locks_the_board(repos, capsys):
    seed, a, b = repos
    (seed / "cards").mkdir()
    (seed / "cards/billing--alex.md").write_text(
        "---\ntitle: Billing\nsubject: billing\nowner: alex\nstatus: claimed\n"
        "updated: yesterday\nttl: 3days\n---\n"
    )
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "bad card", cwd=seed)
    git("push", "-q", "origin", "board", cwd=seed)
    assert run(a, "set", "login-page", "--status", "claimed", owner="sam") == 0
    readme = git("show", "origin/board:README.md", cwd=a).stdout
    assert "## Cards that don't parse (1)" in readme and "cards/billing--alex.md" in readme
    assert run(a, "ls") == 0
    assert run(a, "prune") == 0
    assert run(b, "set", "billing", "--owner", "alex", "--note", "x") == 1
    assert run(b, "set", "billing", "--status", "claimed", owner="alex") == 0
    assert card(b, "cards/billing--alex.md")["status"] == "claimed"
    readme = git("show", "origin/board:README.md", cwd=b).stdout
    assert "Cards that don't parse" not in readme


def test_owner_is_required(repos):
    _, a, _ = repos
    assert run(a, "set", "login-page", "--status", "claimed") == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
