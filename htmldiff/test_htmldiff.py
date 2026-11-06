#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "playwright"]
# ///
"""Browser tests for htmldiff.html.

The algorithm cases are lifted verbatim from the upstream C# suite
(Rohland/htmldiff.net, src/HtmlDiff.Tests/HtmlDiffSpecTests.cs), which is what
makes them worth having: they are the contract the port has to meet, and a
subtle mistranslation of the tokenizer or the formatting-tag stack shows up
here as a wrong string rather than as slightly-off output nobody notices.

The page has no build step, so the tests load it over file:// and call into
`window.htmldiff`. Needs the chromium binary once:

    uvx playwright install chromium
"""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_api.sync_playwright

PAGE = Path(__file__).resolve().parent / "htmldiff.html"


@pytest.fixture(scope="module")
def page_errors() -> list[str]:
    """Uncaught page errors, drained per test by `_attribute_page_errors`."""
    return []


@pytest.fixture(scope="module")
def browser():
    """Separate from `page` so a test that has to open a *second* page — one
    reading a saved report, say — can, which `page.context` cannot offer: the
    context behind `browser.new_page()` is implicit and refuses another page.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield browser
        finally:
            # In a `finally` because the per-test error assertion below can
            # fail, and a browser left open leaks the chromium process.
            browser.close()


@pytest.fixture(scope="module")
def page(browser, page_errors):
    page = browser.new_page()
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.goto(PAGE.as_uri())
    page.wait_for_function("window.htmldiff !== undefined")
    return page


@pytest.fixture(autouse=True)
def _attribute_page_errors(request):
    """Fail the test that *caused* a page error.

    Asserting this in the module-scoped `page` fixture instead reports every
    error against whichever test happens to run last: a SyntaxError from one
    test surfaced as a failure in an unrelated one, while the test that caused
    it showed only a 30s `wait_for_selector` timeout.
    """
    if "page" not in request.fixturenames:
        yield
        return
    errors = request.getfixturevalue("page_errors")
    errors.clear()
    yield
    assert not errors, f"page errors: {errors}"


def diff(page, old: str, new: str, **options) -> str:
    return page.evaluate(
        "([old, new_, opts]) => window.htmldiff.htmlDiff(old, new_, opts)",
        [old, new, options],
    )


def counter(page) -> str:
    """The rendered view counts its marks only after the frame loads, so the
    counter reads "…" until then. Waiting on the element alone races that."""
    page.wait_for_function("() => document.querySelector('#counter').textContent !== '…'")
    return page.inner_text("#counter")


# ---------------------------------------------------------------- algorithm

# (old, new, expected) — upstream Execute_WithStandardSpecs_OutputVerified
SPECS = [
    (
        "a word is here",
        "a nother word is there",
        "a<ins class='diffins'>&nbsp;nother</ins> word is "
        "<del class='diffmod'>here</del><ins class='diffmod'>there</ins>",
    ),
    ("a c", "a b c", "a <ins class='diffins'>b </ins>c"),
    ("a b c", "a c", "a <del class='diffdel'>b </del>c"),
    ("a b c", "a <strong>b</strong> c", "a <strong><ins class='mod'>b</ins></strong> c"),
    ("a b c", "a d c", "a <del class='diffmod'>b</del><ins class='diffmod'>d</ins> c"),
    ("<a title='xx'>test</a>", "<a title='yy'>test</a>", "<a title='yy'>test</a>"),
    ("<img src='logo.jpg'/>", "", "<del class='diffdel'><img src='logo.jpg'/></del>"),
    ("", "<img src='logo.jpg'/>", "<ins class='diffins'><img src='logo.jpg'/></ins>"),
    (
        "symbols 'should not' belong <b>to</b> words",
        'symbols should not belong <b>"to"</b> words',
        "symbols <del class='diffdel'>'</del>should not<del class='diffdel'>'</del> belong "
        "<b><ins class='diffins'>\"</ins>to<ins class='diffins'>\"</ins></b> words",
    ),
    (
        "entities are separate amp;words",
        "entities are&nbsp;separate &amp;words",
        "entities are<del class='diffmod'>&nbsp;</del><ins class='diffmod'>&nbsp;</ins>separate "
        "<del class='diffmod'>amp;</del><ins class='diffmod'>&amp;</ins>words",
    ),
    (
        "This is a longer piece of text to ensure the new blocksize algorithm works",
        "This is a longer piece of text to <strong>ensure</strong> the new blocksize "
        "algorithm works decently",
        "This is a longer piece of text to <strong><ins class='mod'>ensure</ins></strong> "
        "the new blocksize algorithm works<ins class='diffins'>&nbsp;decently</ins>",
    ),
    (
        "By virtue of an agreement between xxx and the <b>yyy schools</b>, ...",
        "By virtue of an agreement between xxx and the <b>yyy</b> schools, ...",
        "By virtue of an agreement between xxx and the <b>yyy</b> schools, ...",
    ),
    (
        "Some plain text",
        "Some <strong><i>plain</i></strong> text",
        "Some <strong><i><ins class='mod'>plain</ins></i></strong> text",
    ),
    (
        "Some <strong><i>formatted</i></strong> text",
        "Some formatted text",
        "Some <ins class='mod'>formatted</ins> text",
    ),
    (
        "<table><tr><td>col1</td><td>col2</td></tr><tr><td>Data 1</td><td>Data 2</td></tr></table>",
        "<table><tr><td>col1</td><td>col2</td></tr></table>",
        "<table><tr><td>col1</td><td>col2</td></tr><tr><td>"
        "<del class='diffdel'>Data 1</del></td><td>"
        "<del class='diffdel'>Data 2</del></td></tr></table>",
    ),
    (
        "text",
        '<span style="text-decoration: line-through;">text</span>',
        "<span style=\"text-decoration: line-through;\"><ins class='mod'>text</ins></span>",
    ),
    # CJK diffs per character, having no spaces to tokenize on.
    (
        "这个是中文内容, CSharp is the bast",
        "这是中国语内容，CSharp is the best language.",
        "这<del class='diffdel'>个</del>是中<del class='diffmod'>文</del>"
        "<ins class='diffmod'>国语</ins>内容<del class='diffmod'>, </del>"
        "<ins class='diffmod'>，</ins>CSharp is the <del class='diffmod'>bast</del>"
        "<ins class='diffmod'>best language.</ins>",
    ),
]


@pytest.mark.parametrize("old,new,expected", SPECS, ids=range(len(SPECS)))
def test_upstream_specs(page, old, new, expected):
    assert diff(page, old, new) == expected


@pytest.mark.parametrize(
    "threshold,expected",
    [
        (
            0.2,
            "<del class='diffmod'>one a</del><ins class='diffmod'>two a nother</ins>"
            " word is somewhere",
        ),
        (
            0.1,
            "<del class='diffmod'>one</del><ins class='diffmod'>two</ins> a"
            "<ins class='diffins'>&nbsp;nother</ins> word is somewhere",
        ),
    ],
)
def test_orphan_match_threshold(page, threshold, expected):
    """A lone matching word between two big gaps folds into the change."""
    got = diff(
        page,
        "one a word is somewhere",
        "two a nother word is somewhere",
        orphanMatchThreshold=threshold,
    )
    assert got == expected


def test_block_expression_groups_a_date(page):
    """Without grouping a date diffs field by field; with it, as one unit."""
    old, new = (
        "This is a date 1 Jan 2016 that will change",
        "This is a date 22 Feb 2017 that did change",
    )

    grouped = page.evaluate(
        "([old, new_]) => window.htmldiff.htmlDiff(old, new_, "
        r"{blockExpressions: [/[\d]{1,2}[\s]*(Jan|Feb)[\s]*[\d]{4}/g]})",
        [old, new],
    )
    assert grouped == (
        "This is a date<del class='diffmod'> 1 Jan 2016</del>"
        "<ins class='diffmod'> 22 Feb 2017</ins> that "
        "<del class='diffmod'>will</del><ins class='diffmod'>did</ins> change"
    )

    assert diff(page, old, new).count("<del") == 4


def test_identical_input_is_returned_untouched(page):
    assert diff(page, "<p>same</p>", "<p>same</p>") == "<p>same</p>"


def test_accented_words_are_one_token(page):
    """The departure from upstream's ASCII \\w: 'café' must not split at 'caf'."""
    words = page.evaluate("() => window.htmldiff.convertHtmlToWords('café naïve', [])")
    assert words == ["café", " ", "naïve"]


def test_ignore_whitespace(page):
    assert diff(page, "a   b", "a b", ignoreWhitespace=True) == "a b"
    assert diff(page, "a   b", "a b") != "a b"


def test_ignore_case(page):
    assert diff(page, "Hello There", "hello there", ignoreCase=True) == "hello there"
    assert "<del" in diff(page, "Hello There", "hello there")


# ------------------------------------------------------------- line diffing


def rows(page, old: str, new: str, **options):
    return page.evaluate(
        "([old, new_, opts]) => window.htmldiff.buildRows(old, new_, opts)",
        [old, new, options],
    )


def test_line_diff_pairs_an_edited_line(page):
    result = rows(page, "alpha\nbravo\ncharlie\n", "alpha\nbravo tweaked\ncharlie\n")
    kinds = [r["kind"] for r in result["rows"]]
    assert kinds == ["equal", "mod", "equal", "equal"]
    assert result["stats"] == {"ins": 0, "del": 0, "mod": 1}
    assert "<ins>" in result["rows"][1]["rightHtml"]


def test_line_diff_keeps_unrelated_lines_apart(page):
    """A delete and an insert that share nothing are two rows, not one edit."""
    result = rows(page, "the quick brown fox\n", "entirely different content here\n")
    assert [r["kind"] for r in result["rows"]] == ["del", "ins", "equal"]


def test_line_diff_aligns_on_the_best_pair_not_by_position(page):
    """A deleted line must not shunt every later pairing off by one.

    Positional pairing matched "Grey linen cover" with "Ships 22 Feb 2017"
    purely because both were first in their run.
    """
    old = "<ul>\n<li>Grey linen cover</li>\n<li>Ships 1 Jan 2016</li>\n</ul>\n"
    new = "<ul>\n<li>Ships 22 Feb 2017</li>\n<li>Now in three colours</li>\n</ul>\n"
    result = rows(page, old, new)
    kinds = [r["kind"] for r in result["rows"]]
    assert kinds == ["equal", "del", "mod", "ins", "equal", "equal"]

    edited = next(r for r in result["rows"] if r["kind"] == "mod")
    assert "Ships" in edited["left"] and "Ships" in edited["right"]


def test_line_diff_handles_pure_insertion(page):
    result = rows(page, "one\ntwo\n", "one\nmiddle\ntwo\n")
    assert [r["kind"] for r in result["rows"]] == ["equal", "ins", "equal", "equal"]


def test_line_diff_anchors_on_unique_lines(page):
    """Repeated braces must not pair with a brace far away (patience anchoring)."""
    old = "a {\n}\nb {\n}\n"
    new = "a {\n}\nnew {\n}\nb {\n}\n"
    result = rows(page, old, new)
    assert [r["kind"] for r in result["rows"]].count("ins") == 2
    assert "del" not in [r["kind"] for r in result["rows"]]


# ------------------------------------------------------------------- the UI


def test_sample_runs_and_reports_changes(page):
    page.goto(PAGE.as_uri())
    page.click("#sample")
    page.wait_for_selector("#results:not([hidden])")

    assert "no changes" not in counter(page)
    frame = page.frame_locator("#frame")
    assert frame.locator("ins").count() > 0
    assert frame.locator("del").count() > 0
    # the spine drew a tick per change
    assert page.locator("#spine .tick").count() > 0


def test_views_switch_and_navigation_walks_changes(page):
    page.goto(PAGE.as_uri())
    page.click("#sample")
    page.wait_for_selector("#results:not([hidden])")

    page.click("[data-view='split']")
    assert page.locator("#view-split table.src tr.mod, #view-split table.src tr.del").count() > 0

    page.click("#next")
    assert counter(page).startswith("1 /")
    page.click("#next")
    assert counter(page).startswith("2 /")
    page.click("#prev")
    assert counter(page).startswith("1 /")

    page.click("[data-view='unified']")
    assert page.locator("#view-unified table.src tr.ins").count() > 0


def test_swap_reverses_the_sides(page):
    page.goto(PAGE.as_uri())
    page.fill("#a", "<p>old</p>")
    page.fill("#b", "<p>new</p>")
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")
    assert "old" in page.frame_locator("#frame").locator("del").inner_text()

    page.click("#edit")
    page.click("#swap")
    assert page.input_value("#a") == "<p>new</p>"


def test_pasted_script_does_not_execute(page):
    """The frame has no allow-scripts, so a hostile page renders inert.

    The script must be *identical on both sides*. A script on the changed side
    only has its body wrapped in <ins class='diffins'>, which makes it a syntax
    error before any sandbox matters — this test used that shape and passed
    with the sandbox and the CSP both removed. Unchanged regions pass through
    verbatim, which is the case the frame's lockdown actually has to survive.

    The frame is defended twice over (no allow-scripts, plus `default-src
    'none'` in the srcdoc), so this goes red when *both* are removed and stays
    green when either survives alone — verified by mutating each in turn. That
    is the strongest guarantee a behavioural test can give against redundant
    defenses; asserting each one's presence separately would only re-check that
    the source says what it says.
    """
    page.goto(PAGE.as_uri())
    hostile = "<script>window.parent.__pwned = true;</script>"
    page.fill("#a", f"<p>before</p>{hostile}")
    page.fill("#b", f"<p>after</p>{hostile}")
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")
    page.wait_for_timeout(200)
    # inner_html, not inner_text: a <script> element contributes no rendered
    # text, so inner_text cannot tell "passed through verbatim" from "dropped".
    frame_html = page.frame_locator("#frame").locator("body").inner_html()
    assert "window.parent.__pwned" in frame_html, (
        "the script must reach the frame intact, or the sandbox is untested"
    )
    assert "diffins" not in frame_html, "the script must be unchanged, not wrapped in <ins>"
    assert page.evaluate("() => window.__pwned") is None


def test_collapsed_context_expands_in_place(page):
    """Clicking a skip bar reveals that run, and only that run."""
    page.goto(PAGE.as_uri())
    page.fill("#a", "\n".join(["same"] * 40 + ["before"] + ["same"] * 40))
    page.fill("#b", "\n".join(["same"] * 40 + ["after"] + ["same"] * 40))
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")
    page.click("[data-view='unified']")

    bars = page.locator("#view-unified tr.skip")
    assert bars.count() == 2
    rows_before = page.locator("#view-unified tr").count()

    bars.first.click()
    assert page.locator("#view-unified tr.skip").count() == 1
    assert page.locator("#view-unified tr").count() > rows_before


def test_spine_tick_scrolls_to_its_change(page):
    page.goto(PAGE.as_uri())
    page.fill("#a", "\n".join(["same"] * 80 + ["before"]))
    page.fill("#b", "\n".join(["same"] * 80 + ["after"]))
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")
    page.click("[data-view='split']")

    assert page.locator("#spine .tick").count() > 0
    page.locator("#spine .tick").first.click()
    assert counter(page).startswith("1 /")
    assert page.locator("#view-split tr.is-current").count() > 0


def test_launcher_preloads_both_sides(page, tmp_path):
    """`htmldiff old new` must land on the diff, with `</script>` surviving.

    The launcher injects both files into the page head as JSON. The HTML parser
    ends a script block at `</script>` wherever it appears, so an unescaped
    input containing one truncates the injection and breaks the page.
    """
    old = tmp_path / "old.html"
    new = tmp_path / "new.html"
    old.write_text("<p>Hello <b>world</b></p>\n")
    new.write_text("<p>Hello <b>there</b></p>\n<script>alert(1)</script>\n")

    launcher = PAGE.parent / "htmldiff"
    built = subprocess.run(
        [str(launcher), str(old), str(new), "--no-open"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(Path(built).as_uri())
    page.wait_for_selector("#results:not([hidden])")
    assert not errors
    assert "there" in page.frame_locator("#frame").locator("ins").first.inner_text()


# ------------------------------------------------------- fetching over HTTP


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """A local server, so the suite never depends on someone else's uptime."""
    root = tmp_path_factory.mktemp("site")
    (root / "a.html").write_text(
        '<h1>Pricing</h1><p>Starts at <b>£12</b>.</p><img src="hero.png" alt="hero">\n'
    )
    (root / "b.html").write_text(
        '<h1>Pricing</h1><p>Starts at <b>£14</b> a month.</p><img src="hero.png" alt="hero">\n'
    )
    (root / "latin1.html").write_text(
        '<meta charset="iso-8859-1"><p>caf\xe9</p>', encoding="iso-8859-1"
    )

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):  # one request line per fetch buries the results
            pass

    handler = functools.partial(Quiet, directory=str(root))
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}", root=root)
    server.shutdown()
    server.server_close()


def launch(*args: str) -> subprocess.CompletedProcess:
    launcher = PAGE.parent / "htmldiff"
    return subprocess.run(
        [str(launcher), *args, "--no-open"], capture_output=True, text=True, check=True
    )


def test_fetches_two_urls_and_diffs_them(page, site):
    done = launch(f"{site.url}/a.html", f"{site.url}/b.html")
    page.goto(Path(done.stdout.strip()).as_uri())
    page.wait_for_selector("#results:not([hidden])")

    frame = page.frame_locator("#frame")
    assert "14" in frame.locator("ins").first.inner_text()
    assert "12" in frame.locator("del").first.inner_text()

    # provenance, so two URLs off one site aren't indistinguishable on screen
    origins = page.inner_text("#origins")
    assert "a.html" in origins and "b.html" in origins

    # and the fetched bodies were kept where the operator can look at them
    kept = [
        line.split("→")[1].split("(")[0].strip() for line in done.stderr.splitlines() if "→" in line
    ]
    assert len(kept) == 2
    assert all(Path(p).is_file() for p in kept)


def test_a_url_and_a_file_can_be_mixed(page, site, tmp_path):
    local = tmp_path / "local.html"
    local.write_text("<h1>Pricing</h1><p>Starts at <b>£12</b>.</p>\n")
    done = launch(str(local), f"{site.url}/b.html")
    page.goto(Path(done.stdout.strip()).as_uri())
    page.wait_for_selector("#results:not([hidden])")
    assert page.inner_text("#origins").startswith("pasted →")


def test_fetched_page_gets_a_base_so_relative_assets_can_resolve(page, site):
    """Without <base>, "Load remote assets" does nothing on the fetched pages
    it exists for — a relative src has nothing to resolve against in srcdoc."""
    done = launch(f"{site.url}/a.html", f"{site.url}/b.html")
    page.goto(Path(done.stdout.strip()).as_uri())
    page.wait_for_selector("#results:not([hidden])")

    resolved = page.frame_locator("#frame").locator("img").first.evaluate("img => img.src")
    assert resolved == f"{site.url}/hero.png"


def test_declared_charset_is_honoured(page, site):
    done = launch(f"{site.url}/latin1.html", f"{site.url}/latin1.html")
    page.goto(Path(done.stdout.strip()).as_uri())
    page.wait_for_selector("#results:not([hidden])")
    assert "café" in page.input_value("#a")


def test_a_missing_url_fails_with_the_status(site):
    launcher = PAGE.parent / "htmldiff"
    done = subprocess.run(
        [str(launcher), f"{site.url}/nope.html", f"{site.url}/b.html", "--no-open"],
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "HTTP 404" in done.stderr


def test_non_http_schemes_are_refused():
    launcher = PAGE.parent / "htmldiff"
    done = subprocess.run(
        [str(launcher), "ftp://example.com/a", "ftp://example.com/b", "--no-open"],
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "only http and https" in done.stderr


def test_oversize_response_is_refused_not_truncated(site):
    """A silently truncated body diffs as if the page ended there."""
    launcher = PAGE.parent / "htmldiff"
    done = subprocess.run(
        [
            str(launcher),
            f"{site.url}/a.html",
            f"{site.url}/b.html",
            "--max-bytes",
            "10",
            "--no-open",
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "exceeds 10 bytes" in done.stderr


# ------------------------------------------------------- size and pathology

# Every other input in this file is a handful of lines, and that gap hid four
# separate ways to kill the tab: two unbounded recursions, a Myers budget that
# measured area while the real cost was driven by span, and no error surface
# when any of them blew.
#
# The sizes below are not decorative. Each was measured against the unfixed
# page to find where it actually breaks — 4,000 elements survived the
# insertTag recursion and 25,000×10 survived the Myers trace, so tests at
# those sizes passed on broken code and proved nothing. The first case is a
# plain scale check and passes either way; the three after it fail without
# their fix.


def test_a_ten_thousand_line_document_diffs(page):
    """Scale smoke test — no specific bug, just that this size works at all."""
    page.goto(PAGE.as_uri())
    old = "\n".join(f"<p>paragraph {i} of the handbook</p>" for i in range(10_000))
    new = old.replace("paragraph 5000 of", "paragraph 5000 (revised) of")
    result = page.evaluate(
        "([a, b]) => { const r = window.htmldiff.buildRows(a, b, {}); return r.stats; }",
        [old, new],
    )
    assert result["mod"] == 1


def test_a_repetitive_table_drives_the_match_phase(page):
    """The match phase at size — the half every other case here misses.

    `htmlDiff(a, '')` sets `matchGranularity` to `min(4, min(n, 0))`, so
    `find`'s loop body never runs and the entire match phase is skipped: the
    one htmlDiff scale test in this file exercised none of it, and the rest of
    the block drives `buildRows`, which is the source view. The rendered view
    is the default one, and its expensive half had no coverage above a handful
    of words. A quadratic scan lived there unseen.

    Repetitive markup is the shape that hurt, because bucketing every
    block-of-N by key and walking that bucket per old word costs
    O(words x repeats). Measured on this input: 2,000 rows took 8.0s before
    the rewrite against 0.37s after, and 4,000 rows — this test — took 45.3s
    before against 0.42s after. The budget below has two orders of magnitude
    either side of it, so it is a regression alarm and not a stopwatch.

    The quality assertion carries as much weight as the clock. The obvious way
    to make this fast is to drop keys that repeat too often, which is what
    `repeatingWordsAccuracy` was for, and it is wrong: measured, it marks
    44,000 words changed here instead of 2, diffing identical rows as one
    wholesale replace.
    """
    page.goto(PAGE.as_uri())
    measured = page.evaluate(
        """(rows) => {
            const build = (price) => {
                const out = ["<table class='products'>"];
                for (let i = 0; i < rows; i++) {
                    out.push(
                        `<tr class='row'><td class='name'>Product ${i}</td>` +
                        `<td class='sku'>SKU-${i}</td>` +
                        `<td class='desc'>A useful thing for doing things, number ${i}.</td>` +
                        `<td class='price'>$${i === (rows >> 1) ? price : 10}.00</td>` +
                        `<td class='stock'>In stock</td></tr>`,
                    );
                }
                return out.join("\\n") + "</table>";
            };
            const started = performance.now();
            const out = window.htmldiff.htmlDiff(build("12"), build("99"), {});
            const ms = performance.now() - started;

            const host = document.implementation.createHTMLDocument("").createElement("div");
            host.innerHTML = out;
            let changed = 0;
            for (const marked of host.querySelectorAll("ins, del")) {
                changed += (marked.textContent || "").split(/\\s+/).filter(Boolean).length;
            }
            return { ms, changed };
        }""",
        4_000,
    )
    assert measured["changed"] <= 12, (
        f"{measured['changed']} words marked changed, for one changed price — "
        "a match phase that drops repeated keys does exactly this"
    )
    assert measured["ms"] < 10_000, f"{measured['ms']:.0f}ms: the match phase is quadratic again"


def test_a_hash_collision_reopens_the_match_phase_blowup(page):
    """A crafted hash collision, not natural repetition, driving findRun.

    HASH_BASE is a fixed public constant, so an attacker who reads the source
    can solve `d0*HASH_BASE**p0 + d1*HASH_BASE**p1 = 0 (mod 2**32)` for small
    d0/d1 and get two length-1688 windows that hash identically while
    differing in content at just two positions (found in review). Repeating
    each window N times reopens exactly the bug this file exists to fix:
    every old-side repeat of the base window lands in the same hash bucket as
    every new-side repeat of the colliding-but-unequal candidate window, so
    verifying them all costs O(N^2) even though the hash lookup itself is O(1).

    g=1687, d0=2843, d1=213 below solve that equation — found by sweeping the
    gap g and delta d0, since d1 is then fixed by both — and are checked
    inline rather than trusted, since a future HASH_BASE change would
    silently invalidate them without touching this test's other assertions.

    Measured directly against findRun at this size (800 repeats, 1,350,400
    words a side): 2.2s with no probe budget (the pre-fix behavior) against
    0.5s with the budget findMatch now computes. The bound below is loose — a
    regression alarm, not a stopwatch — same rationale as the repetitive-table
    case above.
    """
    page.goto(PAGE.as_uri())
    measured = page.evaluate(
        """() => {
            const g = 1687, d0 = 2843, d1 = 213, mid = 3000;
            const L = g + 1;
            const base = new Array(L).fill(mid);
            const cand = base.slice();
            cand[0] = mid + d0;
            cand[g] = mid + d1;

            const hashesMatch =
                window.htmldiff.windowHashes(Int32Array.from(base), L)[0] ===
                window.htmldiff.windowHashes(Int32Array.from(cand), L)[0];

            const repeat = (arr, n) => {
                const out = new Int32Array(arr.length * n);
                for (let i = 0; i < n; i++) out.set(arr, i * arr.length);
                return out;
            };
            const n = 800;
            const a = repeat(base, n);
            const b = repeat(cand, n);
            const probeBudget = Math.max(4096, 64 * (a.length + b.length));

            const started = performance.now();
            const run = window.htmldiff.findRun(a, b, L, probeBudget);
            const ms = performance.now() - started;
            return { hashesMatch, contentDiffers: base.join() !== cand.join(), run, ms };
        }"""
    )
    assert measured["hashesMatch"], "magic numbers no longer collide — HASH_BASE must have changed"
    assert measured["contentDiffers"]
    # No true run of length 1688 exists between these two windows, so the
    # only correct answer is null — a budget that returned a false positive
    # would be a worse bug than the one it fixes.
    assert measured["run"] is None
    assert measured["ms"] < 10_000, (
        f"{measured['ms']:.0f}ms: findRun's probe budget isn't bounding collision work"
    )


def test_a_big_page_against_a_short_error_page(page):
    """The skewed shape: a big page against a short error page, sharing nothing.

    The shape has to be chosen with care to exercise this. 80,000 × 3 is
    240,000 cells, just inside the old area-only budget, but the span is
    80,003 — and Myers keeps a V snapshot per edit step, so it tried to
    allocate tens of GB. Widening to 60,000 × 6 does *not* reproduce it: that
    is 360,000 cells, which the area budget already rejected.

    Measured 3/3 "Array buffer allocation failed" after ~5s unfixed, 3/3 pass
    in ~0.1s fixed, the span cap sending it straight to a wholesale replace.
    """
    page.goto(PAGE.as_uri())
    old = "\n".join(f"<p>line {i} of the production page</p>" for i in range(80_000))
    new = "\n".join(f"<h1>50{i} error</h1>" for i in range(3))
    stats = page.evaluate("([a, b]) => window.htmldiff.buildRows(a, b, {}).stats", [old, new])
    assert stats["del"] + stats["mod"] > 0


def test_many_change_sites_keep_their_order(page):
    """The match collector, once per change site.

    It was a right-leaning recursion whose depth equalled the number of change
    sites exactly, and is now an explicit stack. Order is the thing to hold on
    to: matches have to stay in document order or the operations built from
    them interleave, so this asserts every change is found *and* that they read
    in sequence, which a mis-popped stack would break.

    This passes against the recursive version too — 400 frames never reached a
    RangeError, which is why the depth stayed latent. It is here to hold the
    rewrite to the behaviour it replaced, not to prove a bug is gone.
    """
    page.goto(PAGE.as_uri())
    stats = page.evaluate(
        """(count) => {
            const old_ = [], new_ = [];
            for (let i = 0; i < count; i++) {
                old_.push(`<p>paragraph ${i} says before and stays put</p>`);
                new_.push(`<p>paragraph ${i} says after and stays put</p>`);
            }
            const out = window.htmldiff.htmlDiff(old_.join("\\n"), new_.join("\\n"), {});
            const host = document.implementation.createHTMLDocument("").createElement("div");
            host.innerHTML = out;
            const numbers = [...host.querySelectorAll("p")].map(
                (p) => Number((p.textContent.match(/paragraph (\\d+)/) || [])[1]),
            );
            const ordered = numbers.every((n, at) => at === 0 || n >= numbers[at - 1]);
            return { marks: host.querySelectorAll("ins, del").length, ordered };
        }""",
        400,
    )
    assert stats["marks"] >= 400, f"only {stats['marks']} marks for 400 changes"
    assert stats["ordered"], "matches came back out of document order"


def test_deleting_a_whole_document_does_not_overflow(page):
    """One change block holding thousands of tags — insertTag's recursion.

    Measured: 4,000 elements survived it, 8,000 overflowed the stack.

    Note what this does *not* reach: `htmlDiff(a, '')` sets `matchGranularity`
    to 0, so `find`'s loop body never runs and the match phase is skipped
    entirely. `test_a_repetitive_table_drives_the_match_phase` covers that.
    """
    page.goto(PAGE.as_uri())
    old = "".join(f"<p>element {i}</p>" for i in range(8_000))
    length = page.evaluate("(a) => window.htmldiff.htmlDiff(a, '').length", old)
    assert length > 0


def test_a_reformatted_file_does_not_overflow(page):
    """Every line differs, so alignRun walks one pair at a time down the run."""
    page.goto(PAGE.as_uri())
    old = "\n".join(f"    <div id='n{i}'>old {i}</div>" for i in range(20_000))
    new = "\n".join(f"\t<div id='n{i}'>new {i}</div>" for i in range(20_000))
    stats = page.evaluate("([a, b]) => window.htmldiff.buildRows(a, b, {}).stats", [old, new])
    assert stats["mod"] == 20_000


def test_a_failed_diff_says_so_instead_of_doing_nothing(page):
    """Clicking the button and getting silence reads as 'the tool is broken'.

    Every crash the size cases above cover is now fixed, so the throw has to
    be induced — but it travels the real path through `run()`, which is the
    part being tested.
    """
    page.goto(PAGE.as_uri())
    page.fill("#a", "<p>one</p>")
    page.fill("#b", "<p>two</p>")
    page.evaluate("() => { performance.now = () => { throw new Error('induced'); }; }")

    page.click("#run")
    page.wait_for_selector("#failed:not([hidden])")
    assert "induced" in page.inner_text("#failed")
    assert page.locator("#results").is_hidden()

    # and it clears on the next good run
    page.evaluate("() => { performance.now = () => 0; }")
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")
    assert page.locator("#failed").is_hidden()


def test_a_failure_from_the_results_screen_is_still_visible(page):
    """Some re-runs start from the results bar — "Load remote assets", and
    Refresh in served mode. The error text lives in the input stage, so
    reporting without returning there leaves the previous diff on screen
    looking current, which is the silent failure this all exists to remove.
    """
    page.goto(PAGE.as_uri())
    page.fill("#a", "<p>alpha one</p>")
    page.fill("#b", "<p>alpha two</p>")
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")

    page.evaluate("() => { performance.now = () => { throw new Error('induced'); }; }")
    page.check("#opt-ext")  # a results-bar control that re-runs the diff

    page.wait_for_selector("#failed:not([hidden])")
    assert page.locator("#stage").is_visible(), "the message is in the stage"
    assert page.locator("#results").is_hidden(), "a stale diff must not stay up"


def save_report(page, old: str, new: str, tmp_path: Path) -> Path:
    """Diff `old` against `new` and return the downloaded report on disk."""
    page.goto(PAGE.as_uri())
    page.fill("#a", old)
    page.fill("#b", new)
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")
    with page.expect_download() as caught:
        page.click("#download")
    saved = tmp_path / "htmldiff-report.html"
    caught.value.save_as(saved)
    return saved


def test_saved_report_locks_down_scripts(page, browser, tmp_path):
    """A <script> in *both* sides survives into the report as unchanged text,
    and a report is opened outside any sandbox.

    Opening it is the point. Grepping the file for `Content-Security-Policy`
    and `default-src 'none'` — which is what this did — passes against a policy
    that does not work, and is why the meta-refresh gap went unseen.
    """
    hostile = "<p>x</p><script>window.__ran = 1;</script>"
    saved = save_report(page, hostile + "<p>before</p>", hostile + "<p>after</p>", tmp_path)
    assert "window.__ran" in saved.read_text(), "the script should still be *shown*"

    reader = browser.new_page()
    try:
        reader.goto(saved.as_uri())
        reader.wait_for_timeout(200)
        assert reader.evaluate("() => window.__ran") is None
    finally:
        reader.close()


def test_saved_report_does_not_navigate(page, browser, tmp_path, site):
    """`default-src 'none'` does not cover navigation, and a saved report has
    no sandbox attribute to fall back on, so a <meta http-equiv=refresh> in a
    diffed page carried the reader to the attacker's URL on open.

    The refresh is on the *new side only*, deliberately: tags aren't wrapped in
    <ins>/<del>, so a tag in one document alone still reaches the report. The
    target is the local site fixture and is genuinely reachable — pointing at a
    dead port would let a broken fix pass on the connection error alone.
    """
    refresh = f'<meta http-equiv="refresh" content="0;url={site.url}/a.html">'
    saved = save_report(page, "<p>before</p>", refresh + "<p>after</p>", tmp_path)

    reader = browser.new_page()
    try:
        reader.goto(saved.as_uri())
        reader.wait_for_timeout(500)  # a refresh of 0 has long since fired
        assert reader.url == saved.as_uri(), f"report navigated to {reader.url}"
        assert "after" in reader.locator(".hd-body").inner_text(), "the diff should still render"
    finally:
        reader.close()


# -------------------------------------------------- served mode (npx / node)

CLI = PAGE.parent / "cli.mjs"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture
def served(site):
    """`node cli.mjs` serving the page with its fetch proxy, one per test."""
    proc = subprocess.Popen(
        [shutil.which("node"), str(CLI), "--no-open"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base = proc.stdout.readline().strip()
        assert base.startswith("http://127.0.0.1:"), f"no address on stdout: {base!r}"
        yield base
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@needs_node
def test_served_page_exposes_url_fields(page, served):
    page.goto(served)
    assert page.locator("#url-a").is_visible()
    assert page.locator("#url-b").is_visible()


@needs_node
def test_standalone_page_hides_url_fields(page):
    """No proxy, no promise of one — the fields would just fail."""
    page.goto(PAGE.as_uri())
    assert page.locator("#url-a").is_hidden()


@needs_node
def test_fetches_both_urls_from_the_page(page, served, site):
    page.goto(served)
    page.fill("#url-a", f"{site.url}/a.html")
    page.fill("#url-b", f"{site.url}/b.html")
    page.press("#url-b", "Enter")
    page.wait_for_selector("#results:not([hidden])")

    frame = page.frame_locator("#frame")
    assert "14" in frame.locator("ins").first.inner_text()
    assert "a.html" in page.inner_text("#origins")


@needs_node
def test_enter_with_only_one_url_does_not_diff_against_empty(page, served, site):
    """The common case is one URL typed and the other still being typed."""
    page.goto(served)
    page.fill("#url-a", f"{site.url}/a.html")
    page.press("#url-a", "Enter")
    page.wait_for_function("() => document.querySelector('#a').value.length > 0")
    assert page.locator("#results").is_hidden()


@needs_node
def test_refresh_picks_up_a_changed_page(page, served, site):
    """The deploy-watching loop: re-pull both sides without retyping."""
    # Its own file, so mutating it can't disturb the shared fixture pages.
    moving = site.root / "moving.html"
    moving.write_text("<h1>Pricing</h1><p>Starts at <b>£14</b>.</p>\n")

    page.goto(served)
    page.fill("#url-a", f"{site.url}/a.html")
    page.fill("#url-b", f"{site.url}/moving.html")
    page.press("#url-b", "Enter")
    page.wait_for_selector("#results:not([hidden])")
    assert not page.locator("#refetch").is_hidden()

    moving.write_text("<h1>Pricing</h1><p>Starts at <b>£19</b>.</p>\n")
    page.click("#refetch")
    page.wait_for_function(
        "() => document.querySelector('#frame').contentDocument"
        "?.querySelector('ins')?.textContent.includes('19')"
    )


@needs_node
def test_proxy_requires_the_token(served):
    """Any page you have open can reach 127.0.0.1; only ours knows the token."""
    request = urllib.request.Request(f"{served}fetch?url=https://example.com")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 403


@needs_node
def test_proxy_refuses_a_cross_site_origin(served):
    request = urllib.request.Request(
        f"{served}fetch?url=https://example.com", headers={"Origin": "https://evil.example"}
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 403


@needs_node
def test_proxy_refuses_an_unexpected_host(served):
    """DNS rebinding: `GET /` hands out the token, and fetch() omits Origin on
    same-origin GETs, so Origin alone does not stop a rebound page from reading
    the token and then using the proxy. Rebinding cannot forge Host."""
    request = urllib.request.Request(served, headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 403


@needs_node
def test_a_malformed_request_target_does_not_kill_the_server(served):
    """Node's HTTP parser accepts absolute-form targets that WHATWG `URL`
    rejects, and the handler is `async` — so `new URL(request.url, …)` throwing
    became an unhandled rejection and the process exited, taking the session
    with it. A raw `GET http://[ HTTP/1.1` was enough.

    urllib cannot send this: it builds the request line itself.
    """
    port = urllib.parse.urlsplit(served).port
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"GET http://[ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        status = sock.recv(64).decode("latin1", "replace").splitlines()[0]
    assert "400" in status, status

    with urllib.request.urlopen(served, timeout=10) as answered:
        assert answered.status == 200, "the server should have survived"


@needs_node
def test_a_busy_port_is_one_line_not_a_stack_trace(served):
    """`startServer`'s rejection propagated through the top-level `await
    main()`, so `-p <busy port>` exited 1 with a 12-line dump. Everywhere else
    a bad invocation gets one line."""
    port = str(urllib.parse.urlsplit(served).port)
    done = subprocess.run(
        [shutil.which("node"), str(CLI), "--no-open", "-p", port],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 1, done
    assert port in done.stderr and "in use" in done.stderr, done.stderr
    assert "at Server" not in done.stderr and done.stderr.count("\n") == 1, done.stderr


@needs_node
def test_localhost_host_header_still_works(served):
    """Loopback has more than one spelling; only foreign names are refused."""
    port = served.rstrip("/").rsplit(":", 1)[1]
    request = urllib.request.Request(served, headers={"Host": f"localhost:{port}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


@needs_node
def test_seeded_text_survives_dollar_substitution(page, tmp_path):
    """`$$`, "$`" and `$'` are substitution patterns in a String.replace
    *replacement*, even when the search value is a plain string — and the
    replacement is page content the tool just read.

    This is the seeding path (`buildPage`), not the `/fetch` endpoint, which
    carries its payload as JSON and is unaffected. `$$` halved to `$` silently,
    which is a wrong diff rather than an absent one; "$`" spliced this file's
    own head into the JSON and broke the page outright.
    """
    quirky = "<p>price: $$5, cost $` here, $' there, $& too</p>\n"
    old = tmp_path / "old.html"
    new = tmp_path / "new.html"
    old.write_text(quirky)
    new.write_text(quirky.replace("$$5", "$$6"))

    done = subprocess.run(
        [shutil.which("node"), str(CLI), "--static", str(old), str(new), "--no-open"],
        capture_output=True,
        text=True,
        check=True,
    )
    page.goto(done.stdout.strip())
    page.wait_for_selector("#results:not([hidden])")

    loaded = page.input_value("#a")
    assert "price: $$5" in loaded
    assert "cost $` here" in loaded
    assert "$' there" in loaded
    assert "$& too" in loaded
    assert "<!doctype" not in loaded.lower(), "page HTML spliced into the payload"


@needs_node
def seed_standalone(seeder: str, old: Path, new: Path) -> str:
    """Build a standalone page with one of the two seeders; return its URL.

    The launcher prints a path and `cli.mjs --static` prints a file:// URL.
    """
    argv = {
        "launcher": [str(PAGE.parent / "htmldiff"), str(old), str(new), "--no-open"],
        "cli": [shutil.which("node"), str(CLI), "--static", str(old), str(new), "--no-open"],
    }[seeder]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
    return out if out.startswith("file://") else Path(out).as_uri()


@pytest.mark.parametrize("seeder", ["launcher", pytest.param("cli", marks=needs_node)])
def test_a_comment_opener_does_not_swallow_the_page(page, tmp_path, seeder):
    """`<!--` in an input must not take the rest of the document with it.

    Escaping only `</` closes the `</script>` hole and opens its sibling. The
    tokenizer enters script-data-double-escaped state at `<!--` and leaves it
    at the next `</script>` — which the escape has just hidden — so everything
    after the injected block, the page's own scripts included, is swallowed as
    script data. The result is a blank tab with `window.htmldiff` undefined and
    no message anywhere, after the seeder has exited 0 and printed a path.

    Both seeders inject the same page and are tested together: a fix applied to
    one and not the other leaves the bug reachable from the other entry point.
    """
    opener = '<p>x</p>\n<script>var s = "<!--";</script>\n'
    old, new = tmp_path / "old.html", tmp_path / "new.html"
    old.write_text(opener + "<p>before</p>\n")
    new.write_text(opener + "<p>after</p>\n")

    page.goto(seed_standalone(seeder, old, new))
    page.wait_for_function("window.htmldiff !== undefined")
    page.wait_for_selector("#results:not([hidden])")
    assert "after" in page.frame_locator("#frame").locator("ins").first.inner_text()


def test_proxy_refuses_non_http_schemes(page, served):
    """A file:// target would turn the proxy into a local file reader."""
    page.goto(served)
    status = page.evaluate(
        """async () => {
             const q = new URLSearchParams({
               url: 'file:///etc/passwd', token: window.__htmldiffServer.token });
             const r = await fetch('/fetch?' + q);
             return r.status;
           }"""
    )
    assert status == 400


@needs_node
def test_static_mode_writes_a_working_file(page, tmp_path):
    old = tmp_path / "old.html"
    new = tmp_path / "new.html"
    old.write_text("<p>before</p>")
    new.write_text("<p>after</p>")
    done = subprocess.run(
        [shutil.which("node"), str(CLI), "--static", str(old), str(new), "--no-open"],
        capture_output=True,
        text=True,
        check=True,
    )
    page.goto(done.stdout.strip())
    page.wait_for_selector("#results:not([hidden])")
    assert page.locator("#url-a").is_hidden()  # no server, no proxy
    assert "after" in page.frame_locator("#frame").locator("ins").first.inner_text()


@needs_node
def test_package_ships_the_page_and_the_cli():
    """A `files` array that misses htmldiff.html publishes a broken package."""
    done = subprocess.run(
        [shutil.which("npm"), "pack", "--dry-run", "--json"],
        cwd=PAGE.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    shipped = {entry["path"] for entry in json.loads(done.stdout)[0]["files"]}
    assert {"cli.mjs", "htmldiff.html", "package.json"} <= shipped


def _rendered_text(page, html: str) -> str:
    """The diff as a reader sees it. The corruption these tests guard against is
    visible in the rendered text and not in the markup, so asserting on the diff
    string alone would miss it."""
    return page.evaluate(
        "(html) => { const d = document.createElement('div');"
        " d.innerHTML = html; return d.textContent; }",
        html,
    )


@pytest.mark.parametrize(
    "old, new",
    [
        ("<p>\U0001f600</p>", "<p>\U0001f601</p>"),  # emoji, whole code point changed
        ("<p>a \U0001f44d\U0001f3fd b</p>", "<p>a \U0001f44d\U0001f3fb b</p>"),  # skin tone
        # The astral letter must be the thing that changed. Leave it in an unchanged
        # region and it passes through verbatim, so the test survives the bug.
        ("<p>\U0001d573ello</p>", "<p>\U0001d575ello</p>"),  # astral letter
    ],
)
def test_astral_characters_survive_the_tokenizer(page, old, new):
    # Tokenizing by UTF-16 unit splits an astral character in half. Each lone
    # surrogate fails isWordChar, so the halves become separate words and a change
    # boundary can land inside one character — the redline then renders U+FFFD where
    # the character was. Corrupt output, not merely mis-segmented.
    page.goto(PAGE.as_uri())
    text = _rendered_text(page, diff(page, old, new))
    assert "�" not in text, f"replacement character in rendered diff: {text!r}"


def test_a_bare_less_than_does_not_hide_later_changes(page):
    # `<` opens a tag only when a name character follows. Treating every `<` as a tag
    # start swallows the rest of the text node into one token that then normalizes to
    # `</p>` on both sides, so the matcher calls the region equal and emits the new
    # text verbatim — the price change is reported as no change at all.
    page.goto(PAGE.as_uri())
    out = diff(
        page,
        "<p>5 < 10 and the price is 19 dollars</p>",
        "<p>5 < 10 and the price is 24 dollars</p>",
    )
    assert "diffmod" in out or "diffins" in out, f"change not marked: {out!r}"
    assert "19" in out and "24" in out, f"both sides of the change should appear: {out!r}"


def test_identical_inputs_report_no_changes(page):
    page.goto(PAGE.as_uri())
    page.fill("#a", "<p>same</p>")
    page.fill("#b", "<p>same</p>")
    page.click("#run")
    page.wait_for_selector("#results:not([hidden])")
    assert counter(page) == "no changes"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
