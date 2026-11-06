"""Browser tests for the preview-diff review overlay.

Every defect these cover was found by someone reading the rendered page, not by
the server tests: a comment anchored to the wrong side, a thread landing on the
wrong file card, a cancelled selection stripping a committed highlight, review
markdown rendering as literal text. None of it is reachable from review_server
alone, so it needs a real browser.

Each test starts its own `preview-diff --review` against a fixture diff and
tears it down, so the suite is self-contained.

Needs the chromium binary once: `uvx playwright install chromium`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_api.sync_playwright

SCRIPT = Path(__file__).resolve().parent / "preview-diff"

# A plain unified diff: one file, context plus additions.
BASIC_DIFF = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1,4 +1,8 @@
 import sys

+def added_one():
+    return 1
+
+
 def existing():
     return 0
"""

# Old and new line numbers deliberately differ, so a comment anchored from the
# wrong gutter is visible as a wrong number rather than a coincidence.
SIDE_DIFF = """\
diff --git a/offset.txt b/offset.txt
--- a/offset.txt
+++ b/offset.txt
@@ -10,4 +20,5 @@
 context line one
 context line two
+an added line
 context line three
 context line four
"""

# Every other fixture fits on one screen, where nothing scrolls and one comment
# is as reachable as any other. n/p only mean something on a page taller than
# the viewport, so this one is built to be.
TALL_DIFF = (
    "diff --git a/tall.py b/tall.py\n"
    "--- a/tall.py\n"
    "+++ b/tall.py\n"
    "@@ -1,120 +1,121 @@\n" + "".join(f" line {i}\n" for i in range(1, 121)) + "+an added line\n"
)

# The same path twice, as a multi-commit `git show a b` renders it.
DUP_DIFF = """\
diff --git a/dup.py b/dup.py
--- a/dup.py
+++ b/dup.py
@@ -1,3 +1,4 @@
 first card
+first card addition
 first card tail
diff --git a/dup.py b/dup.py
--- a/dup.py
+++ b/dup.py
@@ -50,3 +50,4 @@
 second card
+second card addition
 second card tail
"""


class Server:
    """A `preview-diff --review` process serving one diff."""

    def __init__(self, diff: str, workdir: Path, body: str | None = None):
        self.dir = workdir
        self.err_path = workdir / "stderr.log"
        self._errf = self.err_path.open("w")
        diff_file = workdir / "input.diff"
        diff_file.write_text(diff)
        env = {**os.environ, "XDG_DATA_HOME": str(workdir / "xdg")}
        body_args = []
        if body is not None:
            body_file = workdir / "body.md"
            body_file.write_text(body)
            body_args = ["--body", str(body_file)]
        self.proc = subprocess.Popen(
            [
                str(SCRIPT),
                "--review",
                "--no-open",
                "--from-diff",
                str(diff_file),
                "--title",
                "ui test",
                *body_args,
            ],
            stdout=subprocess.PIPE,
            # Kept, not discarded: when the script dies before printing a URL,
            # its stderr is the only thing that says why. Throwing it away
            # turned a one-line `mktemp` portability break into 43 identical
            # "no URL" failures with no diagnostic anywhere. A file rather than
            # a pipe because the served process logs every request here — a
            # pipe would fill and block the server mid-test.
            stderr=self._errf,
            text=True,
            env=env,
            # Own session so the bash wrapper and its python child both die.
            start_new_session=True,
        )
        # preview-diff prints the URL as its first stdout line.
        self.url = (self.proc.stdout.readline() or "").strip()
        if not self.url.startswith("http"):
            self.stop()
            raise RuntimeError(
                f"no URL from preview-diff (got {self.url!r}); stderr: {self.stderr()}"
            )
        self._await_ready()

    def stderr(self) -> str:
        """Whatever the script wrote to stderr, for a failure message."""
        try:
            return self.err_path.read_text().strip()
        except OSError:
            return ""

    def _await_ready(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"{self.url} never became ready; stderr: {self.stderr()}")

    def post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url.rstrip("/") + "/review",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def stop(self) -> None:
        if self.proc.poll() is None:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        if self.proc.stdout:
            self.proc.stdout.close()
        if not self._errf.closed:
            self._errf.close()


@pytest.fixture(scope="session")
def browser():
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("browser tests are only wired up for macOS and Linux")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:  # binary not installed
            pytest.skip(f"chromium unavailable ({exc}); run: uvx playwright install chromium")
        yield b
        b.close()


@pytest.fixture
def serve(tmp_path):
    """Start a review server for a diff; stopped when the test ends."""
    started: list[Server] = []

    def _make(diff: str, body: str | None = None) -> Server:
        d = tmp_path / f"s{len(started)}"
        d.mkdir(parents=True, exist_ok=True)
        s = Server(diff, d, body)
        started.append(s)
        return s

    yield _make
    for s in started:
        s.stop()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1280, "height": 1000})
    yield pg
    pg.close()


@pytest.fixture
def kb_page(browser):
    """A page for keyboard tests: reduced motion makes scrolling land at once.

    The page turns off smooth scrolling under prefers-reduced-motion, so asking
    for it here is the difference between reading a final position and racing an
    animation to it.
    """
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, reduced_motion="reduce")
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def phone_page(browser):
    """A narrow touch page, at the width the layout's breakpoint targets."""
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )
    pg = ctx.new_page()
    yield pg
    ctx.close()


def open_review(page, server) -> None:
    page.goto(server.url)
    page.wait_for_load_state("networkidle")


def write_review(page, text):
    """Fill the review box, which starts folded."""
    if not page.locator("#rv-general").is_visible():
        page.locator("#rv-fold-toggle").click()
    page.locator("#rv-general").fill(text)


def add_comment(page, row, text, gutter="td.ln-n"):
    row.locator(gutter).click()
    page.wait_for_selector(".cm-editor textarea")
    page.locator(".cm-editor textarea").fill(text)
    page.locator(".cm-editor .btn-go").click()
    page.wait_for_timeout(150)


# --------------------------------------------------------------------------
# leaving comments
# --------------------------------------------------------------------------


def test_gutter_click_opens_an_editor_naming_its_anchor(page, serve):
    open_review(page, serve(BASIC_DIFF))
    row = page.locator("tr[data-n]").first
    line = row.get_attribute("data-n")
    row.locator("td.ln-n").click()
    page.wait_for_selector(".cm-editor textarea")
    assert f":{line}" in page.locator(".cm-editor textarea").get_attribute("placeholder")


def test_saving_renders_a_thread_and_counts_it(page, serve):
    open_review(page, serve(BASIC_DIFF))
    add_comment(page, page.locator("tr[data-n]").first, "a note")
    assert page.locator(".cm-thread").count() == 1
    assert page.locator("#rv-n").inner_text() == "1"
    assert page.locator("#rv-n-label").inner_text() == "line comment"
    assert page.locator(".cm-editor").count() == 0


def test_the_counter_pluralises(page, serve):
    open_review(page, serve(BASIC_DIFF))
    rows = page.locator("tr[data-n]")
    add_comment(page, rows.nth(0), "one")
    add_comment(page, rows.nth(1), "two")
    assert page.locator("#rv-n").inner_text() == "2"
    assert page.locator("#rv-n-label").inner_text() == "line comments"


def test_shift_click_stretches_the_selection_and_keeps_typed_text(page, serve):
    open_review(page, serve(BASIC_DIFF))
    rows = page.locator("tr[data-n]")
    first, last = rows.nth(0), rows.nth(3)
    lo = int(first.get_attribute("data-n"))
    hi = int(last.get_attribute("data-n"))

    first.locator("td.ln-n").click()
    page.wait_for_selector(".cm-editor textarea")
    page.locator(".cm-editor textarea").fill("half typed")
    last.locator("td.ln-n").click(modifiers=["Shift"])
    page.wait_for_timeout(150)

    ta = page.locator(".cm-editor textarea")
    assert f"{lo}–{hi}" in ta.get_attribute("placeholder")
    assert ta.input_value() == "half typed", "text must survive the re-anchor"
    assert page.locator("tr.sel-pending").count() >= 4


def test_deleting_a_thread_removes_it(page, serve):
    open_review(page, serve(BASIC_DIFF))
    add_comment(page, page.locator("tr[data-n]").first, "a note")
    page.locator(".cm-thread .cm-del").first.click()
    page.wait_for_timeout(150)
    assert page.locator(".cm-thread").count() == 0
    assert page.locator("#rv-n").inner_text() == "0"


def test_cancelling_a_selection_keeps_a_committed_highlight(page, serve):
    # `sel` used to be shared between the pending selection and committed
    # comments, so cancelling one stripped the other's highlight.
    open_review(page, serve(BASIC_DIFF))
    row = page.locator("tr[data-n]").first
    add_comment(page, row, "committed")
    lit = page.locator("tr.sel").count()

    row.locator("td.ln-n").click()
    page.wait_for_selector(".cm-editor textarea")
    page.locator(".cm-editor .btn").first.click()  # Cancel
    page.wait_for_timeout(150)

    assert page.locator("tr.sel").count() == lit
    assert page.locator(".cm-thread").count() == 1


# --------------------------------------------------------------------------
# anchoring
# --------------------------------------------------------------------------


def test_side_follows_the_clicked_gutter(page, serve):
    # A context row carries both numbers. Deriving side from the row anchored a
    # click on the old gutter to the new line: :20 for a line displayed as 10.
    server = serve(SIDE_DIFF)
    open_review(page, server)
    row = page.locator("tr[data-o][data-n]").first
    old, new = row.get_attribute("data-o"), row.get_attribute("data-n")
    assert old != new, "fixture must have differing gutters to be meaningful"

    row.locator("td.ln-o").click()
    page.wait_for_selector(".cm-editor textarea")
    placeholder = page.locator(".cm-editor textarea").get_attribute("placeholder")
    assert f":{old}" in placeholder
    assert "(old)" in placeholder


def test_a_blank_gutter_is_inert(page, serve):
    open_review(page, serve(BASIC_DIFF))
    # An added row has no old-side number, so its old gutter must not comment.
    added = page.locator("tr.r-add").first
    added.locator("td.ln-o").click()
    page.wait_for_timeout(150)
    assert page.locator(".cm-editor").count() == 0


def test_a_comment_stays_on_the_card_it_was_left_on(page, serve):
    # One path can appear on several cards; path alone does not identify one.
    server = serve(DUP_DIFF)
    open_review(page, server)
    cards = page.locator('details.file[data-path="dup.py"]')
    assert cards.count() == 2, "fixture must render the same path twice"
    second_id = cards.nth(1).get_attribute("id")

    add_comment(page, cards.nth(1).locator("tr[data-n]").first, "belongs to the second card")
    landed = page.evaluate("() => document.querySelector('.cm-thread').closest('details.file').id")
    assert landed == second_id

    page.locator("#rv-submit").click()
    page.wait_for_selector("#rv-msg.ok", timeout=5000)
    art = next(w for w in page.locator("#rv-msg").inner_text().split() if w.endswith("review.html"))
    data = json.loads(Path(art).with_name("review.json").read_text())
    assert data["comments"][0]["file_id"] == second_id


# --------------------------------------------------------------------------
# the changed-files list
# --------------------------------------------------------------------------


def test_the_file_list_starts_closed_and_its_header_opens_it(page, serve):
    open_review(page, serve(BASIC_DIFF))
    lst = page.locator("details.summary#filelist")
    assert not lst.evaluate("d => d.open")
    assert not page.locator("a.chip").first.is_visible()

    page.locator("summary.sumhead h3").click()
    assert lst.evaluate("d => d.open")
    assert page.locator("a.chip").first.is_visible()


def test_expand_all_does_not_close_the_list_it_lives_in(page, serve):
    # Both buttons sit inside the <summary>, so a stray default click would
    # shut the list on whoever just pressed them.
    open_review(page, serve(BASIC_DIFF))
    page.locator("summary.sumhead h3").click()
    lst = page.locator("details.summary#filelist")

    page.locator("#collapse-all").click()
    assert lst.evaluate("d => d.open")
    assert page.evaluate("[...document.querySelectorAll('details.file')].every(d => !d.open)")

    page.locator("#expand-all").click()
    assert lst.evaluate("d => d.open")
    assert page.evaluate("[...document.querySelectorAll('details.file')].every(d => d.open)")


def test_the_description_starts_open_and_its_header_closes_it(page, serve):
    open_review(page, serve(BASIC_DIFF, "# Notes\n\nSome *body* text.\n"))
    desc = page.locator("details.desc")
    assert desc.count() == 1
    assert desc.evaluate("d => d.open")

    desc.locator("summary.desc-head").click()
    assert not desc.evaluate("d => d.open")


def test_the_raw_toggle_does_not_close_the_description(page, serve):
    # The button sits in the <summary> of the block whose view it switches.
    open_review(page, serve(BASIC_DIFF, "# Notes\n\nSome *body* text.\n"))
    desc = page.locator("details.desc")

    page.locator("#md-toggle").click()
    assert desc.evaluate("d => d.open")
    assert page.locator("#md-raw").is_visible()

    page.locator("#md-toggle").click()
    assert desc.evaluate("d => d.open")
    assert page.locator("#md-rendered").is_visible()


def test_the_sticky_file_count_reopens_the_list_every_time(page, serve):
    # A plain href only fires hashchange when the hash changes, so the second
    # click after a manual collapse has to be handled, not left to the anchor.
    open_review(page, serve(BASIC_DIFF))
    lst = page.locator("details.summary#filelist")
    link = page.locator('.totals a[href="#filelist"]')

    for _ in range(2):
        link.click()
        assert lst.evaluate("d => d.open")
        page.locator("summary.sumhead h3").click()
        assert not lst.evaluate("d => d.open")


# --------------------------------------------------------------------------
# keyboard navigation
# --------------------------------------------------------------------------

# The header hint is the only place bindings are advertised, so a key that
# works but is not named there is as good as missing.
AT_ANCHOR = """(i) => {
  const t = document.querySelectorAll('.cm-thread')[i];
  const a = parseFloat(getComputedStyle(t).scrollMarginTop) || 0;
  return Math.abs(t.getBoundingClientRect().top - a) < 12;
}"""


def press(page, key):
    page.keyboard.press(key)
    page.wait_for_timeout(80)


def test_j_and_k_walk_the_file_cards(kb_page, serve):
    open_review(kb_page, serve(DUP_DIFF))
    marked = kb_page.locator("details.file.kb")

    press(kb_page, "j")
    assert marked.count() == 1
    first = marked.get_attribute("id")
    press(kb_page, "j")
    assert marked.get_attribute("id") != first
    press(kb_page, "k")
    assert marked.get_attribute("id") == first


def test_o_toggles_the_marked_card_and_e_c_act_on_all(kb_page, serve):
    open_review(kb_page, serve(DUP_DIFF))
    press(kb_page, "j")
    card = kb_page.locator("details.file.kb")

    press(kb_page, "o")
    assert not card.evaluate("d => d.open")
    press(kb_page, "o")
    assert card.evaluate("d => d.open")

    press(kb_page, "c")
    assert kb_page.evaluate("[...document.querySelectorAll('details.file')].every(d => !d.open)")
    press(kb_page, "e")
    assert kb_page.evaluate("[...document.querySelectorAll('details.file')].every(d => d.open)")


def test_f_toggles_the_changed_files_list(kb_page, serve):
    open_review(kb_page, serve(BASIC_DIFF))
    lst = kb_page.locator("details.summary#filelist")
    assert not lst.evaluate("d => d.open")
    press(kb_page, "f")
    assert lst.evaluate("d => d.open")
    press(kb_page, "f")
    assert not lst.evaluate("d => d.open")


def test_n_and_p_reach_every_comment_including_the_first(kb_page, serve):
    # Anchoring on the viewport middle stranded the first comment, which sits
    # in the top half of the page and so was never "below" or "above" the mark.
    open_review(kb_page, serve(TALL_DIFF))
    rows = kb_page.locator("tr[data-n]")
    for i, row in enumerate([0, 40, 80]):
        add_comment(kb_page, rows.nth(row), f"note {i}")
    n = kb_page.locator(".cm-thread").count()
    assert n == 3
    assert kb_page.evaluate("document.body.scrollHeight > window.innerHeight * 2"), (
        "fixture must be tall enough for hopping to move the page"
    )

    press(kb_page, "g")
    for i in range(n):
        press(kb_page, "n")
        assert kb_page.evaluate(AT_ANCHOR, i), f"n did not land on comment {i}"
    press(kb_page, "n")
    assert kb_page.evaluate(AT_ANCHOR, n - 1), "n past the last comment should stay put"

    for i in range(n - 2, -1, -1):
        press(kb_page, "p")
        assert kb_page.evaluate(AT_ANCHOR, i), f"p did not land on comment {i}"
    press(kb_page, "p")
    assert kb_page.evaluate(AT_ANCHOR, 0), "p past the first comment should stay put"


def test_n_reveals_a_comment_inside_a_collapsed_card(kb_page, serve):
    # Two cards, one comment in each: with a single comment every ordering bug
    # lands on the same thread anyway, so the fallback that stands a hidden
    # thread in for its card goes unexercised.
    open_review(kb_page, serve(DUP_DIFF))
    cards = kb_page.locator("details.file")
    assert cards.count() == 2
    for i in range(2):
        add_comment(kb_page, cards.nth(i).locator("tr[data-n]").first, f"note {i}")
    ids = [cards.nth(i).get_attribute("id") for i in range(2)]

    press(kb_page, "c")
    assert kb_page.evaluate("[...document.querySelectorAll('details.file')].every(d => !d.open)")

    press(kb_page, "g")
    press(kb_page, "n")
    landed = kb_page.evaluate("document.querySelector('.cm-thread.kb').closest('details.file').id")
    assert landed == ids[0], "the first hop takes the first card's comment"
    assert kb_page.locator(f"#{ids[0]}").evaluate("d => d.open")

    press(kb_page, "n")
    landed = kb_page.evaluate("document.querySelector('.cm-thread.kb').closest('details.file').id")
    assert landed == ids[1], "the second hop crosses into the second card"
    assert kb_page.locator(f"#{ids[1]}").evaluate("d => d.open")


def test_n_and_p_reach_a_comment_in_the_last_screenful(kb_page, serve):
    # scrollIntoView cannot park a thread at its anchor once the document is
    # scrolled to the end, so a cursor derived from position re-picks the same
    # thread forever and every comment on the last file is unreachable.
    open_review(kb_page, serve(TALL_DIFF))
    rows = kb_page.locator("tr[data-n]")
    for i, row in enumerate([117, 119]):
        add_comment(kb_page, rows.nth(row), f"tail note {i}")

    def at():
        return kb_page.evaluate(
            "[...document.querySelectorAll('.cm-thread')].indexOf(document.querySelector('.cm-thread.kb'))"
        )

    # Walking down from the top has to reach both, including the last.
    press(kb_page, "g")
    press(kb_page, "n")
    assert at() == 0
    press(kb_page, "n")
    assert at() == 1, "the comment in the last screenful must be reachable"

    # And G seeds the cursor past the end, so p walks back into them.
    press(kb_page, "G")
    press(kb_page, "p")
    assert at() == 1
    press(kb_page, "p")
    assert at() == 0
    press(kb_page, "p")
    assert at() == 0, "p at the first comment stays put"


def test_a_jump_target_clears_the_sticky_header(kb_page, serve):
    # The clearance used to be a hardcoded 164px against a header that is 176px
    # here, so opening a block scrolled its own heading out of sight.
    open_review(kb_page, serve(BASIC_DIFF, "# Notes\n\nSome body.\n"))
    header = kb_page.evaluate("document.querySelector('.top').getBoundingClientRect().height")

    for key, sel in (("d", "#descblock summary"), ("f", "#filelist summary")):
        if key == "d":
            press(kb_page, "d")  # close first, so the next press opens and scrolls
        press(kb_page, key)
        top = kb_page.evaluate(
            "sel => document.querySelector(sel).getBoundingClientRect().top", sel
        )
        assert top >= header, f"{key} left {sel} {header - top:.0f}px under the header"


def test_hopping_survives_smooth_scrolling(page, serve):
    # `page` deliberately, not `kb_page`: this is the default motion setting,
    # where reading rects back mid-animation used to drop hops at ordinary
    # key-repeat speed.
    open_review(page, serve(TALL_DIFF))
    rows = page.locator("tr[data-n]")
    for i, row in enumerate([0, 40, 80]):
        add_comment(page, rows.nth(row), f"note {i}")

    page.keyboard.press("g")
    page.wait_for_timeout(150)
    for expected in (0, 1, 2):
        page.keyboard.press("n")
        page.wait_for_timeout(150)  # human repeat speed, mid-animation
        got = page.evaluate(
            "[...document.querySelectorAll('.cm-thread')].indexOf(document.querySelector('.cm-thread.kb'))"
        )
        assert got == expected, f"hop landed on {got}, expected {expected}"


def test_typing_a_comment_does_not_trigger_navigation(kb_page, serve):
    # Every bound letter, c and m and d included — without c the "all open"
    # assertion proves nothing, since e only re-opens what is already open.
    typed = "jkoecfmdgGnpr"
    open_review(kb_page, serve(DUP_DIFF, "# Notes\n"))
    kb_page.locator("tr[data-n]").first.locator("td.ln-n").click()
    kb_page.wait_for_selector(".cm-editor textarea")
    kb_page.locator(".cm-editor textarea").type(typed)

    assert kb_page.locator(".cm-editor textarea").input_value() == typed
    assert kb_page.locator("details.file.kb").count() == 0
    assert kb_page.evaluate("[...document.querySelectorAll('details.file')].every(d => d.open)")
    assert not kb_page.locator("#filelist").evaluate("d => d.open"), "f must not have fired"
    assert kb_page.locator("#descblock").evaluate("d => d.open"), "d must not have fired"
    assert kb_page.evaluate("window.scrollY") == 0, "g/G must not have fired"


def test_d_toggles_the_description(kb_page, serve):
    open_review(kb_page, serve(BASIC_DIFF, "# Notes\n\nSome body.\n"))
    desc = kb_page.locator("details.desc#descblock")

    press(kb_page, "d")
    assert not desc.evaluate("d => d.open")
    press(kb_page, "d")
    assert desc.evaluate("d => d.open")


def test_the_hint_only_offers_keys_for_blocks_that_rendered(page, serve):
    # A review with no --body has no Description, so d must not be advertised.
    open_review(page, serve(BASIC_DIFF))
    bare = page.locator(".keys").inner_text()
    # Whole segments: bare "o" is satisfied by "top/end" and "f" by "file", so
    # either assertion would pass with its hint deleted.
    for seg in (
        "j/k file",
        "o open",
        "e/c all",
        "f list",
        "g/G top/end",
        "n/p comment",
        "r review",
    ):
        assert seg in bare, f"{seg!r} missing from the header hint"
    assert "description" not in bare
    assert page.locator("#descblock").count() == 0

    open_review(page, serve(BASIC_DIFF, "# Notes\n"))
    assert "description" in page.locator(".keys").inner_text()


def test_r_opens_the_folded_review_box_and_esc_closes_it(kb_page, serve):
    # r only opens: once focus is in the box the global handler ignores it, so
    # it types an "r" rather than closing. Esc is the way back out.
    open_review(kb_page, serve(BASIC_DIFF))
    box = kb_page.locator("#rv-general")
    bar = kb_page.locator("#rv-bar")
    folded = bar.bounding_box()["height"]
    assert not box.is_visible(), "the review box starts folded"

    press(kb_page, "r")
    assert box.is_visible()
    assert kb_page.evaluate("document.activeElement.id") == "rv-general"
    assert bar.bounding_box()["height"] > folded, "opening should cost height, not be a no-op"

    kb_page.keyboard.press("Escape")
    kb_page.wait_for_timeout(80)
    assert not box.is_visible()
    assert bar.bounding_box()["height"] == folded

    press(kb_page, "r")
    assert box.is_visible(), "r reopens after Esc"


def test_the_comment_count_click_toggles_the_review_box(page, serve):
    open_review(page, serve(BASIC_DIFF))
    box = page.locator("#rv-general")
    assert not box.is_visible()
    page.locator("#rv-fold-toggle").click()
    assert box.is_visible()
    page.locator("#rv-fold-toggle").click()
    assert not box.is_visible()


def test_a_folded_review_box_says_it_still_holds_a_draft(kb_page, serve):
    # Folding hides typed text. Hiding it silently is how a review gets lost.
    open_review(kb_page, serve(BASIC_DIFF))
    press(kb_page, "r")
    kb_page.locator("#rv-general").type("a partial thought")
    assert not kb_page.locator("#rv-draft").is_visible(), "no marker while it is on screen"

    kb_page.keyboard.press("Escape")
    kb_page.wait_for_timeout(80)
    assert not kb_page.locator("#rv-general").is_visible()
    assert kb_page.locator("#rv-draft").is_visible()
    assert kb_page.locator("#rv-general").input_value() == "a partial thought"


def test_typing_in_the_review_box_does_not_navigate(kb_page, serve):
    open_review(kb_page, serve(BASIC_DIFF))
    press(kb_page, "r")
    kb_page.locator("#rv-general").type("worry over jokes")
    assert kb_page.locator("#rv-general").input_value() == "worry over jokes"
    assert kb_page.locator("#rv-general").is_visible(), "r in the box types, not folds"
    assert kb_page.locator("details.file.kb").count() == 0


def test_cmd_enter_submits_from_the_review_box(page, serve):
    # The line-comment editor has had this since it shipped; the review box did
    # not, and nothing said so either way.
    open_review(page, serve(BASIC_DIFF))
    page.locator("#rv-name").fill("Matt")
    write_review(page, "ship it")
    page.locator("#rv-general").press("Meta+Enter")

    page.wait_for_selector("#rv-msg.ok", timeout=5000)
    assert "review.md" in page.locator("#rv-msg").inner_text()


# --------------------------------------------------------------------------
# narrow screens
# --------------------------------------------------------------------------


def test_the_collapsible_headers_are_thumb_sized_on_a_phone(phone_page, serve):
    # Collapsing put the page's controls on text-height rows. Everything else
    # that takes a tap is card-height and was already fine.
    open_review(phone_page, serve(BASIC_DIFF, "# Notes\n\nSome body.\n"))
    heights = phone_page.evaluate(
        """() => [...document.querySelectorAll('summary.sumhead, summary.desc-head, .file-head')]
             .map(e => e.getBoundingClientRect().height)"""
    )
    assert heights, "expected collapsible headers to measure"
    assert min(heights) >= 44, f"tap target under 44px: {heights}"


def test_the_page_does_not_scroll_sideways_on_a_phone(phone_page, serve):
    open_review(phone_page, serve(BASIC_DIFF, "# Notes\n\nSome body.\n"))
    assert phone_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    # The keybind hint is the widest thing in the header and no use without a keyboard.
    assert not phone_page.locator(".keys").is_visible()


def test_tapping_a_header_toggles_it_on_a_phone(phone_page, serve):
    open_review(phone_page, serve(BASIC_DIFF, "# Notes\n\nSome body.\n"))
    lst = phone_page.locator("#filelist")

    phone_page.locator("summary.sumhead h3").tap()
    assert lst.evaluate("d => d.open")
    assert phone_page.locator("a.chip").first.is_visible()

    # Tap the left edge, not the element centre. This summary carries the raw/
    # rendered button, whose handler stops the event so the details does *not*
    # toggle — and where that button sits depends on how wide the header font
    # renders. On a machine with none of the families in --sans it drifts under
    # the centre point, and the tap silently hits the button instead.
    phone_page.locator("#descblock summary").tap(position={"x": 12, "y": 12})
    assert not phone_page.locator("#descblock").evaluate("d => d.open")


# --------------------------------------------------------------------------
# the saved artifact
# --------------------------------------------------------------------------


def test_the_saved_artifact_is_read_only_with_comments_anchored(page, browser, serve):
    open_review(page, serve(BASIC_DIFF))
    add_comment(page, page.locator("tr[data-n]").first, "a finding")
    page.locator("#rv-name").fill("Matt")
    write_review(page, "The review body.")
    page.locator("#rv-submit").click()
    page.wait_for_selector("#rv-msg.ok", timeout=5000)
    msg = page.locator("#rv-msg").inner_text()
    art = next(w for w in msg.split() if w.endswith("review.html"))
    assert any(w.endswith("review.md") for w in msg.split()), "md path leads the message"

    saved = browser.new_page()
    errors: list[str] = []
    saved.on("pageerror", lambda e: errors.append(str(e)))
    saved.goto("file://" + art)
    saved.wait_for_load_state("networkidle")

    assert saved.locator("#rv-bar").count() == 0, "no submit bar once submitted"
    assert "rv" not in (saved.locator("body").get_attribute("class") or "")
    assert saved.locator(".cm-thread").count() == 1
    assert saved.locator(".cm-del").count() == 0, "read-only: nothing to delete"
    assert saved.locator(".rv-orphan").count() == 0
    assert "Matt" in saved.locator("#rv-summary").inner_text()
    # The review box is gone, so the hint must stop offering the key for it —
    # but n/p still work, and the artifact is exactly where hopping is wanted.
    hint = saved.locator(".keys").inner_text()
    assert "review" not in hint
    assert "n/p" in hint
    assert not errors
    saved.close()


def test_an_unanchorable_comment_is_reported_not_dropped(page, browser, serve):
    server = serve(BASIC_DIFF)
    res = server.post(
        {
            "reviewer": "agent",
            "body": "posted straight to the endpoint",
            "comments": [{"path": "not-in-this-diff.py", "line": 999, "body": "orphan"}],
        }
    )
    saved = browser.new_page()
    saved.goto("file://" + res["review_html"])
    saved.wait_for_load_state("networkidle")
    assert saved.locator(".rv-orphan").count() == 1
    assert "orphan" in saved.locator(".rv-orphan").inner_text()
    saved.close()


# --------------------------------------------------------------------------
# review markdown
# --------------------------------------------------------------------------


def test_the_review_body_renders_as_markdown(page, browser, serve):
    server = serve(BASIC_DIFF)
    res = server.post(
        {
            "reviewer": "agent",
            "body": "**[P1]** `a.py:1` broken\n\n```python\nreturn None\n```\n\n- one\n- two",
            "comments": [],
        }
    )
    saved = browser.new_page()
    saved.goto("file://" + res["review_html"])
    saved.wait_for_load_state("networkidle")
    summary = saved.locator("#rv-summary")
    assert summary.locator("pre code").count() >= 1, "fenced block must render"
    assert summary.locator("strong").count() >= 1
    assert summary.locator("li").count() >= 2
    assert "```" not in summary.inner_text(), "no literal fence markers"
    saved.close()


@pytest.mark.parametrize(
    "hostile, selector",
    [
        ('<form action="javascript:void(0)"><button>go</button></form>', "form"),
        ('<button formaction="javascript:void(0)">go</button>', "button"),
        ('<svg><a xlink:href="javascript:void(0)"><text>x</text></a></svg>', "svg"),
        ('<iframe srcdoc="<script>window.PWNED=1</script>"></iframe>', "iframe"),
        ('<div id="review-data">spoof</div>', "[id]"),
    ],
)
def test_review_markdown_is_allowlisted(page, browser, serve, hostile, selector):
    # A review is written by one person and opened by another, so naming a few
    # dangerous attributes is the wrong shape of check.
    server = serve(BASIC_DIFF)
    res = server.post({"reviewer": "attacker", "body": f"looks fine\n\n{hostile}", "comments": []})
    saved = browser.new_page()
    saved.goto("file://" + res["review_html"])
    saved.wait_for_load_state("networkidle")
    assert saved.locator(f"#rv-summary {selector}").count() == 0
    assert saved.evaluate("() => window.PWNED === undefined")
    saved.close()


def test_no_executable_urls_or_handlers_survive(page, browser, serve):
    server = serve(BASIC_DIFF)
    res = server.post(
        {
            "reviewer": "attacker",
            "body": (
                '<a href="javascript:void(0)">a</a> '
                '<img src="x" onerror="window.PWNED=1"> '
                '<p onclick="window.PWNED=1">p</p>'
            ),
            "comments": [],
        }
    )
    saved = browser.new_page()
    saved.goto("file://" + res["review_html"])
    saved.wait_for_load_state("networkidle")
    leftovers = saved.evaluate(
        """() => {
             const els = [...document.querySelectorAll('#rv-summary *')];
             return {
               handlers: els.filter(e => e.getAttributeNames().some(a => /^on/i.test(a))).length,
               jsUrls: els.filter(e => ['href','src'].some(
                 a => /^\\s*javascript:/i.test(e.getAttribute(a) || ''))).length,
             };
           }"""
    )
    assert leftovers == {"handlers": 0, "jsUrls": 0}
    assert saved.evaluate("() => window.PWNED === undefined")
    saved.close()


# ---- split view ----------------------------------------------------------
# Split is built from the unified rows and drops the thread rows, emitting <tr>
# without data-o/data-n. That makes it a read view by construction: the
# delegated gutter handler has nothing to anchor to. Nothing enforced it, so
# these pin the two places the offer has to be withdrawn.


def render_plain(tmp_path, diff_text):
    """A non-review page on disk, for the cases that must not involve the server."""
    src = tmp_path / "d.diff"
    src.write_text(diff_text, encoding="utf-8")
    out = tmp_path / "d.html"
    proc = subprocess.run(
        [str(SCRIPT), "--from-diff", str(src), "--no-open", "-o", str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return "file://" + str(out)


def test_split_is_not_offered_under_review(page, serve):
    """Taking split in review mode silently turns line commenting off.

    buildSplit emits rows the gutter handler can't anchor to and filters threads
    out, so a reviewer who pressed `v` lost commenting with nothing on the page
    explaining why.
    """
    open_review(page, serve(BASIC_DIFF))
    assert page.locator("#view-toggle").is_hidden()
    assert page.locator(".keys .k-v").is_hidden()

    page.keyboard.press("v")
    page.wait_for_timeout(100)
    assert page.evaluate("document.documentElement.getAttribute('data-view')") == "unified"
    assert page.locator("table.diff-split").count() == 0

    # The point of withholding it: commenting still works.
    page.locator("tr[data-n] td.ln-n").first.click()
    page.wait_for_selector(".cm-editor textarea")


def test_split_is_not_offered_below_the_breakpoint(phone_page, tmp_path):
    """effView() forces unified under SPLIT_MIN, so the control there did nothing.

    It then relabelled itself from `want`, claiming the state it had failed to
    reach, and persisted that choice for the next wide load.
    """
    phone_page.goto(render_plain(tmp_path, BASIC_DIFF))
    phone_page.wait_for_load_state("networkidle")
    assert phone_page.locator("#view-toggle").is_hidden()

    phone_page.keyboard.press("v")
    phone_page.wait_for_timeout(100)
    assert phone_page.evaluate("document.documentElement.getAttribute('data-view')") == "unified"
    assert phone_page.locator("table.diff-split").count() == 0
    # Nothing was written, so a later wide load doesn't open in split by surprise.
    assert phone_page.evaluate("localStorage.getItem('pd-view')") is None


def test_split_toggle_labels_the_view_it_applied(page, tmp_path):
    """The label comes from the applied view, not the requested one."""
    page.goto(render_plain(tmp_path, BASIC_DIFF))
    page.wait_for_load_state("networkidle")
    toggle = page.locator("#view-toggle")
    assert toggle.is_visible()
    assert toggle.text_content().strip() == "Split"

    toggle.click()
    page.wait_for_timeout(100)
    assert page.evaluate("document.documentElement.getAttribute('data-view')") == "split"
    assert page.locator("table.diff-split").count() > 0
    assert toggle.text_content().strip() == "Unified"

    toggle.click()
    page.wait_for_timeout(100)
    assert page.evaluate("document.documentElement.getAttribute('data-view')") == "unified"
    assert toggle.text_content().strip() == "Split"
