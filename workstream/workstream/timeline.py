"""`workstream timeline`: workstreams as a self-contained HTML timeline.

The page embeds each workstream's `show --json` payload and draws it in the
browser, so it needs no server and shows the store as it was when written.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from importlib import resources
from pathlib import Path

MARKER = "/*__DATA__*/"


def default_path() -> Path:
    """Beside the gh cache, as `weft view` writes under `$XDG_CACHE_HOME/weft`."""
    override = os.environ.get("WORKSTREAM_CACHE_DIR")
    if override:
        return Path(override) / "timeline.html"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "workstream" / "timeline.html"


def render(shows: list[dict], generated_at: datetime) -> str:
    """`shows` are `show --json` payloads, in the order the picker lists them."""
    template = resources.files("workstream").joinpath("timeline.html").read_text()
    bundle = {"generated_at": generated_at.isoformat(timespec="seconds"), "workstreams": shows}
    # Inside <script>, `</script` ends the element and `<!--` then `<script`
    # stops the real end tag from closing it. With no `<` left, neither can
    # happen; `<` is the same string to JSON.parse.
    payload = json.dumps(bundle, separators=(",", ":"), default=str).replace("<", "\\u003c")
    return template.replace(MARKER, payload, 1)


def extract(html: str) -> dict:
    """The bundle back out of a written page."""
    start = html.index('<script id="data" type="application/json">') + len(
        '<script id="data" type="application/json">'
    )
    return json.loads(html[start : html.index("</script>", start)])
