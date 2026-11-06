"""A small, safe Markdown -> HTML renderer for transcript prose.

Text is HTML-escaped *first*, then Markdown structure is applied to the escaped text, so
untrusted transcript content can never inject markup (an escaped `&lt;script&gt;` stays
inert). Covers what Claude prose actually uses: headings, lists, blockquotes, fenced and
inline code, bold/italic, links. Not a spec-complete Markdown — deliberately.
"""

import html
import re

_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?![\*\w])|(?<![_\w])_([^_\n]+)_(?![_\w])")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")


def _safe_href(url: str) -> str:
    low = url.strip().lower()
    if not low.startswith(("http://", "https://", "#", "/", "mailto:")):
        return "#"  # drop javascript:/data: and other schemes
    # The text was escaped with quote=False, so `"`/`'` in the URL survive and could
    # break out of the href="…" attribute — neutralize them (ampersands already encoded).
    return url.replace('"', "&quot;").replace("'", "&#x27;")


def _inline(text: str) -> str:
    """Inline formatting on already-escaped text. Code spans are protected first so
    their contents aren't reprocessed."""
    spans = []

    def stash(m):
        spans.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE.sub(stash, text)
    text = _LINK.sub(
        lambda m: (
            f'<a href="{_safe_href(m.group(2))}" target="_blank" rel="noopener">{m.group(1)}</a>'
        ),
        text,
    )
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text


def render_markdown(src: str) -> str:
    esc = html.escape(src or "", quote=False)
    lines = esc.split("\n")
    out = []
    i = 0
    n = len(lines)
    list_open = None  # "ul" | "ol" | None

    def close_list():
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            close_list()
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            out.append('<pre class="code">' + "\n".join(code) + "</pre>")
            continue

        if not stripped:
            close_list()
            i += 1
            continue

        # heading
        h = re.match(r"(#{1,6})\s+(.*)", stripped)
        if h:
            close_list()
            level = min(len(h.group(1)) + 2, 6)  # #->h3, so page h1 stays the slide title
            out.append(f"<h{level}>{_inline(h.group(2))}</h{level}>")
            i += 1
            continue

        # blockquote
        if stripped.startswith("&gt;"):
            close_list()
            body = re.sub(r"^&gt;\s?", "", stripped)
            out.append(f"<blockquote>{_inline(body)}</blockquote>")
            i += 1
            continue

        # list items
        ul = re.match(r"[-*+]\s+(.*)", stripped)
        ol = re.match(r"\d+\.\s+(.*)", stripped)
        if ul or ol:
            want = "ul" if ul else "ol"
            if list_open != want:
                close_list()
                out.append(f"<{want}>")
                list_open = want
            out.append(f"<li>{_inline((ul or ol).group(1))}</li>")
            i += 1
            continue

        # paragraph (gather consecutive plain lines)
        close_list()
        para = [stripped]
        i += 1
        while (
            i < n
            and lines[i].strip()
            and not re.match(r"(```|#{1,6}\s|[-*+]\s|\d+\.\s|&gt;)", lines[i].strip())
        ):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    close_list()
    return "\n".join(out)
