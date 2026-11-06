"""sync: anchors accrue from what a session's tools wrote to, never from what
it only read, on the synthetic `case_sync` store."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from synthetic import case_sync
from test_cli import needs_weft, run, run_json

from workstream import anchors, cli, hooklog, seam, sessions, store, sync


@pytest.fixture
def case(synthetic_store, tmp_path, monkeypatch):
    monkeypatch.setattr(seam, "live_registry", lambda *a, **k: [])
    # gh: anchors are probed by show; the real gh would reach the network.
    monkeypatch.setattr(anchors, "_default_gh_runner", lambda argv: (1, ""))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("WORKSTREAM_DOCS_ROOT", str(tmp_path / "docs"))
    monkeypatch.setenv("COLUMNS", "400")
    return case_sync(synthetic_store)


@pytest.fixture
def seeded(case):
    """`sync-seam` holds the worker with one asserted anchor; `loom-reading`
    holds the reader, which only looked at the same repo."""
    worker, reader = case["sessions"]["worker"], case["sessions"]["reader"]
    asserted = f"gh:{case['slug']}#27"
    ws = store.create("sync-seam", "Anchors follow the work", anchors=[asserted])
    store.attach(ws, worker.ref, canonical=True, note="seed")
    other = store.create("loom-reading", "Reading loom history")
    store.attach(other, reader.ref, canonical=True, note="seed")
    return {**case, "asserted": asserted}


def _usable(found):
    return {f.ref for f in found if not f.unusable}


# --- evidence ------------------------------------------------------------------


def test_acted_records_writes_and_nothing_the_session_only_read(case):
    worker = sessions.facts(case["sessions"]["worker"].id)
    hows = {(a.kind, a.how) for a in worker.acted}
    assert ("branch", "git commit") in hows and ("branch", "git push") in hows
    assert ("pr", "gh pr create") in hows and ("pr", "gh pr merge") in hows
    assert ("pr", "gh api pulls/27/reviews") in hows
    assert not any("list" in a.how or "log" in a.how for a in worker.acted)
    assert sessions.facts(case["sessions"]["reader"].id).acted == []


def test_evidence_is_exactly_the_strong_anchors(case):
    found = sync.evidence(sessions.facts(case["sessions"]["worker"].id))
    assert _usable(found) == case["expected"]
    assert not _usable(found) & case["never"]
    by_ref = {f.ref: f for f in found}
    # A bare `gh pr merge 31` resolves through the worktree's origin to the PR it created.
    assert by_ref[f"gh:{case['slug']}#31"].how == ["gh pr create", "gh pr merge"]
    # A PR named in a prompt counts only where the session also wrote.
    assert "wrote nothing" in by_ref["gh:acme/quarry#7"].unusable


def test_a_session_that_only_read_yields_no_anchor(case):
    found = sync.evidence(sessions.facts(case["sessions"]["reader"].id))
    assert _usable(found) == set()
    assert [f.ref for f in found] == [f"gh:{case['slug']}#31"]


@pytest.mark.parametrize(
    "cmd, verb",
    [
        ("git stash push -m wip src/a.py", None),
        ("git log --grep commit", None),
        ("git -C /x/y push -u origin feat", "push"),
        ("git -C '/x y' commit -am msg", "commit"),
    ],
)
def test_git_verbs_take_only_global_options_before_them(cmd, verb):
    calls = [sessions._git_call(w, "/") for w in sessions.shell_segments(cmd)]
    assert [c[0] for c in calls if c and c[0] in ("push", "commit")] == ([verb] if verb else [])


# --- the verb ------------------------------------------------------------------


@needs_weft
def test_dry_run_reports_without_writing(seeded, capsys):
    before = set(store.get("sync-seam").refs)
    d = run_json(capsys, "sync", "sync-seam", "--dry-run")
    assert d["dry_run"] is True
    assert {r["ref"] for r in d["added"]} == seeded["expected"] - {seeded["asserted"]}
    assert {"ref": seeded["asserted"], "reason": "already an anchor"}.items() <= next(
        r for r in d["skipped"] if r["ref"] == seeded["asserted"]
    ).items()
    assert set(store.get("sync-seam").refs) == before
    code, out, _ = run(capsys, "sync", "sync-seam", "--dry-run")
    assert code == 0 and f"would add gh:{seeded['slug']}#31 to sync-seam" in out


@needs_weft
def test_sync_adds_exactly_the_evidence_then_nothing(seeded, capsys):
    worker = seeded["sessions"]["worker"]
    d = run_json(capsys, "sync", "sync-seam")
    added = {r["ref"] for r in d["added"]}
    assert added == seeded["expected"] - {seeded["asserted"]}
    assert all(r["session"] == worker.ref and r["how"] for r in d["added"])
    assert {r["ref"] for r in d["by_session"][worker.ref] if r["result"] == "added"} == added
    refs = set(store.get("sync-seam").refs)
    assert seeded["expected"] <= refs and not refs & seeded["never"]
    again = run_json(capsys, "sync", "sync-seam")
    assert again["added"] == []


@needs_weft
def test_sync_text_names_session_and_evidence(seeded, capsys):
    worker = seeded["sessions"]["worker"]
    code, out, _ = run(capsys, "sync", "sync-seam")
    assert code == 0
    line = next(x for x in out.splitlines() if x.startswith(f"added gh:{seeded['slug']}#31"))
    assert worker.ref in line and "gh pr create, gh pr merge" in line
    assert "skipped gh:acme/quarry#7" in out and "1 already anchored" in out


@needs_weft
def test_weak_signals_add_nothing(seeded, capsys):
    d = run_json(capsys, "sync", "loom-reading")
    assert d["added"] == []
    assert [r["reason"] for r in d["skipped"]] == ["it wrote nothing to acme/loom"]
    assert not any(r.startswith("gh:") for r in store.get("loom-reading").refs)


@needs_weft
def test_default_syncs_every_open_workstream(seeded, capsys):
    d = run_json(capsys, "sync")
    assert set(d["by_session"]) == {
        seeded["sessions"]["worker"].ref,
        seeded["sessions"]["reader"].ref,
    }
    assert {r["workstream"] for r in d["added"]} == {"sync-seam"}


@needs_weft
def test_session_must_be_attached(seeded, capsys):
    with pytest.raises(SystemExit, match="attached to none"):
        cli.main(["sync", "loom-reading", "--session", seeded["sessions"]["worker"].id])
    d = run_json(capsys, "sync", "--session", seeded["sessions"]["worker"].id, "--dry-run")
    assert {r["workstream"] for r in d["added"]} == {"sync-seam"}


# --- provenance ----------------------------------------------------------------


@needs_weft
def test_show_marks_synced_anchors_only(seeded, capsys):
    run_json(capsys, "sync", "sync-seam")
    pr = f"gh:{seeded['slug']}#31"
    sources = store.ref_sources("sync-seam")
    assert sources[pr] == "sync" and sources[seeded["asserted"]] == "asserted"
    code, out, _ = run(capsys, "show", "sync-seam")
    assert code == 0
    lines = out.splitlines()
    assert any(x.strip().startswith(f"{pr} (sync)") for x in lines)
    asserted = next(x for x in lines if x.strip().startswith(seeded["asserted"]))
    assert "(sync)" not in asserted
    d = json.loads(run(capsys, "show", "sync-seam", "--json")[1])
    assert d["ref_sources"][pr] == "sync"


@needs_weft
def test_reasserting_a_synced_anchor_clears_the_mark(seeded, capsys):
    run_json(capsys, "sync", "sync-seam")
    pr = f"gh:{seeded['slug']}#31"
    # weft records nothing for a ref the node already has, so the mark stays...
    store.add_anchor("sync-seam", pr)
    assert store.ref_sources("sync-seam")[pr] == "sync"
    # ...until the synced binding is retracted and the ref asserted afresh.
    store.rm_anchor("sync-seam", pr)
    assert pr not in store.ref_sources("sync-seam")
    store.add_anchor("sync-seam", pr)
    assert store.ref_sources("sync-seam")[pr] == "asserted"


# --- attach --------------------------------------------------------------------


@needs_weft
def test_attach_syncs_the_attaching_session(case, capsys):
    worker = case["sessions"]["worker"]
    code, out, _ = run(
        capsys, "attach", "--new", "Anchors", "--name", "fresh-ws", "--session", worker.id
    )
    assert code == 0
    assert f"added gh:{case['slug']}#31 to fresh-ws: {worker.ref}" in out
    refs = set(store.get("fresh-ws").refs)
    assert case["expected"] <= refs and not refs & case["never"]


@needs_weft
def test_attach_no_sync_adds_only_membership(case, capsys):
    worker = case["sessions"]["worker"]
    code, out, _ = run(
        capsys,
        "attach",
        "--new",
        "Anchors",
        "--name",
        "bare-ws",
        "--session",
        worker.id,
        "--no-sync",
    )
    assert code == 0 and "added" not in out
    assert set(store.get("bare-ws").refs) == {"ws:bare-ws", worker.ref}


@needs_weft
def test_ancillary_attach_does_not_sync(case, capsys):
    worker = case["sessions"]["worker"]
    helping = store.create("helping", "Helping out")
    code, out, _ = run(capsys, "attach", "helping", "--ancillary", "--session", worker.id)
    assert code == 0 and out.splitlines() == [f"attached {worker.ref} to helping as ancillary"]
    assert set(store.get(helping.id).refs) == {"ws:helping", worker.ref}


@needs_weft
def test_a_named_session_skips_what_its_owning_workstream_holds(seeded, capsys):
    worker = seeded["sessions"]["worker"]
    run_json(capsys, "sync", "sync-seam")
    run(capsys, "attach", "loom-reading", "--ancillary", "--session", worker.id)
    d = run_json(capsys, "sync", "loom-reading", "--session", worker.id)
    assert d["added"] == []
    assert any(r["reason"] == "an anchor of sync-seam" for r in d["skipped"])


# --- ancillary means referenced, not owned ----------------------------------------


@pytest.fixture
def helping(case):
    """The worker attached only as ancillary, to a workstream nothing else anchors."""
    ws = store.create("helping", "Helping out")
    store.attach(ws, case["sessions"]["worker"].ref, canonical=False, note="seed")
    return case


@needs_weft
def test_an_ancillary_session_is_left_out_by_default(helping, capsys):
    worker = helping["sessions"]["worker"]
    d = run_json(capsys, "sync", "helping")
    assert d["added"] == [] and d["by_session"] == {}
    assert d["ancillary"] == [{"workstream": "helping", "session": worker.ref}]
    code, out, _ = run(capsys, "sync", "helping", "--dry-run")
    assert "left out 1 ancillary session(s)" in out


@needs_weft
@pytest.mark.parametrize("how", [["--ancillary"], ["--session", "<worker>"]])
def test_an_ancillary_session_syncs_when_asked(helping, capsys, how):
    worker = helping["sessions"]["worker"]
    argv = [worker.id if a == "<worker>" else a for a in how]
    d = run_json(capsys, "sync", "helping", *argv)
    assert f"gh:{helping['slug']}#31" in {r["ref"] for r in d["added"]}


@needs_weft
def test_a_released_claim_still_owns(case, capsys):
    worker = case["sessions"]["worker"]
    ws = store.create("handed-off", "Handed off")
    store.attach(ws, worker.ref, canonical=True, note="seed")
    store.detach(ws, worker.ref)
    if not store.get("handed-off").released:
        pytest.skip("this weft predates released_claims")
    d = run_json(capsys, "sync", "handed-off", "--dry-run")
    assert f"gh:{case['slug']}#31" in {r["ref"] for r in d["added"]}


# --- pushes that landed nothing ---------------------------------------------------

_TO = "To github.com:acme/loom.git\n"


def _push_acts(command: str, output: str) -> list[sessions.Acted]:
    scan = sessions._Scan()
    scan.tools.append(sessions._Tool(None, "Bash", {"command": command}, command, "/nowhere", "t1"))
    scan.results["t1"] = (output, {})
    return sessions._acted(scan)


@pytest.mark.parametrize(
    "command, output",
    [
        (
            "git push origin feat",
            _TO + " ! [rejected]        feat -> feat (non-fast-forward)\n"
            "error: failed to push some refs to 'github.com:acme/loom.git'",
        ),
        (
            "git push origin feat",
            _TO + " ! [remote rejected] feat -> feat (pre-receive hook declined)\n"
            "error: failed to push some refs to 'github.com:acme/loom.git'",
        ),
        (
            "git push -u origin feat",
            "remote: Permission to acme/loom.git denied to someone.\n"
            "fatal: unable to access 'https://github.com/acme/loom.git/': The requested URL returned error: 403",
        ),
        ("git push origin --delete feat", _TO + " - [deleted]         feat"),
        ("git push origin :feat", _TO + " - [deleted]         feat"),
        ("git push -q origin --delete feat", ""),
        ("git push -q origin :feat", ""),
    ],
)
def test_a_push_that_landed_nothing_is_no_evidence(command, output):
    assert _push_acts(command, output) == []


def test_an_accepted_push_records_branch_and_repo():
    acts = _push_acts("git push -u origin feat", _TO + " * [new branch]      feat -> feat")
    assert [(a.kind, a.value) for a in acts] == [
        ("branch", "feat"),
        ("repo", "github.com:acme/loom.git"),
    ]
    quiet = _push_acts("git push -q origin feat", "")
    assert [(a.kind, a.value) for a in quiet] == [("branch", "feat")]


# --- tail mentions ------------------------------------------------------------------


def test_prs_named_in_an_edited_file_body_are_not_mentions(synthetic_store, case):
    loom = case["repos"]["loom"]
    body = "\n".join(f"- {case['slug']}#{n}" for n in (40, 41, 42))
    editor = synthetic_store.session(
        "sync/editor",
        loom,
        [(case["sessions"]["worker"].start, "update the changelog")],
        [
            (
                None,
                "Edit",
                {"file_path": str(loom / "CHANGELOG.md"), "old_string": "", "new_string": body},
                "The file has been updated.",
            ),
            (
                None,
                "Bash",
                {"command": f"git -C {loom} push origin rel"},
                f"{_TO} * [new branch]      rel -> rel",
            ),
        ],
        start=case["sessions"]["worker"].start,
    )
    found = sync.evidence(sessions.facts(editor.id))
    assert not {f.ref for f in found} & {f"gh:{case['slug']}#{n}" for n in (40, 41, 42)}
    assert "branch:loom@rel" in _usable(found)


def test_each_worktree_is_one_record(case):
    # The hooklog is where this transcript's EnterWorktree path lives.
    log = hooklog.load(datetime(2026, 10, 1, tzinfo=UTC))
    worker = sessions.facts(case["sessions"]["worker"].id, log=log)
    wt = str(case["worktrees"]["sync-seam"])
    assert worker.worktrees.count(wt) == 1
    found = [f for f in sync.evidence(worker) if f.ref.startswith("wt:")]
    assert len(found) == 1


# --- one shell segment at a time --------------------------------------------------


def test_a_piped_push_reads_its_own_refspec():
    acts = _push_acts(
        "git -C /x/y push -u origin feat/x 2>&1 | tail -1",
        "branch 'feat/x' set up to track 'origin/feat/x'.",
    )
    assert [(a.kind, a.value) for a in acts] == [("branch", "feat/x")]


def test_a_heredoc_body_is_not_commands():
    cmd = (
        "cat > notes.md <<'EOF'\n"
        "git push origin junk\n"
        'see o/r#12 and "acme/bramble",#319\n'
        "EOF\n"
        "git push -q origin real 2>/dev/null"
    )
    assert [(a.kind, a.value) for a in _push_acts(cmd, "")] == [("branch", "real")]
    assert "o/r#12" not in sessions.strip_heredocs(cmd)


def test_a_quoted_subject_does_not_leak_into_the_pr():
    cmd = 'gh pr merge 31 --repo acme/loom --subject "loom: say \\"why\\" (acme/bramble#319)" --squash'
    acts = _push_acts(cmd, "✓ Squashed and merged pull request #31")
    assert [(a.kind, a.value) for a in acts] == [("pr", "acme/loom#31")]


@pytest.mark.parametrize(
    "junk",
    [
        "2>",
        "a\\`.",
        "remote))",
        '{branch}",',
        'main",',
        'feat")',
        '""),',
        'rel"},',
        "x..y",
        "x.lock",
    ],
)
def test_a_malformed_branch_is_never_evidence(junk):
    assert not sessions.valid_branch(junk)


@pytest.mark.parametrize("slug", ['acme/bramble",', "acme", "a/b/c", "../x", "acme/.."])
def test_a_malformed_repo_is_never_evidence(slug):
    assert not sessions.valid_repo(slug)


def test_the_case_compound_commands_yield_only_their_pushes(case):
    found = sync.evidence(sessions.facts(case["sessions"]["worker"].id))
    refs = _usable(found)
    assert {"branch:loom@sync-docs", "branch:loom@sync-notes"} <= refs
    assert not refs & {"branch:loom@junk", f"gh:{case['slug']}#12", "gh:acme/bramble#319"}


# --- prefixes, gh globals, cut output ----------------------------------------------


def _acts(command: str, output: str) -> list[tuple[str, str]]:
    return [(a.kind, a.value) for a in _push_acts(command, output)]


def test_a_branch_named_merge_is_read_from_its_output():
    out = "✓ Squashed and merged pull request #31 (loom: fix)"
    assert _acts("gh pr merge feat/x --squash -R acme/loom", out) == [("pr", "acme/loom#31")]


@pytest.mark.parametrize(
    "command, expected",
    [
        ("timeout 60 git push -q origin feat", [("branch", "feat")]),
        ("timeout -s KILL 60 git push -q origin feat", [("branch", "feat")]),
        ("env GIT_TRACE=0 git push -q origin feat", [("branch", "feat")]),
        ("! git push -q origin feat", [("branch", "feat")]),
        ("gh --repo acme/loom pr merge 31 --squash", [("pr", "acme/loom#31")]),
        ("gh -R acme/loom pr comment 31 --body hi", [("pr", "acme/loom#31")]),
        ("gh --hostname example.invalid --repo=acme/loom pr ready 31", [("pr", "acme/loom#31")]),
        # The loop's push is recognised; `$b` is no branch name, so nothing is anchored.
        ("for b in a b; do git push -q origin $b; done", []),
    ],
)
def test_wrappers_and_gh_globals_do_not_hide_the_command(command, expected):
    assert _acts(command, "") == expected


def test_a_loop_body_is_read_as_its_command():
    words = sessions.shell_segments("for b in a b; do git push -q origin $b; done")
    assert ["git", "push", "-q", "origin", "$b"] in words


def test_a_rejected_push_cut_by_tail_is_inconclusive():
    hint = "hint: Updates were rejected because the tip of your current branch is behind"
    assert _acts("git push origin feat 2>&1 | tail -1", hint) == []
    assert _acts("git push origin feat 2>&1 | grep -v remote", "To github.com:acme/loom.git") == []


def test_an_accepted_push_cut_by_tail_keeps_its_refspec():
    out = "branch 'feat' set up to track 'origin/feat'."
    assert _acts("git push -u origin feat 2>&1 | tail -1", out) == [("branch", "feat")]
    assert _acts("git push -q origin feat 2>&1 | tail -1", "") == [("branch", "feat")]


def test_a_pipe_into_tee_still_shows_everything():
    words = sessions._segments("git push origin feat 2>&1 | tee push.log")
    assert words[0] == (["git", "push", "origin", "feat"], False)
    assert sessions._segments("git push origin feat || echo failed")[0][1] is False


# --- gone worktrees -----------------------------------------------------------------


@pytest.fixture
def retired(case, synthetic_store):
    """`retired-ws` anchors the worker's worktree, which was squash-merged and removed."""
    wt = case["worktrees"]["sync-seam"]
    ws = store.create("retired-ws", "A retired slice", anchors=[f"wt:{wt}"])
    store.attach(ws, case["sessions"]["worker"].ref, canonical=True, note="seed")
    synthetic_store.retire_worktree(wt)
    return {**case, "wt_ref": store._canon_ref(f"wt:{wt}")}


@needs_weft
def test_sync_retracts_a_gone_merged_worktree_then_nothing(retired, capsys):
    ref = retired["wt_ref"]
    d = run_json(capsys, "sync", "retired-ws", "--dry-run")
    assert [(r["ref"], r["branch"]) for r in d["retracted"]] == [(ref, "sync-seam")]
    assert d["retracted"][0]["reason"] == "gone (branch sync-seam merged)"
    assert ref in store.get("retired-ws").refs
    code, out, _ = run(capsys, "sync", "retired-ws", "--dry-run")
    assert f"would retract {ref} from retired-ws: gone (branch sync-seam merged)" in out
    assert ", 1 would retract" in out

    d = run_json(capsys, "sync", "retired-ws")
    assert [r["ref"] for r in d["retracted"]] == [ref]
    refs = store.get("retired-ws").refs
    assert ref not in refs
    # The gone worktree is never re-added from the evidence that named it.
    assert any(
        r["ref"] == ref and r["reason"] == "the worktree no longer exists" for r in d["skipped"]
    )
    again = run_json(capsys, "sync", "retired-ws")
    assert again["retracted"] == [] and ref not in store.get("retired-ws").refs


@needs_weft
def test_sync_keeps_a_gone_worktree_whose_branch_did_not_merge(case, synthetic_store, capsys):
    wt = case["worktrees"]["sync-seam"]
    ws = store.create("stranded-ws", "A stranded slice", anchors=[f"wt:{wt}"])
    store.attach(ws, case["sessions"]["worker"].ref, canonical=True, note="seed")
    synthetic_store.retire_worktree(wt, merge=False)
    d = run_json(capsys, "sync", "stranded-ws")
    assert d["retracted"] == []
    assert store._canon_ref(f"wt:{wt}") in store.get("stranded-ws").refs


@needs_weft
def test_attach_does_not_retract(retired, capsys):
    run(capsys, "attach", "retired-ws", "--session", retired["sessions"]["worker"].id)
    assert retired["wt_ref"] in store.get("retired-ws").refs


@needs_weft
def test_sync_keeps_a_worktree_merged_by_branch_name_only(case, capsys, monkeypatch):
    """No session worked in it, so only a merged PR's head name settles it:
    `frontier` stops calling it loose, but sync will not forget it."""
    gone = case["repos"]["loom"].parent / "worktrees" / "loom-fix-tempo"
    ref = store._canon_ref(f"wt:{gone}")
    store.create("by-name", "Settled by name", anchors=[ref, f"gh:{case['slug']}#12"])
    pr = {"state": "MERGED", "mergedAt": "2026-11-01T12:00:00Z", "headRefName": "fix/tempo"}
    monkeypatch.setattr(
        anchors,
        "_default_gh_runner",
        lambda argv: (0, json.dumps(pr)) if argv[1:3] == ["pr", "view"] else (1, ""),
    )
    d = run_json(capsys, "sync", "by-name")
    assert d["retracted"] == []
    [row] = [r for r in d["skipped"] if r["ref"] == ref]
    assert row["reason"] == "gone, merged by branch name only (head_only); not retracted"
    assert ref in store.get("by-name").refs
    fr = run_json(capsys, "frontier", "by-name")
    [wt] = [a for a in fr["anchors"] if a["ref"] == ref]
    assert wt["liveness"]["status"] == "gone (branch fix/tempo merged, by branch name only)"
    assert not any("is gone" in x for x in fr["closure_blockers"])
