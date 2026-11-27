"""--tailscale: the tailnet name passes the Host/Origin gates, and `tailscale serve`
is set up for the run and taken down after, without touching anyone else's mapping."""

import http.client
import json
import os
import subprocess
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


def test_expose_refuses_a_raw_tcp_forward_on_the_port(fake_tailscale):
    log, serve = fake_tailscale
    serve.write_text(json.dumps({"TCP": {"4321": {"TCPForward": "127.0.0.1:22"}}}))
    with pytest.raises(tailnet.TailnetError, match="a TCP forward"):
        with tailnet.expose(4321):
            pass
    assert "--bg" not in log.read_text()


def test_a_hung_or_garbled_tailscale_is_a_tailnet_error(fake_tailscale, tmp_path, monkeypatch):
    (tmp_path / "status.json").write_text("warning: something\n{}")
    with pytest.raises(tailnet.TailnetError, match="not JSON"):
        tailnet.dns_name()

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("tailscale", 10)

    monkeypatch.setattr(tailnet.subprocess, "run", hang)
    with pytest.raises(tailnet.TailnetError, match="enable it"):
        tailnet.dns_name()


def test_a_failed_tailnet_setup_closes_the_socket_and_restores_sigterm(tmp_path, monkeypatch):
    import signal
    import socket

    def boom(port):
        raise tailnet.TailnetError("tailscale is Stopped")

    monkeypatch.setattr(tailnet, "expose", boom)
    before = signal.getsignal(signal.SIGTERM)
    seen = []
    real = live.ThreadingHTTPServer

    def spy(*a, **k):
        srv = real(*a, **k)
        seen.append(srv)
        return srv

    monkeypatch.setattr(live, "ThreadingHTTPServer", spy)
    f = tmp_path / "s.jsonl"
    f.write_text("")
    with pytest.raises(tailnet.TailnetError):
        live.serve(f, lambda: None, tailscale=True)
    assert signal.getsignal(signal.SIGTERM) is before
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert seen[0].socket.fileno() == -1  # closed
    socket.create_server(("127.0.0.1", seen[0].server_address[1])).close()  # port is free


def test_a_second_signal_does_not_cut_the_tailnet_cleanup_short(fake_tailscale, tmp_path):
    """uv forwards the signal it gets, so `pkill -f` or Ctrl+C delivers two, the second a
    moment later. It used to land inside the cleanup and leave the tailnet mapping up."""
    import signal
    import sys
    import time
    from pathlib import Path

    log, _ = fake_tailscale
    # Make the unmapping slow enough for the second signal to arrive during it.
    exe = Path(os.environ["PATH"].split(os.pathsep)[0]) / "tailscale"
    exe.write_text(
        exe.read_text().replace(
            "exit 0\n", f'case "$*" in *off) sleep 0.6; echo unmapped >> "{log}";; esac\nexit 0\n'
        )
    )
    f = tmp_path / "s.jsonl"
    f.write_text("")
    ready = tmp_path / "ready"
    code = (
        "import pathlib\n"
        "from claude_reel import live\n"
        f"live.serve(pathlib.Path({str(f)!r}), lambda: None, tailscale=True,"
        f" on_ready=lambda *a: pathlib.Path({str(ready)!r}).write_text('ok'))\n"
    )
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=root, stderr=subprocess.PIPE)

    def wait_for(cond):
        for _ in range(200):
            if cond():
                return True
            time.sleep(0.02)
        return False

    try:
        assert wait_for(ready.exists)
        proc.send_signal(signal.SIGTERM)
        assert wait_for(lambda: " off" in log.read_text())  # the cleanup has started
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert "unmapped" in log.read_text(), err.decode()
    assert b"Traceback" not in err and proc.returncode == 0, err.decode()
