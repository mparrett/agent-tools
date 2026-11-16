"""Curate: a reel (beats) -> a deck (slides). Deterministic rules only.

Boundary: a new slide starts at each user turn. Within a slide, tool calls fold to chips
(their results linked in), consecutive identical calls collapse to one `N×` chip, and the
slide gets a title (from the user's prompt) and outcome badges (tests, errors, edits).
An LLM curator would consume the same reel and emit the same deck shape — this is just
the rule-based one.
"""

import re

_TEST_RE = re.compile(r"(\d+)\s+passed")
_FAIL_RE = re.compile(r"(\d+)\s+failed")


def _title_from_text(text: str, n: int = 62) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip().strip("*`").strip()
        if line:
            return line if len(line) <= n else line[: n - 1] + "…"
    return ""


def _first_heading(text: str) -> str:
    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _link_results(beats):
    """tool_use id -> its result beat (for folding the result into the call chip)."""
    out = {}
    for b in beats:
        if b["kind"] == "tool_result" and b.get("tool_use_id"):
            out[b["tool_use_id"]] = b
    return out


def _tool_element(call, result):
    el = {
        "type": "tool",
        "ts": call.get("ts"),  # so tool-heavy slides' ts_range covers the work, not just the prompt
        "label": call.get("label", call.get("name", "?").lower()),
        "arg": call.get("arg", ""),
        "stat": call.get("stat"),
        "count": 1,
        "result": None,
        "is_error": False,
    }
    if result:
        el["result"] = result.get("text", "")
        el["is_error"] = result.get("is_error", False)
    if call.get("subagent"):
        el["subagent"] = call["subagent"]
    if call.get("agent_id"):
        el["agent_id"] = call["agent_id"]
    return el


def _subagent_element(b):
    """A subagent launched with no tool call of its own (a forked skill)."""
    return {
        "type": "tool",
        "ts": b.get("ts"),
        "label": "agent",
        "arg": b.get("agentType") or "agent",
        "stat": (b.get("description") or "")[:40] or None,
        "count": 1,
        "result": None,
        "is_error": False,
        "subagent": b.get("subagent"),
        "agent_id": b.get("agent_id"),
    }


def _add_context(cur, b, first_seen, slide_no):
    """Append a context beat to the slide under construction.

    Identical context repeats constantly (the same reminder on every request), so only
    its first occurrence carries the text; later ones point back to that slide. The
    token budget changes every request: one chip per slide shows where it went."""
    if b["label"] == "budget":
        prev = next((e for e in cur if e.get("type") == "context" and e["label"] == "budget"), None)
        if prev:
            start = prev["arg"].split(" → ")[0].removesuffix(" tokens left")
            prev["arg"] = f"{start} → {b['arg']}"
            prev["count"] += 1
            return
        cur.append({**_ctx_base(b), "text": None})
        return
    key = b.get("key")
    el = _ctx_base(b)
    if key in first_seen:
        el["text"] = None
        el["seen"] = first_seen[key]
    else:
        first_seen[key] = slide_no
    last = cur[-1] if cur else None
    if (
        last
        and last.get("type") == "context"
        and last.get("key") == key
        and last.get("seen") is not None
        and el.get("seen") is not None
    ):
        last["count"] += 1
        return
    cur.append(el)


def _ctx_base(b):
    return {
        "type": "context",
        "ts": None,  # injected context shouldn't stretch a slide's time range
        "label": b["label"],
        "arg": b["arg"],
        "text": b["text"],
        "key": b.get("key"),
        "count": 1,
        "seen": None,
    }


def _slide_badges(elements):
    """Outcome badges scanned from a slide's tool results + errors + edits."""
    badges = []
    passed = failed = edits = 0
    any_error = False
    for el in elements:
        if el["type"] != "tool":
            continue
        if el["label"] in ("edit", "write"):
            edits += el["count"]
        res = el.get("result") or ""
        if el.get("is_error"):
            any_error = True
        m = _TEST_RE.search(res)
        if m:
            passed = max(passed, int(m.group(1)))
        f = _FAIL_RE.search(res)
        if f and int(f.group(1)):
            failed = max(failed, int(f.group(1)))
    if failed:
        badges.append({"cls": "err", "text": f"{failed} failed"})
    elif passed:
        badges.append({"cls": "ok", "text": f"{passed} passed"})
    if any_error and not failed:
        badges.append({"cls": "err", "text": "error"})
    if edits:
        badges.append({"cls": "", "text": f"{edits} edit" + ("s" if edits != 1 else "")})
    return badges


def _finish_slide(index, title, elements):
    tss = [e.get("ts") for e in elements if e.get("ts")]
    return {
        "index": index,
        "title": title or "Session start",
        "ts_range": [tss[0], tss[-1]] if tss else [None, None],
        "badges": _slide_badges(elements),
        "elements": elements,
    }


def curate(reel: dict, density: str = "jazz") -> dict:
    beats = reel["beats"]
    results = _link_results(beats)
    slides = []
    cur = []  # elements of the slide under construction
    title = ""
    index = 0
    first_seen = {}  # context key -> the slide number its text first appeared on

    def flush():
        nonlocal cur, title, index
        if cur:
            slides.append(_finish_slide(index, title, cur))
            index += 1
            cur = []
            title = ""

    for b in beats:
        kind = b["kind"]
        if kind == "user_say":
            # Context alone doesn't make a slide (the system prompt lands before the
            # first ask); it rides into the slide this prompt opens.
            if any(e["type"] != "context" for e in cur):
                flush()
            title = _title_from_text(b["text"])
            cur.append({"type": "user", "ts": b.get("ts"), "text": b["text"]})
        elif kind == "assistant_say":
            if not title:  # a leading assistant turn (before any user prompt)
                title = _first_heading(b["text"])
            cur.append({"type": "claude", "ts": b.get("ts"), "text": b["text"]})
        elif kind == "thinking":
            cur.append({"type": "thinking", "ts": b.get("ts"), "text": b["text"]})
        elif kind == "tool_call":
            el = _tool_element(b, results.get(b.get("id")))
            # repeat-collapse: identical adjacent call folds into an N× chip. Surface the
            # group's final state, not just the first — else a later failure hides behind
            # an earlier pass (OR the error flag, keep the latest result for the badges).
            if (
                cur
                and cur[-1].get("type") == "tool"
                and cur[-1]["label"] == el["label"]
                and cur[-1]["arg"] == el["arg"]
                and not (el.get("subagent") or cur[-1].get("subagent"))
            ):
                prev = cur[-1]
                prev["count"] += 1
                prev["is_error"] = prev["is_error"] or el["is_error"]
                if el["result"] is not None:
                    prev["result"] = el["result"]
            else:
                cur.append(el)
        elif kind == "subagent":
            cur.append(_subagent_element(b))
        elif kind == "context":
            # Slide numbers in the deck count the agenda as 0, so this slide is index + 1.
            _add_context(cur, b, first_seen, index + 1)
        # tool_result beats are folded into their call above; nothing to emit here
    flush()

    agenda = [
        {"n": s["index"], "title": s["title"], "badge": _lead_badge(s["badges"])} for s in slides
    ]
    # agent id -> the deck slide its chip sits on (agenda is slide 0), for the credits
    agent_slides = {
        e["agent_id"]: s["index"] + 1 for s in slides for e in s["elements"] if e.get("agent_id")
    }
    return {
        "meta": reel["meta"],
        "slides": slides,
        "agenda": agenda,
        "density": density,
        "agent_slides": agent_slides,
    }


def _lead_badge(badges):
    for b in badges:
        if b["cls"] == "ok":
            return b
    return badges[0] if badges else None
