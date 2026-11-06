#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pytest", "mistune>=3.0.0"]
# ///
"""Characterization tests for mdserve.

Run via `uv run mdserve/test_mdserve.py` (or `just test`).

These pin down what mdserve *currently* does, not what it ideally would.
mdserve is where the incidental complexity pooled (found in an audit), and CLAUDE.md asks
for characterization before refactoring it. Where a test documents behavior
that looks like a bug, it says so and links the reasoning -- changing it is a
separate concern from being able to see it.
"""

import functools
import http.server
import json
import os
import re
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

import mdserve

# ── terse formatters ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "n,expected",
    [(0, "0"), (999, "999"), (1000, "1.0k"), (8403, "8.4k"), (1234567, "1234.6k")],
)
def test_compact_count(n, expected):
    # No 'M' bucket: a million words reads as "1234.6k".
    assert mdserve.compact_count(n) == expected


@pytest.mark.parametrize(
    "age_seconds,expected",
    [(0, "now"), (59, "now"), (60, "1m"), (3599, "59m"), (3600, "1h"), (86399, "23h")],
)
def test_compact_age_buckets(monkeypatch, age_seconds, expected):
    monkeypatch.setattr(mdserve.time, "time", lambda: 1_000_000.0)
    assert mdserve.compact_age(1_000_000.0 - age_seconds) == expected


def test_compact_age_of_falsy_timestamp_is_unknown():
    # copy/serve pass 0 when os.path.getmtime raised.
    assert mdserve.compact_age(0) == "?"


@pytest.mark.parametrize(
    "age_seconds,expected",
    [
        (30, "just now"),
        (60, "1 minute ago"),
        (120, "2 minutes ago"),
        (3600, "1 hour ago"),
        (86400, "1 day ago"),
        (2592000, "1 month ago"),
        (31536000, "1 year ago"),
    ],
)
def test_format_time_ago_pluralizes(monkeypatch, age_seconds, expected):
    monkeypatch.setattr(mdserve.time, "time", lambda: 1_000_000_000.0)
    assert mdserve.format_time_ago(1_000_000_000.0 - age_seconds) == expected


# ── image references ─────────────────────────────────────────────────────────


def test_extract_image_refs_finds_markdown_and_html():
    content = '![alt](pics/a.png)\n<img src="b.jpg" width="10">\n'
    assert mdserve.extract_image_refs(content) == ["pics/a.png", "b.jpg"]


@pytest.mark.parametrize(
    "ref", ["http://x.com/a.png", "https://x.com/a.png", "data:image/png;base64,AAA", "/abs.png"]
)
def test_extract_image_refs_skips_non_relative(ref):
    # Only relative refs are copied into the output tree; the rest resolve as-is.
    assert mdserve.extract_image_refs(f"![alt]({ref})") == []


# ── escaping ─────────────────────────────────────────────────────────────────


def test_breadcrumb_of_nested_path_shows_directory():
    html = mdserve.create_breadcrumb_nav("docs/guide/intro.md", "intro.md")
    assert "docs/guide" in html
    assert "<strong>intro.md</strong>" in html


def test_breadcrumb_of_top_level_file_uses_title():
    html = mdserve.create_breadcrumb_nav("intro.md", "My Title")
    assert "<strong>My Title</strong>" in html


def test_breadcrumb_of_empty_path_is_empty():
    assert mdserve.create_breadcrumb_nav("", "t") == ""


# ── file discovery ───────────────────────────────────────────────────────────


def write(root: Path, rel: str, text: str = "x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_find_collects_supported_extensions_only(tmp_path):
    for name in ["a.md", "b.markdown", "c.py", "d.js", "e.ts", "skip.txt", "skip.json"]:
        write(tmp_path, name)
    found = [rel for _, rel in mdserve.find_markdown_files(str(tmp_path))]
    assert found == ["a.md", "b.markdown", "c.py", "d.js", "e.ts"]


def test_find_matches_extensions_case_insensitively(tmp_path):
    write(tmp_path, "README.MD")
    assert [rel for _, rel in mdserve.find_markdown_files(str(tmp_path))] == ["README.MD"]


def test_find_sorts_case_insensitively_by_relative_path(tmp_path):
    # Names chosen so a case-*sensitive* sort gives a different answer:
    # byte order would put every capital first, as ["Beta.md", "Zeta.md", "alpha.md"].
    for name in ["Zeta.md", "alpha.md", "Beta.md"]:
        write(tmp_path, name)
    found = [rel for _, rel in mdserve.find_markdown_files(str(tmp_path))]
    assert found == ["alpha.md", "Beta.md", "Zeta.md"]


def test_find_descends_into_subdirectories(tmp_path):
    write(tmp_path, "sub/deep/note.md")
    found = [rel for _, rel in mdserve.find_markdown_files(str(tmp_path))]
    assert found == [os.path.join("sub", "deep", "note.md")]


@pytest.mark.parametrize("junk", ["node_modules", ".git", "__pycache__", "dist", "venv"])
def test_find_skips_default_ignored_directories(tmp_path, junk):
    write(tmp_path, "keep.md")
    write(tmp_path, f"{junk}/dep.md")
    found = [rel for _, rel in mdserve.find_markdown_files(str(tmp_path))]
    assert found == ["keep.md"]


def test_find_include_mode_restricts_to_matching_paths(tmp_path):
    # Include mode is a separate branch from the default walk, with its own
    # extension test -- exercise it, or half of discovery goes unwatched.
    write(tmp_path, "guide/intro.md")
    write(tmp_path, "other/misc.md")
    found = [
        rel for _, rel in mdserve.find_markdown_files(str(tmp_path), include_patterns=["guide"])
    ]
    assert found == [os.path.join("guide", "intro.md")]


def test_find_include_mode_sorts_case_insensitively(tmp_path):
    # Include mode returns from its own `sorted(...)` call, so the default
    # walk's ordering test does not cover it.
    for name in ["Zeta.md", "alpha.md", "Beta.md"]:
        write(tmp_path, f"guide/{name}")
    found = [
        rel for _, rel in mdserve.find_markdown_files(str(tmp_path), include_patterns=["guide"])
    ]
    assert [os.path.basename(r) for r in found] == ["alpha.md", "Beta.md", "Zeta.md"]


def test_find_include_mode_matches_extensions_case_insensitively(tmp_path):
    write(tmp_path, "guide/READ.MD")
    found = [
        rel for _, rel in mdserve.find_markdown_files(str(tmp_path), include_patterns=["guide"])
    ]
    assert found == [os.path.join("guide", "READ.MD")]


def test_find_honours_exclude_patterns(tmp_path):
    write(tmp_path, "keep.md")
    write(tmp_path, "drafts/wip.md")
    found = [
        rel for _, rel in mdserve.find_markdown_files(str(tmp_path), exclude_patterns=["drafts"])
    ]
    assert found == ["keep.md"]


@pytest.mark.parametrize("name", ["tmpfile.md", "distributed.md", "venv-notes.md"])
def test_find_drops_files_whose_name_merely_contains_an_ignored_word(tmp_path, name):
    """BUG, pinned deliberately: ignore matching is a substring test.

    The final filter asks `part in rel_path` for each non-`*` segment of every
    ignore pattern, so `*/tmp/*` drops `tmpfile.md` and `*/dist/*` drops
    `distributed.md` -- files the user asked to publish silently vanish, with
    no warning and no way to override. Fixing it means matching path
    *segments* rather than substrings; that changes behavior, so it belongs in
    its own change. This test exists so the fix has something to flip.
    """
    write(tmp_path, name)
    assert [rel for _, rel in mdserve.find_markdown_files(str(tmp_path))] == []


# ── conversion ───────────────────────────────────────────────────────────────


def test_convert_files_writes_index_and_keeps_original_extension(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "note.md", "# Hi\n")
    write(src, "note.py", "print(1)\n")
    out.mkdir()
    files = mdserve.find_markdown_files(str(src))
    mdserve.convert_files(files, str(src), str(out))
    # Extension is preserved so note.md and note.py cannot collide.
    assert sorted(p.name for p in out.iterdir()) == ["index.html", "note.md.html", "note.py.html"]


def test_convert_files_renders_markdown_and_highlights_code(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "a.md", "# Heading\n")
    write(src, "a.py", "print(1)\n")
    write(src, "a.js", "let x = 1\n")
    write(src, "a.ts", "let y: number = 1\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    assert "<h1>Heading</h1>" in (out / "a.md.html").read_text()
    for name, lang in [("a.py", "python"), ("a.js", "javascript"), ("a.ts", "typescript")]:
        assert f'class="language-{lang}"' in (out / f"{name}.html").read_text()


def test_convert_files_escapes_html_in_the_rendered_code_block(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "x.py", "tag = '<script>alert(1)</script>'\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    html = (out / "x.py.html").read_text()
    block = html[html.index("<pre><code") : html.index("</code></pre>")]
    assert "&lt;script&gt;" in block
    assert "<script>" not in block


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        '<a href="javascript:alert(1)">x</a>',
    ],
)
def test_markdown_html_is_passed_through_unsanitized(tmp_path, payload):
    """BUG, pinned deliberately: markdown's raw HTML reaches the page as markup.

    The renderer is built with `escape=False`, so inline HTML in a document is
    emitted verbatim, and nothing sanitizes it afterwards. A `<script>` in a
    markdown file becomes a real script element in the generated page; so do
    `onerror=` handlers and `javascript:` hrefs.

    The markup still reaches the page -- that half is unchanged, and this test
    pins it. What changed is what it can do: pages now carry a CSP whose
    `script-src` is a per-run nonce, so an injected `<script>` and an `onerror=`
    handler are both refused at execution. Verified in a browser against the
    payloads below.

    This is the *primary* injection path, and it matters because converting a
    directory of files you did not write -- vendored docs, a dependency's
    README, a branch under review -- is mdserve's whole job.

    Whether markdown should carry HTML at all remains a product decision
    (`escape=False` is what lets people embed diagrams and media on purpose), so
    escaping or sanitizing is still its own change. The CSP means the answer is
    no longer urgent.
    """
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", f"# Title\n\n{payload}\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    html = (out / "doc.md.html").read_text()
    body = html[: html.index("const RAW_CONTENT")]
    assert payload in body


def test_generated_pages_carry_a_content_security_policy(tmp_path):
    """The other half of the finding above, now closed.

    The policy ships in the page as a meta tag as well as in the response
    header, because `--convert-only` and `--no-cleanup` leave HTML that is
    opened over file:// with no server to set a header.
    """
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", "# Title\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    head = (out / "doc.md.html").read_text()
    assert 'http-equiv="Content-Security-Policy"' in head
    assert "default-src 'none'" in head
    assert f"script-src 'nonce-{mdserve.SCRIPT_NONCE}'" in head


def test_the_policy_admits_media_but_not_frames():
    """The embed split, which is a decision rather than an oversight.

    A <video> is content and carries no script, so `media-src` is open like
    `img-src`. An <iframe> is not: framing an arbitrary https origin inside a
    page the reader trusts is a phishing surface, and mdserve renders files you
    did not write. `frame-src` is therefore left to fall through to
    `default-src 'none'`, and this test fails if someone adds it.
    """
    assert "media-src *" in mdserve.CSP
    assert "frame-src" not in mdserve.CSP
    assert "child-src" not in mdserve.CSP
    assert "default-src 'none'" in mdserve.CSP


@pytest.mark.parametrize("page", ["index.html", "doc.md.html"])
def test_every_script_tag_in_a_generated_page_carries_the_nonce(tmp_path, page):
    """A nonce is only worth anything if nothing is left outside it.

    This is the failure a hash-based `script-src` invites: add an inline script,
    forget to add its hash, and the page breaks in a browser while every
    file-level assertion still passes. Here a missed tag fails the suite.
    """
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", "# Title\n\n```python\nx = 1\n```\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    html = (out / page).read_text()
    tags = re.findall(r"<script\b[^>]*>", html)
    assert tags, "expected the page to carry script tags"
    unnonced = [t for t in tags if f'nonce="{mdserve.SCRIPT_NONCE}"' not in t]
    assert not unnonced, f"script tags without the nonce: {unnonced}"


def test_served_responses_carry_the_policy_header(tmp_path):
    """The header half, over a real socket.

    `--convert-only` output is covered by the meta tag alone; a served
    directory gets both, and the header also reaches the non-HTML responses
    sitting alongside the pages.
    """
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", "# Title\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))

    handler = functools.partial(mdserve.CSPRequestHandler, directory=str(out))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/doc.md.html"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                policy = resp.headers["Content-Security-Policy"]
                nosniff = resp.headers["X-Content-Type-Options"]
        finally:
            httpd.shutdown()

    assert policy == mdserve.CSP
    assert f"script-src 'nonce-{mdserve.SCRIPT_NONCE}'" in policy
    assert nosniff == "nosniff"


def test_a_script_close_in_a_file_no_longer_truncates_the_page(tmp_path):
    """Was a pinned defect; now pins the fix.

    Every generated page carries the file's raw text so the source view and
    copy-to-clipboard have something to read. It used to ride in a JS template
    literal escaped by hand, and an HTML parser ends a script element at the
    first literal `</script>` whatever the JS string context -- so a document
    containing that sequence truncated the page's own script and lost its
    navigation and source view. The nonce-based CSP stopped what broke out
    from running, but could not un-truncate the script.

    The data now rides in a `<script type="application/json">` island with
    `&<>` escaped, so the parser sees nothing to act on and the text survives
    byte for byte.
    """
    payload = "</script><img src=x onerror=alert(1)>"
    body = f"# doc\n\n{payload}\n"
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "evil.md", body)
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    html = (out / "evil.md.html").read_text()

    island = re.search(
        r'<script type="application/json"[^>]*id="page-data">(.*?)</script>', html, re.S
    )
    assert island, "page carries no data island"
    assert json.loads(island.group(1))["raw"] == body

    # The only `</script>` left in the island region is the island's own close.
    assert payload not in island.group(1)


def test_the_json_island_neutralizes_a_script_close():
    # The unit-level face of the fix above: nothing the HTML parser can act on
    # survives, and JSON.parse still reads the original text back.
    island = mdserve.json_island("d", {"raw": "</script><img src=x onerror=alert(1)>"})
    assert "</script><img" not in island
    assert "\\u003c" in island
    body = island[island.index(">") + 1 : island.rindex("</script>")]
    assert json.loads(body)["raw"] == "</script><img src=x onerror=alert(1)>"


def test_convert_files_preserves_subdirectory_layout(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "guide/intro.md", "# Intro\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    assert (out / "guide" / "intro.md.html").is_file()


def test_convert_single_file_writes_sibling_html(tmp_path):
    doc = write(tmp_path, "solo.md", "# Solo\n")
    mdserve.convert_single_file(str(doc))
    assert "<h1>Solo</h1>" in (tmp_path / "solo.md.html").read_text()


def test_convert_single_file_refuses_to_clobber_existing_output(tmp_path):
    doc = write(tmp_path, "solo.md", "# Solo\n")
    write(tmp_path, "solo.md.html", "PRECIOUS")
    with pytest.raises(SystemExit):
        mdserve.convert_single_file(str(doc))
    # Fail-fast means the existing file is left exactly as it was.
    assert (tmp_path / "solo.md.html").read_text() == "PRECIOUS"


# ── frontmatter ──────────────────────────────────────────────────────────────


def _render_frontmatter_source(path: Path) -> str:
    """`render_frontmatter`'s text in `path`, from its `def` to the next one."""
    src = path.read_text()
    start = src.index("def render_frontmatter(")
    return src[start : src.index("\n\n\ndef ", start)]


def test_frontmatter_is_split_off_as_metadata():
    fm_html, body = mdserve.render_frontmatter("---\ntitle: My Page\n---\n\n# Body\n")
    assert "<details" in fm_html and "title: My Page" in fm_html
    assert body == "\n# Body\n"


@pytest.mark.parametrize("text", ["---\n---\n\n# Body\n", "---\n\n---\n\n# Body\n"])
def test_empty_frontmatter_is_consumed_but_renders_nothing(text):
    # A generated page can open with an empty block to opt into a pipeline
    # without saying anything. There is no metadata to show -- an empty
    # <details> labelled "metadata" would be worse than nothing -- but the block
    # still has to go, or both fences reach mistune as thematic breaks.
    assert mdserve.render_frontmatter(text) == ("", "\n# Body\n")


def test_converted_page_shows_frontmatter_as_metadata_not_as_a_heading(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", "---\ntitle: Meta\n---\n\n# Real Title\n")
    write(src, "empty.md", "---\n---\n\n# Real Title\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))

    html = (out / "doc.md.html").read_text()
    body = html[: html.index("const RAW_CONTENT")]
    assert "<h2>title: Meta</h2>" not in body
    assert "<details" in body and "<h1>Real Title</h1>" in body

    html = (out / "empty.md.html").read_text()
    body = html[: html.index("const RAW_CONTENT")]
    assert "<hr" not in body
    assert "<h1>Real Title</h1>" in body


def test_convert_single_file_shows_frontmatter_as_metadata(tmp_path):
    # The tool's second render site, and the one easiest to miss.
    doc = write(tmp_path, "solo.md", "---\ntitle: Meta\n---\n\n# Solo\n")
    mdserve.convert_single_file(str(doc))
    html = (tmp_path / "solo.md.html").read_text()
    body = html[: html.index("const RAW_CONTENT")]
    assert "<h2>title: Meta</h2>" not in body
    assert "<details" in body and "<h1>Solo</h1>" in body


def test_render_frontmatter_is_identical_to_mdfeeds_copy():
    """The two copies have to stay identical, and nothing else enforces it.

    Each tool is a standalone uv script with no importable common module, so the
    helper is duplicated rather than shared -- the trade-off `_WRAPPER_TAGS`
    makes in claude-ls, which is guarded the same way. Fixing one copy and not
    the other is invisible otherwise: both tools keep working, and only one
    renders frontmatter correctly.
    """
    mdfeed_py = Path(__file__).resolve().parent.parent / "mdfeed" / "mdfeed.py"
    assert _render_frontmatter_source(Path(mdserve.__file__)) == _render_frontmatter_source(
        mdfeed_py
    )


# ── intra-site links ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "href,expected",
    [
        ("b.md", "b.md.html"),
        ("b.md#sec", "b.md.html#sec"),
        ("d/c.markdown", "d/c.markdown.html"),
        ("../up.md", "../up.md.html"),
        # Case-insensitive to mirror the collector, which picks README.MD up
        # with `.lower().endswith()` and writes it out as README.MD.html.
        ("README.MD", "README.MD.html"),
    ],
)
def test_relative_markdown_links_point_at_the_rendered_page(href, expected):
    assert (
        mdserve.rewrite_markdown_links(f'<a href="{href}">x</a>') == f'<a href="{expected}">x</a>'
    )


@pytest.mark.parametrize(
    "href",
    [
        "https://x.com/z.md",
        "HTTPS://X.COM/Z.MD",
        "mailto:docs@example.md",
        "ftp://h/a.md",
        "file:///docs/a.md",
        "//cdn.example.com/a.md",
        "/root.md",
        "#local",
        "pic.png",
        "notes.mdx",
    ],
)
def test_links_that_are_not_ours_to_redirect_are_left_alone(href):
    # The exclusion is by scheme rather than by protocol: `.md` is a real ccTLD,
    # so a mailto: or ftp: href ending in `.md` is somebody's address or file,
    # not a page this conversion wrote.
    html = f'<a href="{href}">x</a>'
    assert mdserve.rewrite_markdown_links(html) == html


def test_link_title_survives_the_rewrite():
    html = '<a href="b.md" title="T">x</a>'
    assert mdserve.rewrite_markdown_links(html) == '<a href="b.md.html" title="T">x</a>'


def test_cross_references_resolve_to_pages_that_exist_on_disk(tmp_path):
    # The claim worth pinning end to end: the href a page emits names a file the
    # conversion actually wrote, not the source path the link was authored as.
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "a.md", "[b](b.md), [c](d/c.markdown#sec), [r](README.MD)\n")
    write(src, "b.md", "# B\n")
    write(src, "d/c.markdown", "# C\n")
    write(src, "README.MD", "# R\n")
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    page = (out / "a.md.html").read_text()
    body = page[: page.index("const RAW_CONTENT")]
    for href, on_disk in [
        ("b.md.html", "b.md.html"),
        ("d/c.markdown.html#sec", "d/c.markdown.html"),
        ("README.MD.html", "README.MD.html"),
    ]:
        assert f'href="{href}"' in body
        assert (out / on_disk).is_file()


def test_a_link_shown_inside_a_code_block_is_not_rewritten(tmp_path):
    # What keeps the rewrite off displayed markup is mistune escaping the quotes
    # inside a code block, which the pattern needs literally. A document showing
    # `<a href="notes.md">` as an example keeps saying `notes.md`.
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", '```html\n<a href="notes.md">x</a>\n```\n')
    out.mkdir()
    mdserve.convert_files(mdserve.find_markdown_files(str(src)), str(src), str(out))
    page = (out / "doc.md.html").read_text()
    body = page[: page.index("const RAW_CONTENT")]
    assert "&quot;notes.md&quot;" in body
    assert "notes.md.html" not in body


def test_convert_single_file_leaves_links_as_authored(tmp_path):
    """Deliberate: single-file conversion has no sibling pages to link to.

    It writes `solo.md.html` beside `solo.md` and converts nothing else, so
    rewriting `[b](b.md)` to `b.md.html` would point at a page nobody wrote,
    while the `b.md` the link was authored as is sitting right there.
    """
    doc = write(tmp_path, "solo.md", "[b](b.md)\n")
    write(tmp_path, "b.md", "# B\n")
    mdserve.convert_single_file(str(doc))
    page = (tmp_path / "solo.md.html").read_text()
    body = page[: page.index("const RAW_CONTENT")]
    assert 'href="b.md"' in body


# ── image copying ────────────────────────────────────────────────────────────


def test_copy_relative_images_copies_referenced_files(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", "![a](img/a.png)\n")
    write(src, "img/a.png", "PNGDATA")
    out.mkdir()
    files = [(str(src / "doc.md"), "doc.md")]
    found = mdserve.copy_relative_images(files, str(src), str(out))
    assert "img/a.png" in found
    assert (out / "img" / "a.png").read_text() == "PNGDATA"


def test_copy_relative_images_dry_run_copies_nothing(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write(src, "doc.md", "![a](img/a.png)\n")
    write(src, "img/a.png", "PNGDATA")
    out.mkdir()
    files = [(str(src / "doc.md"), "doc.md")]
    found = mdserve.copy_relative_images(files, str(src), str(out), dry_run=True)
    assert "img/a.png" in found
    assert not (out / "img").exists()


# ── argument parsing ─────────────────────────────────────────────────────────


def test_parse_flexible_args_accepts_directory_then_port(tmp_path):
    args = mdserve.parse_flexible_args([str(tmp_path), "9001"])
    assert args.directory == str(tmp_path)
    assert args.port == 9001


def test_parse_flexible_args_rejects_port_first_ordering(monkeypatch, tmp_path):
    """BUG, pinned deliberately: the advertised flexible ordering cannot work.

    `parse_flexible_args` documents "flexible order support" and carries a
    block that swaps `directory` and `port` when they arrive reversed. That
    block is unreachable: `port` is declared `type=int`, so argparse rejects
    `mdserve 9001 docs` while parsing and exits 2 long before the swap is
    consulted. The feature has never worked, and the dead code hides that.

    Making it work means accepting `port` as a string and coercing after the
    swap -- a behavior change, so not made here.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    with pytest.raises(SystemExit) as exc:
        mdserve.parse_flexible_args(["9001", "docs"])
    assert exc.value.code == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
