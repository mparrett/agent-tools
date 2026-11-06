#!/usr/bin/env python3
"""preview-diff review server — serve the review page and accept submissions.

Two jobs, one process:

    GET  /         the review page (--page within --dir, default index.html)
    POST /review   a review submission (JSON) -> a saved bundle under --out-root

A submission is saved as a directory holding review.json (the data), review.html
(the same page with the review baked in) and source.diff (what was reviewed).
The response carries those paths back, so the caller — the browser overlay or an
agent POSTing straight to /review — learns where the review landed.

Baking the data into a copy of the page is what makes a saved review shareable:
the artifact is self-contained, so it opens standalone anywhere with no server
and no network. The page decides its own mode from that payload — absent means
"live, editable", present means "read-only rendering of a submitted review".

Stdlib only, to hold preview-diff's no-dependencies line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Generous enough that no real review trips them, small enough that a runaway
# agent can't fill the disk in one POST.
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_COMMENTS = 2000
MAX_TEXT = 100_000

SIDES = ("new", "old")

# The per-file card ids the diff parser emits (id="file-1", "file-2", …).
FILE_ID_RE = re.compile(r"file-\d+")

# The placeholder the page ships with. Matched loosely on the inner text so the
# page is free to reformat around it.
DATA_RE = re.compile(
    r'(<script[^>]*id="review-data"[^>]*>)(.*?)(</script>)',
    re.DOTALL,
)


def xdg_data_home() -> Path:
    """$XDG_DATA_HOME, falling back to the spec's ~/.local/share."""
    env = os.environ.get("XDG_DATA_HOME", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".local" / "share"


def default_out_root() -> Path:
    return xdg_data_home() / "preview-diff" / "reviews"


def new_review_id(now: datetime, suffix: str | None = None) -> str:
    """A sortable, collision-resistant directory name.

    Timestamp first so `ls` and globs come out chronological; a short random
    tail because two submissions can land in the same second.
    """
    stamp = now.strftime("%Y%m%dT%H%M%S")
    tail = suffix if suffix is not None else uuid.uuid4().hex[:6]
    return f"{stamp}-{tail}"


def _text(value: object, field: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field}: must not be empty")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{field}: longer than {MAX_TEXT} characters")
    return value


def _line_no(value: object, field: str) -> int:
    # bool is an int subclass; reject it so True doesn't silently become line 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}: expected an integer line number")
    if value < 1:
        raise ValueError(f"{field}: line numbers start at 1")
    return value


def validate(payload: object) -> dict:
    """Normalize a submission, or raise ValueError explaining what's wrong.

    The comment shape mirrors GitHub's review API (path/line/start_line/side/
    body) so an agent that can write a GitHub review can drive this unchanged.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload: expected a JSON object")

    # Default only on absent/null: `or []` would quietly accept a falsy wrong
    # type ({}, "", 0) and skip the check below.
    raw_comments = payload.get("comments")
    if raw_comments is None:
        raw_comments = []
    if not isinstance(raw_comments, list):
        raise ValueError("comments: expected a list")
    if len(raw_comments) > MAX_COMMENTS:
        raise ValueError(f"comments: more than {MAX_COMMENTS} entries")

    comments = []
    for i, c in enumerate(raw_comments):
        if not isinstance(c, dict):
            raise ValueError(f"comments[{i}]: expected an object")
        side = c.get("side")
        if side is None or side == "":
            side = "new"
        if side not in SIDES:
            raise ValueError(f"comments[{i}].side: expected one of {', '.join(SIDES)}")
        line = _line_no(c.get("line"), f"comments[{i}].line")
        start = c.get("start_line")
        if start is not None:
            start = _line_no(start, f"comments[{i}].start_line")
            if start > line:
                raise ValueError(f"comments[{i}].start_line: must not exceed line")
        # One path can appear on several cards (a multi-commit diff renders the
        # file once per commit), so path alone does not identify a card. The
        # overlay records which one it clicked; agents may omit it and take the
        # first card for that path.
        file_id = c.get("file_id")
        if file_id is not None and not (isinstance(file_id, str) and FILE_ID_RE.fullmatch(file_id)):
            raise ValueError(f"comments[{i}].file_id: expected a card id like 'file-3'")

        comments.append(
            {
                "path": _text(c.get("path"), f"comments[{i}].path", required=True),
                "file_id": file_id,
                "side": side,
                "start_line": start,
                "line": line,
                "body": _text(c.get("body"), f"comments[{i}].body", required=True),
            }
        )

    body = _text(payload.get("body"), "body")
    if not body and not comments:
        raise ValueError("a review needs either a general comment or a line comment")

    return {
        "reviewer": _text(payload.get("reviewer"), "reviewer"),
        "body": body,
        "comments": comments,
    }


def bake(html: str, data: dict) -> str:
    """Inject the review payload into the page's review-data script block.

    Escaping the angle brackets and ampersand keeps the JSON valid while making
    it impossible for a comment body containing `</script>` to break out of the
    block — the usual JSON-in-HTML hazard.
    """
    blob = json.dumps(data, indent=2)
    for ch in ("&", "<", ">"):
        blob = blob.replace(ch, "\\u%04x" % ord(ch))
    if not DATA_RE.search(html):
        raise ValueError("page has no review-data block to bake into")
    # A lambda avoids re.sub's backslash interpretation in the replacement.
    return DATA_RE.sub(lambda m: m.group(1) + blob + m.group(3), html, count=1)


def render_markdown(data: dict) -> str:
    """The review as a postable markdown comment.

    The body is emitted verbatim and leads the file: it is the review, and
    whatever structure it carries — severity tags, code fences, headings — is
    the author's, not something this tool models. Everything else is a short
    provenance footer, so the file can go straight to `gh pr comment
    --body-file` without editing.

    This is also the file to point an agent at. review.html is the same review
    with the whole diff around it; this is just the findings.
    """
    out = [data.get("body", "").strip()]

    # Line comments are a different shape from the freeform body, so they go
    # after it rather than being interleaved — but they are not dropped.
    comments = data.get("comments") or []
    if comments:
        out.append("\n### Line comments\n")
        for c in comments:
            start, line = c.get("start_line"), c["line"]
            span = f"{start}-{line}" if start and start < line else str(line)
            side = " (old)" if c.get("side") == "old" else ""
            body = c["body"].strip()
            # Indent continuations so a multi-line comment stays in its bullet.
            body = "\n  ".join(body.splitlines())
            out.append(f"- `{c['path']}:{span}`{side} — {body}")

    title = data.get("title", "").strip()
    who = data.get("reviewer", "").strip()
    when = (data.get("created") or "")[:10]
    bits = [f'Review of "{title}"' if title else "Review"]
    bits += [x for x in (who, when) if x]
    bits.append("via `preview-diff`")
    out.append("\n---\n*" + " · ".join(bits) + "*")

    return "\n".join(p for p in out if p is not None).strip() + "\n"


def save_review(
    submission: dict,
    *,
    out_root: Path,
    page_html: str,
    meta: dict,
    diff_text: str = "",
    now: datetime | None = None,
    review_id: str | None = None,
) -> dict:
    """Write the bundle and return the paths, all absolute.

    out_root is resolved first: a relative one (from --review-out) would write
    fine and then blow up in as_uri() below, reporting failure for a review that
    is already on disk.
    """
    now = now or datetime.now(timezone.utc)
    review_id = review_id or new_review_id(now)
    out_root = Path(out_root).resolve()

    data = {
        "id": review_id,
        "created": now.replace(microsecond=0).isoformat(),
        **meta,
        **submission,
    }

    out_dir = out_root / review_id
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "review.json"
    html_path = out_dir / "review.html"
    md_path = out_dir / "review.md"
    json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(bake(page_html, data), encoding="utf-8")
    md_path.write_text(render_markdown(data), encoding="utf-8")
    if diff_text:
        (out_dir / "source.diff").write_text(diff_text, encoding="utf-8")

    return {
        "id": review_id,
        "dir": str(out_dir),
        "review_json": str(json_path),
        "review_html": str(html_path),
        "review_md": str(md_path),
        "url": html_path.as_uri(),
        "comments": len(data["comments"]),
    }


class ReviewHandler(BaseHTTPRequestHandler):
    # Set by main() — the handler is instantiated per request, so config rides
    # on the class.
    serve_dir: Path
    out_root: Path
    meta: dict
    diff_path: Path | None = None
    page_name: str = "index.html"

    server_version = "preview-diff-review"

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        # Quiet by default; the interesting events are logged explicitly below.
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _page_path(self) -> Path:
        return self.serve_dir / self.page_name

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        # The page answers at / whatever it is called on disk, so -o <file> does
        # not produce a URL this server refuses to serve.
        if path in ("/", "/" + self.page_name):
            page = self._page_path()
            if not page.is_file():
                self._send(404, b"no review page", "text/plain; charset=utf-8")
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if self.path.split("?", 1)[0] != "/review":
            self._send_json(404, {"error": "not found"})
            return

        # Requiring a JSON content type is a cheap CSRF guard: a form POST from
        # a hostile page can't set it, and anything that can is subject to a
        # CORS preflight we never answer.
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        if ctype != "application/json":
            self._send_json(415, {"error": "expected Content-Type: application/json"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "bad Content-Length"})
            return
        if length <= 0:
            self._send_json(400, {"error": "empty request body"})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return

        try:
            submission = validate(payload)
        except ValueError as exc:
            self._send_json(422, {"error": str(exc)})
            return

        page = self._page_path()
        if not page.is_file():
            self._send_json(500, {"error": "no review page to save"})
            return

        diff_text = ""
        if self.diff_path and self.diff_path.is_file():
            diff_text = self.diff_path.read_text(encoding="utf-8", errors="replace")

        try:
            result = save_review(
                submission,
                out_root=self.out_root,
                page_html=page.read_text(encoding="utf-8"),
                meta=self.meta,
                diff_text=diff_text,
            )
        except (OSError, ValueError) as exc:
            self._send_json(500, {"error": f"could not save review: {exc}"})
            return

        print(
            f"preview-diff: review saved — {result['comments']} line comment(s)\n"
            f"  {result['review_html']}",
            file=sys.stderr,
            flush=True,
        )
        self._send_json(201, result)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, type=Path, help="directory holding the page")
    ap.add_argument("--page", default="index.html", help="page filename within --dir")
    ap.add_argument("--port", required=True, type=int)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--out-root", type=Path, default=None, help="where reviews are saved")
    ap.add_argument("--diff", type=Path, default=None, help="the diff under review")
    ap.add_argument("--title", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--head", default="")
    args = ap.parse_args(argv)

    ReviewHandler.serve_dir = args.dir
    ReviewHandler.page_name = args.page
    ReviewHandler.out_root = (args.out_root or default_out_root()).resolve()
    ReviewHandler.diff_path = args.diff
    ReviewHandler.meta = {"title": args.title, "base": args.base, "head": args.head}

    httpd = HTTPServer((args.bind, args.port), ReviewHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        # Ctrl-C is how you stop this server, so it exits 0 rather than dumping
        # a traceback at someone who did exactly what the help text said.
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
