"""Live mode: the server's gates and the slide diffs it streams, over a real socket."""

import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest
from claude_reel import live
from claude_reel.curate import curate
from claude_reel.extract import extract


def _line(kind, n):
    ts = f"2026-11-24T12:{n:02d}:00Z"
    if kind == "user":
        msg = {"role": "user", "content": f"step {n} please"}
    else:
        msg = {"role": "assistant", "model": "m", "content": [{"type": "text", "text": kind}]}
    return (
        json.dumps(
            {"type": kind if kind == "user" else "assistant", "timestamp": ts, "message": msg}
        )
        + "\n"
    )


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "POLL_SECONDS", 0.05)
    f = tmp_path / "live0000-0000.jsonl"
    f.write_text(_line("user", 1) + _line("first answer", 1))
    builder = live.Builder(f, lambda: curate(extract(f)))
    port = [0]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), live.make_handler(builder, "tok", port))
    srv.daemon_threads = True
    port[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f, port[0]
    srv.shutdown()
    srv.server_close()


def _get(port, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    r = c.getresponse()
    return r.status, r


def _events(resp):
    """Yield each SSE data payload as it arrives."""
    while True:
        line = resp.fp.readline().decode()
        if line.startswith("data: "):
            yield json.loads(line[6:])


def test_token_and_host_gate_every_route(server):
    _, port = server
    assert _get(port, "/")[0] == 403
    assert _get(port, "/?t=nope")[0] == 403
    assert _get(port, "/events?t=nope")[0] == 403
    assert _get(port, "/?t=tok", host=f"evil.example:{port}")[0] == 403
    assert _get(port, "/?t=tok", host=f"reel.localhost:{port}")[0] == 200
    assert _get(port, "/nope?t=tok")[0] == 404


def test_stream_sends_only_what_changed(server):
    f, port = server
    status, page = _get(port, "/?t=tok")
    assert status == 200
    events = re.search(r'data-events="([^"]+)"', page.read().decode()).group(1)
    _, resp = _get(port, "/" + events.replace("&amp;", "&"))
    ev = _events(resp)

    first = next(ev)  # the page's own build: nothing to send
    assert first["changed"] == [] and first["count"] == 2

    with f.open("a") as fh:  # the open turn grows: only its slide is resent
        fh.write(_line("second answer", 1))
    grew = next(ev)
    assert [c["i"] for c in grew["changed"]] == [1] and "second answer" in grew["changed"][0][
        "html"
    ]

    with f.open("a") as fh:  # a new turn: a new slide, and the agenda gains a row
        fh.write(_line("user", 2))
    added = next(ev)
    assert [c["i"] for c in added["changed"]] == [0, 2] and added["count"] == 3


def test_unknown_version_resends_everything(server):
    _, port = server
    _, resp = _get(port, "/events?t=tok&v=gone")
    assert [c["i"] for c in next(_events(resp))["changed"]] == [0, 1]


def test_diff():
    assert live.diff(["a", "b"], ["a", "B", "c"]) == [{"i": 1, "html": "B"}, {"i": 2, "html": "c"}]
    assert live.diff(["a"], ["a"]) == []
