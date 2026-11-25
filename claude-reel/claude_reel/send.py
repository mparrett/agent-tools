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
from pathlib import Path

PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
CTRL_Z = b"\x1a"
# The input prompt's marker; seeing it means the TUI is drawn and takes input.
READY_MARK = "❯".encode()
READY_TIMEOUT = 15.0
# How long a typed message may take to reach the transcript.
CONFIRM_TIMEOUT = 6.0
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


def _quiet(fd, gap: float, limit: float) -> None:
    """Read until the terminal has been silent for `gap` seconds (or `limit` passes):
    the TUI has finished drawing, history replay included."""
    end = time.monotonic() + limit
    while time.monotonic() < end and _read_until(fd, b"", gap):
        pass


def landed(transcript: Path, offset: int, body: str, timeout: float = CONFIRM_TIMEOUT) -> bool:
    """Whether a record carrying `body` appeared in `transcript` past `offset`.

    The transcript is the only record of what the session accepted: a typed user
    message when idle, a queued command when busy. Reading it, rather than the
    TUI's screen, is what lets the page say "sent" and mean it.
    """

    # Containment, not equality: Claude Code records a long paste wrapped in
    # <pasted_content> tags, inside whatever else the message holds.
    def carries(v):
        if isinstance(v, str):
            return body in v
        if isinstance(v, dict):
            return any(carries(x) for x in v.values())
        if isinstance(v, list):
            return any(carries(x) for x in v)
        return False

    end = time.monotonic() + timeout
    while True:
        try:
            with transcript.open("rb") as fh:
                fh.seek(offset)
                for line in fh:
                    try:
                        if carries(json.loads(line)):
                            return True
                    except ValueError:
                        continue
        except OSError:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def deliver(
    agent_id: str, text: str, transcript: Path | None = None, command=("claude", "attach")
) -> bool:
    """Paste `text` into background session `agent_id` and press Enter. Returns
    whether the session's `transcript` recorded it within CONFIRM_TIMEOUT (True
    when no transcript is given).

    False is not a failure. While the session is blocked on something other than a
    model turn (a `!` command in its terminal, seen 2026-11-24), Claude Code holds
    typed input in its queue and writes nothing until it runs, which took 106 s that
    time. Calling that an error invited sending the message twice.

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
            _quiet(fd, 0.3, 3.0)
            offset = transcript.stat().st_size if transcript else 0
            os.write(fd, PASTE_START + body.encode() + PASTE_END)
            _read_until(fd, b"", 0.4)
            os.write(fd, b"\r")
            _read_until(fd, b"", 0.8)
            os.write(fd, CTRL_Z)  # detach; the session keeps running
            _read_until(fd, b"", 0.5)
            confirmed = not transcript or landed(transcript, offset, body)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            os.close(fd)
        return confirmed
