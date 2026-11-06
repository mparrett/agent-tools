#!/usr/bin/env python3
"""SessionEnd hook: record the frontier the ending session forgot to.

The most common loose end on a workstream is a session with no recorded
frontier: `retire` and `frontier --record` run only when a session thinks of
them. A SessionEnd hook cannot add anything to the model's context, since the
session is over, but it can act. This runs `workstream hook --event SessionEnd`,
which writes one capture per workstream the session is attached to (marked
`recorded_by: session_end_hook`) unless one already covers its last activity.
It never releases a claim: a /clear or a restart usually continues the same
work, and `retire` remains the deliberate hand-off.

Never fails the session: errors go to one line on stderr, nothing goes to
stdout, and the exit code is 0. Stdlib only. `workstream` is found on PATH,
then as the `bin/workstream` wrapper beside this package.

Test seams: WORKSTREAM_HOOK_JSON names a file whose JSON stands in for the
`workstream hook --event SessionEnd --json` result; WORKSTREAM_HOOK_BIN
overrides the executable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Run as a script, this directory is already on sys.path; loaded by path
# (the tests), it is not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hook_common  # noqa: E402

# settings.json gives the hook 25 s; leave headroom for python startup.
TIMEOUT_S = 20
ARGV = ["hook", "--event", "SessionEnd", "--json"]
workstream_bin = _hook_common.workstream_bin


def fetch(payload: dict) -> dict:
    return _hook_common.fetch(payload, ARGV, TIMEOUT_S)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        result = fetch(payload)
        if result.get("error"):
            sys.stderr.write(f"workstream session-end hook: {result['error']}\n")
    except Exception as e:  # a missed capture is a loose end; a hung exit is worse
        sys.stderr.write(f"workstream session-end hook: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
