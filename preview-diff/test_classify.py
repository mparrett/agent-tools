"""Tests for file classification and the implementation-lines headline.

`catof` is a stack of path rules and the header's added/removed counts changed
meaning — they headline implementation lines and disclose the total behind a `∗`.
Neither needs a browser: render a fixture with `--from-diff ... --no-open -o` and
read the emitted `file-cat` tags and `totals` block out of the HTML.

Kept apart from test_review_ui.py on purpose. That suite drives a live
`--review` server through Playwright; this one is a subprocess and a regex, and
shouldn't inherit a browser dependency to run.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "preview-diff"


def _diff(*paths):
    """A unified diff touching each path with one addition and one removal."""
    out = []
    for path in paths:
        out.append(f"diff --git a/{path} b/{path}")
        out.append(f"--- a/{path}")
        out.append(f"+++ b/{path}")
        out.append("@@ -1,2 +1,2 @@")
        out.append(" context")
        out.append("-old line")
        out.append("+new line")
    return "\n".join(out) + "\n"


def render(tmp_path, diff_text):
    """Run preview-diff over a diff and return the generated HTML."""
    src = tmp_path / "fixture.diff"
    src.write_text(diff_text, encoding="utf-8")
    out = tmp_path / "out.html"
    proc = subprocess.run(
        [str(SCRIPT), "--from-diff", str(src), "--no-open", "-o", str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8")


def categories(html):
    """Every category tag the file cards carry, in order.

    Implementation files are deliberately untagged, so this reports only the
    files that classified as something else.
    """
    return re.findall(r'<span class="file-cat cat-([a-z]+)">', html)


def totals(html):
    """(added, removed) as the header headlines them, and whether `∗ impl` shows."""
    block = re.search(r'<div class="totals">(.*?)</div>', html, re.S)
    assert block, "no totals block in the rendered page"
    body = block.group(1)
    add = re.search(r'<span class="add">\+(\d+)</span>', body)
    dele = re.search(r'<span class="del">-(\d+)</span>', body)
    assert add and dele, body
    return int(add.group(1)), int(dele.group(1)), "impl-star" in body


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/app.py", "implementation"),
        ("tests/test_app.py", "tests"),
        ("spec/thing_spec.rb", "tests"),
        ("docs/guide.md", "docs"),
        ("README.md", "docs"),
        ("LICENSE", "docs"),
        ("CHANGELOG.md", "docs"),
        ("uv.lock", "assets"),
        ("app.min.js", "assets"),
        ("logo.png", "assets"),
    ],
)
def test_catof_buckets_paths(tmp_path, path, expected):
    tags = categories(render(tmp_path, _diff(path)))
    got = tags[0] if tags else "implementation"
    assert got == expected, f"{path} classified as {got}"


@pytest.mark.parametrize("path", ["NOTICEBOARD.js", "AUTHORS_NOTE.py", "LICENSEKEY.go"])
def test_doc_name_rules_do_not_swallow_source_files(tmp_path, path):
    """The doc-name rule matched as a bare prefix, so these classified as docs.

    They then dropped out of the headline count — a real source change reported as
    zero implementation lines. The rule only has to catch the extensionless
    spellings; anything with an extension already matched the rule above it.
    """
    assert categories(render(tmp_path, _diff(path))) == []


def test_headline_counts_implementation_and_discloses_the_total(tmp_path):
    """The whole point of the split: code lines lead, everything else is behind `∗`."""
    html = render(tmp_path, _diff("src/app.py", "docs/guide.md", "tests/test_app.py"))
    add, dele, starred = totals(html)
    assert (add, dele) == (1, 1), "headline should count only src/app.py"
    assert starred, "a mixed diff must disclose that the headline is partial"


def test_an_all_implementation_diff_shows_no_marker(tmp_path):
    """Nothing is hidden, so there's nothing to disclose."""
    html = render(tmp_path, _diff("src/a.py", "src/b.py"))
    add, dele, starred = totals(html)
    assert (add, dele) == (2, 2)
    assert not starred


@pytest.mark.parametrize(
    "paths",
    [
        ("README.md",),
        ("docs/a.md", "docs/b.md"),
        ("tests/test_a.py", "tests/test_b.py"),
    ],
)
def test_a_diff_with_no_implementation_files_headlines_its_real_counts(tmp_path, paths):
    """The bug: impl_add/impl_del are both 0, so the header read `+0 −0`.

    A docs-only or tests-only diff has no implementation signal to headline, and
    `+0 −0` reads as an empty diff — the real counts were reachable only by
    hovering the `∗`, which doesn't exist on touch.
    """
    add, dele, _ = totals(render(tmp_path, _diff(*paths)))
    assert (add, dele) == (len(paths), len(paths))


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
