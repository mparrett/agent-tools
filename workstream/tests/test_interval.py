"""Canonical intervals: a session that moves from one workstream to another
gives each only what it did while attached there (store.session_windows), in
sync, show and frontier alike. Workstreams are hand-built, because weft stamps
claims with the wall clock and the fixture's transcripts are in the past."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from rich.console import Console
from synthetic import case_two_claims
from test_cli import needs_weft, run

from workstream import anchors, cli, hooklog, seam, sessions, store, sync
from workstream import frontier as F
from workstream.anchors import Anchor
from workstream.clock import Instant
from workstream.store import Claim, Workstream

NOW = Instant(datetime(2026, 10, 30, 15, 0, tzinfo=UTC), timedelta(hours=-7))


def _i(dt: datetime) -> Instant:
    return Instant(dt, timedelta(hours=-7))


def _ws(name: str, refs, claims=(), released=()) -> Workstream:
    return Workstream(
        id=f"id-{name}-0123456789",
        name=name,
        title=name.replace("-", " "),
        state="open",
        created=None,
        touched=None,
        refs=list(refs),
        claims=list(claims),
        released=list(released),
    )


@pytest.fixture
def two(synthetic_store, monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "live_registry", lambda *a, **k: [])
    monkeypatch.setattr(anchors, "_default_gh_runner", lambda argv: (1, ""))
    monkeypatch.setenv("WORKSTREAM_DOCS_ROOT", str(tmp_path / "docs"))
    case = case_two_claims(synthetic_store)
    worker, helper = case["sessions"]["worker"], case["sessions"]["helper"]
    early, late = case["subagents"]["early"], case["subagents"]["late"]
    holder = f"agent:{worker.ref}"
    a = _ws(
        "bobbin-rewrite",
        [worker.ref, early.ref, late.ref],
        released=[
            Claim(
                holder,
                _i(case["a_claim"]),
                None,
                live=False,
                released_at=_i(case["a_release"]),
            )
        ],
    )
    b = _ws(
        "spool-cache",
        [worker.ref, early.ref, late.ref, helper.ref],
        claims=[Claim(holder, _i(case["b_claim"]), None)],
    )
    return {**case, "a": a, "b": b, "log": hooklog.load(registry=[])}


def _build(two, ws):
    return F.build(
        ws,
        log=two["log"],
        gh_runner=lambda argv: (1, ""),
        now=NOW,
        history=[two["a"], two["b"]],
    )


# --- windows -------------------------------------------------------------------


def test_windows_reach_back_over_the_gap_to_the_previous_release(two):
    ref = two["sessions"]["worker"].ref
    hist = [two["a"], two["b"]]
    # The first claim reaches back to the session's start; the second starts
    # where the first was released, and stays open.
    assert store.session_windows(ref, two["a"], hist) == [(None, two["a_release"])]
    assert store.session_windows(ref, two["b"], hist) == [(two["a_release"], None)]
    assert store.session_windows(two["sessions"]["helper"].ref, two["b"], hist) is None


def test_a_claim_still_held_elsewhere_gives_no_gap(two):
    ref = two["sessions"]["worker"].ref
    held_a = _ws("bobbin-rewrite", [ref], claims=[Claim(f"agent:{ref}", _i(two["a_claim"]), None)])
    assert store.session_windows(ref, two["b"], [held_a]) == [(two["b_claim"], None)]


def test_an_expired_unreleased_claim_ends_at_its_expiry(two):
    ref = two["sessions"]["worker"].ref
    lapsed = _ws(
        "bobbin-rewrite",
        [ref],
        claims=[Claim(f"agent:{ref}", _i(two["a_claim"]), _i(two["a_release"]), live=False)],
    )
    # The lapse is a real end: A's window stops there and B reaches back to it.
    assert store.session_windows(ref, lapsed, [two["b"]]) == [(None, two["a_release"])]
    assert store.session_windows(ref, two["b"], [lapsed]) == [(two["a_release"], None)]


# --- attach ----------------------------------------------------------------------


@needs_weft
def test_attach_new_releases_the_previous_claim_and_reaches_back_to_it(two, capsys):
    worker = two["sessions"]["worker"]
    sid = worker.id
    code, _, _ = run(capsys, "attach", "--new", "Bobbin", "--name", "ws-a", "--session", sid)
    assert code == 0
    code, out, _ = run(capsys, "attach", "--new", "Spool", "--name", "ws-b", "--session", sid)
    assert code == 0
    assert f"released {worker.ref}'s canonical claim on ws-a" in out
    a, b = store.get("ws-a"), store.get("ws-b")
    assert a.canonical_holders() == [] and b.canonical_holders() == [f"agent:{worker.ref}"]
    released = a.intervals()[0].released_at.utc
    assert released <= b.intervals()[0].at.utc
    assert store.session_windows(worker.ref, b, [a]) == [(released, None)]
    assert store.session_windows(worker.ref, a, [b]) == [(None, released)]


@needs_weft
def test_the_claim_note_is_the_latest_prompt_or_the_new_title(two, capsys):
    sid = two["sessions"]["worker"].id
    run(capsys, "attach", "--new", "Bobbin", "--name", "ws-a", "--session", sid, "--no-sync")
    # The latest prompt, not the session's first ("rewrite the bobbin loader").
    assert store.get("ws-a").intervals()[0].note == "now add the spool cache"
    run(capsys, "attach", "--new", "Spool", "--name", "ws-b", "--session", sid, "--no-sync")
    # That prompt predates the claim on ws-a, so it is ws-a's; the title speaks for ws-b.
    assert store.get("ws-b").intervals()[0].note == "Spool"


# --- sync ----------------------------------------------------------------------


def _added(report) -> set[str]:
    return {r["ref"] for r in report.added}


def test_sync_gives_each_workstream_its_own_interval(two):
    hist = [two["a"], two["b"]]
    late = sync.run([two["b"]], others=[two["a"]], dry_run=True, retract=False, history=hist)
    assert _added(late) == two["late"]
    early = sync.run([two["a"]], others=[two["b"]], dry_run=True, retract=False, history=hist)
    assert _added(early) == two["early"]


def test_without_a_claim_the_whole_session_still_counts(two):
    """An ancillary session has no interval: today's whole-session behaviour."""
    worker = two["sessions"]["worker"]
    loose = _ws("spool-cache", [worker.ref])
    report = sync.run([loose], ancillary=True, dry_run=True, retract=False)
    assert _added(report) == two["early"] | two["late"]


# --- frontier / show -----------------------------------------------------------


def test_frontier_for_b_shows_nothing_from_the_a_interval(two):
    worker, helper = two["sessions"]["worker"], two["sessions"]["helper"]
    fr = _build(two, two["b"])
    by_id = {s.facts.id: s for s in fr.sessions}
    assert two["subagents"]["early"].id not in by_id
    assert two["subagents"]["late"].id in by_id
    w = by_id[worker.id]
    assert w.scope == [(two["a_release"], None)]
    assert [t for _, t in w.facts.human_prompts] == ["now add the spool cache"]
    assert w.facts.subagents == [two["subagents"]["late"].id]
    assert all(at.utc >= two["a_release"] for at, _, _ in w.facts.last_tools)
    # Identity and context stay whole: they describe the transcript.
    assert w.facts.started.utc == datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    assert [m.summary for m in fr.messages] == ["Spool cache is in"]
    worker_rows = [r for r in fr.timeline if worker.id[:8] in r[2] or worker.ref in r[2]]
    assert worker_rows and all(at.utc >= two["a_release"] for at, _, _ in worker_rows)
    # The ancillary helper is whole, as before.
    h = by_id[helper.id]
    assert h.scope is None
    # Same log as build: without it the offsets fall back to the host's zone.
    assert h.facts.human_prompts == sessions.facts(helper.id, log=two["log"]).human_prompts
    assert any(k == "prompt" and helper.id[:8] in t for _, k, t in fr.timeline)


def test_frontier_for_a_still_shows_its_own(two):
    worker = two["sessions"]["worker"]
    fr = _build(two, two["a"])
    by_id = {s.facts.id: s for s in fr.sessions}
    assert two["subagents"]["early"].id in by_id
    assert two["subagents"]["late"].id not in by_id
    w = by_id[worker.id]
    # Released, so ancillary now; the interval is still what counts.
    assert w.role == "ancillary" and w.scope == [(None, two["a_release"])]
    assert [t for _, t in w.facts.human_prompts] == ["rewrite the bobbin loader"]
    assert [m.summary for m in fr.messages] == ["Bobbin rewrite is up for a look"]
    assert all(at.utc <= two["a_release"] for at, _, _ in fr.timeline)


def test_build_reads_each_subagent_transcript_once(two, monkeypatch):
    """Scoping a parent cuts its subagent list with the starts `build` already
    read; it opens no subagent transcript of its own."""
    reads: list[str] = []
    for name in ("_head_entries", "_full_entries"):
        real = getattr(sessions, name)

        def counted(path, *a, _real=real, **k):
            reads.append(str(path))
            return _real(path, *a, **k)

        monkeypatch.setattr(sessions, name, counted)
    fr = _build(two, two["b"])
    worker = next(s for s in fr.sessions if s.facts.id == two["sessions"]["worker"].id)
    assert worker.facts.subagents == [two["subagents"]["late"].id]
    for sub in two["subagents"].values():
        assert reads.count(str(sub.path)) <= 1, sub.name


def test_capture_carries_the_scope(two, tmp_path):
    fr = _build(two, two["b"])
    d = F.to_dict(fr)
    w = next(s for s in d["sessions"] if s["id"] == two["sessions"]["worker"].id)
    assert w["scope"] == [["2026-10-30T12:00:00Z", None]]
    assert [t for _, t in w["human_prompts"]] == ["now add the spool cache"]


def test_show_renders_the_interval_not_the_whole_transcript(two):
    fr = _build(two, two["b"])
    con = Console(file=StringIO(), width=300, no_color=True)
    cli._print_show(con, two["b"], fr, local=False)
    out = con.file.getvalue()
    worker = two["sessions"]["worker"]
    row = next(line for line in out.splitlines() if line.startswith(f"  {worker.ref}"))
    assert "2026-10-30 12:00Z → 2026-10-30 14:30Z" in row
    assert "rewrite the bobbin loader" not in out


# --- concise frontier ------------------------------------------------------------


def _render(two, full: bool) -> str:
    fr = _build(two, two["b"])
    con = Console(file=StringIO(), width=300, no_color=True)
    cli._print_frontier(con, fr, False, {}, full=full)
    return con.file.getvalue()


def test_concise_frontier_counts_subagents_and_drops_the_timeline(two):
    out = _render(two, full=False)
    late = two["subagents"]["late"]
    assert "timeline" not in out and "messages" not in out
    assert "↳ 1 subagent\n" in out
    assert late.ref not in out
    assert "last prompt:" not in out
    assert "loose ends" in out and "options (default: " in out


def test_full_frontier_restores_rows_messages_and_timeline(two):
    out = _render(two, full=True)
    assert "timeline (UTC)" in out and "\nmessages\n" in out
    assert two["subagents"]["late"].ref in out
    assert "last prompt: now add the spool cache" in out
    assert "loose ends" in out


# --- status words ----------------------------------------------------------------


def _a(ref: str, **lv) -> Anchor:
    scheme, _, value = ref.partition(":")
    return Anchor(ref, scheme, value, lv)


@pytest.mark.parametrize(
    "anchor, word",
    [
        (_a("gh:acme/spindle#41", state="MERGED"), "merged"),
        (_a("gh:acme/spindle#42", state="OPEN", is_draft=False), "open"),
        (_a("gh:acme/spindle#43", state="OPEN", is_draft=True), "draft"),
        (_a("gh:acme/spindle#44", state="CLOSED"), "closed"),
        (_a("gh:acme/spindle#45", not_probed=True), "unknown"),
        (_a("wt:/w/gone-merged", exists=False, merged_into_default=True), "gone (merged)"),
        (_a("wt:/w/gone", exists=False), "gone"),
        (_a("wt:/w/dirty", exists=True, dirty=True, unpushed=2, unmerged=1), "dirty"),
        (_a("wt:/w/unpushed", exists=True, unpushed="no upstream", unmerged=1), "unpushed"),
        (_a("wt:/w/unmerged", exists=True, unpushed=0, unmerged=1), "unmerged"),
        (_a("wt:/w/landed", exists=True, unmerged=1, merged_into_default=True), "merged"),
        (_a("wt:/w/clean", exists=True, unpushed=0, unmerged=0), "exists"),
        (_a("wt:/w/unprobed"), "unknown"),
        (_a("branch:spindle@x", exists_local=True, exists_remote=False), "unpushed"),
        (
            _a(
                "branch:spindle@y", exists_local=True, exists_remote=True, merged_into_default=False
            ),
            "unmerged",
        ),
        (
            _a(
                "branch:spindle@z",
                exists_local=False,
                exists_remote=False,
                merged_into_default=True,
            ),
            "gone (merged)",
        ),
        (_a("file:/notes.md", exists=True), "exists"),
    ],
)
def test_status_word(anchor, word):
    assert F.anchor_status(anchor) == word


def test_a_loose_end_names_its_anchor_once(synthetic_store):
    a = anchors.liveness("gh:acme/spindle#52", gh_runner=lambda argv: (1, ""))
    assert a.warnings == ["neither gh pr view nor gh issue view answered"]
    assert F._loose_ends([], [a], []) == [
        "gh:acme/spindle#52: neither gh pr view nor gh issue view answered"
    ]
