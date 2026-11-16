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

import re
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


# Matches any present value, so not_to_have_attribute(name, ANY_VALUE) waits
# for the attribute to be removed.
ANY_VALUE = re.compile(".*")


def _csp_errors(page):
    errors = []
    page.on(
        "console",
        lambda m: (
            m.type == "error" and "Content Security Policy" in m.text and errors.append(m.text)
        ),
    )
    return errors


def test_stored_prefs_apply_before_the_ui_script(browser, viewer):
    """The <head> script sets stored prefs before first paint. It runs under
    its own CSP hash, so a blocked script would show up here as the default
    face, not as an error anyone sees."""
    base, _, _ = viewer
    page = browser.new_page()
    errors = _csp_errors(page)
    page.goto(base + "/")
    page.evaluate(
        "localStorage.setItem('mdfeed-font', 'atkinson'); localStorage.setItem('mdfeed-size', 'large')"
    )
    page.reload()
    root = page.locator("html")
    expect(root).to_have_attribute("data-font", "atkinson")
    expect(root).to_have_attribute("data-size", "large")
    family = page.evaluate("getComputedStyle(document.getElementById('preview')).fontFamily")
    assert family.startswith('"Atkinson Hyperlegible Next"')
    assert page.evaluate("getComputedStyle(document.getElementById('preview')).fontSize") == "18px"
    assert errors == []
    page.close()


def test_prefs_panel_sets_and_clears_a_pref(browser, viewer):
    base, _, _ = viewer
    page = browser.new_page()
    page.goto(base + "/")
    expect(page.locator("#status")).to_have_text("live")
    page.keyboard.press(",")
    panel = page.locator("#prefs")
    expect(panel).to_be_visible()
    page.locator('#prefs button[data-pref="font"][data-value="atkinson"]').click()
    expect(page.locator("html")).to_have_attribute("data-font", "atkinson")
    assert page.evaluate("localStorage.getItem('mdfeed-font')") == "atkinson"
    expect(page.locator('#prefs button[data-value="atkinson"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    # Back to the default: no attribute and no stored key, so the stylesheet's
    # :root values rule and a later change to the default reaches this reader.
    page.locator('#prefs button[data-pref="font"][data-value="instrument"]').click()
    # A face switch waits on the font load; expect() polls. (wait_for_function
    # can't: its polling evals a string, which the page's CSP refuses.)
    expect(page.locator("html")).not_to_have_attribute("data-font", ANY_VALUE)
    assert page.evaluate("localStorage.getItem('mdfeed-font')") is None
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    page.close()


def test_panel_swallows_global_shortcuts(browser, viewer):
    """With a segment focused, 'z' must not flip zen behind the panel."""
    base, _, _ = viewer
    page = browser.new_page()
    page.goto(base + "/")
    page.keyboard.press(",")
    expect(page.locator("#prefs")).to_be_visible()
    page.keyboard.press("z")
    assert page.evaluate("document.documentElement.hasAttribute('data-reader')") is False
    page.close()


def test_size_key_cycles_and_old_small_choice_carries_over(browser, viewer):
    """The old size toggle stored 'small' under the same key; it must still
    read as small, and 'f' now steps through all four sizes."""
    base, _, _ = viewer
    page = browser.new_page()
    page.goto(base + "/")
    page.evaluate("localStorage.setItem('mdfeed-size', 'small')")
    page.reload()
    root = page.locator("html")
    expect(root).to_have_attribute("data-size", "small")
    page.keyboard.press("f")
    expect(root).not_to_have_attribute("data-size", ANY_VALUE)
    page.keyboard.press("f")
    expect(root).to_have_attribute("data-size", "large")
    page.keyboard.press("f")
    expect(root).to_have_attribute("data-size", "xlarge")
    page.close()


def test_theme_button_still_cycles(browser, viewer):
    base, _, _ = viewer
    page = browser.new_page()
    page.goto(base + "/")
    btn = page.locator("#theme-toggle")
    expect(btn).to_have_text("auto")
    btn.click()
    expect(btn).to_have_text("light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
