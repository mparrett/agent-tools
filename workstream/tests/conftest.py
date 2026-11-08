"""Shared fixtures: every store a module reads, built synthetically under tmp_path."""

from __future__ import annotations

import pytest
from synthetic import SyntheticStore, build_all


@pytest.fixture(autouse=True)
def _isolate_weft_actor(monkeypatch):
    """`workstream hook` sets WEFT_ACTOR in os.environ, and the session lookup
    is cached per process; neither may carry from one test to the next."""
    from workstream import store

    # setenv then delenv: teardown removes the key even if a test set it.
    monkeypatch.setenv("WEFT_ACTOR", "")
    monkeypatch.delenv("WEFT_ACTOR")
    store._session_for_actor.cache_clear()
    yield
    store._session_for_actor.cache_clear()


@pytest.fixture
def synthetic_store(tmp_path, monkeypatch):
    store = SyntheticStore(tmp_path / "store")
    for key, value in store.env().items():
        monkeypatch.setenv(key, value)
    # gh cache lives under XDG_CACHE_HOME (anchors.py); keep it in the tmp root too.
    monkeypatch.setenv("XDG_CACHE_HOME", str(store.root / "xdg-cache"))
    # The weft binary itself is left to seam.weft_bin(); only its store is redirected.
    yield store


@pytest.fixture
def acceptance_cases(synthetic_store):
    return build_all(synthetic_store)
