#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pytest", "mistune>=3.0.0"]
# ///
"""Tests for mdfeed (viewer) and mdfeed_write (PostToolUse hook).

Run via `uv run mdfeed/test_mdfeed.py` (or `just test`).
"""

import base64
import http.server
import io
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import mdfeed_write
import pytest

import mdfeed


def write_feed(feed: Path, entries):
    feed.parent.mkdir(parents=True, exist_ok=True)
    with open(feed, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ── load_feed ────────────────────────────────────────────────────────────────


def test_load_feed_missing_file(tmp_path):
    assert mdfeed.load_feed(tmp_path / "nope.jsonl") == []


def test_load_feed_dedupes_by_path_newest_wins(tmp_path):
    feed = tmp_path / "index.jsonl"
    write_feed(
        feed,
        [
            {"ts": 1.0, "path": "/a.md", "tool": "Write"},
            {"ts": 3.0, "path": "/b.md", "tool": "Write"},
            {"ts": 2.0, "path": "/a.md", "tool": "Edit"},
        ],
    )
    entries = mdfeed.load_feed(feed)
    assert [e["path"] for e in entries] == ["/b.md", "/a.md"]  # newest first
    assert entries[1]["tool"] == "Edit"  # ts=2 beat ts=1


def test_load_feed_skips_junk_lines(tmp_path):
    feed = tmp_path / "index.jsonl"
    feed.write_text(
        "not json\n"
        "\n"
        '{"ts": 1.0}\n'  # no path key
        '{"ts": 1.0, "path": "/ok.md"}\n'
    )
    entries = mdfeed.load_feed(feed)
    assert [e["path"] for e in entries] == ["/ok.md"]


def test_load_feed_caps_entries(tmp_path):
    feed = tmp_path / "index.jsonl"
    n = mdfeed.MAX_ENTRIES + 5
    write_feed(feed, [{"ts": float(i), "path": f"/f{i}.md"} for i in range(n)])
    entries = mdfeed.load_feed(feed)
    assert len(entries) == mdfeed.MAX_ENTRIES
    assert entries[0]["path"] == f"/f{n - 1}.md"  # newest kept


def test_load_feed_flags_missing_files(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    write_feed(
        feed,
        [
            {"ts": 1.0, "path": str(real)},
            {"ts": 2.0, "path": str(tmp_path / "gone.md")},
        ],
    )
    by_path = {e["path"]: e["exists"] for e in mdfeed.load_feed(feed)}
    assert by_path[str(real)] is True
    assert by_path[str(tmp_path / "gone.md")] is False


# ── hook: should_skip ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel,skip",
    [
        ("proj/notes.md", False),
        ("proj/notes.markdown", False),
        ("proj/notes.txt", True),
        ("proj/CLAUDE.md", True),
        ("proj/node_modules/pkg/README.md", True),
        ("proj/.git/info.md", True),
        (".claude/whatever.md", True),  # under ~/.claude → skip
        (".claude/plans/big-plan.md", False),  # …except plans
    ],
)
def test_should_skip(tmp_path, rel, skip):
    home = tmp_path
    assert mdfeed_write.should_skip(home / rel, home) is skip


# ── project / category derivation ────────────────────────────────────────────


def test_derive_project_prefers_file_git_root(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    f = repo / "docs" / "x.md"
    assert mdfeed_write.derive_project(f, "") == "myrepo"


def test_derive_project_falls_back_to_cwd_git_root(tmp_path):
    cwd_repo = tmp_path / "cwdrepo"
    (cwd_repo / ".git").mkdir(parents=True)
    outside = tmp_path / "plans" / "p.md"
    assert mdfeed_write.derive_project(outside, str(cwd_repo / "sub")) == "cwdrepo"


def test_derive_project_falls_back_to_cwd_basename(tmp_path):
    outside = tmp_path / "plans" / "p.md"
    assert mdfeed_write.derive_project(outside, str(tmp_path / "somedir")) == "somedir"


def test_derive_project_no_cwd_uses_path_parents(tmp_path):
    f = tmp_path / "a" / "b" / "x.md"
    assert mdfeed_write.derive_project(f, "") == "a/b"


@pytest.mark.parametrize(
    "name,category",
    [
        ("plan.md", "plan"),
        ("PLAN.md", "plan"),
        ("plan.v2-final.md", "plan"),
        ("planning.md", "note"),
        ("notes.md", "note"),
    ],
)
def test_derive_category_by_basename(tmp_path, name, category):
    assert mdfeed_write.derive_category(tmp_path / "proj" / name, tmp_path) == category


def test_derive_category_plans_dir(tmp_path):
    assert mdfeed_write.derive_category(tmp_path / ".claude" / "plans" / "x.md", tmp_path) == "plan"


def test_hook_and_viewer_share_derivations():
    """The derivation logic once lived (and drifted) in both files; it must
    stay the single shared implementation from feedlib."""
    assert mdfeed.derive_project is mdfeed_write.derive_project
    assert mdfeed.derive_category is mdfeed_write.derive_category


# ── append_entries (the `add` subcommand's engine) ───────────────────────────


def test_append_entries_adds_markdown(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    added, errors = mdfeed.append_entries(feed, [doc])
    assert (added, errors) == (1, [])
    (entry,) = mdfeed.load_feed(feed)
    assert entry["path"] == str(doc.resolve())
    assert entry["tool"] == "Add"
    assert entry["category"] == "note"


def test_append_entries_rejects_non_markdown(tmp_path):
    txt = tmp_path / "doc.txt"
    txt.write_text("hi")
    feed = tmp_path / "index.jsonl"
    added, errors = mdfeed.append_entries(feed, [txt, tmp_path / "missing.md", tmp_path])
    assert added == 0
    assert len(errors) == 3
    assert mdfeed.load_feed(feed) == []


# ── hook main(): stdin → feed line ───────────────────────────────────────────


def run_hook(monkeypatch, tmp_path, payload):
    feed = tmp_path / "feed" / "index.jsonl"
    monkeypatch.setattr(mdfeed_write, "FEED_PATH", feed)
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    monkeypatch.setattr(sys, "stdin", stdin)
    mdfeed_write.main()
    if not feed.exists():
        return []
    return [json.loads(line) for line in feed.read_text().splitlines()]


def test_hook_logs_md_write(tmp_path, monkeypatch):
    doc = tmp_path / "proj" / "notes.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# hi\n")
    lines = run_hook(
        monkeypatch,
        tmp_path,
        {"tool_name": "Write", "tool_input": {"file_path": str(doc)}, "cwd": str(doc.parent)},
    )
    assert len(lines) == 1
    assert lines[0]["path"] == str(doc)
    assert lines[0]["tool"] == "Write"


def test_hook_resolves_relative_paths(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    lines = run_hook(
        monkeypatch,
        tmp_path,
        {"tool_name": "Edit", "tool_input": {"file_path": "notes.md"}, "cwd": str(proj)},
    )
    assert len(lines) == 1
    assert lines[0]["path"] == str((proj / "notes.md").resolve())


@pytest.mark.parametrize(
    "payload",
    [
        "",  # empty stdin
        "not json at all",  # garbage
        {"tool_name": "Read", "tool_input": {"file_path": "/x.md"}},  # unwatched tool
        {"tool_name": "Write", "tool_input": {}},  # no file_path
        {"tool_name": "Write", "tool_input": {"file_path": "/proj/CLAUDE.md"}},  # skipped name
    ],
)
def test_hook_ignores_non_events(tmp_path, monkeypatch, payload):
    assert run_hook(monkeypatch, tmp_path, payload) == []


def test_hook_never_raises_on_unwritable_feed(tmp_path, monkeypatch):
    blocker = tmp_path / "feed"
    blocker.write_text("a file where the feed dir should be")
    monkeypatch.setattr(mdfeed_write, "FEED_PATH", blocker / "index.jsonl")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/x/y.md"}})),
    )
    mdfeed_write.main()  # must swallow the OSError, not raise


# ── HTTP endpoints: allowlist is the security boundary ───────────────────────

DOC_BODY = "# Title\n\nhello *world*\n"


# ── frontmatter ──────────────────────────────────────────────────────────────


def test_frontmatter_split_off_and_rendered_as_metadata():
    fm_html, body = mdfeed.render_frontmatter("---\ntitle: My Page\n---\n\n# Body\n")
    assert "<details" in fm_html and "title: My Page" in fm_html
    assert body == "\n# Body\n"


def test_frontmatter_values_are_escaped():
    fm_html, _ = mdfeed.render_frontmatter("---\ntitle: <script>x</script>\n---\n\nhi\n")
    assert "<script>" not in fm_html
    assert "&lt;script&gt;" in fm_html


def test_document_without_frontmatter_is_untouched():
    text = "# Body\n\nhello\n"
    assert mdfeed.render_frontmatter(text) == ("", text)


def test_thematic_break_later_in_document_is_not_frontmatter():
    text = "# Body\n\n---\n\nmore\n"
    assert mdfeed.render_frontmatter(text) == ("", text)


def test_unterminated_frontmatter_is_left_alone():
    # A stray opening fence must not swallow the rest of the file.
    text = "---\ntitle: no close\n\n# Body\n"
    assert mdfeed.render_frontmatter(text) == ("", text)


def test_frontmatter_only_document_without_trailing_newline():
    fm_html, body = mdfeed.render_frontmatter("---\ntitle: x\n---")
    assert "title: x" in fm_html
    assert body == ""


def test_empty_frontmatter_is_consumed_but_renders_nothing():
    # A generated page can open with `---\n---` to opt into a pipeline without
    # saying anything. There is no metadata to show, but the block still has to
    # go: left in the body, both fences reach mistune as thematic breaks.
    fm_html, body = mdfeed.render_frontmatter("---\n---\n\n# Body\n")
    assert fm_html == ""
    assert body == "\n# Body\n"
    assert "<hr" not in mdfeed._MD(body)


def test_frontmatter_holding_only_blank_lines_renders_nothing():
    # Same case, and an empty <details> labelled "metadata" would be worse than
    # nothing.
    assert mdfeed.render_frontmatter("---\n\n---\n\n# Body\n") == ("", "\n# Body\n")


def test_empty_frontmatter_document_without_trailing_newline():
    assert mdfeed.render_frontmatter("---\n---") == ("", "")


def test_api_render_shows_frontmatter_as_metadata(tmp_path):
    # End to end: the block reaches the browser as metadata, not as a heading.
    doc = tmp_path / "fm.md"
    doc.write_text("---\ntitle: Meta\n---\n\n# Real Title\n")
    feed = tmp_path / "index.jsonl"
    mdfeed.append_entries(feed, [doc])
    old_feed = mdfeed.FeedHandler.feed_path
    mdfeed.FeedHandler.feed_path = feed
    srv = mdfeed.ThreadingServer(("127.0.0.1", 0), mdfeed.FeedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        url = base + "/api/render?path=" + urllib.parse.quote(str(doc.resolve()))
        status, _, body = get(url)
        assert status == 200
        rendered = json.loads(body)["html"]
        assert "<h2>title: Meta</h2>" not in rendered
        assert "<details" in rendered
        assert "<h1>Real Title</h1>" in rendered
    finally:
        srv.shutdown()
        srv.server_close()
        mdfeed.FeedHandler.feed_path = old_feed


@pytest.fixture
def server(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(DOC_BODY)
    feed = tmp_path / "index.jsonl"
    mdfeed.append_entries(feed, [doc])
    old_feed = mdfeed.FeedHandler.feed_path
    mdfeed.FeedHandler.feed_path = feed
    srv = mdfeed.ThreadingServer(("127.0.0.1", 0), mdfeed.FeedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", str(doc.resolve())
    finally:
        srv.shutdown()
        srv.server_close()
        mdfeed.FeedHandler.feed_path = old_feed


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_index_served(server):
    base, _ = server
    status, headers, body = get(base + "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"mdfeed" in body


def test_api_feed(server):
    base, doc = server
    status, _, body = get(base + "/api/feed")
    assert status == 200
    (entry,) = json.loads(body)
    assert entry["path"] == doc
    assert entry["exists"] is True


def test_api_render_feed_path(server):
    base, doc = server
    status, _, body = get(base + "/api/render?path=" + urllib.parse.quote(doc))
    assert status == 200
    j = json.loads(body)
    assert "<h1>" in j["html"]
    assert "raw" not in j  # only included with ?raw=1


def test_api_render_raw_param(server):
    base, doc = server
    status, _, body = get(base + "/api/render?raw=1&path=" + urllib.parse.quote(doc))
    assert status == 200
    assert json.loads(body)["raw"] == DOC_BODY


@pytest.mark.parametrize("endpoint", ["/api/render", "/raw"])
def test_paths_outside_feed_are_forbidden(server, endpoint, tmp_path):
    base, _ = server
    outside = tmp_path / "secret.md"
    outside.write_text("not in feed")
    for target in ["/etc/passwd", str(outside)]:
        status, _, _ = get(base + endpoint + "?path=" + urllib.parse.quote(target))
        assert status == 403, f"{endpoint} must reject {target}"


@pytest.mark.parametrize("endpoint", ["/api/render", "/raw"])
def test_missing_path_param_is_400(server, endpoint):
    base, _ = server
    status, _, _ = get(base + endpoint)
    assert status == 400


def test_raw_is_text_plain(server):
    base, doc = server
    status, headers, body = get(base + "/raw?path=" + urllib.parse.quote(doc))
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert body.decode() == DOC_BODY


def test_unknown_route_404(server):
    base, _ = server
    assert get(base + "/nope")[0] == 404


# ── compact ──────────────────────────────────────────────────────────────────


def test_compact_dedupes_and_drops_junk(tmp_path):
    feed = tmp_path / "index.jsonl"
    feed.write_text(
        '{"ts": 1.0, "path": "/a.md", "tool": "Write"}\n'
        "garbage line\n"
        '{"ts": 2.0, "path": "/a.md", "tool": "Edit"}\n'
        '{"ts": 3.0, "path": "/b.md", "tool": "Write"}\n'
    )
    visible_before = mdfeed.load_feed(feed)
    before, after, _ = mdfeed.compact_feed(feed)
    assert (before, after) == (4, 2)
    assert mdfeed.load_feed(feed) == visible_before  # lossless for the viewer
    lines = [json.loads(line) for line in feed.read_text().splitlines()]
    assert [e["ts"] for e in lines] == [2.0, 3.0]  # oldest-first, newest entry per path


def test_compact_keep_cap(tmp_path):
    feed = tmp_path / "index.jsonl"
    write_feed(feed, [{"ts": float(i), "path": f"/f{i}.md"} for i in range(10)])
    before, after, _ = mdfeed.compact_feed(feed, keep=3)
    assert (before, after) == (10, 3)
    assert [e["path"] for e in mdfeed.load_feed(feed)] == ["/f9.md", "/f8.md", "/f7.md"]


def test_compact_prune_missing(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    write_feed(
        feed,
        [
            {"ts": 1.0, "path": str(real)},
            {"ts": 2.0, "path": str(tmp_path / "gone.md")},
        ],
    )
    _, after, _ = mdfeed.compact_feed(feed, prune_missing=True)
    assert after == 1
    assert [e["path"] for e in mdfeed.load_feed(feed)] == [str(real)]


def test_compact_backs_up_by_default(tmp_path):
    feed = tmp_path / "index.jsonl"
    original = '{"ts": 1.0, "path": "/a.md"}\n{"ts": 2.0, "path": "/a.md"}\n'
    feed.write_text(original)
    _, _, bak_path = mdfeed.compact_feed(feed)
    assert bak_path == tmp_path / "index.jsonl.bak"
    assert bak_path.read_text() == original
    assert len(feed.read_text().splitlines()) == 1


def test_compact_no_backup(tmp_path):
    feed = tmp_path / "index.jsonl"
    write_feed(feed, [{"ts": 1.0, "path": "/a.md"}])
    _, _, bak_path = mdfeed.compact_feed(feed, backup=False)
    assert bak_path is None
    assert not (tmp_path / "index.jsonl.bak").exists()


def test_compact_dry_run_writes_nothing(tmp_path):
    feed = tmp_path / "index.jsonl"
    original = '{"ts": 1.0, "path": "/a.md"}\n{"ts": 2.0, "path": "/a.md"}\n'
    feed.write_text(original)
    before, after, bak_path = mdfeed.compact_feed(feed, dry_run=True)
    assert (before, after) == (2, 1)  # numbers reported…
    assert feed.read_text() == original  # …but the feed is untouched
    assert bak_path is None
    assert not (tmp_path / "index.jsonl.bak").exists()


def test_compact_rejects_nonpositive_keep(tmp_path):
    feed = tmp_path / "index.jsonl"
    original = '{"ts": 1.0, "path": "/a.md"}\n'
    feed.write_text(original)
    # A negative keep would hit negative-slice semantics and empty the feed.
    for bad in (0, -1000):
        with pytest.raises(ValueError):
            mdfeed.compact_feed(feed, keep=bad)
    assert feed.read_text() == original
    with pytest.raises(SystemExit):
        mdfeed.build_parser().parse_args(["compact", "--keep", "-1000"])


def test_compact_then_append_composes(tmp_path):
    """The in-place rewrite must leave a file the hook/add path can extend."""
    feed = tmp_path / "index.jsonl"
    write_feed(feed, [{"ts": 1.0, "path": "/a.md"}, {"ts": 2.0, "path": "/a.md"}])
    mdfeed.compact_feed(feed)
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    added, errors = mdfeed.append_entries(feed, [doc])
    assert (added, errors) == (1, [])
    assert [e["path"] for e in mdfeed.load_feed(feed)] == [str(doc.resolve()), "/a.md"]


def test_cmd_compact_missing_feed(tmp_path):
    args = mdfeed.build_parser().parse_args(["--feed", str(tmp_path / "nope.jsonl"), "compact"])
    assert mdfeed.cmd_compact(args) == 1


def test_cli_compact_flags():
    args = mdfeed.build_parser().parse_args(
        ["compact", "--keep", "50", "--prune-missing", "--dry-run"]
    )
    assert (args.keep, args.prune_missing, args.dry_run, args.no_backup) == (50, True, True, False)
    assert args.func is mdfeed.cmd_compact


# ── feed parse cache ─────────────────────────────────────────────────────────


def test_feed_cache_skips_reparse_until_file_changes(tmp_path, monkeypatch):
    feed = tmp_path / "index.jsonl"
    write_feed(feed, [{"ts": 1.0, "path": "/a.md"}])
    calls = []
    orig = mdfeed._dedupe_entries
    monkeypatch.setattr(mdfeed, "_dedupe_entries", lambda lines: (calls.append(1), orig(lines))[1])
    assert len(mdfeed.load_feed(feed)) == 1
    assert len(mdfeed.load_feed(feed)) == 1
    assert len(calls) == 1  # second read served from cache
    with open(feed, "a", encoding="utf-8") as f:  # hook-style append
        f.write('{"ts": 2.0, "path": "/b.md"}\n')
    assert [e["path"] for e in mdfeed.load_feed(feed)] == ["/b.md", "/a.md"]
    assert len(calls) == 2  # change invalidated the cache


def test_feed_cache_exists_flag_stays_fresh(tmp_path):
    """exists is re-stat'd per call — a file can vanish without a feed write."""
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    write_feed(feed, [{"ts": 1.0, "path": str(doc)}])
    assert mdfeed.load_feed(feed)[0]["exists"] is True
    doc.unlink()  # no feed write, so the parse cache is still warm
    assert mdfeed.load_feed(feed)[0]["exists"] is False


# ── CLI parsing ──────────────────────────────────────────────────────────────


def test_cli_flags_after_subcommand():
    args = mdfeed.build_parser().parse_args(["serve", "--port", "8050", "--no-browser"])
    assert (args.port, args.no_browser, args.host) == (8050, True, "127.0.0.1")
    assert args.func is mdfeed.cmd_serve


def test_cli_flags_before_subcommand_still_work():
    args = mdfeed.build_parser().parse_args(["--port", "8050", "serve"])
    assert args.port == 8050  # subparser default must not clobber this


def test_cli_feed_flag_on_add_and_list():
    args = mdfeed.build_parser().parse_args(["add", "--feed", "/tmp/f.jsonl", "a.md"])
    assert (args.feed, args.paths) == ("/tmp/f.jsonl", ["a.md"])
    args = mdfeed.build_parser().parse_args(["--feed", "/tmp/f.jsonl", "list"])
    assert args.feed == "/tmp/f.jsonl"


def test_cli_no_subcommand_defaults():
    args = mdfeed.build_parser().parse_args([])
    assert getattr(args, "func", None) is None  # main() falls back to cmd_serve
    assert (args.port, args.feed) == (mdfeed.DEFAULT_PORT, None)


# ── hook path canonicalization ───────────────────────────────────────────────


def test_hook_canonicalizes_symlinked_absolute_paths(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    lines = run_hook(
        monkeypatch,
        tmp_path,
        {"tool_name": "Write", "tool_input": {"file_path": str(link / "notes.md")}},
    )
    assert len(lines) == 1
    assert lines[0]["path"] == str(real / "notes.md")


def test_feed_file_deleted_after_entry(server):
    base, doc = server
    Path(doc).unlink()
    status, _, _ = get(base + "/api/render?path=" + urllib.parse.quote(doc))
    assert status == 404


# ── security headers / DNS-rebinding guard ───────────────────────────────────


def get_with_host(url, host):
    req = urllib.request.Request(url)
    req.add_unredirected_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_csp_on_index(server):
    base, _ = server
    status, headers, _ = get(base + "/")
    assert status == 200
    csp = headers["Content-Security-Policy"]
    assert "script-src 'sha256-" in csp  # only our inline script may run
    assert "connect-src 'self'" in csp
    assert "default-src 'none'" in csp


def test_nosniff_everywhere(server):
    base, _ = server
    for route in ["/", "/api/feed"]:
        _, headers, _ = get(base + route)
        assert headers["X-Content-Type-Options"] == "nosniff"


def test_rebound_host_rejected(server):
    base, _ = server
    assert get_with_host(base + "/api/feed", "evil.example.com") == 403
    assert get_with_host(base + "/api/feed", "evil.example.com:8038") == 403


def test_loopback_hosts_accepted(server):
    base, _ = server
    port = base.rsplit(":", 1)[1]
    for host in [f"localhost:{port}", f"127.0.0.1:{port}", "localhost", "[::1]:80"]:
        assert get_with_host(base + "/api/feed", host) == 200, host


def test_host_check_skipped_when_bound_nonlocal(server):
    base, _ = server
    old = mdfeed.FeedHandler.require_local_host
    mdfeed.FeedHandler.require_local_host = False
    try:
        assert get_with_host(base + "/api/feed", "evil.example.com") == 200
    finally:
        mdfeed.FeedHandler.require_local_host = old


def test_script_hash_matches_served_page(server):
    """The CSP hash must cover the exact bytes of the inline <script> that the
    browser sees, or the whole UI goes dark."""
    import hashlib
    import re as _re

    base, _ = server
    _, headers, body = get(base + "/")
    script = _re.search(rb"<script>(.*)</script>", body, _re.DOTALL).group(1)
    digest = hashlib.sha256(script).digest()
    expected = base64.b64encode(digest).decode()
    assert f"'sha256-{expected}'" in headers["Content-Security-Policy"]


def test_served_page_carries_the_diff_engine(server):
    """The redline engine is spliced into the inline script at import time;
    if the placeholder ever fails to match, `d` throws ReferenceError in the
    browser and nothing server-side notices."""
    base, _ = server
    _, _, body = get(base + "/")
    assert b"/*@HTMLDIFF_ENGINE@*/" not in body
    assert b"function htmlDiff(" in body


def test_diff_engine_matches_htmldiff_section_one():
    """mdfeed/htmldiff_engine.js is a copy of section 1 of htmldiff.html. Two
    copies exist because htmldiff.html must stay one self-contained file and
    mdfeed's CSP needs the engine inline; this is the drift check. Fix by
    copying whichever side changed on purpose over the other."""
    here = Path(__file__).resolve().parent
    page = (here.parent / "htmldiff" / "htmldiff.html").read_text(encoding="utf-8")
    start = page.index("/* =====", page.index("1. htmldiff") - 200)
    end = page.index("/* =====", page.index("2. Line and token diff") - 200)
    # The slice ends in the blank line before section 2's banner; the file ends
    # in a single newline. Compare content, not the trailing whitespace.
    expected = page[start:end].rstrip("\n") + "\n"
    actual = (here / "htmldiff_engine.js").read_text(encoding="utf-8")
    assert actual == expected, (
        "mdfeed/htmldiff_engine.js drifted from htmldiff/htmldiff.html section 1"
    )


# ── `open`: the feed entry, the focus marker, and the tab decision ───────────


def test_resolve_markdown_splits_admitted_from_rejected(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    other = tmp_path / "up.MARKDOWN"
    other.write_text("# hi\n")
    txt = tmp_path / "doc.txt"
    txt.write_text("hi")
    ok, errors = mdfeed.resolve_markdown([doc, other, txt, tmp_path / "gone.md", tmp_path])
    assert ok == [doc.resolve(), other.resolve()]
    assert len(errors) == 3


def test_append_entries_honors_a_cwd_override(tmp_path):
    """A Finder-launched droplet runs with cwd "/", and derive_project would
    record that as the project for a file outside any repo."""
    doc = tmp_path / "notes" / "doc.md"
    doc.parent.mkdir()
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    mdfeed.append_entries(feed, [doc], cwd=str(doc.parent))
    (entry,) = mdfeed.load_feed(feed)
    assert entry["cwd"] == str(doc.parent)
    assert entry["project"] == "notes"


@pytest.mark.parametrize("cwd", ["/", ""])
def test_append_entries_gives_each_file_its_own_dir_for_a_meaningless_cwd(tmp_path, cwd):
    """One Finder selection can span folders, so the stand-in for a cwd of "/"
    is each file's own directory, not the first file's."""
    docs = []
    for name in ("alpha", "beta"):
        d = tmp_path / name / f"{name}.md"
        d.parent.mkdir()
        d.write_text("# hi\n")
        docs.append(d)
    feed = tmp_path / "index.jsonl"
    mdfeed.append_entries(feed, docs, cwd=cwd)
    got = {e["path"]: (e["cwd"], e["project"]) for e in mdfeed.load_feed(feed)}
    assert got == {
        str(docs[0].resolve()): (str(docs[0].resolve().parent), "alpha"),
        str(docs[1].resolve()): (str(docs[1].resolve().parent), "beta"),
    }


def test_focus_marker_roundtrip(tmp_path):
    feed = tmp_path / "index.jsonl"
    assert mdfeed.read_focus(feed) is None
    mdfeed.write_focus(feed, tmp_path / "doc.md")
    focus = mdfeed.read_focus(feed)
    assert focus["path"] == str(tmp_path / "doc.md")
    assert focus["ts"] > 0
    # The temp file used to make the write atomic must not be left behind: the
    # SSE loop globs nothing, but a stray .tmp beside the feed is confusing.
    assert [p.name for p in tmp_path.iterdir()] == ["focus.json"]


def test_concurrent_focus_writes_do_not_collide(tmp_path):
    """Two `open`s at once. With one fixed temp name, both wrote the same file
    and the second os.replace found it already moved: FileNotFoundError."""
    feed = tmp_path / "index.jsonl"
    failures = []

    def writer(n):
        try:
            for _ in range(50):
                mdfeed.write_focus(feed, tmp_path / f"{n}.md")
        except Exception as e:  # noqa: BLE001
            failures.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    assert mdfeed.read_focus(feed)["ts"] > 0
    assert [p.name for p in tmp_path.iterdir()] == ["focus.json"]


def test_read_focus_survives_a_truncated_marker(tmp_path):
    feed = tmp_path / "index.jsonl"
    mdfeed.focus_path_for(feed).write_text("{not json")
    assert mdfeed.read_focus(feed) is None


@pytest.mark.parametrize("ts", ['"soon"', "null", "true", "[1]", "1e999"])
def test_read_focus_treats_a_bad_stamp_as_absent(tmp_path, ts):
    """The SSE loop compares the stamp with a number every tick; a string one
    raised TypeError there and killed the connection."""
    feed = tmp_path / "index.jsonl"
    mdfeed.focus_path_for(feed).write_text('{"path": "/x.md", "ts": %s}' % ts)
    assert mdfeed.read_focus(feed) == {"path": "/x.md", "ts": 0.0}


def status_of(base):
    return json.loads(get(base + "/api/status")[2])


def wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_api_status_counts_viewers(server):
    base, _ = server
    status, _, body = get(base + "/api/status")
    assert status == 200
    payload = json.loads(body)
    # The census belongs to this fixture's server, so no other test's
    # connections are in it.
    assert payload["clients"] == 0
    assert payload["boot"] == mdfeed.BOOT_ID
    assert payload["feed"] == str(mdfeed.FeedHandler.feed_path)


def test_a_closed_viewer_leaves_the_census_quickly(server):
    """A dead socket only errors on a write. With a keepalive every 15s, a tab
    closed a moment ago still counted, and `open` steered a tab that was gone."""
    base, _ = server
    conn = urllib.request.urlopen(base + "/events", timeout=5)
    conn.readline()  # ": connected" — the loop has registered by now
    assert wait_until(lambda: status_of(base)["clients"] == 1)
    conn.close()
    t0 = time.time()
    assert wait_until(lambda: status_of(base)["clients"] == 0, timeout=5.0)
    assert time.time() - t0 < 3.0


def test_focus_ack_is_recorded_and_reported(server):
    base, _ = server
    host, port = "127.0.0.1", int(base.rsplit(":", 1)[1])
    assert status_of(base)["focus_acked"] == []
    assert get(base + "/api/focus-ack?ts=1234.5")[0] == 204
    assert status_of(base)["focus_acked"] == [1234.5]
    assert mdfeed.wait_for_focus_ack(host, port, 1234.5, wait=0)
    get(base + "/api/focus-ack?ts=inf")
    get(base + "/api/focus-ack?ts=nope")
    assert status_of(base)["focus_acked"] == [1234.5]


def test_a_forged_ack_vouches_for_nothing_else(server):
    """Any page can fire an <img> at /api/focus-ack. With a running maximum,
    one ts=1e300 marked every later request acknowledged, so `open` would
    report steering a tab forever without checking one moved."""
    base, _ = server
    host, port = "127.0.0.1", int(base.rsplit(":", 1)[1])
    get(base + "/api/focus-ack?ts=1e300")
    assert not mdfeed.wait_for_focus_ack(host, port, time.time(), wait=0)
    # And flooding it stays bounded.
    for i in range(100):
        get(f"{base}/api/focus-ack?ts={i}")
    assert len(status_of(base)["focus_acked"]) <= 32


def read_events(base, deadline=6.0):
    """Yield SSE lines for `deadline` seconds. The stream never idles — the
    server writes a keepalive every tick — so a read timeout alone can't end it."""
    end = time.time() + deadline
    with urllib.request.urlopen(base + "/events", timeout=deadline) as r:
        while time.time() < end:
            line = r.readline()
            if not line:
                return
            yield line.decode("utf-8", "replace")


def test_sse_emits_focus_when_the_marker_advances(server, tmp_path):
    base, doc = server
    feed = mdfeed.FeedHandler.feed_path
    seen = []

    def reader():
        try:
            for line in read_events(base, deadline=5.0):
                seen.append(line)
                if seen[-2:-1] and seen[-2].startswith("event: focus"):
                    # That was the data line that follows; let go.
                    return
        except OSError:
            return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    # Let the loop register and seed its baseline before the marker lands.
    time.sleep(1.0)
    ts = mdfeed.write_focus(feed, Path(doc))
    t.join(timeout=5)
    assert any(line.startswith("event: focus") for line in seen), seen
    # The stamp rides along: it is what the tab echoes back to acknowledge.
    assert json.loads(seen[-1][len("data: ") :]) == {"path": doc, "ts": ts}


def test_sse_outlives_a_marker_with_a_bad_stamp(server):
    base, doc = server
    feed = mdfeed.FeedHandler.feed_path
    seen = []

    def reader():
        try:
            for line in read_events(base, deadline=5.0):
                seen.append(line)
                if line.startswith("event: focus"):
                    return
        except OSError:
            return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.0)
    mdfeed.focus_path_for(feed).write_text('{"path": "/x.md", "ts": "soon"}')
    time.sleep(1.0)  # a couple of ticks over the bad marker
    mdfeed.write_focus(feed, Path(doc))
    t.join(timeout=5)
    assert any(line.startswith("event: focus") for line in seen), seen


def test_sse_ignores_a_marker_written_before_the_tab_connected(server, tmp_path):
    """A focus request is not a queue. A tab opening an hour after someone ran
    `mdfeed open` must not jump to that file."""
    base, doc = server
    mdfeed.write_focus(mdfeed.FeedHandler.feed_path, Path(doc))
    seen = []
    try:
        for line in read_events(base, deadline=1.5):
            seen.append(line)
    except (OSError, socket.timeout):
        pass
    assert not any(line.startswith("event: focus") for line in seen), seen


def open_args(*argv):
    return mdfeed.build_parser().parse_args(["open", *argv])


def fake_viewer(feed, clients=1, acks=True):
    """A server_status stand-in for a viewer serving `feed`. With `acks`, its
    tab confirms whatever focus request is on disk, as a live one would."""

    def status(*a, **k):
        focus = mdfeed.read_focus(feed)
        acked = [focus["ts"]] if acks and focus else []
        return {"clients": clients, "feed": str(feed), "focus_acked": acked}

    return status


def test_cmd_open_steers_the_tab_that_is_already_up(tmp_path, monkeypatch):
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    opened = []
    monkeypatch.setattr(mdfeed, "server_status", fake_viewer(feed))
    monkeypatch.setattr(mdfeed, "raise_browser", lambda: True)
    monkeypatch.setattr(mdfeed.webbrowser, "open", lambda url: opened.append(url))
    assert mdfeed.cmd_open(open_args("--feed", str(feed), str(doc))) == 0
    assert opened == []  # no second tab spent
    assert mdfeed.read_focus(feed)["path"] == str(doc.resolve())
    assert [e["path"] for e in mdfeed.load_feed(feed)] == [str(doc.resolve())]


def test_cmd_open_falls_back_to_a_tab_when_nobody_acknowledges(tmp_path, monkeypatch, capsys):
    """The census counts a tab closed a moment ago until a write to it fails.
    Trusting it meant printing success while nothing on screen moved."""
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    opened = []
    monkeypatch.setattr(mdfeed, "FOCUS_ACK_WAIT", 0.3)
    monkeypatch.setattr(mdfeed, "server_status", fake_viewer(feed, acks=False))
    monkeypatch.setattr(
        mdfeed, "raise_browser", lambda: pytest.fail("raised a browser nothing moved in")
    )
    monkeypatch.setattr(mdfeed.webbrowser, "open", lambda url: opened.append(url))
    assert mdfeed.cmd_open(open_args("--feed", str(feed), str(doc))) == 0
    (url,) = opened
    assert urllib.parse.quote(str(doc.resolve())) in url
    assert "acknowledged" in capsys.readouterr().err


def test_cmd_open_steers_a_real_viewer_end_to_end(server, tmp_path, monkeypatch):
    """Against a live server, with a stand-in tab doing what the page's focus
    listener does: take the event, report the stamp back."""
    base, _ = server
    feed = mdfeed.FeedHandler.feed_path
    port = base.rsplit(":", 1)[1]
    doc = tmp_path / "other.md"
    doc.write_text("# other\n")
    opened = []
    monkeypatch.setattr(mdfeed, "raise_browser", lambda: True)
    monkeypatch.setattr(mdfeed.webbrowser, "open", lambda url: opened.append(url))

    def tab():
        prev = ""
        try:
            for line in read_events(base, deadline=8.0):
                if prev.startswith("event: focus"):
                    ts = json.loads(line[len("data: ") :])["ts"]
                    get(f"{base}/api/focus-ack?ts={ts}")
                    return
                prev = line
        except OSError:
            return

    threading.Thread(target=tab, daemon=True).start()
    assert wait_until(lambda: status_of(base)["clients"] == 1)
    rc = mdfeed.cmd_open(open_args("--feed", str(feed), "--port", port, str(doc)))
    assert rc == 0
    assert opened == []
    assert str(doc.resolve()) in mdfeed.allowed_paths(feed)


def test_cmd_open_writes_to_the_feed_the_viewer_serves(tmp_path, monkeypatch, capsys):
    """The server's allowlist is its own feed. An entry and a focus marker next
    to some other feed are a 403 and a marker no SSE loop watches."""
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    ours = tmp_path / "ours" / "index.jsonl"
    theirs = tmp_path / "theirs" / "index.jsonl"
    monkeypatch.setattr(mdfeed, "DEFAULT_FEED", ours)
    monkeypatch.setattr(mdfeed, "server_status", fake_viewer(theirs))
    monkeypatch.setattr(mdfeed, "raise_browser", lambda: True)
    assert mdfeed.cmd_open(open_args(str(doc))) == 0
    assert not ours.exists()
    assert mdfeed.allowed_paths(theirs) == {str(doc.resolve())}
    assert mdfeed.read_focus(theirs)["path"] == str(doc.resolve())
    assert str(theirs) in capsys.readouterr().err


def test_cmd_open_refuses_when_an_explicit_feed_disagrees(tmp_path, monkeypatch, capsys):
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    ours = tmp_path / "ours" / "index.jsonl"
    theirs = tmp_path / "theirs" / "index.jsonl"
    monkeypatch.setattr(mdfeed, "server_status", fake_viewer(theirs))
    monkeypatch.setattr(mdfeed.webbrowser, "open", lambda url: pytest.fail("opened a tab"))
    assert mdfeed.cmd_open(open_args("--feed", str(ours), str(doc))) == 1
    assert not ours.exists() and not theirs.exists()
    assert "serves" in capsys.readouterr().err


def test_cmd_open_spends_a_tab_when_nobody_is_watching(tmp_path, monkeypatch):
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    opened = []
    monkeypatch.setattr(mdfeed, "server_status", lambda *a, **k: {"clients": 0})
    monkeypatch.setattr(mdfeed.webbrowser, "open", lambda url: opened.append(url))
    assert mdfeed.cmd_open(open_args("--feed", str(feed), str(doc))) == 0
    (url,) = opened
    assert url.startswith(f"http://127.0.0.1:{mdfeed.DEFAULT_PORT}/?path=")
    assert urllib.parse.quote(str(doc.resolve())) in url
    # Nothing was told to jump, so no marker either.
    assert mdfeed.read_focus(feed) is None


def test_cmd_open_forces_a_new_tab_on_request(tmp_path, monkeypatch):
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    opened = []
    monkeypatch.setattr(mdfeed, "server_status", lambda *a, **k: {"clients": 3})
    monkeypatch.setattr(mdfeed.webbrowser, "open", lambda url: opened.append(url))
    assert mdfeed.cmd_open(open_args("--feed", str(feed), "--new-tab", str(doc))) == 0
    assert len(opened) == 1
    assert mdfeed.read_focus(feed) is None


def test_cmd_open_lands_on_the_last_of_several_files(tmp_path, monkeypatch):
    one, two = tmp_path / "one.md", tmp_path / "two.md"
    for d in (one, two):
        d.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    monkeypatch.setattr(mdfeed, "server_status", fake_viewer(feed))
    monkeypatch.setattr(mdfeed, "raise_browser", lambda: True)
    assert mdfeed.cmd_open(open_args("--feed", str(feed), str(one), str(two))) == 0
    assert {e["path"] for e in mdfeed.load_feed(feed)} == {str(one.resolve()), str(two.resolve())}
    assert mdfeed.read_focus(feed)["path"] == str(two.resolve())


def test_cmd_open_refuses_a_non_markdown_file(tmp_path, monkeypatch, capsys):
    txt = tmp_path / "doc.txt"
    txt.write_text("hi")
    feed = tmp_path / "index.jsonl"
    # Nothing may be started or opened for a file the feed would reject; it
    # would be a 403 on /api/render, i.e. an empty pane.
    monkeypatch.setattr(
        mdfeed, "server_status", lambda *a, **k: pytest.fail("probed the server anyway")
    )
    assert mdfeed.cmd_open(open_args("--feed", str(feed), str(txt))) == 1
    assert "not markdown" in capsys.readouterr().err
    assert not feed.exists()


def test_cmd_open_does_not_fight_an_older_viewer_for_the_port(tmp_path, monkeypatch, capsys):
    """A viewer without /api/status is still a viewer holding the port. Trying
    to start another one there just burns the start-up timeout."""
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    opened = []
    monkeypatch.setattr(mdfeed, "server_status", lambda *a, **k: {})
    monkeypatch.setattr(
        mdfeed, "start_server", lambda *a, **k: pytest.fail("tried to bind a taken port")
    )
    monkeypatch.setattr(mdfeed.webbrowser, "open", lambda url: opened.append(url))
    assert mdfeed.cmd_open(open_args("--feed", str(feed), str(doc))) == 0
    assert len(opened) == 1  # the deep link; an old viewer lands on the newest anyway
    assert "no /api/status" in capsys.readouterr().err


def test_server_status_tells_an_empty_port_from_a_busy_one(server):
    base, _ = server
    host, port = "127.0.0.1", int(base.rsplit(":", 1)[1])
    assert mdfeed.server_status(host, port)["clients"] == 0
    # Nothing listening at all is None, which is the only case that may spawn.
    assert mdfeed.server_status(host, 1, timeout=0.3) is None


def raw_listener(respond):
    """A bare TCP listener that hands each connection to `respond`."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)

    def loop():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                respond(conn)
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return sock


@pytest.mark.parametrize(
    "respond",
    [
        # Not HTTP at all: http.client raises BadStatusLine, which is neither a
        # URLError nor an OSError and used to escape as a traceback.
        lambda c: (c.recv(1024), c.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")),
        # Accepts, then hangs up without a word: RemoteDisconnected, which is a
        # ConnectionResetError and so read as a free port — and `open` then
        # spawned a server that could not bind.
        lambda c: c.recv(1024),
    ],
    ids=["not-http", "hangs-up"],
)
def test_server_status_reads_any_listener_as_occupied(respond):
    sock = raw_listener(respond)
    try:
        assert mdfeed.server_status("127.0.0.1", sock.getsockname()[1]) == {}
    finally:
        sock.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_server_status_bypasses_a_configured_proxy(server, monkeypatch):
    """urlopen honours http_proxy (and the macOS system proxy), and then the
    proxy answers for our own port: a free one read as occupied, and `open`
    never started a viewer."""
    hits = []

    def proxy(c):
        hits.append(c.recv(4096))
        c.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nhi")

    sock = raw_listener(proxy)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    for var in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(var, url)
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    try:
        assert mdfeed.server_status("127.0.0.1", free_port()) is None
        base, _ = server
        assert mdfeed.server_status("127.0.0.1", int(base.rsplit(":", 1)[1]))["clients"] == 0
        assert hits == []
    finally:
        sock.close()


def test_cmd_open_hands_a_started_server_an_absolute_feed(tmp_path, monkeypatch):
    """The server `open` starts runs with its cwd at $HOME; a relative --feed
    would name a different file there than here."""
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr(mdfeed, "server_status", lambda *a, **k: None)

    def fake_start(host, port, feed_path, **k):
        started.append(feed_path)
        return {"clients": 0}

    monkeypatch.setattr(mdfeed, "start_server", fake_start)
    assert mdfeed.cmd_open(open_args("--feed", "sub/index.jsonl", "--no-browser", str(doc))) == 0
    assert started == [tmp_path.resolve() / "sub" / "index.jsonl"]
    assert mdfeed.allowed_paths(started[0]) == {str(doc.resolve())}


def test_cmd_serve_reports_an_absolute_feed(tmp_path, monkeypatch):
    """/api/status hands the feed to `open`, which runs somewhere else."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mdfeed.FeedHandler, "feed_path", mdfeed.FeedHandler.feed_path)

    def no_bind(*a, **k):
        raise OSError("stop here")

    monkeypatch.setattr(mdfeed, "ThreadingServer", no_bind)
    args = mdfeed.build_parser().parse_args(
        ["serve", "--feed", "rel/index.jsonl", "--no-browser", "--port", str(free_port())]
    )
    assert mdfeed.cmd_serve(args) == 1
    assert mdfeed.FeedHandler.feed_path == tmp_path.resolve() / "rel" / "index.jsonl"


def test_raise_browser_falls_back_to_safari(monkeypatch):
    """No http handler in LaunchServices is the stock setup: Safari."""
    calls = []

    class Done:
        def __init__(self, stdout=b"", returncode=0):
            self.stdout, self.returncode = stdout, returncode

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[0] == "/usr/bin/plutil":
            return Done(json.dumps({"LSHandlers": [{"LSHandlerURLScheme": "mailto"}]}).encode())
        return Done()

    monkeypatch.setattr(mdfeed.sys, "platform", "darwin")
    monkeypatch.setattr(mdfeed.subprocess, "run", fake_run)
    assert mdfeed.raise_browser() is True
    assert calls[-1] == ["/usr/bin/open", "-b", "com.apple.Safari"]


def test_server_status_reads_a_silent_listener_as_occupied():
    sock = raw_listener(lambda c: time.sleep(1.0))
    try:
        assert mdfeed.server_status("127.0.0.1", sock.getsockname()[1], timeout=0.3) == {}
    finally:
        sock.close()


def test_server_status_reads_a_wrong_answer_as_occupied(tmp_path):
    """Anything that answers HTTP holds the port, whatever it answers with."""

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"hi")

        def log_message(self, *a):
            pass

    srv = mdfeed.ThreadingServer(("127.0.0.1", 0), Other)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert mdfeed.server_status("127.0.0.1", srv.server_address[1]) == {}
    finally:
        srv.shutdown()
        srv.server_close()


def test_cmd_open_reports_a_server_that_never_comes_up(tmp_path, monkeypatch, capsys):
    doc = tmp_path / "doc.md"
    doc.write_text("# hi\n")
    feed = tmp_path / "index.jsonl"
    monkeypatch.setattr(mdfeed, "server_status", lambda *a, **k: None)
    monkeypatch.setattr(mdfeed, "start_server", lambda *a, **k: None)
    assert mdfeed.cmd_open(open_args("--feed", str(feed), str(doc))) == 1
    assert "timed out" in capsys.readouterr().err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
