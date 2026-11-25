"""Reply delivery: type a message into a background session as its human would.

There is no command or API that hands a running session a message as the human's
(as of Claude Code 2.1.331). The peer socket that `SendMessage` uses delivers it as
a `<cross-session-message>`: another agent talking, with none of the human's
authority. So this drives `claude attach <id>` under a pty: wait for the prompt,
bracketed-paste the text, press Enter, detach with Ctrl+Z. Claude Code records it
as typed, with `origin: human`, and queues it if a turn is running. A spike
on 2026-11-24 checked idle, busy, multi-line, and a second attach already open.

`attach` is the TUI, not an API, so a Claude Code update can change any of this;
the reel's tests pin the bytes sent, and a smoke run against a throwaway
`claude --bg` session is the check that it still lands.
"""

import json
import os
import re
import select
import subprocess
import threading
import time

PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
CTRL_Z = b"\x1a"
# The input prompt's marker; seeing it means the TUI is drawn and takes input.
READY_MARK = "❯".encode()
READY_TIMEOUT = 15.0
# Only background sessions are attachable; `claude agents --json` lists them.
ATTACHABLE = "background"

_one_at_a_time = threading.Lock()


class SendError(Exception):
    pass


def find_agent(session_id: str, agents_json=None):
    """The `claude agents --json` record for this session, or None.

    `agents_json` is the command's output, for tests; by default it is run.
    """
    if agents_json is None:
        try:
            agents_json = subprocess.run(
                ["claude", "agents", "--json"], capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
    try:
        agents = json.loads(agents_json or "[]")
    except json.JSONDecodeError:
        return None
    return next((a for a in agents if a.get("sessionId") == session_id), None)


def clean(text: str) -> str:
    """Text safe to paste. Control characters other than newline and tab go: an
    ESC could end the bracketed paste early and turn the rest into keystrokes."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()


def _read_until(fd, mark: bytes, timeout: float) -> bytes:
    out, end = b"", time.monotonic() + timeout
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if mark and mark in out:
                return out
    return out


def deliver(agent_id: str, text: str, command=("claude", "attach")) -> None:
    """Paste `text` into background session `agent_id` and press Enter.

    Raises SendError when the TUI never shows its prompt or the text is empty.
    `command` is the attach command, so tests can stand in a fake.
    """
    import fcntl
    import pty
    import struct
    import termios

    body = clean(text)
    if not body:
        raise SendError("nothing to send")
    with _one_at_a_time:
        # Popen rather than pty.fork(): this runs on a server thread, and forking a
        # threaded process from Python can deadlock the child before it execs.
        fd, tty_fd = pty.openpty()
        fcntl.ioctl(tty_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        try:
            proc = subprocess.Popen(
                [*command, agent_id],
                stdin=tty_fd,
                stdout=tty_fd,
                stderr=tty_fd,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as e:
            os.close(fd)
            raise SendError(f"can't run `{command[0]}`: {e}") from e
        finally:
            os.close(tty_fd)
        try:
            if READY_MARK not in _read_until(fd, READY_MARK, READY_TIMEOUT):
                raise SendError(f"`{' '.join(command)} {agent_id}` never showed its prompt")
            _read_until(fd, b"", 0.3)  # let the first frame finish drawing
            os.write(fd, PASTE_START + body.encode() + PASTE_END)
            _read_until(fd, b"", 0.4)
            os.write(fd, b"\r")
            _read_until(fd, b"", 0.8)
            os.write(fd, CTRL_Z)  # detach; the session keeps running
            _read_until(fd, b"", 1.5)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            os.close(fd)
