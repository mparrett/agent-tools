"""Live mode: serve one session's deck and push its tail as the transcript grows.

A stdlib HTTP server on 127.0.0.1. `GET /` is the deck; `GET /events` is a
server-sent-event stream that sends the slides which changed since the last
build. The transcript is polled rather than watched: stdlib has no file watcher,
and one stat a second is nothing.

Every request must carry the token the server printed. The page shows a session's
whole transcript, so without one any other page on this machine could read it.
The Host check stops DNS rebinding, where a hostile domain resolves to 127.0.0.1
and its pages count as same-origin with this server.
"""

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .render_html import render, render_parts

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

    def stamp(self):
        st = self.path.stat()
        return (st.st_size, st.st_mtime_ns)

    def current(self):
        """(version, deck, slides_html, session_html) for the file as it is now."""
        with self._lock:
            key = self.stamp()
            if key != self._key:
                deck = self.build_deck()
                slides, session = render_parts(deck)
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
    name, _, p = (host or "").rpartition(":")
    if p != str(port):
        return False
    return name in ("127.0.0.1", "localhost") or name.endswith(".localhost")


def make_handler(builder: Builder, token: str, port_ref: list):
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
                return self._events(parse_qs(url.query).get("v", [""])[0])
            return self._deny(404, "not found\n")

        def _page(self):
            version, deck, _, _ = builder.current()
            body = render(deck, live_events=f"events?t={token}&v={version}").encode()
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
            # Diff against the build the page came from, so a change landing between
            # the page load and this connection still arrives. Aged out, or unknown:
            # resend everything.
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
                        self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
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


def serve(path: Path, build_deck, port: int = 0, on_ready=None):
    """Serve until interrupted. `on_ready(url)` runs once the port is bound."""
    token = secrets.token_urlsafe(16)
    builder = Builder(path, build_deck)
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(builder, token, port_ref))
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
