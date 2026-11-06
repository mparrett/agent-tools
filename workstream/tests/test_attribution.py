"""attribution: each ranking signal with its weight and reason, built from
hand-made SessionFacts and Workstreams; no weft and no session store."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from workstream import attribution as A
from workstream import seam
from workstream.attribution import Membership, is_scaffolding, mentions, rank, tail_text
from workstream.clock import Instant
from workstream.hooklog import MessageEvent
from workstream.sessions import SessionFacts
from workstream.store import Workstream

ME = "0a1b2c3d-1111-4111-8111-000000000001"
S1 = "5e6f7a8b-2222-4222-8222-000000000002"
S2 = "9c0d1e2f-3333-4333-8333-000000000003"
S3 = "d4e5f6a7-4444-4444-8444-000000000004"
S4 = "b8c9d0e1-5555-4555-8555-000000000005"
S5 = "f2a3b4c5-6666-4666-8666-000000000006"
PR = "acme/trellis#57"
AT = Instant(datetime(2026, 10, 29, 12, 59, tzinfo=UTC), timedelta(hours=-5))


def ws(name: str, *refs: str, title: str | None = None) -> Workstream:
    return Workstream(
        id=f"id-{name}",
        name=name,
        title=title or name,
        state="open",
        created=None,
        touched=None,
        refs=list(refs),
    )


@pytest.fixture
def mk(tmp_path):
    """SessionFacts factory whose transcript path does not exist, so the
    mention signal stays silent unless a test writes one."""

    def make(sid: str = ME, **kw) -> SessionFacts:
        kw.setdefault("path", str(tmp_path / f"{sid}.jsonl"))
        kw.setdefault("kind", "claude")
        return SessionFacts(id=sid, **kw)

    return make


def work(**kw) -> seam.WorkSignals:
    return seam.WorkSignals(**kw)


def only(cands, name):
    hits = [c for c in cands if c.ws.name == name]
    assert len(hits) == 1, [(c.ws.name, c.score, c.reasons) for c in cands]
    return hits[0]


# --- pasted ids --------------------------------------------------------------


def test_pasted_id_scores_one_and_cites_when(mk):
    c = only(rank(mk(pasted_sessions=[(S1, AT)]), [ws("seam", f"claude:{S1}")]), "seam")
    assert c.score == A.W_PASTED == 1.0
    assert c.reasons == [f"pasted session {S1[:8]} at {AT.iso()}"]


def test_pasted_prefix_resolves_and_missing_time_says_so(mk):
    c = only(rank(mk(pasted_sessions=[(S1[:8], None)]), [ws("seam", f"claude:{S1}")]), "seam")
    assert c.score == 1.0
    assert c.reasons == [f"pasted session {S1[:8]} at unknown time"]


def test_pasted_ids_cap_at_three_hits(mk):
    ids = [S1, S2, S3, S4, S5]
    w = ws("seam", *(f"claude:{s}" for s in ids))
    c = only(rank(mk(pasted_sessions=[(s, AT) for s in ids]), [w]), "seam")
    assert c.score == A.W_PASTED_MAX == 3.0
    assert len(c.reasons) == 3


# --- lineage and parent ------------------------------------------------------


def test_origin_is_lineage(mk):
    c = only(rank(mk(origin=S1), [ws("seam", f"claude:{S1}")]), "seam")
    assert c.score == A.W_LINEAGE
    assert c.reasons == [f"same pane lineage {S1[:8]} is attached"]


def test_chain_counts_once_and_skips_self(mk):
    # origin repeated in chain, and the session's own id in its chain.
    s = mk(origin=S1, chain=[S1, ME])
    c = only(rank(s, [ws("seam", f"claude:{S1}", f"claude:{ME}")]), "seam")
    assert c.score == A.W_LINEAGE
    assert c.reasons == ["already attached", f"same pane lineage {S1[:8]} is attached"]


def test_fork_sibling_is_named_as_such(mk):
    c = only(rank(mk(fork_siblings=[S2]), [ws("seam", f"codex:{S2}")]), "seam")
    assert c.score == A.W_LINEAGE
    assert c.reasons == [f"fork sibling {S2[:8]} is attached"]


@pytest.mark.parametrize("kind", ["subagent", "codex"])
def test_subagent_inherits_parent(mk, kind):
    c = only(rank(mk("ag01", kind=kind, parent_id=S1), [ws("seam", f"claude:{S1}")]), "seam")
    assert c.score == A.W_PARENT == 0.95
    assert c.reasons == [f"parent session {S1[:8]} is attached"]


def test_parent_id_on_a_main_session_is_ignored(mk):
    assert rank(mk(parent_id=S1), [ws("seam", f"claude:{S1}")]) == []


# --- anchors touched ---------------------------------------------------------


def test_pr_anchor_touched(mk):
    c = only(rank(mk(work=work(prs=[PR])), [ws("seam", f"gh:{PR}")]), "seam")
    assert c.score == A.W_PR
    assert c.reasons == [f"touched {PR}, an anchor here"]


def test_branch_anchor_uses_repo_basename(mk, tmp_path):
    s = mk(work=work(branches=["feat/mixer-seam"]), repos=[str(tmp_path / "orchard")])
    c = only(rank(s, [ws("seam", "branch:orchard@feat/mixer-seam")]), "seam")
    assert c.score == A.W_BRANCH
    assert c.reasons == ["checked out branch orchard@feat/mixer-seam, an anchor here"]


def test_branch_anchor_falls_back_to_cwd_basename(mk, tmp_path):
    s = mk(work=work(branches=["feat/mixer-seam"]), cwd=str(tmp_path / "orchard"))
    c = only(rank(s, [ws("seam", "branch:orchard@feat/mixer-seam")]), "seam")
    assert c.score == A.W_BRANCH


def test_branch_in_another_repo_does_not_match(mk, tmp_path):
    s = mk(work=work(branches=["feat/mixer-seam"]), repos=[str(tmp_path / "trellis")])
    assert rank(s, [ws("seam", "branch:orchard@feat/mixer-seam")]) == []


def test_worktree_anchor_from_workspace_events(mk, tmp_path):
    wt = tmp_path / "kiln" / ".claude" / "worktrees" / "fix-track"
    wt.mkdir(parents=True)
    c = only(rank(mk(worktrees=[str(wt)]), [ws("seam", f"wt:{wt}")]), "seam")
    assert c.score == A.W_WORKTREE
    assert c.reasons == [f"worked in worktree {wt.resolve()}, an anchor here"]


def test_worktree_anchor_from_cwd(mk, tmp_path):
    wt = tmp_path / "scratch-clone"
    wt.mkdir()
    c = only(rank(mk(cwd=str(wt)), [ws("seam", f"wt:{wt}")]), "seam")
    assert c.score == A.W_WORKTREE


def test_cwd_alone_is_not_a_repo_signal(mk, tmp_path):
    repo = tmp_path / "orchard"
    other = mk(S1, repos=[str(repo)])
    assert rank(mk(cwd=str(repo)), [ws("seam", f"claude:{S1}")], [other]) == []


# --- messages ----------------------------------------------------------------


def _msg(frm: str, to: str | None) -> MessageEvent:
    return MessageEvent(at=AT, from_sid=frm, to_raw="pane-x", summary="hi", body="hi", to_sid=to)


@pytest.mark.parametrize("frm,to", [(ME, S1), (S1, ME)])
def test_message_with_attached_session(mk, frm, to):
    c = only(rank(mk(), [ws("seam", f"claude:{S1}")], messages=[_msg(frm, to)]), "seam")
    assert c.score == A.W_MESSAGE
    assert c.reasons == [f"messaged {S1[:8]}, which is attached"]


def test_unresolved_or_unrelated_messages_are_ignored(mk):
    msgs = [_msg(ME, None), _msg(S2, S3)]
    assert rank(mk(), [ws("seam", f"claude:{S1}")], messages=msgs) == []


# --- repo overlap and files --------------------------------------------------


def test_repo_overlap_one_repo(mk, tmp_path):
    r = str(tmp_path / "orchard")
    c = only(rank(mk(repos=[r]), [ws("seam", f"claude:{S1}")], [mk(S1, repos=[r])]), "seam")
    assert c.score == pytest.approx(A.W_REPO)
    assert c.reasons == ["acted in orchard, where its sessions worked"]


def test_repo_overlap_caps(mk, tmp_path):
    rs = [str(tmp_path / n) for n in ("orchard", "trellis", "kiln")]
    c = only(rank(mk(repos=rs), [ws("seam", f"claude:{S1}")], [mk(S1, repos=rs)]), "seam")
    assert c.score == pytest.approx(A.W_REPO_MAX)


def test_repo_overlap_needs_attached_sessions(mk, tmp_path):
    # An anchor-only workstream has no sessions whose repos could overlap.
    wt = tmp_path / "orchard" / ".claude" / "worktrees" / "w"
    s = mk(repos=[str(tmp_path / "orchard")])
    assert rank(s, [ws("seam", f"wt:{wt}")]) == []


def test_shared_files_are_down_weighted_and_skip_scaffolding(mk, tmp_path):
    r = tmp_path / "orchard"
    real = [str(r / "src/a.py"), str(r / "src/b.py")]
    scaff = [str(r / "CLAUDE.md"), str(r / "README.md"), str(tmp_path / "x/memory/notes.md")]
    mine = mk(work=work(edited_paths=real + scaff))
    theirs = mk(S1, work=work(edited_paths=real + scaff))
    c = only(rank(mine, [ws("seam", f"claude:{S1}")], [theirs]), "seam")
    assert c.score == pytest.approx(2 * A.W_FILE)
    assert c.reasons == ["edited 2 file(s) its sessions also edited"]


def test_shared_files_cap(mk, tmp_path):
    paths = [str(tmp_path / f"src/m{i}.py") for i in range(9)]
    mine, theirs = mk(work=work(edited_paths=paths)), mk(S1, work=work(edited_paths=paths))
    c = only(rank(mine, [ws("seam", f"claude:{S1}")], [theirs]), "seam")
    assert c.score == pytest.approx(A.W_FILE_MAX)


def test_scaffolding_only_overlap_is_no_signal(mk, tmp_path):
    paths = [str(tmp_path / "CLAUDE.md"), str(tmp_path / "uv.lock")]
    mine, theirs = mk(work=work(edited_paths=paths)), mk(S1, work=work(edited_paths=paths))
    assert rank(mine, [ws("seam", f"claude:{S1}")], [theirs]) == []


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/r/CLAUDE.md", True),
        ("/r/CLAUDE.local.md", True),
        ("/r/docs/README.md", True),
        ("/r/justfile", True),
        ("/r/uv.lock", True),
        ("/h/.claude/projects/p/memory/MEMORY.md", True),
        ("/h/notes/memory/today.md", True),
        ("/r/.claude/settings.json", True),
        ("/r/src/mixer.py", False),
        ("/r/docs/design.md", False),
    ],
)
def test_is_scaffolding(path, expected):
    assert is_scaffolding(path) is expected


# --- mentions in the transcript tail -----------------------------------------


def _members(*refs: str) -> Membership:
    return Membership.of(ws("m", *refs))


@pytest.mark.parametrize(
    "text",
    [
        "see acme/trellis#57 for the review",
        "https://github.com/acme/trellis/pull/57",
        "gh pr view 57 --repo acme/trellis",
    ],
)
def test_mentions_pr(text):
    assert mentions(text, _members(f"gh:{PR}")) == [(A.W_MENTION_PR, f"names {PR}, an anchor here")]


@pytest.mark.parametrize(
    "text",
    [
        "gh pr view 57",  # number without the repo
        "acme/trellis has 157 open issues",  # number inside another number
        "acme/trellis#570",
        "https://github.com/acme/trellis/pull/571",
    ],
)
def test_mentions_pr_negative(text):
    assert mentions(text, _members(f"gh:{PR}")) == []


def test_mentions_worktree_by_path_and_by_name(tmp_path):
    wt = (tmp_path / "kiln" / ".claude" / "worktrees" / "fix-track").resolve()
    m = _members(f"wt:{wt}")
    want = [(A.W_MENTION_WT, f"names worktree {wt}, an anchor here")]
    assert mentions(f"{wt}  abc1234 [wt/fix-track]", m) == want
    # `git worktree list` from another checkout: name and parent dir, not the path.
    assert mentions("worktrees/fix-track is checked out", m) == want
    assert mentions("fix-track", m) == []


def test_mentions_branch_long_enough():
    m = _members("branch:orchard@mixer-seam")
    assert mentions("gh pr create --head mixer-seam", m) == [
        (A.W_MENTION_BRANCH, "names branch mixer-seam, an anchor here")
    ]
    assert mentions("--head mixer-seam-2", m) == []
    assert mentions("--head old/mixer-seam", m) == []


def test_short_branch_names_are_not_mentions():
    assert mentions("git switch main", _members("branch:orchard@main")) == []


def test_mentions_empty_text():
    assert mentions("", _members(f"gh:{PR}")) == []


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_tail_text_collects_every_text_shape(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_transcript(
        p,
        [
            {"type": "user", "message": {"role": "user", "content": "did we ship it?"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Checking."},
                        {"type": "tool_use", "input": {"command": "gh pr view 57"}},
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "state: OPEN"},
                        {"type": "tool_result", "content": [{"type": "text", "text": "draft"}]},
                    ]
                },
            },
            {"type": "system", "message": "not a dict"},
        ],
    )
    text = tail_text(SessionFacts(id=ME, kind="claude", path=str(p)))
    for frag in ("did we ship it?", "Checking.", "gh pr view 57", "state: OPEN", "draft"):
        assert frag in text
    assert "not a dict" not in text


def test_tail_text_missing_file_is_empty(mk):
    assert tail_text(mk()) == ""


def test_rank_uses_transcript_mentions(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_transcript(
        p, [{"type": "user", "message": {"content": "what happened to acme/trellis#57?"}}]
    )
    s = SessionFacts(id=ME, kind="claude", path=str(p))
    c = only(rank(s, [ws("seam", f"gh:{PR}")]), "seam")
    assert c.score == A.W_MENTION_PR
    assert c.reasons == [f"names {PR}, an anchor here"]


# --- ranking -----------------------------------------------------------------


def test_ranking_is_by_score_then_title(mk):
    s = mk(pasted_sessions=[(S1, AT)], work=work(prs=[PR]))
    wss = [
        ws("pr", f"gh:{PR}", title="B pr"),
        ws("pasted", f"claude:{S1}", title="Z pasted"),
        ws("tie", f"gh:{PR}", title="A tie"),
        ws("nothing", f"claude:{S2}"),
    ]
    assert [c.ws.name for c in rank(s, wss)] == ["pasted", "tie", "pr"]


def test_already_attached_kept_at_zero(mk):
    c = only(rank(mk(), [ws("seam", f"claude:{ME}")]), "seam")
    assert c.score == 0
    assert c.reasons == ["already attached"]


def test_already_attached_leads_the_reasons(mk):
    c = only(rank(mk(work=work(prs=[PR])), [ws("seam", f"claude:{ME}", f"gh:{PR}")]), "seam")
    assert c.reasons[0] == "already attached"
    assert c.score == A.W_PR


# --- Membership.of -----------------------------------------------------------


def test_membership_parses_every_scheme(tmp_path):
    repo = tmp_path / "orchard"
    wt = repo / ".claude" / "worktrees" / "w1"
    wt.mkdir(parents=True)
    other = tmp_path / "scratch"
    other.mkdir()
    m = Membership.of(
        ws(
            "m",
            f"claude:{S1}",
            f"codex:{S2}",
            f"agent:{S3}/ag01",
            f"agent:{S4}/",
            f"gh:{PR}",
            "pr:acme/orchard#9",
            "branch:orchard@feat/x",
            f"wt:{wt}",
            f"wt:{other}",
            f"repo:{tmp_path / 'kiln'}",
            "file:/r/notes.md",
            "ws:other",
        )
    )
    assert m.sessions == {S1, S2, S3, "ag01", S4}
    assert m.prs == {PR, "acme/orchard#9"}
    assert m.branches == {"orchard@feat/x"}
    assert m.worktrees == {str(wt.resolve()), str(other.resolve())}
    assert m.repos == {str(repo.resolve()), str((tmp_path / "kiln").resolve())}
