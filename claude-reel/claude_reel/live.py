"""Live mode: serve one session's deck and push its tail as the transcript grows.

A stdlib HTTP server on 127.0.0.1. `GET /` is the deck; `GET /events` is a
server-sent-event stream that sends the slides which changed since the last
build; `POST /send` types a reply into the session (see send.py), when it is a
background session. The transcript is polled rather than watched: stdlib has no file watcher,
and one stat a second is nothing.

Every request must carry the token the server printed. The page shows a session's
whole transcript, so without one any other page on this machine could read it.
The Host check stops DNS rebinding, where a hostile domain resolves to 127.0.0.1
and its pages count as same-origin with this server.
"""

import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .render_html import render, render_parts
from .send import SendError

POLL_SECONDS = 1.0
# An SSE comment line, so a client that went away is noticed on the next write.
HEARTBEAT_SECONDS = 15.0
HISTORY = 16


class Builder:
    """Builds the deck for the transcript's current state, once per change.

    `build_deck` is the CLI's own extract → filter → curate pipeline, so the live
    deck is the one a plain run would write. Builds are shared across connections
    and keyed on the file's (size, mtime).
    """

    def __init__(self, path: Path, build_deck):
        self.path = path
        self.build_deck = build_deck
        self._lock = threading.Lock()
        self._key = None
        self._built = None
        # Recent builds' slides by version, so a stream can diff against the build
        # its page was rendered from (or the last it saw, after a reconnect).
        self._history: dict = {}
        self.error = None

    def stamp(self):
        st = self.path.stat()
        return (st.st_size, st.st_mtime_ns)

    def current(self):
        """(version, deck, slides_html, session_html) for the file as it is now.

        A failed build keeps the last good one: live mode is the one place the
        pipeline sees half-written turns, and a transcript can move or vanish. The
        next poll retries. With no good build yet, the error propagates.
        """
        with self._lock:
            try:
                key = self.stamp()
                if key == self._key:
                    return self._built
                deck = self.build_deck()
                slides, session = render_parts(deck)
            except Exception as e:
                if self._built is None:
                    raise
                if repr(e) != self.error:
                    print(
                        f"claude-reel: rebuild failed, showing the last good one: {e!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                self.error = repr(e)
                return self._built
            self.error = None
            version = f"{key[0]}-{key[1]}"
            self._key, self._built = key, (version, deck, slides, session)
            self._history[version] = slides
            while len(self._history) > HISTORY:
                self._history.pop(next(iter(self._history)))
            return self._built

    def slides_at(self, version):
        """A recent build's slides, or None once it has aged out."""
        with self._lock:
            return self._history.get(version)


def diff(old: list, new: list) -> list:
    """The slides to send: every index whose HTML changed, plus any new ones."""
    return [{"i": i, "html": h} for i, h in enumerate(new) if i >= len(old) or old[i] != h]


def _allowed_host(host: str, port: int) -> bool:
    """Loopback or *.localhost, on this port or none (a local proxy drops it)."""
    name, sep, p = (host or "").rpartition(":")
    if not sep or not p.isdigit():
        name = host or ""
    elif p != str(port):
        return False
    return name in ("127.0.0.1", "localhost") or name.endswith(".localhost")


# A reply is a message, not a document; this is generous and stops a runaway body.
MAX_REPLY_BYTES = 64 * 1024


def make_handler(
    builder: Builder, token: str, port_ref: list, sender=None, reply_note=None, check=None
):
    """`sender(text)` delivers a reply and returns whether the session has recorded it
    yet (raising send.SendError when it can't deliver);
    without one there is no reply bar, and `reply_note` says why. `check` names where
    to look when a reply is never recorded (the session's attach command)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # the terminal is the user's, not an access log
            pass

        def _deny(self, code, why):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(why.encode())

        def do_GET(self):
            url = urlsplit(self.path)
            if not _allowed_host(self.headers.get("Host", ""), port_ref[0]):
                return self._deny(403, "bad host\n")
            if not secrets.compare_digest(parse_qs(url.query).get("t", [""])[0], token):
                return self._deny(403, "missing or wrong token\n")
            if url.path == "/":
                return self._page()
            if url.path == "/events":
                # EventSource reconnects to the same URL, so `v` is the page's first
                # build; Last-Event-ID is the last one this browser applied.
                since = self.headers.get("Last-Event-ID") or parse_qs(url.query).get("v", [""])[0]
                return self._events(since)
            return self._deny(404, "not found\n")

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            url = urlsplit(self.path)
            host = self.headers.get("Host", "")
            if not _allowed_host(host, port_ref[0]):
                return self._json(403, {"error": "bad host"})
            if not secrets.compare_digest(parse_qs(url.query).get("t", [""])[0], token):
                return self._json(403, {"error": "missing or wrong token"})
            if url.path != "/send":
                return self._json(404, {"error": "not found"})
            # This route types into an agent with a shell, so it is held tighter than
            # the reads: the request must come from this page's own origin, and a JSON
            # body means a cross-site form can't send it without a CORS preflight,
            # which nothing here answers.
            if self.headers.get("Origin") != f"http://{host}":
                return self._json(403, {"error": "bad origin"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "send JSON"})
            if sender is None:
                return self._json(409, {"error": reply_note or "replies are off"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:  # read(-1) would wait for the client to hang up
                return self._json(400, {"error": "bad Content-Length"})
            if n > MAX_REPLY_BYTES:
                return self._json(413, {"error": "reply too long"})
            try:
                text = json.loads(self.rfile.read(n) or b"{}").get("text", "")
            except (ValueError, AttributeError):  # bad JSON or bad UTF-8, or not an object
                return self._json(400, {"error": "bad JSON"})
            if not isinstance(text, str) or not text.strip():
                return self._json(400, {"error": "nothing to send"})
            try:
                recorded = sender(text)
            except SendError as e:
                return self._json(502, {"error": str(e)})
            # Only an explicit False is pending: a sender that returns nothing delivered.
            if recorded is False:
                return self._json(200, {"ok": True, "pending": True, "check": check})
            return self._json(200, {"ok": True, "pending": False})

        def _page(self):
            try:
                version, deck, _, _ = builder.current()
            except Exception as e:  # no good build yet: say so rather than hang up
                return self._deny(500, f"claude-reel: can't build the deck: {e!r}\n")
            body = render(
                deck,
                live_events=f"events?t={token}&v={version}",
                live_send=f"send?t={token}" if sender else None,
                reply_note=reply_note,
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _events(self, since):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # Diff against the build the browser holds, so a change landing between
            # the page load (or a dropped connection) and this one still arrives.
            # Aged out, or unknown: resend everything.
            sent = builder.slides_at(since) or []
            sent_version, last_write = None, time.monotonic()
            try:
                while True:
                    # Cheap when nothing changed: a stat, and the cached build.
                    version, _, slides, session = builder.current()
                    if version != sent_version:
                        msg = {
                            "changed": diff(sent, slides),
                            "count": len(slides),
                            "session": session,
                        }
                        self.wfile.write(f"id: {version}\ndata: {json.dumps(msg)}\n\n".encode())
                        self.wfile.flush()
                        sent, sent_version, last_write = slides, version, time.monotonic()
                    elif time.monotonic() - last_write > HEARTBEAT_SECONDS:
                        self.wfile.write(b": hb\n\n")
                        self.wfile.flush()
                        last_write = time.monotonic()
                    time.sleep(POLL_SECONDS)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(
    path: Path, build_deck, port: int = 0, on_ready=None, sender=None, reply_note=None, check=None
):
    """Serve until interrupted. `on_ready(url)` runs once the port is bound."""
    token = secrets.token_urlsafe(16)
    builder = Builder(path, build_deck)
    port_ref = [port]
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(builder, token, port_ref, sender, reply_note, check)
    )
    server.daemon_threads = True
    port_ref[0] = server.server_address[1]
    url = f"http://127.0.0.1:{port_ref[0]}/?t={token}"
    if on_ready:
        on_ready(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return server
