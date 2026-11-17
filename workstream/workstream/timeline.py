"""`workstream timeline`: workstreams as a self-contained HTML timeline.

The page embeds each workstream's `show --json` payload and draws it in the
browser, so it needs no server and shows the store as it was when written.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Optional

MARKER = "/*__DATA__*/"


def default_path() -> Path:
    """Beside the gh cache, as `weft view` writes under `$XDG_CACHE_HOME/weft`."""
    override = os.environ.get("WORKSTREAM_CACHE_DIR")
    if override:
        return Path(override) / "timeline.html"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "workstream" / "timeline.html"


def backup(path: Path, now: datetime) -> Optional[Path]:
    """Copy `path` aside before it is rewritten, as `<name>.<YYYYmmdd-HHMM>.bak`.

    Overwriting is the point of the command, so this never refuses; it only
    makes the overwrite recoverable. A narrower `--since` or workstream list
    rewrites the page with a subset of what it held, so the old page is worth
    keeping as an audit trail, and nothing here prunes the backups.

    One backup per minute, stamped with the time of the copy: a later run in the
    same minute leaves it alone, so it holds what was there before that minute's
    first write rather than the previous few seconds' run.
    """
    if not path.is_file():
        return None
    bak = path.with_name(f"{path.name}.{now.strftime('%Y%m%d-%H%M')}.bak")
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


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
