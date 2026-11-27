"""--tailscale: the tailnet name passes the Host/Origin gates, and `tailscale serve`
is set up for the run and taken down after, without touching anyone else's mapping."""

import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer

import pytest
from claude_reel import live, tailnet
from claude_reel.curate import curate
from claude_reel.extract import extract

NAME = "box.tail0000.ts.net"


def test_allowed_host_takes_the_tailnet_name_only_when_given():
    assert live._allowed_host(f"{NAME}:4321", 4321, NAME)
    assert not live._allowed_host(f"{NAME}:4321", 4321)  # not without --tailscale
    assert not live._allowed_host(f"{NAME}:9999", 4321, NAME)  # wrong port
    assert not live._allowed_host("evil.example:4321", 4321, NAME)
    assert live._allowed_host("127.0.0.1:4321", 4321, NAME)


def test_send_wants_the_https_origin_on_the_tailnet(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}))
    got = []
    builder = live.Builder(f, lambda: curate(extract(f)))
    port = [0]
    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        live.make_handler(builder, "tok", port, got.append, tailnet=NAME),
    )
    srv.daemon_threads = True
    port[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"{NAME}:{port[0]}"

    def post(origin):
        c = http.client.HTTPConnection("127.0.0.1", port[0], timeout=5)
        c.request(
            "POST",
            "/send?t=tok",
            body=json.dumps({"text": "hello"}),
            headers={"Host": host, "Origin": origin, "Content-Type": "application/json"},
        )
        return c.getresponse().status

    try:
        assert post(f"http://{host}") == 403  # the tailnet page is only ever https
        assert post(f"https://{host}") == 200
        assert got == ["hello"]
    finally:
        srv.shutdown()


@pytest.fixture
def fake_tailscale(tmp_path, monkeypatch):
    """A `tailscale` that logs its argv and answers status / serve status from files."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls"
    status = tmp_path / "status.json"
    serve = tmp_path / "serve.json"
    status.write_text(json.dumps({"BackendState": "Running", "Self": {"DNSName": NAME + "."}}))
    serve.write_text("{}")
    exe = bin_ / "tailscale"
    exe.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        f'[ "$1" = status ] && exec cat "{status}"\n'
        f'[ "$1 $2" = "serve status" ] && exec cat "{serve}"\n'
        "exit 0\n"
    )
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    return log, serve


def test_expose_maps_the_port_then_removes_it(fake_tailscale):
    log, _ = fake_tailscale
    with tailnet.expose(4321) as name:
        assert name == NAME
        assert "serve --bg --https=4321 http://127.0.0.1:4321" in log.read_text()
        assert "off" not in log.read_text()
    assert log.read_text().splitlines()[-1] == "serve --https=4321 off"


def test_expose_refuses_a_port_someone_else_serves(fake_tailscale):
    log, serve = fake_tailscale
    other = {"Web": {f"{NAME}:4321": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
    serve.write_text(json.dumps(other))
    with pytest.raises(tailnet.TailnetError, match="already uses port 4321"):
        with tailnet.expose(4321):
            pass
    assert "--bg" not in log.read_text() and "off" not in log.read_text()


def test_expose_says_why_when_tailscale_is_stopped(fake_tailscale, tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"BackendState": "Stopped"}))
    with pytest.raises(tailnet.TailnetError, match="tailscale is Stopped"):
        with tailnet.expose(4321):
            pass
