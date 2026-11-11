"""Lane assignment and header-fit limits, shared by fetch.py and build.py.

`fetch.py --preview` has to answer "which lane would this land in" before any
data file exists, and `build.py` has to report which nodes fell through to
`default_lane`. One copy, so a preview cannot disagree with the map it previews.
"""

import re

# Mirrored from template.html's lane-header wrapping. It splits a lane name on
# spaces and fits `Math.max(10, Math.floor((laneW - 24) / 7.2))` characters per
# line, with laneW never dropping below minLaneW. A single word longer than that
# overflows its column and no wrapping can save it, which is the one lane-header
# failure that is a config error rather than a template bug.
# test_build.py re-reads these out of template.html and fails if they drift.
MIN_LANE_W = 120
HEADER_PAD = 24
CHAR_PX = 7.2
MIN_CHARS = 10


def header_max_chars(lane_w=MIN_LANE_W):
    return max(MIN_CHARS, int((lane_w - HEADER_PAD) / CHAR_PX))


def overflowing_lane_names(lanes, lane_w=MIN_LANE_W):
    """[(lane key, word)] for words too long to fit a column at `lane_w`."""
    cap = header_max_chars(lane_w)
    return [(ln["key"], w) for ln in lanes for w in ln["name"].split() if len(w) > cap]


def make_lane_assigner(cfg):
    """-> assign(repo, number, title, labels) -> (lane key, "override"|"match"|"default").

    The reason is what makes the build's fall-through report possible: a node in
    a lane because a regex matched is settled, one there because nothing matched
    is a lane rule or an `override` waiting to be written.
    """
    lanes = cfg["lanes"]
    lane_keys = [ln["key"] for ln in lanes]
    override = cfg.get("override", {})
    default_lane = cfg.get("default_lane", {})
    fallback = lane_keys[0] if lane_keys else ""

    def assign(repo, number, title, labels=""):
        key = f"{repo}#{number}"
        if key in override:
            return override[key], "override"
        text = title + " " + labels
        for ln in lanes:
            if ln.get("repos") and repo not in ln["repos"]:
                continue
            if ln.get("match") and re.search(ln["match"], text, re.I):
                return ln["key"], "match"
        return default_lane.get(repo, fallback), "default"

    return assign
