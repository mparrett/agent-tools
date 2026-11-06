"""What both hook wrappers share: finding `workstream` and running one of its
`hook` subcommands. Stdlib only, like the wrappers that import it.

Test seams: WORKSTREAM_HOOK_JSON names a file whose JSON stands in for the
subcommand's result; WORKSTREAM_HOOK_BIN overrides the executable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

SIBLING_BIN = Path(__file__).resolve().parent.parent / "bin" / "workstream"


def workstream_bin() -> str | None:
    override = os.environ.get("WORKSTREAM_HOOK_BIN")
    if override:
        return override if os.access(override, os.X_OK) else None
    found = shutil.which("workstream")
    if found:
        return found
    return str(SIBLING_BIN) if os.access(SIBLING_BIN, os.X_OK) else None


def fetch(payload: dict, argv_tail: list[str], timeout_s: float) -> dict:
    """Run `workstream <argv_tail...>` with `payload` on stdin; its JSON stdout."""
    fixture = os.environ.get("WORKSTREAM_HOOK_JSON")
    if fixture:
        return json.loads(Path(fixture).read_text(encoding="utf-8"))
    exe = workstream_bin()
    if not exe:
        raise FileNotFoundError("workstream: not on PATH and no bin/workstream beside this hook")
    out = subprocess.run(
        [exe, *argv_tail],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if out.returncode != 0:
        raise RuntimeError(f"workstream hook exited {out.returncode}: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout or "{}")
    return data if isinstance(data, dict) else {}
