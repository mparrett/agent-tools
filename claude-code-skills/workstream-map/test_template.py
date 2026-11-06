#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pytest", "playwright"]
# ///
"""Layout tests for template.html. Run via
`uv run --with pytest --with playwright claude-code-skills/workstream-map/test_template.py`
(or `just test`). Needs network for d3 from cdnjs, like the page itself.

The template is a fixed renderer fed by placeholders build.py fills. These fill
them with a small map shaped like a real one and measure the result in a
browser. The bugs they exist for: lane headers running into the next column
when seven lanes share a laptop width, the seventh lane pushed off-screen by a
fixed column width, and SVG labels that swallow clicks so the panel never opens.
"""

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

TEMPLATE = Path(__file__).parent / "template.html"
DESKTOP = 1500
LAPTOP = 1100

LANES = [
    ["ci", "CI timeline & runners"],
    ["gate", "Ratchet, baselines & gates"],
    ["vm", "VM hot paths & call ABI"],
    ["ir", "IR / AOT lowering"],
    ["foot", "Startup, footprint & targets"],
    ["xr", "xsofy: render perf"],
    ["xb", "xsofy: bench & determinism"],
]
PAL = {
    k: c
    for (k, _), c in zip(
        LANES, ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7", "#eda100", "#e87ba4", "#008300"]
    )
}


def node(i, lane, pr, state, day, start="", author="alice"):
    return dict(
        id=f"o/r#{i}",
        repo="o/r",
        num=i,
        label=f"#{i}",
        pr=pr,
        state=state,
        title=f"a long enough title for item {i} that wraps in a panel",
        author=author,
        created=f"2026-0{day[0]}-{day[1]:02d}",
        closed="",
        merged="",
        url=f"https://example.invalid/{i}",
        lane=lane,
        labels="",
        base="main" if pr else "",
        head=f"wt/{i}" if pr else "",
        notes=[],
        start=start,
        engaged=["alice"] if author != "alice" else [],
        deg=0,
    )


NODES = [
    node(100, "ci", False, "open", (6, 1), start="the tracker"),
    node(101, "ci", True, "merged", (6, 5)),
    node(102, "gate", True, "closed", (6, 9)),
    node(103, "vm", True, "merged", (7, 1)),
    node(104, "ir", False, "open", (7, 3)),
    node(105, "foot", True, "merged", (7, 9)),
    node(106, "xr", True, "merged", (8, 1)),
    node(107, "xb", False, "open", (8, 2), author="bob"),
]
EDGES = [
    dict(s="o/r#101", t="o/r#100", w=1),
    dict(s="o/r#103", t="o/r#100", w=2),
    dict(s="o/r#104", t="o/r#103", w=1),
]
for e in EDGES:
    for k in ("s", "t"):
        next(n for n in NODES if n["id"] == e[k])["deg"] += 1


CONTRIBUTORS = [{"login": "alice", "n": 8}, {"login": "bob", "n": 1}]
PHONE = 390


def _map_file(tmp_path_factory):
    html = TEMPLATE.read_text(encoding="utf-8")
    data = dict(
        generated="2026-09-15 04:30 UTC",
        lanes=LANES,
        nodes=NODES,
        edges=EDGES,
        contributors=CONTRIBUTORS,
    )
    html = (
        html.replace("__DATA__", json.dumps(data))
        .replace("__PAL__", json.dumps(PAL))
        .replace("__USER_JSON__", json.dumps("alice"))
        .replace("__USER__", "alice")
        .replace("__TITLE__", "Test map")
        .replace("__SUBTITLE__", "eight items")
        .replace("__TODAY__", "2026-09-15 04:30 UTC")
    )
    assert not re.search(r"__[A-Z_]+__", html), "unfilled placeholder in template"
    out = tmp_path_factory.mktemp("map") / "map.html"
    out.write_text(html, encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def page_file(tmp_path_factory):
    return _map_file(tmp_path_factory)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _page(browser, page_file, width, scheme="light"):
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(page_file.as_uri(), wait_until="networkidle")
    page.wait_for_selector(".node")
    page._errors = errors
    return page


def test_every_visible_node_is_drawn_once(browser, page_file):
    """`only alice` is on by default: bob's item shows because alice engaged on it."""
    page = _page(browser, page_file, DESKTOP)
    assert page.evaluate("document.querySelectorAll('.node').length") == len(NODES)
    assert page._errors == []
    page.close()


@pytest.mark.parametrize("width", [DESKTOP, LAPTOP], ids=["desktop", "laptop"])
def test_lane_headers_do_not_run_into_each_other(browser, page_file, width):
    """Seven lanes at 1100px leave ~150px each; a long name has to wrap, not collide."""
    page = _page(browser, page_file, width)
    boxes = page.evaluate("""() => [...document.querySelectorAll('.lanehdr')]
        .map(t => { const r = t.getBoundingClientRect(); return [r.left, r.right]; })""")
    assert len(boxes) == len(LANES)
    for (l1, r1), (l2, r2) in zip(boxes, boxes[1:]):
        assert l2 >= r1 - 1, f"lane headers overlap at {width}px: {boxes}"
    page.close()


def test_all_lanes_fit_a_desktop_without_horizontal_scroll(browser, page_file):
    """The column width follows the viewport; a fixed 190px pushed lane seven off-screen."""
    page = _page(browser, page_file, DESKTOP)
    fits = page.evaluate("""() => { const c = document.getElementById('chart');
        return c.scrollWidth <= c.clientWidth + 1; }""")
    assert fits, "chart scrolls horizontally at desktop width"
    page.close()


def test_a_laptop_keeps_every_lane_reachable(browser, page_file):
    """Seven lanes beside the panel at 1100px cannot all fit at a readable width, so the
    chart scrolls sideways there; what must hold is that no lane is dropped and the
    last header is inside the scrollable width."""
    page = _page(browser, page_file, LAPTOP)
    assert page.evaluate("document.querySelectorAll('.lanehdr').length") == len(LANES)
    inside = page.evaluate("""() => { const c = document.getElementById('chart');
        const last = [...document.querySelectorAll('.lanehdr')].pop().getBoundingClientRect();
        return last.right - c.getBoundingClientRect().left <= c.scrollWidth + 1; }""")
    assert inside
    page.close()


def test_clicking_a_node_pins_it_in_the_panel(browser, page_file):
    """Labels are pointer-events:none, so the shape must take the click and open the panel
    with the item's references."""
    page = _page(browser, page_file, DESKTOP)
    page.evaluate("""() => { const g = [...document.querySelectorAll('.node')]
        .find(g => g.textContent.includes('#100')); g.dispatchEvent(new MouseEvent('click', {bubbles: true})); }""")
    panel = page.locator("#panel")
    assert "#100" in panel.locator("h2").text_content()
    assert "Start here" in panel.text_content()
    refs = panel.locator("ul a[data-id]").all_text_contents()
    assert set(refs) == {"#101", "#103"}, refs
    page.close()


def test_toggling_a_lane_removes_its_nodes_and_column(browser, page_file):
    page = _page(browser, page_file, DESKTOP)
    page.locator(".chip[data-lane='ci']").click()
    assert page.evaluate("document.querySelectorAll('.node').length") == len(NODES) - 2
    assert page.evaluate("document.querySelectorAll('.lanehdr').length") == len(LANES) - 1
    page.close()


def test_table_view_lists_the_same_items(browser, page_file):
    page = _page(browser, page_file, DESKTOP)
    page.locator(".chip[data-k='table']").click()
    rows = page.locator("#tableview tbody tr").count()
    assert rows == len(NODES)
    assert page.locator("#chart").is_hidden()
    page.close()


def test_people_filter_starts_on_the_user_and_widens_when_cleared(browser, page_file):
    """`alice` is preselected, so bob's item shows only because alice engaged on it.
    Clearing every person means everyone; picking bob alone leaves his one item."""
    page = _page(browser, page_file, DESKTOP)
    assert page.locator(".chip[data-person='alice']").get_attribute("class").endswith("on")
    assert page.evaluate("document.querySelectorAll('.node').length") == len(NODES)
    page.locator(".chip[data-person='alice']").click()
    assert page.evaluate("document.querySelectorAll('.node').length") == len(NODES)
    page.locator(".chip[data-person='bob']").click()
    assert page.evaluate("document.querySelectorAll('.node').length") == 1
    page.close()


def test_hiding_the_panel_hands_the_lanes_the_width(browser, page_file):
    page = _page(browser, page_file, LAPTOP)
    before = page.evaluate("document.getElementById('chart').clientWidth")
    page.locator(".chip[data-k='panel']").click()
    assert page.locator("#panel").is_hidden()
    after = page.evaluate("document.getElementById('chart').clientWidth")
    assert after > before + 200, (before, after)
    fits = page.evaluate("""() => { const c = document.getElementById('chart');
        return c.scrollWidth <= c.clientWidth + 1; }""")
    assert fits, "seven lanes should fit a 1100px laptop once the panel is hidden"
    page.close()


def test_a_lane_header_only_highlights_and_never_hides(browser, page_file):
    """Hover dims everything outside the lane; a click on the header changes nothing.
    Hiding a lane is the chip's job, so a stray click over the chart cannot lose a lane."""
    page = _page(browser, page_file, DESKTOP)
    page.evaluate("""() => document.querySelector(".hdrbtn[data-lane='ir']")
        .dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}))""")
    dimmed = page.evaluate("document.querySelectorAll('.node.dim').length")
    assert 0 < dimmed < len(NODES)
    page.evaluate("""() => document.querySelector(".hdrbtn[data-lane='ir']")
        .dispatchEvent(new MouseEvent('click', {bubbles: true}))""")
    assert page.evaluate("document.querySelectorAll('.lanehdr').length") == len(LANES)
    assert page.locator(".chip[data-lane='ir']").get_attribute("class").endswith("on")
    page.close()


def test_shift_click_keeps_only_that_chip_and_the_reset_chip_restores(browser, page_file):
    """Shift-click on any filter chip is exclusive; the reset chip at the head of the
    group ("all", or "everyone" for people) puts the group back to no restriction."""
    page = _page(browser, page_file, DESKTOP)
    page.locator(".chip[data-lane='ir']").click(modifiers=["Shift"])
    assert page.evaluate("document.querySelectorAll('.lanehdr').length") == 1
    assert not page.locator(".chip[data-lane='']").get_attribute("class").endswith("on")
    page.locator(".chip[data-lane='']").click()
    assert page.evaluate("document.querySelectorAll('.lanehdr').length") == len(LANES)
    assert page.locator(".chip[data-lane='']").get_attribute("class").endswith("on")
    # people: alice is preselected, so "everyone" starts off; shift-click bob is bob alone
    assert not page.locator(".chip[data-person='']").get_attribute("class").endswith("on")
    page.locator(".chip[data-person='bob']").click(modifiers=["Shift"])
    assert page.evaluate("document.querySelectorAll('.node').length") == 1
    assert not page.locator(".chip[data-person='alice']").get_attribute("class").endswith("on")
    page.locator(".chip[data-person='']").click()
    assert page.evaluate("document.querySelectorAll('.node').length") == len(NODES)
    assert page.locator(".chip[data-person='']").get_attribute("class").endswith("on")
    page.close()


def test_a_phone_never_scrolls_the_page_sideways(browser, page_file):
    """The chart scrolls inside itself; the body must not, or the header goes off-screen."""
    page = _page(browser, page_file, PHONE)
    body_overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert body_overflow <= 1, f"page scrolls {body_overflow}px sideways on a phone"
    assert page.evaluate("document.getElementById('chart').scrollWidth") > 390
    page.close()


def test_header_links_the_agent_readable_files(browser, page_file):
    page = _page(browser, page_file, DESKTOP)
    hrefs = page.evaluate(
        "[...document.querySelectorAll('.ident .data a')].map(a => a.getAttribute('href'))"
    )
    assert hrefs == ["data.json", "inventory.md"]
    assert "04:30 UTC" in page.locator(".ident .sub").text_content()
    page.close()


def test_dark_scheme_renders_without_errors(browser, page_file):
    page = _page(browser, page_file, DESKTOP, scheme="dark")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg != "rgb(245, 246, 244)", "dark scheme still paints the light ground"
    assert page._errors == []
    page.close()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
