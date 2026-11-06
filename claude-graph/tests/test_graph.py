"""build_session_graph over synthetic records — the artifact-anchored model.

Pure fixtures, no seam, no store: this is both the correctness suite and part of the
leak-safety guarantee (nothing real is embedded).
"""

from claude_graph.graph import (
    KIND_PRIOR,
    ArtifactGroup,
    SessionRecord,
    _first_user_text,
    _rel_rooted_key,
    artifacts_of,
    build_session_graph,
    disambiguate_basenames,
    group_label,
    group_weight,
)


def _rec(id, prs=(), branches=(), files=()):
    return SessionRecord(id=id, prs=list(prs), branches=list(branches), files=list(files))


def test_shared_pr_groups_sessions():
    g = build_session_graph(
        [
            _rec("s1", prs=["o/r#1"]),
            _rec("s2", prs=["o/r#1"]),
            _rec("s3", prs=["o/r#9"]),  # alone -> no group
        ]
    )
    assert len(g.groups) == 1
    grp = g.groups[0]
    assert grp.kind == "pr" and grp.value == "o/r#1"
    assert grp.members == ["s1", "s2"]


def test_singleton_artifacts_are_not_groups():
    g = build_session_graph([_rec("s1", prs=["o/r#1"]), _rec("s2", files=["/w/a.py"])])
    assert g.groups == []  # nothing shared by >=2


def test_multi_membership():
    # s1+s2 share a PR; s1+s3 share a file -> s1 is in two groups.
    g = build_session_graph(
        [
            _rec("s1", prs=["o/r#1"], files=["/w/a.py"]),
            _rec("s2", prs=["o/r#1"]),
            _rec("s3", files=["/w/a.py"]),
        ]
    )
    by_kind = {grp.kind: grp.members for grp in g.groups}
    assert by_kind["pr"] == ["s1", "s2"]
    assert by_kind["file"] == ["s1", "s3"]


def test_default_branch_is_not_a_link():
    g = build_session_graph([_rec("s1", branches=["main"]), _rec("s2", branches=["main"])])
    assert g.groups == []  # sharing only main -> no branch group


def test_real_shared_branch_groups():
    g = build_session_graph(
        [
            _rec("s1", branches=["main", "feat/x"]),
            _rec("s2", branches=["main", "feat/x"]),
        ]
    )
    assert [grp.value for grp in g.groups] == ["feat/x"]


def test_file_hub_suppression():
    # a file touched by 4 sessions with hub=3 is suppressed; a shared PR is not.
    recs = [_rec(f"s{i}", files=["/w/scaffold.py"], prs=["o/r#1"]) for i in range(4)]
    g = build_session_graph(recs, hub=3)
    kinds = {grp.kind for grp in g.groups}
    assert "file" not in kinds  # 4 > hub 3 -> suppressed
    assert "pr" in kinds  # PRs are never hub-suppressed


def test_fileset_merge_collapses_identical_membership():
    # s1+s2 both edited the same THREE files -> one merged group, not three.
    files = ["/w/a.py", "/w/b.py", "/w/README.md"]
    g = build_session_graph([_rec("s1", files=files), _rec("s2", files=files)])
    assert len(g.groups) == 1
    grp = g.groups[0]
    assert len(grp.keys) == 3
    assert grp.members == ["s1", "s2"]
    assert "3 files" in group_label(grp)


def test_cross_kind_merge_keeps_most_specific_first():
    # a PR and a file with identical membership merge; PR heads the label.
    g = build_session_graph(
        [
            _rec("s1", prs=["o/r#1"], files=["/w/a.py"]),
            _rec("s2", prs=["o/r#1"], files=["/w/a.py"]),
        ]
    )
    assert len(g.groups) == 1
    grp = g.groups[0]
    assert grp.kind == "pr"  # most-specific heads it
    label = group_label(grp)
    assert label.startswith("PR o/r#1")
    assert "file a.py" in label


def test_group_label_single_pr_and_file():
    assert group_label(ArtifactGroup(keys=[("pr", "o/r#1")], members=["s1", "s2"])) == "PR o/r#1"
    assert (
        group_label(ArtifactGroup(keys=[("file", "/w/x/a.py")], members=["s1", "s2"]))
        == "file a.py"
    )


def test_groups_ordered_pr_before_file():
    g = build_session_graph(
        [
            _rec("s1", prs=["o/r#1"], files=["/w/z.py"]),
            _rec("s2", prs=["o/r#1"]),
            _rec("s3", files=["/w/z.py"]),
        ]
    )
    assert [grp.kind for grp in g.groups] == ["pr", "file"]  # precedence order


def _grp(keys, members):
    return ArtifactGroup(keys=keys, members=members)


def test_group_weight_prefers_pr_over_file_same_size():
    pr = _grp([("pr", "o/r#1")], ["s1", "s2"])
    fi = _grp([("file", "/w/a.py")], ["s1", "s2"])
    assert group_weight(pr) > group_weight(fi)


def test_group_weight_scales_with_session_count():
    small = _grp([("pr", "o/r#1")], ["s1", "s2"])
    big = _grp([("pr", "o/r#1")], ["s1", "s2", "s3", "s4"])
    assert group_weight(big) > group_weight(small)


def test_group_weight_penalizes_scaffolding_but_not_code():
    code = _grp([("file", "/w/src/poller.ex")], ["s1", "s2", "s3"])
    notes = _grp([("file", "/w/docs/project_notes/MEMORY.md")], ["s1", "s2", "s3"])
    assert group_weight(code) > group_weight(notes)
    # a widely-shared scaffolding file must not outrank a small real-code hub
    big_notes = _grp([("file", "/w/MEMORY.md")], ["a", "b", "c", "d", "e"])
    small_code = _grp([("file", "/w/poller.ex")], ["a", "b"])
    assert group_weight(small_code) > group_weight(big_notes)


def test_group_weight_pr_anchored_group_is_never_scaffolding():
    # merged PR + notes file -> anchored by the PR, so no scaffold penalty
    g = _grp([("pr", "o/r#1"), ("file", "/w/MEMORY.md")], ["s1", "s2"])
    assert group_weight(g) == KIND_PRIOR["pr"] * 2


def test_disambiguate_unique_basename_stays_bare():
    d = disambiguate_basenames(["/a/repo/render.lg", "/a/repo/main.lg"])
    assert d["/a/repo/render.lg"] == "render.lg"
    assert d["/a/repo/main.lg"] == "main.lg"


def test_disambiguate_cross_repo_collision_adds_parent():
    d = disambiguate_basenames(["/root/xsofy/README.md", "/root/let-go/README.md"])
    assert d["/root/xsofy/README.md"] == "xsofy/README.md"
    assert d["/root/let-go/README.md"] == "let-go/README.md"


def test_disambiguate_walks_up_past_shared_parents():
    # same trailing dirs in different repos -> keep adding segments until unique
    d = disambiguate_basenames(["/root/a/docs/notes/MEMORY.md", "/root/b/docs/notes/MEMORY.md"])
    assert d["/root/a/docs/notes/MEMORY.md"] == "a/docs/notes/MEMORY.md"
    assert d["/root/b/docs/notes/MEMORY.md"] == "b/docs/notes/MEMORY.md"


def test_group_label_uses_file_display():
    g = _grp([("file", "/root/xsofy/README.md")], ["s1", "s2"])
    fd = {"/root/xsofy/README.md": "xsofy/README.md"}
    assert group_label(g, file_display=fd) == "file xsofy/README.md"
    assert group_label(g) == "file README.md"  # unchanged without the map


def test_rel_rooted_key_merges_worktree_copies():
    main = "/repo/xsofy"
    # main-repo copy and two worktree copies of the same relative file → one key
    k_main = _rel_rooted_key("/repo/xsofy/tools/shell.html", "/repo/xsofy", main)
    k_wt1 = _rel_rooted_key("/repo/worktrees/wt-a/tools/shell.html", "/repo/worktrees/wt-a", main)
    k_wt2 = _rel_rooted_key("/repo/worktrees/wt-b/tools/shell.html", "/repo/worktrees/wt-b", main)
    assert k_main == k_wt1 == k_wt2 == "/repo/xsofy/tools/shell.html"


def test_rel_rooted_key_keeps_distinct_repos_apart():
    a = _rel_rooted_key("/repo/x/render.lg", "/repo/x", "/repo/x")
    b = _rel_rooted_key("/repo/y/render.lg", "/repo/y", "/repo/y")
    assert a != b  # same basename, different repos — not merged


def test_rel_rooted_key_falls_back_when_unresolvable():
    # deleted/undetectable worktree (no roots) → raw path, kept distinct
    raw = "/somewhere/orphan/file.py"
    assert _rel_rooted_key(raw, None, None) == raw
    # path not under its claimed worktree root → raw path (don't guess)
    assert _rel_rooted_key(raw, "/other/root", "/canon") == raw


def test_canonical_file_keys_merge_into_one_group():
    # two sessions editing the same logical file via different worktree keys land in
    # one group (the builder is pure — it groups on whatever keys the record carries).
    g = build_session_graph(
        [
            _rec("s1", files=["/repo/xsofy/tools/shell.html"]),
            _rec("s2", files=["/repo/xsofy/tools/shell.html"]),
        ]
    )
    assert len(g.groups) == 1
    assert group_label(g.groups[0]) == "file shell.html"


def test_artifacts_of_filters_defaults():
    rec = _rec("s", prs=["o/r#1"], branches=["main", "feat/x"], files=["/w/a.py"])
    keys = artifacts_of(rec)
    assert ("branch", "main") not in keys
    assert ("branch", "feat/x") in keys
    assert ("pr", "o/r#1") in keys
    assert ("file", "/w/a.py") in keys


def test_first_user_text_extracts_and_truncates():
    assert _first_user_text("  wire   up  the grid ") == "wire up the grid"
    assert _first_user_text([{"type": "text", "text": "hello there"}]) == "hello there"
    assert _first_user_text("x" * 300, limit=10) == "x" * 10


def test_first_user_text_skips_command_wrappers():
    # A session that opened with a slash command or `!`-bash command: Claude Code
    # injects these synthetic wrappers as the first "user" messages. None is the
    # human's prompt, so each yields "" and the caller falls through to real prose.
    caveat = (
        "<local-command-caveat>Caveat: The messages below were generated by the user "
        "while running local commands. DO NOT respond to these messages…"
    )
    assert _first_user_text(caveat) == ""
    assert (
        _first_user_text("<command-name>/clear</command-name>\n<command-args></command-args>") == ""
    )
    assert _first_user_text("<bash-input>git status</bash-input>") == ""
    assert (
        _first_user_text(
            [{"type": "text", "text": "<local-command-stdout></local-command-stdout>"}]
        )
        == ""
    )
    # A real prompt that merely mentions a tag name in prose is untouched.
    assert _first_user_text("fix the <command-name> rendering bug").startswith("fix the")


def test_first_user_text_skips_task_notifications():
    # Background-task completions get routed as synthetic "user" messages too — a
    # session driven by them must not label as "<task-notification>…". (Found by
    # dogfooding on the live store; fixtures wouldn't have surfaced it.)
    notif = (
        "<task-notification>\n<task-id>bncnj6mc8</task-id>\n"
        "<tool-use-id>toolu_01Keu</tool-use-id>\n<output-file>/tmp/out</output-file>"
    )
    assert _first_user_text(notif) == ""
    assert _first_user_text("<task-id>abc</task-id>") == ""


def test_first_user_text_keeps_unknown_leading_tag():
    # Only known injection tags are stripped; genuine prose that opens with some other
    # tag-like token is prose, not a wrapper.
    assert _first_user_text("<div> isn't rendering — help").startswith("<div>")


def test_first_user_text_ignores_tool_results():
    # tool_result blocks aren't prose; skipping them leaves "" for the caller.
    assert _first_user_text([{"type": "tool_result", "content": "ok"}]) == ""
