"""frontier: recommendation, loose ends, foreign merges, timeline, slug and
capture. Pure functions get hand-built objects; `build` runs on the synthetic
store with a hand-built Workstream, so no weft is needed."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console

from workstream import cli, hooklog, seam
from workstream import frontier as F
from workstream.anchors import Anchor
from workstream.clock import Instant
from workstream.frontier import Frontier, Recommendation, SessionFrontier
from workstream.hooklog import MessageEvent
from workstream.sessions import Acted, SessionFacts
from workstream.store import Child, Claim, Workstream

S1 = "5e6f7a8b-2222-4222-8222-000000000002"
S2 = "9c0d1e2f-3333-4333-8333-000000000003"
S3 = "d4e5f6a7-4444-4444-8444-000000000004"
NOW = Instant(datetime(2026, 11, 2, 17, 0, tzinfo=UTC), timedelta(hours=-8))


def at(minutes_ago: float, offset_h: int = -8) -> Instant:
    return Instant(NOW.utc - timedelta(minutes=minutes_ago), timedelta(hours=offset_h))


def facts(sid: str = S1, **kw) -> SessionFacts:
    kw.setdefault("kind", "claude")
    kw.setdefault("path", f"/nonexistent/{sid}.jsonl")
    return SessionFacts(id=sid, **kw)


def sf(role: str = "canonical", **kw) -> SessionFrontier:
    live = kw.pop("live", False)
    dropped = kw.pop("dropped", True)
    lifecycle = kw.pop("lifecycle", None)
    return SessionFrontier(
        facts=facts(**kw), role=role, live=live, dropped=dropped, lifecycle=lifecycle
    )


def ws(name: str = "mixer-seam", *refs: str, claims=()) -> Workstream:
    return Workstream(
        id=f"id-{name}-0123456789",
        name=name,
        title="Mixer seam",
        state="open",
        created=None,
        touched=None,
        refs=list(refs),
        claims=list(claims),
    )


def anchor(ref: str, warnings=(), **liveness) -> Anchor:
    scheme, _, value = ref.partition(":")
    return Anchor(ref=ref, scheme=scheme, value=value, liveness=liveness, warnings=list(warnings))


# --- options -----------------------------------------------------------------


def opts(sfs, captures=(), root=None, name="mixer-seam"):
    return F.options(ws(name), sfs, NOW, captures, root=root)


def test_no_canonical_offers_only_fresh(tmp_path):
    o = opts([sf("ancillary", last_seen=at(1))], root=tmp_path)
    assert (o.default, o.session_id, o.message, o.resume) == ("fresh", None, None, None)
    assert o.default_why == "no canonical session is attached"
    assert o.recommendation() == Recommendation("fresh", None, ["no canonical session is attached"])
    assert o.fresh.facts[0] == (
        f"prose frontier: {tmp_path}/_shared/workstreams/mixer-seam.md (not written yet)"
    )


def test_context_over_the_cap_leans_fresh_and_keeps_resume_as_an_option(tmp_path):
    o = opts([sf(peak_context=F.RESUME_MAX_CONTEXT + 1, last_seen=at(5))], root=tmp_path)
    assert (o.default, o.session_id) == ("fresh", S1)
    assert o.default_why == "its context is above the resume cap"
    assert o.resume.facts[:2] == [
        "peak context 150,001 (never compacted)",
        "above the resume cap (RESUME_MAX_CONTEXT 150,000)",
    ]
    assert o.resume.data["within_resume_cap"] is False and o.resume.data["cache_warm"] is True


def test_compacted_session_is_judged_on_its_post_compaction_context(tmp_path):
    big = sf(
        peak_context=380_000,
        compaction_count=2,
        context_after_compaction=42_000,
        last_seen=at(10),
        cwd="/w/orchard",
    )
    o = opts([big], root=tmp_path)
    assert o.default == "resume"
    assert o.default_why == "its context is within the resume cap and the cache is still warm"
    assert o.resume.how == f"cd /w/orchard && claude --resume {S1}"
    assert o.resume.facts == [
        "context since the last compaction 42,000 (2 compactions; whole-transcript peak 380,000)",
        "within the resume cap (RESUME_MAX_CONTEXT 150,000)",
        "idle 10m, within CACHE_TTL 1h00m: the cache is warm",
    ]
    assert o.resume.data == {
        "command": f"cd /w/orchard && claude --resume {S1}",
        "context": 42_000,
        "context_basis": "after_compaction",
        "peak_context": 380_000,
        "compaction_count": 2,
        "within_resume_cap": True,
        "idle_min": 10,
        "cache_warm": True,
    }


def test_unknown_post_compaction_context_never_leans_resume():
    s = sf(peak_context=300_000, compaction_count=1, context_after_compaction=None, last_seen=at(5))
    o = opts([s])
    assert o.default == "fresh"
    assert o.default_why == "its context since the last compaction is unknown"
    assert o.resume.facts[0] == (
        "no turn since the last compaction, so its resume cost is unknown "
        "(1 compaction; whole-transcript peak 300,000)"
    )
    assert not any("resume cap" in x for x in o.resume.facts)
    assert (o.resume.data["context"], o.resume.data["within_resume_cap"]) == (None, None)
    assert o.recommendation().action == "fresh"


def test_idle_past_cache_ttl_leans_fresh():
    o = opts([sf(peak_context=90_000, last_seen=at(125))])
    assert (o.default, o.default_why) == ("fresh", "the cache is cold")
    assert "idle 2h05m, past CACHE_TTL 1h00m: the cache is cold" in o.resume.facts


def test_no_last_activity_leans_fresh():
    o = opts([sf(peak_context=90_000)])
    assert (o.default, o.default_why) == ("fresh", "its last activity is unknown")
    assert "no last activity recorded, so cache warmth is unknown" in o.resume.facts


def test_live_session_offers_message_with_its_address():
    s = sf(peak_context=90_000, last_seen=at(2), live=True)
    s.address = {"name": "mixer-pane", "tmux": "work:2.1"}
    o = opts([s])
    assert o.default == "message"
    assert o.message.how == 'SendMessage to "mixer-pane" or tmux pane work:2.1'
    assert any("two processes" in x for x in o.resume.facts)
    r = o.recommendation()
    assert (r.action, r.session_id) == ("fresh", S1)
    assert any("message it rather than resuming" in w for w in r.why)


def test_small_and_warm_resumes_the_first_canonical():
    sfs = [
        sf(peak_context=F.RESUME_MAX_CONTEXT, last_seen=at(59)),
        sf(sid=S2, peak_context=10_000, last_seen=at(1)),
    ]
    o = opts(sfs)
    assert (o.default, o.session_id) == ("resume", S1)
    assert o.recommendation().action == "resume"
    assert f"claude-ls summary {S1[:8]}" in o.fresh.how


def test_fresh_names_the_prose_frontier_and_newest_capture(tmp_path):
    (tmp_path / "_shared/workstreams").mkdir(parents=True)
    (tmp_path / "_shared/workstreams/mixer-seam.md").write_text("# state\n")
    old, new = tmp_path / "a.json", tmp_path / "b.json"
    for p, mins in ((old, 300), (new, 90)):
        p.write_text("{}")
        t = (NOW.utc - timedelta(minutes=mins)).timestamp()
        os.utime(p, (t, t))
    o = opts([], captures=[str(new), str(old), str(tmp_path / "gone.json")], root=tmp_path)
    assert o.fresh.facts == [
        f"prose frontier: {tmp_path}/_shared/workstreams/mixer-seam.md",
        f"newest capture: {new} (1h30m old)",
    ]
    assert o.fresh.data["prose_exists"] is True


# --- loose ends --------------------------------------------------------------


def test_worktree_with_everything_outstanding():
    a = anchor("wt:/w/fix", exists=True, unmerged=2, default_branch="main", dirty=True, unpushed=3)
    assert F._loose_ends([], [a], []) == [
        "worktree /w/fix: 2 commit(s) not on main, uncommitted changes, 3 unpushed commit(s)"
    ]


@pytest.mark.parametrize("up", ["no remote", "no upstream"])
def test_worktree_without_remote(up):
    a = anchor("wt:/w/fix", exists=True, unmerged=1, unpushed=up)
    assert F._loose_ends([], [a], []) == [
        f"worktree /w/fix: 1 commit(s) not on the default branch, {up}"
    ]


def test_clean_worktree_is_not_a_loose_end():
    assert F._loose_ends([], [anchor("wt:/w/fix", exists=True, unmerged=0, unpushed=0)], []) == []


def test_worktree_gone():
    assert F._loose_ends([], [anchor("wt:/w/fix", exists=False)], []) == ["worktree /w/fix is gone"]


def test_branch_without_remote():
    a = anchor("branch:kiln@wt/fix", exists_local=True, exists_remote=False)
    b = anchor("branch:kiln@wt/ok", exists_local=True, exists_remote=True)
    assert F._loose_ends([], [a, b], []) == ["branch kiln@wt/fix has no remote copy"]


def test_changes_requested_names_the_reviewer():
    a = anchor(
        "gh:acme/trellis#57",
        state="OPEN",
        review_decision="CHANGES_REQUESTED",
        reviews=[
            {"author": "reviewer-one", "state": "CHANGES_REQUESTED"},
            {"author": "reviewer-two", "state": "APPROVED"},
        ],
    )
    assert F._loose_ends([], [a], []) == [
        "gh:acme/trellis#57: changes requested by reviewer-one, not yet addressed"
    ]


def test_changes_requested_on_a_merged_pr_is_settled():
    a = anchor("gh:acme/trellis#57", state="MERGED", review_decision="CHANGES_REQUESTED")
    assert F._loose_ends([], [a], []) == []


def test_open_draft():
    a = anchor("gh:acme/trellis#58", state="OPEN", is_draft=True)
    assert F._loose_ends([], [a], []) == ["gh:acme/trellis#58: still a draft"]


def test_anchor_warnings_are_loose_ends():
    a = anchor("gh:acme/trellis#59", warnings=["gh unavailable"])
    assert F._loose_ends([], [a], []) == ["gh:acme/trellis#59: gh unavailable"]


def test_dropped_sessions_by_role():
    sfs = [
        sf("canonical", lifecycle="SessionEnd(other)"),
        sf("ancillary", sid=S2),
        sf("subagent", sid="ag01"),
        sf("canonical", sid=S3, dropped=False),
    ]
    assert F._loose_ends(sfs, [], []) == [
        f"{S1[:8]} (canonical) has no recorded frontier; last SessionEnd(other)",
        f"{S2[:8]} (ancillary) has no recorded frontier; last no lifecycle event",
    ]


def test_ending_on_a_question():
    s = sf(dropped=False, last_assistant=(at(3), "Shall I push the branch?  "))
    assert F._loose_ends([s], [], []) == [
        f"{S1[:8]} ended on a question for the human: Shall I push the branch?  "
    ]


def test_unresolved_message():
    m = MessageEvent(at=at(4), from_sid=S1, to_raw="pane-7", summary="s", body="b")
    assert F._loose_ends([], [], [m]) == [
        f"message from {S1[:8]} to 'pane-7' could not be resolved to a session"
    ]


# --- foreign actions ---------------------------------------------------------


def _merger(sid: str, command: str, kind: str = "claude", repos=()) -> SessionFacts:
    return facts(sid, kind=kind, repos=list(repos), last_tools=[(at(30), "Bash", command)])


def test_foreign_merge_is_reported():
    w = ws("origin", "gh:acme/meadow#319")
    out = F.foreign_actions(w, [_merger(S2, "gh pr merge 319 --repo acme/meadow --squash")], {S1})
    assert out == [
        f"gh:acme/meadow#319 was merged by {S2[:8]} at {at(30).iso()}, a session not attached here"
    ]


def test_foreign_merge_by_repo_basename_from_the_session():
    w = ws("origin", "gh:acme/meadow#319")
    m = _merger(S2, "gh pr merge --squash 319", repos=["/src/meadow"])
    assert len(F.foreign_actions(w, [m], set())) == 1


def test_foreign_actions_ignore_members_subagents_other_repos_and_other_prs():
    w = ws("origin", "gh:acme/meadow#319")
    recent = [
        _merger(S1, "gh pr merge 319 --repo acme/meadow"),  # attached here
        _merger("ag01", "gh pr merge 319 --repo acme/meadow", kind="subagent"),
        _merger(S2, "gh pr merge 319 --repo acme/bramble"),
        _merger(S3, "gh pr merge 3190 --repo acme/meadow"),
        facts("x", last_tools=[(at(1), "Read", "gh pr merge 319 --repo acme/meadow")]),
    ]
    assert F.foreign_actions(w, recent, {S1}) == []


def test_foreign_actions_without_pr_anchors():
    assert F.foreign_actions(ws("x", "wt:/w"), [_merger(S2, "gh pr merge 1")], set()) == []


# --- timeline ----------------------------------------------------------------


def test_timeline_is_sorted_utc_and_includes_gh_updates():
    s = SessionFrontier(
        facts=facts(
            started=at(100, -5),
            last_seen=at(10, -8),
            first_prompt="fix the mixer",
            human_prompts=[(at(90, -7), "first"), (at(50, -8), "second")],
        ),
        role="canonical",
        lifecycle="SessionEnd(other)",
        lifecycle_at=at(9),
    )
    m = MessageEvent(at=at(40), from_sid=S1, to_raw="p", summary="handover", body="b", to_sid=S2)
    gh = anchor(
        "gh:acme/trellis#57",
        state="OPEN",
        review_decision="CHANGES_REQUESTED",
        updated_at="2026-11-02T16:30:00Z",
    )
    bad = anchor("gh:acme/trellis#58", updated_at="not a time")
    rows = F._timeline([s], [m], [gh, bad])
    assert [r[0].utc for r in rows] == sorted(r[0].utc for r in rows)
    assert [k for _, k, _ in rows] == [
        "session",
        "prompt",
        "prompt",
        "message",
        "anchor",
        "last-seen",
        "lifecycle",
    ]
    assert rows[0][2] == f"claude:{S1} started (canonical): fix the mixer"
    assert rows[4][2] == "gh:acme/trellis#57 updated upstream (OPEN, CHANGES_REQUESTED)"
    assert rows[4][0].utc == datetime(2026, 11, 2, 16, 30, tzinfo=UTC)


# --- slug and capture --------------------------------------------------------


def test_slug_for(tmp_path):
    sfs = [
        sf(repos=["/a/orchard", "/c/trellis/"]),
        sf(sid=S2, repos=["/b/orchard"]),
    ]
    w = ws()
    assert F.slug_for(w, sfs, tmp_path) == "_shared"
    (tmp_path / "trellis").mkdir()
    assert F.slug_for(w, sfs, tmp_path) == "trellis"
    (tmp_path / "orchard").mkdir()
    assert F.slug_for(w, sfs, tmp_path) == "orchard"
    assert F.slug_for(w, sfs, tmp_path, explicit="pinned") == "pinned"


def test_slug_for_skips_the_docs_repo(tmp_path):
    docs_repo = tmp_path / "notes"
    root = docs_repo / "docs"
    (docs_repo / ".git").mkdir(parents=True)
    (root / "notes").mkdir(parents=True)
    (root / "orchard").mkdir()
    (root / tmp_path.name).mkdir()
    sfs = [
        sf(repos=[str(docs_repo), str(docs_repo), "/a/orchard"]),
        sf(sid=S2, repos=[str(docs_repo)]),
    ]
    assert F.slug_for(ws(), sfs, root) == "orchard"
    # Only the repo holding the docs root is skipped, not its ancestors.
    outside = [sf(repos=[str(tmp_path), str(tmp_path), "/a/orchard"])]
    assert F.slug_for(ws(), outside, root) == tmp_path.name

    # A capture already filed under the docs repo's slug does not pin it.
    cap = root / "notes" / "workstreams" / "w" / "captures" / "a.json"
    cap.parent.mkdir(parents=True)
    cap.write_text("{}")
    assert F.slug_for(ws("mixer-seam", f"frontier:{cap}"), sfs, root) == "orchard"


def test_slug_for_follows_the_newest_capture(tmp_path):
    (tmp_path / "orchard").mkdir()
    sfs = [sf(repos=["/a/orchard"])]

    def cap(slug, stamp, mtime):
        p = tmp_path / slug / "workstreams" / "w" / "captures" / f"{stamp}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        os.utime(p, (mtime, mtime))
        return f"frontier:{p}"

    old, new = cap("orchard", "a", 1_000), cap("grove", "b", 2_000)
    w = ws("mixer-seam", old, new)
    assert F.slug_for(w, sfs, tmp_path) == "grove"
    assert F.prose_path(w, sfs, tmp_path) == tmp_path / "grove" / "workstreams" / f"{w.name}.md"
    # An explicit slug still wins; a capture gone from disk no longer counts.
    assert F.slug_for(w, sfs, tmp_path, explicit="pinned") == "pinned"
    Path(new.partition(":")[2]).unlink()
    assert F.slug_for(w, sfs, tmp_path) == "orchard"


def _frontier(sessions, name="mixer-seam") -> Frontier:
    w = ws(name)
    return Frontier(
        ws=w,
        as_of=NOW,
        sessions=sessions,
        anchors=[anchor("wt:/w/fix", exists=True, unmerged=1)],
        messages=[MessageEvent(at=at(4), from_sid=S1, to_raw="p", summary="s", body="b")],
        timeline=[(at(5), "session", "x")],
        loose_ends=["one"],
        options=F.options(w, sessions, NOW, root=Path("/nonexistent-docs")),
    )


def test_capture_writes_json_without_weft(tmp_path):
    s = sf(
        repos=["/a/orchard"],
        human_prompts=[(at(20), "go")],
        last_assistant=(at(19), "done?"),
        last_tools=[(at(18), "Bash", "ls")],
    )
    fr = _frontier([s])
    path = F.capture(fr, root=tmp_path, slug="orchard", record=False)
    assert path == (
        tmp_path / "orchard/workstreams/mixer-seam/captures" / f"20261102T170000Z-{S1[:8]}.json"
    )
    data = json.loads(path.read_text())
    assert [x["id"] for x in data["sessions"]] == [S1]
    assert data["thresholds"] == {"RESUME_MAX_CONTEXT": 150_000, "CACHE_TTL_MIN": 60}
    assert data["anchors"][0]["liveness"]["unmerged"] == 1


def test_capture_without_sessions_or_name(tmp_path):
    fr = _frontier([], name=None)
    path = F.capture(fr, root=tmp_path, record=False)
    assert path.parent == tmp_path / "_shared/workstreams" / fr.ws.id[:12] / "captures"
    assert path.name.endswith("-none.json")


def test_to_dict_round_trips_through_json():
    fr = _frontier([sf(last_seen=at(1), work=seam.WorkSignals(prs=["acme/trellis#57"]))])
    d = F.to_dict(fr)
    assert json.loads(json.dumps(d)) == json.loads(json.dumps(d, sort_keys=True))
    assert d["sessions"][0]["prs"] == ["acme/trellis#57"]
    o = d["options"]
    assert (o["default"], o["session_id"]) == ("resume", S1)
    assert set(o) == {"session_id", "default", "default_why", "message", "fresh", "resume"}
    assert o["message"] is None
    assert set(o["fresh"]) == set(o["resume"]) == {"how", "facts", "data"}
    assert o["resume"]["data"]["context_basis"] == "peak"
    # The pre-options key is still there, derived from the default.
    assert d["recommendation"]["action"] == "resume"
    assert d["recommendation"]["why"][0] == o["default_why"]
    assert d["sessions"][0]["compaction_count"] == 0
    assert d["as_of"] == NOW.iso()


# --- build on the synthetic store --------------------------------------------


@pytest.fixture
def dup(acceptance_cases, monkeypatch, tmp_path):
    # The options name a prose path under the docs root; keep it off the real one.
    monkeypatch.setenv("WORKSTREAM_DOCS_ROOT", str(tmp_path / "docs"))
    # The real registry would ask `claude agents`; the frontier only needs ids.
    live: list[dict] = []
    monkeypatch.setattr(seam, "live_registry", lambda *a, **k: live)
    case = acceptance_cases["duplicate-bug"]
    first, second = case["sessions"]["first"], case["sessions"]["second"]
    w = ws(
        "default-track",
        *case["workstreams"][0]["anchors"],
        first.ref,
        second.ref,
        claims=[Claim(holder=f"agent:{first.ref}", at=NOW, expires=None)],
    )
    return case, w, live, hooklog.load(registry=[])


def test_build_roles_dropped_and_loose_ends(dup):
    case, w, _, log = dup
    first = case["sessions"]["first"]
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    assert [(s.facts.id, s.role) for s in fr.sessions] == [
        (first.id, "canonical"),
        (case["sessions"]["second"].id, "ancillary"),
    ]
    assert all(s.dropped for s in fr.sessions)
    assert f"{first.id[:8]} (canonical) has no recorded frontier; last SessionStart(resume)" in (
        fr.loose_ends
    )
    assert fr.timeline and fr.as_of == NOW


def test_build_carries_released_intervals(dup):
    case, w, _, log = dup
    first, second = case["sessions"]["first"], case["sessions"]["second"]
    # second was canonical once, released, attached again, then released for good.
    w.released = [
        Claim(
            holder=f"agent:{second.ref}",
            at=at(90),
            expires=at(-10_000),
            note="earlier stint",
            live=False,
            released_at=at(60),
        ),
        Claim(
            holder=f"agent:{second.ref}",
            at=at(50),
            expires=at(-10_000),
            note="later stint",
            live=False,
            released_at=at(30, offset_h=0),
        ),
    ]
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    by_id = {s.facts.id: s for s in fr.sessions}
    canon, anc = by_id[first.id], by_id[second.id]
    # The role is the session's role now; the intervals say when it was canonical.
    assert anc.role == "ancillary"
    assert [(i.note, i.released_at) for i in anc.intervals] == [
        ("earlier stint", at(60)),
        ("later stint", at(30, offset_h=0)),
    ]
    assert [(i.at, i.released_at) for i in canon.intervals] == [(NOW, None)]

    sessions = {d["id"]: d for d in F.to_dict(fr)["sessions"]}
    assert sessions[second.id]["intervals"][1] == {
        "at": at(50).iso(),
        "expires": at(-10_000).iso(),
        "released_at": at(30, offset_h=0).iso(),
        "note": "later stint",
    }
    assert sessions[first.id]["intervals"][0]["released_at"] is None

    con = Console(record=True, width=200, no_color=True)
    cli._print_show(con, w, fr, local=False)
    out = con.export_text()
    assert "      canonical 2026-11-02 15:30Z → 2026-11-02 16:00Z released" in out
    assert "      canonical 2026-11-02 16:10Z → 2026-11-02 16:30Z released" in out
    assert "      canonical 2026-11-02 17:00Z → held" in out


def test_build_marks_live_sessions(dup):
    case, w, live, log = dup
    first = case["sessions"]["first"]
    live.append({"sessionId": first.id})
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    canon = fr.sessions[0]
    assert canon.live
    assert fr.options.default == "message"
    assert fr.recommendation.action == "fresh"
    assert any("message it" in x for x in fr.recommendation.why)


def test_recorded_capture_clears_dropped(dup, tmp_path):
    case, w, _, log = dup
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    path = F.capture(fr, root=tmp_path, slug="kiln", record=False)
    w.refs.append(f"frontier:{path}")
    again = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    assert not any(s.dropped for s in again.sessions)
    assert again.captures == [str(path)]


def test_explicit_repo_flag_for_another_repo_is_not_a_foreign_merge():
    # The session worked in both repos; --repo says which one it merged in.
    w = ws("origin", "gh:acme/meadow#319")
    m = _merger(S2, "gh pr merge 319 --repo acme/bramble", repos=["/src/meadow", "/src/bramble"])
    assert F.foreign_actions(w, [m], set()) == []


# --- tasks and closure -------------------------------------------------------


def _with_tasks(state: str = "open", *children: Child) -> Workstream:
    w = ws("mixer-seam")
    w.state = state
    w.children = list(children)
    return w


T_DONE = Child("t-done-000000", "Reproduce the hum", "task", "done")
T_OPEN = Child("t-open-000000", "Patch the seam", "task", "open")
T_BLOCKED = Child("t-blkd-000000", "Ship the patch", "task", "open")
T_DROPPED = Child("t-drop-000000", "Try a bigger buffer", "task", "dropped")
SUB = Child("w-sub-0000000", "Mixer sub-track", "workstream", "open")


def test_tasks_order_open_first_and_skip_sub_workstreams():
    w = _with_tasks("open", T_DROPPED, T_DONE, T_BLOCKED, SUB, T_OPEN)
    got = F.tasks_for(w, {T_BLOCKED.id})
    assert [(t.title, t.state, t.blocked) for t in got] == [
        ("Patch the seam", "open", False),
        ("Ship the patch", "open", True),
        ("Reproduce the hum", "done", None),
        ("Try a bigger buffer", "dropped", None),
    ]


def test_tasks_blocked_unknown_when_weft_could_not_say():
    w = _with_tasks("open", T_OPEN)
    (t,) = F.tasks_for(w, None)
    assert t.blocked is None
    assert F._task_loose_ends(w, [t]) == ["1 task open (blocked unknown)"]


def test_open_tasks_are_a_loose_end():
    w = _with_tasks("open", T_DONE, T_OPEN, T_BLOCKED)
    assert F._task_loose_ends(w, F.tasks_for(w, {T_BLOCKED.id})) == ["2 tasks open (1 blocked)"]


def test_all_tasks_settled_but_not_closed():
    w = _with_tasks("open", T_DONE, T_DROPPED)
    assert F._task_loose_ends(w, F.tasks_for(w, set())) == [F.NOT_CLOSED]
    w.state = "done"
    assert F._task_loose_ends(w, F.tasks_for(w, set())) == []


def test_no_tasks_no_task_loose_ends():
    # A workstream without tasks is not nagged to close.
    assert F._task_loose_ends(_with_tasks("open", SUB), []) == []


def test_build_carries_tasks_and_closure_blockers(dup):
    case, w, _, log = dup
    w.children = [T_DONE, T_OPEN, T_BLOCKED]
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW, blocked={T_BLOCKED.id})
    assert [t.id for t in fr.tasks] == [T_OPEN.id, T_BLOCKED.id, T_DONE.id]
    assert "2 tasks open (1 blocked)" in fr.loose_ends
    assert "2 tasks open (1 blocked)" in fr.closure_blockers
    # Dropped sessions are a loose end, but not a reason to refuse closing.
    dropped = [x for x in fr.loose_ends if "no recorded frontier" in x]
    assert dropped and not set(dropped) & set(fr.closure_blockers)
    assert any("commit(s) not on" in x for x in fr.closure_blockers)
    d = F.to_dict(fr)
    assert [t["blocked"] for t in d["tasks"]] == [False, True, None]
    assert d["closure_blockers"] == fr.closure_blockers


def test_not_closed_nudge_is_not_a_closure_blocker(dup):
    case, w, _, log = dup
    w.children = [T_DONE]
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    assert F.NOT_CLOSED in fr.loose_ends
    assert F.NOT_CLOSED not in fr.closure_blockers


# --- squash-merged anchors ----------------------------------------------------


def test_squash_merged_worktree_is_not_a_loose_end():
    a = anchor(
        "wt:/w/fix",
        exists=True,
        unmerged=2,
        default_branch="main",
        unpushed="no upstream",
        merged_into_default=True,
        merged_via={"pr": 41, "merged_at": "2026-10-30T09:00:00Z", "commit": "c0ffee"},
    )
    assert F._loose_ends([], [a], []) == []


def test_squash_merged_worktree_still_reports_uncommitted_changes():
    a = anchor(
        "wt:/w/fix", exists=True, unmerged=1, dirty=True, unpushed=2, merged_into_default=True
    )
    assert F._loose_ends([], [a], []) == ["worktree /w/fix: uncommitted changes"]


def test_merged_branch_without_remote_copy_is_not_a_loose_end():
    merged = anchor(
        "branch:kiln@wt/fix", exists_local=True, exists_remote=False, merged_into_default=True
    )
    open_ = anchor(
        "branch:kiln@wt/new", exists_local=True, exists_remote=False, merged_into_default=False
    )
    assert F._loose_ends([], [merged, open_], []) == ["branch kiln@wt/new has no remote copy"]


def test_build_squash_merged_worktree_drops_its_loose_end(dup, synthetic_store):
    case, w, _, log = dup
    wt = case["worktrees"]["fix-default-track"]
    before = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    assert any(le.startswith(f"worktree {wt}: 1 commit(s) not on main") for le in before.loose_ends)

    info = synthetic_store.worktrees[wt]
    repo = info["repo"]
    synthetic_store.git(repo, "merge", "-q", "--squash", info["branch"])
    synthetic_store.git(repo, "commit", "-q", "-m", "fix: default track (#9)")
    after = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    assert not any(le.startswith(f"worktree {wt}") for le in after.loose_ends), after.loose_ends
    (wt_anchor,) = [a for a in after.anchors if a.ref == f"wt:{wt}"]
    assert wt_anchor.liveness["merged_via"] == {"tree": True}


# --- gone worktrees -------------------------------------------------------------


def _wt_anchor(fr: Frontier, wt) -> Anchor:
    (a,) = [
        a
        for a in fr.anchors
        if a.scheme == "wt" and os.path.realpath(a.value) == os.path.realpath(wt)
    ]
    return a


def test_gone_worktree_whose_branch_merged_is_history_via_session_evidence(dup, synthetic_store):
    case, w, _, log = dup
    wt = case["worktrees"]["fix-default-track"]
    # No branch anchor: only what the first session did in the worktree names its branch.
    w.refs = [r for r in w.refs if not r.startswith("branch:")]
    synthetic_store.retire_worktree(wt)
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    lv = _wt_anchor(fr, wt).liveness
    assert lv["exists"] is False
    assert lv["status"] == "gone (branch wt/fix-default-track merged)"
    assert lv["branch_from"].endswith("committed on it there")
    assert not any(str(wt) in x for x in fr.loose_ends + fr.closure_blockers), fr.loose_ends


def test_gone_worktree_whose_branch_did_not_merge_stays_loose(dup, synthetic_store):
    case, w, _, log = dup
    wt = case["worktrees"]["fix-default-track"]
    synthetic_store.retire_worktree(wt, merge=False)
    fr = F.build(w, log=log, gh_runner=lambda argv: (1, ""), now=NOW)
    assert _wt_anchor(fr, wt).liveness["status"] == "gone (branch wt/fix-default-track not merged)"
    end = f"worktree {_wt_anchor(fr, wt).value} is gone; its branch wt/fix-default-track is not merged"
    assert end in fr.loose_ends and end in fr.closure_blockers


def _merged_pr_view(head: str):
    def run(argv):
        if argv[1:3] != ["pr", "view"]:
            return 1, ""
        return 0, json.dumps(
            {"state": "MERGED", "mergedAt": "2026-11-01T12:00:00Z", "headRefName": head}
        )

    return run


def test_gone_worktree_merged_via_a_pr_anchors_head_ref(dup, tmp_path):
    _, _, _, log = dup
    gone = tmp_path / "worktrees" / "kiln-fix-tempo"
    w = ws("tempo", f"wt:{gone}", "gh:acme/kiln#12")
    fr = F.build(w, log=log, gh_runner=_merged_pr_view("fix/tempo"), now=NOW)
    lv = _wt_anchor(fr, gone).liveness
    # No session worked there, so the PR's head name is the only evidence.
    assert lv["status"] == "gone (branch fix/tempo merged, by branch name only)"
    assert lv["branch_from"] == "named for the head of gh:acme/kiln#12"
    assert lv["merged_via"] == {
        "pr_anchor": "gh:acme/kiln#12",
        "merged_at": "2026-11-01T12:00:00Z",
        "head_only": True,
    }
    assert fr.loose_ends == [] and fr.closure_blockers == []


def test_gone_worktree_with_nothing_to_derive_stays_loose(dup, tmp_path):
    _, _, _, log = dup
    gone = tmp_path / "worktrees" / "kiln-mystery"
    # The PR's head does not match the worktree's name, so it says nothing about it.
    w = ws("mystery", f"wt:{gone}", "gh:acme/kiln#12")
    fr = F.build(w, log=log, gh_runner=_merged_pr_view("fix/tempo"), now=NOW)
    assert "status" not in _wt_anchor(fr, gone).liveness
    assert fr.closure_blockers == [f"worktree {gone} is gone"]


@pytest.mark.parametrize(
    ("path", "branch", "repo", "want"),
    [
        ("/w/kiln-fix-tempo", "fix/tempo", "kiln", True),
        ("/w/kiln-fix-tempo", "fix/tempo", "loom", False),
        ("/w/kiln-fix-tempo", "fix/other", "kiln", False),
        ("/r/kiln/.claude/worktrees/tempo", "worktree-tempo", None, True),
        ("/r/kiln/.claude/worktrees/fix-tempo", "fix/tempo", None, True),
        ("/r/kiln/.claude/worktrees/tempo", "wt/tempo", None, True),
        ("/r/kiln/.claude/worktrees/tempo", "rhythm", None, False),
    ],
)
def test_worktree_named_for(path, branch, repo, want):
    assert F.worktree_named_for(path, branch, repo) is want


def test_a_just_wt_creation_names_the_branch_of_a_bare_named_worktree():
    f = facts(worktree_branches={"fix/tempo": "fix/tempo"}, repos=["/r/kiln"])
    assert F._session_branch("/w/kiln-fix-tempo", [f]) == (
        "fix/tempo",
        f"{S1[:8]} created it with `just wt fix/tempo`",
        None,
    )
    assert F._session_branch("/w/kiln-fix-rhythm", [f]) is None


def test_a_shorter_just_wt_name_does_not_claim_a_longer_worktree():
    # `just wt tempo` made kiln-tempo; it says nothing about kiln-fix-tempo.
    f = facts(worktree_branches={"tempo": "tempo"}, repos=["/r/kiln"])
    assert F._session_branch("/w/kiln-fix-tempo", [f]) is None
    assert F._session_branch("/w/kiln-tempo", [f])[0] == "tempo"
    # A repo the session never worked in is no prefix to match against.
    assert F._session_branch("/w/loom-tempo", [f]) is None
    assert F._session_branch("/r/kiln/.claude/worktrees/fix-tempo", [f]) is None
    assert F._session_branch("/r/kiln/.claude/worktrees/tempo", [f])[0] == "tempo"


GONE = "/w/kiln-fix-tempo"


def _merged_pr_anchor(merged_at="2026-11-01T12:00:00Z") -> Anchor:
    return anchor(
        "gh:acme/kiln#12", kind="pr", state="MERGED", head_ref="fix/tempo", merged_at=merged_at
    )


def _commit_in(path: str, when: datetime) -> SessionFacts:
    act = Acted("branch", "fix/tempo", "git commit", Instant(when, timedelta(0)), path)
    return facts(acted=[act])


def test_work_after_a_same_named_pr_merged_is_not_merged_by_it(monkeypatch):
    # PR #12 merged head fix/tempo from an earlier slice; the worktree was
    # recreated on a new fix/tempo and committed to after that merge.
    monkeypatch.setattr(F.anchors_mod, "liveness", lambda ref, **kw: anchor(ref))
    wt = anchor(f"wt:{GONE}", exists=False)
    later = _commit_in(GONE, datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    F.settle_gone_worktrees([wt, _merged_pr_anchor()], [later])
    assert wt.liveness.get("merged_into_default") is not True
    assert F._loose_ends([], [wt], []) == [f"worktree {GONE} is gone"]


def test_work_before_the_pr_merged_is_credited_in_full(monkeypatch):
    monkeypatch.setattr(F.anchors_mod, "liveness", lambda ref, **kw: anchor(ref))
    wt = anchor(f"wt:{GONE}", exists=False)
    earlier = _commit_in(GONE, datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    F.settle_gone_worktrees([wt, _merged_pr_anchor()], [earlier])
    assert wt.liveness["status"] == "gone (branch fix/tempo merged)"
    assert not F.merged_by_name_only(wt) and F.gone_and_merged(wt)


def test_probes_only_the_repo_the_evidence_ran_in(monkeypatch):
    probed: list[str] = []

    def probe(ref, **kw):
        probed.append(ref)
        return anchor(ref, merged_into_default=True, merged_via={"tree": True})

    monkeypatch.setattr(F.anchors_mod, "liveness", probe)
    gone = "/w/scratch-dir"  # named by no convention
    act = Acted("branch", "fix/tempo", "git push", None, gone, "git@github.com:acme/kiln.git")
    hints = {n: f"/r/{n}" for n in ("kiln", "loom", "quarry", "bramble", "trellis", "mixer")}
    wt = anchor(f"wt:{gone}", exists=False)
    F.settle_gone_worktrees([wt], [facts(acted=[act])], hints=hints)
    assert probed == ["branch:kiln@fix/tempo"]
    assert wt.liveness["status"] == "gone (branch fix/tempo merged)"


def test_a_decided_branch_anchor_is_not_probed_again(monkeypatch):
    probed: list[str] = []
    monkeypatch.setattr(F.anchors_mod, "liveness", lambda ref, **kw: probed.append(ref))
    wt = anchor("wt:/r/kiln/.claude/worktrees/tempo", exists=False)
    br = anchor("branch:kiln@wt/tempo", merged_into_default=False)
    F.settle_gone_worktrees([wt, br], [])
    assert probed == []
    assert wt.liveness["status"] == "gone (branch wt/tempo not merged)"
