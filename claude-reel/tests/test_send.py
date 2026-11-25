"""Reply delivery: the bytes typed into `claude attach`, and the /send route's gates."""

import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest
from claude_reel import live, send
from claude_reel.curate import curate
from claude_reel.extract import extract
from claude_reel.render_html import render

# Stands in for `claude attach <id>`: draws the prompt, then records every byte the
# terminal hands it until Ctrl+Z (the detach), raw, as the TUI would see them.
FAKE_ATTACH = r"""
import os, sys, tty
out = sys.argv[1]
if sys.argv[2] != "silent":
    os.write(1, "draw ❯ ".encode())
tty.setraw(0)
got = b""
while not got.endswith(b"\x1a"):
    c = os.read(0, 1024)
    if not c:
        break
    got += c
open(out, "wb").write(got)
"""


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "fake_attach.py"
    script.write_text(FAKE_ATTACH)
    rec = tmp_path / "typed.bin"
    return lambda mode="ok": ((sys.executable, str(script), str(rec), mode), rec)


def test_deliver_pastes_presses_enter_and_detaches(fake):
    cmd, rec = fake()
    send.deliver("ab12cd34", "line one\r\nline two  ", command=cmd)
    assert rec.read_bytes() == (
        send.PASTE_START + b"line one\nline two" + send.PASTE_END + b"\r" + send.CTRL_Z
    )


def test_deliver_gives_up_without_a_prompt(fake, monkeypatch):
    monkeypatch.setattr(send, "READY_TIMEOUT", 0.5)
    cmd, rec = fake("silent")
    with pytest.raises(send.SendError, match="never showed its prompt"):
        send.deliver("ab12cd34", "hello", command=cmd)
    assert not rec.exists() or send.PASTE_START not in rec.read_bytes()


def test_clean_cannot_break_out_of_the_paste():
    assert send.clean("hi\x1b[201~\x1b[A\x03there\ttab") == "hi[201~[Athere\ttab"
    with pytest.raises(send.SendError, match="nothing"):
        send.deliver("x", " \x1b ", command=("true",))


def test_find_agent_matches_the_session_id():
    out = json.dumps([{"sessionId": "aaa", "id": "a", "kind": "background"}, {"id": "b"}])
    assert send.find_agent("aaa", out)["id"] == "a"
    assert send.find_agent("zzz", out) is None
    assert send.find_agent("aaa", "not json") is None


def _serve(tmp_path, sender, note=None):
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}))
    builder = live.Builder(f, lambda: curate(extract(f)))
    port = [0]
    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), live.make_handler(builder, "tok", port, sender, note)
    )
    srv.daemon_threads = True
    port[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port[0]


def _post(port, body, origin="same", ctype="application/json", path="/send?t=tok"):
    host = f"127.0.0.1:{port}"
    headers = {"Host": host, "Content-Type": ctype}
    if origin:
        headers["Origin"] = f"http://{host}" if origin == "same" else origin
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=body, headers=headers)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_send_route_gates_then_delivers(tmp_path):
    got = []

    def sender(text):
        if text == "fail":
            raise send.SendError("no prompt")
        got.append(text)

    srv, port = _serve(tmp_path, sender)
    try:
        ok = json.dumps({"text": "keep going"})
        assert _post(port, ok, path="/send?t=nope")[0] == 403
        assert _post(port, ok, origin=None)[0] == 403
        assert _post(port, ok, origin="http://evil.example")[0] == 403
        assert _post(port, "text=x", ctype="application/x-www-form-urlencoded")[0] == 415
        assert _post(port, json.dumps({"text": "  "}))[0] == 400
        assert _post(port, ok) == (200, {"ok": True})
        assert _post(port, json.dumps({"text": "fail"})) == (502, {"error": "no prompt"})
        assert got == ["keep going"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_without_a_sender_the_page_says_why(tmp_path):
    srv, port = _serve(tmp_path, None, "Replies need a background session")
    try:
        assert _post(port, json.dumps({"text": "x"}))[0] == 409
    finally:
        srv.shutdown()
        srv.server_close()
    deck = curate(extract(tmp_path / "s.jsonl"))
    on = render(deck, live_events="events?t=t", live_send="send?t=t")
    off = render(deck, live_events="events?t=t", reply_note="Replies need a background session")
    assert 'id="reply" data-send="send?t=t"' in on and "REPLY" not in off
    assert 'class="reply off">Replies need a background session' in off
    assert 'id="reply"' not in render(deck)  # a plain deck has neither
