#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pytest", "playwright", "mistune>=3.0.0"]
# ///
"""Browser tests for mdserve's Content-Security-Policy.

The CSP is the one part of mdserve that no file-level assertion can check. A
policy string can be spelled correctly, appear in every page, and still refuse
something the page needs -- or admit something it shouldn't -- and the suite
would stay green either way. Both halves of that have already happened: the
nonce-based CSP shipped with `frame-src` falling through to `default-src 'none'`, silently
dropping an embed the passthrough test's own docstring called deliberate, and
the first, hash-based policy blocked the external highlight.js while every
file-level test still passed.

So these load real pages in a real browser and ask what the browser did.

Pages are served over HTTP rather than opened as `file://`, which exercises
`CSPRequestHandler`'s header alongside the in-page meta tag, and avoids the
origin quirks that would otherwise make a `file://` result mean something else.

highlight.js is stubbed by intercepting the cdnjs request, so the suite neither
needs the network nor goes red when a CDN has a bad day. The interception does
not bypass the policy: a fulfilled route is still subject to `script-src`, so
the assertion that it loads is still an assertion about the nonce on that tag.

Needs the chromium binary once: `uvx playwright install chromium`.
"""

from __future__ import annotations

import http.server
import sys
import threading
from functools import partial
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_api.sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mdserve  # noqa: E402

HLJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/**"

# Stands in for highlight.js. It only has to prove the tag was allowed to load
# and run, so it records that it did and gives `hljs.highlightAll()` -- called by
# the next inline script -- something to find.
HLJS_STUB = (
    "window.hljs = {"
    "  highlightAll: function(){ window.__HLJS_RAN = true; },"
    "  highlightElement: function(){}"
    "};"
)

# Collects every refusal the page makes, before any of the page's own script
# runs. An `on('securitypolicyviolation')` attached after load would miss the
# violations raised during parsing, which is all of the interesting ones.
VIOLATION_RECORDER = """
window.__CSP = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__CSP.push({directive: e.effectiveDirective, blocked: e.blockedURI});
});
"""


@pytest.fixture(scope="session")
def browser():
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("browser tests are only wired up for macOS and Linux")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - any launch failure means "not installed"
            pytest.skip(f"chromium unavailable ({exc}); run: uvx playwright install chromium")
        yield b
        b.close()


@pytest.fixture
def serve(tmp_path):
    """Convert a markdown body and serve the result, yielding the page URL."""
    servers = []

    def go(markdown: str, name: str = "doc.md") -> str:
        src, out = tmp_path / "src", tmp_path / "out"
        src.mkdir(exist_ok=True)
        out.mkdir(exist_ok=True)
        (src / name).write_text(markdown, encoding="utf-8")
        mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))

        handler = partial(mdserve.CSPRequestHandler, directory=str(out))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/{name}.html"

    yield go
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    pg.add_init_script(VIOLATION_RECORDER)
    pg.route(
        HLJS_URL,
        lambda route: route.fulfill(
            status=200, content_type="application/javascript", body=HLJS_STUB
        ),
    )
    yield pg
    pg.close()


def violations(page) -> list:
    return page.evaluate("window.__CSP")


def test_an_injected_script_is_refused(page, serve):
    """The claim the whole policy exists to make.

    Note when weakening this to check the test still bites: adding
    `'unsafe-inline'` alongside the nonce proves nothing, because a browser
    ignores `'unsafe-inline'` in a `script-src` that carries a nonce. Replace
    the nonce to see this fail.
    """
    page.goto(serve("# Doc\n\n<script>window.PWNED = 1</script>\n"))
    assert page.evaluate("window.PWNED") is None
    assert any(v["directive"] == "script-src-elem" for v in violations(page)), violations(page)


def test_an_injected_event_handler_is_refused(page, serve):
    """Inline handlers need `unsafe-inline`; a nonce does not grant them."""
    page.goto(serve('# Doc\n\n<img src=x onerror="window.PWNED = 1">\n'))
    page.wait_for_timeout(100)  # the handler would fire on the load failure
    assert page.evaluate("window.PWNED") is None
    assert any("script-src" in v["directive"] for v in violations(page)), violations(page)


def test_a_clean_page_raises_no_violations(page, serve):
    """The half a hash-based policy got wrong: refusing the page's own scripts.

    The first, hash-based policy blocked highlight.js and both large inline
    blocks while every file-level assertion still passed. A clean document must load in silence.
    """
    page.goto(serve("# Doc\n\nProse.\n\n```python\nx = 1\n```\n"))
    assert violations(page) == []


def test_the_pages_own_scripts_still_run(page, serve):
    """A policy that silences the page is not a fix."""
    page.goto(serve("# Doc\n\nProse.\n"))
    assert page.evaluate("typeof NAV_DATA") == "object"
    assert page.evaluate("window.__HLJS_RAN") is True


def test_a_video_embed_is_admitted_and_an_iframe_is_refused(page, serve):
    """The embed split, in the only place it is observable.

    `media-src` is open and `frame-src` falls through to `default-src 'none'`,
    which is a decision rather than an oversight -- see the comment on `CSP`.
    Nothing in the generated HTML distinguishes the two cases: both elements are
    emitted verbatim, and only the browser can say which one was allowed to
    fetch.
    """
    page.goto(
        serve(
            "# Doc\n\n"
            '<video src="https://example.invalid/clip.mp4"></video>\n\n'
            '<iframe src="https://example.invalid/page"></iframe>\n'
        )
    )
    page.wait_for_timeout(100)
    refused = violations(page)
    assert any(v["directive"] == "frame-src" for v in refused), refused
    assert not any(v["directive"] == "media-src" for v in refused), refused


def test_a_script_close_in_a_file_leaves_the_page_working(page, serve):
    """The fix for the defect the CSP could only defang.

    `</script>` in a document used to end the page's own script element early,
    taking the navigation and the source view with it. That is invisible to a
    file-level assertion -- the bytes are all present in the HTML either way --
    and it is the browser's parser that decides where the script stops.
    """
    body = "# Doc\n\nBefore.\n\n</script>\n\nAfter.\n"
    page.goto(serve(body))

    # The script that defines these is after the injection point, so any of them
    # being undefined means the element was truncated.
    assert page.evaluate("typeof PAGE_DATA") == "object"
    assert page.evaluate("typeof showRawMarkdown") == "function"
    assert page.evaluate("RAW_CONTENT") == body

    page.evaluate("showRawMarkdown()")
    assert page.locator("#rawContent").inner_text().strip() == body.strip()
    assert violations(page) == []


def test_the_toolbar_buttons_respond_to_clicks(page, serve):
    """Calling a handler is not clicking its button.

    The buttons were wired with onclick= attributes, which the nonce-only
    `script-src` refuses, so every one was dead while the test above, which
    calls `showRawMarkdown()` directly, stayed green.
    """
    url = serve("# Doc\n\nProse.\n")
    page.goto(url)
    page.click(".floating-buttons [data-action='showRawMarkdown']")
    assert page.locator("#rawContent").is_visible()
    page.click(".modal-close")
    assert not page.locator("#rawContent").is_visible()
    # Navigating away resets the recorder, so check this page's refusals here.
    assert violations(page) == []

    page.goto(url.rsplit("/", 1)[0] + "/index.html")
    page.click(".filter-btn[data-filter='.md']")
    assert page.locator("#searchInput").input_value() == ".md"
    assert "active" in page.get_attribute(".filter-btn[data-filter='.md']", "class")
    page.click(".clear-btn")
    assert page.locator("#searchInput").input_value() == ""
    assert violations(page) == []


def test_a_files_own_data_action_markup_triggers_nothing(page, serve):
    """Rendered files keep their raw HTML, so the wiring must not reach into it."""
    page.goto(serve('# Doc\n\n<button id="gadget" data-action="showRawMarkdown">x</button>\n'))
    page.click("#gadget")
    assert not page.locator("#rawContent").is_visible()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
