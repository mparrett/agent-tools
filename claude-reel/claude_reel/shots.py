"""Images from a transcript's tool results, for live mode's thumbnails.

The deck only carries a label per image; the bytes stay in the transcript, which can
run to hundreds of MB, mostly screenshots. Finding one is a byte search for its tool
call's id and a parse of the lines that contain it, then a cached offset: the file
is append-only, so an offset stays good for the life of the server.
"""

import base64
import binascii
import json
import mmap
import re
import threading
from pathlib import Path

# Only raster types a browser draws as an image: an SVG can carry script, and the
# bytes come from a transcript, not from anyone vouching for them.
KINDS = {b"\x89PNG": "image/png", b"\xff\xd8": "image/jpeg", b"GIF8": "image/gif"}
TOOL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CACHE_MAX = 4096


def _kind(data: bytes) -> str | None:
    for magic, ctype in KINDS.items():
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _result_images(obj: dict, tool_id: str) -> list | None:
    """The image blocks of `tool_id`'s result in this record, if it holds it. Only the
    conversation's own user record counts; request logs copy results too."""
    if obj.get("type") != "user":
        return None
    content = (obj.get("message") or {}).get("content")
    for b in content if isinstance(content, list) else []:
        if (
            isinstance(b, dict)
            and b.get("type") == "tool_result"
            and b.get("tool_use_id") == tool_id
        ):
            inner = b.get("content")
            return (
                [x for x in inner if isinstance(x, dict) and x.get("type") == "image"]
                if isinstance(inner, list)
                else []
            )
    return None


class Shots:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._where: dict = {}  # tool id -> (offset, length) of its result's line

    def _scan(self, tool_id: str):
        needle = f'"{tool_id}"'.encode()
        if self.path.stat().st_size == 0:  # mmap refuses an empty file
            return None
        with open(self.path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            i = m.find(needle)
            while i >= 0:
                start = m.rfind(b"\n", 0, i) + 1
                end = m.find(b"\n", i)
                end = len(m) if end < 0 else end
                try:
                    obj = json.loads(m[start:end])
                except ValueError:
                    obj = None
                if isinstance(obj, dict) and _result_images(obj, tool_id) is not None:
                    return start, end - start
                i = m.find(needle, end)
        return None

    def get(self, tool_id: str, n: int):
        """(bytes, content type) for image `n` of `tool_id`'s result, or None, including
        when the transcript has moved or vanished under a running server."""
        if not TOOL_ID.fullmatch(tool_id) or n < 0:
            return None
        try:
            return self._get(tool_id, n)
        except (OSError, ValueError):
            return None

    def _get(self, tool_id: str, n: int):
        with self._lock:
            where = self._where.get(tool_id)
        if where is None:
            where = self._scan(tool_id)
            if where is None:
                return None
            with self._lock:
                if len(self._where) >= _CACHE_MAX:
                    self._where.pop(next(iter(self._where)))
                self._where[tool_id] = where
        with open(self.path, "rb") as f:
            f.seek(where[0])
            images = _result_images(json.loads(f.read(where[1])), tool_id) or []
        if n >= len(images):
            return None
        src = images[n].get("source") or {}
        if not isinstance(src, dict) or src.get("type") != "base64":
            return None
        try:
            data = base64.b64decode(src.get("data") or "", validate=False)
        except (ValueError, binascii.Error):
            return None
        ctype = _kind(data)
        return (data, ctype) if ctype else None
