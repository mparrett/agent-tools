# htmldiff

Compare two web pages and see what changed — either as a **rendered redline**,
with the changes marked up in the page itself, or as a **source diff**, side by
side or unified.

It is one HTML file. No build step, no dependencies, no server, no network. Open
`htmldiff.html` in a browser and it works, including from a USB stick or an
airgapped box.

```sh
open htmldiff/htmldiff.html                            # just open it
node htmldiff/cli.mjs                                  # serve it, and type URLs in the page
node htmldiff/cli.mjs https://ex.com/a https://ex.com/b
./htmldiff/htmldiff old.html new.html                  # the no-Node path (uv)
```

## Three ways to run it

| | Fetches URLs | Needs |
|---|---|---|
| Open `htmldiff.html` | no — paste or drop files | a browser |
| `node htmldiff/cli.mjs` | **yes, from inside the page** | Node 18+ |
| `./htmldiff` (the uv script) | yes, at the command line | uv |

The three share one page; only the way inputs reach it differs. Opened as a
file it cannot fetch anything, so it takes pasted text and dropped files. The
two launchers add fetching, and the Node one goes further — see below.

## Why both views

A source diff tells you a `<strong>` tag moved. A rendered diff tells you the
price went up. Most of the time you want the second, and every text differ gives
you the first.

The rendered view is a port of [htmldiff.net][] (itself a port of
[myobie/htmldiff][]): a word-level diff that understands tags, so its output is
still valid markup with the changes wrapped in `<ins>` and `<del>`. The source
views are an ordinary line diff with word-level highlighting inside changed
lines, in the shape [Diffchecker][] made familiar.

[htmldiff.net]: https://github.com/Rohland/htmldiff.net
[myobie/htmldiff]: https://github.com/myobie/htmldiff
[Diffchecker]: https://www.diffchecker.com/

## Reading the colours

Deletions are oxblood and struck through; insertions are ink blue and
underlined. That is the editorial proofing convention `<ins>`/`<del>` come from,
and unlike the git-standard red/green it stays legible with red-green colour
blindness.

The strip down the right edge is the **change spine**: every change in the whole
document, drawn where it falls, deletions striking left of the axis and
insertions right. It is a map of what changed and a scrollbar you can read.
Click a tick to jump to it, or click anywhere on the strip to scroll there.

## Controls

| | |
|---|---|
| `⌘`/`Ctrl` + `Enter` | run the diff |
| `n` / `j`, `p` / `k` | next / previous change |
| `r` | re-fetch both URLs and re-diff (served mode) |
| `Esc` | back to the inputs |
| drag and drop | load a file into either pane |

**Ignore whitespace** treats every run of whitespace as equal. **Ignore case**
does the same for capitalisation. **Group** takes a regular expression and diffs
anything matching it as a single unit rather than word by word — the case it
exists for is a date, where `1 Jan 2016` → `22 Feb 2017` otherwise reads as three
unrelated edits. Try `[\d]{1,2}\s*\w+\s*[\d]{4}` on the sample.

**Save report** writes the rendered redline out as its own self-contained HTML
file, which is the thing to send to someone who asked what changed. It carries
the same `default-src 'none'` CSP as the preview — a report is opened outside
any sandbox, and unchanged regions of the diff pass through verbatim, so a
`<script>` present in both documents would otherwise be live in the file you
just mailed someone.

## Comparing two URLs

The page cannot fetch them itself. A cross-origin GET from a `file://` document
is blocked, and no amount of client-side code gets around that — which is why
every browser-based differ makes you paste. Both launchers work around it, in
different places.

### Node — the page does the fetching

`node htmldiff/cli.mjs` serves the page from `127.0.0.1` with a fetch proxy
beside it. Same origin, so the block does not apply, and the page grows two URL
fields:

```sh
node htmldiff/cli.mjs                        # serve and open; type URLs in the page
node htmldiff/cli.mjs https://a https://b    # same, opened on the diff
node htmldiff/cli.mjs --static a.html b.html # no server: write a plain file:// page
```

Type a URL into each side and press Enter. The payoff is **Refresh** (or `r`):
it re-pulls both sides and re-diffs, so you can watch a staging box through a
deploy without retyping anything or going back to the shell.

The proxy is an SSRF surface — any page you have open can reach `127.0.0.1`. It
binds to loopback only, requires a per-run random token that only the served
page is given, sends no CORS headers so no other origin could read a reply
anyway, and rejects requests carrying a cross-site `Origin`. Private and
link-local targets are deliberately **not** blocked: diffing a staging box
against production is the main reason this exists.

The package has no dependencies — `npx` should not pull a tree to show a diff.

### The uv script — fetches at the command line

For machines with uv but no Node. It GETs both URLs, keeps the bodies in temp
files, and opens a standalone page seeded with them:

```console
$ htmldiff https://www.example.com/pricing https://staging.example.com/pricing
fetched https://www.example.com/pricing → /tmp/htmldiff-ab12/original.html (48,211 bytes)
fetched https://staging.example.com/pricing → /tmp/htmldiff-ab12/changed.html (48,904 bytes)
```

The two temp files are what got diffed, kept so you can look at them. The page
shows which URL is which side, since two URLs off the same site are otherwise
indistinguishable once the markup is on screen.

| Option | |
|---|---|
| `-H 'Name: value'` | extra request header, repeatable — cookies, auth, an override `User-Agent` |
| `--user-agent` | override the default (a browser UA; urllib's default gets refused or served a stub, which reads as a change that isn't there) |
| `--timeout`, `--max-bytes` | 20s and 8 MB. An oversize response is refused, not truncated — a truncated body diffs as if the page ended there |
| `-k`, `--insecure` | skip TLS checks, for a staging host with a self-signed cert |
| `-o`, `--no-open` | write the page somewhere durable / don't launch a browser |

The same options exist on the Node CLI, plus `-p/--port` and `--static`. Both
follow redirects and record the **final** URL, which becomes the frame's
`<base>`, so relative asset paths resolve when you turn remote assets on.

Redirects and `-H` are worth one warning. `Cookie` and `Authorization` are
dropped when a redirect leaves the origin you aimed at; **any other header you
pass is not**, so a custom auth header — `X-Api-Key` and friends, the usual
shape for the staging boxes this exists to diff — arrives at wherever the
redirect points. This matches `curl`, and is the sort of thing to know before
pointing `-H` at a host you do not control.

The two launchers overlap on purpose, and that is a maintenance cost worth
knowing about: a change to the seeding contract has to be made in both. Use the
Node one unless Node is the thing you do not have.

### Publishing

The package is not on npm yet: it is `"private": true` with a `prepublishOnly`
that refuses, because it is still being dogfooded. Once it is published,
`npx @mparrett/htmldiff` does what `node htmldiff/cli.mjs` does today. Publishing means removing both, then
`npm publish`. Note that `npm publish --dry-run` does **not** check `private`,
so it cannot confirm the guard — the `prepublishOnly` script is the part that
fires either way.

**Minified pages are a poor fit for the source views.** A page served as one
long line is one row in the side-by-side, and while the word-level highlights
inside it are still correct and change-to-change navigation still works, it is a
wall of text. Use the rendered view for those, or run the files through a
formatter first.

## Safety and privacy

The pasted pages render in an iframe with no `allow-scripts`, so nothing in them
executes, and a `default-src 'none'` CSP inside that frame blocks every remote
image, stylesheet and font. A page you are diffing cannot phone home, and no
tracker in it learns you looked. **Load remote assets** relaxes the CSP when you
actually want the images — in the preview only; a saved report stays locked
down, because it is opened outside the sandbox.

A report gets one thing the preview does not need: `<meta http-equiv=refresh>`
is stripped out of it. CSP governs what a page *fetches*, not where it
navigates, and a saved report has no `sandbox` attribute to fall back on — so a
refresh tag in a diffed page would send whoever opened the report to its URL.
The preview suppresses the same tag through the frame's sandbox.

Opened as a file, the page never makes a request — there is no code in it that
can. Under the Node launcher it talks to the local proxy and nothing else. Either way a
fetch only happens because you asked for one, and then it is an ordinary GET
from your machine, with your IP and whatever headers you passed. Nothing is
uploaded anywhere; there is nothing to upload to.

Your last two inputs and your preferences are kept in `localStorage` so a reload
doesn't lose your work — **Clear** empties both panes.

## Where it differs from upstream

The algorithm is a faithful port: the whole upstream C# spec suite runs against
this page in `test_htmldiff.py` and passes. Two deliberate departures:

- **Unicode words.** Upstream compiles its word-character class in ECMAScript
  mode, so `\w` is ASCII and `café` tokenizes as `caf` + `é` — every accented
  word reads as a change. Unicode letters count here, minus the CJK blocks,
  which upstream splits per character and which genuinely do diff better that
  way, having no spaces to tokenize on.
- **Overlapping group expressions** take the first match instead of raising.
  Upstream's caller writes the regex once in code; here it is a text field, and
  half-typed regexes overlap constantly.

Known limitation, inherited: **attribute changes are not diffed.** Attributes
are stripped before matching, so `<a href="/a">` → `<a href="/b">` shows as no
change in the rendered view. The source views show it. `<img>` is the exception
— it is treated as a word, so a swapped image is a visible replacement.

## Tests

```sh
uv run htmldiff/test_htmldiff.py       # or: just test
uvx playwright install chromium        # once, if the browser is missing
```

The page has no build step, so the tests drive it in a real browser over
`file://` and call into `window.htmldiff`. The algorithm cases are lifted
verbatim from upstream's `HtmlDiffSpecTests.cs` — they are the contract the port
has to meet, and a mistranslated tokenizer shows up there as a wrong string
rather than as slightly-off output nobody notices.

The served-mode tests start the real `cli.mjs` and drive the page against a
local HTTP server, so the suite never reaches the internet. They skip if `node`
is not installed.
