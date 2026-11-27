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
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # First use of serve on a tailnet prints a link to enable it, then waits.
        raise TailnetError(
            f"tailscale {args[0]} didn't answer in {timeout}s"
            " (if Serve isn't enabled on this tailnet, run `tailscale serve` once to enable it)"
        ) from None
    except OSError as e:
        raise TailnetError(f"couldn't run tailscale: {e}") from None
    if r.returncode != 0:
        raise TailnetError(f"tailscale {args[0]}: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def _json(*args) -> dict:
    out = _ts(*args)
    try:
        data = json.loads(out or "{}")
    except ValueError:
        raise TailnetError(f"tailscale {' '.join(args)}: not JSON: {out[:80]!r}") from None
    return data if isinstance(data, dict) else {}


def dns_name() -> str:
    """This machine's MagicDNS name, e.g. `mac.tail1234.ts.net`."""
    status = _json("status", "--json")
    if status.get("BackendState") != "Running":
        raise TailnetError(f"tailscale is {status.get('BackendState') or 'not running'}")
    name = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not name:
        raise TailnetError("this machine has no MagicDNS name")
    return name


def _mapped(port: int):
    """What `tailscale serve` already sends HTTPS on `port` to, or None."""
    status = _json("serve", "status", "--json")
    tcp = (status.get("TCP") or {}).get(str(port))
    # An HTTPS mapping is listed under TCP too, with HTTPS set; one without it is a raw
    # TCP forward, which `--https=<port> off` would also take down.
    if tcp is not None and not (isinstance(tcp, dict) and tcp.get("HTTPS")):
        return "a TCP forward"
    web = status.get("Web") or {}
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
