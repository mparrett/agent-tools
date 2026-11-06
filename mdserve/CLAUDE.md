# mdserve

## Coverage gaps

**mdserve's suite is characterization, not specification.** It's where the
incidental complexity pooled (found in an audit), so `test_mdserve.py` pins what the code
*currently does* — including the defects deliberately asserted as-is, each
with a docstring saying so. A test failing there may mean you fixed one; read
the docstring before "correcting" the assertion.

The one to know about: **mdserve renders markdown with `escape=False`**, so a
`<script>`, an `onerror=` handler, or a `javascript:` href in a converted file
is still live markup in the output. Converting a directory of files you didn't
write is the tool's whole job, so this is a real exposure, not a theoretical
one.

What it can *do* is now constrained at the other end: generated pages carry a
CSP whose `script-src` is a per-run nonce, so an injected `<script>` and an
inline handler are both refused at execution. The nonce is one per process
rather than per response, which is only sound because every page is rewritten
at startup — if conversion ever becomes incremental, or a file watcher
re-converts inside a running process, that assumption dies and the nonce has to
move with it. Frames are refused too, deliberately and not by omission
(`frame-src` falls through to `default-src 'none'`): an `<iframe>` pointing at
an arbitrary origin is a phishing surface rather than content. `media-src` is
open, like `img-src`.

So what remains of the passthrough defect is markup-level, not script: injected
HTML can still deface a page.

**Per-file data goes in a JSON island, and must stay there.** `json_island`
writes the navigation object and the file's raw text into a
`<script type="application/json">` block with `&<>` escaped as `\uXXXX`. This
is not a style preference. The data used to ride in a JS template literal
escaped by hand, and an HTML parser ends a script element at the first literal
`</script>` whatever the JS string context — so a document containing that
sequence truncated the page's own script and took the navigation and source
view with it. No amount of escaping for *JavaScript* can fix that; escaping for
the *HTML parser* is the fix. Interpolating file content back into executable
JS reopens it, and only a browser can see that it is broken — which is what
`test_a_script_close_in_a_file_leaves_the_page_working` is for.

The remaining two: `find_markdown_files` drops any file whose *name merely
contains* an ignored word (`distributed.md`, `tmpfile.md`), and
`parse_flexible_args`' advertised port/directory swap is unreachable behind
`type=int`.
