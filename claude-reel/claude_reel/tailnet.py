"""Expose a live reel on the tailnet with `tailscale serve`, for the length of a run.

`tailscale serve` terminates HTTPS on this machine's tailnet name and proxies to the
loopback port, so the server itself never binds anything but 127.0.0.1. Tailnet only:
this never calls `tailscale funnel`, which would put the page on the internet.
"""

import json
import shutil
import subprocess
from contextlib import contextmanager


class TailnetError(RuntimeError):
    pass


def _ts(*args, timeout=10) -> str:
    exe = shutil.which("tailscale")
    if not exe:
        raise TailnetError("tailscale isn't on PATH")
    r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise TailnetError(f"tailscale {args[0]}: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def dns_name() -> str:
    """This machine's MagicDNS name, e.g. `mac.tail1234.ts.net`."""
    status = json.loads(_ts("status", "--json"))
    if status.get("BackendState") != "Running":
        raise TailnetError(f"tailscale is {status.get('BackendState') or 'not running'}")
    name = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not name:
        raise TailnetError("this machine has no MagicDNS name")
    return name


def _mapped(port: int):
    """What `tailscale serve` already sends HTTPS on `port` to, or None."""
    web = json.loads(_ts("serve", "status", "--json") or "{}").get("Web") or {}
    for hostport, conf in web.items():
        if hostport.rsplit(":", 1)[-1] == str(port):
            handler = ((conf or {}).get("Handlers") or {}).get("/") or {}
            return handler.get("Proxy") or "something"
    return None


@contextmanager
def expose(port: int):
    """Proxy https://<name>:<port> to 127.0.0.1:<port> while the block runs; yields the
    tailnet hostname. Refuses a port that `tailscale serve` already uses for something
    else, since removing the mapping afterwards would break that."""
    name = dns_name()
    target = f"http://127.0.0.1:{port}"
    existing = _mapped(port)
    if existing and existing != target:
        raise TailnetError(f"tailscale serve already uses port {port} (for {existing})")
    _ts("serve", "--bg", f"--https={port}", target)
    try:
        yield name
    finally:
        try:
            _ts("serve", f"--https={port}", "off")
        except (TailnetError, subprocess.SubprocessError, OSError):
            pass  # the server is going away regardless; a stale mapping only 502s
