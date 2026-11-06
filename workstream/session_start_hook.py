#!/usr/bin/env python3
"""SessionStart hook: tell a session which workstreams it probably belongs to.

A session that starts from a vague prompt rebuilds its state by searching
transcripts, and a session nobody attached to a workstream is invisible to the
next one. This prints the ranked candidates `workstream match` computes for the
starting session, with the exact attach lines, so the model can attach, create,
or record why it is unassociated (spec D8). Startup, resume, clear, compact and
fork each hand the model a context that has never seen the block, so match all
of them.

Never fails the session: errors go to stderr, which is not added to context,
and the exit code is 0. Stdlib only. `workstream` is found on PATH, then as the
`bin/workstream` wrapper beside this package.

Test seams: WORKSTREAM_HOOK_JSON names a file whose JSON stands in for the
`workstream hook --json` result; WORKSTREAM_HOOK_BIN overrides the executable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Run as a script, this directory is already on sys.path; loaded by path
# (the tests), it is not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hook_common  # noqa: E402

# The whole hook has a 5 s budget in settings.json; leave headroom for python startup.
TIMEOUT_S = 4
ARGV = ["hook", "--json"]
workstream_bin = _hook_common.workstream_bin


def fetch(payload: dict) -> dict:
    return _hook_common.fetch(payload, ARGV, TIMEOUT_S)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        block = fetch(payload).get("block") or ""
        if block:
            sys.stdout.write(block if block.endswith("\n") else block + "\n")
    except Exception as e:  # a missing candidate list is a nuisance; a broken session start is not
        sys.stderr.write(f"workstream hook: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
