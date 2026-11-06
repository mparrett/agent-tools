#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pytest", "playwright", "mistune>=3.0.0"]
# ///
"""Browser tests for the two ways `mdfeed open` steers the viewer.

Both live entirely in the page's inline script, and neither can be checked
server-side. The server's part is a query string it never interprets and an SSE
line it writes blind; whether the right file ends up on screen is decided by
JavaScript reading `location.search`, by which filters happen to be persisted
in that browser's localStorage, and by an EventSource listener.

The filter case is the reason this file exists. A deep link names a file the
persisted project filter or the Plans/Lanes view may exclude, and a viewer that
quietly selects nothing looks exactly like a broken link.

The CSP pins script execution to a hash of that inline script, so any page here
that runs at all is also evidence the policy still admits it.

Needs the chromium binary once: `uvx playwright install chromium`.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_api.sync_playwright
expect = playwright_api.expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mdfeed  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def viewer(tmp_path):
    """A running viewer over a feed holding one file per project.

    `cwd` per append is what gives the two entries different projects: the
    files sit outside any repo, so derive_project falls back to it.
    """
    docs = {}
    feed = tmp_path / "index.jsonl"
    for project in ("alpha", "beta"):
        doc = tmp_path / project / f"{project}.md"
        doc.parent.mkdir()
        doc.write_text(f"# {project.title()} heading\n\nbody\n")
        docs[project] = doc.resolve()
        mdfeed.append_entries(feed, [doc], cwd=str(doc.parent))

    old_feed = mdfeed.FeedHandler.feed_path
    mdfeed.FeedHandler.feed_path = feed
    srv = mdfeed.ThreadingServer(("127.0.0.1", 0), mdfeed.FeedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", feed, docs
    finally:
        srv.shutdown()
        srv.server_close()
        mdfeed.FeedHandler.feed_path = old_feed


def test_no_deep_link_lands_on_the_newest(browser, viewer):
    base, _, docs = viewer
    page = browser.new_page()
    page.goto(base + "/")
    expect(page.locator("#preview-path")).to_have_text(str(docs["beta"]))
    page.close()


def test_deep_link_selects_the_named_file(browser, viewer):
    """alpha is the older entry, so this is not the default selection."""
    base, _, docs = viewer
    page = browser.new_page()
    page.goto(base + "/?path=" + str(docs["alpha"]))
    expect(page.locator("#preview-path")).to_have_text(str(docs["alpha"]))
    page.close()


def test_deep_link_is_stripped_from_the_url(browser, viewer):
    # Left in place, a reload re-selects the deep-linked file forever, and the
    # path sits in history where the next `?path=` would be ambiguous.
    base, _, docs = viewer
    page = browser.new_page()
    page.goto(base + "/?path=" + str(docs["alpha"]))
    expect(page.locator("#preview-path")).to_have_text(str(docs["alpha"]))
    assert page.evaluate("location.search") == ""
    page.close()


def test_deep_link_clears_a_filter_that_hides_the_file(browser, viewer):
    base, _, docs = viewer
    page = browser.new_page()
    page.goto(base + "/")
    page.evaluate("localStorage.setItem('mdfeed-projects', JSON.stringify(['beta']))")
    page.goto(base + "/?path=" + str(docs["alpha"]))
    expect(page.locator("#preview-path")).to_have_text(str(docs["alpha"]))
    assert page.evaluate("localStorage.getItem('mdfeed-projects')") == "[]"
    page.close()


def test_deep_link_leaves_a_filter_that_does_not_hide_the_file(browser, viewer):
    """Clearing is a last resort, not the greeting: a link to a file already on
    screen must not throw away the filter the user set."""
    base, _, docs = viewer
    page = browser.new_page()
    page.goto(base + "/")
    page.evaluate("localStorage.setItem('mdfeed-projects', JSON.stringify(['alpha']))")
    page.goto(base + "/?path=" + str(docs["alpha"]))
    expect(page.locator("#preview-path")).to_have_text(str(docs["alpha"]))
    assert page.evaluate("localStorage.getItem('mdfeed-projects')") == '["alpha"]'
    page.close()


def test_clearing_a_filter_keeps_lanes_which_hide_nothing(browser, viewer):
    """Only Plans can hide the target. When the project filter has to go,
    Lanes stays: it regroups the list, and resetting it to Recent threw away a
    view the user chose for no reason."""
    base, _, docs = viewer
    page = browser.new_page()
    page.goto(base + "/")
    page.evaluate("localStorage.setItem('mdfeed-view', 'lanes')")
    page.evaluate("localStorage.setItem('mdfeed-projects', JSON.stringify(['beta']))")
    page.goto(base + "/?path=" + str(docs["alpha"]))
    expect(page.locator("#preview-path")).to_have_text(str(docs["alpha"]))
    assert page.evaluate("localStorage.getItem('mdfeed-projects')") == "[]"
    assert page.evaluate("localStorage.getItem('mdfeed-view')") == "lanes"
    page.close()


def test_focus_marker_moves_an_open_page(browser, viewer):
    base, feed, docs = viewer
    page = browser.new_page()
    page.goto(base + "/")
    expect(page.locator("#preview-path")).to_have_text(str(docs["beta"]))
    expect(page.locator("#status")).to_have_text("live")  # SSE attached
    ts = mdfeed.write_focus(feed, docs["alpha"])
    expect(page.locator("#preview-path")).to_have_text(str(docs["alpha"]), timeout=10_000)
    # And says so: `open` reports success on this ack, not on the census.
    host, port = base.rsplit("/", 1)[1].split(":")
    assert mdfeed.wait_for_focus_ack(host, int(port), ts, wait=5.0)
    page.close()


def test_focus_marker_clears_a_filter_that_hides_the_file(browser, viewer):
    base, feed, docs = viewer
    page = browser.new_page()
    page.goto(base + "/")
    page.evaluate("localStorage.setItem('mdfeed-projects', JSON.stringify(['beta']))")
    page.reload()
    expect(page.locator("#status")).to_have_text("live")
    mdfeed.write_focus(feed, docs["alpha"])
    expect(page.locator("#preview-path")).to_have_text(str(docs["alpha"]), timeout=10_000)
    page.close()


def test_focus_marker_for_a_file_not_in_the_feed_changes_nothing(browser, viewer, tmp_path):
    """The feed is the server's allowlist; a path outside it would render 403.
    Staying put beats blanking the pane."""
    base, feed, docs = viewer
    page = browser.new_page()
    page.goto(base + "/")
    expect(page.locator("#preview-path")).to_have_text(str(docs["beta"]))
    ts = mdfeed.write_focus(feed, tmp_path / "nowhere.md")
    page.wait_for_timeout(2000)
    expect(page.locator("#preview-path")).to_have_text(str(docs["beta"]))
    # Nothing moved, so nothing is acknowledged and `open` falls back to a tab.
    host, port = base.rsplit("/", 1)[1].split(":")
    assert not mdfeed.wait_for_focus_ack(host, int(port), ts, wait=0)
    page.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
