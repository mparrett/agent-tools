"""Shell coloring for run chips: lossless, and the classes land on the right tokens."""

import html
import re

import pytest
from claude_reel.term import highlight


def _plain(h: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", h))


def _classes(h: str) -> list[tuple[str, str]]:
    return [
        (c, html.unescape(t))
        for c, t in re.findall(r'<span class="t-([a-z]+)">(.*?)</span>', h, re.S)
    ]


@pytest.mark.parametrize(
    "cmd",
    [
        "git log --oneline -3 | head -1",
        'cd ~/x && FOO=1 just test 2>&1 | tail -1; echo "$HOME ${X:-y}" $(date +%s)',
        "python3 - <<'EOF'\nprint('<b>&amp;')\nx = \"$NOT_A_VAR\"\nEOF\nls -la",
        "for f in a b; do echo $f; done  # trailing comment",
        "echo 'unterminated",
        'echo "a \\" b" \\\n  --next-line',
        'a=$(( 1 + 2 )); [ -n "$a" ] && echo `whoami` &> /dev/null',
        "",
    ],
)
def test_highlight_is_lossless(cmd):
    assert _plain(highlight(cmd)) == cmd


def test_tokens_get_their_classes():
    got = _classes(highlight("FOO=1 rg -n 'x y' \"$HOME\" | head -3 && echo $USER # done"))
    assert ("var", "FOO=1") in got and ("prog", "rg") in got and ("flag", "-n") in got
    assert ("str", "'x y'") in got and ("op", "|") in got and ("prog", "head") in got
    assert ("op", "&&") in got and ("prog", "echo") in got and ("var", "$USER") in got
    assert ("comment", "# done") in got


def test_a_heredoc_body_is_plain_data():
    got = _classes(highlight("python3 - <<'EOF'\nimport sys  # not shell\nx = 'q'\nEOF\nls"))
    assert ("here", "import sys  # not shell\nx = 'q'\n") in got
    assert ("op", "EOF") in got and ("prog", "ls") in got
    assert not any(c == "comment" for c, _ in got)


def test_keywords_and_the_command_after_them():
    got = _classes(highlight("for f in *.py; do wc -l $f; done"))
    assert ("kw", "for") in got and ("kw", "in") in got and ("kw", "do") in got
    assert ("prog", "wc") in got and ("kw", "done") in got


def test_a_hash_inside_a_word_is_not_a_comment():
    got = _classes(highlight("curl https://x.test/#frag"))
    assert not any(c == "comment" for c, _ in got)
