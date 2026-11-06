#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "playwright"]
# ///
"""Layout tests for template.html. Run via
`uv run --with pytest --with playwright claude-code-skills/pr-dashboard/test_template.py`
(or `just test`).

The template is a fixed renderer driven by a JSON island, so these fill the island
with a board shaped like a real one and measure the result in a browser. The bug
they exist for: every layout in the template is a column count, and a phone can't
honor them — the queue's five auto-sized columns squeezed the prose to ~60px, six
characters a line, and pushed the status pill under `.queue`'s `overflow:hidden`
where it was clipped rather than wrapped.

Long `why` strings are load-bearing. Short ones fit any column width and the
squeeze never shows up.
"""

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

TEMPLATE = Path(__file__).parent / "template.html"
PHONE = 390
DESKTOP = 1200
# The band 641-1199 was unmeasured, and the queue's prose column was at its
# narrowest of any viewport at the bottom of it. BAND sits just inside the
# reflow, WIDE just outside, so the pair pins the breakpoint itself.
BAND = 641
WIDE = 721

BOARD = {
    "title": "PR dashboard — owner/project",
    "subtitle": "12 open PRs across 3 workstreams",
    "generatedAt": "2026-08-12 23:21:51 PDT",
    "legend": True,
    "queue": [
        {
            "rank": 1,
            "ref": "owner/project#698",
            "author": "a-maintainer",
            "action": "review",
            "why": "Bottom of the 3-deep os stack, CI green, nobody has looked — "
            "clears #699 and the already-approved #717 above it.",
            "lastTouch": {
                "kind": "force-push",
                "by": "a-maintainer",
                "at": "2026-08-10T13:00:00Z",
            },
            "url": "https://example.test/pull/698",
            "closes": [690],
            "approvals": {"atHead": [], "stale": ["a-reviewer"]},
            "threads": {"unresolved": 3, "outdated": 1, "replied": 1},
            "reviewers": {"requested": ["r2"], "reviewed": ["a-reviewer"]},
            "comments": 7,
            "mergeReady": {
                "verdict": "blocked",
                "checks": [
                    {
                        "check": "approval",
                        "level": "block",
                        "state": "fail",
                        "note": "approval predates head",
                    },
                    {
                        "check": "changes",
                        "level": "block",
                        "state": "fail",
                        "note": "changes requested by a-reviewer, r2",
                    },
                    {"check": "behind", "level": "warn", "state": "warn", "note": "behind main"},
                    {"check": "ci", "level": "block", "state": "pass", "note": None},
                ],
            },
        },
        {
            "rank": 2,
            "ref": "owner/project#501",
            "author": "another-person",
            "action": "blocked",
            "why": "Badge still reads CHANGES_REQUESTED, but the last touch is the "
            "author's force-push — the turn is back with a reviewer. Gates #502.",
        },
    ],
    "sections": [
        {
            "repo": "owner/project",
            "base": "main 4f1c9ab",
            "lanes": [
                {
                    "title": "os stack",
                    "meta": "off main",
                    "accent": "active",
                    "kind": "stack",
                    "items": [
                        {
                            "pr": "#699",
                            "branch": "feat/os-reader-and-the-shared-fd-table",
                            "desc": "reader half, plus the shared fd table it needs",
                            "sub": "↳ on #698",
                            "status": "review",
                            "author": "a-maintainer",
                            "approvals": {"atHead": ["r1", "r2"], "stale": []},
                            "threads": {"unresolved": 0, "outdated": 0},
                        },
                        {
                            "pr": "#698",
                            "branch": "feat/os-core",
                            "desc": "core namespace scaffolding",
                            "status": "review",
                            "author": "a-maintainer",
                            "approvals": {"atHead": [], "stale": []},
                        },
                    ],
                },
                {
                    "title": "loose ends",
                    "kind": "grid",
                    "span": True,
                    "items": [
                        {
                            "pr": "#666",
                            "branch": "chore/dep-bump",
                            "desc": "routine dependency bump",
                            "status": "review",
                            "author": "third-contributor",
                            "approvals": None,
                            "threads": None,
                        }
                    ],
                },
            ],
        }
    ],
    "footnotes": ["#698 is the single highest-leverage review."],
}


def _board_file(tmp_path_factory, model):
    """template.html with its sample island swapped for `model`."""
    html = TEMPLATE.read_text(encoding="utf-8")
    island = json.dumps(model, indent=2)
    html = re.sub(
        r'(<script id="dash-data" type="application/json">)(.*?)(</script>)',
        lambda m: m.group(1) + "\n" + island + "\n" + m.group(3),
        html,
        flags=re.S,
    )
    out = tmp_path_factory.mktemp("board") / "board.html"
    out.write_text(html, encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    return _board_file(tmp_path_factory, BOARD)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _page(browser, board, width):
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.goto(board.as_uri())
    page.wait_for_selector(".qrow")
    return page


def test_review_signals_say_what_was_measured(browser, board):
    # Every measured state gets its own chip, zero included — a board read by
    # what is missing can't tell "none" from "not measured" or "not rendered".
    page = _page(browser, board, WIDE)
    at = """(sel) => [...document.querySelectorAll(sel + ' .sig')]
              .filter(e => e.offsetParent !== null).map(e => e.textContent)"""
    sigs = {"queue": page.evaluate(f"({at})('.qrow')")}
    page.click("[data-tab=work]")  # lanes live on their own tab
    sigs["stack"] = page.evaluate(f"({at})('.slice')")
    sigs["grid"] = page.evaluate(f"({at})('.card')")
    page.close()
    assert sigs == {
        # 721px is too narrow for this board's scan line, so the queue stacks
        # and the chips carry their full wording.
        "queue": [
            "✗ stale approval +1",
            "1/2 reviewed",
            "approval predates head",
            "3 open threads: 2 unanswered · 1 replied · 1 outdated",
            "7 comments",
        ],
        "stack": ["✓ 2 approvals on head", "no open threads", "no approval on head"],
        "grid": ["approvals ?", "threads ?"],
    }


def test_nothing_is_clipped_or_overflowing_on_a_phone(browser, board):
    """`.queue` and `.lane` set overflow:hidden, so a too-wide row is *silently* cut.

    Measuring the document's scroll width can't see that — the page reports no
    overflow while the status pill sits outside the card. Check every element
    against the viewport instead.
    """
    page = _page(browser, board, PHONE)
    past_edge = {}
    # Every tab: a hidden pane measures zero wide, so checking only the
    # default tab would pass for lanes nobody looked at.
    for tab in page.evaluate(
        "[...document.querySelectorAll('[data-tab]')].map(b => b.dataset.tab)"
    ):
        page.click(f"[data-tab={tab}]")
        past_edge[tab] = page.evaluate("""() => {
          const vw = document.documentElement.clientWidth;
          return [...document.querySelectorAll('body *')]
            .filter(e => e.getBoundingClientRect().right > vw + 1)
            .map(e => e.tagName + '.' + e.className);
        }""")
    page.close()
    assert len(past_edge) > 1
    assert all(v == [] for v in past_edge.values()), past_edge


@pytest.mark.parametrize("width", [PHONE, BAND, WIDE], ids=["phone", "band", "wide"])
def test_queue_prose_keeps_a_readable_column(browser, board, width):
    """The reported symptom: ~60px of text column, six characters a line.

    Measured on both sides of the breakpoint as well as on a phone: the column
    was worst just above the old 640px query, which is the one width nothing
    checked.
    """
    page = _page(browser, board, width)
    why = page.evaluate(
        "Math.round(document.querySelector('.qrow .why').getBoundingClientRect().width)"
    )
    assert why > 250, f"queue prose column is {why}px at {width}px wide"
    page.close()


@pytest.mark.parametrize("width", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_header_chrome_is_one_row_at_every_width(browser, board, width):
    """Status key, board age and theme switch share a row — same layout everywhere.

    They used to be two chips pinned to opposite corners plus an in-flow summary.
    Pinning cost a reserved band at the top that was empty at every width where
    the chips didn't actually collide.
    """
    page = _page(browser, board, width)
    tops = page.evaluate("""() => [...document.querySelectorAll('.meta-row > *')]
        .map(e => Math.round(e.getBoundingClientRect().top))""")
    assert len(tops) == 3, f"expected key, stamp and theme in the row, got {len(tops)}"
    assert len(set(tops)) == 1, f"header chrome wrapped at {width}px: tops {tops}"
    page.close()


@pytest.mark.parametrize("width", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_nothing_is_pinned_so_no_band_is_reserved(browser, board, width):
    """The blank strip above the title was room held for position:fixed chips."""
    page = _page(browser, board, width)
    for node in ("generated", "theme"):
        pos = page.evaluate(f"getComputedStyle(document.getElementById('{node}')).position")
        assert pos != "fixed", f"#{node} is still pinned at {width}px"
    gap = page.evaluate("""() => Math.round(
        document.querySelector('h1').getBoundingClientRect().top
        - parseFloat(getComputedStyle(document.body).paddingTop))""")
    assert abs(gap) <= 2, f"title sits {gap}px below the body padding at {width}px"
    page.close()


@pytest.mark.parametrize("width", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_legend_folds_at_every_width(browser, board, width):
    """Seven rows of key ahead of the queue is a lot of screen on any display."""
    page = _page(browser, board, width)
    assert page.evaluate("document.querySelector('.legend-box').open") is False
    assert page.locator(".legend-box > summary").is_visible()
    assert page.locator(".legend").is_hidden()

    page.locator(".legend-box > summary").click()
    assert page.locator(".legend").is_visible()
    page.close()


@pytest.mark.parametrize("width", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_the_header_chips_are_one_height(browser, board, width):
    """Top alignment only reads as alignment when the chips match.

    They're a summary, a div and a button in a flex column, so their own padding
    left the theme switch 4px short of its neighbours.
    """
    page = _page(browser, board, width)
    boxes = page.evaluate("""() => ['.legend-box > summary','#generated','#theme']
        .map(s => Math.round(document.querySelector(s).getBoundingClientRect().height))""")
    assert len(set(boxes)) == 1, f"chip heights differ at {width}px: {boxes}"
    page.close()


@pytest.mark.parametrize("width", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_opening_the_legend_does_not_move_the_header(browser, board, width):
    """In the flow the legend widened its flex item and bumped the other two chips
    onto a second line, so consulting the key rearranged the row holding it."""
    page = _page(browser, board, width)

    def tops():
        return page.evaluate("""() => ['#generated','#theme']
            .map(s => Math.round(document.querySelector(s).getBoundingClientRect().top))""")

    before = tops()
    page.locator(".legend-box > summary").click()
    assert page.locator(".legend").is_visible()
    assert tops() == before, "header chips moved when the legend opened"
    page.close()


def test_the_legend_panel_can_be_dismissed(browser, board):
    """It overlays the queue, so it needs the same exits as the theme menu.

    <details> only knows how to close from its own summary; left at that, the
    panel would sit over the board while you scrolled past it.
    """
    page = _page(browser, board, DESKTOP)

    def is_open():
        return page.evaluate("!!document.querySelector('.legend-box[open]')")

    for dismiss in ("outside", "escape"):
        page.locator(".legend-box > summary").click()
        assert is_open()
        if dismiss == "outside":
            page.locator("h1").click()
        else:
            page.keyboard.press("Escape")
        assert not is_open(), f"not dismissed by {dismiss}"
    page.close()


def test_the_header_row_survives_a_board_with_no_legend(browser, tmp_path_factory):
    """`legend:false` removes the summary, not the stamp and the switch."""
    no_legend = dict(BOARD, legend=False)
    page = _page(browser, _board_file(tmp_path_factory, no_legend), PHONE)
    assert page.locator(".legend-box").count() == 0
    assert page.evaluate("!!document.getElementById('generated').closest('.meta-row')")
    assert page.evaluate("!!document.getElementById('theme').closest('.meta-row')")
    page.close()


def test_theme_switch_is_collapsed_until_asked(browser, board):
    """Three permanent buttons cost chrome at every width for a set-once control."""
    page = _page(browser, board, PHONE)
    assert page.locator(".theme-menu").is_hidden()
    assert page.locator("#theme-cur").text_content().strip() == "System"

    page.locator("#theme-cur").click()
    assert page.locator(".theme-menu").is_visible()

    page.locator("[data-mode='dark']").click()
    # Choosing closes it, and the trigger reports the mode now in force.
    assert page.locator(".theme-menu").is_hidden()
    assert page.locator("#theme-cur").text_content().strip() == "Dark"
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
    page.close()


def test_the_expanded_theme_menu_can_be_dismissed(browser, board):
    """A popup that only closes by re-tapping its trigger is the usual bug here."""
    page = _page(browser, board, PHONE)
    for dismiss in ("outside", "escape"):
        page.locator("#theme-cur").click()
        assert page.locator(".theme-menu").is_visible()
        if dismiss == "outside":
            page.locator("h1").click()
        else:
            page.keyboard.press("Escape")
        assert page.locator(".theme-menu").is_hidden(), f"not dismissed by {dismiss}"
    page.close()


def test_desktop_keeps_its_multi_column_queue(browser, board):
    """The narrow rules must not leak upward — the wide layout is the default."""
    page = _page(browser, board, DESKTOP)
    columns = page.evaluate(
        "getComputedStyle(document.querySelector('.queue')).gridTemplateColumns.split(' ').length"
    )
    assert columns == 10, f"desktop queue has {columns} columns, expected 10"
    page.close()


def test_queue_signal_columns_line_up_across_rows(browser, board):
    """The point of the scan line: one column answers one question for every row."""
    page = _page(browser, board, DESKTOP)
    lefts = page.evaluate(
        """() => ['.act', '.merge', '.revs', '.appr', '.thr', '.cmts'].map(sel =>
             [...document.querySelectorAll('.qrow ' + sel)]
               .map(e => Math.round(e.getBoundingClientRect().left)))"""
    )
    page.close()
    for col in lefts:
        assert len(set(col)) == 1, f"queue column is ragged: {col}"


@pytest.mark.parametrize("width", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_queue_signals_are_worded_for_their_layout(browser, board, width):
    # Desktop has column headers, so "none" is enough; a phone hides them and
    # must say what "none" is of.
    page = _page(browser, board, width)
    shown = page.evaluate(
        """() => [...document.querySelectorAll('.qrow .sigcell .sig')]
             .filter(e => e.offsetParent !== null).map(e => e.textContent)"""
    )
    page.close()
    if width == DESKTOP:
        assert shown == [
            "✗ stale approval +1",
            "1/2 reviewed",
            "predates head",
            "2 unanswered · 1 replied",
            "7",
        ]
    else:
        assert shown == [
            "✗ stale approval +1",
            "1/2 reviewed",
            "approval predates head",
            "3 open threads: 2 unanswered · 1 replied · 1 outdated",
            "7 comments",
        ]


def test_merge_chip_title_lists_every_open_check(browser, board):
    # The chip names one reason so the column keeps its width; the rest,
    # warnings included, must still be one hover away.
    page = _page(browser, board, DESKTOP)
    title = page.evaluate("document.querySelector('.qrow .merge .lbl-short .sig').title")
    page.close()
    assert title.splitlines() == [
        "✗ approval: approval predates head",
        "✗ changes: changes requested by a-reviewer, r2",
        "! behind: behind main",
    ]


@pytest.mark.parametrize("width", [WIDE, 800, 901, 1000, DESKTOP])
def test_the_queue_never_overflows_it_stacks(browser, board, width):
    # Where the scan line fits depends on the board's content, so the queue
    # measures itself; at no width may it clip a column instead.
    page = _page(browser, board, width)
    past = page.evaluate(
        """() => { const q = document.querySelector('.queue').getBoundingClientRect().right;
             return [...document.querySelectorAll('.queue *')]
               .filter(e => e.offsetParent !== null && e.getBoundingClientRect().right > q + 1)
               .map(e => e.className); }"""
    )
    page.close()
    assert past == []


def _approval_none(browser, tmp_path_factory, hours_ago):
    import datetime

    asked = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    model = json.loads(json.dumps(BOARD))
    model["queue"][0]["approvals"] = {"atHead": [], "stale": []}
    model["queue"][0]["reviewers"] = {
        "requested": ["r2"],
        "reviewed": [],
        "lastRequestedAt": asked.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    page = _page(browser, _board_file(tmp_path_factory, model), DESKTOP)
    cls = page.evaluate("document.querySelector('.qrow .appr .lbl-short .sig').className")
    page.close()
    return cls


def test_no_approval_is_grey_until_a_nudge_is_due(browser, tmp_path_factory):
    # A review requested this morning isn't late; one requested three days ago is.
    assert _approval_none(browser, tmp_path_factory, 5) == "sig"
    assert _approval_none(browser, tmp_path_factory, 72) == "sig sig-wait"


def test_open_threads_link_to_the_pr_and_closed_ones_do_not(browser, board):
    # The chip is where the eye lands when threads are open, and reading them
    # is the next step. "none" has nothing to go and read.
    page = _page(browser, board, DESKTOP)
    links = page.evaluate(
        """() => [...document.querySelectorAll('.sig')].filter(e => e.offsetParent !== null)
             .map(e => [e.textContent, e.getAttribute('href')])
             .filter(([t]) => /open|unanswered|replied/.test(t))"""
    )
    page.close()
    assert ["2 unanswered · 1 replied", "https://example.test/pull/698"] in links
    assert all(href is None for text, href in links if text in ("none", "no open threads"))


def _tabs(page):
    return page.evaluate(
        """() => [...document.querySelectorAll('[data-tab]')]
             .map(b => [b.dataset.tab, b.getAttribute('aria-selected')])"""
    )


def test_tabs_exist_only_for_content_and_the_hash_picks_one(browser, board):
    page = _page(browser, board, DESKTOP)
    # The fixture has workstream lanes but no issues or merged lanes.
    assert _tabs(page) == [["now", "true"], ["work", "false"]]
    page.goto(board.as_uri() + "#tab=work")
    page.reload()
    page.wait_for_selector(".lane")
    assert _tabs(page) == [["now", "false"], ["work", "true"]]
    assert page.is_visible(".lane") and not page.is_visible(".queue")
    page.close()


@pytest.mark.parametrize(
    "changes, expect",
    [
        (
            {"first": True},
            ["First scan of this board: baseline saved. What moves shows here next time."],
        ),
        ({"since": "2026-10-19T09:00:00+00:00", "items": []}, ["Nothing moved."]),
        (
            {
                "since": "2026-10-19T09:00:00+00:00",
                "items": [
                    {
                        "ref": "owner/project#7",
                        "url": "u7",
                        "kind": "ci",
                        "tone": "bad",
                        "text": "CI: pass → fail",
                    },
                    {
                        "ref": "owner/project#7",
                        "url": "u7",
                        "kind": "comments",
                        "tone": "info",
                        "text": "+2 comments",
                    },
                    {
                        "ref": "owner/project#9",
                        "url": "u9",
                        "kind": "merged",
                        "tone": "good",
                        "text": "merged",
                    },
                ],
            },
            ["#7", "CI: pass → fail", "+2 comments", "#9", "merged"],
        ),
    ],
    ids=["first", "quiet", "moved"],
)
def test_since_last_scan_states(browser, tmp_path_factory, changes, expect):
    # One row per PR, in one repo shown without the repo prefix.
    model = json.loads(json.dumps(BOARD))
    model["changes"] = changes
    page = _page(browser, _board_file(tmp_path_factory, model), DESKTOP)
    got = page.evaluate(
        """() => [...document.querySelectorAll('.changes .quiet, .changes .crow .ref, .changes .crow .sig')]
             .map(e => e.textContent)"""
    )
    page.close()
    assert got == expect


def test_a_reviewed_tick_dims_the_row_until_the_pr_moves(browser, board):
    page = _page(browser, board, DESKTOP)
    row = page.locator(".qrow").first
    box, note = row.locator("input[type=checkbox]"), row.locator(".seen-note")
    box.check()
    assert "seen" in row.get_attribute("class")
    assert note.text_content() == "looked just now"
    # A tick from before the PR's last touch: still ticked, no longer dimmed.
    page.evaluate(
        "localStorage.setItem('prdash:seen:https://example.test/pull/698', '2026-08-01T00:00:00Z')"
    )
    page.reload()
    page.wait_for_selector(".qrow")
    assert box.is_checked()
    assert "seen" not in row.get_attribute("class")
    assert note.text_content() == "changed since you looked"
    box.uncheck()
    assert note.text_content() == ""
    page.close()


def test_closes_links_to_the_issue(browser, board):
    page = _page(browser, board, DESKTOP)
    link = page.evaluate(
        "[...document.querySelectorAll('.qrow .closes a')].map(a => [a.textContent, a.href])"
    )
    page.close()
    assert link == [["#690", "https://example.test/issues/690"]]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
