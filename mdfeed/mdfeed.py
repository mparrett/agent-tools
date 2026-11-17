#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "mistune>=3.0.0",
# ]
# ///
"""mdfeed — live viewer for .md files written/edited by Claude.

Reads ~/.claude/md-feed/index.jsonl (populated by the md-feed-write hook)
and serves a split-pane web UI: list of recent .md files on the left,
rendered preview on the right. Auto-refreshes via SSE.

Hook wiring: register mdfeed_write.py (same directory) as a PostToolUse
hook in ~/.claude/settings.json. See README.md ("Setup") for the snippet.
"""

import argparse
import ast
import base64
import collections
import contextlib
import fcntl
import hashlib
import html
import http.client
import http.server
import json
import math
import os
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import mistune
from feedlib import derive_category, derive_project

# Honor MD_FEED_PATH so the viewer reads the same file the write hook
# (mdfeed_write.py) writes to; otherwise a custom path silently diverges.
DEFAULT_FEED = Path(
    os.environ.get("MD_FEED_PATH") or str(Path.home() / ".claude" / "md-feed" / "index.jsonl")
)
DEFAULT_PORT = 8038
MAX_ENTRIES = 200

# Changes on every (re)start. The SSE 'boot' event carries it so a connected
# browser can detect a server restart (e.g. after --reload) and refresh itself
# to pick up UI changes, which live in INDEX_HTML below.
BOOT_ID = str(time.time())

_MD = mistune.create_markdown(
    escape=False,
    plugins=["strikethrough", "footnotes", "table"],
)


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


def _dedupe_entries(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Parse JSONL lines → entries deduped by path (newest ts wins), newest
    first. Junk lines (blank, malformed, no path) are skipped."""
    seen: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = obj.get("path")
        if not p:
            continue
        prev = seen.get(p)
        if not prev or obj.get("ts", 0) >= prev.get("ts", 0):
            seen[p] = obj
    return sorted(seen.values(), key=lambda e: e.get("ts", 0), reverse=True)


# The feed is re-read by every /api/feed, /api/render and /raw request (the
# latter two via allowed_paths), and the JSONL grows without bound between
# compactions — so the parse is cached on the file's (mtime_ns, size). The
# key is stat'd BEFORE reading: if a hook append lands mid-read we may cache
# newer content under the older key, and the next request simply misses and
# re-parses. Never stale, at worst one redundant parse.
_FEED_CACHE_LOCK = threading.Lock()
_FEED_CACHE: Dict[str, Tuple[Tuple[int, int], List[Dict[str, Any]]]] = {}


def _parsed_feed(feed_path: Path) -> List[Dict[str, Any]]:
    try:
        st = feed_path.stat()
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    cache_id = str(feed_path)
    with _FEED_CACHE_LOCK:
        cached = _FEED_CACHE.get(cache_id)
        if cached and cached[0] == key:
            return cached[1]
    try:
        with open(feed_path, "r", encoding="utf-8") as f:
            entries = _dedupe_entries(f)[:MAX_ENTRIES]
    except OSError:
        return []
    with _FEED_CACHE_LOCK:
        _FEED_CACHE[cache_id] = (key, entries)
    return entries


def load_feed(feed_path: Path) -> List[Dict[str, Any]]:
    """Read JSONL feed, dedupe by path (newest wins), newest first, capped."""
    # Copies, so the cached entries stay immutable across threads; the exists
    # flag is re-stat'd per call (a file can vanish without a feed write).
    entries = [dict(e) for e in _parsed_feed(feed_path)]
    # Flag entries whose file is gone (moved/renamed/deleted) so the UI can dim
    # them — feed paths are recorded at write time and never pruned. Only the
    # capped set is stat'd, so this is bounded.
    for e in entries:
        e["exists"] = os.path.isfile(e.get("path", ""))
    return entries


def allowed_paths(feed_path: Path) -> set:
    return {e["path"] for e in load_feed(feed_path)}


# A focus request is a file, not an HTTP call, for the same reason the feed is:
# `mdfeed open` has to work whether or not a server is up, and the SSE loop is
# already a polling loop over a file's mtime. It sits beside the feed so
# MD_FEED_PATH moves both together.
def focus_path_for(feed_path: Path) -> Path:
    return feed_path.parent / "focus.json"


def write_focus(feed_path: Path, target: Path) -> float:
    """Ask every connected viewer to select `target`. Returns the request's
    stamp, which is what a tab echoes back to acknowledge it."""
    p = focus_path_for(feed_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    # A temp name of its own, in the same directory so the rename stays atomic.
    # A fixed name let two concurrent `open`s write the same temp file, and the
    # second os.replace found it already moved.
    fd, tmp = tempfile.mkstemp(prefix=".focus.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"path": str(target), "ts": ts}, f, ensure_ascii=False)
        # Atomic: the SSE loop polls this file twice a second and must never
        # read a half-written one.
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return ts


def read_focus(feed_path: Path) -> Optional[Dict[str, Any]]:
    """The focus marker, with `ts` a finite float and `path` a str, or None.

    Normalized here because the SSE loop compares `ts` with a number on every
    tick: a hand-edited or foreign marker with a string stamp raised TypeError
    there and killed the connection.
    """
    try:
        data = json.loads(focus_path_for(feed_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("ts")
    ok = isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts)
    path = data.get("path")
    return {"path": path if isinstance(path, str) else "", "ts": float(ts) if ok else 0.0}


def resolve_markdown(paths: Iterable[Path]) -> Tuple[List[Path], List[str]]:
    """Split `paths` into resolved existing markdown files and reject reasons.

    Extracted from append_entries so `open` can decide which file to land the
    viewer on by the same rule that decides what `add` admits — the feed is the
    server's allowlist, so a file it rejected is a 403 on /api/render, not a
    blank pane.
    """
    ok: List[Path] = []
    errors: List[str] = []
    for p in paths:
        p = p.expanduser().resolve()
        if not p.exists():
            errors.append(f"missing: {p}")
            continue
        if not p.is_file():
            errors.append(f"not a file: {p}")
            continue
        name_lower = p.name.lower()
        if not (name_lower.endswith(".md") or name_lower.endswith(".markdown")):
            errors.append(f"not markdown: {p}")
            continue
        ok.append(p)
    return ok, errors


def append_entries(
    feed_path: Path, paths: Iterable[Path], tool: str = "Add", cwd: Optional[str] = None
) -> Tuple[int, List[str]]:
    """Append one feed entry per existing .md file. Returns (added_count, errors).

    `cwd` overrides the working directory recorded on each entry, which
    derive_project falls back to for a file outside any repo.
    """
    targets, errors = resolve_markdown(paths)
    return append_resolved(feed_path, targets, tool=tool, cwd=cwd), errors


def append_resolved(
    feed_path: Path, targets: Iterable[Path], tool: str = "Add", cwd: Optional[str] = None
) -> int:
    """append_entries for paths resolve_markdown already admitted.

    A cwd of "/" (what Finder launches a droplet with) or "" says nothing about
    where a file belongs, and derive_project would record it as the project for
    anything outside a repo. Each file then gets its own directory instead —
    per file, since one Finder selection can span several folders.
    """
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    cwd = cwd if cwd is not None else os.getcwd()
    added = 0
    with open(feed_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            for p in targets:
                entry_cwd = cwd if cwd not in ("", "/") else str(p.parent)
                entry = {
                    "ts": time.time(),
                    "path": str(p),
                    "tool": tool,
                    "cwd": entry_cwd,
                    "project": derive_project(p, entry_cwd),
                    "category": derive_category(p),
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                added += 1
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return added


def compact_feed(
    feed_path: Path,
    keep: int = MAX_ENTRIES,
    prune_missing: bool = False,
    backup: bool = True,
    dry_run: bool = False,
) -> Tuple[int, int, Optional[Path]]:
    """Rewrite the feed to the deduped, capped set the viewer can actually
    show. Lossy only for what's already invisible: per-file write history and
    entries past the cap. Returns (lines_before, lines_after, backup_path).

    The feed accumulates across live sessions, so the rewrite is guarded:
    keep < 1 is rejected (a negative value would slice to an empty feed), a
    .bak of the original is written first unless backup=False, and dry_run
    computes the numbers without touching the file.

    Rewrites IN PLACE under the same exclusive flock the hook takes — the
    usual write-temp-then-rename would let a hook blocked on the old inode's
    lock append to the orphaned file, silently losing the entry.
    """
    if keep < 1:
        raise ValueError(f"keep must be >= 1, got {keep}")
    bak_path = None
    with open(feed_path, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            original = f.read()
            lines = original.splitlines()
            entries = _dedupe_entries(lines)
            if prune_missing:
                entries = [e for e in entries if os.path.isfile(e.get("path", ""))]
            entries = entries[:keep]
            entries.reverse()  # keep the file oldest-first, matching append order
            if not dry_run:
                if backup:
                    bak_path = feed_path.with_name(feed_path.name + ".bak")
                    bak_path.write_text(original, encoding="utf-8")
                f.seek(0)
                f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
                f.truncate()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return len(lines), len(entries), bak_path


# ── Dwell: which parts of a doc the reader has looked at ─────────────────────
# The page anchors each rendered block by a hash of its text plus an ordinal for
# duplicates, so heat is independent of layout (font, width, folds) and carries
# over to the blocks an edit leaves alone. The server never sees the blocks
# themselves; it keeps an append-only event log per doc, one line per block per
# flush, and derives totals on read. Events rather than a running sum because
# the paint modes still to come (follow-up, confused, edit; see `kind`) will
# want the events, not just the sum.
DWELL_KINDS = frozenset({"dwell"})
_DWELL_BLOCK_RE = re.compile(r"^[0-9a-z]{1,8}#[0-9]{1,5}$")
# One flush covers a few seconds of painting; anything larger is a bug or a
# forged request, and clamping keeps one bad post from saturating a doc.
_DWELL_MAX_SECS = 60.0
_DWELL_MAX_BLOCKS = 5000


def dwell_dir(feed_path: Path) -> Path:
    return feed_path.parent / "dwell"


def dwell_log_for(feed_path: Path, doc: str) -> Path:
    digest = hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]
    return dwell_dir(feed_path) / f"{digest}.jsonl"


def doc_version(doc: Path) -> str:
    """Content hash of the doc as it is now, recorded on every dwell event so a
    later reader can tell which version a block was read in."""
    return hashlib.sha256(doc.read_bytes()).hexdigest()[:12]


def clean_dwell_blocks(blocks: Any) -> Dict[str, float]:
    """The valid part of a posted {block: seconds} map; drops the rest."""
    if not isinstance(blocks, dict):
        return {}
    out: Dict[str, float] = {}
    for key, secs in list(blocks.items())[:_DWELL_MAX_BLOCKS]:
        if not isinstance(key, str) or not _DWELL_BLOCK_RE.match(key):
            continue
        if isinstance(secs, bool) or not isinstance(secs, (int, float)):
            continue
        if not math.isfinite(secs) or secs <= 0:
            continue
        out[key] = round(min(float(secs), _DWELL_MAX_SECS), 3)
    return out


def append_dwell(
    feed_path: Path,
    doc: str,
    version: str,
    blocks: Dict[str, float],
    kind: str = "dwell",
    ts: Optional[float] = None,
) -> int:
    if not blocks:
        return 0
    log = dwell_log_for(feed_path, doc)
    log.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time() if ts is None else ts
    lines = "".join(
        json.dumps({"ts": ts, "doc": doc, "block": b, "kind": kind, "secs": s, "version": version})
        + "\n"
        for b, s in blocks.items()
    )
    with open(log, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(lines)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return len(blocks)


def dwell_totals(feed_path: Path, doc: str) -> Dict[str, Dict[str, float]]:
    """{kind: {block: seconds}} summed over every event for `doc`."""
    totals: Dict[str, Dict[str, float]] = {}
    try:
        text = dwell_log_for(feed_path, doc).read_text(encoding="utf-8")
    except OSError:
        return totals
    for line in text.splitlines():
        try:
            ev = json.loads(line)
            # The log name is a truncated hash; the stored path is the check.
            if ev.get("doc") != doc:
                continue
            per = totals.setdefault(str(ev["kind"]), {})
            per[ev["block"]] = round(per.get(ev["block"], 0.0) + float(ev["secs"]), 3)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return totals


def _dwell_index_path(feed_path: Path) -> Path:
    return dwell_dir(feed_path) / "index.json"


def read_dwell_index(feed_path: Path) -> Dict[str, Dict[str, Any]]:
    """{doc: {"pct", "version", "ts"}}: how much of each doc was read, as the
    page last measured it. The page knows the blocks, so it computes the share;
    the server only files it for the list view."""
    try:
        data = json.loads(_dwell_index_path(feed_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_read_share(feed_path: Path, doc: str, pct: float, version: str) -> None:
    idx_path = _dwell_index_path(feed_path)
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    lock = idx_path.with_name("index.lock")
    with open(lock, "a") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            idx = read_dwell_index(feed_path)
            idx[doc] = {"pct": round(pct, 1), "version": version, "ts": time.time()}
            fd, tmp = tempfile.mkstemp(dir=str(idx_path.parent), prefix=".index-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
            os.replace(tmp, idx_path)
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def with_read_shares(entries: List[Dict[str, Any]], feed_path: Path) -> List[Dict[str, Any]]:
    """Attach {"pct", "stale"} to entries the reader has painted. Stale means
    the file was written after the share was measured, judged by feed time so
    /api/feed does not hash every file on each poll."""
    idx = read_dwell_index(feed_path)
    if not idx:
        return entries
    out = []
    for e in entries:
        rec = idx.get(e.get("path", ""))
        if isinstance(rec, dict) and isinstance(rec.get("pct"), (int, float)):
            stale = float(e.get("ts") or 0) > float(rec.get("ts") or 0)
            e = dict(e, read={"pct": rec["pct"], "stale": stale})
        out.append(e)
    return out


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mdfeed</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<!-- One stylesheet for every typeface the prefs panel offers. Listing a family
     here costs only its @font-face rules: the browser fetches a font file the
     first time text is drawn in it, so readers who never pick Atkinson never
     download it. -->
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&family=Atkinson+Hyperlegible+Next:ital,wght@0,400..700;1,400..700&family=Atkinson+Hyperlegible+Mono:wght@400..700&display=swap">
<style>
  /* Palette: Warm Burnout (warmburnout.com) — warm, earthy, low-strain. */
  :root {
    --bg: #F5EDE0;
    --text: #3a3630;
    --muted: #5c5750;
    --faint: #8a8070;
    --border: #DDD6CA;
    --border-strong: #3a3630;
    --card-bg: #EDE6DA;
    --row-hover: #E6DCCB;
    --link-underline: #cdc1ad;
    --code-bg: #ECE3D3;
    --blockquote-border: #b8522e;
    --accent: #b8522e;
    --ok: #226414;
    --err: #c43040;
    --ins: #285464;  /* Types: the palette's cool accent */
    --del: #b03434;  /* Errors */
    --lane-a: #b8522e; --lane-b: #2e6db8; --lane-c: #2f8f4e; --lane-x: #8a8070;
    --font: 'Instrument Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    --mono: 'SF Mono', Monaco, monospace;
    --radius: 6px;
    --fs: 16px;
    --lh: 1.7;      /* body-text line height (prefs: spacing) */
    --col: 820px;   /* reading column width (prefs: width) */
    /* Zen-mode gutter vignette. Derived from --bg so it tracks the active
       theme; biased toward black so edges recede. Dark themes override this
       with a stronger value (see below) — a near-black bg has little room
       left to darken, so the light-mode strength is invisible there. */
    --vignette: color-mix(in srgb, var(--bg) 67%, #000);
  }
  /* Reading preferences (the Aa panel). Each is a data-* attribute on <html>,
     absent at its default, set before first paint by the <head> script. */
  :root[data-size="small"] { --fs: 14px; }
  :root[data-size="large"] { --fs: 18px; }
  :root[data-size="xlarge"] { --fs: 20px; }
  :root[data-spacing="compact"] { --lh: 1.5; }
  :root[data-spacing="relaxed"] { --lh: 1.95; }
  :root[data-width="narrow"] { --col: 680px; }
  :root[data-width="wide"] { --col: 1040px; }
  /* Typefaces come as a UI + mono pair, chosen together: Atkinson's mono cut
     shares its letterform rules (distinct 0/O, 1/l/I), which is the point of
     picking it, and mixing faces independently is a choice nobody asked for. */
  :root[data-font="atkinson"] {
    --font: 'Atkinson Hyperlegible Next', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    --mono: 'Atkinson Hyperlegible Mono', 'SF Mono', Monaco, monospace;
  }
  :root[data-font="system"] {
    --font: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    --mono: ui-monospace, 'SF Mono', Monaco, monospace;
  }
  /* Dark vars apply two ways: when the system prefers dark and the user
     hasn't forced light (auto mode), or when the user explicitly picks dark. */
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #1a1510;
      --text: #bfbdb6;
      --muted: #ada69c;
      --faint: #a59f96;
      --border: #322c24;
      --border-strong: #bfbdb6;
      --card-bg: #1f1d17;
      --row-hover: #222018;
      --link-underline: #5a5044;
      --code-bg: #14120f;
      --blockquote-border: #b8522e;
      --accent: #b8522e;
      --ok: #70bf56;
      --err: #f26d78;
      --ins: #90aec0;
      --del: #f49090;
      --lane-a: #d96a44; --lane-b: #5a9fe0; --lane-c: #56bf6e; --lane-x: #a59f96;
      --vignette: #000;
    }
  }
  :root[data-theme="dark"] {
    --bg: #1a1510;
    --text: #bfbdb6;
    --muted: #ada69c;
    --faint: #a59f96;
    --border: #322c24;
    --border-strong: #bfbdb6;
    --card-bg: #1f1d17;
    --row-hover: #222018;
    --link-underline: #5a5044;
    --code-bg: #14120f;
    --blockquote-border: #b8522e;
    --accent: #b8522e;
    --ok: #70bf56;
    --err: #f26d78;
    --ins: #90aec0;
    --del: #f49090;
    --lane-a: #d96a44; --lane-b: #5a9fe0; --lane-c: #56bf6e; --lane-x: #a59f96;
    --vignette: #000;
  }
  * { box-sizing: border-box; }
  /* Quiet, theme-matched scrollbars — the native chrome clashes with the warm
     palette. Standard props (Chromium, Firefox, Safari 18.2+) render a thin
     themed bar; the ::-webkit-scrollbar block is a fallback for older Safari,
     which ignores scrollbar-color and so still honors the pseudo-elements. */
  #list, #preview { scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
  #list::-webkit-scrollbar, #preview::-webkit-scrollbar { width: 10px; }
  #list::-webkit-scrollbar-track, #preview::-webkit-scrollbar-track { background: transparent; }
  #list::-webkit-scrollbar-thumb, #preview::-webkit-scrollbar-thumb { background: var(--border); border-radius: 6px; border: 3px solid transparent; background-clip: padding-box; }
  #list::-webkit-scrollbar-thumb:hover, #preview::-webkit-scrollbar-thumb:hover { background: var(--faint); background-clip: padding-box; }
  html, body { margin: 0; padding: 0; height: 100%; font-family: var(--font); color: var(--text); background: var(--bg); -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
  body { display: flex; flex-direction: column; }
  header { padding: 8px 14px; border-bottom: 1px solid var(--border); background: var(--card-bg); display: flex; align-items: center; gap: 12px; font-size: 13px; }
  header .title { font-weight: 600; color: var(--text); }
  header .status { color: var(--muted); }
  header .sep { margin: 0 9px; color: var(--faint); }
  header .live { color: var(--ok); }
  header .stale { color: var(--err); }
  main { flex: 1; display: flex; min-height: 0; }
  #list { width: 320px; min-width: 280px; max-width: 40vw; border-right: 1px solid var(--border); overflow-y: auto; background: var(--card-bg); }
  /* Splitter between list and preview: a thin full-height strip (easy target)
     with a centered grip pill, instead of the native bottom-right resize nub.
     Negative margins let it straddle the border without adding a visible gap. */
  #resizer { flex: 0 0 7px; margin: 0 -3px; z-index: 5; position: relative; cursor: col-resize; }
  #resizer::before { content: ''; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 4px; height: 32px; border-radius: 3px; background: var(--border); transition: background 0.15s, height 0.15s; }
  #resizer:hover::before, #resizer.dragging::before { background: var(--accent); height: 52px; }
  #filters { position: sticky; top: 0; z-index: 1; display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 10px; background: var(--card-bg); border-bottom: 1px solid var(--border); }
  #filters:empty { display: none; }
  .fchip { font-family: var(--font); font-size: 11px; color: var(--muted); background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 2px 9px; cursor: pointer; white-space: nowrap; }
  .fchip:hover { color: var(--text); border-color: var(--border-strong); }
  .fchip.active { color: var(--bg); background: var(--accent); border-color: var(--accent); }
  .fchip.more { color: var(--accent); font-weight: 600; }
  #preview { flex: 1; overflow-y: auto; padding: 24px 32px; max-width: 100%; font-size: var(--fs); }
  .preview-inner { max-width: var(--col); margin: 0 auto; }
  #viewtabs { display: flex; padding: 8px 10px 2px; }
  .vtab { flex: 1; text-align: center; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); background: transparent; border: 1px solid var(--border); padding: 5px 8px; cursor: pointer; }
  .vtab:first-child { border-radius: 6px 0 0 6px; }
  .vtab:last-child { border-radius: 0 6px 6px 0; }
  .vtab + .vtab { border-left: none; }
  .vtab:hover { color: var(--text); }
  .vtab.active { color: var(--bg); background: var(--accent); border-color: var(--accent); }
  .vtab .vcount { opacity: 0.7; font-weight: 500; margin-left: 5px; }
  .row { display: block; padding: 8px 14px; cursor: pointer; border-left: 3px solid transparent; border-bottom: 1px solid var(--border); }
  .row:hover { background: var(--row-hover); }
  .row.active { background: var(--bg); border-left-color: var(--accent); }
  .row .fname { font-weight: 600; color: var(--text); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .meta { font-size: 11px; color: var(--muted); margin-top: 2px; display: flex; gap: 6px; align-items: center; }
  .chip { background: var(--bg); color: var(--muted); padding: 1px 6px; border-radius: 10px; font-weight: 500; font-size: 10px; border: 1px solid var(--border); }
  .chip.plan { color: var(--accent); border-color: var(--accent); }
  .tool { font-family: var(--mono); color: var(--faint); font-size: 10px; }
  .empty { padding: 40px 14px; color: var(--muted); text-align: center; font-size: 13px; }
  /* ── Lanes view ─────────────────────────────────────────────────────────── */
  /* Event-ranked temporal lanes: the 3 most-recently-touched projects get their
     own rail; everything else merges into an "others" rail. One write = one row,
     so a long filename just overflows rightward over neighbor rails — no columns
     to truncate. Empty rail space = a project that was idle while others worked. */
  /* Width shrinks/grows to fit the actual content: renderLanes measures the
     rightmost pill edge and sets --lane-fit, so a lane whose last pill is short
     doesn't reserve dead tail space. The auto fit is clamped to 50vw in JS;
     max-width here is looser only so a manual resize-drag can exceed 50%. */
  :root[data-lanes] #list { width: var(--lane-fit, 50vw); min-width: 280px; max-width: 80vw; }
  .lanescene { position: relative; }
  /* Stick below the (also-sticky) filter bar instead of on top of it; offset is
     the live filter height, set by renderLanes (0 when filters are hidden). */
  .laneheads { position: sticky; top: var(--lanehead-top, 0); z-index: 4; height: 38px; background: var(--card-bg); border-bottom: 1px solid var(--border); }
  .lanehead { position: absolute; top: 10px; font-size: 12px; white-space: nowrap; }
  .lanehead .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }
  .lanehead .name { font-weight: 600; color: var(--text); }
  .lanehead .count { color: var(--faint); font-size: 11px; margin-left: 4px; }
  .lanebody { position: relative; }
  .rail { position: absolute; top: 0; bottom: 0; width: 2px; opacity: 0.3; }
  .lanerow { position: relative; height: 30px; }
  /* lanerow height is set inline by pillRowHtml from the pill's total line count */
  .lanerow .ltime { position: absolute; left: 0; top: 50%; transform: translateY(-50%); width: 46px; text-align: right; padding-right: 8px; font-size: 10px; font-family: var(--mono); color: var(--faint); }
  .lanenode { position: absolute; top: 50%; width: 9px; height: 9px; border-radius: 50%; transform: translate(-50%, -50%); border: 2px solid var(--card-bg); z-index: 2; }
  .lanepill { position: absolute; top: 50%; transform: translateY(-50%); display: inline-flex; align-items: center; gap: 6px; background: var(--bg); border: 1px solid var(--border); border-radius: 12px; padding: 2px 9px; font-size: 12px; font-weight: 500; white-space: nowrap; cursor: pointer; z-index: 3; max-width: 360px; }
  .lanepill .fname span { display: block; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 1.25; }
  .lanepill:hover { background: var(--row-hover); z-index: 5; }
  .lanepill.active { border-color: var(--accent); }
  .lanepill.plan .fname { color: var(--accent); }
  .lanepill .ltool { font-family: var(--mono); font-size: 9px; color: var(--faint); }
  /* Stack the name line(s) and the ·project tag vertically so the tag wraps to
     its own line (others lane) instead of widening the pill. */
  .lanepill .ptext { display: inline-flex; flex-direction: column; min-width: 0; }
  .lanepill .ptag { display: block; font-size: 9px; color: var(--faint); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* Stale entries: the file recorded in the feed is no longer on disk. */
  .row.gone, .lanepill.gone { opacity: 0.45; }
  .row.gone .fname, .lanepill.gone .fname span { text-decoration: line-through; }
  .lanegap { position: relative; height: 22px; }
  .lanegap .gline { position: absolute; left: 54px; right: 10px; top: 50%; border-top: 1px dashed var(--border); }
  .lanegap .glabel { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); background: var(--card-bg); padding: 0 10px; font-size: 10px; font-family: var(--mono); color: var(--faint); }
  #preview h1, #preview h2, #preview h3 { border-bottom: 1px solid var(--border); padding-bottom: 6px; font-weight: 600; }
  #preview h1 { font-size: 1.8em; margin-top: 0; font-weight: 400; }
  #preview h2 { font-size: 1.4em; }
  #preview p, #preview ul, #preview ol { line-height: var(--lh); }
  #preview pre { background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; overflow-x: auto; }
  #preview code { background: var(--code-bg); padding: 2px 5px; border-radius: 3px; font-size: 0.9em; font-family: var(--mono); }
  #preview pre code { background: none; padding: 0; }
  #preview blockquote { border-left: 3px solid var(--blockquote-border); color: var(--muted); padding: 0 1em; margin: 0 0 16px; font-style: italic; }
  #preview table { border-collapse: collapse; margin: 16px 0; }
  #preview th, #preview td { border: 1px solid var(--border); padding: 6px 12px; }
  #preview th { background: var(--card-bg); }
  #preview a { color: var(--muted); text-decoration: none; border-bottom: 1px solid var(--link-underline); }
  #preview a:hover { color: var(--text); border-bottom-color: var(--text); }
  #preview-path { font-family: var(--mono); font-size: 11px; color: var(--muted); padding: 4px 8px; background: var(--code-bg); border-radius: 4px; display: inline-block; margin-bottom: 16px; cursor: pointer; }
  #preview-path:hover { background: var(--card-bg); }
  /* The bar owns the gap below; the pill's own margin-bottom would otherwise
     shift it 16px off-center against the raw link inside the flex row. */
  #preview-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
  #preview-bar #preview-path { margin-bottom: 0; }
  #preview-raw-link { font-family: var(--mono); font-size: 11px; color: var(--muted); text-decoration: none; padding: 4px 8px; background: var(--code-bg); border-radius: 4px; }
  #preview-raw-link:hover { background: var(--card-bg); color: var(--text); }
  /* Source view: the raw markdown in a textarea, so select-all/copy grabs the
     exact bytes on disk rather than the rendered DOM's reflowed text. */
  #preview-source { display: block; width: 100%; height: calc(100vh - 130px); box-sizing: border-box; font-family: var(--mono); font-size: 12px; line-height: 1.55; color: var(--text); background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px; padding: 14px 16px; resize: none; outline: none; }
  #preview-source:focus { border-color: var(--accent); }
  #preview-empty { color: var(--muted); text-align: center; padding: 60px 0; }
  .toast { position: fixed; bottom: 20px; right: 20px; background: var(--border-strong); color: var(--bg); padding: 8px 14px; border-radius: 6px; font-size: 12px; opacity: 0; transition: opacity 0.2s; pointer-events: none; }
  .toast.show { opacity: 1; }
  /* "updated" pill: the viewed file changed on disk; refresh is opt-in (press r
     / click) so live writes never yank scroll position out from under you. */
  #fresh-pill { position: fixed; top: 46px; right: 26px; z-index: 20; display: none; align-items: center; gap: 6px; background: var(--card-bg); border: 1px solid var(--accent); color: var(--text); border-radius: 14px; padding: 4px 11px; font-size: 12px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.14); }
  #fresh-pill.show { display: inline-flex; }
  #fresh-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
  #fresh-pill kbd { font-family: var(--mono); font-size: 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 3px; padding: 0 4px; }
  #fresh-pill.diffing { border-color: var(--ins); }
  #fresh-pill.diffing .dot { background: var(--ins); }
  /* Diff view: the editorial proofing convention (<ins> underlined, <del>
     struck) rather than git's red/green, which fails red-green colour blindness.
     Tints are mixed from --bg so they sit on the warm paper in either theme. */
  #preview ins { text-decoration: none; background: color-mix(in srgb, var(--ins) 14%, var(--bg)); color: var(--ins); box-shadow: inset 0 -2px 0 var(--ins); border-radius: 2px; }
  #preview del { text-decoration: line-through; text-decoration-thickness: 1px; background: color-mix(in srgb, var(--del) 12%, var(--bg)); color: var(--del); border-radius: 2px; }
  #preview ins img, #preview del img { outline: 2px solid var(--ins); }
  #preview del img { outline-color: var(--del); opacity: 0.5; }
  #diff-bar { font-family: var(--mono); font-size: 11px; color: var(--muted); background: var(--card-bg); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; margin-bottom: 14px; }
  #diff-bar .warn { color: var(--err); }
  #diff-bar kbd { font-family: var(--mono); font-size: 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 3px; padding: 0 4px; }
  /* Source-mode diff: a <pre> instead of the textarea, since a textarea can't
     show markup. Copying (y) still fetches the raw bytes. */
  #preview-source-diff { white-space: pre-wrap; word-break: break-word; font-family: var(--mono); font-size: 12px; line-height: 1.55; color: var(--text); background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px; padding: 14px 16px; margin: 0; }
  .hbtn { font-family: var(--font); font-size: 12px; color: var(--muted); background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; cursor: pointer; text-transform: capitalize; }
  .hbtn:hover { color: var(--text); border-color: var(--border-strong); }
  .hbtn.active { color: var(--bg); background: var(--accent); border-color: var(--accent); }
  #search-bar { display: none; padding: 6px 10px; background: var(--card-bg); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 2; }
  #search-input { width: 100%; font-family: var(--font); font-size: 13px; color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 4px 8px; outline: none; }
  #search-input:focus { border-color: var(--accent); }
  .kbd-hint { font-size: 10px; color: var(--faint); }
  #list-hint { font-size: 10px; color: var(--faint); padding: 5px 10px; text-align: right; position: sticky; bottom: 0; background: var(--card-bg); border-top: 1px solid var(--border); }
  /* Reader view: hide the list panel and strip horizontal rules from content. */
  :root[data-reader] #list { display: none; }
  :root[data-reader] #preview h1, :root[data-reader] #preview h2, :root[data-reader] #preview h3 { border-bottom: none; padding-bottom: 0; }
  :root[data-reader] #preview hr { display: none; }
  :root[data-reader] #preview th, :root[data-reader] #preview td { border: none; }
  /* Tint the empty side gutters toward the screen edges, framing the centered
     reading column. Gutter width = (100% - column)/2. The tint holds solid for
     the outer sixth, then falls off steeply to transparent by ~two-thirds of
     the gutter, leaving the third nearest the text clear — so the darkening
     hugs the screen edge rather than ramping flat across the whole gutter. */
  :root[data-reader] #preview {
    background: linear-gradient(90deg,
      var(--vignette) 0,
      var(--vignette) calc((100% - var(--col)) / 12),
      color-mix(in srgb, var(--vignette), transparent 55%) calc((100% - var(--col)) / 4.5),
      transparent calc((100% - var(--col)) / 3),
      transparent calc(100% - (100% - var(--col)) / 3),
      color-mix(in srgb, var(--vignette), transparent 55%) calc(100% - (100% - var(--col)) / 4.5),
      var(--vignette) calc(100% - (100% - var(--col)) / 12),
      var(--vignette) 100%);
  }
  /* Zen header: collapse the control cluster to a minimal status line
     — mdfeed · live · [zen] · z · <path>. Non-zen chrome is hidden; the file
     path (normally rendered inside the preview) hops up into the header. */
  .flex-spacer { flex: 1; }
  .zen-only { display: none; }
  .zen-sep { color: var(--faint); }
  .zen-hint { font-size: 10px; color: var(--faint); font-family: var(--mono); }
  .zen-path { font-family: var(--mono); font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }
  :root[data-reader] .zen-only { display: inline-block; }
  :root[data-reader] .zen-hide { display: none; }
  :root[data-reader] .flex-spacer { display: none; }
  /* Keep the in-preview path's box so hiding it causes no layout shift. */
  :root[data-reader] #preview-path, :root[data-reader] #preview-raw-link { visibility: hidden; }
  :root[data-reader] .preview-inner { padding: 0 55px; }
  /* Collapsible headings: click to fold a section to the next same/higher level. */
  /* Dwell gutter: one bar per block, opacity = how long it was read. A layer of
     its own, measured from the blocks, because headings already spend ::before
     on the fold caret and list items indent; one fixed column reads cleaner. */
  .preview-inner { position: relative; }
  .dwell-gutter { position: absolute; top: 0; left: 0; width: 0; height: 0; pointer-events: none; }
  :root:not([data-heat]) .dwell-gutter { display: none; }
  .dwell-bar { position: absolute; width: 4px; border-radius: 2px; background: var(--accent); }
  .dwell-bar.agg { background: repeating-linear-gradient(var(--accent) 0 4px, transparent 4px 7px); }
  :root[data-painting] #preview .preview-inner { cursor: cell; }
  :root[data-painting] #preview [data-dwell].dwell-hover { outline: 1px dashed var(--faint); outline-offset: 3px; border-radius: 2px; }
  .row .meta .read { font-variant-numeric: tabular-nums; color: var(--accent); }
  .row .meta .read.stale { opacity: 0.5; }
  #preview .collapsible { cursor: pointer; }
  #preview .collapsible::before { content: '\\25be'; display: inline-block; width: 0.9em; margin-left: -0.9em; color: var(--faint); font-size: 0.8em; transition: transform 0.15s; }
  #preview .collapsible.collapsed::before { transform: rotate(-90deg); }
  #preview .collapsible.cursor::before { color: var(--accent); }
  /* Leader jump-mode: number the visible headings; press a digit to jump+expand.
     Hidden (collapsed-away) headings don't generate boxes, so they don't count. */
  :root[data-leader] .preview-inner { counter-reset: hd; }
  :root[data-leader] #preview .collapsible { counter-increment: hd; }
  :root[data-leader] #preview .collapsible::after { content: counter(hd); display: inline-block; margin-left: 0.6em; background: var(--accent); color: var(--bg); font-size: 0.6em; font-weight: 600; line-height: 1.5; padding: 0 6px; border-radius: 8px; vertical-align: middle; }
  /* ── Mobile ─────────────────────────────────────────────────────────────── */
  #back-btn { display: none; }
  @media (max-width: 640px) {
    header { padding: 10px 12px; gap: 8px; flex-wrap: nowrap; }
    .hbtn { padding: 5px 10px; }
    #count { display: none; }
    .kbd-hints, .kbd-hint, #list-hint { display: none; }
    /* No keyboard on mobile — drop the 'z' hint and its separators in zen. */
    :root[data-reader] .zen-hint, :root[data-reader] .zen-sep { display: none; }
    main { flex-direction: column; }
    #list { width: 100%; max-width: 100%; min-width: 0; border-right: none; resize: none; flex: 1; }
    #resizer { display: none; }
    /* Lanes on a phone: full-width panel, scene scrolls horizontally rather than
       letting the measured --lane-fit width drive the layout. */
    :root[data-lanes] #list { width: 100%; max-width: 100%; min-width: 0; overflow-x: auto; }
    /* default: list visible, preview hidden */
    #preview { display: none; padding: 12px 16px; }
    .preview-inner { max-width: 100%; }
    .row { padding: 10px 14px; }
    /* reader mode must not hide the list while browsing */
    :root[data-reader] #list { display: flex; flex-direction: column; }
    /* preview mode: swap panels and show back button */
    :root[data-mobile-view="preview"] #preview { display: block; overflow-y: auto; flex: 1; }
    :root[data-mobile-view="preview"] #list { display: none; }
    :root[data-mobile-view="preview"] #back-btn { display: inline-flex; align-items: center; }
  }
  /* ── Reading preferences panel (Aa button, ',' key) ─────────────────────── */
  #prefs { position: fixed; top: 44px; right: 14px; z-index: 30; width: 300px; background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 6px 24px color-mix(in srgb, var(--text) 18%, transparent); padding: 10px 12px 12px; font-size: 12px; }
  #prefs[hidden] { display: none; }
  #prefs .prow { margin-top: 10px; }
  #prefs .prow:first-child { margin-top: 0; }
  #prefs .plabel { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  #prefs .pseg { display: flex; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
  #prefs .pseg button { flex: 1; font-family: var(--font); font-size: 12px; color: var(--muted); background: var(--bg); border: 0; border-left: 1px solid var(--border); padding: 5px 4px; cursor: pointer; }
  #prefs .pseg button:first-child { border-left: 0; }
  #prefs .pseg button:hover { color: var(--text); }
  #prefs .pseg button[aria-pressed="true"] { color: var(--bg); background: var(--accent); }
  #prefs .pseg button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  /* Each typeface option is drawn in its own face, so the choice is visible. */
  #prefs .pseg button[data-face="atkinson"] { font-family: 'Atkinson Hyperlegible Next', sans-serif; }
  #prefs .pseg button[data-face="system"] { font-family: system-ui, sans-serif; }
  #prefs .pseg button[data-face="instrument"] { font-family: 'Instrument Sans', sans-serif; }
  #prefs .pfoot { margin-top: 10px; display: flex; justify-content: space-between; align-items: center; color: var(--faint); font-size: 10px; }
  #prefs .pfoot button { font-family: var(--font); font-size: 11px; color: var(--muted); background: none; border: 0; padding: 0; cursor: pointer; text-decoration: underline; }
  /* On a phone the header row overflows, and theme is also in the panel, so
     its standalone button makes room for Aa. The key hints have no keyboard. */
  @media (max-width: 640px) {
    #prefs { left: 12px; right: 12px; top: 52px; width: auto; }
    #prefs .pfoot span, #theme-toggle { display: none; }
  }
  /* A pref change cross-fades (view transition) unless motion is reduced. */
  ::view-transition-old(root), ::view-transition-new(root) { animation-duration: 160ms; }
  @media (prefers-reduced-motion: reduce) {
    ::view-transition-old(root), ::view-transition-new(root) { animation: none; }
  }
</style>
<script>
  // Reading preferences, applied before first paint so a stored font or size
  // never flashes the default first. This table is the single definition: the
  // main script reads it back from window.mdfeedPrefs to build the Aa panel.
  // A pref at its default is stored as absent and sets no attribute, so the
  // stylesheet's :root values are the defaults. Keys and values match what
  // the old theme and size toggles stored, so existing choices carry over.
  (() => {
    const PREFS = {
      font:    { key: 'mdfeed-font',    attr: 'data-font',    def: 'instrument', label: 'Typeface',
                 opts: [['instrument', 'Instrument'], ['atkinson', 'Atkinson'], ['system', 'System']] },
      size:    { key: 'mdfeed-size',    attr: 'data-size',    def: 'regular',    label: 'Text size',
                 opts: [['small', 'S'], ['regular', 'M'], ['large', 'L'], ['xlarge', 'XL']] },
      spacing: { key: 'mdfeed-spacing', attr: 'data-spacing', def: 'normal',     label: 'Line spacing',
                 opts: [['compact', 'Compact'], ['normal', 'Normal'], ['relaxed', 'Relaxed']] },
      width:   { key: 'mdfeed-width',   attr: 'data-width',   def: 'normal',     label: 'Column width',
                 opts: [['narrow', 'Narrow'], ['normal', 'Normal'], ['wide', 'Wide']] },
      theme:   { key: 'mdfeed-theme',   attr: 'data-theme',   def: 'auto',       label: 'Theme',
                 opts: [['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']] },
    };
    const read = (p) => {
      let v = null;
      try { v = localStorage.getItem(p.key); } catch (e) {}
      return p.opts.some(o => o[0] === v) ? v : p.def;
    };
    const apply = (p, v) => {
      if (v === p.def) document.documentElement.removeAttribute(p.attr);
      else document.documentElement.setAttribute(p.attr, v);
    };
    for (const p of Object.values(PREFS)) apply(p, read(p));
    try { if (localStorage.getItem('mdfeed-reader') === '1') document.documentElement.setAttribute('data-reader', ''); } catch (e) {}
    window.mdfeedPrefs = { PREFS, read, apply };
  })();
</script>
</head>
<body>
<header>
  <button id="back-btn" class="hbtn" title="Back to file list">← files</button>
  <span class="title">mdfeed</span>
  <span class="status" id="status">connecting…</span>
  <span class="zen-only zen-sep">·</span>
  <span class="flex-spacer"></span>
  <span class="status zen-hide" id="count"></span>
  <button id="search-btn" class="hbtn zen-hide" title="Search (/)" style="font-size:14px;line-height:1">⌕</button>
  <button id="reader-toggle" class="hbtn" title="Zen view (z)">zen</button>
  <span class="zen-only zen-sep">·</span>
  <span class="zen-only zen-hint">z</span>
  <span class="zen-only zen-sep">·</span>
  <span class="zen-only zen-path" id="zen-path"></span>
  <button id="source-toggle" class="hbtn zen-hide" title="Raw markdown source (s) · copy it (y)">source</button>
  <button id="theme-toggle" class="hbtn zen-hide" title="Theme (t)">auto</button>
  <button id="prefs-btn" class="hbtn zen-hide" title="Reading preferences: typeface, size, spacing, width (,)" aria-haspopup="dialog" aria-expanded="false" aria-controls="prefs">Aa</button>
  <button id="diff-btn" class="hbtn zen-hide" title="Diff against what you last saw (d)">diff</button>
  <button id="paint-btn" class="hbtn zen-hide" title="Painter: the block under the cursor counts as read (m) · show heat (h)">paint</button>
  <button id="refresh-btn" class="hbtn zen-hide" title="Refresh (r)" style="font-size:14px;padding:1px 6px;line-height:1">↻</button>
  <span class="status kbd-hints zen-hide">/ search<span class="sep">·</span>⇥ recent/plans<span class="sep">·</span>j/k section<span class="sep">·</span>e/c expand<span class="sep">·</span>g# jump<span class="sep">·</span>b copy path<span class="sep">·</span>s source<span class="sep">·</span>y copy md<span class="sep">·</span>d diff<span class="sep">·</span>z zen<span class="sep">·</span>t theme<span class="sep">·</span>f size<span class="sep">·</span>, prefs<span class="sep">·</span>m paint<span class="sep">·</span>h heat</span>
</header>
<main>
  <aside id="list">
    <div id="search-bar"><input id="search-input" type="text" placeholder="search files…" autocomplete="off" spellcheck="false"></div>
    <div id="viewtabs"></div>
    <div id="filters"></div>
    <div id="rows"><div class="empty">Loading…</div></div>
    <div id="list-hint">n/p · files</div>
  </aside>
  <div id="resizer" title="Drag to resize · double-click to reset"></div>
  <section id="preview"><div id="preview-empty">Pick an entry on the left.</div></section>
  <div id="fresh-pill" title="The file you're viewing changed on disk"><span class="dot"></span><span id="fresh-pill-text"></span></div>
</main>
<div class="toast" id="toast"></div>
<div id="prefs" role="dialog" aria-label="Reading preferences" hidden></div>
<script>
(() => {
  /*@HTMLDIFF_ENGINE@*/
  const $rows = document.getElementById('rows');
  const $filters = document.getElementById('filters');
  const $viewtabs = document.getElementById('viewtabs');
  const $preview = document.getElementById('preview');
  const $status = document.getElementById('status');
  const $count = document.getElementById('count');
  const $toast = document.getElementById('toast');
  const $searchBar = document.getElementById('search-bar');
  const $searchInput = document.getElementById('search-input');
  const $zenPath = document.getElementById('zen-path');
  const $list = document.getElementById('list');
  const $resizer = document.getElementById('resizer');
  const $freshPill = document.getElementById('fresh-pill');
  let entries = [];       // raw, newest-first
  let displayOrder = [];  // entries in DOM order (newest-first, after active filters), used for n/p nav
  let selectedIdx = 0;
  let selectedPath = null;
  let viewedTs = 0;       // feed ts of the file shown in the preview; a bump means it changed on disk
  // The diff baseline is whatever the tab last rendered — "what changed under my
  // eyes", not "since the last write". It survives several writes until the
  // user refreshes, so d always diffs against the version they actually read.
  let viewedHtml = '';    // rendered HTML as last shown (rendered mode)
  let diffTs = 0;         // feed ts the current diff was taken against; 0 = not in diff mode
  // Bumped by every preview load or diff. A fetch that resolves after a newer
  // one started is dropped, so a slow response for file A can't render under
  // file B's path — or, worse, diff A's content against B's baseline.
  let previewGen = 0;
  // Deep link from `mdfeed open`: ?path=<abs path> lands on that file instead
  // of the newest. Stripped from the URL once read, so a reload behaves like a
  // normal visit and the path doesn't linger in history.
  const deepPath = new URLSearchParams(location.search).get('path');
  if (deepPath) history.replaceState(null, '', location.pathname);
  let activeProjects = new Set();  // empty = show all
  let viewMode = localStorage.getItem('mdfeed-view') || 'recent';  // 'recent' (all, chronological) | 'plans' (plans only)
  let searchQuery = '';
  let filtersExpanded = false;     // chip overflow: collapsed by default
  const FILTER_LIMIT = 12;         // chips shown before the "+N more" toggle
  try { activeProjects = new Set(JSON.parse(localStorage.getItem('mdfeed-projects') || '[]')); } catch (e) {}

  function toast(msg) {
    $toast.textContent = msg;
    $toast.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => $toast.classList.remove('show'), 1400);
  }

  // List/preview splitter. Drag sets an inline width (clamped by CSS min/max-
  // width); double-click clears it so the panel returns to its auto width
  // (320px in recent/plans, or the measured --lane-fit in lanes).
  let resizeStart = null;
  $resizer.addEventListener('pointerdown', e => {
    e.preventDefault();
    resizeStart = { x: e.clientX, w: $list.getBoundingClientRect().width };
    $resizer.setPointerCapture(e.pointerId);
    $resizer.classList.add('dragging');
    document.body.style.userSelect = 'none';
  });
  $resizer.addEventListener('pointermove', e => {
    if (!resizeStart) return;
    $list.style.width = (resizeStart.w + e.clientX - resizeStart.x) + 'px';
  });
  function endResize() {
    if (!resizeStart) return;
    resizeStart = null;
    $resizer.classList.remove('dragging');
    document.body.style.userSelect = '';
  }
  $resizer.addEventListener('pointerup', endResize);
  $resizer.addEventListener('pointercancel', endResize);
  $resizer.addEventListener('dblclick', () => { $list.style.width = ''; toast('width reset'); });

  function fmtAgo(ts) {
    const d = Date.now()/1000 - ts;
    if (d < 5) return 'just now';
    if (d < 60) return Math.floor(d) + 's ago';
    if (d < 3600) return Math.floor(d/60) + 'm ago';
    if (d < 86400) return Math.floor(d/3600) + 'h ago';
    return Math.floor(d/86400) + 'd ago';
  }

  // String(t || '?') guards entries with no tool field — `t[0]` on undefined
  // would throw inside renderList and blank the whole list.
  function toolLetter(t) { return ({Write:'W', Edit:'E', MultiEdit:'M'}[t] || String(t || '?')[0]); }

  function basename(p) { return p.split('/').pop(); }

  function projectOf(e) { return e.project || '(none)'; }

  function searchScore(entry, q) {
    const name = basename(entry.path).toLowerCase().replace(/\\.md$/, '');
    if (name === q) return 0;
    if (name.startsWith(q)) return 1;
    return 2;
  }

  function searchOpen() { return $searchBar.style.display === 'block'; }

  function openSearch() {
    // On mobile the list (and the search bar inside it) is hidden while reading
    // a doc, so surface the list before revealing search.
    document.documentElement.setAttribute('data-mobile-view', 'list');
    searchQuery = '';
    $searchInput.value = '';
    $searchBar.style.display = 'block';
    $searchInput.focus();
    renderList();
  }

  function closeSearch() {
    searchQuery = '';
    $searchBar.style.display = 'none';
    renderList();
  }

  function toggleSearch() { searchOpen() ? closeSearch() : openSearch(); }

  $searchInput.addEventListener('input', () => {
    searchQuery = $searchInput.value;
    renderList();
  });

  $searchInput.addEventListener('keydown', e => {
    if (e.key === 'Escape' || e.key === 'ArrowDown') {
      e.preventDefault();
      closeSearch();
    } else if (e.key === 'Enter' || e.key === '/') {
      e.preventDefault();
      if (!searchQuery.trim()) { closeSearch(); return; }
      if (displayOrder.length) {
        activeProjects = new Set();
        localStorage.setItem('mdfeed-projects', '[]');
        selectByIndex(0);
      } else { toast('no match'); }
      closeSearch();
    }
  });

  // Chip per distinct project; click toggles it in/out of the active filter
  // (empty filter = show all). Selection persists in localStorage.
  // Recent|Plans is a view mode, not a pinned group: it sits orthogonal to the
  // project filter, so e.g. "project X's plans" composes for free. Counts reflect
  // the active project filter so the tab numbers match what clicking shows.
  function renderViewTabs() {
    const base = activeProjects.size ? entries.filter(e => activeProjects.has(projectOf(e))) : entries;
    const planCount = base.filter(e => e.category === 'plan').length;
    const projCount = new Set(base.map(projectOf)).size;
    const tabs = [['recent', 'Recent', base.length], ['plans', 'Plans', planCount], ['lanes', 'Lanes', projCount]];
    $viewtabs.innerHTML = tabs.map(([m, label, n]) =>
      '<button class="vtab' + (viewMode === m ? ' active' : '') + '" data-mode="' + m + '">' +
      label + '<span class="vcount">' + n + '</span></button>'
    ).join('');
    $viewtabs.querySelectorAll('.vtab').forEach(el =>
      el.addEventListener('click', () => setViewMode(el.dataset.mode)));
  }
  function setViewMode(mode) {
    if (mode !== 'recent' && mode !== 'plans' && mode !== 'lanes') return;
    viewMode = mode;
    localStorage.setItem('mdfeed-view', mode);
    toast(mode);
    renderList();
  }
  function toggleViewMode() { setViewMode(viewMode === 'recent' ? 'plans' : 'recent'); }

  function renderFilters() {
    const present = [...new Set(entries.map(projectOf))].sort();
    // Drop any persisted filters whose project no longer appears in the feed.
    for (const p of [...activeProjects]) if (!present.includes(p)) activeProjects.delete(p);
    if (present.length < 2) { $filters.innerHTML = ''; return; } // nothing to filter
    // Collapse a long project list to FILTER_LIMIT chips behind a "+N more"
    // toggle. Active filters are always surfaced so the current selection stays
    // visible even when it sorts past the cutoff.
    const overflow = present.length > FILTER_LIMIT;
    let visible = present;
    if (overflow && !filtersExpanded) {
      visible = present.slice(0, FILTER_LIMIT);
      for (const p of present) if (activeProjects.has(p) && !visible.includes(p)) visible.push(p);
    }
    let html = visible.map(p =>
      '<button class="fchip' + (activeProjects.has(p) ? ' active' : '') + '" data-proj="' +
      escapeAttr(p) + '">' + escapeHtml(p) + '</button>'
    ).join('');
    if (overflow) {
      html += filtersExpanded
        ? '<button class="fchip more" data-more="1">less</button>'
        : '<button class="fchip more" data-more="1">+' + (present.length - visible.length) + ' more</button>';
    }
    html += '<span class="kbd-hint" style="margin-left:auto;align-self:center">a all · x cycle</span>';
    $filters.innerHTML = html;
    $filters.querySelectorAll('.fchip[data-proj]').forEach(el => {
      el.addEventListener('click', () => toggleProject(el.dataset.proj));
    });
    const moreBtn = $filters.querySelector('.fchip[data-more]');
    if (moreBtn) moreBtn.addEventListener('click', () => { filtersExpanded = !filtersExpanded; renderFilters(); });
  }
  function toggleProject(p) {
    if (activeProjects.has(p)) activeProjects.delete(p); else activeProjects.add(p);
    localStorage.setItem('mdfeed-projects', JSON.stringify([...activeProjects]));
    renderList();
  }
  // 'x': cycle the filter through [no filter, then each project in chip order].
  function cycleFilter() {
    const present = [...new Set(entries.map(projectOf))].sort();
    if (!present.length) { toast('no projects'); return; }
    const cycle = [null].concat(present);  // index 0 = show all
    let pos = 0;
    if (activeProjects.size === 1) {
      const i = present.indexOf([...activeProjects][0]);
      if (i >= 0) pos = i + 1;
    }
    const next = cycle[(pos + 1) % cycle.length];
    activeProjects = next === null ? new Set() : new Set([next]);
    localStorage.setItem('mdfeed-projects', JSON.stringify([...activeProjects]));
    toast('filter: ' + (next === null ? 'all' : next));
    renderList();
    if (next !== null && displayOrder.length) selectByIndex(0);
  }

  function renderList() {
    renderFilters();
    renderViewTabs();
    document.documentElement.toggleAttribute('data-lanes', viewMode === 'lanes' && !searchQuery.trim());
    if (!entries.length) {
      $rows.innerHTML = '<div class="empty">No .md activity yet. Ask Claude to write a markdown file.</div>';
      $count.textContent = '';
      return;
    }
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toLowerCase();
      const matches = entries
        .filter(e => basename(e.path).toLowerCase().includes(q))
        .map((e, i) => ({ e, score: searchScore(e, q), i }))
        .sort((a, b) => a.score - b.score || a.i - b.i)
        .slice(0, 3).map(r => r.e);
      displayOrder = matches;
      $rows.innerHTML = matches.length ? matches.map(rowHtml).join('') : '<div class="empty">No match.</div>';
      $count.textContent = matches.length ? matches.length + ' match' + (matches.length > 1 ? 'es' : '') : '';
      $rows.querySelectorAll('.row').forEach((el, idx) => el.addEventListener('click', () => { selectByIndex(idx); closeSearch(); }));
      if (selectedPath) { const idx = displayOrder.findIndex(e => e.path === selectedPath); if (idx >= 0) markSelected(idx); }
      return;
    }
    let shown = activeProjects.size ? entries.filter(e => activeProjects.has(projectOf(e))) : entries;
    if (viewMode === 'plans') shown = shown.filter(e => e.category === 'plan');
    displayOrder = shown;  // natural newest-first order; plans no longer jump the queue
    if (viewMode === 'lanes') {
      renderLanes(shown);
      const total = entries.length;
      $count.textContent = activeProjects.size
        ? shown.length + ' of ' + total + ' file' + (total === 1 ? '' : 's')
        : total + ' file' + (total === 1 ? '' : 's');
      $rows.querySelectorAll('.lanepill').forEach((el, idx) => el.addEventListener('click', () => selectByIndex(idx)));
      if (selectedPath) { const idx = displayOrder.findIndex(e => e.path === selectedPath); if (idx >= 0) markSelected(idx); }
      return;
    }
    let html = shown.map(rowHtml).join('');
    if (!shown.length) html = '<div class="empty">' + (viewMode === 'plans' ? 'No plans yet.' : 'No files match the active filter.') + '</div>';
    $rows.innerHTML = html;
    const total = entries.length;
    $count.textContent = (activeProjects.size || viewMode === 'plans')
      ? shown.length + ' of ' + total + ' file' + (total === 1 ? '' : 's')
      : total + ' file' + (total === 1 ? '' : 's');
    $rows.querySelectorAll('.row').forEach((el, idx) => {
      el.addEventListener('click', () => { selectByIndex(idx); });
    });
    if (selectedPath) {
      const idx = displayOrder.findIndex(e => e.path === selectedPath);
      if (idx >= 0) markSelected(idx);
    }
  }

  function rowHtml(e) {
    const chipCls = e.category === 'plan' ? 'chip plan' : 'chip';
    const proj = e.project || '';
    const gone = e.exists === false ? ' gone' : '';
    return '<div class="row' + gone + '" data-path="' + escapeAttr(e.path) + '">' +
      '<div class="fname">' + escapeHtml(basename(e.path)) + '</div>' +
      '<div class="meta">' +
        '<span class="' + chipCls + '">' + escapeHtml(proj) + '</span>' +
        '<span class="tool">' + toolLetter(e.tool) + '</span>' +
        '<span>' + fmtAgo(e.ts) + '</span>' +
        readHtml(e.read) +
      '</div></div>';
  }
  function readHtml(read) {
    if (!read) return '';
    const pct = Math.round(read.pct);
    return '<span class="read' + (read.stale ? ' stale' : '') + '" title="' +
      (read.stale ? pct + '% read before the file last changed' : pct + '% read') + '">' + pct + '%</span>';
  }

  // ── Lanes rendering ───────────────────────────────────────────────────────
  const LANE_X0 = 54, HEAD_GAP = 126, N_HERO = 3, GAP_THRESHOLD = 2 * 3600;
  const LANE_COLORS = ['var(--lane-a)', 'var(--lane-b)', 'var(--lane-c)', 'var(--lane-x)'];
  // Headers keep a readable spacing (HEAD_GAP); the rails/pills squeeze left by
  // LANE_OFFSET per lane so the subway packs tighter. Lane 0 is fixed; color is
  // what ties a shifted rail back to its (unmoved) header. ?laneoffset=NN tunes.
  let LANE_OFFSET = 40;
  { const o = new URLSearchParams(location.search).get('laneoffset'); if (o !== null) LANE_OFFSET = +o; }
  function headX(i) { return LANE_X0 + i * HEAD_GAP; }
  function railX(i) { return headX(i) - i * LANE_OFFSET; }

  function fmtAgoShort(ts) {
    const d = Date.now() / 1000 - ts;
    if (d < 60) return Math.max(1, Math.floor(d)) + 's';
    if (d < 3600) return Math.floor(d / 60) + 'm';
    if (d < 86400) return Math.floor(d / 3600) + 'h';
    return Math.floor(d / 86400) + 'd';
  }
  function fmtGap(sec) {
    if (sec < 36 * 3600) return Math.round(sec / 3600) + 'h';
    return Math.round(sec / 86400) + 'd';
  }

  function headHtml(i, name, count) {
    return '<div class="lanehead" style="left:' + (headX(i) - 4) + 'px">' +
      '<span class="dot" style="background:' + LANE_COLORS[i] + '"></span>' +
      '<span class="name">' + escapeHtml(name) + '</span>' +
      '<span class="count">' + count + '</span></div>';
  }
  // Split a long filename across (at most) two lines, breaking on the space/_/-/.
  // nearest the center so the two lines stay balanced. Short names stay one line;
  // a name with no delimiter stays one line and is ellipsized by CSS.
  function splitName(name) {
    const MAXLEN = 28;  // ~200px at the pill font size; matches .fname max-width
    if (name.length <= MAXLEN) return [name];
    const delims = new Set([' ', '_', '-', '.']);
    const mid = name.length / 2;
    let best = -1, bestDist = Infinity;
    for (let i = 1; i < name.length - 1; i++) {
      if (delims.has(name[i]) && Math.abs(i - mid) < bestDist) { bestDist = Math.abs(i - mid); best = i; }
    }
    if (best < 0) return [name];
    const drop = name[best] === ' ' ? 0 : 1;  // keep _/-/. on line 1, drop a space
    return [name.slice(0, best + drop), name.slice(best + 1)];
  }

  function pillRowHtml(e, li, isOther, timeLabel) {
    const x = railX(li);
    const parts = splitName(basename(e.path));
    const ptag = isOther ? '<span class="ptag">·' + escapeHtml(projectOf(e)) + '</span>' : '';
    // The ·project tag (others lane) wraps onto its own line using the same
    // stacked-line treatment as a long filename, so it doesn't widen the pill.
    // Row height follows the total line count: name line(s) + the tag line.
    const lines = parts.length + (isOther ? 1 : 0);
    const rowH = 30 + (lines - 1) * 16;
    const cls = 'lanepill' + (e.category === 'plan' ? ' plan' : '') + (e.exists === false ? ' gone' : '');
    const ptext = '<span class="ptext"><span class="fname">' +
      parts.map(s => '<span>' + escapeHtml(s) + '</span>').join('') + '</span>' + ptag + '</span>';
    return '<div class="lanerow" style="height:' + rowH + 'px">' +
      '<div class="ltime">' + timeLabel + '</div>' +
      '<div class="lanenode" style="left:' + x + 'px;background:' + LANE_COLORS[li] + '"></div>' +
      '<div class="' + cls + '" data-path="' + escapeAttr(e.path) + '" style="left:' + (x + 12) + 'px" title="' + escapeAttr(projectOf(e) + ' · ' + e.path) + '">' +
        '<span class="ltool">' + toolLetter(e.tool) + '</span>' + ptext +
      '</div></div>';
  }

  // Hero lanes = the N_HERO most-recently-touched projects (by latest write),
  // ordered left→right by recency. Remaining projects merge into "others".
  function renderLanes(shown) {
    if (!shown.length) {
      $rows.innerHTML = '<div class="empty">No files match the active filter.</div>';
      return;
    }
    const recency = {};
    for (const e of shown) {
      const p = projectOf(e), t = e.ts || 0;
      if (!(p in recency) || t > recency[p]) recency[p] = t;
    }
    const ranked = Object.keys(recency).sort((a, b) => recency[b] - recency[a]);
    const hero = ranked.slice(0, N_HERO);
    const heroIdx = {};
    hero.forEach((p, i) => heroIdx[p] = i);
    const OTHERS = hero.length;                  // lane index for the merged rail
    const hasOthers = ranked.length > hero.length;
    const laneOf = p => (p in heroIdx ? heroIdx[p] : OTHERS);

    const counts = {};
    for (const e of shown) { const li = laneOf(projectOf(e)); counts[li] = (counts[li] || 0) + 1; }

    let heads = hero.map((p, i) => headHtml(i, p, counts[i] || 0)).join('');
    if (hasOthers) heads += headHtml(OTHERS, 'others', counts[OTHERS] || 0);

    const nLanes = OTHERS + (hasOthers ? 1 : 0);
    let rails = '';
    for (let i = 0; i < nLanes; i++) rails += '<div class="rail" style="left:' + railX(i) + 'px;background:' + LANE_COLORS[i] + '"></div>';

    // The time gutter only labels a row when its relative-age bucket differs from
    // the row above (a gap resets it), so runs of same-age rows read as one block.
    let body = '', prevTs = null, prevLabel = null;
    for (const e of shown) {  // shown is newest-first
      if (prevTs !== null && prevTs - e.ts > GAP_THRESHOLD) {
        body += '<div class="lanegap"><div class="gline"></div><div class="glabel">⋯ ' + fmtGap(prevTs - e.ts) + '</div></div>';
        prevLabel = null;
      }
      prevTs = e.ts;
      const label = fmtAgoShort(e.ts);
      const li = laneOf(projectOf(e));
      body += pillRowHtml(e, li, li === OTHERS && hasOthers, label === prevLabel ? '' : label);
      prevLabel = label;
    }
    $rows.innerHTML = '<div class="lanescene"><div class="laneheads">' + heads +
      '</div><div class="lanebody">' + rails + body + '</div></div>';

    // Shrink/grow the panel to the rightmost pill edge (see --lane-fit in CSS),
    // but never auto-grow past 50% of the viewport — only a manual drag may.
    let right = headX(nLanes - 1) + 16;
    $rows.querySelectorAll('.lanepill, .lanehead').forEach(el => { right = Math.max(right, el.offsetLeft + el.offsetWidth); });
    const fit = Math.min(right + 16, Math.round(window.innerWidth * 0.5));
    document.documentElement.style.setProperty('--lane-fit', fit + 'px');
  }

  // Park the sticky lane header just below the (also-sticky) filter bar so the
  // two don't collide at top:0 while scrolling. A ResizeObserver tracks the
  // filter bar's height as it changes (chip wrapping, +N more, web-font load) —
  // sampling it once is off by the post-load reflow. Fires on observe(), so the
  // initial value is set too; 0 when the bar is empty (display:none).
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      document.documentElement.style.setProperty('--lanehead-top', $filters.offsetHeight + 'px');
    }).observe($filters);
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function escapeAttr(s) { return escapeHtml(s); }

  function markSelected(idx) {
    document.querySelectorAll('.row, .lanepill').forEach(el => el.classList.remove('active'));
    const rows = document.querySelectorAll('.row, .lanepill');
    if (idx >= 0 && idx < rows.length) {
      rows[idx].classList.add('active');
      rows[idx].scrollIntoView({block: 'nearest'});
    }
    selectedIdx = idx;
  }

  // Select a file by path, clearing whatever would hide it. A deep link or a
  // focus event names a file the persisted project filter, the Plans view or an
  // open search may exclude, and silently doing nothing looks like a bug.
  // Filters are only cleared when the file is genuinely not on screen. Lanes is
  // left alone: it regroups the list but hides nothing.
  function selectByPath(path) {
    if (!path) return false;
    let idx = displayOrder.findIndex(e => e.path === path);
    if (idx < 0) {
      if (!entries.some(e => e.path === path)) return false;
      activeProjects = new Set();
      localStorage.setItem('mdfeed-projects', '[]');
      if (viewMode === 'plans') { viewMode = 'recent'; localStorage.setItem('mdfeed-view', 'recent'); }
      // closeSearch clears the query and re-renders; only render here otherwise.
      if (searchQuery) { $searchInput.value = ''; closeSearch(); } else { renderList(); }
      idx = displayOrder.findIndex(e => e.path === path);
      if (idx < 0) return false;
    }
    selectByIndex(idx);
    return true;
  }

  function selectByIndex(idx) {
    if (idx < 0 || idx >= displayOrder.length) return;
    selectedIdx = idx;
    selectedPath = displayOrder[idx].path;
    markSelected(idx);
    loadPreview(selectedPath);
    document.documentElement.setAttribute('data-mobile-view', 'preview');
  }

  // Fetch with a hard timeout so an unreachable/starved server surfaces a
  // visible error instead of hanging forever (see UNREACHABLE_HINT).
  function fetchWithTimeout(url, ms) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), ms || 8000);
    return fetch(url, { signal: ctrl.signal, cache: 'no-store' }).finally(() => clearTimeout(t));
  }
  const UNREACHABLE_HINT = 'Server unreachable (timed out). If several mdfeed tabs are open, close the extras — they exhaust the browser connection limit.';

  const $freshPillText = document.getElementById('fresh-pill-text');
  function latestTs() { return (entries.find(x => x.path === selectedPath) || {}).ts || 0; }
  // Three pill states: hidden (nothing newer than what you accepted), "updated"
  // (newer on disk; r or d), and "diff" (showing a redline that is itself
  // current). A write landing while a diff is up flips it back to "updated".
  function updatePill() {
    const latest = latestTs();
    if (!selectedPath || latest <= viewedTs) {
      $freshPill.classList.remove('show', 'diffing');
      return;
    }
    if (diffTs && latest <= diffTs) {
      $freshPillText.innerHTML = 'diff vs last view&nbsp;·&nbsp;<kbd>r</kbd>&nbsp;accept';
      $freshPill.classList.add('show', 'diffing');
    } else {
      $freshPillText.innerHTML = 'updated&nbsp;·&nbsp;<kbd>r</kbd>&nbsp;refresh&nbsp;·&nbsp;<kbd>d</kbd>&nbsp;diff';
      $freshPill.classList.add('show');
      $freshPill.classList.remove('diffing');
    }
  }
  // Manual, scroll-preserving refresh of the currently-viewed file.
  function refreshPreview() {
    if (!selectedPath) return;
    loadPreview(selectedPath, { keepScroll: true });
  }
  $freshPill.addEventListener('click', refreshPreview);

  // Redline of the viewed version against what is on disk now. Runs the
  // htmldiff engine over the two rendered-HTML strings (or the escaped raw
  // markdown in source mode), so the output is the document itself with
  // <ins>/<del> in it — not a line diff of the source.
  async function showDiff() {
    if (!selectedPath) return;
    const path = selectedPath;
    const mode = sourceMode;
    const before = mode ? currentRaw : viewedHtml;
    const keepY = $preview.scrollTop;
    const keepFolds = mode ? null : collapsedKeys();
    const latest = latestTs();
    const gen = ++previewGen;
    let j;
    try {
      const r = await fetchWithTimeout(
        '/api/render?path=' + encodeURIComponent(path) + (mode ? '&raw=1' : ''));
      if (gen !== previewGen) return;
      if (!r.ok) { toast('diff failed: ' + r.status); return; }
      j = await r.json();
      if (gen !== previewGen) return;
    } catch (e) {
      if (gen !== previewGen) return;
      toast(e.name === 'AbortError' ? 'server unreachable' : 'diff failed');
      return;
    }
    const after = mode ? (j.raw || '') : j.html;
    if (before === after) {
      // A Write that produced identical bytes still bumps the feed; the diff is
      // the one place that can tell "written" from "changed".
      viewedTs = Math.max(viewedTs, latest);
      diffTs = 0;
      updatePill();
      toast('no changes since you last looked');
      return;
    }
    // Without an orphan threshold the spaces between words all match, so a
    // replaced phrase renders as alternating one-word del/ins confetti. 0.2
    // drops those tiny matches and keeps a genuine single-word edit intact.
    const opts = { orphanMatchThreshold: 0.2 };
    let diffHtml;
    try {
      diffHtml = mode
        ? htmlDiff(escapeHtml(before), escapeHtml(after), opts)
        : htmlDiff(before, after, opts);
    } catch (e) {
      toast('diff failed: ' + e.message);
      return;
    }
    // Share of the text inside <ins>/<del>. A mostly-rewritten file makes a
    // redline into a wall of strikethrough; say so rather than pretend it reads.
    const probe = document.createElement('div');
    probe.innerHTML = diffHtml;
    const total = probe.textContent.length || 1;
    let changed = 0;
    probe.querySelectorAll('ins, del').forEach(el => { changed += el.textContent.length; });
    const pct = Math.round(100 * changed / total);
    // The engine matches on tag names with attributes stripped, so a link whose
    // target changed under the same label produces no markers at all. The
    // bytes differ, so say what kind of change it was rather than "0%".
    const markers = probe.querySelectorAll('ins, del').length;
    let note;
    if (markers === 0) {
      note = '<span class="warn">no text changed — only markup</span> (a link target, image, or formatting) · <kbd>s</kbd> then <kbd>d</kbd> shows it in the source · <kbd>r</kbd> accept';
    } else if (pct >= 60) {
      note = '<span class="warn">mostly rewritten (' + pct + '% of text changed)</span> — <kbd>r</kbd> for a clean refresh';
    } else {
      note = pct + '% of text changed since you last looked · <kbd>r</kbd> accept';
    }
    const bar = '<div id="diff-bar">' + note + '</div>';
    const pathHtml = previewBarHtml(path);
    if (mode) {
      $preview.innerHTML = '<div class="preview-inner">' + pathHtml + bar +
        '<pre id="preview-source-diff">' + diffHtml + '</pre></div>';
    } else {
      $preview.innerHTML = '<div class="preview-inner">' + pathHtml + bar + diffHtml + '</div>';
      makeCollapsible();
      applyCollapsed(keepFolds);
    }
    wirePreviewBar(path);
    $preview.scrollTop = Math.min(keepY, $preview.scrollHeight);
    diffTs = latest;
    updatePill();
  }
  document.getElementById('diff-btn').addEventListener('click', showDiff);

  function previewBarHtml(path) {
    return '<div id="preview-bar">' +
      '<div id="preview-path" title="click to copy">' + escapeHtml(path) + '</div>' +
      '<a id="preview-raw-link" href="/raw?path=' + encodeURIComponent(path) +
      '" target="_blank" title="raw markdown as text/plain">raw</a></div>';
  }
  function wirePreviewBar(path) {
    document.getElementById('preview-path').addEventListener('click', () => {
      navigator.clipboard.writeText(path);
      toast('path copied');
    });
  }

  async function loadPreview(path, opts) {
    opts = opts || {};
    // Capture scroll + which sections are folded before the DOM swap, so an
    // in-place refresh of the same file restores both (fresh selections don't).
    const keepY = opts.keepScroll ? $preview.scrollTop : 0;
    const keepFolds = opts.keepScroll && !sourceMode ? collapsedKeys() : null;
    flushDwell();  // the share read is measured against the blocks about to go
    const gen = ++previewGen;
    try {
      const r = await fetchWithTimeout(
        '/api/render?path=' + encodeURIComponent(path) + (sourceMode ? '&raw=1' : ''));
      if (gen !== previewGen) return;
      if (!r.ok) {
        $preview.innerHTML = '<div id="preview-empty">Failed to load: ' + r.status + '</div>';
        return;
      }
      const j = await r.json();
      if (gen !== previewGen) return;
      const pathHtml = previewBarHtml(path);
      if (sourceMode) {
        // Raw source in a textarea: .value assignment, never innerHTML, so the
        // markdown can't smuggle markup (e.g. a literal </textarea>) into the page.
        currentRaw = j.raw || '';
        viewedHtml = '';
        $preview.innerHTML = '<div class="preview-inner">' + pathHtml +
          '<textarea id="preview-source" readonly spellcheck="false"></textarea></div>';
        document.getElementById('preview-source').value = currentRaw;
      } else {
        currentRaw = '';
        viewedHtml = j.html;
        $preview.innerHTML = '<div class="preview-inner">' + pathHtml + j.html + '</div>';
      }
      $zenPath.textContent = path;
      $zenPath.title = path;
      wirePreviewBar(path);
      if (!sourceMode) {
        makeCollapsible();
        applyCollapsed(keepFolds);  // re-fold sections that were folded (refresh only)
        initDwell(path, gen);
      } else {
        clearDwell();
      }
      // Fresh selection jumps to top; a refresh restores scroll (clamped to the
      // new, possibly-shorter doc) so an in-place update doesn't move you.
      $preview.scrollTop = opts.keepScroll ? Math.min(keepY, $preview.scrollHeight) : 0;
      // Remember what we rendered; clears any pending "updated" or diff state.
      viewedTs = latestTs();
      diffTs = 0;
      updatePill();
    } catch (e) {
      if (gen !== previewGen) return;
      const msg = e.name === 'AbortError' ? UNREACHABLE_HINT : 'Error: ' + escapeHtml(e.message);
      $preview.innerHTML = '<div id="preview-empty">' + msg + '</div>';
    }
  }

  // ── Source view: rendered ⇄ raw-markdown textarea ─────────────────────────
  let sourceMode = false;
  let currentRaw = '';  // raw markdown of the current preview (source mode only)
  const $sourceToggle = document.getElementById('source-toggle');

  function toggleSource() {
    sourceMode = !sourceMode;
    $sourceToggle.classList.toggle('active', sourceMode);
    toast(sourceMode ? 'source' : 'rendered');
    if (selectedPath) loadPreview(selectedPath, { keepScroll: true });
  }
  $sourceToggle.addEventListener('click', toggleSource);

  async function copySource() {
    if (!selectedPath) return;
    let raw = sourceMode ? currentRaw : '';
    if (!raw) {
      try {
        const r = await fetchWithTimeout(
          '/api/render?path=' + encodeURIComponent(selectedPath) + '&raw=1');
        if (r.ok) raw = (await r.json()).raw || '';
      } catch (e) { /* fall through to the empty-raw toast */ }
    }
    if (!raw) { toast('nothing to copy'); return; }
    navigator.clipboard.writeText(raw);
    toast('markdown copied');
  }

  async function loadFeed(autoSelect) {
    try {
      const r = await fetchWithTimeout('/api/feed');
      const j = await r.json();
      entries = j;
      renderList();
      // A deep link decides the first selection; otherwise take the newest.
      if (autoSelect && deepPath && selectByPath(deepPath)) return;
      if (autoSelect && entries.length && !selectedPath) {
        selectByIndex(0);
      } else if (selectedPath) {
        // Only react when the file we're *viewing* actually changed (its feed ts
        // advanced) — not when some other file was written. Don't auto-replace;
        // surface the "updated" pill and let the user refresh (or diff) on their terms.
        updatePill();
      }
    } catch (e) {
      console.error('feed fetch failed', e);
      $status.textContent = e.name === 'AbortError' ? 'unreachable' : 'offline';
      $status.className = 'stale';
    }
  }

  let sse = null;
  let bootId = null;  // server boot id; a change across reconnect = server restarted
  function connectSSE() {
    if (sse) return; // already open
    sse = new EventSource('/events');
    sse.onopen = () => { $status.textContent = 'live'; $status.className = 'live'; };
    sse.onerror = () => {
      $status.textContent = 'reconnecting…';
      $status.className = 'stale';
    };
    sse.addEventListener('feed-changed', () => { loadFeed(false); });
    // `mdfeed open` (and the Finder droplet behind it) appends to the feed and
    // then writes a focus marker. Re-read the feed first: the marker can arrive
    // on the same tick as the append, and the file has to be in `entries`
    // before it can be selected.
    // Once selected, tell the server: `open` waits for that ack before
    // reporting success, because a connection the server still counts can
    // belong to a tab that has already closed.
    sse.addEventListener('focus', async e => {
      let msg = {};
      try { msg = JSON.parse(e.data) || {}; } catch (err) { return; }
      const path = msg.path || '';
      if (!path) return;
      await loadFeed(false);
      if (!selectByPath(path)) return;
      toast(path.split('/').pop());
      fetch('/api/focus-ack?ts=' + encodeURIComponent(msg.ts)).catch(() => {});
    });
    // Dev live-reload: after a --reload restart, EventSource auto-reconnects and
    // reports a new boot id; refresh to pick up UI changes baked into the HTML.
    sse.addEventListener('boot', e => {
      if (bootId !== null && e.data !== bootId) { location.reload(); return; }
      bootId = e.data;
    });
  }
  function disconnectSSE() {
    if (sse) { sse.close(); sse = null; }
  }
  // Release the SSE connection while the tab is backgrounded. Each open tab
  // otherwise holds a connection forever, and browsers cap concurrent
  // connections per origin (~6) — enough hidden tabs starve the foreground one.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      disconnectSSE();
      $status.textContent = 'paused'; $status.className = 'status';
    } else {
      connectSSE();
      loadFeed(false);
    }
  });

  // ── Dwell heatmap ─────────────────────────────────────────────────────────
  // Painter mode (m) counts the block under the cursor as being read, sampled
  // on a timer rather than per mousemove. Blocks are keyed by a hash of their
  // text plus an ordinal among identical blocks, so heat survives reflow
  // (font, width, folds) and carries over to whatever an edit left unchanged:
  // an edited block gets a new key and reads as cold. Heat (h) is a gutter bar
  // per block; a folded heading's bar stands for its whole section. Totals
  // live server-side (/api/dwell) so they outlast the tab and the browser.
  const DWELL_TICK_MS = 250;
  const DWELL_IDLE_MS = 45000;      // a cursor parked this long stops counting
  const DWELL_READ_SECS = 2;        // a block counts toward "% read" from here
  const DWELL_SATURATE_SECS = 60;
  let DWELL_FLUSH_MS = 3000;
  { const v = new URLSearchParams(location.search).get('dwellflush'); if (v !== null) DWELL_FLUSH_MS = +v; }
  const $paintBtn = document.getElementById('paint-btn');
  let painting = false;
  let dwellPath = null;      // the file whose blocks are keyed right now
  let dwellHeat = {};        // block key -> seconds: server totals plus local ticks
  let dwellPending = {};     // block key -> seconds not yet posted
  let dwellPendingPath = null;
  let dwellHover = null;
  let dwellLastMove = 0;
  let dwellPointer = null;   // last {x, y}, to re-hit-test on scroll
  let dwellBars = {};        // block key -> gutter bar element
  let dwellAgg = {};         // folded heading key -> keys of its whole section
  let dwellObserver = null;

  function fnv1a(s) {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
    return (h >>> 0).toString(36);
  }
  function blockText(el) {
    let node = el;
    // A list item's own text, not its sublist's: the sublist items are blocks too.
    if (el.tagName === 'LI' && el.querySelector('ul,ol')) {
      node = el.cloneNode(true);
      node.querySelectorAll('ul,ol').forEach(n => n.remove());
    }
    const t = (node.textContent || '').replace(/\\s+/g, ' ').trim();
    if (t) return el.tagName + ':' + t;
    const img = el.querySelector('img');
    return img ? el.tagName + ':img:' + (img.getAttribute('src') || '') : '';
  }
  function dwellBlocks(root) {
    return [...root.querySelectorAll('h1,h2,h3,h4,h5,h6,p,pre,table,li')].filter(el => {
      if (el.closest('#preview-bar, #diff-bar, details.frontmatter')) return false;
      // A paragraph, code block or table inside a list item reads as part of it.
      return el.tagName === 'LI' || !el.parentElement.closest('li');
    });
  }
  function keyBlocks(root) {
    const seen = {};
    for (const el of dwellBlocks(root)) {
      const t = blockText(el);
      if (!t) continue;
      const h = fnv1a(t);
      seen[h] = (seen[h] || 0) + 1;
      el.dataset.dwell = h + '#' + seen[h];
    }
  }
  function heatLevel(s) {
    return s > 0 ? Math.min(1, 0.15 + 0.85 * Math.log1p(s) / Math.log1p(DWELL_SATURATE_SECS)) : 0;
  }
  function paintBar(k) {
    const bar = dwellBars[k];
    if (!bar) return;
    const keys = dwellAgg[k];
    const lvl = keys
      ? keys.reduce((a, x) => a + heatLevel(dwellHeat[x] || 0), 0) / keys.length
      : heatLevel(dwellHeat[k] || 0);
    bar.style.opacity = lvl.toFixed(3);
  }
  function paintAll() { for (const k in dwellBars) paintBar(k); }

  function layoutHeat() {
    const root = previewRoot();
    if (!root || dwellPath !== selectedPath) return;
    let gutter = root.querySelector(':scope > .dwell-gutter');
    if (!root.querySelector('[data-dwell]')) { if (gutter) gutter.remove(); dwellBars = {}; return; }
    if (!gutter) {
      // First child, not last: refreshCollapse hides every child after a folded
      // heading, and the gutter must never be one of them.
      gutter = document.createElement('div');
      gutter.className = 'dwell-gutter';
      gutter.setAttribute('aria-hidden', 'true');
      root.prepend(gutter);
    }
    dwellAgg = {};
    let open = null;  // the folded heading whose section is being collected
    for (const el of root.children) {
      const lvl = headLevel(el);
      if (lvl && open && lvl <= open.lvl) open = null;
      if (open) {
        if (el.dataset.dwell) open.keys.push(el.dataset.dwell);
        el.querySelectorAll('[data-dwell]').forEach(b => open.keys.push(b.dataset.dwell));
      } else if (lvl && el.classList.contains('collapsed') && el.dataset.dwell) {
        open = { lvl, keys: [el.dataset.dwell] };
        dwellAgg[el.dataset.dwell] = open.keys;
      }
    }
    const rr = root.getBoundingClientRect();
    const x = parseFloat(getComputedStyle(root).paddingLeft) - 14;
    const frag = document.createDocumentFragment();
    dwellBars = {};
    for (const el of root.querySelectorAll('[data-dwell]')) {
      const r = el.getBoundingClientRect();
      if (!r.height) continue;  // folded away
      let h = r.height;
      const sub = el.tagName === 'LI' && el.querySelector(':scope > ul, :scope > ol');
      if (sub) h = Math.max(4, sub.getBoundingClientRect().top - r.top);
      const bar = document.createElement('div');
      bar.className = 'dwell-bar' + (dwellAgg[el.dataset.dwell] ? ' agg' : '');
      bar.dataset.for = el.dataset.dwell;
      bar.style.left = x + 'px';
      bar.style.top = (r.top - rr.top) + 'px';
      bar.style.height = h + 'px';
      dwellBars[el.dataset.dwell] = bar;
      frag.appendChild(bar);
    }
    gutter.replaceChildren(frag);
    paintAll();
  }

  async function initDwell(path, gen) {
    const root = previewRoot();
    if (!root) return;
    dwellPath = path;
    dwellHeat = {};
    setHover(null);
    keyBlocks(root);
    if (dwellObserver) dwellObserver.disconnect();
    // Folds, font and width changes all resize the column; one observer covers them.
    let queued = false;
    dwellObserver = new ResizeObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; layoutHeat(); });
    });
    dwellObserver.observe(root);
    layoutHeat();
    try {
      const r = await fetchWithTimeout('/api/dwell?path=' + encodeURIComponent(path));
      if (gen !== previewGen || !r.ok) return;
      const j = await r.json();
      if (gen !== previewGen) return;
      const server = (j.totals && j.totals.dwell) || {};
      for (const [k, s] of Object.entries(server)) dwellHeat[k] = (dwellHeat[k] || 0) + s;
      paintAll();
    } catch (e) { /* heat is best-effort; the document still reads */ }
  }
  function clearDwell() {
    dwellPath = null;
    dwellBars = {};
    setHover(null);
    if (dwellObserver) { dwellObserver.disconnect(); dwellObserver = null; }
  }

  function readPct() {
    const root = previewRoot();
    if (!root) return null;
    const keys = [...root.querySelectorAll('[data-dwell]')].map(el => el.dataset.dwell);
    if (!keys.length) return null;
    return 100 * keys.filter(k => (dwellHeat[k] || 0) >= DWELL_READ_SECS).length / keys.length;
  }
  function noteRead(path, pct) {
    const e = entries.find(x => x.path === path);
    if (e) e.read = { pct, stale: false };
    const row = $rows.querySelector('.row[data-path="' + CSS.escape(path) + '"] .meta');
    if (!row) return;
    const old = row.querySelector('.read');
    if (old) old.remove();
    row.insertAdjacentHTML('beforeend', readHtml({ pct, stale: false }));
  }
  function flushDwell() {
    const blocks = dwellPending, path = dwellPendingPath;
    dwellPending = {};
    if (!path || !Object.keys(blocks).length) return;
    const body = { path, kind: 'dwell', blocks };
    // Only while this file's blocks are on screen can the share be measured.
    if (path === dwellPath) {
      const pct = readPct();
      if (pct !== null) { body.pct = pct; noteRead(path, pct); }
    }
    fetch('/api/dwell', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), keepalive: true,
    }).then(r => { if (!r.ok) throw new Error(r.status); }).catch(() => {
      // Put it back for the next flush rather than drop what was read.
      if (dwellPendingPath !== path) return;
      for (const [k, s] of Object.entries(blocks)) dwellPending[k] = (dwellPending[k] || 0) + s;
    });
  }

  function setHover(el) {
    if (dwellHover === el) return;
    if (dwellHover) dwellHover.classList.remove('dwell-hover');
    dwellHover = el;
    if (el && painting) el.classList.add('dwell-hover');
  }
  function hoverAt(x, y) {
    const t = document.elementFromPoint(x, y);
    setHover(t && $preview.contains(t) ? t.closest('[data-dwell]') : null);
  }
  $preview.addEventListener('mousemove', (e) => {
    dwellPointer = { x: e.clientX, y: e.clientY };
    dwellLastMove = Date.now();
    if (painting) hoverAt(e.clientX, e.clientY);
  });
  $preview.addEventListener('mouseleave', () => { dwellPointer = null; setHover(null); });
  // Scrolling moves the text under a still cursor, and counts as activity.
  $preview.addEventListener('scroll', () => {
    if (!painting || !dwellPointer) return;
    dwellLastMove = Date.now();
    hoverAt(dwellPointer.x, dwellPointer.y);
  }, { passive: true });

  setInterval(() => {
    if (!painting || document.hidden || !document.hasFocus()) return;
    const el = dwellHover;
    if (!el || !el.isConnected || !el.dataset.dwell || dwellPath !== selectedPath) return;
    if (Date.now() - dwellLastMove > DWELL_IDLE_MS) return;
    const k = el.dataset.dwell, dt = DWELL_TICK_MS / 1000;
    dwellHeat[k] = (dwellHeat[k] || 0) + dt;
    if (dwellPendingPath !== dwellPath) { flushDwell(); dwellPendingPath = dwellPath; }
    dwellPending[k] = (dwellPending[k] || 0) + dt;
    paintBar(k);
  }, DWELL_TICK_MS);
  setInterval(flushDwell, DWELL_FLUSH_MS);
  document.addEventListener('visibilitychange', () => { if (document.hidden) flushDwell(); });
  window.addEventListener('pagehide', flushDwell);

  function setHeat(on) {
    document.documentElement.toggleAttribute('data-heat', on);
    try { localStorage.setItem('mdfeed-heat', on ? '1' : '0'); } catch (e) {}
    if (on) layoutHeat();
  }
  function toggleHeat() {
    const on = !document.documentElement.hasAttribute('data-heat');
    setHeat(on);
    toast(on ? 'heat: shown' : 'heat: hidden');
  }
  function togglePainting() {
    painting = !painting;
    document.documentElement.toggleAttribute('data-painting', painting);
    $paintBtn.classList.toggle('active', painting);
    if (painting) {
      // Painting with the heat hidden would give no feedback at all.
      if (!document.documentElement.hasAttribute('data-heat')) setHeat(true);
      if (dwellPointer) hoverAt(dwellPointer.x, dwellPointer.y);
      dwellLastMove = Date.now();
    } else {
      setHover(null);
      flushDwell();
    }
    toast(painting ? 'painting: hover to read' : 'painting: off');
  }
  $paintBtn.addEventListener('click', togglePainting);
  try { if (localStorage.getItem('mdfeed-heat') === '1') document.documentElement.setAttribute('data-heat', ''); } catch (e) {}

  // Collapsible headings. Clicking a heading folds everything beneath it up to
  // the next heading of equal or higher level, so nested subsections fold too.
  function headLevel(el) { return /^H[1-6]$/.test(el.tagName) ? +el.tagName[1] : 0; }
  function previewRoot() { return $preview.querySelector('.preview-inner'); }

  function refreshCollapse() {
    const root = previewRoot();
    if (!root) return;
    const stack = []; // levels of currently-collapsed ancestor sections
    for (const el of root.children) {
      const lvl = headLevel(el);
      if (lvl) while (stack.length && stack[stack.length - 1] >= lvl) stack.pop();
      el.style.display = stack.length ? 'none' : '';
      if (lvl && el.classList.contains('collapsed')) stack.push(lvl);
    }
    layoutHeat();  // a fold changes which heading stands for a section
  }
  // Pick the level to collapse-all at: skip leading levels that occur only once
  // (e.g. a lone title H1) and fold from the first level that repeats — so a
  // single H1 over many H2s collapses to the H2 outline, not into the H1.
  function outlineLevel(heads) {
    const counts = {};
    heads.forEach((h) => { const l = headLevel(h); counts[l] = (counts[l] || 0) + 1; });
    const levels = Object.keys(counts).map(Number).sort((a, b) => a - b);
    let i = 0;
    while (i < levels.length - 1 && counts[levels[i]] === 1) i++;
    return levels[i];
  }
  function setAllCollapsed(on) {
    const root = previewRoot();
    if (!root) return;
    const heads = [...root.children].filter(headLevel);
    if (!heads.length) return;
    const target = on ? outlineLevel(heads) : 0;
    heads.forEach((h) => h.classList.toggle('collapsed', on && headLevel(h) >= target));
    refreshCollapse();
    toast(on ? 'collapsed all' : 'expanded all');
  }
  // Mark a heading as the navigation "cursor" (accent caret), clearing the old one.
  function setCursor(h) {
    const prev = $preview.querySelector('.collapsible.cursor');
    if (prev) prev.classList.remove('cursor');
    if (h) h.classList.add('cursor');
  }
  function makeCollapsible() {
    const root = previewRoot();
    if (!root) return;
    for (const el of root.children) {
      if (!headLevel(el)) continue;
      el.classList.add('collapsible');
      el.addEventListener('click', (ev) => {
        if (ev.target.closest('a')) return;
        el.classList.toggle('collapsed');
        setCursor(el);
        refreshCollapse();
      });
    }
  }

  // Fold state survives an in-place refresh by keying headings on level+text
  // (the DOM is rebuilt, so element identity is gone). Duplicate heading texts
  // collapse together — acceptable for the common case.
  function headKey(el) { return headLevel(el) + ':' + (el.textContent || '').trim(); }
  function collapsedKeys() {
    const root = previewRoot();
    if (!root) return null;
    const keys = new Set();
    for (const el of root.children) {
      if (headLevel(el) && el.classList.contains('collapsed')) keys.add(headKey(el));
    }
    return keys;
  }
  function applyCollapsed(keys) {
    if (!keys || !keys.size) return;
    const root = previewRoot();
    if (!root) return;
    for (const el of root.children) {
      if (headLevel(el) && keys.has(headKey(el))) el.classList.add('collapsed');
    }
    refreshCollapse();
  }

  const SECTION_PAD = 12;
  // Visible (rendered) headings in document order. Hidden/collapsed ones excluded.
  function visibleHeadings() {
    const root = previewRoot();
    return root ? [...root.children].filter((el) => headLevel(el) && el.offsetParent !== null) : [];
  }
  function scrollToHeading(h) {
    const base = $preview.getBoundingClientRect().top - $preview.scrollTop;
    const top = Math.round(h.getBoundingClientRect().top - base);
    setCursor(h);
    $preview.scrollTo({ top: Math.max(0, top - SECTION_PAD), behavior: 'smooth' });
  }

  // Jump the preview scroll to the previous/next heading. dir < 0 = up.
  function scrollToSection(dir) {
    const heads = visibleHeadings();
    if (!heads.length) return;
    const base = $preview.getBoundingClientRect().top - $preview.scrollTop;
    const tops = heads.map((h) => Math.round(h.getBoundingClientRect().top - base));
    // After a jump the target heading sits PAD below the top, so the reference
    // point for "current section" is scrollTop + PAD — excludes where we just landed.
    const ref = $preview.scrollTop + SECTION_PAD;
    let idx;
    if (dir > 0) { idx = tops.findIndex((t) => t > ref + 2); }
    else { idx = -1; for (let i = tops.length - 1; i >= 0; i--) { if (tops[i] < ref - 2) { idx = i; break; } } if (idx < 0) idx = 0; }
    if (idx < 0) return; // nothing further in this direction
    scrollToHeading(heads[idx]);
  }

  // Leader jump: press 'g' to number the visible headings, then a digit to
  // expand+jump to that one. Multi-digit accumulates with a short debounce.
  let leaderActive = false;
  let leaderBuf = '';
  let leaderTimer = null;

  function enterLeader() {
    if (!visibleHeadings().length) return;
    leaderActive = true;
    leaderBuf = '';
    document.documentElement.setAttribute('data-leader', '');
  }
  function exitLeader() {
    leaderActive = false;
    leaderBuf = '';
    clearTimeout(leaderTimer);
    document.documentElement.removeAttribute('data-leader');
  }
  function leaderJump(n) {
    const heads = visibleHeadings();
    exitLeader();
    const target = heads[n - 1];
    if (!target) return;
    if (target.classList.contains('collapsed')) { target.classList.remove('collapsed'); refreshCollapse(); }
    scrollToHeading(target);
  }

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (leaderActive) {
      e.preventDefault();
      if (/^[0-9]$/.test(e.key)) {
        leaderBuf += e.key;
        clearTimeout(leaderTimer);
        const n = parseInt(leaderBuf, 10);
        const count = visibleHeadings().length;
        // Jump immediately once no longer number could be valid; else debounce.
        if (n * 10 > count) leaderJump(n);
        else leaderTimer = setTimeout(() => leaderJump(n), 600);
      } else {
        exitLeader(); // Esc or any non-digit cancels
      }
      return;
    }
    if (e.key === 'Tab') { e.preventDefault(); toggleViewMode(); return; }  // switch Recent/Plans; visible control, costs no letter key
    if (e.key === '/') { e.preventDefault(); openSearch(); }
    else if (e.key === 'j') { e.preventDefault(); scrollToSection(1); }
    else if (e.key === 'k') { e.preventDefault(); scrollToSection(-1); }
    else if (e.key === 'n') { e.preventDefault(); selectByIndex(Math.min(selectedIdx + 1, displayOrder.length - 1)); }
    else if (e.key === 'p') { e.preventDefault(); selectByIndex(Math.max(selectedIdx - 1, 0)); }
    else if (e.key === 'x') { e.preventDefault(); cycleFilter(); }
    else if (e.key === 'a') { e.preventDefault(); activeProjects = new Set(); localStorage.setItem('mdfeed-projects', '[]'); toast('filter: all'); renderList(); }
    else if (e.key === 'g') { e.preventDefault(); enterLeader(); }
    else if (e.key === 't') { e.preventDefault(); cycleTheme(); }
    else if (e.key === 'f') { e.preventDefault(); cycleSize(); }
    else if (e.key === 'z') { e.preventDefault(); toggleReader(); }
    else if (e.key === ',') { e.preventDefault(); togglePrefs(); }
    else if (e.key === 'b') { e.preventDefault(); if (selectedPath) { navigator.clipboard.writeText(selectedPath); toast('path copied'); } }
    else if (e.key === 'e') { e.preventDefault(); setAllCollapsed(false); }
    else if (e.key === 'c') { e.preventDefault(); setAllCollapsed(true); }
    else if (e.key === 'r') { e.preventDefault(); refreshPreview(); }
    else if (e.key === 'd') { e.preventDefault(); showDiff(); }
    else if (e.key === 's') { e.preventDefault(); toggleSource(); }
    else if (e.key === 'y') { e.preventDefault(); copySource(); }
    else if (e.key === 'm') { e.preventDefault(); togglePainting(); }
    else if (e.key === 'h') { e.preventDefault(); toggleHeat(); }
  });

  // Reading preferences. The table and the attribute writer live in the
  // <head> script (window.mdfeedPrefs) so stored choices land before first
  // paint; this half owns changing them: the Aa panel, the t/f cycle keys,
  // font loading, keeping the reader's place, and cross-tab sync.
  const { PREFS, read: readPref, apply: applyPref } = window.mdfeedPrefs;
  const $themeToggle = document.getElementById('theme-toggle');
  const $prefsBtn = document.getElementById('prefs-btn');
  const $prefs = document.getElementById('prefs');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  // Faces a typeface needs before the swap. Google's CSS only declares them;
  // nothing downloads until text is drawn in the face, so switching without
  // this paints old font, then fallback, then new font. Loading first makes it
  // one step. Capped so an offline viewer still switches (to the fallback).
  const FONT_FACES = {
    atkinson: ['400 16px "Atkinson Hyperlegible Next"', '600 16px "Atkinson Hyperlegible Next"',
               'italic 400 16px "Atkinson Hyperlegible Next"', '400 12px "Atkinson Hyperlegible Mono"'],
    instrument: ['400 16px "Instrument Sans"', '600 16px "Instrument Sans"'],
  };
  function loadFaces(font) {
    const faces = FONT_FACES[font];
    if (!faces || !document.fonts || !document.fonts.load) return Promise.resolve();
    const all = Promise.all(faces.map(f => document.fonts.load(f).catch(() => {})));
    return Promise.race([all, new Promise(r => setTimeout(r, 1500))]);
  }

  // A size, spacing, width or face change reflows the document. Pin the first
  // block visible at the top of the preview and put it back where it was, so
  // changing the text size mid-read does not lose the reader's place.
  function captureAnchor() {
    const top = $preview.getBoundingClientRect().top;
    for (const el of $preview.querySelectorAll('.preview-inner > *')) {
      const r = el.getBoundingClientRect();
      if (r.bottom > top) return { el, offset: r.top - top };
    }
    return null;
  }
  function restoreAnchor(a) {
    if (!a || !a.el.isConnected) return;
    const top = $preview.getBoundingClientRect().top;
    $preview.scrollTop += (a.el.getBoundingClientRect().top - top) - a.offset;
  }

  // The chosen value lives here, set the moment a choice is made. Storage is
  // only persistence: a write can fail (blocked site data), and the swap that
  // writes it runs a frame later inside the view transition, so reading the
  // current value back from storage lost steps on a fast double 'f'.
  const prefState = {};
  for (const [name, p] of Object.entries(PREFS)) prefState[name] = readPref(p);
  // Bumped per change; a font change that finishes loading after a newer
  // choice was made is dropped instead of overwriting it.
  const prefSeq = {};

  function renderPrefsChrome() {
    $themeToggle.textContent = prefState.theme;
    if ($prefs.hidden) return;
    for (const btn of $prefs.querySelectorAll('button[data-pref]')) {
      btn.setAttribute('aria-pressed', String(prefState[btn.dataset.pref] === btn.dataset.value));
    }
  }

  async function setPref(name, value) {
    const p = PREFS[name];
    const seq = prefSeq[name] = (prefSeq[name] || 0) + 1;
    prefState[name] = value;
    renderPrefsChrome();
    if (name === 'font') await loadFaces(value);
    if (seq !== prefSeq[name]) return;
    const swap = () => {
      const anchor = captureAnchor();
      try {
        if (value === p.def) localStorage.removeItem(p.key);
        else localStorage.setItem(p.key, value);
      } catch (e) {}
      applyPref(p, value);
      restoreAnchor(anchor);
      renderPrefsChrome();
    };
    // Cross-fade the change rather than snapping. Skipped under reduced
    // motion; where the API is missing the swap simply happens.
    if (document.startViewTransition && !reducedMotion.matches) document.startViewTransition(swap);
    else swap();
  }
  function cyclePref(name) {
    const p = PREFS[name];
    const vals = p.opts.map(o => o[0]);
    const next = vals[(vals.indexOf(prefState[name]) + 1) % vals.length];
    setPref(name, next);
    toast(p.label.toLowerCase() + ': ' + p.opts.find(o => o[0] === next)[1].toLowerCase());
  }
  const cycleTheme = () => cyclePref('theme');
  const cycleSize = () => cyclePref('size');

  function buildPrefsPanel() {
    const rows = Object.entries(PREFS).map(([name, p]) =>
      '<div class="prow"><div class="plabel">' + p.label + '</div>' +
      '<div class="pseg" role="group" aria-label="' + p.label + '">' +
      p.opts.map(([v, text]) =>
        '<button type="button" data-pref="' + name + '" data-value="' + v + '"' +
        (name === 'font' ? ' data-face="' + v + '"' : '') + '>' + text + '</button>').join('') +
      '</div></div>');
    $prefs.innerHTML = rows.join('') +
      '<div class="pfoot"><span>f size · t theme · Esc close</span>' +
      '<button type="button" id="prefs-reset">reset</button></div>';
  }
  function openPrefs() {
    if (!$prefs.firstChild) buildPrefsPanel();
    $prefs.hidden = false;
    $prefsBtn.setAttribute('aria-expanded', 'true');
    $prefsBtn.classList.add('active');
    renderPrefsChrome();
    const cur = $prefs.querySelector('button[aria-pressed="true"]');
    if (cur) cur.focus({ preventScroll: true });
  }
  function closePrefs(refocus) {
    if ($prefs.hidden) return;
    $prefs.hidden = true;
    $prefsBtn.setAttribute('aria-expanded', 'false');
    $prefsBtn.classList.remove('active');
    if (refocus) $prefsBtn.focus({ preventScroll: true });
  }
  const togglePrefs = () => ($prefs.hidden ? openPrefs() : closePrefs(true));

  $prefs.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    if (btn.id === 'prefs-reset') {
      for (const [name, p] of Object.entries(PREFS)) if (prefState[name] !== p.def) setPref(name, p.def);
      return;
    }
    if (btn.dataset.pref) setPref(btn.dataset.pref, btn.dataset.value);
  });
  // Keys typed inside the panel stay there: Esc or ',' closes it, arrows move
  // within a row, f/t (named in the panel footer) still cycle size and theme,
  // and the other global shortcuts must not fire while a segment has focus.
  $prefs.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' || e.key === ',') { e.preventDefault(); closePrefs(true); }
    else if (e.key === 'f' || e.key === 't') {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      e.preventDefault();
      (e.key === 'f' ? cycleSize : cycleTheme)();
    }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      const seg = [...e.target.parentElement.querySelectorAll('button')];
      const i = seg.indexOf(e.target);
      if (i >= 0) {
        e.preventDefault();
        seg[(i + (e.key === 'ArrowRight' ? 1 : seg.length - 1)) % seg.length].focus();
      }
    }
    e.stopPropagation();
  });
  document.addEventListener('click', (e) => {
    if (!$prefs.hidden && !$prefs.contains(e.target) && e.target !== $prefsBtn) closePrefs(false);
  });
  $prefsBtn.addEventListener('click', togglePrefs);
  $themeToggle.addEventListener('click', cycleTheme);

  // Another tab changed a pref: follow it without animating or re-storing.
  window.addEventListener('storage', (e) => {
    for (const [name, p] of Object.entries(PREFS)) {
      if (e.key !== p.key) continue;
      prefState[name] = readPref(p);
      applyPref(p, prefState[name]);
      renderPrefsChrome();
    }
  });
  renderPrefsChrome();

  // Reader view: hide the list panel and strip content rules. Persisted.
  const $readerToggle = document.getElementById('reader-toggle');
  let readerOn = localStorage.getItem('mdfeed-reader') === '1';

  function applyReader() {
    const root = document.documentElement;
    if (readerOn) root.setAttribute('data-reader', '');
    else root.removeAttribute('data-reader');
    $readerToggle.classList.toggle('active', readerOn);
  }
  function toggleReader() {
    readerOn = !readerOn;
    localStorage.setItem('mdfeed-reader', readerOn ? '1' : '0');
    applyReader();
    toast('zen: ' + (readerOn ? 'on' : 'off'));
  }
  $readerToggle.addEventListener('click', toggleReader);
  applyReader();

  document.getElementById('refresh-btn').addEventListener('click', () => {
    loadFeed(false);
    if (selectedPath) refreshPreview();
    toast('refreshed');
  });

  document.getElementById('back-btn').addEventListener('click', () => {
    document.documentElement.setAttribute('data-mobile-view', 'list');
    const active = document.querySelector('.row.active, .lanepill.active');
    if (active) active.scrollIntoView({ block: 'nearest' });
  });

  // Touch-reachable search: the / shortcut is keyboard-only and its hint is
  // hidden on mobile, so expose the same toggle as a header button.
  document.getElementById('search-btn').addEventListener('click', toggleSearch);

  loadFeed(true);
  connectSSE();
  // Belt-and-suspenders polling in case SSE drops silently. Skipped while
  // hidden so backgrounded tabs hold no connections at all.
  setInterval(() => { if (!document.hidden && $status.className !== 'live') loadFeed(false); }, 5000);
})();
</script>
</body>
</html>
"""


# Feed files are rendered with raw HTML passthrough (escape=False + innerHTML),
# and they often carry content from untrusted sources — a web page Claude
# summarized can plant <img onerror=…> that would otherwise run in this origin
# and read /api/feed + /raw for every feed file. The CSP pins script execution
# to the hash of our own inline <script> (inline handlers and javascript: URLs
# in rendered markdown don't match it, so they're blocked). img-src stays open
# so notes can embed images.
# The redline engine is a copy of section 1 of ../htmldiff/htmldiff.html (a
# port of htmldiff.net). It lives in a sibling file, not inline, so the drift
# test can compare the two byte-for-byte; it is spliced in here, before the CSP
# hash below is taken, so the hash covers it. Wrapped in its own strict-mode
# scope so its dozen helper names stay out of the UI script's namespace.
_ENGINE_JS = (Path(__file__).resolve().parent / "htmldiff_engine.js").read_text(encoding="utf-8")
INDEX_HTML = INDEX_HTML.replace(
    "/*@HTMLDIFF_ENGINE@*/",
    'const htmlDiff = (() => {\n"use strict";\n' + _ENGINE_JS + "\nreturn htmlDiff;\n})();",
)
# Two inline scripts: the small <head> one that applies stored reading prefs
# before first paint, and the UI script. Each gets its own hash.
_SCRIPT_BODIES = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.DOTALL)
_SCRIPT_HASHES = " ".join(
    f"'sha256-{base64.b64encode(hashlib.sha256(b.encode('utf-8')).digest()).decode()}'"
    for b in _SCRIPT_BODIES
)
CSP = (
    "default-src 'none'; "
    f"script-src {_SCRIPT_HASHES}; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "img-src * data:; "
    "base-uri 'none'; "
    "form-action 'none'"
)


class FeedHandler(http.server.BaseHTTPRequestHandler):
    feed_path: Path = DEFAULT_FEED
    # DNS-rebinding guard, set by cmd_serve: enforced when bound to loopback,
    # skipped when the user explicitly binds a non-loopback interface (their
    # LAN hostname/IP can't be enumerated here, and they opted into exposure).
    require_local_host: bool = True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Quiet by default; uncomment for debugging.
        # super().log_message(format, *args)
        pass

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        """DNS-rebinding guard: a hostile page can point its own domain at
        127.0.0.1, but it can't forge the Host header — so when we're bound to
        loopback, only accept requests addressed to a loopback name."""
        if not self.require_local_host:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # [::1]:port
            host = host[1 : host.find("]")]
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        return host in ("localhost", "127.0.0.1", "::1")

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send(403, b"forbidden host", "text/plain")
            return
        url = urlparse(self.path)
        path = url.path
        if path == "/":
            self._send(
                200,
                INDEX_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
                extra_headers={"Content-Security-Policy": CSP},
            )
            return
        if path == "/api/feed":
            self._send_json(200, with_read_shares(load_feed(self.feed_path), self.feed_path))
            return
        if path == "/api/dwell":
            target = (parse_qs(url.query).get("path") or [""])[0]
            if target not in allowed_paths(self.feed_path):
                self._send_json(403, {"error": "path not in feed"})
                return
            self._send_json(200, {"totals": dwell_totals(self.feed_path, target)})
            return
        if path == "/api/status":
            self._send_json(
                200,
                {
                    "clients": self.server.sse_census(),
                    "focus_acked": self.server.acked_focus(),
                    "boot": BOOT_ID,
                    "feed": str(self.feed_path),
                },
            )
            return
        if path == "/api/focus-ack":
            # A tab reporting that it selected the file a focus event named.
            # Any page can fire a GET here, so a forged ack must be able to
            # vouch for at most the one request whose stamp it names.
            try:
                ts = float((parse_qs(url.query).get("ts") or [""])[0])
            except ValueError:
                ts = None
            if ts is not None and math.isfinite(ts):
                self.server.ack_focus(ts)
            self._send(204, b"", "text/plain")
            return
        if path == "/api/render":
            qs = parse_qs(url.query)
            target = (qs.get("path") or [""])[0]
            want_raw = (qs.get("raw") or ["0"])[0] == "1"
            self._handle_render(target, want_raw)
            return
        if path == "/raw":
            # The linkable raw view: the file's bytes as text/plain, so the
            # browser shows exactly what's on disk (curl-able, save-as-able).
            qs = parse_qs(url.query)
            self._handle_raw((qs.get("path") or [""])[0])
            return
        if path == "/events":
            self._handle_sse()
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send(403, b"forbidden host", "text/plain")
            return
        # Any page in the browser can POST to loopback. Requiring a JSON content
        # type makes a cross-origin fetch preflight, which this server never
        # answers, and a form cannot send one; the Origin check covers the rest.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        origin = self.headers.get("Origin")
        host = self.headers.get("Host") or ""
        if ctype != "application/json" or (origin is not None and origin != f"http://{host}"):
            self._send(403, b"forbidden", "text/plain")
            return
        if urlparse(self.path).path != "/api/dwell":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= 512 * 1024:
            self._send_json(400, {"error": "bad length"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "bad body"})
            return
        self._handle_dwell_post(body)

    def _handle_dwell_post(self, body: Dict[str, Any]) -> None:
        target = body.get("path")
        kind = body.get("kind", "dwell")
        if not isinstance(target, str) or target not in allowed_paths(self.feed_path):
            self._send_json(403, {"error": "path not in feed"})
            return
        if kind not in DWELL_KINDS:
            self._send_json(400, {"error": "unknown kind"})
            return
        try:
            version = doc_version(Path(target))
        except OSError:
            self._send_json(404, {"error": "file missing on disk"})
            return
        n = append_dwell(
            self.feed_path, target, version, clean_dwell_blocks(body.get("blocks")), kind
        )
        pct = body.get("pct")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0 <= pct <= 100:
            record_read_share(self.feed_path, target, float(pct), version)
        self._send_json(200, {"recorded": n, "version": version})

    def _handle_raw(self, target: str) -> None:
        """Serve a feed file's raw markdown as text/plain. Same allowlist as
        render — only paths already in the feed, nothing else on disk."""
        if not target:
            self._send(400, b"missing path", "text/plain")
            return
        if target not in allowed_paths(self.feed_path):
            self._send(403, b"path not in feed", "text/plain")
            return
        p = Path(target)
        if not p.is_file():
            self._send(404, b"file missing on disk", "text/plain")
            return
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._send(500, f"read failed: {e}".encode("utf-8"), "text/plain")
            return
        self._send(200, raw.encode("utf-8"), "text/plain; charset=utf-8")

    def _handle_render(self, target: str, want_raw: bool = False) -> None:
        if not target:
            self._send_json(400, {"error": "missing path"})
            return
        allowed = allowed_paths(self.feed_path)
        if target not in allowed:
            self._send_json(403, {"error": "path not in feed"})
            return
        p = Path(target)
        if not p.is_file():
            self._send_json(404, {"error": "file missing on disk"})
            return
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._send_json(500, {"error": f"read failed: {e}"})
            return
        try:
            fm_html, body = render_frontmatter(raw)
            rendered = fm_html + _MD(body)
        except Exception as e:
            rendered = f"<pre>render error: {html.escape(str(e))}\n\n{html.escape(raw)}</pre>"
        resp = {"html": rendered}
        # Raw markdown only on request (?raw=1) — feeds the source view (s)
        # and copy-markdown (y); omitted by default to keep the payload lean.
        if want_raw:
            resp["raw"] = raw
        self._send_json(200, resp)

    def _handle_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_mtime = self._feed_mtime()
        # Seeded from what is already on disk: a tab connecting now must not
        # act on a focus request some earlier `mdfeed open` left behind.
        last_focus_sig = self._focus_sig()
        last_focus = (read_focus(self.feed_path) or {}).get("ts") or 0
        self.server.sse_census(+1)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.write(f"event: boot\ndata: {BOOT_ID}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                time.sleep(0.5)
                # A keepalive every tick, not every 15s. A socket whose tab has
                # closed only errors on a write, and until one fails it stays in
                # the census, which is what `open` reads to decide whether a tab
                # is up. Three bytes a tick bounds that lag to about a second;
                # the focus ack covers what is left of it.
                self.wfile.write(b":\n\n")
                m = self._feed_mtime()
                if m != last_mtime:
                    last_mtime = m
                    self.wfile.write(b"event: feed-changed\ndata: {}\n\n")
                # Stat, and parse only on a change: this runs twice a second per
                # tab. write_focus replaces the file, so the inode moves even
                # when two writes land within one mtime tick.
                sig = self._focus_sig()
                if sig != last_focus_sig:
                    last_focus_sig = sig
                    focus = read_focus(self.feed_path) or {}
                    ts = focus.get("ts") or 0
                    if ts > last_focus:
                        last_focus = ts
                        payload = json.dumps({"path": focus.get("path") or "", "ts": ts})
                        self.wfile.write(f"event: focus\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.server.sse_census(-1)

    def _focus_sig(self) -> Optional[Tuple[int, int]]:
        try:
            st = focus_path_for(self.feed_path).stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns)

    def _feed_mtime(self) -> float:
        try:
            return self.feed_path.stat().st_mtime
        except OSError:
            return 0.0


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    # Held per server rather than per process: it describes who is connected to
    # this listener. A module-level count let one test's lingering connections
    # leak into the next test's assertions.
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state_lock = threading.Lock()
        # Viewers holding an SSE connection. `open` reads it through
        # /api/status, but only as a hint: zero means open a tab without
        # waiting. A closed tab lingers here until a write to it fails, so
        # whether a tab really moved is the focus ack's job, not this count's.
        self._sse_clients = 0
        # Stamps of focus requests some tab reported selecting, matched
        # exactly. A running maximum let one forged ts=1e300 acknowledge every
        # request after it. Bounded, since anyone can add to it.
        self._focus_acked: "collections.deque[float]" = collections.deque(maxlen=32)

    def sse_census(self, delta: int = 0) -> int:
        with self._state_lock:
            self._sse_clients += delta
            return self._sse_clients

    def ack_focus(self, ts: float) -> None:
        with self._state_lock:
            if ts not in self._focus_acked:
                self._focus_acked.append(ts)

    def acked_focus(self) -> List[float]:
        with self._state_lock:
            return list(self._focus_acked)

    def handle_error(self, request, client_address) -> None:
        # Browsers routinely reset kept-alive/EventSource sockets; that's normal,
        # not an error worth a traceback. Surface anything else.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def _safe_mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None  # editors briefly unlink-then-rewrite; treat as not-ready


def _reload_on_change(path: str, interval: float = 0.5, settle: float = 0.4) -> None:
    """Re-exec the process when its source file changes (dev --reload).

    Restart, not importlib.reload (which leaves stale state). The listening
    socket is non-inheritable by default (PEP 446), so it closes on exec and the
    fresh process rebinds via allow_reuse_address. Single-process, so the
    debounce + ast.parse preflight below guard against acting on a half-written
    or unparseable save (no supervisor to recover from a bad exec).
    """
    last = _safe_mtime(path)
    while True:
        time.sleep(interval)
        m = _safe_mtime(path)
        if m is None or m == last:
            continue
        time.sleep(settle)  # debounce: let a multi-step save finish
        if _safe_mtime(path) != m:
            continue  # still in flux; reassess next tick without touching `last`
        last = m
        try:
            ast.parse(Path(path).read_bytes(), filename=path)
        except SyntaxError as e:
            print(
                f"mdfeed: skip reload — {os.path.basename(path)} won't parse "
                f"(line {e.lineno}: {e.msg})",
                file=sys.stderr,
            )
            continue
        except OSError:
            continue  # vanished mid-read; pick it up next tick
        print("mdfeed: source changed, reloading…", file=sys.stderr)
        os.execv(sys.executable, [sys.executable, *sys.argv])


def cmd_serve(args) -> int:
    # Absolute, because /api/status reports it and `open` acts on it from a
    # different working directory.
    feed_path = (Path(args.feed).expanduser() if args.feed else DEFAULT_FEED).resolve()
    FeedHandler.feed_path = feed_path
    FeedHandler.require_local_host = args.host in ("127.0.0.1", "localhost", "::1")

    try:
        server = ThreadingServer((args.host, args.port), FeedHandler)
    except OSError as e:
        print(f"failed to bind {args.host}:{args.port}: {e}", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}/"
    print(f"mdfeed serving {feed_path} at {url}")
    print("Ctrl-C to stop.")

    if not args.no_browser:

        def delayed_open():
            time.sleep(0.6)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=delayed_open, daemon=True).start()

    if args.reload:
        src = os.path.abspath(__file__)
        print(f"auto-reload: watching {src}")
        threading.Thread(target=_reload_on_change, args=(src,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        server.server_close()
    return 0


def cmd_add(args) -> int:
    feed_path = Path(args.feed).expanduser() if args.feed else DEFAULT_FEED
    paths = [Path(p) for p in args.paths]
    added, errors = append_entries(feed_path, paths, tool="Add")
    for err in errors:
        print(f"skip: {err}", file=sys.stderr)
    print(f"added {added} entr{'y' if added == 1 else 'ies'} to {feed_path}")
    return 0 if added > 0 or not errors else 1


# Every probe of the viewer goes through this opener, never urlopen. urlopen
# honours http_proxy and the macOS system proxy, and then a question about our
# own port is answered by the proxy: a free port reads as occupied, and `open`
# never starts a viewer. An empty ProxyHandler replaces the default one.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def server_status(host: str, port: int, timeout: float = 0.7) -> Optional[Dict[str, Any]]:
    """The running viewer's /api/status.

    Three outcomes, and the difference matters: the payload, `None` when nothing
    is listening, and `{}` when something answered but not with a status — an
    mdfeed predating /api/status, or an unrelated service on the port. Only
    `None` means the port is ours to start a server on. Conflating the two sent
    `mdfeed open` off to bind a port that was already taken, and it spent the
    whole start-up timeout failing to.

    The line falls between connecting and answering. urllib wraps a failure to
    connect or send in URLError, so that — and only that — is `None`. Anything
    after the connection was accepted reaches us unwrapped: a status line that
    isn't HTTP (BadStatusLine), a peer that hangs up without a word
    (RemoteDisconnected, which is also a ConnectionResetError and so must be
    caught first), or one that never replies (a timeout). Every one of those is
    a listener, so the port is taken.
    """
    try:
        with _DIRECT.open(f"http://{host}:{port}/api/status", timeout=timeout) as r:
            body = r.read()
    except (urllib.error.HTTPError, http.client.HTTPException):
        return {}
    except urllib.error.URLError:
        return None
    except OSError:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def start_server(
    host: str, port: int, feed_path: Path, wait: float = 10.0
) -> Optional[Dict[str, Any]]:
    """Launch a detached viewer and wait for it to answer. None on timeout.

    sys.executable, not this script's shebang: we are already running inside
    the uv environment that has mistune, so the child needs no second uv
    resolution — which matters because a Finder-launched parent has no uv on
    PATH.

    Two `open`s racing both get here and both spawn. The loser's bind fails
    (logged, and it exits), and the poll below still answers, because by then
    the winner is serving.
    """
    log = feed_path.parent / "serve.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--feed",
        str(feed_path),
        "--no-browser",
    ]
    try:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n--- mdfeed open started a server at {time.strftime('%F %T')}\n")
            fh.flush()
            subprocess.Popen(  # noqa: S603
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=fh,
                # Detached: it must outlive this process, and must not die with
                # the terminal (or the droplet) that started it.
                start_new_session=True,
                cwd=str(Path.home()),
            )
    except OSError as e:
        print(f"could not start a viewer: {e}", file=sys.stderr)
        return None
    deadline = time.time() + wait
    while time.time() < deadline:
        st = server_status(host, port, timeout=0.5)
        if st is not None:
            return st
        time.sleep(0.25)
    return None


def raise_browser() -> bool:
    """Bring the default browser to the front without opening a tab (macOS).

    A tab that jumps to the requested file is useless if the window is behind
    Finder. `open <url>` would raise the browser too, but only by spending a
    tab — and the whole point of the focus marker is not to.
    """
    if sys.platform != "darwin":
        return False
    plist = (
        Path.home()
        / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    bundle_id = None
    try:
        raw = subprocess.run(  # noqa: S603
            ["/usr/bin/plutil", "-convert", "json", "-o", "-", str(plist)],
            capture_output=True,
            timeout=5,
        )
        handlers = json.loads(raw.stdout.decode("utf-8")).get("LSHandlers") or []
        for h in handlers:
            if h.get("LSHandlerURLScheme") == "http" and h.get("LSHandlerRoleAll"):
                bundle_id = h["LSHandlerRoleAll"]
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # no readable plist: nobody ever changed the default
    # No http handler recorded means the default was never changed, and the
    # system default is Safari. That is the stock setup, not an edge case.
    bundle_id = bundle_id or "com.apple.Safari"
    try:
        return (
            subprocess.run(  # noqa: S603
                ["/usr/bin/open", "-b", bundle_id], capture_output=True, timeout=5
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


# How long `open` waits for a tab to confirm it moved. The SSE loop ticks every
# 0.5s and the tab re-reads the feed before selecting, so a live tab answers in
# well under a second; the rest is slack for a loaded machine.
FOCUS_ACK_WAIT = 3.0


def wait_for_focus_ack(host: str, port: int, ts: float, wait: Optional[float] = None) -> bool:
    """True once some tab reports selecting the focus request stamped `ts`.

    Exact membership, not "some ack at least this new": see ack_focus.
    """
    deadline = time.time() + (FOCUS_ACK_WAIT if wait is None else wait)
    while True:
        st = server_status(host, port, timeout=0.5) or {}
        acked = st.get("focus_acked")
        if isinstance(acked, list) and ts in acked:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.15)


def cmd_open(args) -> int:
    # Absolute: a server this starts runs with its cwd at $HOME, and would
    # read a relative --feed as a different file.
    feed_path = (Path(args.feed).expanduser() if args.feed else DEFAULT_FEED).resolve()
    targets, errors = resolve_markdown(Path(p) for p in args.paths)
    for err in errors:
        print(f"skip: {err}", file=sys.stderr)
    if not targets:
        return 1
    # Several files all land in the feed; the viewer opens on the last one, so
    # that `mdview *.md` behaves like opening them in order.
    target = targets[-1]

    # Probe before writing anything: which feed the entry belongs in depends on
    # who is serving.
    st = server_status(args.host, args.port)
    served = (st or {}).get("feed")
    if served and Path(served).expanduser().resolve() != feed_path:
        # The server's allowlist is its own feed. An entry written anywhere
        # else is a file it will refuse to render, and a focus marker beside
        # that other feed is one no SSE loop is watching.
        if args.feed:
            print(
                f"the viewer on {args.host}:{args.port} serves {served}, not {feed_path}; "
                f"stop it, or pass a --port with no viewer on it",
                file=sys.stderr,
            )
            return 1
        print(f"using the running viewer's feed, {served}", file=sys.stderr)
        feed_path = Path(served).expanduser().resolve()

    append_resolved(feed_path, targets, tool="Open")

    if st == {}:
        # An older viewer still shows the right file: it selects the newest feed
        # entry, which is the one just appended. It cannot be steered, though.
        print(
            f"whatever is serving {args.host}:{args.port} has no /api/status "
            f"(an mdfeed older than this one?) — opening a tab rather than steering it",
            file=sys.stderr,
        )
    if st is None:
        st = start_server(args.host, args.port, feed_path)
        if st is None:
            print(
                f"no viewer on {args.host}:{args.port} and starting one timed out "
                f"(see {feed_path.parent / 'serve.log'})",
                file=sys.stderr,
            )
            return 1

    if st.get("clients") and not args.new_tab:
        # Somebody may be watching: steer that tab rather than spending another
        # SSE connection (browsers cap them per origin, and every extra tab
        # starves the rest). "May", because the count includes a tab closed a
        # moment ago whose socket has not failed a write yet — so success is a
        # tab saying it selected the file, and silence falls through to a new tab.
        ts = write_focus(feed_path, target)
        if wait_for_focus_ack(args.host, args.port, ts):
            if not args.no_raise:
                raise_browser()
            print(f"opened {target.name} in the mdfeed tab already up")
            return 0
        print(
            f"no connected tab acknowledged within {FOCUS_ACK_WAIT:g}s — opening a new one",
            file=sys.stderr,
        )

    url = f"http://{args.host}:{args.port}/?path={quote(str(target))}"
    if args.no_browser:
        print(url)
        return 0
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"could not open a browser: {e}\n{url}", file=sys.stderr)
        return 1
    print(f"opened {target.name} at {url}")
    return 0


def cmd_list(args) -> int:
    feed_path = Path(args.feed).expanduser() if args.feed else DEFAULT_FEED
    for entry in load_feed(feed_path):
        print(entry["path"])
    return 0


def cmd_compact(args) -> int:
    feed_path = Path(args.feed).expanduser() if args.feed else DEFAULT_FEED
    if not feed_path.is_file():
        print(f"no feed at {feed_path}", file=sys.stderr)
        return 1
    before, after, bak_path = compact_feed(
        feed_path,
        keep=args.keep,
        prune_missing=args.prune_missing,
        backup=not args.no_backup,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"would compact {feed_path}: {before} → {after} lines (dry run, nothing written)")
    else:
        note = f" (backup: {bak_path})" if bak_path else " (no backup)"
        print(f"compacted {feed_path}: {before} → {after} lines{note}")
    return 0


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        # A negative count would hit Python's negative-slice semantics in
        # compact_feed and rewrite the feed as (nearly) empty.
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _add_feed_flag(p: argparse.ArgumentParser, for_sub: bool) -> None:
    """Register --feed. Subparsers get default=SUPPRESS: argparse fills one
    namespace in two passes (main parser, then subparser), so a real default
    on the subparser would clobber a value given before the subcommand
    (`--feed x add …`). SUPPRESS leaves the first pass's value alone."""
    p.add_argument(
        "--feed",
        default=argparse.SUPPRESS if for_sub else None,
        help=f"feed file (default {DEFAULT_FEED})",
    )


def _add_serve_flags(p: argparse.ArgumentParser, for_sub: bool, reload: bool = True) -> None:
    def d(value):
        return argparse.SUPPRESS if for_sub else value

    p.add_argument(
        "--port", type=int, default=d(DEFAULT_PORT), help=f"port (default {DEFAULT_PORT})"
    )
    p.add_argument("--host", default=d("127.0.0.1"), help="host (default 127.0.0.1)")
    p.add_argument(
        "--no-browser", action="store_true", default=d(False), help="don't open a browser tab"
    )
    if not reload:
        # `open` takes the address flags but has no server of its own to watch.
        return
    p.add_argument(
        "--reload",
        action="store_true",
        default=d(False),
        help="dev: re-exec on source change and live-reload the browser",
    )


def build_parser() -> argparse.ArgumentParser:
    # Flags live on the top-level parser (compat: `mdfeed.py --port 8050`) AND
    # on each subparser, so the natural `mdfeed.py serve --port 8050` works too.
    ap = argparse.ArgumentParser(description="Live viewer for the Claude md-feed.")
    _add_feed_flag(ap, for_sub=False)
    _add_serve_flags(ap, for_sub=False)
    sub = ap.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="run the web viewer (default if no subcommand given)")
    _add_feed_flag(serve_p, for_sub=True)
    _add_serve_flags(serve_p, for_sub=True)
    serve_p.set_defaults(func=cmd_serve)

    add_p = sub.add_parser("add", help="manually add markdown files to the feed")
    _add_feed_flag(add_p, for_sub=True)
    add_p.add_argument("paths", nargs="+", help="one or more .md paths")
    add_p.set_defaults(func=cmd_add)

    open_p = sub.add_parser(
        "open", help="add markdown files and show one in the viewer (starting it if needed)"
    )
    _add_feed_flag(open_p, for_sub=True)
    _add_serve_flags(open_p, for_sub=True, reload=False)
    open_p.add_argument("paths", nargs="+", help="one or more .md paths")
    open_p.add_argument(
        "--new-tab",
        action="store_true",
        help="always open a new tab, even when a viewer tab is already connected",
    )
    open_p.add_argument(
        "--no-raise",
        action="store_true",
        help="don't bring the browser to the front when steering an open tab (macOS)",
    )
    open_p.set_defaults(func=cmd_open)

    list_p = sub.add_parser("list", help="print deduped feed paths, newest first")
    _add_feed_flag(list_p, for_sub=True)
    list_p.set_defaults(func=cmd_list)

    compact_p = sub.add_parser(
        "compact", help="rewrite the feed to the deduped, capped set the viewer can show"
    )
    _add_feed_flag(compact_p, for_sub=True)
    compact_p.add_argument(
        "--keep",
        type=_positive_int,
        default=MAX_ENTRIES,
        help=f"entries to keep, newest first (default {MAX_ENTRIES}, the viewer's cap)",
    )
    compact_p.add_argument(
        "--prune-missing",
        action="store_true",
        help="also drop entries whose file no longer exists on disk",
    )
    compact_p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what compaction would do without writing anything",
    )
    compact_p.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the .bak of the original written alongside the feed before the rewrite",
    )
    compact_p.set_defaults(func=cmd_compact)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if not getattr(args, "func", None):
        args.func = cmd_serve
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
