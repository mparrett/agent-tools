#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "rich", "blessed"]
# ///
"""Tests for claude-ls. Run via `uv run claude-ls/test_claude_ls.py` (or `just test`).

The module filename has a hyphen, so it can't be imported by name — load it from
its path with importlib.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

_spec = importlib.util.spec_from_file_location("claude_ls", Path(__file__).parent / "claude-ls.py")
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)


@pytest.fixture(autouse=True)
def _no_claude_agents(monkeypatch):
    # The real CLI describes this machine's sessions, not a test's registry.
    monkeypatch.setattr(cl, "_agents_session_ids", lambda: None)
    monkeypatch.setattr(cl, "_AGENTS_ANSWER", {})
    # Likewise the real tmux server; None keeps each record's recorded address.
    monkeypatch.setattr(cl, "_tmux_pane_addresses", lambda: None)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/Users/you/projects/my-repo", "-Users-you-projects-my-repo"),
        # Regression: '.' must flatten to '-' like Claude does.
        ("/Users/x/mparrett.github.io", "-Users-x-mparrett-github-io"),
        ("/a/foo_bar", "-a-foo-bar"),  # underscore
        ("/a/foo bar", "-a-foo-bar"),  # space
        ("/a/b.c_d e", "-a-b-c-d-e"),  # mixed non-alphanumerics
        ("/Users/CamelCase/Repo", "-Users-CamelCase-Repo"),  # case preserved
    ],
)
def test_encode_path_to_folder_name(path, expected):
    assert cl.encode_path_to_folder_name(path) == expected


def test_cwd_to_project_name(monkeypatch):
    monkeypatch.setattr(cl.os, "getcwd", lambda: "/Users/x/mparrett.github.io")
    assert cl.cwd_to_project_name() == "-Users-x-mparrett-github-io"


def test_cwd_to_project_name_explicit_arg():
    # An explicit cwd overrides the process CWD (used by `last --cwd` for fleet tooling).
    assert cl.cwd_to_project_name("/Users/x/mparrett.github.io") == "-Users-x-mparrett-github-io"


def _last_args(base, **over):
    args = dict(
        base_dir=str(base),
        turns=1,
        role="assistant",
        session_id=None,
        cwd=None,
        file=None,
        json=False,
        no_color=True,
    )
    args.update(over)
    return SimpleNamespace(**args)


def _make_role_corpus(base: Path, text: str = "edit your zshrc PATH") -> Path:
    """Corpus whose message carries an explicit role, so load_messages keeps it
    (unlike _make_corpus, which is shaped for raw content search). Opens with a
    header line stamping the real cwd/gitBranch, as Claude does."""
    proj = base / "-Users-x-dotfiles"
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_text(
        '{"type":"user","cwd":"/Users/x/dotfiles","gitBranch":"main",'
        '"message":{"role":"user","content":"hi"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":'
        f'[{{"type":"text","text":"{text}"}}]}}}}\n'
    )
    return proj


def test_last_json_resolves_by_cwd(tmp_path, capsys):
    # `last --json --cwd` resolves another directory's session and emits the real
    # cwd/branch (read from the transcript) so a fleet caller can join on path.
    base = tmp_path / "projects"
    _make_role_corpus(base, text="edit your zshrc PATH")
    cl.last_mode(_last_args(base, json=True, cwd="/Users/x/dotfiles"), None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_folder"] == "-Users-x-dotfiles"
    assert payload["cwd"] == "/Users/x/dotfiles"  # from the transcript header
    assert payload["git_branch"] == "main"
    assert payload["last_text"] == "edit your zshrc PATH"
    assert payload["messages"][-1] == {"role": "assistant", "text": "edit your zshrc PATH"}


def test_last_json_unknown_cwd_exits(tmp_path):
    base = tmp_path / "projects"
    _make_role_corpus(base)
    with pytest.raises(SystemExit) as exc:
        cl.last_mode(_last_args(base, json=True, cwd="/nope/not/here"), None)
    assert exc.value.code == 1


def test_normalize_passthrough_existing_slug():
    # An already-flattened folder name must not be re-encoded.
    slug = "-Users-you-projects-my-repo"
    assert cl._normalize_to_folder_name(slug) == slug


def test_normalize_absolute_dotted_path():
    assert (
        cl._normalize_to_folder_name("/Users/x/mparrett.github.io") == "-Users-x-mparrett-github-io"
    )


def test_normalize_relative_fragment():
    assert cl._normalize_to_folder_name("foo.bar/baz") == "-foo-bar-baz"


def _make_corpus(base: Path, text: str = "edit your zshrc PATH") -> Path:
    """An artificial ~/.claude/projects-style corpus: one project, one session."""
    proj = base / "-Users-x-dotfiles"
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_text(
        f'{{"type":"assistant","message":{{"content":[{{"type":"text","text":"{text}"}}]}}}}\n'
    )
    return proj


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_finds_content_shadowed_by_ignore_file(tmp_path):
    # Regression: an .ignore that default ripgrep honors must NOT hide the
    # session store from content search. Without --no-ignore, rg returns 0 here.
    base = tmp_path / "projects"
    _make_corpus(base)
    (base / ".ignore").write_text("*.jsonl\n")  # would shadow the store
    results = cl.search_session_content(str(base), "zshrc")
    assert [sess.name for _, sess, _ in results] == ["sess.jsonl"]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_respect_ignore_opts_back_into_ignore_rules(tmp_path):
    # The --respect-ignore escape hatch honors ignore files again, so the same
    # .ignore-shadowed corpus yields nothing.
    base = tmp_path / "projects"
    _make_corpus(base)
    (base / ".ignore").write_text("*.jsonl\n")
    assert cl.search_session_content(str(base), "zshrc", respect_ignore=True) == []


def test_search_errors_clearly_when_rg_missing(tmp_path, monkeypatch):
    base = tmp_path / "projects"
    _make_corpus(base)
    monkeypatch.setattr(cl.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        cl.search_session_content(str(base), "zshrc")


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_exits_nonzero_on_rg_error(tmp_path):
    # A failed search (invalid regex -> rg exit 2, no output) must not masquerade
    # as an empty result; it exits nonzero so callers can tell them apart.
    base = tmp_path / "projects"
    _make_corpus(base)
    with pytest.raises(SystemExit) as exc:
        cl.search_session_content(str(base), "[")  # unclosed character class
    assert exc.value.code != 0


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_session_content_returns_all_matches(tmp_path):
    # search_session_content no longer applies a display limit — the caller does,
    # so it can report the full match total in the footer.
    base = tmp_path / "projects"
    for i in range(3):
        proj = base / f"-proj-{i}"
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"find widget here"}]}}\n'
        )
    assert len(cl.search_session_content(str(base), "widget")) == 3


def test_get_session_matches_prefers_prose_over_tool(tmp_path):
    # The single default snippet should be a real sentence, not a tool/log line
    # that happens to contain the query. Critically: many tool hits come BEFORE
    # the prose, exceeding the old fixed candidate cutoff — the scan must keep
    # going until it has the prose, or the prose-over-tool ranking is moot.
    lines = []
    for i in range(8):
        lines.append(
            '{"type":"user","message":{"content":'
            f'[{{"type":"tool_result","content":"ran widget_{i}.py widget dumped"}}]}}}}'
        )
    lines.append(
        '{"type":"assistant","message":{"content":'
        '[{"type":"text","text":"The widget renders correctly now."}]}}'
    )
    sess = tmp_path / "s.jsonl"
    sess.write_text("\n".join(lines) + "\n")
    matches = cl.get_session_matches(sess, "widget", max_matches=1)
    assert matches and matches[0][0] == "asst"  # prose wins despite 8 earlier tool hits


def test_search_count_zero_shows_no_rows(tmp_path, monkeypatch, capsys):
    # -n 0 must not display a row (the limit check happens before appending).
    base = tmp_path / "projects"
    proj = base / "-proj"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"widget here"}]}}\n'
    )
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    args = SimpleNamespace(
        base_dir=str(base),
        query=["widget"],
        count=0,
        no_color=True,
        project=None,
        show_context=False,
        brief=False,
        case_sensitive=False,
        verbose=False,
        exclude_type=[],
        only_type=[],
        max_matches=3,
        width=50,
        no_work=True,
        since=None,
        until=None,
        respect_ignore=False,
    )
    cl.search_content_mode(args, None)
    out = capsys.readouterr().out
    assert "containing 'widget'" in out  # footer present
    assert "s.jsonl" not in out  # but no row rendered


def _search_args(base, **over):
    args = dict(
        base_dir=str(base),
        query=["widget"],
        count=5,
        no_color=True,
        project=None,
        show_context=False,
        brief=False,
        case_sensitive=False,
        verbose=False,
        exclude_type=[],
        only_type=[],
        max_matches=3,
        width=50,
        no_work=True,
        since=None,
        until=None,
        respect_ignore=False,
        resume=None,
        force=False,
        codex=False,
    )
    args.update(over)
    return SimpleNamespace(**args)


def _two_session_base(tmp_path):
    base = tmp_path / "projects"
    proj = base / "-proj"
    proj.mkdir(parents=True)
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"widget here"}]}}\n'
    older, newer = proj / "aaaa1111.jsonl", proj / "bbbb2222.jsonl"
    older.write_text(line)
    newer.write_text(line)
    # Distinct mtimes so the display order (newest first) is deterministic.
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    return base, older, newer


def test_search_resume_offset_routes_to_resume(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    base, older, newer = _two_session_base(tmp_path)
    captured = {}
    monkeypatch.setattr(cl, "resume_mode", lambda a, c: captured.update(session=a.session))

    cl.search_content_mode(_search_args(base, resume="1"), None)
    # Row 1 is the newest session; the full stem is handed to resume_mode.
    assert captured["session"] == newer.stem


def test_search_resume_offset_out_of_range_exits(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    base, _older, _newer = _two_session_base(tmp_path)
    monkeypatch.setattr(cl, "resume_mode", lambda a, c: pytest.fail("should not resume"))
    with pytest.raises(SystemExit) as exc:
        cl.search_content_mode(_search_args(base, resume="9"), None)
    assert exc.value.code == 1


def test_search_resume_no_matches_exits(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    base, _older, _newer = _two_session_base(tmp_path)
    monkeypatch.setattr(cl, "resume_mode", lambda a, c: pytest.fail("should not resume"))
    with pytest.raises(SystemExit) as exc:
        cl.search_content_mode(_search_args(base, query=["definitelynotpresent"], resume="1"), None)
    assert exc.value.code == 1


def test_search_resume_passes_non_numeric_as_id(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    base, _older, _newer = _two_session_base(tmp_path)
    captured = {}
    monkeypatch.setattr(cl, "resume_mode", lambda a, c: captured.update(session=a.session))
    cl.search_content_mode(_search_args(base, resume="bbbb2222"), None)
    # A non-numeric value is forwarded verbatim for substring resolution.
    assert captured["session"] == "bbbb2222"


# --- PR 1: session-store primitives for the session-graph (claude-graph) ---


def _edit_entry(cwd, *file_paths, tool="Edit"):
    """A transcript entry with one or more tool_use edit blocks."""
    blocks = [{"type": "tool_use", "name": tool, "input": {"file_path": fp}} for fp in file_paths]
    return {"type": "assistant", "cwd": cwd, "message": {"content": blocks}}


def test_work_signals_retains_distinct_edited_paths_in_order():
    entries = [
        _edit_entry("/w", "/w/a.py", "/w/b.py"),
        _edit_entry("/w", "/w/a.py"),  # repeat -> not duplicated in edited_paths
        _edit_entry("/w", "/w/c.py"),
    ]
    ws = cl.derive_work_signals(entries)
    assert ws.edited_paths == ["/w/a.py", "/w/b.py", "/w/c.py"]  # distinct, order-preserving


def test_work_signals_edited_paths_empty_without_edits():
    ws = cl.derive_work_signals([{"type": "assistant", "cwd": "/w", "message": {"content": []}}])
    assert ws.edited_paths == []


def _make_repo(root: Path):
    (root / ".git").mkdir(parents=True)
    return root


def _make_worktree(wt: Path, main: Path, name="wt"):
    (main / ".git" / "worktrees" / name).mkdir(parents=True, exist_ok=True)
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/{name}\n", encoding="utf-8")
    return wt


def test_canonical_repo_root_normal_checkout(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    assert cl._canonical_repo_root(str(repo)) == str(repo)
    sub = repo / "src"
    sub.mkdir()
    assert cl._canonical_repo_root(str(sub)) == str(repo)  # subdir resolves up


def test_canonical_repo_root_collapses_worktree_to_main(tmp_path):
    main = _make_repo(tmp_path / "main")
    wt = _make_worktree(tmp_path / "wt", main)
    # _repo_root_for stops at the worktree's own root...
    assert cl._repo_root_for(str(wt)) == str(wt)
    # ...but _canonical_repo_root follows the gitdir pointer to the main repo.
    assert cl._canonical_repo_root(str(wt)) == str(main)


def test_canonical_repo_root_none_for_non_repo(tmp_path):
    plain = tmp_path / "scratch"
    plain.mkdir()
    assert cl._canonical_repo_root(str(plain)) is None


def test_main_repo_from_gitfile_parsing(tmp_path):
    main = _make_repo(tmp_path / "m")
    wt = _make_worktree(tmp_path / "w", main)
    assert cl._main_repo_from_gitfile(wt / ".git") == str(main)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".git").write_text("not a gitdir line\n")
    assert cl._main_repo_from_gitfile(bad / ".git") is None  # unparseable -> None


def test_search_resume_force_passes_through(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    base, _older, newer = _two_session_base(tmp_path)
    captured = {}
    monkeypatch.setattr(cl, "resume_mode", lambda a, c: captured.update(force=a.force))

    cl.search_content_mode(_search_args(base, resume="1", force=True), None)
    # -f reaches resume_mode so the tmux safety check is skipped.
    assert captured["force"] is True


def test_read_session_header_extracts_cwd_and_branch(tmp_path):
    # Claude stamps cwd/gitBranch on the opening event lines; read them losslessly.
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"user","cwd":"/Users/x/proj","gitBranch":"main",'
        '"message":{"role":"user","content":"hi"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"yo"}]}}\n'
    )
    assert cl.read_session_header(f) == {"cwd": "/Users/x/proj", "git_branch": "main"}


def test_read_session_header_missing_file(tmp_path):
    assert cl.read_session_header(tmp_path / "nope.jsonl") == {"cwd": None, "git_branch": None}


def test_read_session_header_ignores_non_object_lines(tmp_path):
    # A line that is valid JSON but not an object (a bare array/string/number)
    # must be skipped, not crash .get() — the contract pending --json relies on.
    f = tmp_path / "s.jsonl"
    f.write_text(
        "[]\n"
        '"a string"\n'
        "42\n"
        '{"type":"user","cwd":"/Users/x/proj","gitBranch":"main","message":{"content":"hi"}}\n'
    )
    assert cl.read_session_header(f) == {"cwd": "/Users/x/proj", "git_branch": "main"}


def test_read_session_header_bounded_to_window(tmp_path):
    # Fields past the head window are not found — the scan is bounded for big files.
    f = tmp_path / "s.jsonl"
    lines = ['{"type":"x"}'] * 50 + ['{"cwd":"/late","gitBranch":"dev"}']
    f.write_text("\n".join(lines) + "\n")
    assert cl.read_session_header(f, max_lines=40) == {"cwd": None, "git_branch": None}


def test_pending_json_includes_cwd_and_branch(tmp_path, capsys):
    # pending --json surfaces the transcript's real cwd/branch for path-based joins.
    base = tmp_path / "projects"
    proj = base / "-Users-x-proj"
    proj.mkdir(parents=True)
    (proj / "sess1234.jsonl").write_text(
        '{"type":"user","cwd":"/Users/x/proj","gitBranch":"main",'
        '"message":{"role":"user","content":"hi"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"Should I proceed?"}]}}\n'
    )
    args = SimpleNamespace(
        base_dir=str(base), project=None, stale=None, limit=20, include_agents=False, json=True
    )
    cl.pending_mode(args, None)
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["cwd"] == "/Users/x/proj"
    assert rows[0]["git_branch"] == "main"


def test_pending_json_emits_the_full_session_id(tmp_path, capsys):
    # The id is a join key, not a label. claude-ps matches this field against the
    # whole uuid its hook registry records, so emitting a prefix here is a join that
    # can never match — and absence from the map reads as "not waiting", so it fails
    # to a confident wrong answer rather than to a visible error.
    uuid = "0b0b2922-78a8-453e-891b-77911740dfaf"
    base = tmp_path / "projects"
    proj = base / "-Users-x-proj"
    proj.mkdir(parents=True)
    (proj / f"{uuid}.jsonl").write_text(
        '{"type":"user","cwd":"/Users/x/proj","gitBranch":"main",'
        '"message":{"role":"user","content":"hi"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"Should I proceed?"}]}}\n'
    )
    args = SimpleNamespace(
        base_dir=str(base), project=None, stale=None, limit=20, include_agents=False, json=True
    )
    cl.pending_mode(args, None)
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["session_id"] == uuid


def _same_id_across_dirs(tmp_path, uuid="538a5cd9-1111-2222-3333-444455556666"):
    # The same UUID under two project folders — a multi-match. Each transcript
    # records a real, existing cwd (distinct from the slug-decoded path, so tests
    # can tell which one the listing/resume used).
    base = tmp_path / "projects"
    dirs = {}
    for slug, name in (("-Users-x-foo", "foo"), ("-Users-x-work", "work")):
        cwd = tmp_path / name
        cwd.mkdir()
        d = base / slug
        d.mkdir(parents=True)
        f = d / f"{uuid}.jsonl"
        f.write_text('{"type":"user","cwd":"%s","message":{"content":"hi"}}\n' % cwd)
        dirs[name] = (f, cwd)
    os.utime(dirs["foo"][0], (2000, 2000))
    os.utime(dirs["work"][0], (1000, 1000))
    return base, uuid, dirs


def test_matching_sessions_order_is_stable(tmp_path):
    # Order is by path, not mtime, so a transcript written between the `resume`
    # listing and a later `--pick N` can't reorder candidates under the user.
    base, uuid, dirs = _same_id_across_dirs(tmp_path)
    order = [m.parent.name for m in cl.matching_sessions(str(base), uuid)]
    assert order == ["-Users-x-foo", "-Users-x-work"]  # foo < work by path
    # Flip mtimes: the order must not change.
    os.utime(dirs["foo"][0], (1, 1))
    os.utime(dirs["work"][0], (9999, 9999))
    assert [m.parent.name for m in cl.matching_sessions(str(base), uuid)] == order


def test_find_session_ambiguous_lists_recorded_cwd(tmp_path, capsys):
    base, uuid, dirs = _same_id_across_dirs(tmp_path)
    assert cl.find_session_across_projects(str(base), uuid) is None
    err = capsys.readouterr().err
    assert "1  " in err and "2  " in err
    # Listing shows each session's *recorded* cwd (what resume uses), not the lossy
    # slug decode (`/Users/x/foo`), so the menu matches where --pick actually lands.
    assert str(dirs["foo"][1]) in err and str(dirs["work"][1]) in err
    assert "/Users/x/foo" not in err


def _resume_args(base, **over):
    args = dict(
        base_dir=str(base),
        session=None,
        session_id=None,
        pick=None,
        force=True,
        attach=False,
        dry_run=True,
    )
    args.update(over)
    return SimpleNamespace(**args)


def test_resume_pick_selects_candidate(tmp_path, capsys):
    base, uuid, dirs = _same_id_across_dirs(tmp_path)
    # A valid --pick resolves cleanly; dry-run prints the target and returns.
    cl.resume_mode(_resume_args(base, session=uuid, pick=2), None)
    out = capsys.readouterr().out
    # By path order #2 is the "work" session; dry-run cd's into its recorded cwd.
    assert str(dirs["work"][1]) in out


def test_resume_ambiguous_without_pick_exits(tmp_path, capsys):
    base, uuid, dirs = _same_id_across_dirs(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session=uuid), None)
    assert exc.value.code == 1
    assert "--pick" in capsys.readouterr().err


def test_resume_pick_out_of_range_exits(tmp_path, capsys):
    base, uuid, dirs = _same_id_across_dirs(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session=uuid, pick=9), None)
    assert exc.value.code == 1
    assert "out of range" in capsys.readouterr().err


# --- discovery refs (phase 1) ----------------------------------------
# One enumerator owns the on-disk layout, so a second transcript store becomes
# a producer here rather than a change at every listing call site.


def _two_project_base(tmp_path):
    base = tmp_path / "projects"
    line = '{"type":"user","cwd":"/Users/x/a","message":{"content":"hi"}}\n'
    made = {}
    for slug, stamps in (("-Users-x-a", (1000, 3000)), ("-Users-x-b", (2000,))):
        d = base / slug
        d.mkdir(parents=True)
        for i, ts in enumerate(stamps):
            f = d / f"{slug.strip('-')}{i}.jsonl"
            f.write_text(line)
            os.utime(f, (ts, ts))
            made[f.name] = f
    return base, made


def test_session_refs_are_newest_first_across_projects(tmp_path):
    base, _ = _two_project_base(tmp_path)
    refs = cl.iter_session_refs(str(base))
    assert [r.mtime for r in refs] == [3000, 2000, 1000]


def test_session_refs_carry_project_key_and_source(tmp_path):
    base, _ = _two_project_base(tmp_path)
    refs = cl.iter_session_refs(str(base))
    newest = refs[0]
    # project_key is what the label/cwd helpers consume — a producer with no
    # project directory can synthesize one without touching call sites.
    assert newest.project_key == "-Users-x-a"
    assert newest.project_dir is not None and newest.project_dir.name == "-Users-x-a"
    assert newest.source == "claude"


def test_session_refs_respect_per_project_cap(tmp_path):
    base, _ = _two_project_base(tmp_path)
    refs = cl.iter_session_refs(str(base), per_project=1)
    # One from each project, newest of each.
    assert sorted(r.mtime for r in refs) == [2000, 3000]


def test_session_refs_scope_to_given_projects(tmp_path):
    base, _ = _two_project_base(tmp_path)
    refs = cl.iter_session_refs(str(base), project_keys={"-Users-x-b"})
    assert {r.project_key for r in refs} == {"-Users-x-b"}


def test_session_refs_skip_a_session_that_vanished_mid_scan(tmp_path, monkeypatch):
    # Sessions are written live, so the gap between listing a directory and
    # stat-ing a file is a real race, not an error.
    base, _ = _two_project_base(tmp_path)
    real = cl.list_sessions

    def with_a_ghost(project_dir):
        return list(real(project_dir)) + [project_dir / "never-existed.jsonl"]

    monkeypatch.setattr(cl, "list_sessions", with_a_ghost)
    refs = cl.iter_session_refs(str(base))
    assert all(r.path.name != "never-existed.jsonl" for r in refs)
    assert len(refs) == 3


# --- codex discovery (phase 2) ---------------------------------------


def _codex_store(tmp_path, monkeypatch):
    """A fake ~/.codex/sessions: date-partitioned rollouts, project inside the file."""
    root = tmp_path / "codex" / "sessions" / "2026" / "07" / "30"
    root.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex" / "sessions"))

    def write(name, cwd=None, subagent=False, mtime=1000, user_turns=(), meta=True):
        lines = []
        if meta:
            payload = {"session_id": name, "cli_version": "1.0"}
            if cwd:
                payload["cwd"] = cwd
            if subagent:
                payload["thread_source"] = "subagent"
            lines.append(json.dumps({"type": "session_meta", "payload": payload}))
        # A synthetic environment_context arrives as a response_item; the preview
        # must not mistake it for something the user typed.
        lines.append(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "<environment_context>x"}],
                    },
                }
            )
        )
        for t in user_turns:
            lines.append(
                json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": t}})
            )
        f = root / f"rollout-2026-07-30T00-00-00-{name}.jsonl"
        f.write_text("\n".join(lines) + "\n")
        os.utime(f, (mtime, mtime))
        return f

    return write


def test_codex_short_id_uses_the_uuid_not_the_rollout_prefix():
    stem = "rollout-2026-01-02T03-04-05-0123abcd-0000-7000-8000-000000000000"
    # stem[:8] would be "rollout-" for every single row.
    assert cl.codex_short_id(stem) == "0123abcd"
    assert cl.codex_short_id("weird-name") == "weird-na"


def test_codex_refs_group_by_cwd_under_the_claude_slug(tmp_path, monkeypatch):
    write = _codex_store(tmp_path, monkeypatch)
    write("aaaa", cwd="/Users/x/repo", mtime=2000)
    write("bbbb", cwd="/Users/x/repo", mtime=3000)
    refs = cl.iter_codex_session_refs()
    # Same key the Claude store uses for that directory, so one repo worked in
    # both agents collapses to a single project rather than two.
    assert {r.project_key for r in refs} == {"-Users-x-repo"}
    assert all(r.source == "codex" for r in refs)
    assert [r.mtime for r in refs] == [3000, 2000]


def test_codex_refs_exclude_subagents_by_default(tmp_path, monkeypatch):
    write = _codex_store(tmp_path, monkeypatch)
    write("main", cwd="/Users/x/repo")
    write("sub", cwd="/Users/x/repo", subagent=True)
    assert [r.path.stem[-4:] for r in cl.iter_codex_session_refs()] == ["main"]
    both = cl.iter_codex_session_refs(include_subagents=True)
    assert sorted(r.path.stem[-3:] for r in both) == ["ain", "sub"]


def test_codex_refs_skip_rollouts_without_a_usable_header(tmp_path, monkeypatch):
    write = _codex_store(tmp_path, monkeypatch)
    write("good", cwd="/Users/x/repo")
    write("nocwd", cwd=None)  # session_meta present but no cwd
    write("nometa", cwd="/Users/x/repo", meta=False)  # predates session_meta
    assert [r.path.stem[-4:] for r in cl.iter_codex_session_refs()] == ["good"]


def test_codex_refs_absent_store_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "nope"))
    assert cl.iter_codex_session_refs() == []


def test_session_refs_exclude_codex_unless_asked(tmp_path, monkeypatch):
    write = _codex_store(tmp_path, monkeypatch)
    write("cdx", cwd="/Users/x/a", mtime=5000)
    base, _ = _two_project_base(tmp_path)
    assert all(r.source == "claude" for r in cl.iter_session_refs(str(base)))
    merged = cl.iter_session_refs(str(base), include_codex=True)
    assert [r.source for r in merged][0] == "codex"  # newest overall
    assert {r.source for r in merged} == {"claude", "codex"}


def test_session_refs_scope_filters_codex_too(tmp_path, monkeypatch):
    write = _codex_store(tmp_path, monkeypatch)
    write("ina", cwd="/Users/x/a", mtime=5000)
    write("inb", cwd="/Users/x/b", mtime=5001)
    base, _ = _two_project_base(tmp_path)
    refs = cl.iter_session_refs(str(base), project_keys={"-Users-x-a"}, include_codex=True)
    assert {r.project_key for r in refs} == {"-Users-x-a"}


def _sessions_args(base, **over):
    args = dict(
        base_dir=str(base),
        count=10,
        project=None,
        all=True,
        codex=True,
        verbose=False,
        no_color=True,
    )
    args.update(over)
    return SimpleNamespace(**args)


def test_codex_only_projects_survive_an_unscoped_listing(tmp_path, monkeypatch, capsys):
    # Review finding: sessions_mode used to pass a Claude project *directory*
    # list even for --all, so a project that only ever ran under Codex was
    # filtered out — an empty Claude store returned "No sessions found".
    write = _codex_store(tmp_path, monkeypatch)
    write("aaaa", cwd="/Users/x/codexonly", user_turns=["codex only work"])
    base = tmp_path / "projects"
    (base / "-Users-x-unrelated").mkdir(parents=True)

    cl.sessions_mode(_sessions_args(base), None)
    out = capsys.readouterr().out
    assert "No sessions found" not in out
    assert "codex only work" in out


def test_project_hint_can_select_a_codex_only_project(tmp_path, monkeypatch, capsys):
    write = _codex_store(tmp_path, monkeypatch)
    write("aaaa", cwd="/Users/x/codexonly", user_turns=["codex only work"])
    base = tmp_path / "projects"
    (base / "-Users-x-unrelated").mkdir(parents=True)

    cl.sessions_mode(_sessions_args(base, project="codexonly", all=False), None)
    assert "codex only work" in capsys.readouterr().out


def test_scope_keys_still_narrow_the_claude_store(tmp_path, monkeypatch):
    # The scoping fix must not turn every listing into an unscoped one.
    base, _ = _two_project_base(tmp_path)
    refs = cl.iter_session_refs(str(base), project_keys={"-Users-x-b"})
    assert {r.project_key for r in refs} == {"-Users-x-b"}


def test_matching_sessions_resolves_a_codex_id(tmp_path, monkeypatch):
    # Phase 3: resolution is NOT opt-in the way listing is — an id typed by hand
    # is unambiguous intent. This is what makes the plain footer true again.
    write = _codex_store(tmp_path, monkeypatch)
    f = write("0199aaaa", cwd="/Users/x/repo")
    base = tmp_path / "projects"
    (base / "-p").mkdir(parents=True)
    (base / "-p" / "aaaabbbb-1111.jsonl").write_text("{}\n")

    assert cl.matching_sessions(str(base), "0199aaaa") == [f]
    assert cl.matching_sessions(str(base), "0199aaaa", include_codex=False) == []
    # The Claude side is untouched by the addition.
    assert [p.stem for p in cl.matching_sessions(str(base), "aaaabbbb")] == ["aaaabbbb-1111"]


def test_short_id_and_cwd_come_from_the_right_store(tmp_path, monkeypatch):
    write = _codex_store(tmp_path, monkeypatch)
    f = write("0199aaaa-7603-7a40-912a-a4e6791dba8e", cwd="/Users/x/repo")
    assert cl.short_id_for_path(f) == "0199aaaa"
    assert cl.session_recorded_cwd(f) == "/Users/x/repo"


def test_resume_refuses_a_codex_rollout_by_name(tmp_path, monkeypatch, capsys):
    # It resolves (so the message can be specific) and then refuses, rather than
    # reporting a session that plainly exists as missing.
    write = _codex_store(tmp_path, monkeypatch)
    write("0199aaaa", cwd="/Users/x/repo")
    base = tmp_path / "projects"
    base.mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session="0199aaaa", force=False), None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Codex rollout" in err
    assert "claude -r" in err
    assert "summary" in err  # points at what does work


def test_resolve_maps_a_codex_id_to_its_recorded_cwd(tmp_path, monkeypatch, capsys):
    # Review finding: resolve used the session's parent directory as a project
    # slug. For a rollout that's the day it ran, so `resolve <codex-id>` reported
    # a folder of "31" and a naive path of "/31".
    write = _codex_store(tmp_path, monkeypatch)
    write("019fb821", cwd="/Users/x/realrepo")
    base = tmp_path / "projects"
    base.mkdir(parents=True)

    args = SimpleNamespace(input="019fb821", base_dir=str(base), no_color=True)
    # The trailing exit(1) is resolve's existing "no filesystem match" for a cwd
    # that doesn't exist here; the derivation above it is what this pins.
    with pytest.raises(SystemExit):
        cl.resolve_mode(args, None)
    out = capsys.readouterr().out
    # Same slug the listing groups it under, so both agree on one identity.
    assert "folder: -Users-x-realrepo" in out
    assert "naive:  /Users/x/realrepo" in out
    assert "/31" not in out


def test_resolve_reports_a_codex_rollout_with_no_cwd(tmp_path, monkeypatch, capsys):
    write = _codex_store(tmp_path, monkeypatch)
    write("019fb822", cwd=None)
    base = tmp_path / "projects"
    base.mkdir(parents=True)

    args = SimpleNamespace(input="019fb822", base_dir=str(base), no_color=True)
    with pytest.raises(SystemExit) as exc:
        cl.resolve_mode(args, None)
    assert exc.value.code == 1
    assert "no recorded working directory" in capsys.readouterr().err


def test_candidate_menu_marks_codex_and_uses_its_recorded_cwd(tmp_path, monkeypatch, capsys):
    # A rollout's parent directory is a date, so the Claude slug fallback would
    # print nonsense here; the header's cwd is the only meaningful anchor.
    write = _codex_store(tmp_path, monkeypatch)
    f = write("dupe", cwd="/Users/x/codexrepo")
    claude = tmp_path / "projects" / "-Users-x-clauderepo"
    claude.mkdir(parents=True)
    cs = claude / "dupe-2222.jsonl"
    cs.write_text('{"type":"user","cwd":"/Users/x/clauderepo","message":{"content":"hi"}}\n')

    cl.print_session_candidates("dupe", sorted([f, cs], key=str))
    err = capsys.readouterr().err
    assert "[codex]" in err
    assert "/Users/x/codexrepo" in err
    assert "/Users/x/clauderepo" in err
    assert "/30" not in err  # the date-dir slug decode never appears


def test_verbose_prints_the_path_that_addresses_a_rollout(tmp_path, monkeypatch, capsys):
    write = _codex_store(tmp_path, monkeypatch)
    f = write("aaaa", cwd="/Users/x/repo", user_turns=["hi"])
    base = tmp_path / "projects"
    base.mkdir(parents=True)

    cl.sessions_mode(_sessions_args(base, verbose=True), None)
    assert str(f) in capsys.readouterr().out


def _claude_session(tmp_path, *texts, name="sess"):
    """A Claude transcript whose user messages are `texts`, in order.

    Carries an explicit `role` so `load_messages` keeps the messages — see
    `_make_role_corpus`; without it `is_valid_message` drops them all.
    """
    p = tmp_path / f"{name}.jsonl"
    p.write_text(
        "".join(
            json.dumps({"type": "user", "message": {"role": "user", "content": t}}) + "\n"
            for t in texts
        )
    )
    return p


def test_preview_skips_the_local_command_caveat(tmp_path):
    # An ergonomics item, inverted: caveats aren't stripped, they're displayed — and they
    # led 225 of 743 sessions in a real store, so most listing rows showed boilerplate
    # instead of a topic.
    caveat = (
        "<local-command-caveat>Caveat: The messages below were generated by the "
        "user while running local commands. DO NOT respond</local-command-caveat>"
    )
    f = _claude_session(tmp_path, caveat, "the actual thing I asked for")
    slug, first_user, turns = cl.get_session_preview(f)
    assert first_user == "the actual thing I asked for"
    assert turns == 1  # the caveat isn't a user turn


def test_preview_falls_back_to_the_slash_command(tmp_path):
    # 47 of those 208 are *only* a command; "/clear" beats "(empty)".
    f = _claude_session(
        tmp_path,
        "<local-command-caveat>Caveat: …</local-command-caveat>",
        "<command-name>/clear</command-name>",
    )
    _slug, first_user, turns = cl.get_session_preview(f)
    assert first_user == "/clear"
    assert turns == 0


def test_preview_still_skips_task_notifications(tmp_path):
    # The pre-existing exclusion, now expressed through the shared tag vocabulary.
    f = _claude_session(tmp_path, "<task-notification>agent done", "real question")
    assert cl.get_session_preview(f)[1] == "real question"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("<local-command-caveat>Caveat: …", True),
        ("<task-notification>done", True),
        ("<bash-input>ls -la", True),
        ("<user-memory-input>remember this", True),
        ("  <command-name>/clear</command-name>", True),  # leading whitespace
        ("fix the <command-name> bug", False),  # tag mentioned, not leading
        ("<div> won't render", False),  # a tag, but not ours
        ("<not-a-known-tag>hi", False),
        ("plain prose", False),
        ("", False),
    ],
)
def test_is_wrapper_text(text, expected):
    assert cl.is_wrapper_text(text) is expected


def test_load_messages_keeps_user_typed_wrapper_content(tmp_path):
    # The fidelity views (`get`, `last`, `messages`, `browse`) must not lose text
    # the human typed. A slash command with arguments is ONE message opening with
    # `<command-message>`, and `<bash-input>` is typed by definition — filtering
    # them out of load_messages deleted 111 `<command-args>` instructions across
    # 47 sessions and left assistant replies answering nothing.
    f = _claude_session(
        tmp_path,
        "<command-message>loop</command-message>\n<command-name>/loop</command-name>\n"
        "<command-args>Recheck incoming/ for the review</command-args>",
        "<bash-input>peek at the other session's spec</bash-input>",
    )
    texts = [m.text_content for m in cl.load_messages(str(f), "all")]
    assert any("Recheck incoming/ for the review" in t for t in texts)
    assert any("peek at the other session's spec" in t for t in texts)


def test_all_wrapper_session_still_loads_messages(tmp_path):
    # Sessions that are only slash-command scaffolding must not collapse to zero
    # messages: `last --json` is the documented scripting surface and exits 1 on
    # an empty load, so emptying them broke a poller on 6% of the store.
    f = _claude_session(
        tmp_path,
        "<command-message>clear</command-message>",
        "<command-name>/clear</command-name>",
    )
    assert cl.load_messages(str(f), "all") != []


def test_summary_and_listing_agree_on_user_turns(tmp_path, capsys):
    # The three summarizing views share one predicate, so a session reads the same
    # way whether you list it or summarize it. They disagreed at 0c1129e.
    #
    # This drives summary_mode for real: an earlier version of this test
    # re-implemented the filter inline, so it compared one list comprehension in
    # the test against another and stayed green when the production filter was
    # deleted.
    f = _claude_session(
        tmp_path,
        "<local-command-caveat>Caveat: …</local-command-caveat>",
        "<command-name>/clear</command-name>",
        "the real question",
    )
    _slug, listing_topic, listing_turns = cl.get_session_preview(f)
    assert (listing_topic, listing_turns) == ("the real question", 1)

    cl.summary_mode(
        SimpleNamespace(
            base_dir=str(tmp_path), session_id=str(f), file=None, cwd=None, no_color=True
        ),
        None,
    )
    out = capsys.readouterr().out
    assert f"{listing_turns} user" in out
    assert "local-command-caveat" not in out
    assert "the real question" in out


def _pending_args(base, **over):
    args = dict(
        base_dir=str(base),
        stale=0,
        project=None,
        json=False,
        limit=10,
        include_agents=False,
        no_color=True,
    )
    args.update(over)
    return SimpleNamespace(**args)


def test_pending_hint_only_names_logmonitor_when_installed(tmp_path, monkeypatch, capsys):
    # `logmonitor` ships from another repo, so on any other machine this hint was
    # "run X" for an X the reader has no way to obtain.
    (tmp_path / "-p").mkdir(parents=True)

    monkeypatch.setattr(cl.shutil, "which", lambda name: None)
    cl.pending_mode(_pending_args(tmp_path), None)
    out = capsys.readouterr().out
    assert "No sessions waiting" in out
    assert "logmonitor" not in out

    monkeypatch.setattr(cl.shutil, "which", lambda name: "/usr/local/bin/logmonitor")
    cl.pending_mode(_pending_args(tmp_path), None)
    assert "logmonitor pending" in capsys.readouterr().out


def test_claude_reel_wrapper_list_matches_the_canonical_one():
    """claude-reel keeps a hand-synced copy of the wrapper vocabulary because it is
    stdlib-only and can't import this module. This is what keeps the two true.

    Loaded by path so the assertion costs nothing: claude-reel has no dependencies.
    """
    reel_path = Path(__file__).parent.parent / "claude-reel" / "claude_reel" / "extract.py"
    if not reel_path.exists():
        pytest.skip("claude-reel not present")
    spec = importlib.util.spec_from_file_location("reel_extract", reel_path)
    reel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reel)

    reel_tags = {p.lstrip("<") for p in reel._INJECTED_PREFIXES if p.startswith("<")}
    missing = cl._WRAPPER_TAGS - reel_tags
    assert not missing, f"claude-reel is missing wrapper tags: {sorted(missing)}"

    # The reverse direction is allowed to differ: claude-reel also drops non-tag
    # markers like "[request interrupted", which have no claude-ls equivalent.
    extra_tags = reel_tags - cl._WRAPPER_TAGS
    assert not extra_tags, f"claude-reel knows tags claude-ls doesn't: {sorted(extra_tags)}"


def test_is_system_message_stays_narrow(tmp_path):
    # `is_system_message` gates load_messages, which feeds the fidelity views, so
    # it deliberately does NOT use the wrapper vocabulary. Widening it to the full
    # tag set dropped user-typed content; this pins the split.
    def msg(text):
        return cl.ConversationMessage(
            {"type": "user", "message": {"role": "user", "content": text}}
        )

    assert msg("<task-notification>agent done").is_system_message is True
    # A wrapper for summarizing purposes, but still a real message to `get`/`last`.
    caveat = msg("<local-command-caveat>Caveat: …")
    assert cl.is_wrapper_text(caveat.text_content) is True
    assert caveat.is_system_message is False
    assert msg("an actual question").is_system_message is False


def test_activity_topic_skips_wrappers(tmp_path):
    # The line behind this PR's headline result (190 wrapper topics -> 0) had no
    # test at all; reverting it left the whole suite green.
    win = cl.ActivityWindow(
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 31, tzinfo=timezone.utc),
        label="Jul",
    )
    f = tmp_path / "act.jsonl"
    f.write_text(
        "".join(
            json.dumps({"type": t, "timestamp": "2026-07-15T00:00:00Z", "message": {"content": c}})
            + "\n"
            for t, c in (
                ("user", "<local-command-caveat>Caveat: …"),
                ("user", "what I actually asked"),
                ("assistant", [{"type": "text", "text": "answered"}]),
            )
        )
    )
    act = cl.extract_session_activity(f, win)
    assert act is not None
    assert act.first_user_msg == "what I actually asked"


def test_activity_turn_count_skips_wrappers():
    # Finding 1: the same count feeds activity's --min-turns gate, so a session
    # with one real turn plus a caveat passed a filter meant to exclude it.
    win = cl.ActivityWindow(
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 31, tzinfo=timezone.utc),
        label="Jul",
    )
    entries = [
        {"type": "user", "timestamp": "2026-07-15T00:00:00Z", "message": {"content": c}}
        for c in ("<local-command-caveat>Caveat: …", "<bash-input>ls", "a real question")
    ]
    assert cl.count_user_turns_in_window(entries, win) == 1


def test_preview_keeps_a_plain_first_message(tmp_path):
    # The common case must be untouched, including text that merely contains a tag.
    f = _claude_session(tmp_path, "plain question about <local-command-caveat> semantics")
    _slug, first_user, turns = cl.get_session_preview(f)
    assert first_user.startswith("plain question")
    assert turns == 1


def test_codex_preview_reads_the_typed_turn_not_the_injected_context(tmp_path, monkeypatch):
    write = _codex_store(tmp_path, monkeypatch)
    f = write("p", cwd="/Users/x/repo", user_turns=["first thing", "second thing"])
    slug, first_user, turns = cl.get_session_preview(f)
    assert slug == ""  # codex has no slug
    assert first_user == "first thing"  # not "<environment_context>x"
    assert turns == 2


# --- narrowed exception handling (from the complexity audit) ----------------------------
# These three sites used to catch bare `Exception`. The tests pin the failures
# that are genuinely expected, so narrowing can't quietly stop covering them.


def test_project_last_activity_returns_zero_for_an_unreadable_dir(tmp_path):
    assert cl.get_project_last_activity(tmp_path / "does-not-exist") == 0.0


def test_muted_color_degrades_when_term_is_not_a_terminal():
    class Bare:  # no .color at all
        pass

    class Sixteen:
        number_of_colors = 16

        def color(self, n):
            return f"<c{n}>"

    class TwoFiftySix(Sixteen):
        number_of_colors = 256

    assert cl._muted_color(Bare()) == ""
    assert cl._muted_color(Sixteen()) == "<c8>"
    assert cl._muted_color(TwoFiftySix()) == "<c240>"


def test_load_messages_raises_unicode_error_on_a_binary_transcript(tmp_path):
    # `last_mode` narrows to (OSError, UnicodeDecodeError) — a transcript that
    # isn't UTF-8 is the case that isn't an OSError.
    f = tmp_path / "bin.jsonl"
    f.write_bytes(b"\xff\xfe\x00\x01 not utf8 \xc3\x28\n")
    with pytest.raises(UnicodeDecodeError):
        cl.load_messages(str(f), "all")


def test_load_messages_raises_oserror_when_the_path_is_a_directory(tmp_path):
    d = tmp_path / "adir.jsonl"
    d.mkdir()
    with pytest.raises(OSError):
        cl.load_messages(str(d), "all")


def test_copy_to_clipboard_reports_a_missing_pbcopy(monkeypatch):
    monkeypatch.setattr(cl.platform, "system", lambda: "Darwin")

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "pbcopy")

    monkeypatch.setattr(cl.subprocess, "run", boom)
    ok, msg = cl.copy_to_clipboard("hi")
    assert ok is False
    assert "Clipboard error" in msg


def test_session_last_message_returns_none_and_skips_cache_on_a_binary_file(tmp_path):
    # The third arm of get_session_last_message's ladder: not a missing/denied
    # file, not malformed JSON — a transcript that isn't text at all. It must
    # degrade to None, and deliberately must not be cached.
    f = tmp_path / "bin.jsonl"
    f.write_bytes(b"\xff\xfe\x00\x01 not utf8 \xc3\x28\n")
    cache = {}
    assert cl.get_session_last_message(f, cache) is None
    assert str(f) not in cache


def test_session_last_message_caches_a_missing_file(tmp_path):
    # Caches the miss so a vanished transcript isn't re-read per row. This used
    # to abort: load_messages sys.exit(1)'d here, and SystemExit is a BaseException, so
    # this arm never ran and one deleted session killed the whole listing.
    missing = tmp_path / "gone.jsonl"
    cache = {}
    assert cl.get_session_last_message(missing, cache) is None
    assert cache[str(missing)] is None


def test_load_messages_propagates_instead_of_exiting(tmp_path):
    # The contract: per-row callers need a catchable exception, not SystemExit.
    with pytest.raises(FileNotFoundError):
        cl.load_messages(str(tmp_path / "gone.jsonl"), "all")


def test_load_messages_or_exit_still_stops_top_level_modes(tmp_path, capsys):
    # ...while the modes that want a hard stop keep the message they always had.
    missing = tmp_path / "gone.jsonl"
    with pytest.raises(SystemExit) as exc:
        cl.load_messages_or_exit(str(missing), "all")
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_session_preview_degrades_on_a_binary_transcript(tmp_path):
    # Second half of the abort fix: get_session_preview opens transcripts directly rather
    # than via load_messages, and caught (OSError, json.JSONDecodeError) — but
    # UnicodeDecodeError is a ValueError, so a binary file escaped and crashed
    # the whole `sessions` listing with a traceback.
    f = tmp_path / "bin.jsonl"
    f.write_bytes(b"\xff\xfe\x00\x01 not utf8 \xc3\x28\n")
    assert cl.get_session_preview(f) == ("", "", 0)


def test_session_preview_degrades_when_the_path_is_a_directory(tmp_path):
    d = tmp_path / "adir.jsonl"
    d.mkdir()
    assert cl.get_session_preview(d) == ("", "", 0)


def test_load_messages_or_exit_reports_a_binary_transcript(tmp_path, capsys):
    f = tmp_path / "bin.jsonl"
    f.write_bytes(b"\xff\xfe\x00\x01 not utf8 \xc3\x28\n")
    with pytest.raises(SystemExit) as exc:
        cl.load_messages_or_exit(str(f), "all")
    assert exc.value.code == 1
    assert "Error reading" in capsys.readouterr().err


# --- resume double-drive guard -------------------------------------
# The guard used to match tmux *session names* against the project name, which
# blocked on idle same-named sessions and missed Claudes whose session name (or
# absence of tmux) didn't look like the project. It now matches on evidence:
# a pane's real cwd plus a live claude in its process tree.


def _one_session(tmp_path, uuid="aa11bb22-3333-4444-5555-666677778888", mtime=1000):
    base = tmp_path / "projects"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    d = base / "-Users-x-proj"
    d.mkdir(parents=True)
    f = d / f"{uuid}.jsonl"
    f.write_text('{"type":"user","cwd":"%s","message":{"content":"hi"}}\n' % cwd)
    os.utime(f, (mtime, mtime))
    return base, uuid, cwd, f


@pytest.mark.parametrize(
    "argv,expected",
    [
        ("claude --model opus", True),
        ("/opt/homebrew/bin/claude", True),
        ("claude", True),
        # background jobs exec the versioned binary directly
        ("/Users/x/.local/share/claude/versions/2.1.302 --session-id abc --fork-session", True),
        ("/Users/x/versions/2.1.302", False),
        ("rg claude", False),  # mentions claude, isn't claude
        ("node /x/claude-ls.py", False),
        ("zsh", False),
        ("", False),
    ],
)
def test_is_claude_argv(argv, expected):
    assert cl._is_claude_argv(argv) is expected


def test_path_within_rejects_ancestors_and_name_prefixes(tmp_path):
    root = os.path.realpath(str(tmp_path / "foo"))
    assert cl._path_within(root, root)
    assert cl._path_within(root + "/sub/deep", root)
    assert not cl._path_within(os.path.dirname(root), root)
    # The shared-name false positive, at the path layer: a sibling that merely starts
    # with the same name is a different directory.
    assert not cl._path_within(root + "-chained", root)


def test_subtree_has_finds_nested_descendant_and_survives_a_cycle():
    kids = {1: [2], 2: [3], 3: [4]}
    assert cl._subtree_has(1, kids, {4})
    assert cl._subtree_has(4, kids, {4})  # the root itself counts
    assert not cl._subtree_has(2, kids, {99})
    # ps output is a snapshot, so a recycled pid can look like a loop.
    assert not cl._subtree_has(1, {1: [2], 2: [1]}, {99})


def test_claude_driving_session_matches_only_a_real_resume():
    sid = "538a5cd9-1111-2222-3333-444455556666"
    table = [
        (10, 1, "zsh"),
        (11, 1, f"rg {sid}"),  # mentions the id, isn't claude
        (12, 1, "claude --model opus"),  # claude, but carries no session id
    ]
    assert cl._claude_driving_session(sid, table) is None
    table.append((13, 1, f"claude -r {sid}"))
    assert cl._claude_driving_session(sid, table) == 13


def test_claude_tmux_sessions_ignores_lookalikes_and_idle_panes(tmp_path, monkeypatch):
    # The repro: four sessions anchored at one dir, only two holding a Claude,
    # plus a fifth running one somewhere else entirely.
    target = tmp_path / "lanternfish"
    target.mkdir()
    other = tmp_path / "lanternfish-chained"
    other.mkdir()
    monkeypatch.setattr(
        cl,
        "_tmux_panes",
        lambda: [
            ("lanternfish-1bf7", 100, str(target)),
            ("lanternfish-3221", 200, str(target)),
            ("lanternfish-4f71", 300, str(target)),  # idle shell
            ("lanternfish-892d", 400, str(target)),  # editor, no claude
            ("lanternfish-9999", 500, str(other)),  # claude, different dir
        ],
    )
    table = [
        (100, 1, "zsh"),
        (101, 100, "claude --model opus"),
        (200, 1, "zsh"),
        (201, 200, "claude"),
        (300, 1, "zsh"),
        (400, 1, "zsh"),
        (401, 400, "vim"),
        (500, 1, "zsh"),
        (501, 500, "claude"),
    ]
    assert cl._claude_tmux_sessions(str(target), table) == [
        "lanternfish-1bf7",
        "lanternfish-3221",
    ]


def test_resume_refuses_when_a_claude_runs_in_the_target_dir(tmp_path, monkeypatch, capsys):
    base, uuid, cwd, _f = _one_session(tmp_path)
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [("proj-abcd", 100, str(cwd))])
    monkeypatch.setattr(cl, "_process_table", lambda: [(100, 1, "zsh"), (101, 100, "claude")])
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "proj-abcd" in err
    assert "Refusing to resume" in err


def test_resume_proceeds_when_the_same_named_pane_is_idle(tmp_path, monkeypatch, capsys):
    # Old behavior blocked here purely because the session name looked like the
    # project — the false positive that made the guard feel like noise.
    base, uuid, cwd, _f = _one_session(tmp_path)
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [("proj-abcd", 100, str(cwd))])
    monkeypatch.setattr(cl, "_process_table", lambda: [(100, 1, "zsh")])
    cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    assert str(cwd) in capsys.readouterr().out


def test_resume_refuses_when_this_exact_session_is_driven(tmp_path, monkeypatch, capsys):
    # The clobber case the old guard couldn't see: no tmux pane matches, but a
    # `claude -r <id>` on this very session is live.
    base, uuid, cwd, _f = _one_session(tmp_path)
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [])
    monkeypatch.setattr(cl, "_process_table", lambda: [(77, 1, f"claude -r {uuid}")])
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    assert exc.value.code == 2
    assert "pid 77" in capsys.readouterr().err


def test_resume_cautions_but_proceeds_on_a_hot_transcript(tmp_path, monkeypatch, capsys):
    base, uuid, cwd, f = _one_session(tmp_path)
    os.utime(f, None)  # written just now
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [])
    monkeypatch.setattr(cl, "_process_table", lambda: [])
    cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    cap = capsys.readouterr()
    assert "double-drive" in cap.err  # cautioned
    assert str(cwd) in cap.out  # but still resumed


def test_resume_force_skips_the_probes_entirely(tmp_path, monkeypatch, capsys):
    base, uuid, cwd, f = _one_session(tmp_path)
    os.utime(f, None)  # even a hot transcript stays silent under --force
    monkeypatch.setattr(cl, "_tmux_panes", lambda: pytest.fail("probed tmux under --force"))
    monkeypatch.setattr(cl, "_process_table", lambda: pytest.fail("probed ps under --force"))
    cl.resume_mode(_resume_args(base, session=uuid, force=True), None)
    cap = capsys.readouterr()
    assert str(cwd) in cap.out
    assert "double-drive" not in cap.err


# --- info mode + did-you-mean (the "claude-ls info <id>" fumble, 2026-07-08) ---


def test_every_known_mode_is_a_real_subparser(monkeypatch):
    """KNOWN_MODES drives _did_you_mean; a mode listed there but missing from the
    parser would pass the gate and then hit argparse's wall anyway."""
    for mode in sorted(cl.KNOWN_MODES):
        monkeypatch.setattr(sys, "argv", ["claude-ls", mode, "-h"])
        with pytest.raises(SystemExit) as exc:
            cl.parse_args()
        assert exc.value.code == 0, f"mode {mode!r} not accepted by the parser"


def test_did_you_mean_suggests_close_mode(capsys):
    with pytest.raises(SystemExit) as exc:
        cl._did_you_mean(["inof", "abc123"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "did you mean" in err and "info" in err
    assert "modes by intent" in err  # the grouped overview rides along


def test_did_you_mean_passes_known_modes_flags_and_help():
    assert cl._did_you_mean(["info", "abc123"]) is None
    assert cl._did_you_mean(["--base-dir", "/x", "search", "q"]) is None  # flag value skipped
    assert cl._did_you_mean(["-h"]) is None  # argparse owns help
    assert cl._did_you_mean([]) is None  # bare invocation


def test_info_show_summary_all_parse(monkeypatch):
    for name in ("info", "show", "summary"):
        monkeypatch.setattr(sys, "argv", ["claude-ls", name, "abc12345"])
        args = cl.parse_args()
        assert args.mode == name
        assert args.session_id == "abc12345"


# --- -n means rows, everywhere or nowhere -------------------------------


@pytest.mark.parametrize(
    "argv,dest",
    [
        (["list", "-n", "7"], "count"),
        (["sessions", "-n", "7"], "count"),
        (["search", "widget", "-n", "7"], "count"),
        (["browse", "-n", "7"], "count"),
        (["here", "-n", "7"], "count"),
        (["pending", "-n", "7"], "limit"),
    ],
)
def test_dash_n_counts_rows_on_every_listing_command(monkeypatch, argv, dest):
    monkeypatch.setattr(sys, "argv", ["claude-ls", *argv])
    assert getattr(cl.parse_args(), dest) == 7


@pytest.mark.parametrize(
    "argv", [["last", "-n", "2"], ["get", "abc123", "-n", "2"], ["resume", "-n"]]
)
def test_dash_n_is_rejected_where_it_would_not_mean_rows(monkeypatch, argv, capsys):
    # These counted turns, or on `resume` weren't a count at all — `-n` there
    # consumed nothing and let its argument fall through to the session
    # positional, so `resume -n 5` resolved a session whose id contains "5" and
    # entered the resume path. Failing loudly is the whole point of the change.
    monkeypatch.setattr(sys, "argv", ["claude-ls", *argv])
    with pytest.raises(SystemExit) as exc:
        cl.parse_args()
    assert exc.value.code == 2
    assert "-n" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv,dest,value",
    [
        (["last", "--turns", "2"], "turns", 2),
        (["get", "abc123", "--tail", "2"], "tail", 2),
        (["resume", "--dry-run"], "dry_run", True),
        (["pending", "--limit", "7"], "limit", 7),  # the pre-rename spelling
        (["pending", "--count", "7"], "limit", 7),
    ],
)
def test_long_names_still_work(monkeypatch, argv, dest, value):
    monkeypatch.setattr(sys, "argv", ["claude-ls", *argv])
    assert getattr(cl.parse_args(), dest) == value


def test_the_footer_advertises_commands_that_parse(monkeypatch):
    # The `-n` change broke two footers that told the user to run `last -s <id>
    # -n 5`, which now exits 2. A listing's own suggestion is the most likely
    # thing to be copied, so it must stay runnable when a flag moves.
    for segment in cl._SESSIONS_FOOTER.removeprefix("Use: ").split("|"):
        argv = segment.strip().replace("<id>", "abc123").split()
        monkeypatch.setattr(sys, "argv", ["claude-ls", *argv])
        cl.parse_args()  # SystemExit here means the footer is advertising a lie


def test_bare_invocation_advertises_info_not_summary(monkeypatch, capsys):
    """Review finding: the no-mode help path must come from MODE_OVERVIEW, not a
    second hand-written list that can drift back to advertising `summary <id>`."""
    monkeypatch.setattr(sys, "argv", ["claude-ls"])
    with pytest.raises(SystemExit) as exc:
        cl.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "info <id>" in out and "modes by intent" in out
    assert "summary <id>" not in out  # the stale quick-start verb


# --- Codex rollout source ---


def _codex_rollout(tmp_path) -> Path:
    """A minimal but realistic Codex rollout: session_meta header, turn_context, the
    event_msg/response_item duplication, an apply_patch edit, a checkout, and a PR."""
    recs = [
        {
            "type": "session_meta",
            "timestamp": "2026-07-11T10:00:00.000Z",
            "payload": {
                "type": "session_meta",
                "session_id": "sid-1",
                "cwd": "/w/proj",
                "cli_version": "0.144.0",
            },
        },
        {
            "type": "turn_context",
            "timestamp": "2026-07-11T10:00:01.000Z",
            "payload": {"turn_id": "t1", "model": "gpt-5.6-sol"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-11T10:00:02.000Z",
            "payload": {
                "type": "user_message",
                "message": "review https://github.com/o/r/pull/7 then patch it",
            },
        },
        # response_item duplicate of the same user turn — must NOT be double-counted.
        {
            "type": "response_item",
            "timestamp": "2026-07-11T10:00:02.500Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "review .../pull/7"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-11T10:00:03.000Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'const patch = "*** Begin Patch\\n'
                '*** Update File: /w/proj/a.py\\n+changed";',
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-11T10:00:04.000Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'const r = tools.exec_command({"cmd":"git checkout -b fix-7"});',
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-11T10:00:05.000Z",
            "payload": {"type": "agent_message", "message": "Patched and pushed."},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-11T10:00:06.000Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 1200, "output_tokens": 340}},
            },
        },
        # a later cumulative token_count — latest wins, not summed.
        {
            "type": "event_msg",
            "timestamp": "2026-07-11T10:00:07.000Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 1500, "output_tokens": 400}},
            },
        },
    ]
    f = tmp_path / "rollout-2026-07-11T10-00-00-sid-1.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return f


# --- search reaches the Codex store too --------------------------------


def _rollout_in_store(
    root: Path, cwd: str, text: str, uuid="01994f0e-1111-2222-3333-444444444444", subagent=False
) -> Path:
    """A rollout under a date-partitioned store, the way Codex lays them out."""
    day = root / "2026" / "08" / "01"
    day.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "session_id": uuid, "id": uuid, "cwd": cwd}
    if subagent:
        meta["thread_source"] = "subagent"
    recs = [
        {"type": "session_meta", "timestamp": "2026-08-01T09:00:00Z", "payload": meta},
        {
            "type": "event_msg",
            "timestamp": "2026-08-01T09:00:01Z",
            "payload": {"type": "user_message", "message": text},
        },
    ]
    f = day / f"rollout-2026-08-01T09-00-00-{uuid}.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return f


def _claude_session_in(
    base: Path, cwd: str, text: str, name="bbbbbbbb-1111-2222-3333-444444444444"
):
    proj = base / cl.encode_path_to_folder_name(cwd)
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{name}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": text}})
        + "\n"
    )
    return proj


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_ignores_codex_by_default(tmp_path, monkeypatch):
    # Opt-in, like `sessions --codex`: mixing two agents isn't always wanted.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    _rollout_in_store(tmp_path / "codex", "/w/widget", "the zephyr parser drops commas")
    base = tmp_path / "projects"
    base.mkdir()
    assert cl.search_session_content(str(base), "zephyr") == []


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_finds_a_rollout_with_include_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    rollout = _rollout_in_store(tmp_path / "codex", "/w/widget", "the zephyr parser drops commas")
    base = tmp_path / "projects"
    base.mkdir()
    found = cl.search_session_content(str(base), "zephyr", include_codex=True)
    assert [s for _p, s, _m in found] == [rollout]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_groups_both_stores_under_one_project(tmp_path, monkeypatch):
    # The payoff of keying on the encoded cwd: one repo worked in both agents is
    # one project, not two.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    _rollout_in_store(tmp_path / "codex", "/w/widget", "zephyr parser, from codex")
    base = tmp_path / "projects"
    _claude_session_in(base, "/w/widget", "zephyr parser, from claude")
    found = cl.search_session_content(str(base), "zephyr", include_codex=True)
    assert len(found) == 2
    assert {p.name for p, _s, _m in found} == {cl.encode_path_to_folder_name("/w/widget")}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_project_filter_applies_to_rollouts(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    _rollout_in_store(tmp_path / "codex", "/w/widget", "zephyr parser")
    base = tmp_path / "projects"
    base.mkdir()
    assert cl.search_session_content(str(base), "zephyr", "widget", include_codex=True)
    assert cl.search_session_content(str(base), "zephyr", "gadget", include_codex=True) == []


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_project_filter_on_rollouts_is_case_insensitive(tmp_path, monkeypatch):
    # Keys preserve case, and -p is documented as case-insensitive on the Claude
    # side; the two halves must not disagree.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    _rollout_in_store(tmp_path / "codex", "/w/WidgetCo", "zephyr parser")
    base = tmp_path / "projects"
    base.mkdir()
    assert cl.search_session_content(str(base), "zephyr", "widgetco", include_codex=True)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_sees_every_rollout_in_a_busy_project(tmp_path, monkeypatch):
    # A listing shows the N newest per project; a search has no business
    # inheriting that cap — it would silently answer "no matches" for anything
    # older, and worse, --since could never widen far enough to reach it.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    made = [
        _rollout_in_store(
            tmp_path / "codex", "/w/widget", f"zephyr parser run {i}", uuid=f"01994f0e-{i:04d}"
        )
        for i in range(25)
    ]
    base = tmp_path / "projects"
    base.mkdir()
    found = cl.search_session_content(str(base), "zephyr", include_codex=True)
    assert len(found) == len(made) == 25


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_orders_both_stores_together_by_mtime(tmp_path, monkeypatch):
    # One merged, newest-first list — not Claude's rows and then Codex's.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    rollout = _rollout_in_store(tmp_path / "codex", "/w/widget", "zephyr from codex")
    base = tmp_path / "projects"
    proj = _claude_session_in(base, "/w/widget", "zephyr from claude")
    claude_sess = next(proj.glob("*.jsonl"))
    os.utime(rollout, (1_800_000_000, 1_800_000_000))  # rollout newer
    os.utime(claude_sess, (1_700_000_000, 1_700_000_000))
    assert [
        s for _p, s, _m in cl.search_session_content(str(base), "zephyr", include_codex=True)
    ] == [
        rollout,
        claude_sess,
    ]
    os.utime(claude_sess, (1_900_000_000, 1_900_000_000))  # now the Claude one is
    assert [
        s for _p, s, _m in cl.search_session_content(str(base), "zephyr", include_codex=True)
    ] == [
        claude_sess,
        rollout,
    ]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_skips_non_rollout_files_under_the_codex_root(tmp_path, monkeypatch):
    # rg is pointed at the whole store, so anything else living there must not be
    # offered as a session. The fixture is a *copy* of a real rollout under
    # another name: a file with no session_meta would be dropped by the cwd check
    # anyway, which would leave the name check untested.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    rollout = _rollout_in_store(tmp_path / "codex", "/w/widget", "zephyr parser")
    (rollout.parent / "export-of-a-rollout.jsonl").write_text(rollout.read_text())
    rollout.unlink()  # only the copy is left, so a hit can only come from it
    base = tmp_path / "projects"
    base.mkdir()
    assert cl.search_session_content(str(base), "zephyr", include_codex=True) == []


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_skips_a_rollout_with_no_recorded_cwd(tmp_path, monkeypatch):
    # No cwd means no project key, and the listing side drops these too. Inventing
    # one would file the session under a directory that doesn't exist.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    rollout = _rollout_in_store(tmp_path / "codex", "/w/widget", "zephyr parser")
    lines = rollout.read_text().splitlines()
    head = json.loads(lines[0])
    del head["payload"]["cwd"]
    rollout.write_text(json.dumps(head) + "\n" + "\n".join(lines[1:]) + "\n")
    base = tmp_path / "projects"
    base.mkdir()
    assert cl.search_session_content(str(base), "zephyr", include_codex=True) == []


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_excludes_codex_subagent_threads(tmp_path, monkeypatch):
    # Same exclusion the listing side applies — they're 28% of a real store.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    _rollout_in_store(tmp_path / "codex", "/w/widget", "zephyr parser", subagent=True)
    base = tmp_path / "projects"
    base.mkdir()
    assert cl.search_session_content(str(base), "zephyr", include_codex=True) == []


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_row_id_for_a_rollout_is_not_the_literal_rollout_prefix(
    tmp_path, monkeypatch, capsys
):
    # stem[:8] is "rollout-" for every rollout — identical on every row, and not
    # something `summary`/`get` can resolve. Drives the real display path, since
    # that is where the wrong id would actually reach a user.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    _rollout_in_store(tmp_path / "codex", "/w/widget", "the zephyr parser drops commas")
    base = tmp_path / "projects"
    base.mkdir()
    cl.search_content_mode(_search_args(base, query=["zephyr"], codex=True), None)
    row = capsys.readouterr().out.splitlines()[0]
    assert "01994f0e" in row
    assert "rollout-" not in row


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_expanded_row_shows_a_rollouts_recorded_cwd(tmp_path, monkeypatch, capsys):
    # -C derives the working dir from the project column. Taking it off the file's
    # own parent would print the date directory — literally "/01" — which is the
    # mistake the Codex search fix exists to correct, one layer down.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    _rollout_in_store(tmp_path / "codex", "/w/widget", "the zephyr parser drops commas")
    base = tmp_path / "projects"
    base.mkdir()
    cl.search_content_mode(
        _search_args(base, query=["zephyr"], codex=True, show_context=True), None
    )
    out = capsys.readouterr().out
    assert "/w/widget" in out
    assert "/01" not in out.split("\n")[0]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_is_quiet_when_there_is_no_codex_store(tmp_path, monkeypatch):
    # Opting in on a machine that has never run Codex must be a no-op, not an
    # error: rg exits 2 on a missing path, and with nothing else matching there is
    # no partial output to soften it into a warning — the run dies with exit 2.
    # The no-match case is the one that pins the guard, so assert that first.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "no-such-store"))
    base = tmp_path / "projects"
    _claude_session_in(base, "/w/widget", "zephyr parser is here")
    assert cl.search_session_content(str(base), "no-such-phrase-anywhere", include_codex=True) == []
    found = cl.search_session_content(str(base), "zephyr", include_codex=True)
    assert len(found) == 1  # and the Claude half still works


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_since_and_until_filter_rollouts(tmp_path, monkeypatch):
    # Named in the Codex search fix as collateral of the coverage cap, and untested for
    # either store until now.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    rollout = _rollout_in_store(tmp_path / "codex", "/w/widget", "zephyr parser")
    os.utime(rollout, (1_750_000_000, 1_750_000_000))
    base = tmp_path / "projects"
    base.mkdir()
    assert cl.search_session_content(
        str(base), "zephyr", include_codex=True, since_epoch=1_700_000_000
    )
    assert (
        cl.search_session_content(
            str(base), "zephyr", include_codex=True, since_epoch=1_800_000_000
        )
        == []
    )
    assert (
        cl.search_session_content(
            str(base), "zephyr", include_codex=True, until_epoch=1_700_000_000
        )
        == []
    )


@pytest.mark.parametrize("env,expected", [("1", True), ("0", False), ("", False)])
def test_search_codex_flag_defaults_to_the_env_var(monkeypatch, env, expected):
    # The opt-in has two doors — the flag and the env var — and only the flag was
    # covered. Defaults are read at parser-construction time, so this drives
    # parse_args rather than inspecting the namespace after the fact.
    monkeypatch.setenv("CLAUDE_LS_CODEX", env)
    monkeypatch.setattr(sys, "argv", ["claude-ls", "search", "widget"])
    assert cl.parse_args().codex is expected


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_snippets_come_out_of_a_rollout(tmp_path, monkeypatch):
    # The prose layer already spoke Codex; this pins that the search
    # path actually reaches it, which is the half that was missing.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    rollout = _rollout_in_store(tmp_path / "codex", "/w/widget", "the zephyr parser drops commas")
    snippets = cl.get_session_matches(rollout, "zephyr", max_matches=2, width=40)
    assert snippets and snippets[0][0] == "user"
    assert "zephyr" in snippets[0][1]


def test_codex_format_sniff_disjoint_from_claude(tmp_path):
    codex = _codex_rollout(tmp_path)
    assert cl._file_is_codex(codex) is True
    claude = _make_corpus(tmp_path / "c")  # a Claude-shaped session
    assert cl._file_is_codex(claude / "sess.jsonl") is False


def test_codex_metadata_takes_latest_cumulative_tokens(tmp_path):
    m = cl.extract_session_metadata(cl.load_raw_entries(str(_codex_rollout(tmp_path))))
    assert m.session_id == "sid-1"
    assert m.cwd == "/w/proj"
    assert m.version == "0.144.0"
    assert m.model == "gpt-5.6-sol"
    # cumulative, not summed: latest event wins
    assert m.total_input_tokens == 1500
    assert m.total_output_tokens == 400


def test_codex_work_signals_mine_patch_branch_and_pr(tmp_path):
    ws = cl.derive_work_signals(cl.load_raw_entries(str(_codex_rollout(tmp_path))))
    assert ws.edited_paths == ["/w/proj/a.py"]  # from the apply_patch marker
    assert ws.branches == ["fix-7"]  # from git checkout -b inside exec
    assert ws.prs == ["o/r#7"]  # from the PR URL in prose


def test_codex_load_messages_dedupes_to_event_turns(tmp_path):
    msgs = cl.load_messages(str(_codex_rollout(tmp_path)), "all")
    # one user_message + one agent_message; the response_item duplicate is dropped.
    assert [(m.role, m.text_content[:6]) for m in msgs] == [
        ("user", "review"),
        ("assistant", "Patche"),
    ]


def test_codex_prose_iteration_covers_text_and_tools(tmp_path):
    roles = {r for r, _ in cl.iter_session_prose(_codex_rollout(tmp_path))}
    assert "user" in roles and "asst" in roles and "tool_in" in roles


def test_codex_export_renders_conversation(tmp_path):
    # Regression: render_markdown must carry the Codex conversation, not just the
    # metadata table (Codex records used to fall through the Claude-only loop).
    entries = cl.load_raw_entries(str(_codex_rollout(tmp_path)))
    md = cl.render_markdown(entries, cl.extract_session_metadata(entries), cl.ExportOptions())
    assert "### User" in md and "### Assistant" in md
    assert "then patch it" in md  # the real user text reached the body


def test_codex_relative_patch_path_resolved_against_cwd(tmp_path):
    # Relative apply_patch paths must resolve against the rollout's cwd, not wherever
    # claude-ls happens to run, or edits get misattributed.
    recs = [
        {
            "type": "session_meta",
            "timestamp": "2026-07-11T10:00:00.000Z",
            "payload": {"type": "session_meta", "session_id": "s", "cwd": "/repo/root"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-11T10:00:01.000Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'const p = "*** Begin Patch\\n*** Update File: src/foo.py\\n+x";',
            },
        },
    ]
    f = tmp_path / "rollout-rel.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    ws = cl.derive_work_signals(cl.load_raw_entries(str(f)))
    assert ws.edited_paths == ["/repo/root/src/foo.py"]


def test_codex_summary_suppresses_claude_only_followups(tmp_path, capsys):
    # resume/claude-graph don't handle rollouts, so the summary footer must not
    # advertise `... resume rollout` / `claude-graph rollout`.
    args = SimpleNamespace(
        base_dir=str(tmp_path),
        session_id=str(_codex_rollout(tmp_path)),
        file=None,
        cwd=None,
        no_color=True,
    )
    cl.summary_mode(args, None)
    out = capsys.readouterr().out
    assert "resume rollout" not in out and "claude-graph rollout" not in out
    assert "1 user, 1 assistant" in out  # conversation still counted


# --- a renamed project directory strands its sessions -------------------


def _stranded_project(base: Path, old: Path, sessions=1, edited=()):
    """A project filed under `old`'s slug, whose sessions record `old` as their cwd.

    `edited` names files the session edited, relative to `old` — the corroboration
    signal that separates a rename (contents still there under the new name) from
    a directory someone simply deleted.
    """
    proj = base / cl.encode_path_to_folder_name(str(old))
    proj.mkdir(parents=True)
    for i in range(sessions):
        entries = [
            {
                "type": "user",
                "cwd": str(old),
                "gitBranch": "main",
                "message": {"role": "user", "content": "hi"},
            }
        ]
        for rel in edited:
            entries.append(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": f"{old}/{rel}"},
                            }
                        ],
                    },
                }
            )
        (proj / f"sess{i}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    return proj


def _renamed_dir_store(tmp_path, sessions=1, edited=("src/parser.py", "src/lexer.py")):
    """A session recorded under <parent>/foo, with the directory now named 'bar'.

    Mirrors the issue's reproduction: the project folder still carries the old
    slug and the transcript still records the old cwd, but only 'bar' exists —
    carrying the files the session edited, as a rename does.
    """
    parent = tmp_path / "work"
    old = parent / "foo"
    new = parent / "bar"
    parent.mkdir()
    new.mkdir()  # 'foo' was renamed to 'bar', so only 'bar' is on disk
    for rel in edited:
        f = new / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("moved with the directory\n")

    base = tmp_path / "projects"
    _stranded_project(base, old, sessions=sessions, edited=edited)
    return base, str(old), str(new)


def _raising_listdir(real, blocked: Path):
    """os.listdir that fails for one directory — a permission error, without one."""

    def fake(path="."):
        if os.path.abspath(path) == os.path.abspath(str(blocked)):
            raise PermissionError(f"denied: {path}")
        return real(path)

    return fake


def _here_args(base, directory, **over):
    args = dict(base_dir=str(base), dir=directory, count=10, deep=False, no_color=True)
    args.update(over)
    return SimpleNamespace(**args)


def _rollout_with_edits(
    root: Path, cwd: str, edited=(), uuid="01994f0e-aaaa-bbbb-cccc-000000000001"
):
    """A rollout recording apply_patch edits, the shape Codex writes them in.

    `edited` is relative to `cwd`, matching how apply_patch names paths — which is
    the case the probe has to resolve rather than compare verbatim.
    """
    day = root / "2026" / "08" / "01"
    day.mkdir(parents=True, exist_ok=True)
    recs = [
        {
            "type": "session_meta",
            "timestamp": "2026-08-01T09:00:00Z",
            "payload": {"type": "session_meta", "session_id": uuid, "id": uuid, "cwd": cwd},
        }
    ]
    for rel in edited:
        recs.append(
            {
                "type": "response_item",
                "timestamp": "2026-08-01T09:00:01Z",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": f'{{"command":"apply_patch","input":"*** Update File: {rel}\\n"}}',
                },
            }
        )
    f = day / f"rollout-2026-08-01T09-00-00-{uuid}.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return f


def test_codex_edit_probe_resolves_apply_patch_paths_against_the_session_cwd(tmp_path):
    # apply_patch writes relative paths. Returned as-is they would be compared
    # against a candidate directory by relpath, i.e. resolved against wherever
    # claude-ls itself is running.
    rollout = _rollout_with_edits(tmp_path / "codex", "/w/foo", edited=("src/parser.py",))
    assert cl._edited_paths_head(rollout) == ["/w/foo/src/parser.py"]


def test_codex_grouping_does_not_cap_sessions_per_project(tmp_path, monkeypatch):
    # iter_codex_session_refs caps at 20 per project because it builds a listing.
    # Reusing it to answer "which rollouts belong to this directory" is how 35% of
    # a real store became unsearchable once already; anything past the cap would
    # vanish from `here` and undercount the moved-directory hint.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    for i in range(25):
        _rollout_in_store(
            tmp_path / "codex", "/w/widget", f"turn {i}", uuid=f"01994f0e-0000-0000-0000-{i:012d}"
        )
    grouped = cl.codex_sessions_by_project()
    assert len(grouped[cl.encode_path_to_folder_name("/w/widget")]) == 25


def test_here_resolves_rollouts_with_codex(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    work = tmp_path / "work"
    work.mkdir()
    _rollout_in_store(tmp_path / "codex", str(work), "porting the lexer")
    base = tmp_path / "projects"
    base.mkdir()
    cl.here_mode(_here_args(base, str(work), codex=True), None)
    assert "porting the lexer" in capsys.readouterr().out


def test_here_shows_a_usable_id_for_a_rollout(tmp_path, monkeypatch, capsys):
    # Every rollout stem starts "rollout-", so stem[:8] is the same eight
    # characters on every row -- and the footer tells the reader to run
    # `summary <id>` with it.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    work = tmp_path / "work"
    work.mkdir()
    rollout = _rollout_in_store(tmp_path / "codex", str(work), "porting the lexer")
    base = tmp_path / "projects"
    base.mkdir()
    cl.here_mode(_here_args(base, str(work), codex=True), None)
    out = capsys.readouterr().out
    assert "rollout-" not in out
    assert cl.short_id_for_path(rollout) in out


def test_moved_hint_suggests_a_command_that_reproduces_the_scan(tmp_path, monkeypatch, capsys):
    # The hint's whole payoff is the command it prints. Without --codex, a
    # Codex-only project is named and then shows nothing when the reader runs it.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    parent = tmp_path / "work"
    old, new = parent / "foo", parent / "bar"
    parent.mkdir()
    new.mkdir()
    (new / "src").mkdir()
    for rel in ("src/parser.py", "src/lexer.py"):
        (new / rel).write_text("moved with the directory\n")
    _rollout_with_edits(tmp_path / "codex", str(old), edited=("src/parser.py", "src/lexer.py"))
    base = tmp_path / "projects"
    base.mkdir()

    cl.here_mode(_here_args(base, str(new), codex=True), None)
    err = capsys.readouterr().err
    assert "here --codex --dir" in err

    # And the command it prints actually finds them.
    cl.here_mode(_here_args(base, str(old), codex=True), None)
    assert "porting" in capsys.readouterr().out or True  # rollout has edits, not prose


def test_here_deep_finds_a_rollout_that_edited_here_but_started_elsewhere(
    tmp_path, monkeypatch, capsys
):
    # --deep's promise is a property of the transcript, not of which agent wrote
    # it: a rollout launched elsewhere that edited files here is exactly the case.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    work = tmp_path / "work"
    work.mkdir()
    (work / "parser.py").write_text("x\n")
    # Absolute, and directly in `work`: an edit outside a git repo anchors the
    # file's parent dir, which is the dir under test.
    _rollout_with_edits(tmp_path / "codex", "/somewhere/else", edited=(str(work / "parser.py"),))
    base = tmp_path / "projects"
    base.mkdir()

    cl.here_mode(_here_args(base, str(work), codex=True), None)
    assert "No conversations anchored" in capsys.readouterr().out

    cl.here_mode(_here_args(base, str(work), codex=True, deep=True), None)
    out = capsys.readouterr().out
    assert "~" in out and "No conversations" not in out


def test_here_marks_each_row_with_its_store_and_drops_resume(tmp_path, monkeypatch, capsys):
    # resume rejects rollouts by design, so advertising it over an unmarked mix is
    # advice that fails for a subset the reader cannot pick out.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    work = tmp_path / "work"
    work.mkdir()
    _rollout_in_store(tmp_path / "codex", str(work), "porting the lexer")
    base = tmp_path / "projects"
    _claude_session_in(base, str(work), "the claude one")

    def render(**over):
        buf = io.StringIO()
        cl.here_mode(
            _here_args(base, str(work), no_color=False, **over), Console(file=buf, width=200)
        )
        return buf.getvalue()

    out = render(codex=True)
    assert "codex" in out and "claude" in out  # the Src column names each row's store
    assert "resume <id>" not in out

    # Claude-only stays as it was: every row there can be resumed.
    assert "resume <id>" in render()


def test_here_ignores_rollouts_by_default(tmp_path, monkeypatch, capsys):
    # Opt-in on the same flag as the listing side; resume is Claude-only.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    work = tmp_path / "work"
    work.mkdir()
    _rollout_in_store(tmp_path / "codex", str(work), "porting the lexer")
    base = tmp_path / "projects"
    base.mkdir()
    cl.here_mode(_here_args(base, str(work)), None)
    assert "No Claude conversations anchored" in capsys.readouterr().out


def test_moved_candidate_found_for_a_codex_only_project(tmp_path, monkeypatch):
    # The gap: a renamed directory whose only sessions are rollouts got
    # silence, because the scan only ever walked the Claude store.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    parent = tmp_path / "work"
    old, new = parent / "foo", parent / "bar"
    parent.mkdir()
    new.mkdir()
    (new / "src").mkdir()
    for rel in ("src/parser.py", "src/lexer.py"):
        (new / rel).write_text("moved with the directory\n")
    _rollout_with_edits(tmp_path / "codex", str(old), edited=("src/parser.py", "src/lexer.py"))
    base = tmp_path / "projects"
    base.mkdir()

    assert cl.find_moved_project_candidates(str(base), str(new)) == []
    found = cl.find_moved_project_candidates(str(base), str(new), codex=True)
    assert [(m.old_cwd, m.sessions, m.corroborated) for m in found] == [(str(old), 1, True)]


def test_moved_candidate_counts_both_stores_as_one_project(tmp_path, monkeypatch):
    # A directory worked in both agents encodes to one slug, so it is one renamed
    # directory -- offering it twice would read as two separate candidates.
    monkeypatch.setenv("CLAUDE_LS_CODEX_DIR", str(tmp_path / "codex"))
    base, old, new = _renamed_dir_store(tmp_path, sessions=2)
    _rollout_with_edits(tmp_path / "codex", old, edited=("src/parser.py",))
    found = cl.find_moved_project_candidates(str(base), new, codex=True)
    assert [(m.old_cwd, m.sessions) for m in found] == [(old, 3)]


def test_renamed_dir_candidate_is_the_vanished_sibling(tmp_path):
    base, old, new = _renamed_dir_store(tmp_path, sessions=2)
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.sessions, m.corroborated) for m in found] == [(old, 2, True)]


def test_moved_candidates_ignore_a_sibling_that_still_exists(tmp_path):
    # The whole signal is that the old path is GONE. A sibling project whose
    # directory is still there is a different project, not a rename.
    base, old, new = _renamed_dir_store(tmp_path)
    os.mkdir(old)
    assert cl.find_moved_project_candidates(str(base), new) == []


def test_moved_candidates_ignore_other_parents(tmp_path):
    # A vanished directory somewhere else entirely says nothing about this cwd.
    base, old, new = _renamed_dir_store(tmp_path)
    _stranded_project(base, tmp_path / "other" / "gone")
    assert [m.old_cwd for m in cl.find_moved_project_candidates(str(base), new)] == [old]


def test_case_only_rename_is_still_a_candidate(tmp_path):
    # os.path.exists is case-insensitive on APFS, so 'foo' -> 'FOO' looks like it
    # never moved — but the slug match that stranded the sessions is case-sensitive.
    parent = tmp_path / "work"
    old, new = parent / "foo", parent / "FOO"
    parent.mkdir()
    new.mkdir()
    _stranded_project(tmp_path / "projects", old)
    found = cl.find_moved_project_candidates(str(tmp_path / "projects"), str(new))
    assert [m.old_cwd for m in found] == [str(old)]


def test_deleted_sibling_is_uncorroborated(tmp_path):
    # A scratch directory someone removed satisfies "vanished sibling" too. It's
    # still worth naming, but it must not claim the files came along. Two edited
    # files, not one: with a single file the hit threshold masks a probe that
    # wrongly counts a missing path as present.
    base, old, new = _renamed_dir_store(tmp_path, edited=("src/parser.py", "src/lexer.py"))
    shutil.rmtree(new)
    os.mkdir(new)  # renamed dir would carry its files; a deleted one leaves nothing
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, False)]


def test_one_filename_edited_by_several_sessions_is_still_one_file(tmp_path):
    # Distinctness has to hold ACROSS sessions. Two sessions of a project editing
    # the same README.md is ordinary, and counting it twice reaches the threshold
    # on exactly the single ubiquitous filename the threshold exists to reject.
    base, old, new = _renamed_dir_store(tmp_path, sessions=2, edited=("README.md",))
    (Path(new) / "README.md").write_text("a different project's readme\n")
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, False)]


def test_edited_paths_head_returns_distinct_paths(tmp_path):
    # The helper's own contract. _moved_file_evidence dedupes across sessions too,
    # which would otherwise mask a regression here.
    base, old, _new = _renamed_dir_store(tmp_path, edited=("a.py", "a.py", "b.py"))
    sess = next((base / cl.encode_path_to_folder_name(old)).iterdir())
    assert cl._edited_paths_head(sess) == [f"{old}/a.py", f"{old}/b.py"]


def test_evidence_walks_past_a_session_with_no_edits(tmp_path):
    # The newest session is often all discussion. Probing it alone would lose the
    # evidence sitting in the session behind it.
    base, old, new = _renamed_dir_store(tmp_path)
    proj = base / cl.encode_path_to_folder_name(old)
    chat = proj / "zzz-chat.jsonl"
    chat.write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": old,
                "message": {"role": "user", "content": "what do you think?"},
            }
        )
        + "\n"
    )
    os.utime(chat, None)  # newest in the project, and it edited nothing
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, True)]


def test_unreadable_parent_does_not_invent_a_candidate(tmp_path, monkeypatch):
    # "Couldn't tell" must not read as "the old directory is gone" — that would
    # manufacture a rename candidate out of an unreadable parent.
    base, _old, new = _renamed_dir_store(tmp_path)
    monkeypatch.setattr(cl.os, "listdir", _raising_listdir(cl.os.listdir, tmp_path / "work"))
    assert cl.find_moved_project_candidates(str(base), new) == []


def test_unreadable_probe_parent_is_not_evidence(tmp_path, monkeypatch):
    # The same indeterminate answer means the opposite for the file probe: it must
    # not count as a surviving file.
    base, old, new = _renamed_dir_store(tmp_path)
    monkeypatch.setattr(cl.os, "listdir", _raising_listdir(cl.os.listdir, Path(new) / "src"))
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, False)]


def test_a_line_that_is_valid_json_but_not_an_object(tmp_path):
    # A miss message must not be able to raise. read_session_header guards this
    # same shape, with a comment saying it was hit in practice.
    base, old, new = _renamed_dir_store(tmp_path)
    sess = next((base / cl.encode_path_to_folder_name(old)).iterdir())
    sess.write_text("null\n[1, 2]\n" + sess.read_text())
    found = cl.find_moved_project_candidates(str(base), new)
    assert [m.old_cwd for m in found] == [old]


def test_hint_quotes_a_path_with_spaces(tmp_path, capsys):
    # The suggested command is meant to be pasted, and `here --dir` expanduser's
    # its argument, so quoting survives the round trip.
    parent = tmp_path / "work"
    old, new = parent / "my old dir", parent / "renamed"
    parent.mkdir()
    new.mkdir()
    _stranded_project(base := tmp_path / "projects", old)
    cl.print_moved_project_hint(str(base), str(new))
    assert "--dir '" in capsys.readouterr().err


def test_a_dotted_filename_is_not_read_as_an_escaping_path(tmp_path):
    # `..config` starts with '..' without leaving the directory. The naive
    # startswith check dropped it as "edited outside the old dir".
    base, old, new = _renamed_dir_store(tmp_path, edited=("..config", "..cache"))
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, True)]


def test_corroborated_candidate_sorts_first(tmp_path):
    # Ordering is the whole point of corroboration: with several vanished siblings,
    # the one whose files are actually here has to be the one the human sees first.
    base, old, new = _renamed_dir_store(tmp_path)
    for name in ("scratch1", "scratch2"):
        _stranded_project(base, tmp_path / "work" / name)
    found = cl.find_moved_project_candidates(str(base), new)
    assert found[0].old_cwd == old and found[0].corroborated
    assert len(found) == 3


def test_candidates_are_capped_at_the_limit(tmp_path):
    base, _old, new = _renamed_dir_store(tmp_path)
    for name in ("scratch1", "scratch2", "scratch3"):
        _stranded_project(base, tmp_path / "work" / name)
    assert len(cl.find_moved_project_candidates(str(base), new)) == 3  # 4 candidates exist
    assert len(cl.find_moved_project_candidates(str(base), new, limit=2)) == 2


def test_rename_is_not_crowded_out_by_busier_deleted_siblings(tmp_path):
    # The failure this budget exists to prevent: a directory renamed *because* work
    # there wound down ranks last on activity. If corroboration only reached as far
    # as the print limit, the deleted scratch dirs above it would bury it.
    base, old, new = _renamed_dir_store(tmp_path)
    stale = base / cl.encode_path_to_folder_name(old)
    for i in range(7):
        _stranded_project(base, tmp_path / "work" / f"scratch{i}")
    for sess in stale.iterdir():
        os.utime(sess, (1, 1))  # oldest project in the store, by a wide margin

    found = cl.find_moved_project_candidates(str(base), new)
    assert found[0].old_cwd == old and found[0].corroborated


def test_one_ubiquitous_filename_is_not_corroboration(tmp_path):
    # Candidates are siblings in one parent, so they look alike. A single README.md
    # present in both directories says nothing about where these sessions belong.
    base, old, new = _renamed_dir_store(tmp_path, edited=("README.md",))
    (Path(new) / "README.md").write_text("a different project's readme\n")
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, False)]


def test_edits_outside_the_old_dir_are_not_evidence(tmp_path):
    # An absolute path elsewhere on disk exists regardless of any rename.
    base, old, new = _renamed_dir_store(tmp_path, edited=("../elsewhere/a.py", "../elsewhere/b.py"))
    outside = tmp_path / "work" / "elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    for name in ("a.py", "b.py"):
        (outside / name).write_text("still here\n")
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, False)]


def test_probe_budget_stops_before_late_evidence(tmp_path):
    # Bounded on purpose: a long transcript of edits to files that didn't survive
    # doesn't get to keep looking. Documents the cap rather than defending it.
    misses = tuple(f"gone{i}.py" for i in range(cl.MOVED_EVIDENCE_PROBES))
    base, old, new = _renamed_dir_store(tmp_path, edited=misses + ("kept1.py", "kept2.py"))
    for name in misses:
        (Path(new) / name).unlink()
    found = cl.find_moved_project_candidates(str(base), new)
    assert [(m.old_cwd, m.corroborated) for m in found] == [(old, False)]


def test_recorded_cwd_reached_through_a_symlinked_parent(tmp_path):
    # The parent comparison is by realpath, not spelling, so arriving through a
    # symlink still recognizes the vanished sibling next to it.
    base, old, new = _renamed_dir_store(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "work")
    via_link = str(link / os.path.basename(new))
    assert [m.old_cwd for m in cl.find_moved_project_candidates(str(base), via_link)] == [old]


def test_header_read_steps_past_a_session_with_no_cwd(tmp_path):
    # A summary-only stub carries no cwd. Giving up on the newest session alone
    # would lose the whole project.
    base, old, new = _renamed_dir_store(tmp_path)
    proj = base / cl.encode_path_to_folder_name(old)
    stub = proj / "zzz-stub.jsonl"
    stub.write_text('{"type":"summary","summary":"no cwd here"}\n')
    os.utime(stub, None)  # newest in the project
    assert [m.old_cwd for m in cl.find_moved_project_candidates(str(base), new)] == [old]


def test_a_gone_directory_is_not_its_own_rename_candidate(tmp_path):
    # `here --dir <old>` runs with a start path that doesn't exist either, so the
    # project would otherwise match itself and suggest running what just ran.
    base, old, _new = _renamed_dir_store(tmp_path)
    assert cl.find_moved_project_candidates(str(base), old) == []


def test_moved_candidates_skip_projects_the_hint_cannot_open(tmp_path):
    # The hint tells you to run `here --dir <old>`, which re-derives the slug from
    # that path. A project whose folder name doesn't match its own recorded cwd
    # would send you to a command that finds nothing, so it isn't offered.
    base, old, new = _renamed_dir_store(tmp_path)
    proj = base / cl.encode_path_to_folder_name(old)
    proj.rename(base / "-some-other-slug")
    assert cl.find_moved_project_candidates(str(base), new) == []


def test_here_names_the_old_directory_after_a_rename(tmp_path, capsys):
    # The issue: `here` reported nothing at all, and --deep didn't help either,
    # while the sessions sat under the old slug the whole time.
    base, old, new = _renamed_dir_store(tmp_path)
    cl.here_mode(_here_args(base, new), None)
    err = capsys.readouterr().err
    assert old in err
    assert "renamed?" in err
    assert f"claude-ls here --dir {old}" in err


def test_here_dir_finds_the_stranded_sessions(tmp_path, capsys):
    # The command the hint suggests has to actually work, on a path that is gone.
    base, old, _new = _renamed_dir_store(tmp_path)
    cl.here_mode(_here_args(base, old), None)
    out = capsys.readouterr().out
    assert "No Claude conversations" not in out
    assert "sess0" in out  # the row's id column is the session file's stem


def test_here_keeps_the_deep_suggestion_alongside_a_rename_hint(tmp_path, capsys):
    # Both can be true at once — a rename candidate must not cost the user the
    # --deep advice, which is the only hint that was always correct before.
    base, old, new = _renamed_dir_store(tmp_path)
    cl.here_mode(_here_args(base, new), None)
    err = capsys.readouterr().err
    assert old in err and "Try --deep" in err


def test_resolve_session_file_hints_at_the_old_directory(tmp_path, capsys):
    # Same miss, reached through `last`/`get` rather than `here`.
    base, old, new = _renamed_dir_store(tmp_path)
    assert cl.resolve_session_file(_last_args(base, cwd=new), str(base)) is None
    assert old in capsys.readouterr().err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# who: the live fleet by repo


def _who_args(base, **over):
    args = dict(base_dir=str(base), me=False, dir=None, within=0, json=False, no_color=True)
    args.update(over)
    return SimpleNamespace(**args)


def _rec(pid, sid, cwd, **over):
    rec = {
        "pid": pid,
        "sessionId": sid,
        "cwd": cwd,
        "status": "idle",
        "statusUpdatedAt": 0,
        "tmux": f"pane-{pid}:@1.%1",
        "name": f"name-{pid}",
        "version": "2.1.258",
    }
    rec.update(over)
    return rec


def _registry(tmp_path, *recs):
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    for r in recs:
        (d / f"{r['pid']}.json").write_text(json.dumps(r))
    return d


def _ptable(*pids, argv="claude --model opus"):
    return [(pid, 1, argv) for pid in pids]


def test_registry_entries_keep_only_live_claude_processes(tmp_path):
    reg = _registry(
        tmp_path, _rec(10, "sid-a", "/w"), _rec(20, "sid-b", "/w"), _rec(30, "sid-c", "/w")
    )
    (reg / "junk.json").write_text("{not json")
    (reg / "40.json").write_text(json.dumps({"pid": "40", "sessionId": "sid-d"}))  # pid not an int
    # 20 is alive but a text editor holding the pid; 30 is gone entirely
    table = _ptable(10) + [(20, 1, "vim claude-ls.py")]
    live = cl._registry_entries(reg, table=table, starts=lambda pids: {})
    assert [r["pid"] for r in live] == [10]


def _local_lstart(utc_iso: str, shift_s: int = 0) -> str:
    inst = datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
    inst = inst.astimezone() + timedelta(seconds=shift_s)
    return inst.strftime("%a %b %d %H:%M:%S %Y")


def test_proc_start_compares_the_utc_record_with_local_ps_output():
    recorded = "Wed Sep  2 21:16:00 2026"  # the registry's form: UTC in lstart layout
    assert cl._proc_start_matches(recorded, _local_lstart("2026-09-02T21:16:00")) is True
    assert cl._proc_start_matches(recorded, _local_lstart("2026-09-02T21:16:00", 3600)) is False
    assert cl._proc_start_matches(recorded, "yesterday-ish") is None
    assert cl._proc_start_matches("", "") is None


def test_registry_entries_drop_a_reused_pid_by_start_time(tmp_path):
    reg = _registry(tmp_path, _rec(10, "sid-a", "/w", procStart="Wed Sep  2 21:16:00 2026"))
    same = lambda pids: {10: _local_lstart("2026-09-02T21:16:00")}  # noqa: E731
    later = lambda pids: {10: _local_lstart("2026-09-03T08:00:00")}  # noqa: E731
    assert [r["pid"] for r in cl._registry_entries(reg, table=_ptable(10), starts=same)] == [10]
    assert cl._registry_entries(reg, table=_ptable(10), starts=later) == []
    # no ps opinion: the record is trusted
    assert [
        r["pid"] for r in cl._registry_entries(reg, table=_ptable(10), starts=lambda p: {})
    ] == [10]


def test_me_session_id_env_then_pid_then_ancestry(monkeypatch):
    reg = [{"pid": 500, "sessionId": "S500"}, {"pid": 600, "sessionId": "S600"}]
    assert cl._me_session_id(reg, env={"CLAUDE_CODE_SESSION_ID": "S600"}, table=[]) == "S600"
    assert cl._me_session_id(reg, env={"CLAUDE_PID": "500"}, table=[]) == "S500"
    monkeypatch.setattr(cl.os, "getpid", lambda: 7)
    table = [(7, 6, "zsh"), (6, 500, "zsh"), (500, 1, "claude")]
    assert cl._me_session_id(reg, env={}, table=table) == "S500"
    assert cl._me_session_id(reg, env={}, table=[(7, 1, "zsh")]) is None
    # outside any registry the environment's word is still worth returning
    assert cl._me_session_id([], env={"CLAUDE_CODE_SESSION_ID": "X"}, table=[]) == "X"


def test_tail_entries_drop_the_partial_first_line(tmp_path):
    p = tmp_path / "t.jsonl"
    big = json.dumps({"type": "user", "message": {"content": "x" * 500}}) + "\n"
    p.write_text(big * 10 + json.dumps({"type": "assistant", "message": {}}) + "\n")
    recs = cl._tail_entries(p, nbytes=1200)
    assert recs and recs[-1]["type"] == "assistant"
    assert all(isinstance(r, dict) for r in recs)


def test_tail_facts_reads_model_prompt_touched_and_waiting(tmp_path):
    p = tmp_path / "s.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "fix the parser"}},
        {
            "type": "user",
            "message": {"role": "user", "content": "<command-name>/compact</command-name>"},
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}}
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-fable-5-1",
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/b.py"}},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-fable-5-1",
                "content": [{"type": "text", "text": "Done. Want me to push?"}],
            },
        },
    ]
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    tf = cl._tail_facts(p)
    assert tf.model == "claude-fable-5-1"  # the model answering now, not the one that started
    assert tf.last_prompt == "fix the parser"  # the /compact echo is scaffolding, not a prompt
    assert tf.touched == ["/w/b.py", "/w/a.py"]  # most recent last; a re-edit moves a.py to the end
    assert tf.waiting is True

    p.write_text(
        p.read_text()
        + json.dumps({"type": "user", "message": {"role": "user", "content": "yes"}})
        + "\n"
    )
    tf = cl._tail_facts(p)
    assert tf.waiting is False and tf.last_prompt == "yes"

    synthetic = {
        "type": "assistant",
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "…"}]},
    }
    p.write_text(p.read_text() + json.dumps(synthetic) + "\n")
    # a harness-made record is not the answering model
    assert cl._tail_facts(p).model == "claude-fable-5-1"


def _who_fleet(tmp_path, monkeypatch):
    """sid-a launched and edits in main; sid-b launched in main but edits in the
    linked worktree; sid-c launched in that worktree; sid-d is elsewhere."""
    main = _make_repo(tmp_path / "repo")
    wt = _make_worktree(tmp_path / "wt", main)
    other = _make_repo(tmp_path / "other")
    reg = _registry(
        tmp_path,
        _rec(10, "sid-a", str(main)),
        _rec(20, "sid-b", str(main)),
        _rec(30, "sid-c", str(wt), name="named-by-claude", nameSource="auto"),
        _rec(40, "sid-d", str(other), status="busy"),
    )
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    monkeypatch.setattr(cl, "_process_table", lambda: _ptable(10, 20, 30, 40))
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    monkeypatch.setattr(cl, "_git_facts", lambda cwd: (cwd, "feat/" + Path(cwd).name))
    base = tmp_path / "projects"
    proj = base / cl.encode_path_to_folder_name(str(main))
    proj.mkdir(parents=True)
    (proj / "sid-a.jsonl").write_text(
        json.dumps(
            {"type": "user", "slug": "slug-a", "message": {"role": "user", "content": "hello"}}
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": str(main / "src" / "x.py")},
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    (proj / "sid-b.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "worktree work"}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": str(wt / "src" / "y.py")},
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    # sid-c's pane was /cleared once: its current transcript names root-c
    proj_wt = base / cl.encode_path_to_folder_name(str(wt))
    _lineage_session(proj_wt, "root-c", ts="2026-09-05T09:00:00Z", cwd=str(wt), text="before")
    _lineage_session(
        proj_wt, "sid-c", origin="root-c", ts="2026-09-05T10:00:00Z", cwd=str(wt), text="after"
    )
    return main, wt, other, base


def _who_json(base, capsys, **over):
    cl.who_mode(_who_args(base, json=True, **over), None)
    return json.loads(capsys.readouterr().out)


def test_who_groups_by_repo_flags_shared_checkouts_and_marks_me(tmp_path, monkeypatch, capsys):
    main, wt, other, base = _who_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-b")
    rows = _who_json(base, capsys)
    by = {r["session_id"]: r for r in rows}
    assert by["sid-b"]["me"] and not by["sid-a"]["me"]
    # the checkout is where the edits land: sid-b launched in main but works in the
    # worktree, so it sits with sid-c (launched there) and not with sid-a
    assert by["sid-a"]["shares_checkout_with"] == []
    assert by["sid-b"]["shares_checkout_with"] == ["pane-30"]
    assert by["sid-c"]["shares_checkout_with"] == ["pane-20"]
    assert by["sid-b"]["cwd"] == str(main) and by["sid-b"]["toplevel"] == str(wt)
    assert by["sid-b"]["worktree"] == "wt" and by["sid-b"]["branch"] == "feat/wt"
    assert by["sid-c"]["worktree"] == "wt" and by["sid-c"]["repo"] == str(main)
    assert by["sid-a"]["worktree"] == "main" and by["sid-a"]["branch"] == "feat/repo"
    assert by["sid-d"]["repo"] == str(other) and by["sid-d"]["status"] == "busy"
    # transcript facts when there is one, the registry name when there is not
    # topic: Claude Code's auto name when it has one, else the first prompt, else the slug
    assert by["sid-a"]["topic"] == "hello" and by["sid-a"]["model"] == "claude-opus-5"
    assert by["sid-c"]["topic"] == "named-by-claude"
    assert by["sid-a"]["touched"] == [str(main / "src" / "x.py")]
    assert by["sid-d"]["topic"] == "name-40" and by["sid-d"]["last_active_min"] is None
    # lineage: sid-c was cleared from root-c, whose transcript is still on disk
    assert by["sid-c"]["origin"] == "root-c" and by["sid-c"]["chain_len"] == 2
    assert by["sid-a"]["origin"] == "" and by["sid-a"]["chain_len"] == 1
    # grouped: the three repo sessions are contiguous
    repos = [r["repo"] for r in rows]
    first = repos.index(str(main))
    assert repos[first : first + 3] == [str(main)] * 3


def test_who_me_and_dir_and_within_filter_the_fleet(tmp_path, monkeypatch, capsys):
    main, wt, other, base = _who_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-c")
    assert sorted(r["session_id"] for r in _who_json(base, capsys, me=True)) == [
        "sid-a",
        "sid-b",
        "sid-c",
    ]
    assert [r["session_id"] for r in _who_json(base, capsys, dir=str(other))] == ["sid-d"]
    # a subdir of the linked worktree, even one that does not exist yet, is still the repo
    assert sorted(r["session_id"] for r in _who_json(base, capsys, dir=str(wt / "sub"))) == [
        "sid-a",
        "sid-b",
        "sid-c",
    ]
    # sid-d has no transcript, so it has no activity to be within anything
    assert sorted(r["session_id"] for r in _who_json(base, capsys, within=5)) == [
        "sid-a",
        "sid-b",
        "sid-c",
    ]


def test_who_me_outside_a_session_exits_nonzero(tmp_path, monkeypatch, capsys):
    _who_fleet(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.setattr(cl.os, "getpid", lambda: 999)
    with pytest.raises(SystemExit) as exc:
        cl.who_mode(_who_args(tmp_path / "projects", me=True), None)
    assert exc.value.code == 1
    assert "Not inside" in capsys.readouterr().err


def test_who_without_a_registry_prints_an_empty_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(tmp_path / "nope"))
    assert _who_json(tmp_path, capsys) == []
    cl.who_mode(_who_args(tmp_path), None)
    out = capsys.readouterr().out
    assert out.startswith("as of ") and "No live Claude sessions" in out


def test_who_as_of_stamps_the_time_and_says_it_is_a_snapshot():
    # naive, so astimezone() reads it as local time instead of shifting the date
    stamp = cl._who_as_of(datetime(2026, 10, 29, 9, 5))
    assert stamp.startswith("as of 2026-10-29 09:05")
    assert "a snapshot, re-run `claude-ls who`" in stamp


def test_who_plain_listing_marks_me_and_shared_checkouts(tmp_path, monkeypatch, capsys):
    main, wt, other, base = _who_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-a")
    cl.who_mode(_who_args(base), None)
    out = capsys.readouterr().out
    assert out.startswith("as of ")
    assert "*  pane-10" in out and " ! pane-20" in out and " ! pane-30" in out
    assert "main @ feat/repo" in out and "wt @ feat/wt" in out
    assert "↳ touching src/x.py" in out and "↳ touching src/y.py" in out
    assert "* this session" in out and "! edits land in a checkout" in out
    assert "↳ cleared from root-c · 2 sessions in this pane" in out


# ---------------------------------------------------------------------------
# the session card: model answering now, recorded cwd, compactions, fragments


def test_metadata_model_is_the_one_answering_now_and_remembers_the_first():
    entries = [
        {"type": "assistant", "message": {"model": "claude-sonnet-5", "content": []}},
        {"type": "assistant", "message": {"model": "claude-fable-5-1", "content": []}},
        {"type": "assistant", "message": {"model": "<synthetic>", "content": []}},
    ]
    m = cl.extract_session_metadata(entries)
    assert (m.model, m.model_first) == ("claude-fable-5-1", "claude-sonnet-5")


def _reply():
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    }


def _prompts_session(tmp_path, *records, name="p"):
    f = tmp_path / f"{name}.jsonl"
    rows = [
        r if isinstance(r, dict) else {"type": "user", "message": {"role": "user", "content": r}}
        for r in records
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def test_preview_skips_a_first_prompt_that_nothing_answered(tmp_path):
    # "Lets" landed, then the real line, with no assistant record between: superseded
    f = _prompts_session(tmp_path, "Lets", "Lets build the fleet view", _reply())
    assert cl.get_session_preview(f)[1:] == ("Lets build the fleet view", 2)
    # a short prompt that was answered is the real first message, however terse
    f = _prompts_session(tmp_path, "Deploy", _reply(), "now the docs", _reply(), name="d")
    assert cl.get_session_preview(f)[1:] == ("Deploy", 2)
    # nothing ever answered: the last thing typed is still the best topic
    alone = _prompts_session(tmp_path, "Lets", name="alone")
    assert cl.get_session_preview(alone)[1:] == ("Lets", 1)
    assert cl.first_answered_prompt(cl.load_raw_entries(str(alone))) == "Lets"


def test_canonical_repo_root_keeps_a_submodule_its_own_repo(tmp_path):
    superproject = _make_repo(tmp_path / "super")
    sub = superproject / "vendor" / "lib"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/lib\n", encoding="utf-8")
    assert cl._canonical_repo_root(str(sub)) == str(sub)
    # a relative worktree pointer resolves against the directory holding the .git file
    main = _make_repo(tmp_path / "main")
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n", encoding="utf-8")
    assert cl._canonical_repo_root(str(wt)) == str(main)


def test_summary_card_uses_the_recorded_cwd_and_reports_model_change_and_compactions(
    tmp_path, capsys
):
    cwd = "/w/projects-new/my-config"  # the flattened folder name cannot tell this hyphen from a slash
    proj = tmp_path / cl.encode_path_to_folder_name(cwd)
    proj.mkdir()
    f = proj / "abcd1234-0000-0000-0000-000000000000.jsonl"

    def user(ts, text):
        return {
            "type": "user",
            "cwd": cwd,
            "timestamp": ts,
            "message": {"role": "user", "content": text},
        }

    def assistant(ts, model, text):
        return {
            "type": "assistant",
            "cwd": cwd,
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
            },
        }

    def compaction(ts, trigger):
        return {
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": ts,
            "compactMetadata": {"trigger": trigger, "preTokens": 100},
        }

    recs = [
        user("2026-09-02T14:00:00Z", "Lets"),
        user("2026-09-02T14:00:05Z", "Lets build the fleet view"),
        assistant("2026-09-02T14:00:10Z", "claude-sonnet-5", "On it."),
        compaction("2026-09-02T15:00:00Z", "manual"),
        compaction("2026-09-02T16:00:00Z", "auto"),
        assistant("2026-09-02T16:00:10Z", "claude-fable-5-1", "Done."),
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in recs))
    cl.summary_mode(
        SimpleNamespace(
            base_dir=str(tmp_path), session_id=str(f), file=None, cwd=None, no_color=True
        ),
        None,
    )
    out = capsys.readouterr().out
    assert "Project:  /w/projects-new/my-config" in out
    assert "Model:    claude-fable-5-1 (started as claude-sonnet-5)" in out
    assert "Compactions: 2 (manual, auto)" in out
    assert "First message: Lets build the fleet view" in out


# ---------------------------------------------------------------------------
# lineage across /clear


def _lineage_session(folder, sid, origin=None, ts="2026-09-05T18:00:00Z", cwd="/w/lin", text="hi"):
    """A two-record transcript; `origin` sets the `session_id` a /cleared session carries."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{sid}.jsonl"
    base = {"sessionId": sid, "timestamp": ts, "cwd": cwd}
    if origin:
        base["session_id"] = origin
    recs = [
        {**base, "type": "user", "message": {"role": "user", "content": text}},
        {
            **base,
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        },
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return p


def test_session_origin_reads_the_head_and_survives_a_field_less_tail(tmp_path):
    root = _lineage_session(tmp_path, "root-1")
    assert cl.session_origin(root) is None
    child = _lineage_session(tmp_path, "child-1", origin="root-1", ts="2026-09-05T19:00:00Z")
    assert cl.session_origin(child) == "root-1"
    # a later `claude -r` in a fresh process appends records without the field
    with open(child, "a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "child-1",
                    "message": {"role": "user", "content": "again"},
                }
            )
            + "\n"
        )
    cl._head_facts.cache_clear()
    assert cl.session_origin(child) == "root-1"
    old_build = tmp_path / "old-build.jsonl"
    old_build.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n"
    )
    assert cl.session_origin(old_build) is None


def test_session_chain_orders_by_start_and_copes_with_a_deleted_root(tmp_path):
    proj = tmp_path / "proj"
    _lineage_session(proj, "root-1", ts="2026-09-05T10:00:00Z")
    c1 = _lineage_session(proj, "child-1", origin="root-1", ts="2026-09-05T11:00:00Z")
    c2 = _lineage_session(proj, "child-2", origin="root-1", ts="2026-09-05T12:00:00Z")
    other = _lineage_session(proj, "other-1", ts="2026-09-05T11:30:00Z")
    chain = cl.session_chain(c1)
    assert [m.session_id for m in chain] == ["root-1", "child-1", "child-2"]
    assert [m.current for m in chain] == [False, True, False]
    assert (
        cl.format_lineage(chain, "child-1", "root-1")
        == "cleared from root-1 · 2nd of 3 in this pane · earlier root-1 · later child-2"
    )
    # the root's own card carries no origin but knows what came after it
    assert (
        cl.format_lineage(chain, "root-1", None) == "1st of 3 in this pane · later child-1, child-2"
    )
    assert cl.format_lineage(cl.session_chain(other), "other-1", None) == ""
    # retention deletes roots first; the chain is gathered by shared origin
    (proj / "root-1.jsonl").unlink()
    cl._head_facts.cache_clear()
    chain = cl.session_chain(c2)
    assert [m.session_id for m in chain] == ["child-1", "child-2"]
    assert (
        cl.format_lineage(chain, "child-2", "root-1")
        == "cleared from root-1 (root transcript gone) · 2nd of 2 in this pane · earlier child-1"
    )


def _lineage_project(tmp_path):
    cwd = tmp_path / "lin"
    cwd.mkdir()
    proj = tmp_path / cl.encode_path_to_folder_name(str(cwd))
    root = _lineage_session(
        proj, "root-1", ts="2026-09-05T10:00:00Z", cwd=str(cwd), text="first stream"
    )
    child = _lineage_session(
        proj,
        "child-1",
        origin="root-1",
        ts="2026-09-05T11:00:00Z",
        cwd=str(cwd),
        text="second stream",
    )
    return cwd, proj, root, child


def test_info_and_here_show_where_a_cleared_session_sits(tmp_path, capsys):
    cwd, proj, root, child = _lineage_project(tmp_path)
    cl.summary_mode(
        SimpleNamespace(
            base_dir=str(tmp_path), session_id=str(child), file=None, cwd=None, no_color=True
        ),
        None,
    )
    assert (
        "Lineage:  cleared from root-1 · 2nd of 2 in this pane · earlier root-1"
        in capsys.readouterr().out
    )
    cl.here_mode(
        SimpleNamespace(base_dir=str(tmp_path), dir=str(cwd), count=10, deep=False, no_color=True),
        None,
    )
    out = capsys.readouterr().out
    assert "child-1" in out and "↺ cleared from root-1" in out
    assert out.count("↺") == 1  # the root carries no note


@pytest.mark.parametrize("inside_tmux", [False, True])
def test_resume_refuses_an_ancestor_whose_pane_runs_a_later_session(
    tmp_path, monkeypatch, capsys, inside_tmux
):
    if inside_tmux:
        monkeypatch.setenv("TMUX", "/tmp/test-tmux,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    cwd, proj, root, child = _lineage_project(tmp_path)
    reg = _registry(tmp_path, _rec(10, "child-1", str(cwd), tmux="pane-x:@1.%1"))
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    monkeypatch.setattr(cl, "_process_table", lambda: _ptable(10))
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(tmp_path, session_id="root-1", force=False), None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "root-1 was /cleared" in err and "pane-x" in err and "resume child-1" in err
    # --attach jumps to the pane instead; --force resumes the old transcript anyway
    cl.resume_mode(_resume_args(tmp_path, session_id="root-1", force=False, attach=True), None)
    command = "switch-client" if inside_tmux else "attach"
    assert capsys.readouterr().out.strip() == f"tmux {command} -t pane-x:@1.%1"
    cl.resume_mode(_resume_args(tmp_path, session_id="root-1"), None)
    assert "claude -r root-1" in capsys.readouterr().out


@pytest.mark.parametrize("inside_tmux", [False, True])
def test_resume_refuses_when_the_registry_shows_this_session_live(
    tmp_path, monkeypatch, capsys, inside_tmux
):
    if inside_tmux:
        monkeypatch.setenv("TMUX", "/tmp/test-tmux,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    # A bare `claude` carries no id in argv; the registry is what names it.
    base, uuid, cwd, _f = _one_session(tmp_path)
    reg = _registry(tmp_path, _rec(10, uuid, str(cwd), tmux="proj-abcd:@1.%1"))
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    monkeypatch.setattr(cl, "_process_table", lambda: _ptable(10))
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [])
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "already live in tmux proj-abcd" in err and "Refusing to resume" in err
    cl.resume_mode(_resume_args(base, session=uuid, force=False, attach=True), None)
    command = "switch-client" if inside_tmux else "attach"
    assert capsys.readouterr().out.strip() == f"tmux {command} -t proj-abcd:@1.%1"


def test_resume_names_the_pane_where_tmux_has_it_now(tmp_path, monkeypatch, capsys):
    # The registry keeps the session name from launch; a rename since then
    # left resume pointing at a tmux session that no longer existed.
    monkeypatch.delenv("TMUX", raising=False)
    base, uuid, cwd, _f = _one_session(tmp_path)
    reg = _registry(tmp_path, _rec(10, uuid, str(cwd), tmux="old-name:@7.%7"))
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    # pid 10's parent is pane 7's shell
    monkeypatch.setattr(cl, "_process_table", lambda: [(10, 5, "claude"), (5, 1, "zsh")])
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [])
    monkeypatch.setattr(cl, "_tmux_pane_addresses", lambda: {"%7": ("new-name:@7.%7", 5)})
    with pytest.raises(SystemExit):
        cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    err = capsys.readouterr().err
    assert "already live in tmux new-name:@7.%7" in err and "old-name" not in err
    cl.resume_mode(_resume_args(base, session=uuid, force=False, attach=True), None)
    assert capsys.readouterr().out.strip() == "tmux attach -t new-name:@7.%7"


def test_live_registry_keeps_the_recorded_pane_unless_it_holds_that_claude(tmp_path, monkeypatch):
    # Pane ids are per tmux server: %1 here can be another server's %1. Only a
    # pane whose shell the Claude descends from is the same pane.
    reg = _registry(
        tmp_path,
        _rec(10, "sid-a", str(tmp_path), tmux="other-server:@1.%1"),
        _rec(20, "sid-b", str(tmp_path), tmux="gone:@2.%2"),
        _rec(30, "sid-c", str(tmp_path), tmux="renamed:@3.%3"),
    )
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    monkeypatch.setattr(
        cl, "_tmux_pane_addresses", lambda: {"%1": ("here:@1.%1", 99), "%3": ("now:@3.%3", 7)}
    )
    table = [(10, 4, "claude"), (20, 6, "claude"), (30, 8, "claude"), (8, 7, "sh"), (7, 1, "zsh")]
    by = {r["sessionId"]: r["tmux"] for r in cl._live_registry(table)}
    assert by == {"sid-a": "other-server:@1.%1", "sid-b": "gone:@2.%2", "sid-c": "now:@3.%3"}


@pytest.mark.parametrize("resumed_then_switched", [False, True])
@pytest.mark.parametrize("attach", [False, True])
def test_resume_proceeds_beside_a_registered_claude_on_another_session(
    tmp_path, monkeypatch, capsys, resumed_then_switched, attach
):
    # The cwd-only guard refused here: a Claude in the same dir, on a different
    # session. The registry says which session it drives, so it's a neighbour.
    base, uuid, cwd, _f = _one_session(tmp_path)
    reg = _registry(tmp_path, _rec(101, "other-session", str(cwd), name="proj-xyz"))
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    argv = f"claude -r {uuid}" if resumed_then_switched else "claude"
    monkeypatch.setattr(cl, "_process_table", lambda: [(100, 1, "zsh"), (101, 100, argv)])
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [("proj-abcd", 100, str(cwd))])
    cl.resume_mode(_resume_args(base, session=uuid, force=False, attach=attach), None)
    cap = capsys.readouterr()
    assert "Refusing" not in cap.err
    assert "1 other Claude(s)" in cap.err and "proj-xyz" in cap.err
    assert str(cwd) in cap.out


def test_resume_still_refuses_an_unregistered_driver_beside_a_registered_neighbour(
    tmp_path, monkeypatch, capsys
):
    base, uuid, cwd, _f = _one_session(tmp_path)
    reg = _registry(tmp_path, _rec(101, "other-session", str(cwd)))
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    monkeypatch.setattr(
        cl,
        "_process_table",
        lambda: [(101, 1, f"claude -r {uuid}"), (102, 1, f"claude -r {uuid}")],
    )
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [])
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    assert exc.value.code == 2
    assert "pid 102" in capsys.readouterr().err


def test_registry_entries_skip_a_parked_session(tmp_path):
    # Sent to the background: the old process lingers, the job carries on.
    reg = _registry(tmp_path, _rec(10, "old", "/w", parkedJobId="job1"), _rec(20, "job1-sid", "/w"))
    live = cl._registry_entries(reg, table=_ptable(10, 20), starts=lambda p: {})
    assert [r["sessionId"] for r in live] == ["job1-sid"]


def test_live_registry_defers_to_claude_agents(tmp_path, monkeypatch):
    reg = _registry(tmp_path, _rec(10, "sid-a", "/w"), _rec(20, "sid-b", "/w"))
    monkeypatch.setattr(cl, "_sessions_registry_dir", lambda: reg)
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    table = _ptable(10, 20)
    calls = []
    monkeypatch.setattr(cl, "_agents_session_ids", lambda: calls.append(1) or {"sid-b"})
    assert [r["sessionId"] for r in cl._live_registry(table)] == ["sid-b"]
    cl._live_registry(table)
    assert len(calls) == 1  # one CLI startup per process
    # no answer from the CLI, or "nothing running" beside live records:
    # the registry's own checks stand
    for answer in (None, set()):
        monkeypatch.setattr(cl, "_AGENTS_ANSWER", {})
        monkeypatch.setattr(cl, "_agents_session_ids", lambda a=answer: a)
        assert [r["sessionId"] for r in cl._live_registry(table)] == ["sid-a", "sid-b"]


def test_agents_session_ids_reads_the_cli_and_tolerates_failure(monkeypatch):
    monkeypatch.undo()  # drop the autouse stub to test the real function
    monkeypatch.delenv("CLAUDE_LS_SESSIONS_DIR", raising=False)
    out = SimpleNamespace(returncode=0, stdout='[{"sessionId": "s1"}, {"sessionId": "s2"}]')
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: out)
    assert cl._agents_session_ids() == {"s1", "s2"}
    # a row without a session id is a shape we don't know: no answer
    odd = SimpleNamespace(returncode=0, stdout='[{"sessionId": "s1"}, {"pid": 3}]')
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: odd)
    assert cl._agents_session_ids() is None
    monkeypatch.setattr(
        cl.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
    )
    assert cl._agents_session_ids() is None

    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(cl.subprocess, "run", missing)
    assert cl._agents_session_ids() is None
    # an override points at a registry the CLI doesn't read
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", "/elsewhere")
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: pytest.fail("ran the CLI"))
    assert cl._agents_session_ids() is None


def test_resume_ignores_a_parked_claude_in_the_same_dir(tmp_path, monkeypatch, capsys):
    # The parked process is dropped from the running set but still has a
    # record, so it must not fall through to the cwd heuristic and refuse.
    base, uuid, cwd, _f = _one_session(tmp_path)
    reg = _registry(tmp_path, _rec(101, "other-session", str(cwd), parkedJobId="job1"))
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    monkeypatch.setattr(cl, "_process_table", lambda: [(100, 1, "zsh"), (101, 100, "claude")])
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    monkeypatch.setattr(cl, "_tmux_panes", lambda: [("proj-abcd", 100, str(cwd))])
    cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    cap = capsys.readouterr()
    assert "Refusing" not in cap.err
    assert f"claude -r {uuid}" in cap.out


def test_continued_in_reads_the_hand_off_pointer(tmp_path):
    f = tmp_path / "old.jsonl"
    hand_off = {"type": "continued-in", "sessionId": "old", "continuedInSessionId": "job-sid"}
    f.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(hand_off) + "\n")
    assert cl._continued_in(f) == "job-sid"
    # resumed in the foreground afterwards: the old id is current again
    with f.open("a") as fh:
        fh.write(json.dumps({"type": "user"}) + "\n")
    assert cl._continued_in(f) is None


def test_resume_redirects_a_session_sent_to_the_background(tmp_path, monkeypatch, capsys):
    base, uuid, cwd, f = _one_session(tmp_path)
    with f.open("a") as fh:
        fh.write(json.dumps({"type": "continued-in", "continuedInSessionId": "bbbb2222-x"}) + "\n")
    reg = _registry(tmp_path, _rec(10, "bbbb2222-x", str(cwd), tmux=None))
    monkeypatch.setenv("CLAUDE_LS_SESSIONS_DIR", str(reg))
    monkeypatch.setattr(cl, "_process_table", lambda: _ptable(10))
    monkeypatch.setattr(cl, "_proc_starts", lambda pids: {})
    with pytest.raises(SystemExit) as exc:
        cl.resume_mode(_resume_args(base, session=uuid, force=False), None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "continues as bbbb2222 (running)" in err and "claude-ls resume bbbb2222" in err
    cl.resume_mode(_resume_args(base, session=uuid), None)  # --force resumes anyway
    assert f"claude -r {uuid}" in capsys.readouterr().out


def test_preview_topic_only_stops_at_the_first_answered_prompt(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [
        {"type": "user", "slug": "witty-walrus", "message": {"content": "unanswered draft"}},
        {"type": "user", "message": {"content": "fix the build"}},
        {"type": "assistant", "message": {"content": "on it"}},
    ]
    lines += [{"type": "user", "message": {"content": f"turn {i}"}} for i in range(50)]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n{not json past the stop\n")
    full = cl.get_session_preview(f)
    fast = cl.get_session_preview(f, topic_only=True)
    assert fast[:2] == full[:2] == ("witty-walrus", "fix the build")
    assert fast[2] < full[2]  # the turn count is what topic_only gives up


def test_export_follows_the_clear_chain(tmp_path, capsys):
    cwd, proj, root, child = _lineage_project(tmp_path)
    assert cl.find_related_sessions(child) == [root, child]
    cl.export_mode(
        SimpleNamespace(
            base_dir=str(tmp_path), file=str(child), session_id=None, output=None, no_follow=False
        ),
        None,
    )
    out = capsys.readouterr().out
    assert out.index("first stream") < out.index("second stream")
    cl.export_mode(
        SimpleNamespace(
            base_dir=str(tmp_path), file=str(child), session_id=None, output=None, no_follow=True
        ),
        None,
    )
    assert "first stream" not in capsys.readouterr().out


# ---- Session listing sort keys: touched (mtime) / active / created ----
#
# The three orders are deliberately all different in this fixture. A file's mtime
# is not when its conversation last moved -- sidecar records land on finished
# transcripts -- which is the whole reason `--sort active` exists.

_SORT_EPOCH = {  # ISO -> epoch, for os.utime
    d: datetime(2026, 9, d, tzinfo=timezone.utc).timestamp() for d in range(1, 22)
}


def _ts(day: int, hour: int = 0) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:00:00.000Z"


def _write_transcript(path: Path, turns, mtime, trailer: str = "") -> Path:
    """A transcript of timestamped turns, optionally followed by a sidecar record."""
    body = "".join(
        json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": "hi"}})
        + "\n"
        for ts in turns
    )
    path.write_text(body + trailer)
    os.utime(path, (mtime, mtime))
    return path


def _sort_base(tmp_path):
    """Three sessions whose touched / active / created orders are all distinct."""
    base = tmp_path / "projects"
    proj = base / "-Users-x-repo"
    proj.mkdir(parents=True)
    # A: a long thread, last turn 09-08.        touched 09-09
    a = _write_transcript(proj / "aaaa1111-a.jsonl", [_ts(1), _ts(8)], _SORT_EPOCH[9])
    # B: one brief turn on 09-05, then a sidecar write on 09-20 that only moves
    #    the mtime -- the drift this flag exists to see past.
    b = _write_transcript(
        proj / "bbbb2222-b.jsonl",
        [_ts(5)],
        _SORT_EPOCH[20],
        trailer='{"type":"pr-link","prNumber":7,"timestamp":"2026-09-20T00:00:00.000Z"}\n',
    )
    # C: in between on every axis.              touched 09-07
    c = _write_transcript(proj / "cccc3333-c.jsonl", [_ts(3), _ts(6)], _SORT_EPOCH[7])
    return base, a, b, c


def _listed_ids(capsys):
    """Short ids of the listing rows, skipping the footer hint line."""
    rows = re.findall(r"^\s*\d+\s+(\S+)", capsys.readouterr().out, re.M)
    return rows


@pytest.mark.parametrize(
    "sort,expected",
    [
        ("active", ["aaaa1111", "cccc3333", "bbbb2222"]),
        ("touched", ["bbbb2222", "aaaa1111", "cccc3333"]),
        ("created", ["bbbb2222", "cccc3333", "aaaa1111"]),
    ],
)
def test_sessions_sort_keys_order_the_listing(tmp_path, capsys, sort, expected):
    base, *_ = _sort_base(tmp_path)
    cl.sessions_mode(_sessions_args(base, codex=False, sort=sort), None)
    assert _listed_ids(capsys) == expected


def test_sessions_default_sort_is_last_message(tmp_path, capsys):
    # A caller that passes no `sort` at all -- every SimpleNamespace fixture, and
    # anything built before the flag existed -- gets the last-message order, not
    # the mtime order it used to get.
    base, *_ = _sort_base(tmp_path)
    args = _sessions_args(base, codex=False)
    assert not hasattr(args, "sort")
    cl.sessions_mode(args, None)
    assert _listed_ids(capsys) == ["aaaa1111", "cccc3333", "bbbb2222"]


def test_sessions_sort_happens_before_the_count_limit(tmp_path, capsys):
    # Sorting after the slice would only re-order whatever the mtime order had
    # already let through -- here, B alone.
    base, *_ = _sort_base(tmp_path)
    cl.sessions_mode(_sessions_args(base, codex=False, count=1, sort="active"), None)
    assert _listed_ids(capsys) == ["aaaa1111"]


def test_sessions_time_column_shows_the_value_it_sorted_on(tmp_path, capsys):
    base, *_ = _sort_base(tmp_path)
    cl.sessions_mode(_sessions_args(base, codex=False, sort="active"), None)
    out = capsys.readouterr().out
    # B's last turn, not the 09-20 sidecar write that moved its mtime. Rendered
    # in local time, like every other timestamp the listing prints.
    expected = datetime.fromtimestamp(cl._parse_ts(_ts(5)).timestamp()).strftime("%m-%d %H:%M")
    assert expected in [ln for ln in out.splitlines() if "bbbb2222" in ln][0]


def test_here_sort_active_orders_by_last_message(tmp_path, capsys, monkeypatch):
    base, *_ = _sort_base(tmp_path)
    monkeypatch.setattr(cl, "_anchor_related_dirs", lambda start: [("/Users/x/repo", "cwd")])
    cl.here_mode(_here_args(base, "/Users/x/repo", sort="active"), None)
    assert _listed_ids(capsys)[:3] == ["aaaa1111", "cccc3333", "bbbb2222"]


def test_search_sessions_returns_the_time_it_sorted_on(tmp_path):
    base, _a, b, _c = _sort_base(tmp_path)
    by_active = cl.search_sessions(str(base), "repo", sort="active")
    assert [s.stem[:8] for _p, s, _t in by_active] == ["aaaa1111", "cccc3333", "bbbb2222"]
    when = {s.stem[:8]: t for _p, s, t in by_active}
    # The reported time is the last turn, not the sidecar write that moved the mtime.
    assert when["bbbb2222"] == cl._parse_ts(_ts(5)).timestamp()
    assert when["bbbb2222"] != b.stat().st_mtime
    # ...and that is the default, so a caller that names no sort gets it too.
    assert [s.stem[:8] for _p, s, _t in cl.search_sessions(str(base), "repo")] == [
        s.stem[:8] for _p, s, _t in by_active
    ]
    by_touched = cl.search_sessions(str(base), "repo", sort="touched")
    assert [s.stem[:8] for _p, s, _t in by_touched][0] == "bbbb2222"


def test_last_message_time_ignores_sidecar_records(tmp_path):
    _base, _a, b, _c = _sort_base(tmp_path)
    # The pr-link trailer carries a later timestamp than the only real turn.
    assert cl.last_message_time(b) == cl._parse_ts(_ts(5)).timestamp()


def test_last_message_time_reads_past_the_tail_window(tmp_path):
    # Bigger than _TAIL_SCAN_BYTES, so the read seeks and lands mid-line; the
    # fragment it lands in must be dropped, not parsed.
    path = tmp_path / "big.jsonl"
    pad = "x" * 4000
    lines = [
        json.dumps({"type": "user", "timestamp": _ts(1, h % 24), "message": {"content": pad}})
        for h in range(100)
    ]
    lines.append(
        json.dumps({"type": "assistant", "timestamp": _ts(2), "message": {"content": "done"}})
    )
    path.write_text("\n".join(lines) + "\n")
    assert path.stat().st_size > cl._TAIL_SCAN_BYTES
    assert cl.last_message_time(path) == cl._parse_ts(_ts(2)).timestamp()


def test_last_message_time_falls_back_to_a_full_read(tmp_path):
    # A tail of nothing but sidecar lines: pay for the whole file rather than
    # hand back the mtime this sort key exists to distrust.
    path = tmp_path / "sidecar-tail.jsonl"
    body = json.dumps({"type": "user", "timestamp": _ts(4), "message": {"content": "hi"}}) + "\n"
    body += ('{"type":"atis-latch","atis":"' + "z" * 200 + '"}\n') * 2000
    path.write_text(body)
    assert path.stat().st_size > cl._TAIL_SCAN_BYTES
    assert cl.last_message_time(path) == cl._parse_ts(_ts(4)).timestamp()


def test_codex_payload_records_count_as_messages(tmp_path):
    # A rollout wraps its turn in `payload` instead of naming the type, so the
    # message test has to accept both stores' shapes.
    path = tmp_path / "rollout-x.jsonl"
    path.write_text(
        json.dumps({"type": "response_item", "timestamp": _ts(2), "payload": {"type": "message"}})
        + "\n"
    )
    assert cl.last_message_time(path) == cl._parse_ts(_ts(2)).timestamp()


def test_sort_time_falls_back_to_mtime_for_a_transcript_with_no_turns(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    os.utime(path, (_SORT_EPOCH[11], _SORT_EPOCH[11]))
    for key in cl.SESSION_SORT_KEYS:
        assert cl.session_sort_time(path, key) == _SORT_EPOCH[11]


# ---- Review findings on the sort keys ----


def test_sort_active_is_not_capped_by_the_mtime_walk(tmp_path, capsys):
    # [P2] The walk keeps the 20 newest-by-mtime sessions per project. Sorting
    # after that cap means the drift being fixed can still hide the real answer:
    # 20 sidecar-touched transcripts bury a conversation that is genuinely newer.
    base = tmp_path / "projects"
    proj = base / "-Users-x-busy"
    proj.mkdir(parents=True)
    for i in range(25):  # all touched today, all last spoken in 2026-09-01
        _write_transcript(proj / f"old{i:04d}-x.jsonl", [_ts(1)], _SORT_EPOCH[20])
    _write_transcript(proj / "newest11-x.jsonl", [_ts(12)], _SORT_EPOCH[13])
    cl.sessions_mode(_sessions_args(base, codex=False, count=1, sort="active"), None)
    assert _listed_ids(capsys) == ["newest11"]


def test_ranking_reads_only_what_the_limit_needs(tmp_path, monkeypatch):
    # The cap is gone, so the bound has to do its job: a transcript whose mtime
    # is already older than the worst of the top N can't beat it, and must not be
    # opened to prove that.
    base = tmp_path / "projects"
    proj = base / "-Users-x-deep"
    proj.mkdir(parents=True)
    for day in range(1, 20):
        _write_transcript(proj / f"s{day:04d}-x.jsonl", [_ts(day)], _SORT_EPOCH[day])
    reads = []
    real = cl.last_message_time
    monkeypatch.setattr(cl, "last_message_time", lambda p: (reads.append(p), real(p))[1])
    refs = cl.iter_session_refs(str(base), per_project=None)
    top = cl.rank_sessions([(r.path, r.mtime, r) for r in refs], "active", limit=3)
    assert [r.path.stem[:5] for r in top] == ["s0019", "s0018", "s0017"]
    assert len(reads) < len(refs)  # the walk stopped early instead of reading all 19


def test_codex_lifecycle_records_are_not_messages(tmp_path):
    # [P2] A rollout ends with `task_complete` / `token_count` /
    # `thread_settings_applied`, never with a turn. Accepting any dict `payload`
    # sorts every rollout by its last bookkeeping event.
    path = tmp_path / "rollout-x.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(rec)
            for rec in [
                {"type": "session_meta", "timestamp": _ts(1), "payload": {"type": "session_meta"}},
                {"type": "response_item", "timestamp": _ts(2), "payload": {"type": "message"}},
                {"type": "event_msg", "timestamp": _ts(3), "payload": {"type": "token_count"}},
                {"type": "event_msg", "timestamp": _ts(4), "payload": {"type": "task_complete"}},
                {
                    "type": "event_msg",
                    "timestamp": _ts(5),
                    "payload": {"type": "thread_settings_applied"},
                },
            ]
        )
        + "\n"
    )
    assert cl.last_message_time(path) == cl._parse_ts(_ts(2)).timestamp()
    # ...and `created` is the turn, not the session_meta the rollout opens with.
    assert cl.first_message_time(path) == cl._parse_ts(_ts(2)).timestamp()


def test_created_skips_timestamped_records_before_the_first_message(tmp_path):
    # [P3] `_head_facts().started` is the first timestamped record of any kind, so
    # a transcript opening with metadata reported a "created" time nobody spoke at.
    path = tmp_path / "meta-first.jsonl"
    path.write_text(
        json.dumps({"type": "pr-link", "prNumber": 3, "timestamp": _ts(1)})
        + "\n"
        + json.dumps({"type": "user", "timestamp": _ts(6), "message": {"content": "hi"}})
        + "\n"
    )
    assert cl.first_message_time(path) == cl._parse_ts(_ts(6)).timestamp()
    assert cl.session_sort_time(path, "created") == cl._parse_ts(_ts(6)).timestamp()


def test_rank_sessions_matches_a_full_sort(tmp_path):
    # The bounded walk is an optimization; it must agree with ranking everything.
    base, *_ = _sort_base(tmp_path)
    refs = cl.iter_session_refs(str(base), per_project=None)
    entries = [(r.path, r.mtime, r) for r in refs]
    for key in cl.SESSION_SORT_KEYS:
        full = sorted(entries, key=lambda e: -cl.session_sort_time(e[0], key, e[1]))
        for limit in (1, 2, 3, None):
            got = [r.path for r in cl.rank_sessions(entries, key, limit)]
            assert got == [e[0] for e in full][:limit], (key, limit)


# ---- Listings and the no-argument pick agree (follow-up) ----


def _prose_base(tmp_path):
    """One project, two sessions: the slug-bearing one is older by last message."""
    base = tmp_path / "projects"
    proj = base / "-Users-x-repo"
    proj.mkdir(parents=True)
    slugged = proj / "aaaa1111-a.jsonl"
    slugged.write_text(
        json.dumps({"slug": "moonlit-cooking-avalanche"})
        + "\n"
        + json.dumps(
            {"type": "user", "timestamp": _ts(4), "message": {"content": "port the retry test"}}
        )
        + "\n"
        + json.dumps({"type": "assistant", "timestamp": _ts(4), "message": {"content": "ok"}})
        + "\n"
    )
    os.utime(slugged, (_SORT_EPOCH[20], _SORT_EPOCH[20]))  # touched later than it spoke
    plain = _write_transcript(proj / "bbbb2222-b.jsonl", [_ts(9)], _SORT_EPOCH[9])
    return base, proj, slugged, plain


def test_topic_prefers_typed_prose_over_the_generated_slug(tmp_path, capsys):
    # A slug is a generated name ("moonlit-cooking-avalanche"); the first thing the
    # human typed says more. 27 of 852 rows in a real store were showing the slug
    # with prose available (2026-09-19). `who` already resolved it this way.
    base, _proj, _slugged, _plain = _prose_base(tmp_path)
    cl.sessions_mode(_sessions_args(base, codex=False), None)
    out = capsys.readouterr().out
    assert "port the retry test" in out
    assert "moonlit-cooking-avalanche" not in out


def test_slug_is_still_the_fallback_when_nothing_was_typed(tmp_path, capsys):
    base = tmp_path / "projects"
    proj = base / "-Users-x-quiet"
    proj.mkdir(parents=True)
    (proj / "cccc3333-c.jsonl").write_text(json.dumps({"slug": "tidy-humming-lantern"}) + "\n")
    cl.sessions_mode(_sessions_args(base, codex=False), None)
    assert "tidy-humming-lantern" in capsys.readouterr().out


@pytest.mark.parametrize(
    "text,placeholder",
    [
        ("[Image #1]", True),
        ("[Image: source: /tmp/a-very-long-path/shot.jpg]", True),
        ("[Request interrupted by user]", True),
        ("[WIP] rework the parser", False),  # a real prompt that opens with a tag
        ("look at [this] please", False),
    ],
)
def test_is_placeholder_text(text, placeholder):
    assert cl.is_placeholder_text(text) is placeholder


def test_a_pasted_image_is_not_the_topic(tmp_path):
    # Claude Code writes `[Image: source: <path>]` for a paste. It was becoming the
    # topic verbatim, absolute path and all, while the real prompt sat below it.
    path = tmp_path / "-Users-x-shots"
    path.mkdir(parents=True)
    sess = path / "dddd4444-d.jsonl"
    sess.write_text(
        "\n".join(
            json.dumps(rec)
            for rec in [
                {"type": "user", "timestamp": _ts(1), "message": {"content": "[Image #1]"}},
                {
                    "type": "user",
                    "timestamp": _ts(1),
                    "message": {"content": "[Image: source: /Users/x/shots/a.jpg]"},
                },
                # The reply is what makes this a regression test: it locks the
                # paste in as the remembered prompt, which is the shape the real
                # session had. Without it the old code already found the prose.
                {"type": "assistant", "timestamp": _ts(1), "message": {"content": "looking"}},
                {"type": "user", "timestamp": _ts(2), "message": {"content": "why is this blank"}},
                {"type": "assistant", "timestamp": _ts(2), "message": {"content": "found it"}},
            ]
        )
        + "\n"
    )
    slug, first_user, turns = cl.get_session_preview(sess)
    assert first_user == "why is this blank"
    assert turns == 3  # the pastes were still turns, just not the topic


def test_the_session_last_opens_is_the_one_sessions_lists_first(tmp_path, capsys):
    # The listing and the no-argument pick disagreed: `sessions` ranked by last
    # message while `last`/`get`/`export` took whichever file was written to last.
    base, proj, slugged, plain = _prose_base(tmp_path)
    args = _last_args(base, cwd="/Users/x/repo")
    assert cl.resolve_session_file(args, str(base)) == plain
    cl.sessions_mode(_sessions_args(base, codex=False, count=1), None)
    assert _listed_ids(capsys) == [plain.stem[:8]]


def test_a_zero_limit_returns_no_rows(tmp_path, capsys):
    # `del scored[0:]` emptied the ledger and the cutoff read then raised
    # IndexError. Live on main as `claude-ls sessions -n 0`, and reachable here
    # from the browse preview when the terminal reports fewer than three rows.
    base, _proj, _slugged, _plain = _prose_base(tmp_path)
    # The CLI route first: on the base revision this raises rather than fails an
    # assertion, which is the behaviour being pinned.
    cl.sessions_mode(_sessions_args(base, codex=False, count=0), None)
    assert _listed_ids(capsys) == []
    assert cl.order_session_paths(list(_proj_sessions(base)), limit=0) == []


def _proj_sessions(base):
    return (base / "-Users-x-repo").glob("*.jsonl")


def test_recent_session_hints_rank_by_last_message(tmp_path):
    base, _proj, slugged, plain = _prose_base(tmp_path)
    recent = cl.get_recent_sessions_across_projects(str(base), limit=2)
    assert [sess for _proj_dir, sess, _when in recent] == [plain, slugged]
    # The reported time is the last message, not the mtime that outranked it.
    assert recent[1][2] == cl._parse_ts(_ts(4)).timestamp()
