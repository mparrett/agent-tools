"""Live-store smoke test — label-quality invariants over the *real* session store.

This is the guard fixtures can't be: it catches a new Claude Code injection-wrapper
tag leaking into a session's first-user label (the `<task-notification>` regression),
which only shows up against real transcripts.

It stays CI-safe and leak-safe by construction:
- **Skips** when no store / no claude-ls seam is present (fresh machine, CI), or when
  the store is too small to be meaningful.
- Asserts **properties, never specific content** — no private data is embedded or
  printed. Failure messages name only Claude Code's generic tag vocabulary.
- **Bounded**: samples a capped number of recent sessions so it stays fast.
"""

import re

import pytest
from claude_graph import sessions
from claude_graph.graph import load_session_record

_SAMPLE = 40  # cap the walk (newest-first per project) — keep the smoke test snappy
_MIN = 5  # below this the store isn't worth asserting over

# Claude Code's injected wrappers (caveat / command / task-notification / …) are all
# lowercase *hyphenated* tags. Real prose that opens with a tag is almost always a
# non-hyphenated HTML element ("<div> won't render"), so a hyphenated leading tag is a
# reliable signature for an injection wrapper we failed to strip — known or brand new.
_HYPHENATED_TAG = re.compile(r"^<([a-z][a-z0-9]*-[a-z0-9-]*)>")


def _sample_records():
    store = sessions.projects_dir()
    if not store.is_dir():
        pytest.skip(f"no live session store at {store}")
    recs = []
    try:
        for sf in sessions.iter_session_files():
            r = load_session_record(sf)
            if r:
                recs.append(r)
            if len(recs) >= _SAMPLE:
                break
    except RuntimeError as e:  # seam couldn't locate claude-ls
        pytest.skip(str(e))
    if len(recs) < _MIN:
        pytest.skip(f"live store has too few sessions ({len(recs)}) to smoke-test")
    return recs


def test_no_injection_wrapper_leaks_into_first_user_labels():
    """The canary: no first-user label starts with a hyphenated injection tag."""
    recs = _sample_records()
    offenders: dict = {}  # leading tag -> count (tag names only; no private content)
    for r in recs:
        m = _HYPHENATED_TAG.match(r.first_user or "")
        if m:
            offenders[m.group(1)] = offenders.get(m.group(1), 0) + 1
    assert not offenders, (
        "first-user labels start with hyphenated injection-style tags — a wrapper class "
        f"is leaking through _first_user_text. Add to _WRAPPER_TAGS: {sorted(offenders)}"
    )


def test_pipeline_produces_usable_records_on_live_store():
    """End-to-end smoke: the seam + parsing + canonicalization actually yield usable
    records against real data (not just synthetic fixtures)."""
    recs = _sample_records()
    assert any(r.first_user for r in recs), "no session produced a first-user label"
    assert any(r.repos for r in recs), "no session resolved a repo"
