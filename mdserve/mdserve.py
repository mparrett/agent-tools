#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "mistune>=3.0.0",
# ]
# ///
"""
Pure Python markdown server with arrow key navigation.

A clean, modern implementation of the markdown serve tool that converts
markdown files to HTML and serves them with navigation features.
"""

import argparse
import base64
import fnmatch
import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from typing import Any, Dict, List, Optional, Set, Tuple


def render_frontmatter(text: str) -> Tuple[str, str]:
    """Split a leading `---` frontmatter block off `text`.

    Returns `(metadata_html, body)`, with an empty string for documents that
    have no frontmatter.

    mistune has no frontmatter concept: the opening `---` reads as a thematic
    break and the closing one as a setext-heading underline, so an unstripped
    block renders as a stray <hr> above the raw YAML dumped into an <h2> --
    machine metadata styled as the document's headline. Emit it as a collapsed
    <details> instead, so it stays available without competing with the real
    title.

    Only a block at the very start counts, and only a terminated one: a `---`
    further down is a thematic break, and a stray opening fence must not
    swallow the rest of the file.

    An empty block -- closed on the very next line, or holding nothing but
    blank ones -- is consumed and renders nothing. Generated pages use one to
    opt into a pipeline without saying anything, so there is no metadata to
    show; leaving it in the body would put both fences back in front of
    mistune as a pair of thematic breaks.
    """
    match = re.match(r"^---[ \t]*\n(.*?)^---[ \t]*(?:\n|$)", text, re.DOTALL | re.MULTILINE)
    if not match:
        return "", text
    body = text[match.end() :]
    lines = [
        line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for line in match.group(1).splitlines()
    ]
    if not any(line.strip() for line in lines):
        return "", body
    return (
        '<details class="frontmatter" style="opacity:.65;font-size:.85em;'
        'margin-bottom:1em"><summary>metadata</summary><pre>'
        + "\n".join(lines)
        + "</pre></details>",
        body,
    )


def rewrite_markdown_links(html: str) -> str:
    """Point relative `.md` hrefs at the `.md.html` pages the conversion writes.

    `convert_files` keeps the original extension, so `notes.md` is written as
    `notes.md.html` and a `[see](notes.md)` authored between two documents names
    a path the generated site does not contain. Every cross-reference in a
    multi-file conversion 404s without this.

    Only a relative href is ours to redirect. One carrying a scheme is not --
    `https://`, but `mailto:`, `ftp:` and `file:` just as much, and `.md` is a
    real ccTLD, so the exclusion is by scheme rather than by protocol. Neither
    is a root-relative path or a bare fragment. A trailing `#anchor` rides
    across.

    Matching is case-insensitive to mirror the collector, not to assume anything
    about the filesystem: `find_markdown_files` picks files up with
    `.lower().endswith()`, so `README.MD` is converted, and the output name is
    the path plus `.html`, so `README.MD.html` is what lands on disk. The path
    is echoed verbatim rather than normalized, so the rewrite cannot make a link
    resolve differently on a case-sensitive filesystem than a case-insensitive
    one.
    """
    return re.sub(
        r'href="(?![a-z][a-z0-9+.\-]*:|#|/)([^"]+?\.(?:md|markdown))(#[^"]*)?"',
        lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"',
        html,
        flags=re.IGNORECASE,
    )


def format_time_ago(timestamp: float) -> str:
    """Format a timestamp as a human-readable 'time ago' string."""
    now = time.time()
    diff = now - timestamp

    if diff < 60:
        return "just now"
    elif diff < 3600:  # Less than an hour
        minutes = int(diff / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif diff < 86400:  # Less than a day
        hours = int(diff / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif diff < 2592000:  # Less than 30 days
        days = int(diff / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    elif diff < 31536000:  # Less than a year
        months = int(diff / 2592000)
        return f"{months} month{'s' if months != 1 else ''} ago"
    else:
        years = int(diff / 31536000)
        return f"{years} year{'s' if years != 1 else ''} ago"


def compact_age(timestamp: float) -> str:
    """Terse age for the file manifest: now, 5m, 3h, 2d, 4mo, 1y."""
    if not timestamp:
        return "?"
    diff = time.time() - timestamp
    if diff < 60:
        return "now"
    # (cap, seconds-per-unit, label) — first bucket the age falls under wins.
    for cap, unit_seconds, label in (
        (3600, 60, "m"),
        (86400, 3600, "h"),
        (2592000, 86400, "d"),
        (31536000, 2592000, "mo"),
    ):
        if diff < cap:
            return f"{int(diff / unit_seconds)}{label}"
    return f"{int(diff / 31536000)}y"


def compact_count(n: int) -> str:
    """Abbreviate a count for dense metadata: 545 -> 545, 8403 -> 8.4k."""
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def extract_image_refs(content: str) -> List[str]:
    """Extract relative image paths from markdown content."""
    md_refs = re.findall(r"!\[.*?\]\(([^)]+)\)", content)
    html_refs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', content)
    return [
        r for r in md_refs + html_refs if not r.startswith(("http://", "https://", "data:", "/"))
    ]


def copy_relative_images(
    files: List[Tuple[str, str]],
    base_dir: str,
    output_dir: str,
    verbose: bool = False,
    dry_run: bool = False,
) -> Set[str]:
    """
    Copy images referenced in markdown files to output directory.

    Returns set of relative image paths found (for warning when skipped).
    If dry_run=True, only returns found refs without copying.
    """
    found_refs: Set[str] = set()
    copied: Set[str] = set()

    for full_path, rel_path in files:
        if not rel_path.lower().endswith((".md", ".markdown")):
            continue
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (IOError, UnicodeDecodeError):
            continue

        for img_ref in extract_image_refs(content):
            img_src = os.path.normpath(os.path.join(os.path.dirname(full_path), img_ref))
            img_rel = os.path.relpath(img_src, base_dir)
            found_refs.add(img_rel)

            if dry_run or img_src in copied:
                continue

            if os.path.isfile(img_src):
                img_dst = os.path.join(output_dir, img_rel)
                os.makedirs(os.path.dirname(img_dst), exist_ok=True)
                shutil.copy2(img_src, img_dst)
                copied.add(img_src)
                if verbose:
                    print(f"📷 Copied: {img_rel}")
            elif verbose:
                print(f"⚠️  Image not found: {img_rel}")

    return found_refs


def export_md_inline(input_path: str) -> None:
    """
    Export markdown with images inlined as base64 data URIs.

    Uses reference-style links (Google Docs compatible):
      ![alt][image1]
      [image1]: <data:image/png;base64,...>
    """
    if not os.path.isfile(input_path):
        print(f"❌ Error: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if not input_path.lower().endswith((".md", ".markdown")):
        print(f"❌ Error: Expected markdown file, got: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read()

    base_dir = os.path.dirname(os.path.abspath(input_path))
    image_refs: Dict[str, str] = {}  # path -> reference name
    image_data: List[Tuple[str, str]] = []  # (reference name, data URI)
    counter = 1

    def replace_image(match: re.Match) -> str:
        nonlocal counter
        alt_text = match.group(1)
        img_path = match.group(2)

        # Skip non-relative paths
        if img_path.startswith(("http://", "https://", "data:", "/")):
            return match.group(0)

        # Resolve full path
        full_path = os.path.normpath(os.path.join(base_dir, img_path))

        if not os.path.isfile(full_path):
            print(f"⚠️  Image not found: {img_path}", file=sys.stderr)
            return match.group(0)

        # Reuse reference if same image
        if img_path in image_refs:
            ref_name = image_refs[img_path]
        else:
            ref_name = f"image{counter}"
            counter += 1
            image_refs[img_path] = ref_name

            # Read and encode image
            mime_type, _ = mimetypes.guess_type(full_path)
            if not mime_type:
                mime_type = "application/octet-stream"

            with open(full_path, "rb") as img_file:
                encoded = base64.b64encode(img_file.read()).decode("ascii")

            data_uri = f"data:{mime_type};base64,{encoded}"
            image_data.append((ref_name, data_uri))

        # Return reference-style link
        if alt_text:
            return f"![{alt_text}][{ref_name}]"
        else:
            return f"![][{ref_name}]"

    # Replace all image references
    pattern = r"!\[(.*?)\]\(([^)]+)\)"
    result = re.sub(pattern, replace_image, content)

    # Append reference definitions
    if image_data:
        result = result.rstrip() + "\n\n"
        for ref_name, data_uri in image_data:
            result += f"[{ref_name}]: <{data_uri}>\n"

    print(result)


def find_markdown_files(
    directory: str,
    exclude_patterns: Optional[List[str]] = None,
    include_patterns: Optional[List[str]] = None,
) -> List[Tuple[str, str]]:
    """
    Find all markdown files in directory with include/exclude filtering.

    Args:
        directory: Directory to search
        exclude_patterns: Patterns to exclude (blocklist mode)
        include_patterns: Patterns to include (allowlist mode)

    Returns:
        List of (full_path, relative_path) tuples
    """
    md_files = []

    # Default ignore patterns (always applied unless in include mode)
    default_ignore_patterns = [
        "*/node_modules/*",
        "*/.git/*",
        "*/.venv/*",
        "*/.env/*",
        "*/venv/*",
        "*/__pycache__/*",
        "*/.next/*",
        "*/dist/*",
        "*/.cache/*",
        "*/.tmp/*",
        "*/tmp/*",
    ]

    # Include mode: only include matching patterns
    if include_patterns:
        include_glob_patterns = []
        for pattern in include_patterns:
            # Convert simple patterns to glob-like patterns if needed
            if "*" not in pattern:
                include_glob_patterns.extend(
                    [
                        f"*/{pattern}/*",
                        f"{pattern}/*",
                        f"*{pattern}*",
                        f"{pattern}",
                        f"*/{pattern}",
                        f"{pattern}.md",
                        f"*{pattern}.md",
                    ]
                )
            else:
                include_glob_patterns.append(pattern)

        # In include mode, start with empty list and only add matches
        for root, dirs, files in os.walk(directory):
            # Skip default ignored directories
            dirs[:] = [
                d
                for d in dirs
                if not any(d in pattern.split("/") for pattern in default_ignore_patterns)
            ]

            for file in files:
                if file.lower().endswith((".md", ".markdown", ".py", ".js", ".ts")):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, directory)

                    # Check if matches any include pattern
                    include_match = False
                    for pattern in include_glob_patterns:
                        if (
                            fnmatch.fnmatch(rel_path, pattern)
                            or fnmatch.fnmatch(os.path.basename(rel_path), pattern)
                            or pattern in rel_path
                        ):
                            include_match = True
                            break

                    if include_match:
                        md_files.append((full_path, rel_path))

        return sorted(md_files, key=lambda x: x[1].lower())

    # Exclude mode (default): exclude matching patterns
    ignore_patterns = default_ignore_patterns.copy()

    # Add custom exclusion patterns
    if exclude_patterns:
        for pattern in exclude_patterns:
            # Convert simple patterns to glob-like patterns if needed
            if "*" not in pattern:
                ignore_patterns.extend([f"*/{pattern}/*", f"{pattern}/*", f"*{pattern}*"])
            else:
                ignore_patterns.append(pattern)

    for root, dirs, files in os.walk(directory):
        # Skip ignored directories
        dirs[:] = [
            d for d in dirs if not any(d in pattern.split("/") for pattern in ignore_patterns)
        ]

        for file in files:
            if file.lower().endswith((".md", ".markdown", ".py", ".js", ".ts")):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, directory)

                # Skip if matches ignore patterns
                skip = False
                for pattern in ignore_patterns:
                    if any(part in rel_path for part in pattern.split("/") if part != "*"):
                        skip = True
                        break

                if not skip:
                    md_files.append((full_path, rel_path))

    return sorted(md_files, key=lambda x: x[1].lower())


def json_island(element_id: str, payload: Any) -> str:
    r"""Serialize per-file data into a non-executable JSON script block.

    Interpolating a file's own text into executable JS is what made a document
    containing `</script>` truncate the page's script and lose its navigation
    and source view: an HTML parser ends a script element at the first literal
    `</script>` whatever the JS string context, so no amount of escaping for
    *JavaScript* can help. Escaping for the *HTML parser* is the fix, and a data
    block is the place to do it -- `json.dumps` handles every other hazard
    (quotes, backslashes, newlines, control characters) that hand-built JSON
    kept getting wrong one at a time.

    `&<>` become `\uXXXX`, which `JSON.parse` reads back unchanged while leaving
    the parser nothing to act on. preview-diff's `review_server.bake` escapes the
    same three characters for the same reason.
    """
    blob = json.dumps(payload)
    for ch in ("&", "<", ">"):
        blob = blob.replace(ch, "\\u%04x" % ord(ch))
    return (
        f'<script type="application/json" nonce="{SCRIPT_NONCE}" id="{element_id}">{blob}</script>'
    )


def create_breadcrumb_nav(rel_path: str, title: str) -> str:
    """Create breadcrumb navigation HTML."""
    if not rel_path:
        return ""

    parts = rel_path.split("/")
    if len(parts) > 1:
        dir_path = "/".join(parts[:-1])
        return f"""
        <div class="breadcrumb">
            <a href="/">index</a> <span class="sep">/</span> <span>{dir_path}</span> <span class="sep">/</span> <strong>{parts[-1]}</strong>
        </div>"""
    else:
        return f"""
        <div class="breadcrumb">
            <a href="/">index</a> <span class="sep">/</span> <strong>{title}</strong>
        </div>"""


def create_navigation_data(
    nav_info: Optional[Dict[str, Any]], title: str
) -> Optional[Dict[str, Any]]:
    """Build the page's navigation object, or None when there is no neighbour."""
    if not nav_info:
        return None

    def neighbour(kind: str) -> Optional[Dict[str, str]]:
        path = nav_info.get(f"{kind}_path")
        if not path:
            return None
        return {"path": path, "title": nav_info.get(f"{kind}_title", "")}

    return {
        "current": {
            "path": nav_info.get("current_path", ""),
            "title": nav_info.get("current_title", title),
        },
        "prev": neighbour("prev"),
        "next": neighbour("next"),
        "index": nav_info.get("current_index", 0),
        "total": nav_info.get("total_files", 1),
    }


# --- Shared light/dark theme -------------------------------------------------
# These are plain (non-f-string) constants injected into each template via
# placeholders, so the CSS/JS braces here stay single — no f-string doubling.
# Colors are semantic CSS variables; the toggle cycles auto -> light -> dark and
# stores the choice in localStorage. `auto` follows the OS via prefers-color-scheme.

# Dark palette, reused by both the explicit data-theme="dark" selector and the
# prefers-color-scheme media query (so it can't drift between the two).
_DARK_VARS = """
    --bg: #0f1216;
    --fg: #d7dce3;
    --fg-strong: #f0f3f7;
    --muted: #828b98;
    --faint: #5e6672;
    --rule: #20262e;
    --surface: #161a20;
    --surface-2: #1c222b;
    --accent: #e0a93d;
    --accent-soft: rgba(224, 169, 61, 0.14);
    --type-py: #5b9bd5;
    --type-js: #d2b24a;
    --type-ts: #3fb0c2;
    --overlay: rgba(0, 0, 0, 0.7);
    --copy-btn-bg: rgba(22, 26, 32, 0.9);
    --success: #4fb567;
    --shadow-sm: rgba(0, 0, 0, 0.4);
    color-scheme: dark;
"""

THEME_VARS_CSS = (
    """
        :root {
    --bg: #fbfcfd;
    --fg: #1a1d22;
    --fg-strong: #0b0d10;
    --muted: #6b7280;
    --faint: #9aa1ac;
    --rule: #e6e9ed;
    --surface: #f4f6f8;
    --surface-2: #edf0f3;
    --accent: #b8801e;
    --accent-soft: rgba(184, 128, 30, 0.12);
    --type-py: #2f6fb0;
    --type-js: #9a7d12;
    --type-ts: #1f8090;
    --overlay: rgba(0, 0, 0, 0.5);
    --copy-btn-bg: rgba(255, 255, 255, 0.92);
    --success: #1a7f37;
    --shadow-sm: rgba(0, 0, 0, 0.08);
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
    color-scheme: light;
        }
        :root[data-theme="dark"] {"""
    + _DARK_VARS
    + """}
        @media (prefers-color-scheme: dark) {
          :root:not([data-theme="light"]) {"""
    + _DARK_VARS
    + """}
        }
        * { box-sizing: border-box; }
        .theme-toggle {
            position: fixed; top: 14px; right: 16px; z-index: 3000;
            background: var(--surface); color: var(--muted);
            border: 1px solid var(--rule); border-radius: 6px;
            padding: 5px 12px; font-family: var(--mono); font-size: 12px;
            letter-spacing: 0.04em; cursor: pointer;
        }
        .theme-toggle:hover { border-color: var(--accent); color: var(--accent); }
"""
)

# One nonce per process, not per response. Every page is regenerated at startup
# (`convert_files` rewrites index.html and each file unconditionally), so the
# documents on disk and the header serving them are always written by the same
# run -- a nonce cannot go stale against a page it did not generate.
#
# Nonce rather than script hashes: mdserve's file pages interpolate per-file data
# into their script block (nav data, the raw source, the relative path), so the
# bytes differ per page and no fixed hash can cover them. A nonce also cannot be
# silently invalidated by adding an inline script later, which a hash list can.
SCRIPT_NONCE = secrets.token_urlsafe(16)

# `script-src` carries the nonce alone: the highlight.js tag wears it too, so
# cdnjs is never a blanket script source and an injected <script> pointing there
# is still refused. Markdown legitimately embeds remote and data: images, hence
# the open img-src; `unsafe-inline` styles cover the pages' own <style> blocks
# and the style= attributes markdown emits. `media-src` is open for the same
# reason as img-src: a document that embeds a video is embedding content, and
# media carries no script.
#
# `frame-src` is deliberately left to fall through to `default-src 'none'`, so
# an <iframe> in a converted file renders as nothing. Framing is the one embed
# that is a phishing surface rather than content -- a hostile markdown file
# could otherwise frame a convincing login page inside a doc the reader trusts,
# and mdserve's whole job is rendering files you did not write.
CSP = (
    "default-src 'none'; "
    f"script-src 'nonce-{SCRIPT_NONCE}'; "
    "style-src 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "img-src * data:; "
    "media-src *; "
    "font-src data:; "
    "base-uri 'none'; "
    "form-action 'none'"
)

CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'

# Set the stored theme before first paint to avoid a light flash on dark pages.
THEME_HEAD_JS = (
    "(function(){var p=localStorage.getItem('mdserve-theme');"
    "if(p&&p!=='auto')document.documentElement.setAttribute('data-theme',p);})();"
)

THEME_TOGGLE_HTML = (
    '<button class="theme-toggle" id="theme-toggle" '
    'title="Cycle theme: auto / light / dark">auto</button>'
)

# Cycles the theme and, on pages that have one, swaps the highlight.js stylesheet
# between github (light) and github-dark to match.
THEME_JS = """
(function() {
    var root = document.documentElement;
    var btn = document.getElementById('theme-toggle');
    var THEMES = ['auto', 'light', 'dark'];
    var pref = localStorage.getItem('mdserve-theme') || 'auto';
    var hljsLink = document.getElementById('hljs-theme');
    var HLJS_LIGHT = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css';
    var HLJS_DARK = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css';
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    function effectiveDark() { return pref === 'dark' || (pref === 'auto' && mq.matches); }
    function apply() {
        if (pref === 'auto') root.removeAttribute('data-theme');
        else root.setAttribute('data-theme', pref);
        if (btn) btn.textContent = pref;
        if (hljsLink) hljsLink.href = effectiveDark() ? HLJS_DARK : HLJS_LIGHT;
    }
    if (btn) {
        btn.addEventListener('click', function() {
            pref = THEMES[(THEMES.indexOf(pref) + 1) % THEMES.length];
            localStorage.setItem('mdserve-theme', pref);
            apply();
        });
    }
    mq.addEventListener('change', apply);
    apply();
})();
"""

# Directory-index stylesheet (var-based; pairs with THEME_VARS_CSS).
INDEX_CSS = """
        body { font-family: var(--sans); line-height: 1.5; color: var(--fg); background: var(--bg); max-width: 760px; margin: 0 auto; padding: 28px 20px 80px; }
        ul { list-style: none; padding: 0; margin: 0; }
        a { color: inherit; }

        /* Header: path eyebrow + count on one hairline-ruled line, no box. */
        /* padding-right clears the floating theme toggle (top-right) so the count doesn't sit under it. */
        .head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; padding-bottom: 12px; padding-right: 72px; border-bottom: 1px solid var(--rule); margin-bottom: 18px; }
        .head .path { font-family: var(--mono); font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .head .path .sep { color: var(--faint); margin: 0 2px; }
        .head .count { font-family: var(--mono); font-size: 12px; color: var(--faint); white-space: nowrap; flex: none; }

        /* Search as a terminal prompt: caret glyph + hairline underline. */
        .search-container { position: relative; margin: 0 0 4px; }
        .search-input { width: 100%; padding: 8px 4px 8px 22px; border: none; border-bottom: 1px solid var(--rule); background: transparent; color: var(--fg); font-family: var(--mono); font-size: 15px; }
        .search-input::placeholder { color: var(--faint); }
        .search-input:focus { outline: none; border-bottom-color: var(--accent); }
        .search-icon { position: absolute; left: 2px; top: 7px; color: var(--accent); font-family: var(--mono); font-size: 16px; pointer-events: none; }
        .search-results { font-family: var(--mono); font-size: 12px; color: var(--faint); margin: 6px 0 0; }

        /* Filter toggles: outlined chips; active = accent border + soft fill. */
        .filter-buttons { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
        .filter-btn { background: none; border: 1px solid var(--rule); border-radius: 6px; padding: 3px 11px; font-family: var(--mono); font-size: 12px; letter-spacing: 0.05em; text-transform: lowercase; color: var(--muted); cursor: pointer; transition: border-color 0.12s ease, color 0.12s ease, background 0.12s ease; }
        .filter-btn:hover { color: var(--fg); border-color: var(--muted); }
        .filter-btn.active { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
        .filter-btn.clear-btn { margin-left: auto; color: var(--faint); }
        .filter-btn.clear-btn:hover { color: var(--fg); border-color: var(--muted); }

        /* Section + recent labels share one eyebrow treatment. */
        .file-section { margin-top: 22px; }
        .file-section h3, .recent-header h3, .recent-tier-label { font-family: var(--mono); font-weight: 600; font-size: 11.5px; letter-spacing: 0.04em; color: var(--muted); margin: 0; padding: 0; border: none; }
        .file-section h3 { margin-bottom: 2px; }

        /* File rows: borderless, hairline divider, filename brightens on hover. */
        .file-item { display: flex; align-items: baseline; justify-content: space-between; gap: 14px; padding: 9px 8px; border-bottom: 1px solid var(--rule); border-radius: 5px; transition: background 0.12s ease; }
        .file-item:hover { background: var(--surface); }
        .file-link { text-decoration: none; color: inherit; display: flex; align-items: baseline; gap: 11px; min-width: 0; }
        /* Extension is a small color-coded type marker (color = information). */
        .ext { font-family: var(--mono); font-size: 11px; font-weight: 600; color: var(--faint); flex: none; width: 2.4em; text-align: right; }
        .ext-md { color: var(--accent); }
        .ext-py { color: var(--type-py); }
        .ext-js { color: var(--type-js); }
        .ext-ts { color: var(--type-ts); }
        .file-name { font-family: var(--sans); font-weight: 500; font-size: 15px; color: var(--fg-strong); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .file-item:hover .file-name { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
        .file-stats { font-family: var(--mono); font-size: 12px; color: var(--faint); white-space: nowrap; flex: none; font-variant-numeric: tabular-nums; }

        /* Recently modified: an eyebrow group, not a tinted card. */
        .recent-section { margin: 0; border: none; background: none; overflow: visible; }
        .recent-header { display: flex; align-items: center; gap: 8px; padding: 0; background: none; border: none; cursor: pointer; user-select: none; }
        .recent-header:hover { background: none; }
        .recent-toggle { font-size: 9px; color: var(--faint); transition: transform 0.15s ease; }
        .recent-header.collapsed .recent-toggle { transform: rotate(-90deg); }
        .recent-body { padding: 4px 0 0; background: none; }
        .recent-body.hidden { display: none; }
        .recent-tier { margin-bottom: 2px; }
        .recent-tier-label { margin: 10px 0 2px; opacity: 0.75; }

        .no-results { text-align: center; padding: 40px 20px; color: var(--muted); font-style: italic; }
        .footer { margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--rule); color: var(--faint); font-family: var(--mono); font-size: 11px; text-align: center; }
        .footer code { color: var(--muted); }

        @media (max-width: 520px) {
            .file-stats { font-size: 11px; }
            .ext { width: 2em; }
            /* Narrow portrait: stack the row so stats drop to their own line
               under the filename instead of crowding it. */
            .file-item { flex-direction: column; align-items: stretch; gap: 3px; }
            .file-stats { margin-left: calc(2em + 11px); }
        }
"""

# Individual file-view stylesheet (var-based; pairs with THEME_VARS_CSS).
FILE_CSS = """
        body { font-family: var(--sans); line-height: 1.6; color: var(--fg); background: var(--bg); max-width: 820px; margin: 0 auto; padding: 28px 20px 80px; }
        .breadcrumb { font-family: var(--mono); font-size: 13px; color: var(--muted); padding: 0 0 12px; margin-bottom: 22px; border-bottom: 1px solid var(--rule); background: none; }
        .breadcrumb a { color: var(--muted); text-decoration: none; }
        .breadcrumb a:hover { color: var(--accent); }
        .footer { margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--rule); color: var(--faint); font-family: var(--mono); font-size: 11px; }
        /* Sits below the floating theme toggle (top:14) so they don't overlap. */
        .floating-buttons { position: fixed; top: 54px; right: 16px; display: flex; flex-direction: column; gap: 6px; z-index: 1000; }
        .btn { background: var(--surface); color: var(--fg); border: 1px solid var(--rule); border-radius: 6px; padding: 6px 10px; font-family: var(--mono); font-size: 12px; cursor: pointer; text-decoration: none; transition: border-color 0.12s ease, color 0.12s ease; }
        .btn:hover { border-color: var(--accent); color: var(--accent); }
        .nav-buttons { display: flex; gap: 4px; }
        .btn.compact { padding: 5px 9px; min-width: 30px; text-align: center; }
        /* Thin clipboard glyph marks the copy actions; inherits btn color, dimmed. */
        .btn-ico { width: 11px; height: 11px; vertical-align: -1px; margin-right: 5px; opacity: 0.65; }
        .btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn:disabled:hover { border-color: var(--rule); color: var(--fg); }
        .modal { display: none; position: fixed; inset: 0; background: var(--overlay); z-index: 2000; }
        .modal-content { background: var(--bg); margin: 3% auto; width: 90%; max-width: 1000px; height: 90%; border: 1px solid var(--rule); border-radius: 6px; display: flex; flex-direction: column; }
        .modal-header { padding: 14px 18px; border-bottom: 1px solid var(--rule); display: flex; justify-content: space-between; align-items: center; }
        .modal-title { margin: 0; font-size: 14px; font-family: var(--mono); color: var(--fg-strong); }
        .modal-close { background: none; border: none; font-size: 22px; cursor: pointer; color: var(--muted); }
        .modal-close:hover { color: var(--accent); }
        .modal-body { flex: 1; overflow: auto; }
        .modal-code { margin: 0; height: 100%; }
        .modal-code pre { margin: 0; padding: 18px; height: 100%; overflow: auto; font-family: var(--mono); font-size: 13px; line-height: 1.5; background: transparent !important; border: none !important; border-radius: 0 !important; box-shadow: none !important; }
        /* Flatten: code sits directly on the modal background — no nested gray panel. */
        .modal-code pre code, .modal-code .hljs { background: transparent !important; padding: 0 !important; }
        pre { background: var(--surface) !important; border: 1px solid var(--rule) !important; border-left: 2px solid var(--accent) !important; border-radius: 6px !important; box-shadow: none !important; margin: 16px 0 !important; }
        .code-block-container { position: relative; }
        .copy-code-btn { position: absolute; top: 8px; right: 8px; background: var(--copy-btn-bg); border: 1px solid var(--rule); border-radius: 5px; padding: 3px 8px; font-family: var(--mono); font-size: 11px; cursor: pointer; color: var(--muted); z-index: 10; }
        .copy-code-btn:hover { color: var(--accent); border-color: var(--accent); }
        .copy-code-btn.copied { background: var(--success); color: #fff; border-color: var(--success); }
        /* Wide tables scroll inside their own box rather than widening the 820px column. */
        .table-wrap { overflow-x: auto; margin: 16px 0; }
        .table-wrap table { margin: 0; }
        table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px; }
        th, td { border: 1px solid var(--rule); padding: 7px 11px; text-align: left; vertical-align: top; }
        thead th { background: var(--surface); color: var(--fg-strong); font-weight: 600; }
        tbody tr:nth-child(even) { background: var(--accent-soft); }
"""


def create_index_template(files: List[Tuple[str, str]], base_dir: str) -> str:
    """Create HTML index page listing all markdown files."""
    file_list_html = ""

    if not files:
        file_list_html = "<p>📭 No markdown files found in this directory.</p>"
    else:
        # Group files by directory
        grouped = {}
        for full_path, rel_path in files:
            dir_name = os.path.dirname(rel_path) if os.path.dirname(rel_path) else "."
            if dir_name not in grouped:
                grouped[dir_name] = []
            grouped[dir_name].append((full_path, rel_path))

        for dir_name in sorted(grouped.keys()):
            section_id = dir_name.replace("/", "_").replace(".", "root")
            if dir_name != ".":
                file_list_html += f'<div class="file-section" data-section="{section_id}"><h3>{dir_name}/</h3>\n<ul>\n'
            else:
                file_list_html += (
                    f'<div class="file-section" data-section="{section_id}"><h3>Files</h3>\n<ul>\n'
                )

            for full_path, rel_path in grouped[dir_name]:
                # Get file stats
                try:
                    with open(full_path, encoding="utf-8") as f:
                        content = f.read()
                        line_count = len(content.splitlines())
                        word_count = len(content.split())

                    # Get file modification time
                    mod_time = os.path.getmtime(full_path)
                except (OSError, UnicodeDecodeError):
                    line_count = word_count = 0
                    mod_time = 0

                # Create URL-safe path
                url_path = rel_path.replace("\\", "/")
                if not url_path.endswith(".html"):
                    url_path = url_path + ".html"

                filename = os.path.basename(rel_path)
                # Compact, manifest-style metadata: "1,542 lines · 8.4k words · 2d".
                stats = f"{line_count:,} lines · {compact_count(word_count)} words · {compact_age(mod_time)}"

                # Extension as a monospace marker (the structural cue), not an emoji.
                ext = os.path.splitext(filename)[1].lstrip(".").lower() or "txt"

                file_list_html += f'''
                <li class="file-entry" data-filename="{filename.lower()}" data-path="{rel_path.lower()}" data-mtime="{mod_time}">
                    <div class="file-item">
                        <a href="{url_path}" class="file-link">
                            <span class="ext ext-{ext}">{ext}</span>
                            <span class="file-name">{filename}</span>
                        </a>
                        <div class="file-stats">{stats}</div>
                    </div>
                </li>'''

            file_list_html += "</ul></div>\n"

    total_count = len(files)
    base_name = os.path.basename(os.path.abspath(base_dir))
    abs_dir = os.path.abspath(base_dir)
    home = os.path.expanduser("~")
    # Only abbreviate true descendants of home — a bare startswith would turn
    # /Users/mattress into ~ress when home is /Users/matt.
    if abs_dir == home:
        display_path = "~"
    elif abs_dir.startswith(home + os.sep):
        display_path = "~" + abs_dir[len(home) :]
    else:
        display_path = abs_dir

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {CSP_META}
    <script nonce="{SCRIPT_NONCE}">{THEME_HEAD_JS}</script>
    <title>📚 Files - {base_name}</title>
    <style>{THEME_VARS_CSS}{INDEX_CSS}    </style>
</head>
<body>
    {THEME_TOGGLE_HTML}
    <div class="head">
        <span class="path">{display_path}</span>
        <span class="count">{total_count} file{"s" if total_count != 1 else ""}</span>
    </div>

    <div class="search-container">
        <span class="search-icon">/</span>
        <input type="text" class="search-input" id="searchInput" placeholder="filter files…" autocomplete="off" spellcheck="false">
        <div class="filter-buttons">
            <button class="filter-btn" data-filter=".js">js</button>
            <button class="filter-btn" data-filter=".ts">ts</button>
            <button class="filter-btn" data-filter=".md">md</button>
            <button class="filter-btn" data-filter=".py">py</button>
            <button class="filter-btn clear-btn" data-filter="">clear</button>
        </div>
        <div class="search-results" id="searchResults"></div>
    </div>

    <div id="recentlyModified"></div>

    <div id="fileList">
        {file_list_html}
    </div>

    <div class="no-results" id="noResults" style="display: none;">
        📭 No files match your search. Try different keywords.
    </div>

    <div class="footer">
        <p>served by <code>mdserve.py</code> · click a file to read it · / to filter</p>
    </div>

    <script nonce="{SCRIPT_NONCE}">
        // Search functionality
        const searchInput = document.getElementById('searchInput');
        const searchResults = document.getElementById('searchResults');
        const fileList = document.getElementById('fileList');
        const noResults = document.getElementById('noResults');
        const allFiles = document.querySelectorAll('.file-entry');
        const allSections = document.querySelectorAll('.file-section');

        let totalFiles = allFiles.length;

        function updateSearchResults(visibleCount) {{
            if (searchInput.value.trim()) {{
                searchResults.textContent = `${{visibleCount}} of ${{totalFiles}} files match`;
                searchResults.style.display = 'block';
            }} else {{
                searchResults.style.display = 'none';
            }}
        }}

        function performSearch() {{
            const query = searchInput.value.toLowerCase().trim();
            let visibleCount = 0;
            const recentSection = document.getElementById('recentlyModified');

            if (!query) {{
                allFiles.forEach(file => {{
                    file.style.display = 'block';
                    visibleCount++;
                }});
                allSections.forEach(section => section.style.display = 'block');
                if (recentSection) recentSection.style.display = 'block';
                fileList.style.display = 'block';
                noResults.style.display = 'none';
                updateSearchResults(0);
                return;
            }}

            if (recentSection) recentSection.style.display = 'none';
            allSections.forEach(section => section.style.display = 'none');

            allFiles.forEach(file => {{
                const filename = file.dataset.filename || '';
                const filepath = file.dataset.path || '';
                const isMatch = filename.includes(query) || filepath.includes(query);

                if (isMatch) {{
                    file.style.display = 'block';
                    visibleCount++;

                    const section = file.closest('.file-section');
                    if (section) {{
                        section.style.display = 'block';
                    }}
                }} else {{
                    file.style.display = 'none';
                }}
            }});

            if (visibleCount === 0) {{
                fileList.style.display = 'none';
                noResults.style.display = 'block';
            }} else {{
                fileList.style.display = 'block';
                noResults.style.display = 'none';
            }}

            updateSearchResults(visibleCount);
        }}

        searchInput.addEventListener('input', performSearch);
        searchInput.addEventListener('keydown', function(e) {{
            if (e.key === 'Escape') {{
                searchInput.value = '';
                performSearch();
                searchInput.blur();
            }}
        }});

        document.addEventListener('keydown', function(e) {{
            const inField = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
            // Ctrl/Cmd+K or a bare "/" (when not already typing) focuses the filter.
            if (((e.ctrlKey || e.metaKey) && e.key === 'k') || (e.key === '/' && !inField)) {{
                e.preventDefault();
                searchInput.focus();
                searchInput.select();
            }}
        }});

        // Filter functionality
        function filterByType(extension, button) {{
            searchInput.value = extension;
            performSearch();

            // Update button states
            document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
            button.classList.add('active');
        }}

        function clearFilter() {{
            searchInput.value = '';
            performSearch();

            // Update button states
            document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
        }}

        // Wired here, not with onclick= attributes, which the nonce-only
        // script-src refuses: the buttons were dead under the policy.
        document.querySelectorAll('.filter-btn').forEach(btn => btn.addEventListener('click', () =>
            btn.dataset.filter ? filterByType(btn.dataset.filter, btn) : clearFilter()
        ));

        // Recently Modified section
        (function() {{
            const now = Date.now() / 1000;
            const TIERS = [
                {{ label: 'Last 30 minutes', maxAge: 30 * 60 }},
                {{ label: 'Last hour', maxAge: 60 * 60 }},
            ];

            const entries = Array.from(document.querySelectorAll('.file-entry[data-mtime]'));
            const tiered = TIERS.map(tier => ({{
                ...tier,
                files: entries
                    .filter(el => {{
                        const mtime = parseFloat(el.dataset.mtime);
                        return mtime > 0 && (now - mtime) <= tier.maxAge;
                    }})
                    .sort((a, b) => parseFloat(b.dataset.mtime) - parseFloat(a.dataset.mtime))
            }}));

            // Use only the files unique to each tier (30m gets priority, 60m shows the rest)
            const seen = new Set();
            const uniqueTiers = tiered.map(tier => {{
                const unique = tier.files.filter(el => !seen.has(el.dataset.path));
                unique.forEach(el => seen.add(el.dataset.path));
                return {{ ...tier, files: unique }};
            }});

            const hasRecent = uniqueTiers.some(t => t.files.length > 0);
            const container = document.getElementById('recentlyModified');

            // Fallback: if nothing in the last 60m, show top 5 most recently modified
            if (!hasRecent) {{
                const byMtime = entries
                    .filter(el => parseFloat(el.dataset.mtime) > 0)
                    .sort((a, b) => parseFloat(b.dataset.mtime) - parseFloat(a.dataset.mtime))
                    .slice(0, 5);

                if (byMtime.length === 0) return;

                let fallbackHtml = '<ul>';
                byMtime.forEach(el => {{
                    fallbackHtml += '<li>' + el.querySelector('.file-item').outerHTML + '</li>';
                }});
                fallbackHtml += '</ul>';

                container.innerHTML =
                    '<div class="recent-section">' +
                        '<div class="recent-header" id="recentHeader">' +
                            '<h3>Recently modified</h3>' +
                            '<span class="recent-toggle">▼</span>' +
                        '</div>' +
                        '<div class="recent-body" id="recentBody">' +
                            fallbackHtml +
                        '</div>' +
                    '</div>';

                document.getElementById('recentHeader').addEventListener('click', function() {{
                    this.classList.toggle('collapsed');
                    document.getElementById('recentBody').classList.toggle('hidden');
                }});
                return;
            }}

            let bodyHtml = '';
            uniqueTiers.forEach(tier => {{
                if (tier.files.length === 0) return;
                bodyHtml += '<div class="recent-tier">';
                bodyHtml += '<div class="recent-tier-label">' + tier.label + '</div>';
                bodyHtml += '<ul>';
                tier.files.forEach(el => {{
                    bodyHtml += '<li>' + el.querySelector('.file-item').outerHTML + '</li>';
                }});
                bodyHtml += '</ul></div>';
            }});

            const totalRecent = uniqueTiers.reduce((n, t) => n + t.files.length, 0);

            container.innerHTML =
                '<div class="recent-section">' +
                    '<div class="recent-header" id="recentHeader">' +
                        '<h3>Recently modified · ' + totalRecent + '</h3>' +
                        '<span class="recent-toggle">▼</span>' +
                    '</div>' +
                    '<div class="recent-body" id="recentBody">' +
                        bodyHtml +
                    '</div>' +
                '</div>';

            document.getElementById('recentHeader').addEventListener('click', function() {{
                this.classList.toggle('collapsed');
                document.getElementById('recentBody').classList.toggle('hidden');
            }});
        }})();
    </script>
    <script nonce="{SCRIPT_NONCE}">{THEME_JS}</script>
</body>
</html>"""


def create_html_template(
    title: str,
    content: str,
    raw_markdown: str,
    line_count: int,
    rel_path: str = "",
    nav_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Create HTML template for individual markdown files."""
    breadcrumb = create_breadcrumb_nav(rel_path, title)
    page_data = json_island(
        "page-data",
        {"nav": create_navigation_data(nav_info, title), "raw": raw_markdown},
    )

    # This is a very long template - I'll include a condensed version for now
    # and can expand it later if needed
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {CSP_META}
    <script nonce="{SCRIPT_NONCE}">{THEME_HEAD_JS}</script>
    <title>{title}</title>
    <link id="hljs-theme" rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
    <style>{THEME_VARS_CSS}{FILE_CSS}    </style>
</head>
<body>
    {THEME_TOGGLE_HTML}
    {breadcrumb}

    <div class="floating-buttons">
        <a href="/" class="btn secondary"><svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>index</a>
        <div class="nav-buttons">
            <button class="btn compact" id="prevBtn" data-action="navigatePrev" disabled>←</button>
            <button class="btn compact" id="nextBtn" data-action="navigateNext" disabled>→</button>
        </div>
        <button class="btn" data-action="showRawMarkdown">raw</button>
        <button class="btn" data-action="copyPath"><svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>path</button>
        <button class="btn" data-action="copySource"><svg class="btn-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>source</button>
    </div>

    {content}

    <div class="footer">
        <p>📄 Served by <code>mdserve.py</code> • {line_count} lines</p>
    </div>

    <!-- Raw source modal -->
    <div id="rawModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 class="modal-title">raw · {title}</h3>
                <button class="modal-close" data-action="closeRawModal">&times;</button>
            </div>
            <div class="modal-body">
                <div class="modal-code">
                    <pre><code id="rawContent"></code></pre>
                </div>
            </div>
        </div>
    </div>

    {page_data}
    <script nonce="{SCRIPT_NONCE}">
        const PAGE_DATA = JSON.parse(document.getElementById('page-data').textContent);
        const NAV_DATA = PAGE_DATA.nav;

        function navigatePrev() {{
            if (NAV_DATA && NAV_DATA.prev) {{
                window.location.href = NAV_DATA.prev.path;
            }}
        }}

        function navigateNext() {{
            if (NAV_DATA && NAV_DATA.next) {{
                window.location.href = NAV_DATA.next.path;
            }}
        }}

        // Raw content for modal
        const RAW_CONTENT = PAGE_DATA.raw;

        function showRawMarkdown() {{
            const modal = document.getElementById('rawModal');
            const content = document.getElementById('rawContent');
            content.textContent = RAW_CONTENT;
            modal.style.display = 'block';

            // Auto-highlight if it's code
            if (typeof hljs !== 'undefined') {{
                hljs.highlightElement(content);
            }}
        }}

        function closeRawModal() {{
            const modal = document.getElementById('rawModal');
            modal.style.display = 'none';
        }}

        // Close modal when clicking outside
        window.onclick = function(event) {{
            const modal = document.getElementById('rawModal');
            if (event.target === modal) {{
                closeRawModal();
            }}
        }}

        // Copy functionality
        function copyPath() {{
            const path = window.location.pathname.replace('.html', '').substring(1) || '{rel_path}';
            navigator.clipboard.writeText(path).then(() => {{
                showToast('Path copied to clipboard');
            }}).catch(() => {{
                // Fallback for older browsers
                const textArea = document.createElement('textarea');
                textArea.value = path;
                document.body.appendChild(textArea);
                textArea.select();
                document.execCommand('copy');
                document.body.removeChild(textArea);
                showToast('Path copied to clipboard');
            }});
        }}

        function copySource() {{
            navigator.clipboard.writeText(RAW_CONTENT).then(() => {{
                showToast('Source copied to clipboard');
            }}).catch(() => {{
                // Fallback for older browsers
                const textArea = document.createElement('textarea');
                textArea.value = RAW_CONTENT;
                document.body.appendChild(textArea);
                textArea.select();
                document.execCommand('copy');
                document.body.removeChild(textArea);
                showToast('Source copied to clipboard');
            }});
        }}

        function showToast(message) {{
            const toast = document.createElement('div');
            toast.style.cssText = 'position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: var(--success); color: #fff; padding: 8px 16px; border-radius: 6px; z-index: 3000; font-size: 14px;';
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => document.body.removeChild(toast), 2000);
        }}

        document.addEventListener('keydown', function(e) {{
            if (e.key === 'ArrowLeft') navigatePrev();
            if (e.key === 'ArrowRight') navigateNext();
            if (e.key === 'Home') window.location.href = '/';
            if (e.key === 'Escape') closeRawModal();
        }});

        // Wired here, not with onclick= attributes, which the nonce-only
        // script-src refuses: the buttons were dead under the policy. Scoped to
        // the toolbar and modal because rendered files keep their raw HTML, and
        // a page-wide [data-action] query would let a file's markup trigger these.
        const ACTIONS = {{ navigatePrev, navigateNext, showRawMarkdown, copyPath, copySource, closeRawModal }};
        [document.querySelector('.floating-buttons'), document.getElementById('rawModal')].forEach(root =>
            root.querySelectorAll('[data-action]').forEach(el =>
                el.addEventListener('click', () => ACTIONS[el.dataset.action]())
            )
        );

        // Initialize navigation and code block copy functionality
        document.addEventListener('DOMContentLoaded', function() {{
            const prevBtn = document.getElementById('prevBtn');
            const nextBtn = document.getElementById('nextBtn');

            if (NAV_DATA) {{
                prevBtn.disabled = !NAV_DATA.prev;
                nextBtn.disabled = !NAV_DATA.next;
            }}

            // Add copy buttons to all code blocks
            addCopyButtonsToCodeBlocks();
            wrapTables();
        }});

        function wrapTables() {{
            document.querySelectorAll('table').forEach(function(table) {{
                if (table.parentElement.classList.contains('table-wrap')) return;
                const wrap = document.createElement('div');
                wrap.className = 'table-wrap';
                table.parentNode.insertBefore(wrap, table);
                wrap.appendChild(table);
            }});
        }}

        function addCopyButtonsToCodeBlocks() {{
            const codeBlocks = document.querySelectorAll('pre > code');
            
            codeBlocks.forEach(function(codeBlock) {{
                const pre = codeBlock.parentElement;
                
                // Skip if already has a copy button
                if (pre.querySelector('.copy-code-btn')) return;
                
                // Create container wrapper
                const container = document.createElement('div');
                container.className = 'code-block-container';
                pre.parentNode.insertBefore(container, pre);
                container.appendChild(pre);
                
                // Create copy button
                const copyBtn = document.createElement('button');
                copyBtn.className = 'copy-code-btn';
                copyBtn.textContent = 'copy';
                copyBtn.title = 'Copy code to clipboard';
                
                copyBtn.addEventListener('click', function() {{
                    const text = codeBlock.textContent || codeBlock.innerText;
                    
                    navigator.clipboard.writeText(text).then(function() {{
                        copyBtn.textContent = 'copied';
                        copyBtn.classList.add('copied');
                        
                        setTimeout(function() {{
                            copyBtn.textContent = 'copy';
                            copyBtn.classList.remove('copied');
                        }}, 2000);
                    }}).catch(function() {{
                        // Fallback for older browsers
                        const textArea = document.createElement('textarea');
                        textArea.value = text;
                        document.body.appendChild(textArea);
                        textArea.select();
                        document.execCommand('copy');
                        document.body.removeChild(textArea);
                        
                        copyBtn.textContent = 'copied';
                        copyBtn.classList.add('copied');
                        
                        setTimeout(function() {{
                            copyBtn.textContent = 'copy';
                            copyBtn.classList.remove('copied');
                        }}, 2000);
                    }});
                }});
                
                container.appendChild(copyBtn);
            }});
        }}
    </script>

    <script nonce="{SCRIPT_NONCE}" src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
    <script nonce="{SCRIPT_NONCE}">hljs.highlightAll();</script>
    <script nonce="{SCRIPT_NONCE}">{THEME_JS}</script>
</body>
</html>"""


def convert_files(
    files: List[Tuple[str, str]], base_dir: str, output_dir: str, verbose: bool = False
) -> None:
    """Convert markdown and Python files to HTML with navigation."""
    import mistune

    # Create index page
    index_html = create_index_template(files, base_dir)
    with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)
    print("✅ Created index.html")

    # Initialize markdown renderer
    markdown_renderer = mistune.create_markdown(
        escape=False, plugins=["strikethrough", "footnotes", "table"]
    )

    for i, (full_path, rel_path) in enumerate(files):
        # Read file content
        try:
            with open(full_path, encoding="utf-8") as f:
                file_content = f.read()
        except Exception as e:
            print(f"⚠️  Skipping {rel_path}: {e}")
            continue

        # Count lines
        line_count = len(file_content.splitlines())

        # Determine file type and convert accordingly
        # Note: Expects properly formatted markdown with URL-encoded special chars
        # (e.g., spaces as %20 in image URLs)
        if rel_path.lower().endswith((".md", ".markdown")):
            fm_html, body = render_frontmatter(file_content)
            html_content = fm_html + rewrite_markdown_links(markdown_renderer(body))
        elif rel_path.lower().endswith(".py"):
            # Wrap Python code in HTML with syntax highlighting
            escaped_content = (
                file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            html_content = f'<pre><code class="language-python">{escaped_content}</code></pre>'
        elif rel_path.lower().endswith(".js"):
            # Wrap JavaScript code in HTML with syntax highlighting
            escaped_content = (
                file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            html_content = f'<pre><code class="language-javascript">{escaped_content}</code></pre>'
        elif rel_path.lower().endswith(".ts"):
            # Wrap TypeScript code in HTML with syntax highlighting
            escaped_content = (
                file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            html_content = f'<pre><code class="language-typescript">{escaped_content}</code></pre>'
        else:
            # Fallback for other file types
            escaped_content = (
                file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            html_content = f"<pre><code>{escaped_content}</code></pre>"

        # Create output path (keep original extension to avoid collisions)
        html_rel_path = rel_path + ".html"
        html_full_path = os.path.join(output_dir, html_rel_path)

        # Ensure directory exists
        os.makedirs(os.path.dirname(html_full_path), exist_ok=True)

        # Create navigation info
        nav_info = {
            "current_path": html_rel_path,
            "current_title": os.path.basename(rel_path),
            "current_index": i,
            "total_files": len(files),
        }

        # Add previous file info
        if i > 0:
            prev_full_path, prev_rel_path = files[i - 1]
            prev_html_path = prev_rel_path + ".html"
            nav_info["prev_path"] = prev_html_path
            nav_info["prev_title"] = os.path.basename(prev_rel_path)

        # Add next file info
        if i < len(files) - 1:
            next_full_path, next_rel_path = files[i + 1]
            next_html_path = next_rel_path + ".html"
            nav_info["next_path"] = next_html_path
            nav_info["next_title"] = os.path.basename(next_rel_path)

        # Create full HTML
        title = os.path.basename(rel_path)
        full_html = create_html_template(
            title, html_content, file_content, line_count, rel_path, nav_info
        )

        # Write output
        with open(html_full_path, "w", encoding="utf-8") as f:
            f.write(full_html)

        if verbose:
            if rel_path.lower().endswith(".py"):
                file_type = "Python"
            elif rel_path.lower().endswith(".js"):
                file_type = "JavaScript"
            elif rel_path.lower().endswith(".ts"):
                file_type = "TypeScript"
            elif rel_path.lower().endswith((".md", ".markdown")):
                file_type = "Markdown"
            else:
                file_type = "Code"
            print(f"✅ Converted {file_type} {rel_path} ({line_count} lines)")


def create_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Serve markdown, Python, JavaScript, and TypeScript files as HTML with navigation features",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Features:
  • Index page with all supported files (.md, .py, .js, .ts)
  • Individual file viewing with syntax highlighting
  • Arrow key navigation between documents
  • Raw source modal on each page
  • Live search functionality
  • Syntax highlighting for Python, JavaScript, and TypeScript
  • Responsive design with copy functionality

Examples:
  %(prog)s                                    # Serve current directory
  %(prog)s docs/                             # Serve docs directory
  %(prog)s --no-browser . 8080               # Custom port, no browser
  %(prog)s --exclude drafts --exclude temp   # Exclude specific patterns
  %(prog)s --include docs --include README   # Only serve matching files
  %(prog)s --include 'team-*'               # Only serve team-prefixed files
  %(prog)s --no-cleanup docs/                # Keep HTML files for copying/embedding

Keyboard shortcuts:
  ← → arrows    Navigate between documents
  Home          Jump to index page
  Ctrl+Shift+R  Show raw markdown
  Esc           Close modal
  / or Ctrl+K   Focus search
        """,
    )

    # Positional arguments with flexible order support
    parser.add_argument(
        "directory", nargs="?", default=".", help="Directory to serve (default: current directory)"
    )
    parser.add_argument(
        "port", nargs="?", type=int, default=8037, help="Port to serve on (default: 8037)"
    )
    parser.add_argument(
        "host", nargs="?", type=str, default="0.0.0.0", help="Host to listen on (default 0.0.0.0)"
    )

    # Options
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't automatically open browser"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        dest="exclude_patterns",
        help="Exclude files/dirs matching pattern (can be used multiple times)",
    )
    parser.add_argument(
        "--include",
        action="append",
        dest="include_patterns",
        help="Only include files/dirs matching pattern (allowlist mode)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose output for debugging"
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep generated HTML files after server stops (useful for copying/embedding elsewhere)",
    )
    parser.add_argument(
        "--convert-only",
        action="store_true",
        help="Convert a single file to HTML without starting server (outputs to same directory as input)",
    )
    parser.add_argument(
        "--export-md-inline",
        action="store_true",
        help="Export markdown with images as base64 data URIs to stdout (Google Docs compatible)",
    )
    parser.add_argument(
        "--no-copy-images",
        action="store_true",
        help="Do not copy images referenced in markdown files to serve directory",
    )

    return parser


def parse_flexible_args(args: List[str]) -> argparse.Namespace:
    """
    Parse arguments with flexible order support.
    Handles cases where port and directory can be in any order.
    """
    parser = create_parser()

    # Handle the case where args might be [port, directory] instead of [directory, port]
    parsed_args = parser.parse_args(args)

    # If directory looks like a port number and port looks like a directory, swap them
    if (
        parsed_args.directory.isdigit()
        and not parsed_args.directory == "."
        and not os.path.isdir(parsed_args.directory)
        and isinstance(parsed_args.port, int)
    ):
        # Check if the port value could be a directory
        potential_dir = str(parsed_args.port)
        if os.path.isdir(potential_dir):
            # Swap them
            port_val = int(parsed_args.directory)
            dir_val = potential_dir
            parsed_args.directory = dir_val
            parsed_args.port = port_val

    return parsed_args


def convert_single_file(input_path: str) -> None:
    """Convert a single markdown/code file to HTML."""
    import mistune

    # Validate input file exists
    if not os.path.isfile(input_path):
        print(f"❌ Error: File '{input_path}' not found")
        sys.exit(1)

    # Check file extension
    valid_extensions = (".md", ".markdown", ".py", ".js", ".ts")
    if not input_path.lower().endswith(valid_extensions):
        print(f"❌ Error: Unsupported file type. Supported: {', '.join(valid_extensions)}")
        sys.exit(1)

    # Determine output path (keep original extension to avoid collisions)
    output_path = input_path + ".html"

    # Fail fast if output already exists
    if os.path.exists(output_path):
        print(f"❌ Error: Output file already exists: {output_path}")
        print("💡 Remove the existing file or rename it to proceed")
        sys.exit(1)

    # Read input file
    try:
        with open(input_path, encoding="utf-8") as f:
            file_content = f.read()
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        sys.exit(1)

    line_count = len(file_content.splitlines())

    # Initialize markdown renderer
    markdown_renderer = mistune.create_markdown(
        escape=False, plugins=["strikethrough", "footnotes", "table"]
    )

    # Convert based on file type
    # Note: Expects properly formatted markdown with URL-encoded special chars
    # (e.g., spaces as %20 in image URLs)
    if input_path.lower().endswith((".md", ".markdown")):
        fm_html, body = render_frontmatter(file_content)
        html_content = fm_html + markdown_renderer(body)
    elif input_path.lower().endswith(".py"):
        escaped_content = (
            file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        html_content = f'<pre><code class="language-python">{escaped_content}</code></pre>'
    elif input_path.lower().endswith(".js"):
        escaped_content = (
            file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        html_content = f'<pre><code class="language-javascript">{escaped_content}</code></pre>'
    elif input_path.lower().endswith(".ts"):
        escaped_content = (
            file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        html_content = f'<pre><code class="language-typescript">{escaped_content}</code></pre>'
    else:
        escaped_content = (
            file_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        html_content = f"<pre><code>{escaped_content}</code></pre>"

    # Create full HTML (no navigation for single file)
    title = os.path.basename(input_path)
    rel_path = os.path.basename(input_path)
    full_html = create_html_template(
        title, html_content, file_content, line_count, rel_path, nav_info=None
    )

    # Write output
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(full_html)
    except Exception as e:
        print(f"❌ Error writing file: {e}")
        sys.exit(1)

    print(f"✅ Converted: {input_path} → {output_path}")


class CSPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static handler that sends the policy the generated pages were built for.

    The meta tag in each page covers the `--convert-only` and `--no-cleanup`
    files, which are opened over file:// with no server to set a header. This
    sends the same policy over HTTP, where it also reaches the non-HTML
    responses -- images, and anything else sitting in the served directory.
    """

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def serve_single_file(file_path: str, host: str, port: int, open_browser_flag: bool) -> None:
    """Serve a single markdown/code file."""

    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        print(f"❌ Error: File '{file_path}' not found")
        sys.exit(1)

    base_dir = os.path.dirname(abs_path)
    rel_path = os.path.basename(abs_path)

    with tempfile.TemporaryDirectory() as temp_dir:
        serve_root = os.path.join(temp_dir, "serve")
        os.makedirs(serve_root)

        files = [(abs_path, rel_path)]
        convert_files(files, base_dir, serve_root, verbose=False)
        copy_relative_images(files, base_dir, serve_root)

        # The converted file is at rel_path + '.html'
        html_name = rel_path + ".html"
        url = f"http://localhost:{port}/{html_name}"

        print(f"🚀 Serving {rel_path} at {url}")
        print("Press Ctrl+C to stop the server")

        if open_browser_flag:

            def delayed_open():
                time.sleep(1)
                open_browser(url)

            t = threading.Thread(target=delayed_open, daemon=True)
            t.start()

        os.chdir(serve_root)
        try:
            with http.server.ThreadingHTTPServer((host, port), CSPRequestHandler) as httpd:
                httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 Cleaning up...")
        except OSError as e:
            if e.errno == 48:
                print(f"❌ Error: Port {port} is already in use")
            else:
                print(f"❌ Error starting server: {e}")
            sys.exit(1)


def main():
    """Main entry point."""
    # Parse arguments with flexible order support
    args = parse_flexible_args(sys.argv[1:])

    # Handle --convert-only mode
    if args.convert_only:
        convert_single_file(args.directory)
        return

    # Handle --export-md-inline mode
    if args.export_md_inline:
        export_md_inline(args.directory)
        return

    # Validate arguments
    if args.include_patterns and args.exclude_patterns:
        print("❌ Error: Cannot use both --include and --exclude options together")
        print("💡 Use --include for allowlist mode OR --exclude for blocklist mode, not both")
        sys.exit(1)

    # Single file mode: serve just one file
    if os.path.isfile(args.directory):
        serve_single_file(args.directory, args.host, args.port, not args.no_browser)
        return

    if not os.path.isdir(args.directory):
        print(f"❌ Error: '{args.directory}' is not a file or directory")
        sys.exit(1)

    if not 1 <= args.port <= 65535:
        print(f"❌ Error: Port {args.port} must be between 1 and 65535")
        sys.exit(1)

    # Print scanning info
    scan_dir = os.path.realpath(args.directory)
    print(f"📂 Scanning {scan_dir} for supported files...")

    if args.include_patterns:
        print(f"✅ Including only patterns: {args.include_patterns}")
    if args.exclude_patterns:
        print(f"🚫 Excluding patterns: {args.exclude_patterns}")

    # Find files to serve
    files = (
        find_markdown_files(  # Function name unchanged for compatibility but now handles .py too
            args.directory,
            exclude_patterns=args.exclude_patterns,
            include_patterns=args.include_patterns,
        )
    )

    print(f"📊 Found {len(files)} files (.md, .py, .js, .ts)")

    if len(files) == 0:
        print("📭 No supported files found with the given criteria.")
        sys.exit(0)

    # Create directory for HTML files (temporary or persistent)
    if args.no_cleanup:
        # Create persistent directory in current working directory
        temp_dir = os.path.join(os.getcwd(), f"mdserve_html_{int(time.time())}")
        os.makedirs(temp_dir, exist_ok=True)
        serve_root = os.path.join(temp_dir, "serve")
        os.makedirs(serve_root, exist_ok=True)

        print("🔄 Converting files to HTML...")

        # Convert files to HTML
        convert_files(files, args.directory, serve_root, args.verbose)

        # Copy images referenced in markdown files
        if args.no_copy_images:
            image_refs = copy_relative_images(files, args.directory, serve_root, dry_run=True)
            if image_refs:
                print(f"⚠️  Skipping {len(image_refs)} image(s) (--no-copy-images)")
        else:
            copy_relative_images(files, args.directory, serve_root, args.verbose)

        print(f"🚀 Serving files from '{scan_dir}' at http://{args.host}:{args.port}")
        print(f"📁 HTML files saved in: {serve_root}")
        print("💾 Files will persist after server stops (--no-cleanup enabled)")
        print(f"🏠 Index page: http://{args.host}:{args.port}/")
        print("Press Ctrl+C to stop the server")

        # Start HTTP server and optionally open browser
        serve_files(serve_root, args.host, args.port, not args.no_browser)

        # After server stops, remind user where files are
        print(f"📂 Generated HTML files are available at: {serve_root}")
    else:
        # Use temporary directory (original behavior)
        with tempfile.TemporaryDirectory() as temp_dir:
            serve_root = os.path.join(temp_dir, "serve")
            os.makedirs(serve_root, exist_ok=True)

            print("🔄 Converting files to HTML...")

            # Convert files to HTML
            convert_files(files, args.directory, serve_root, args.verbose)

            # Copy images referenced in markdown files
            if args.no_copy_images:
                image_refs = copy_relative_images(files, args.directory, serve_root, dry_run=True)
                if image_refs:
                    print(f"⚠️  Skipping {len(image_refs)} image(s) (--no-copy-images)")
            else:
                copy_relative_images(files, args.directory, serve_root, args.verbose)

            print(f"🚀 Serving files from '{scan_dir}' at http://{args.host}:{args.port}")
            print(f"📁 Temporary files in: {serve_root}")
            print(f"🏠 Index page: http://{args.host}:{args.port}/")
            print("Press Ctrl+C to stop the server")

            # Start HTTP server and optionally open browser
            serve_files(serve_root, args.host, args.port, not args.no_browser)


def open_browser(url: str) -> None:
    """Open browser to the given URL."""
    try:
        webbrowser.open(url)
    except Exception:
        if os.name == "posix":  # Unix/Linux/macOS
            try:
                subprocess.run(["open", url], check=False, capture_output=True)
            except FileNotFoundError:
                try:
                    subprocess.run(["xdg-open", url], check=False, capture_output=True)
                except FileNotFoundError:
                    pass  # Browser opening failed, but that's okay


def serve_files(serve_dir: str, host: str, port: int, open_browser_flag: bool) -> None:
    """Serve files using Python's built-in HTTP server."""
    os.chdir(serve_dir)

    # Open browser after a short delay
    if open_browser_flag:

        def delayed_browser_open():
            time.sleep(1)  # Give server time to start
            open_browser(f"http://localhost:{port}")

        browser_thread = threading.Thread(target=delayed_browser_open)
        browser_thread.daemon = True
        browser_thread.start()

    # Start HTTP server
    try:
        with http.server.ThreadingHTTPServer((host, port), CSPRequestHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Cleaning up...")
    except OSError as e:
        if e.errno == 48:  # Address already in use
            print(f"❌ Error: Port {port} is already in use")
            print("💡 Try a different port or stop the service using that port")
        else:
            print(f"❌ Error starting server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
