# preview-diff

Render a `git diff` (optionally with a description) to a self-contained HTML
file and open it in the browser. No dependencies — it builds a throwaway HTML
artifact in a temp dir and hands it to `open`. (Optionally serve it over HTTP
with `--serve`; see below.)

Handy for eyeballing a PR candidate locally before pushing, without a remote
branch or a running server. The page is a dark, GitHub-review-style surface: a
header with the refs and totals, a linked changed-files summary with per-file
add/del bars, and collapsible per-file sections with two-gutter line numbers.

`--review` goes a step further and makes it a review tool: leave comments on
lines and spans, then save a self-contained artifact you can send to someone.
Agents can produce the same artifact by POSTing their findings. See
[Review mode](#review-mode).

## Quick start

```bash
preview-diff                          # origin/main..HEAD in the current repo
preview-diff origin/main HEAD         # explicit base/head (two args)
preview-diff origin/main..HEAD        # same, git range syntax (A..B / A...B)
preview-diff HEAD~3                   # last 3 commits vs working HEAD
preview-diff --show                   # `git show HEAD` (a single commit)
preview-diff --show abc123            # `git show abc123`
preview-diff --serve origin/main HEAD # serve over HTTP, open an http:// URL
preview-diff --review origin/main HEAD # leave comments, save a review artifact
preview-diff --body pr-body.md --title "ci(pages): add workflow_dispatch" \
  -C ~/path/to/repo origin/main..HEAD # full PR preview, run against another repo
```

## Arguments

| Positional | Default | Meaning |
|---|---|---|
| `base` | `origin/main` if it resolves, else `HEAD~1` | ref to diff **from** |
| `head` | `HEAD` | ref to diff **to** |

A single arg containing `..` (or `...`) is treated as git range syntax, so
`preview-diff origin/main..HEAD` is equivalent to `preview-diff origin/main HEAD`.

| Option | Meaning |
|---|---|
| `--show` | render `git show <commit>` instead of a diff; the commit is the first positional (default: `HEAD`) |
| `--from-diff [f]` | render a diff you supply instead of generating one — from file `f`, or stdin if omitted or `-`; no repo required |
| `-C <dir>` | run git in this repo dir (default: cwd) |
| `--body <file>` | markdown/text shown above the diff (e.g. a PR description) |
| `--title <text>` | page title (default: `<base>..<head>`, or the commit for `--show`) |
| `-o <file>` | output HTML path (default: a temp file) |
| `--serve [port]` | serve the file over HTTP and open the `http://` URL instead of a `file://` path (default port auto-picked from 8137); blocks until Ctrl-C — see [Serving over HTTP](#serving-over-http) |
| `--review [port]` | review mode: the same page plus a comment overlay, saving a shareable review artifact — see [Review mode](#review-mode). Implies `--serve` |
| `--review-out <dir>` | save reviews here instead of the XDG location |
| `--no-open` | write the file but don't launch the browser (prints the path) |
| `-h`, `--help` | usage |

The output path is always printed to stdout, so you can pipe or re-open it.

## Scoping the diff

`--from-diff` renders any diff git can print, so scoping happens in git. Three
flags cover most of it, and they compose:

**`--diff-filter` — filter by file status.** Uppercase includes, lowercase
excludes: `M`odified, `A`dded, `D`eleted, `R`enamed, `C`opied. The big win is
a branch that added piles of docs, artifacts, or generated files:
`--diff-filter=M` cuts to just the edits to files that already existed —
usually the entire review (and rebase-conflict) surface. Works anywhere git
takes diff options (`git diff`, `git show`, `git log --stat`).

**Merge-base — isolate your changes on a stale branch.** When your branch is
behind, `git diff origin/main my-branch` renders every upstream commit you
lack as if you reverted it. Diff from the merge-base instead to see only what
you added:

```bash
base=$(git merge-base origin/main my-branch)
git diff "$base" my-branch
```

**Pathspec — drop generated files.** Layer paths after `--`:
`git diff BASE HEAD -- src tests` skips lockfiles, configs, and anything else
outside the listed paths.

Put together, piped through `--from-diff`:

```bash
# my edits to existing code, isolated from upstream drift, as HTML
base=$(git merge-base origin/main my-branch)
git diff --diff-filter=M "$base" my-branch -- src tests \
  | preview-diff --from-diff --title "my-branch — code changes"
```

`--from-diff` also renders a saved `.patch`, `gh pr diff` output, or
multi-commit `git show a b c`.

## The page

A self-contained dark HTML review surface:

- **Header** — the mode, title, `base → head` refs, `files / added / removed`
  totals, and the keybind hint. The `files` count opens the changed-files list.
- **Changed files** — one row per file with its `+/-` counts and a proportional
  add/del bar. Each row links to that file's section; **Expand all** / **Collapse
  all** fold every section at once. Collapsed by default, since on a wide change
  it pushes every diff below the fold.
- **Message / Description** — the commit body (`--show`) and the `--body`
  markdown, each collapsible and open by default.
- **Per-file sections** — a language-tagged, collapsible (`<details>`) card;
  click the header to fold it. Inside is a two-gutter (old / new line number)
  diff table with tinted add/del rows and highlighted `@@` hunks.

### Keys

The header hint lists whatever the current page binds — a key is only live when
the thing it acts on rendered, so a plain diff has no `n`/`p` and a page without
a `--body` has no `d`.

| key | |
| --- | --- |
| `j` / `k` | next / previous file |
| `o` | fold the marked file |
| `e` / `c` | expand / collapse every file |
| `f` | changed-files list |
| `m` / `d` | message / description |
| `g` / `G` | top / end |
| `n` / `p` | next / previous comment (review mode) |
| `r` | open the review box; `Esc` folds it again (live review only) |

The review box starts folded — the bar is fixed to the bottom of every screen
and most of a review is spent reading. Folding keeps whatever was typed and
flags it as a draft next to the comment count. `⌘/Ctrl+Enter` submits from the
review box, and saves from a line-comment editor.

## `--show` mode

`--show` renders a single commit via `git show` rather than a two-ref diff. The
commit comes from the first positional and defaults to `HEAD`. The header shows
the commit subject as the title, the short hash, `author · date`, and the commit
message body (pass `--title` to override the title). The file changes render
below as usual.

## Serving over HTTP

By default the artifact is a local `file://` handed to `open`. `--serve` instead
serves it with Python's built-in `http.server` (loopback only, still no external
deps) and opens an `http://127.0.0.1:PORT/` URL:

```bash
preview-diff --serve origin/main HEAD   # serve + open the browser
preview-diff --serve 9000 HEAD~1 HEAD   # pin the port (else auto-picked from 8137)
```

The point is a short, unambiguous URL. A `file://` path into the macOS temp dir
(`/var/folders/9c/…/preview-diff.XXXX.html`) is long and easily mangled — an agent
or chat client relaying it may abbreviate the middle to an ellipsis, and the
resulting "link" 404s. An `http://` URL sidesteps that entirely.

**It blocks.** The server runs in the foreground and holds the terminal until you
Ctrl-C (which stops the server). For non-interactive use — scripts, or an agent
that just wants the URL without hanging — pair it with `--no-open` and background
the call:

```bash
preview-diff --serve --no-open HEAD~1 HEAD &   # prints http://127.0.0.1:8137/ then serves
```

Bound to `127.0.0.1`, so the artifact stays off the network. Requires `python3`.

## Review mode

`--review` turns the page from something you read into something you mark up. It
renders the same diff, plus an overlay for leaving comments on a line or a span,
a general comment, and your name — then saves the whole thing as an artifact you
can hand to someone else.

```bash
preview-diff --review origin/main HEAD    # review the branch
preview-diff --review 9000 --show abc123  # review a single commit, pinned port
```

**Leaving comments.** Click a line number to comment on that line; shift-click a
second line number in the same file to stretch the comment across a span. The
editor takes ⌘/Ctrl+Enter to save and Esc to cancel. Saved comments render
inline under the code they cover, and carry an `×` to remove them. The bar along
the bottom holds your name, the review itself, and the submit button.

The review box is **markdown** — it renders as markdown in the saved artifact
and is what `review.md` carries, so write it the way you would write a PR
comment. Line comments are optional; a review can be prose alone, the way a
GitHub review with no inline comments is just its body.

**Submitting.** Submit writes a directory under
`$XDG_DATA_HOME/preview-diff/reviews/<id>/` (`~/.local/share/…` if `XDG_DATA_HOME`
is unset; `--review-out` overrides it):

| File | What it is |
|---|---|
| `review.md` | the review as a postable markdown comment |
| `review.json` | the review as data — reviewer, body, every line comment |
| `review.html` | the diff *and* the review, in one self-contained page |
| `source.diff` | the diff that was reviewed |

The id is `<timestamp>-<random>`, so reviews sort chronologically.

### review.md — the postable view

The review text is markdown and is saved verbatim as `review.md`, so it goes
straight to a PR:

```bash
gh pr comment 73 --body-file "$(ls -d ~/.local/share/preview-diff/reviews/* | tail -1)/review.md"
```

Any structure you give it is yours — severity tags, file:line references, code
fences. The tool models none of that; it carries the text through and appends a
provenance line. If the review also has line comments, they follow the body as a
short list rather than being dropped.

`review.md` is also what to point an agent at. It is the findings alone —
typically under a kilobyte, against ~100 KB for `review.html`, which is the same
review with the entire diff wrapped around it.

**Sharing.** `review.html` is the shareable piece: it's the same page with the
review baked in, rendered read-only, with no server and no network behind it.
Send the file and it opens standalone. Note this is a *file*, not a URL — the
`http://127.0.0.1:PORT` address only exists while the server runs, and only on
your machine. Submit prints the path (and offers a copy button) for that reason.

**Agents can drive it.** The overlay is one client of a plain HTTP endpoint;
anything that can POST is another. While the server runs, `POST /review`:

```bash
curl -X POST http://127.0.0.1:8137/review \
  -H 'Content-Type: application/json' -d '{
    "reviewer": "review-agent",
    "body": "General comments on the change.",
    "comments": [
      {"path": "src/app.py", "line": 42, "body": "This drops the error."},
      {"path": "src/app.py", "start_line": 10, "line": 14, "side": "old",
       "body": "A comment spanning the removed block."}
    ]}'
```

It replies with the saved paths:

```json
{"id": "20260727T213950-1e18c9", "dir": "…/reviews/20260727T213950-1e18c9",
 "review_json": "…/review.json", "review_html": "…/review.html",
 "url": "file://…/review.html", "comments": 2}
```

The comment shape deliberately mirrors GitHub's review API — `path`, `line`,
`start_line`, `side` (`new` or `old`, default `new`), `body` — so a review
written for one can be posted to the other. `line` is required and 1-based;
`start_line` is optional and makes it a span. A review needs at least one line
comment or a general `body`.

One extra optional field, `file_id` (`"file-3"`), names a specific file card.
It matters only for a diff where the same path appears more than once — a
multi-commit `git show a b c` renders each file once per commit — where `path`
alone is ambiguous. The overlay always records it; a comment without one takes
the first card for that path.

To get a review server without hanging the terminal, background it the same way
as `--serve`:

```bash
preview-diff --review --no-open origin/main HEAD &   # prints the URL, then serves
```

**How comments stay attached.** Each diff row carries `data-o` / `data-n` (its
old- and new-side line numbers) and each file card a `data-path`, so a comment
anchors to a `(path, side, line)` triple rather than to a pixel or a DOM index.
A comment that no longer matches any row — an agent citing a line that isn't in
the diff — is listed in the review header instead of being dropped silently.

The server is stdlib-only and bound to `127.0.0.1`. It accepts a submission only
with `Content-Type: application/json`, which keeps a hostile page in another tab
from posting to it.

**Untrusted review text.** A review can be written by someone else and is meant
to be opened by someone else, so the markdown is rendered through an allowlist:
only the tags and attributes markdown actually produces survive, and `href`/`src`
must be `http(s)`, `mailto:`, a fragment, a relative path, or a `data:image/`.
Everything else — `<script>`, `<form>`, `<svg>`, `action`, `formaction`, `on*`,
`id` — is dropped. `id` in particular, because comments anchor by element id and
review text must not be able to mint one.

Markdown images are kept, so a review containing `![](https://…)` will fetch
that URL when the artifact is opened. That is the one thing in the page that
reaches the network.

## Markdown description

When you pass `--body`, the text is rendered as **markdown** via `micromd`, a
tiny self-built renderer (~7KB minified). It's vendored pre-minified and
base64'd in `vendor/micromd.min.js.b64` and rides into the page as a
`data:text/javascript;base64,…` `<script>` — no network, no runtime build step,
still a self-contained `file://`. A small toggle button in the Description
header flips between the rendered view and the raw source. If the vendored blob
is missing, it falls back to showing the raw text.

To refresh the blob after changing the parser:

```bash
# from the micromd project: drop the ESM `export` (so it's a classic-script
# global), expose it on window, minify, then base64 onto one line.
{ sed 's/export function markdownToHtml/function markdownToHtml/' references/v_a1.js;
  echo 'window.markdownToHtml=markdownToHtml;'; } \
  | terser -c -m | base64 | tr -d '\n' \
  > .../preview-diff/vendor/micromd.min.js.b64
```

## Tests

```bash
just test    # or: uv run --with pytest --with playwright --directory preview-diff pytest -q
```

`test_review_server.py` covers the server — validation, the JSON-in-HTML
escaping, the saved bundle, the HTTP surface. `test_review_ui.py` drives the
overlay in a real browser, because the defects that surface there are not
reachable from the server: a comment anchored to the wrong side, a thread
landing on the wrong file card, review markdown rendering as literal text.
Each browser test starts its own `preview-diff --review` and tears it down.

The browser tests need the chromium binary once, and skip with a note if it is
missing rather than failing the suite:

```bash
uvx playwright install chromium
```

## Notes

- The diff is HTML-escaped, so `<`, `>`, `&` in code render as text.
- Dark theme only. The page itself is a local `file://` (or a loopback `http://` under `--serve`), so it is visible only on this machine. A `--review` artifact is self-contained and does travel — as a file, not a URL.
- Binary files and pure renames show a section header but an empty diff table.
- `open` on macOS, `xdg-open` on Linux.
