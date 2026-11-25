"""Shell syntax coloring for a run chip's command, done at build time so the deck
stays one self-contained file with no highlighter to ship.

It is a coloring pass, not a parser: anything it doesn't recognize comes out as
plain text, and every input character comes out exactly once (escaped), so the
worst a strange command can get is the wrong color.
"""

import html
import re

_TOKEN = re.compile(
    r"""
    (?P<str>\$?'[^']*'?|"(?:\\.|[^"\\])*"?)
   |(?P<arith>\$\(\((?:[^()]|\([^()]*\))*\)\))
   |(?P<var>\$\{[^}\n]*\}?|\$\(|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@#?$!*-])
   |(?P<herestr><<<)
   |(?P<here><<-?)
   |(?P<op>&&|\|\||[|;&]|[0-9]?>>?(?:&[0-9-])?|&>|<|[()`])
   |(?P<ws>[ \t]+|\n)
   |(?P<word>(?:\\.|[^\s'"$|;&<>()`\\])+|\\)
   |(?P<other>.)
    """,
    re.X | re.S,
)
_KEYWORDS = {
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "in", "function", "select",
}  # fmt: skip
# After these a new command starts, so its first word is the command name.
_CMD_AFTER_KW = {"if", "then", "else", "elif", "do", "while", "until", "!"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _span(cls: str, text: str) -> str:
    return f'<span class="t-{cls}">{html.escape(text, quote=False)}</span>'


def highlight(cmd: str) -> str:
    out = []
    pos, expect_cmd, prev_ws_ok = 0, True, True
    heredoc = None  # the delimiter of a heredoc whose body starts at the next newline
    want_delim = False
    while pos < len(cmd):
        m = _TOKEN.match(cmd, pos)
        kind, text = m.lastgroup, m.group()
        pos = m.end()
        if kind == "ws":
            out.append(text)
            if text == "\n":
                if heredoc:
                    # The body runs to a line holding only the delimiter; it's data
                    # (often a script in another language), so it stays plain.
                    end = re.compile(rf"^[ \t]*{re.escape(heredoc)}[ \t]*$", re.M).search(cmd, pos)
                    body_end = end.start() if end else len(cmd)
                    out.append(_span("here", cmd[pos:body_end]))
                    if end:
                        out.append(_span("op", cmd[end.start() : end.end()]))
                    pos = end.end() if end else len(cmd)
                    heredoc = None
                prev = cmd[m.start() - 1] if m.start() else ""
                if prev != "\\":
                    expect_cmd = True
            prev_ws_ok = True
            continue
        if kind == "word" and text.startswith("#") and prev_ws_ok:
            nl = cmd.find("\n", m.start())
            nl = len(cmd) if nl < 0 else nl
            out.append(_span("comment", cmd[m.start() : nl]))
            pos = nl
            continue
        prev_ws_ok = False
        if want_delim and kind in ("word", "str"):
            heredoc = text.strip("'\"")
            want_delim = False
            out.append(_span("op", text))
            continue
        if kind == "here":
            want_delim = True
            out.append(_span("op", text))
        elif kind == "herestr":
            out.append(_span("op", text))  # <<< feeds a word to stdin; no body follows
        elif kind == "arith":
            out.append(_span("var", text))  # $(( … )): a << in here is a shift
            expect_cmd = False
        elif kind == "op":
            out.append(_span("op", text))
            if text in ("&&", "||", "|", ";", "&", "(", "`"):
                expect_cmd = True
        elif kind == "var":
            out.append(_span("var", text))
            if text == "$(":
                expect_cmd = True
        elif kind == "str":
            out.append(_span("str", text))
            expect_cmd = False
        elif kind == "word":
            if text in _KEYWORDS and (expect_cmd or text in ("in", "do", "then", "done", "fi")):
                out.append(_span("kw", text))
                expect_cmd = text in _CMD_AFTER_KW
            elif expect_cmd and _ASSIGN.match(text):
                out.append(_span("var", text))  # FOO=bar before a command
            elif expect_cmd:
                out.append(_span("prog", text))
                expect_cmd = False
            elif text.startswith("-") and len(text) > 1:
                out.append(_span("flag", text))
            else:
                out.append(html.escape(text, quote=False))
        else:
            out.append(html.escape(text, quote=False))
    return "".join(out)
