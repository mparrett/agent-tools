#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "rich"]
# ///
"""Tests for memory-ls. Run via `uv run memory-ls/test_memory_ls.py` (or `just test`).

The module filename has a hyphen, so it can't be imported by name — load it from
its path with importlib.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("memory_ls", Path(__file__).parent / "memory-ls.py")
ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A throwaway $HOME.

    Cited paths are checked with `os.path.expanduser`, which reads $HOME, so
    moving it lets a test write `~/…` in a memory file and control whether that
    path resolves.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def write_entry(memory_dir: Path, name: str, body: str, mem_type: str = "project") -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: test entry\nmetadata:\n  type: {mem_type}\n---\n\n{body}\n"
    )
    return path


def make_store(home: Path, slug: str = "-tmp-proj") -> Path:
    """A `<projects>/<slug>/memory/` directory, returned ready to write into."""
    memory_dir = home / ".claude" / "projects" / slug / ml.MEMORY_DIRNAME
    memory_dir.mkdir(parents=True)
    return memory_dir


# --- frontmatter -------------------------------------------------------------


def test_frontmatter_reads_scalars_and_nested_type():
    fm = ml.parse_frontmatter(
        "---\nname: a-slug\ndescription: hello\nmetadata:\n  type: feedback\n---\n\nbody\n"
    )
    assert fm == {"name": "a-slug", "description": "hello", "type": "feedback"}


def test_frontmatter_absent_or_unterminated_is_empty():
    assert ml.parse_frontmatter("no frontmatter here") == {}
    assert ml.parse_frontmatter("---\nname: x\nstill going") == {}


def test_frontmatter_keys_after_metadata_block_are_not_swallowed():
    fm = ml.parse_frontmatter("---\nmetadata:\n  type: user\nname: late\n---\n")
    assert fm["type"] == "user"
    assert fm["name"] == "late"


# --- path extraction ---------------------------------------------------------


def test_templated_paths_are_not_cited():
    text = (
        "see `~/backups/foo-*.bundle` and `~/le/{cert,key}.pem` and ~/notes/…/thing.md "
        "and `~/plans/<name>.md`"
    )
    assert ml.extract_paths(text) == []


def test_glob_is_rejected_rather_than_truncated():
    """The `*` must land inside the match, or a live prefix reports as dead."""
    assert ml.extract_paths("`~/backups/agent-tools-wip-*.bundle`") == []


def test_trailing_sentence_punctuation_and_slash_are_stripped():
    assert ml.extract_paths("we keep it in ~/notes/thing.md.") == ["~/notes/thing.md"]
    assert ml.extract_paths("`~/src/proj/` and ~/src/proj") == ["~/src/proj"]


def test_code_span_keeps_a_path_containing_a_space(fake_home):
    (fake_home / "Library" / "Application Support").mkdir(parents=True)
    cited = ml.extract_paths("lives at `~/Library/Application Support/Chrome`")
    assert cited == ["~/Library/Application Support/Chrome"]


def test_command_span_cites_only_the_program(fake_home):
    """A span is a command line when the whole thing doesn't resolve but its
    first token does — otherwise a live binary reports as a dead path."""
    (fake_home / "bin").mkdir()
    (fake_home / "bin" / "lg").write_text("#!/bin/sh\n")
    assert ml.extract_paths("run `~/bin/lg -w dist main.lg`") == ["~/bin/lg"]
    assert ml.extract_paths("run `~/bin/lg test/run.lg`") == ["~/bin/lg"]


def test_space_path_that_is_gone_is_still_reported(fake_home):
    """The command rule must not swallow a genuinely dead space-containing path."""
    assert ml.extract_paths("`~/Downloads 2023/Misc/x.zip`") == ["~/Downloads 2023/Misc/x.zip"]


# --- dead paths --------------------------------------------------------------


def test_dead_paths_reports_only_the_missing_one(fake_home):
    memory_dir = make_store(fake_home)
    (fake_home / "here.md").write_text("x")
    write_entry(memory_dir, "e", "alive `~/here.md`, gone `~/nope.md`")
    findings = ml.check_dead_paths(ml.load_entries(memory_dir))
    assert [(f.kind, f.subject) for f in findings] == [("dead-path", "~/nope.md")]


# --- index drift -------------------------------------------------------------


def test_index_drift_finds_unlinked_and_dangling(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "linked", "body")
    write_entry(memory_dir, "orphan", "body")
    (memory_dir / ml.INDEX_NAME).write_text(
        "- [Linked](linked.md) — hook\n- [Ghost](ghost.md) — hook\n"
    )
    findings = ml.check_index_drift(memory_dir, ml.load_entries(memory_dir))
    assert ("unlinked", "orphan.md") in [(f.kind, f.entry) for f in findings]
    assert ("dangling", "ghost.md") in [(f.kind, f.subject) for f in findings]


def test_index_drift_ignores_prose_bullets(fake_home):
    """Some stores keep prose in MEMORY.md. Only markdown links count as index
    lines, and a non-link bullet must not read as a dangling one."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "only", "body")
    (memory_dir / ml.INDEX_NAME).write_text("# Notes\n- a plain fact\n- [Only](only.md) — hook\n")
    assert ml.check_index_drift(memory_dir, ml.load_entries(memory_dir)) == []


def test_missing_index_with_entries_is_a_finding(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    findings = ml.check_index_drift(memory_dir, ml.load_entries(memory_dir))
    assert [f.kind for f in findings] == ["dangling"]


# --- name/slug ---------------------------------------------------------------


def test_name_slug_divergence_is_reported_only_when_asked(fake_home):
    memory_dir = make_store(fake_home)
    path = write_entry(memory_dir, "on-disk-name", "body")
    path.write_text(path.read_text().replace("name: on-disk-name", "name: other-name"))
    (memory_dir / ml.INDEX_NAME).write_text("- [E](on-disk-name.md) — hook\n")

    reports, _ = ml.scan(fake_home / ".claude" / "projects")
    assert reports[0].findings == []

    reports, _ = ml.scan(fake_home / ".claude" / "projects", strict=True)
    assert [f.kind for f in reports[0].findings] == ["name-slug"]


# --- project resolution ------------------------------------------------------


def test_recorded_path_beats_reversing_the_slug(fake_home):
    """The slug can't distinguish a separator from a literal hyphen; the path
    Claude Code recorded can."""
    real = fake_home / "src" / "my-proj"
    real.mkdir(parents=True)
    project_dir = fake_home / ".claude" / "projects" / "-nonsense-slug"
    project_dir.mkdir(parents=True)
    (project_dir / "sessions-index.json").write_text(json.dumps({"originalPath": str(real)}))
    assert ml.project_path(project_dir) == real


def test_recorded_path_falls_back_to_a_session_header(fake_home):
    real = fake_home / "src" / "other"
    real.mkdir(parents=True)
    project_dir = fake_home / ".claude" / "projects" / "-nonsense-slug"
    project_dir.mkdir(parents=True)
    (project_dir / "a.jsonl").write_text(json.dumps({"type": "user", "cwd": str(real)}) + "\n")
    assert ml.project_path(project_dir) == real


def test_unresolvable_project_reports_none(fake_home):
    project_dir = fake_home / ".claude" / "projects" / "-no-such-place-anywhere"
    project_dir.mkdir(parents=True)
    assert ml.project_path(project_dir) is None
    assert "unresolved" in ml.display_path(None, "-no-such-place-anywhere")


def test_resolve_slug_prefers_a_directory_with_a_literal_hyphen(fake_home, monkeypatch):
    (fake_home / "let-go").mkdir()
    monkeypatch.chdir(fake_home)
    resolved = ml.resolve_slug(str(fake_home).replace("/", "-") + "-let-go")
    assert fake_home / "let-go" in resolved


# --- remotes and reference lookups ------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:owner/repo.git",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
        "git@github.com/owner/repo",
    ],
)
def test_remote_forms_all_parse(url):
    match = ml.REMOTE_RE.search(url)
    assert match and match.groups() == ("owner", "repo")


def test_lookup_refs_keeps_resolved_aliases_when_gh_exits_nonzero(monkeypatch):
    """`gh api graphql` exits non-zero if any alias 404s but still returns the
    ones that resolved. Trusting the exit code would drop a whole batch over one
    deleted reference."""
    payload = {
        "data": {
            "repository": {
                "r7": {"__typename": "PullRequest", "number": 7, "state": "MERGED", "title": "t"},
                "r9": None,
            }
        },
        "errors": [{"type": "NOT_FOUND"}],
    }

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, json.dumps(payload), "gh: not found")

    monkeypatch.setattr(ml.subprocess, "run", fake_run)
    states = ml.lookup_refs("owner", "repo", [7, 9])
    assert states == {7: {"state": "MERGED", "title": "t", "type": "PR"}}


def test_lookup_refs_batches_above_the_query_cap(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": {"repository": {}}}), "")

    monkeypatch.setattr(ml.subprocess, "run", fake_run)
    ml.lookup_refs("owner", "repo", list(range(1, ml.REFS_PER_QUERY + 2)))
    assert len(calls) == 2


# --- stale references --------------------------------------------------------


def stub_lookups(monkeypatch, table):
    """Stub lookup_refs with {(owner, name): {number: info}}."""
    seen = {}

    def fake(owner, name, numbers):
        seen[(owner, name)] = list(numbers)
        return {n: info for n, info in table.get((owner, name), {}).items() if n in numbers}

    monkeypatch.setattr(ml, "lookup_refs", fake)
    return seen


def check_refs(memory_dir, all_types):
    """Extract then check, the way `scan` does."""
    entries = ml.load_entries(memory_dir)
    cited = ml.extract_refs(entries, all_types, ("owner", "repo"))
    return ml.check_stale_refs(cited, ("owner", "repo"))


def test_the_advertised_count_equals_the_number_of_lookups(fake_home, monkeypatch):
    """The count and the check must come from one extraction.

    Two traversals keyed even slightly differently drift silently: an earlier
    cut counted raw matched text while the check keyed (owner, name, number),
    so `other/proj#5` and `other/proj #5` advertised 2 and looked up 1.
    """
    memory_dir = make_store(fake_home)
    write_entry(
        memory_dir,
        "e",
        "other/proj#5 and other/proj #5, plus #12 twice: #12, and our own owner/repo#12",
    )
    entries = ml.load_entries(memory_dir)
    cited = ml.extract_refs(entries, all_types=False, repo=("owner", "repo"))

    seen = stub_lookups(monkeypatch, {})
    ml.check_stale_refs(cited, ("owner", "repo"))
    assert len(cited) == sum(len(numbers) for numbers in seen.values())
    # A qualified reference naming the project's own repo keys as a bare one.
    assert set(cited) == {("other", "proj", 5), ("owner", "repo", 12)}


def test_stale_refs_flags_landed_work_and_ignores_open(fake_home, monkeypatch):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "tracked in #12, follow-up #13")
    stub_lookups(
        monkeypatch,
        {
            ("owner", "repo"): {
                12: {"state": "MERGED", "title": "done", "type": "PR"},
                13: {"state": "OPEN", "title": "live", "type": "issue"},
            }
        },
    )
    findings = check_refs(memory_dir, all_types=False)
    assert [f.subject for f in findings] == ["#12"]


def test_qualified_refs_resolve_against_the_repo_they_name(fake_home, monkeypatch):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "upstream other/proj #5 and our own #5")
    seen = stub_lookups(
        monkeypatch,
        {
            ("other", "proj"): {5: {"state": "MERGED", "title": "theirs", "type": "PR"}},
            ("owner", "repo"): {5: {"state": "MERGED", "title": "ours", "type": "PR"}},
        },
    )
    findings = check_refs(memory_dir, all_types=False)
    assert seen[("other", "proj")] == [5]
    assert seen[("owner", "repo")] == [5]
    subjects = {f.subject for f in findings}
    assert subjects == {"other/proj#5", "#5"}


def test_qualified_ref_number_is_not_reused_for_the_project_repo(fake_home, monkeypatch):
    """The qualified match is blanked before the bare sweep runs, or `other/proj
    #5` would also be looked up as this project's #5."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "see other/proj #5")
    seen = stub_lookups(monkeypatch, {})
    check_refs(memory_dir, all_types=False)
    assert ("owner", "repo") not in seen


@pytest.mark.parametrize(
    "text",
    [
        "Baseline w/o #522",  # one-character owner and repo: prose, not a repo
        "~2-3 min/PR.   #581",  # repo ends in a dot, and three spaces
        "read ~/docs/notes.md #12",  # a path, not a repo
    ],
)
def test_prose_is_not_read_as_a_cross_repo_reference(text):
    """A false qualified match costs more than a wasted lookup: the span is
    blanked before the bare sweep, so the number goes unchecked entirely."""
    assert ml.QUALIFIED_ISSUE_RE.findall(text) == []


@pytest.mark.parametrize("text", ["owner/repo#5", "owner/repo #5", "tinygo-org/tinygo#1"])
def test_genuine_cross_repo_spellings_still_match(text):
    assert len(ml.QUALIFIED_ISSUE_RE.findall(text)) == 1


def test_qualified_refs_are_checked_without_a_project_remote(fake_home, monkeypatch):
    """A project with no GitHub remote can still cite `owner/repo#123`, which
    names its own repository. Skipping the whole project left 32 resolvable
    references unchecked across the real store."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "upstream other/proj#5, and our own #9")
    seen = stub_lookups(
        monkeypatch,
        {("other", "proj"): {5: {"state": "MERGED", "title": "theirs", "type": "PR"}}},
    )
    cited = ml.extract_refs(ml.load_entries(memory_dir), all_types=False)
    findings = ml.check_stale_refs(cited, repo=None)
    assert [f.subject for f in findings] == ["other/proj#5"]
    assert ("other", "proj") in seen  # the bare #9 is dropped, not the batch


def test_remote_lookup_falls_back_past_origin(fake_home):
    """A fork often keeps its GitHub remote under another name."""
    repo_dir = fake_home / "forked"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "upstream", "git@github.com:owner/repo.git"],
        check=True,
    )
    assert ml.repo_slug(repo_dir) == ("owner", "repo")


def test_origin_wins_when_several_remotes_are_github(fake_home):
    repo_dir = fake_home / "multi"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    for name, url in [
        ("upstream", "git@github.com:them/proj.git"),
        ("origin", "git@github.com:me/proj.git"),
    ]:
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", name, url], check=True)
    assert ml.repo_slug(repo_dir) == ("me", "proj")


def test_stale_refs_are_scoped_to_project_entries_by_default(fake_home, monkeypatch):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "cited", "shipped in #4", mem_type="reference")
    table = {("owner", "repo"): {4: {"state": "MERGED", "title": "t", "type": "PR"}}}

    stub_lookups(monkeypatch, table)
    assert check_refs(memory_dir, all_types=False) == []

    stub_lookups(monkeypatch, table)
    findings = check_refs(memory_dir, all_types=True)
    assert [f.subject for f in findings] == ["#4"]


# --- scan and output ---------------------------------------------------------


def test_scan_is_read_only(fake_home):
    memory_dir = make_store(fake_home)
    entry = write_entry(memory_dir, "e", "gone `~/nope.md`")
    before = entry.read_text()
    ml.scan(fake_home / ".claude" / "projects")
    assert entry.read_text() == before
    assert not (memory_dir / ml.INDEX_NAME).exists()


def test_scan_filters_by_project_match(fake_home):
    base = fake_home / ".claude" / "projects"
    for slug in ("-tmp-alpha", "-tmp-beta"):
        memory_dir = base / slug / ml.MEMORY_DIRNAME
        memory_dir.mkdir(parents=True)
        write_entry(memory_dir, "e", "body")
        (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")
    reports, _ = ml.scan(base, only="alpha")
    assert [r.slug for r in reports] == ["-tmp-alpha"]


def test_default_run_touches_no_network(fake_home, monkeypatch):
    """Online is opt-in, so the default must not probe `gh` at all."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "tracked in #12")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")

    def explode(*args, **kwargs):
        raise AssertionError("a local run must not shell out")

    monkeypatch.setattr(ml, "gh_unavailable_reason", explode)
    monkeypatch.setattr(ml.subprocess, "run", explode)
    reports, _ = ml.scan(fake_home / ".claude" / "projects")
    assert reports[0].findings == []


def test_unchecked_references_are_advertised_not_hidden(fake_home):
    """A local scan skips a third of what the store says; saying nothing would
    read as a clean bill of health."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "tracked in #12 and other/proj #13, plus #12 again")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")
    reports, notes = ml.scan(fake_home / ".claude" / "projects")
    assert reports[0].refs == 2  # #12 deduped, other/proj #13 counted once
    assert any("--online" in note and "2 issue/PR references" in note for note in notes)


def test_no_advisory_when_nothing_cites_a_reference(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "no references here")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")
    _, notes = ml.scan(fake_home / ".claude" / "projects")
    assert notes == []


def test_advisory_respects_the_type_filter(fake_home):
    """The count has to match what --online would actually look up."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "cited", "shipped in #4", mem_type="reference")
    (memory_dir / ml.INDEX_NAME).write_text("- [C](cited.md) — hook\n")
    reports, notes = ml.scan(fake_home / ".claude" / "projects")
    assert reports[0].refs == 0 and notes == []
    reports, notes = ml.scan(fake_home / ".claude" / "projects", all_types=True)
    assert reports[0].refs == 1 and notes


def test_online_run_does_not_advertise_unchecked_references(fake_home, monkeypatch):
    """The advisory belongs to local runs. Telling someone who passed `--online`
    to re-run with `--online` is advice they already took."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "tracked in #12")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")
    stub_lookups(monkeypatch, {})
    monkeypatch.setattr(ml, "repo_slug", lambda path: ("owner", "repo"))
    _, notes = ml.scan(fake_home / ".claude" / "projects", online=True)
    assert not any("re-run with --online" in note for note in notes)


def test_online_without_working_gh_exits_before_scanning(fake_home, monkeypatch, capsys):
    """A precondition failure is not a footnote under a report that silently
    dropped the check the user asked for."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "tracked in #12")
    monkeypatch.setattr(ml, "gh_unavailable_reason", lambda: "`gh` is not installed")

    def explode(*args, **kwargs):
        raise AssertionError("must not scan when the precondition fails")

    monkeypatch.setattr(ml, "scan", explode)
    code = ml.main(["--base", str(fake_home / ".claude" / "projects"), "--online"])
    err = capsys.readouterr().err
    assert code == 2
    assert "`gh` is not installed" in err
    assert "without --online" in err


def test_gh_is_not_probed_without_online(fake_home, monkeypatch):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")

    def explode(*args, **kwargs):
        raise AssertionError("a local run must not probe gh")

    monkeypatch.setattr(ml, "gh_unavailable_reason", explode)
    assert ml.main(["--base", str(fake_home / ".claude" / "projects")]) == 0


def test_gh_failure_is_reported_in_ghs_own_words(monkeypatch):
    """`gh auth status` validates against the API, so a network blip looks
    identical to a missing login unless the real error is passed through."""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, "", "error connecting to api.github.com\ncheck your internet connection\n"
        )

    monkeypatch.setattr(ml.subprocess, "run", fake_run)
    assert "error connecting to api.github.com" in ml.gh_unavailable_reason()


GH_INVALID_TOKEN_OUTPUT = """github.com
  X Failed to log in to github.com account mparrett (default)
  - Active account: true
  - The token in default is invalid.
  - To re-authenticate, run: gh auth login -h github.com
  - To forget about this account, run: gh auth logout -h github.com -u mparrett
"""


def test_diagnosis_is_the_failure_not_the_last_line():
    """Verbatim output from a real invalid-token case. `gh` ends with logout
    housekeeping, so the tail is the least useful line in it."""
    reason = ml._gh_diagnosis(GH_INVALID_TOKEN_OUTPUT)
    assert "Failed to log in" in reason
    assert "gh auth login -h github.com" in reason
    assert "logout" not in reason


def test_diagnosis_falls_back_to_the_last_line_when_nothing_matches():
    assert ml._gh_diagnosis("something unexpected\nfinal word") == "final word"


def test_diagnosis_survives_empty_output():
    assert ml._gh_diagnosis("") == "no output"


def test_missing_gh_binary_says_so(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(ml.subprocess, "run", fake_run)
    assert ml.gh_unavailable_reason() == "`gh` is not installed"


def test_working_gh_reports_no_reason(monkeypatch):
    monkeypatch.setattr(
        ml.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "logged in", "")
    )
    assert ml.gh_unavailable_reason() is None


def test_narrow_terminal_still_prints_what_a_finding_is_about(fake_home):
    """At 44 columns the subject column used to collapse to zero width, so a
    dead-path finding printed with no path in it."""
    from rich.console import Console

    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "a-fairly-long-entry-filename", "gone `~/nope/really/gone.md`")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](a-fairly-long-entry-filename.md) — hook\n")
    reports, notes = ml.scan(fake_home / ".claude" / "projects")

    import io

    buffer = io.StringIO()
    ml.render(Console(file=buffer, width=44, force_terminal=False), reports, notes, strict=False)
    assert "nope" in buffer.getvalue()


def test_skip_lines_name_the_project_readably(fake_home, monkeypatch):
    """A skip line is the only place a project is named without its findings
    under it, so the flattened slug can't be decoded from context."""
    import io

    from rich.console import Console

    memory_dir = make_store(fake_home, slug="-Users-someone-src-my-proj")
    write_entry(memory_dir, "e", "tracked in #12")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")
    monkeypatch.setattr(ml, "gh_unavailable_reason", lambda: None)
    monkeypatch.setattr(ml, "repo_slug", lambda path: None)

    reports, notes = ml.scan(fake_home / ".claude" / "projects", online=True)
    assert reports[0].skipped, "expected a no-remote skip"

    buffer = io.StringIO()
    ml.render(Console(file=buffer, width=120), reports, notes, strict=False)
    rendered = buffer.getvalue()
    assert "-Users-someone-src-my-proj:" not in rendered
    assert reports[0].path in rendered


def test_json_carries_the_reference_count(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "tracked in #12")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")
    reports, notes = ml.scan(fake_home / ".claude" / "projects")
    payload = json.loads(ml.as_json(reports, notes))
    assert payload["projects"][0]["references"] == 1


def test_unmatched_filter_is_distinguished_from_an_empty_store(fake_home, capsys):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    assert ml.main(["--base", str(fake_home / ".claude" / "projects"), "--project", "nope"]) == 2
    assert "no project matches" in capsys.readouterr().err


def test_json_payload_totals_match_findings(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "gone `~/nope.md`")
    reports, notes = ml.scan(fake_home / ".claude" / "projects")
    payload = json.loads(ml.as_json(reports, notes))
    assert payload["totals"] == {"projects": 1, "entries": 1, "findings": 2}
    assert {f["kind"] for f in payload["projects"][0]["findings"]} == {"dead-path", "dangling"}


def test_empty_base_exits_nonzero(tmp_path, capsys):
    assert ml.main(["--base", str(tmp_path / "nothing")]) == 2


# --- search ------------------------------------------------------------------


def test_search_matches_body_lines_with_line_numbers(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "first line\nmentions worktrees here\nlast")
    matches = ml.search_entries(memory_dir, re.compile("worktree", re.I))
    assert [(m.entry, m.line_no, m.line) for m in matches] == [
        ("e.md", 9, "mentions worktrees here")
    ]


def test_search_is_case_insensitive_and_takes_a_regex(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "Uses WORKTREE hygiene")
    assert ml.search_entries(memory_dir, re.compile("work(tree|space)", re.I))


def test_frontmatter_only_match_reports_the_entry_once(fake_home):
    """The header already prints name and description, so repeating those lines
    under it says the same thing twice."""
    memory_dir = make_store(fake_home)
    path = write_entry(memory_dir, "worktree-hygiene", "body says nothing relevant")
    path.write_text(path.read_text().replace("test entry", "how to handle a worktree"))
    matches = ml.search_entries(memory_dir, re.compile("worktree", re.I))
    assert len(matches) == 1
    assert matches[0].line_no == 0  # header stands in for the hit
    assert matches[0].description == "how to handle a worktree"


def test_search_includes_the_index(fake_home):
    """Eight of fifty surveyed projects keep every fact in MEMORY.md and have no
    entry files, so skipping the index returns nothing for them."""
    memory_dir = make_store(fake_home)
    (memory_dir / ml.INDEX_NAME).write_text("# Notes\n- a fact about worktrees\n")
    matches = ml.search_entries(memory_dir, re.compile("worktree", re.I))
    assert [(m.entry, m.mem_type) for m in matches] == [(ml.INDEX_NAME, "index")]


def test_search_groups_by_project_and_skips_empty_ones(fake_home):
    base = fake_home / ".claude" / "projects"
    for slug, body in (("-tmp-alpha", "has worktree talk"), ("-tmp-beta", "unrelated")):
        memory_dir = base / slug / ml.MEMORY_DIRNAME
        memory_dir.mkdir(parents=True)
        write_entry(memory_dir, "e", body)
    results = ml.search(base, re.compile("worktree", re.I))
    assert [r.slug for r in results] == ["-tmp-alpha"]


def test_search_honours_the_project_filter(fake_home):
    base = fake_home / ".claude" / "projects"
    for slug in ("-tmp-alpha", "-tmp-beta"):
        memory_dir = base / slug / ml.MEMORY_DIRNAME
        memory_dir.mkdir(parents=True)
        write_entry(memory_dir, "e", "worktree")
    assert [r.slug for r in ml.search(base, re.compile("worktree", re.I), only="beta")] == [
        "-tmp-beta"
    ]


def test_search_exits_nonzero_when_nothing_matches(fake_home, capsys):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "nothing here")
    code = ml.main(["--base", str(fake_home / ".claude" / "projects"), "--search", "worktree"])
    assert code == 1
    assert "no memory mentions" in capsys.readouterr().out


def test_invalid_search_regex_is_rejected(fake_home, capsys):
    """Reported even when the store is empty: a bad pattern is wrong regardless
    of what is on disk, and it is the error the user can act on."""
    code = ml.main(["--base", str(fake_home / ".claude" / "projects"), "--search", "a[b"])
    assert code == 2
    assert "not valid regex" in capsys.readouterr().err


def test_search_rejects_check_only_flags(fake_home, capsys):
    """Silently dropping them would answer a narrower question than was asked."""
    make_store(fake_home)
    code = ml.main(
        [
            "--base",
            str(fake_home / ".claude" / "projects"),
            "--search",
            "x",
            "--strict",
        ]
    )
    assert code == 2
    assert "--strict" in capsys.readouterr().err


def test_search_json_carries_entry_context(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "mentions worktrees", mem_type="reference")
    results = ml.search(fake_home / ".claude" / "projects", re.compile("worktree", re.I))
    payload = json.loads(ml.search_as_json(results, "worktree"))
    match = payload["projects"][0]["matches"][0]
    assert match["entry"] == "e.md"
    assert match["type"] == "reference"
    assert payload["totals"] == {"projects": 1, "matches": 1}


def test_line_numbers_are_true_without_frontmatter(fake_home):
    memory_dir = make_store(fake_home)
    (memory_dir / "bare.md").write_text("first\nmentions worktree\n")
    matches = ml.search_entries(memory_dir, re.compile("worktree", re.I))
    assert [(m.line_no, m.line) for m in matches] == [(2, "mentions worktree")]


def test_line_numbers_are_true_with_unterminated_frontmatter(fake_home):
    """A stray opening fence must not swallow the file: treat it as no
    frontmatter so everything below stays searchable."""
    memory_dir = make_store(fake_home)
    (memory_dir / "odd.md").write_text("---\nname: x\nmentions worktree\n")
    matches = ml.search_entries(memory_dir, re.compile("worktree", re.I))
    assert [(m.line_no, m.line) for m in matches] == [(3, "mentions worktree")]


def test_a_later_horizontal_rule_is_not_read_as_a_fence(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "intro\n\n---\n\nmentions worktree")
    matches = ml.search_entries(memory_dir, re.compile("worktree", re.I))
    assert [m.line for m in matches] == ["mentions worktree"]


def test_one_frontmatter_boundary_serves_parser_and_search():
    """Two copies of this predicate disagreeing by a `.strip()` leaves a stretch
    of a file that neither the parse nor the search reads."""
    text = "  ---\nname: x\ndescription: hidden worktree fact\n---\nbody\n"
    lines = text.splitlines()
    parsed = ml.parse_frontmatter(text)
    assert bool(parsed) == bool(ml.body_start(lines))
    assert parsed.get("description") == "hidden worktree fact"


def test_quoted_frontmatter_values_are_unquoted():
    """The quotes travel: descriptions print beside every hit and ship in --json."""
    fm = ml.parse_frontmatter('---\nname: x\ndescription: "Protocol for X"\n---\n')
    assert fm["description"] == "Protocol for X"
    assert ml.parse_frontmatter("---\ndescription: 'single'\n---\n")["description"] == "single"
    assert ml.parse_frontmatter('---\ndescription: un"quoted\n---\n')["description"] == 'un"quoted'


def test_a_mistyped_scope_is_not_answered_as_an_empty_result(fake_home, capsys):
    """`no memory mentions X` is an answer about the store. A bad --base or
    --project never asked the store anything."""
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "mentions worktree")
    base = fake_home / ".claude" / "projects"

    assert ml.main(["--base", str(base / "nope"), "--search", "worktree"]) == 2
    assert "no memory stores found" in capsys.readouterr().err

    assert ml.main(["--base", str(base), "--project", "zzz", "--search", "worktree"]) == 2
    assert "no project matches" in capsys.readouterr().err

    # A real store, a real project, genuinely no hits — that is exit 1.
    assert ml.main(["--base", str(base), "--search", "nothing-matches-this"]) == 1


# --- index prose -------------------------------------------------------------


def test_index_prose_fires_when_facts_and_pointers_are_mixed(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    (memory_dir / ml.INDEX_NAME).write_text(
        "- [E](e.md) — hook\n- a fact written inline\n- another one\n"
    )
    findings = ml.check_index_prose(memory_dir, ml.load_entries(memory_dir))
    assert [f.kind for f in findings] == ["index-prose"]
    assert findings[0].subject == "2 of 3 lines"
    assert "every session" in findings[0].detail


def test_index_prose_ignores_a_store_that_never_split_into_entries(fake_home):
    """Eight of fifty surveyed projects keep everything in the index and have no
    entry files. That is a different convention, not drift — reporting it would
    bury the four projects where facts and pointers genuinely mixed."""
    memory_dir = make_store(fake_home)
    (memory_dir / ml.INDEX_NAME).write_text("- a fact\n- another fact\n")
    assert ml.check_index_prose(memory_dir, ml.load_entries(memory_dir)) == []


def test_index_prose_silent_on_a_pure_pointer_index(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    (memory_dir / ml.INDEX_NAME).write_text("# Notes\n\n- [E](e.md) — hook\n")
    assert ml.check_index_prose(memory_dir, ml.load_entries(memory_dir)) == []


def test_index_prose_is_on_by_default(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n- inline fact\n")
    reports, _ = ml.scan(fake_home / ".claude" / "projects")
    assert "index-prose" in {f.kind for f in reports[0].findings}


def test_index_cost_is_reported_even_when_nothing_is_wrong(fake_home):
    """The standing bill of the store, whether or not it has findings."""
    import io

    from rich.console import Console

    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n" * 100)
    reports, notes = ml.scan(fake_home / ".claude" / "projects")
    assert not reports[0].findings

    buffer = io.StringIO()
    ml.render(Console(file=buffer, width=100), reports, notes, strict=True)
    assert "loaded every session" in buffer.getvalue()


def test_json_carries_the_index_size(fake_home):
    memory_dir = make_store(fake_home)
    write_entry(memory_dir, "e", "body")
    (memory_dir / ml.INDEX_NAME).write_text("- [E](e.md) — hook\n")
    reports, notes = ml.scan(fake_home / ".claude" / "projects")
    payload = json.loads(ml.as_json(reports, notes))
    assert payload["projects"][0]["index_chars"] == len((memory_dir / ml.INDEX_NAME).read_text())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
