"""Pipeline tests over synthetic transcripts — no store, no seam (sessions.py isn't
imported, so claude-ls isn't required to run these)."""

import json

from claude_reel.curate import curate
from claude_reel.extract import derive_tool_summary, extract
from claude_reel.markdown import render_markdown
from claude_reel.render_html import render


def _write(tmp_path, records):
    f = tmp_path / "sess1234-abcd-0000.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return f


def _u(text, ts):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def _a_blocks(blocks, ts, model="claude-opus-4-8"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "model": model, "content": blocks},
    }


def _result(tid, text, ts, is_error=False):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error}
            ],
        },
    }


def _transcript(tmp_path):
    recs = [
        {
            **_u("Please add the widget", "2026-07-05T10:00:00Z"),
            "cwd": "/x/myrepo",
            "gitBranch": "main",
            "sessionId": "sess1234",
        },
        _a_blocks(
            [
                {"type": "text", "text": "On it. **Editing** now."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Edit",
                    "input": {
                        "file_path": "/x/myrepo/app.py",
                        "old_string": "a\nb",
                        "new_string": "a\nb\nc\nd",
                    },
                },
            ],
            "2026-07-05T10:00:01Z",
        ),
        _result("t1", "ok", "2026-07-05T10:00:02Z"),
        _a_blocks(
            [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}],
            "2026-07-05T10:00:03Z",
        ),
        _result("t2", "..... 54 passed in 1.2s", "2026-07-05T10:00:04Z"),
        _u(
            "<system-reminder>ignore me</system-reminder>", "2026-07-05T10:00:05Z"
        ),  # injected -> dropped
        _u("now write the docs", "2026-07-05T10:01:00Z"),
        _a_blocks(
            [
                {
                    "type": "tool_use",
                    "id": "t3",
                    "name": "Edit",
                    "input": {
                        "file_path": "/x/myrepo/README.md",
                        "old_string": "x",
                        "new_string": "y",
                    },
                },
                {
                    "type": "tool_use",
                    "id": "t4",
                    "name": "Edit",
                    "input": {
                        "file_path": "/x/myrepo/README.md",
                        "old_string": "p",
                        "new_string": "q",
                    },
                },
            ],
            "2026-07-05T10:01:01Z",
        ),
    ]
    return _write(tmp_path, recs)


def test_extract_beats_and_meta(tmp_path):
    reel = extract(_transcript(tmp_path))
    kinds = [b["kind"] for b in reel["beats"]]
    assert kinds.count("user_say") == 2  # injected system-reminder dropped
    assert "assistant_say" in kinds and "tool_call" in kinds and "tool_result" in kinds
    m = reel["meta"]
    assert m["project"] == "myrepo"
    assert m["branch"] == "main"
    assert m["model"] == "claude-opus-4-8"
    assert m["tool_calls"]["Edit"] == 3 and m["tool_calls"]["Bash"] == 1


def test_extract_drops_synthetic_user_turns(tmp_path):
    recs = [
        {**_u("real prompt one", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
        {
            **_u("Approach this as the design lead …", "2026-07-05T10:00:01Z"),
            "isMeta": True,
        },  # skill inject
        _u(
            "[Request interrupted by user for tool use]", "2026-07-05T10:00:02Z"
        ),  # interrupt marker
        _u("real prompt two", "2026-07-05T10:00:03Z"),
    ]
    reel = extract(_write(tmp_path, recs))
    says = [b["text"] for b in reel["beats"] if b["kind"] == "user_say"]
    assert says == ["real prompt one", "real prompt two"]


def test_tool_summary_diff_stat():
    label, arg, stat = derive_tool_summary(
        "Edit", {"file_path": "/x/app.py", "old_string": "a\nb", "new_string": "a\nb\nc\nd"}
    )
    assert label == "edit" and arg == "app.py"
    assert stat == "+4 −2"
    assert derive_tool_summary("Bash", {"command": "pytest -q\nsecond line"})[0] == "run"
    assert derive_tool_summary("Read", {"file_path": "/a/b/render.py"}) == (
        "read",
        "render.py",
        None,
    )


def test_curate_slides_badges_and_repeat(tmp_path):
    deck = curate(extract(_transcript(tmp_path)))
    assert len(deck["slides"]) == 2  # one per real user turn
    s0, s1 = deck["slides"]
    assert s0["title"] == "Please add the widget"
    assert any(b["text"] == "54 passed" for b in s0["badges"])
    # second slide: two identical adjacent Edits collapse to one N× chip
    tools = [e for e in s1["elements"] if e["type"] == "tool"]
    assert len(tools) == 1 and tools[0]["count"] == 2
    # tool elements carry ts, so the slide's range extends past the prompt to the work
    assert s1["ts_range"] == ["2026-07-05T10:01:00Z", "2026-07-05T10:01:01Z"]


def test_repeat_collapse_surfaces_later_failure(tmp_path):
    # two identical Bash calls; the second fails. The collapsed chip must reflect the
    # failure, not the first call's pass.
    recs = [
        {**_u("run the tests twice", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
        _a_blocks(
            [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}],
            "2026-07-05T10:00:01Z",
        ),
        _result("t1", "1 passed", "2026-07-05T10:00:02Z"),
        _a_blocks(
            [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}],
            "2026-07-05T10:00:03Z",
        ),
        _result("t2", "boom: 1 failed", "2026-07-05T10:00:04Z", is_error=True),
    ]
    s = curate(extract(_write(tmp_path, recs)))["slides"][0]
    tools = [e for e in s["elements"] if e["type"] == "tool"]
    assert len(tools) == 1 and tools[0]["count"] == 2
    assert tools[0]["is_error"] is True
    badges = [b["text"] for b in s["badges"]]
    assert any("failed" in b for b in badges) and "1 passed" not in badges


def test_render_html_is_escaped(tmp_path):
    recs = [
        {
            **_u("danger <script>alert(1)</script> and **bold**", "2026-07-05T10:00:00Z"),
            "cwd": "/x/repo",
        },
        _a_blocks([{"type": "text", "text": "done"}], "2026-07-05T10:00:01Z"),
    ]
    html = render(curate(extract(_write(tmp_path, recs))))
    assert "&lt;script&gt;alert(1)" in html  # user content escaped
    assert "<script>alert(1)" not in html  # ...never live
    assert "claude" in html and "recap" in html


def test_markdown_safe_and_formatted():
    out = render_markdown("**bold** and `co<de`\n\n- one\n- two")
    assert "<strong>bold</strong>" in out
    assert "<code>co&lt;de</code>" in out  # escaped inside code span
    assert "<li>one</li>" in out and "<ul>" in out
    assert "<script>" not in render_markdown("<script>evil()</script>")


def test_markdown_link_href_cannot_break_out():
    out = render_markdown('[x](https://evil"onmouseover=alert)')
    assert '"onmouseover' not in out  # the quote can't close the href attribute
    assert "&quot;onmouseover" in out


# ---- injected context (api-request-blob, prompt_snapshot) ----------------------


def _blob(role, text):
    # Real blobs carry no timestamp; they sit between the entries of the request they
    # were sent with.
    return {
        "type": "api-request-blob",
        "hash": "h",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _context_transcript(tmp_path):
    return _write(
        tmp_path,
        [
            {
                "type": "attachment",
                "timestamp": "2026-07-05T09:59:59Z",
                "attachment": {
                    "type": "prompt_snapshot",
                    "systemPrompt": ["You are an agent.", "Be brief."],
                },
            },
            {**_u("first ask", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
            _blob("user", "first ask"),  # copies the prompt above
            _blob("system", "<total_tokens>15000000 tokens left</total_tokens>"),
            _blob("system", "# Environment update\n - cwd moved"),
            _blob("assistant", "ok"),  # copies Claude's reply
            _a_blocks([{"type": "text", "text": "ok"}], "2026-07-05T10:00:01Z"),
            _blob("system", "<total_tokens>14950755 tokens left</total_tokens>"),
            _u("second ask", "2026-07-05T10:01:00Z"),
            _blob("system", "# Environment update\n - cwd moved"),  # same text again
            _a_blocks([{"type": "text", "text": "done"}], "2026-07-05T10:01:01Z"),
        ],
    )


def test_request_blobs_become_context_not_prose(tmp_path):
    """The bug: blob entries carry a `message`, so their parts became beats, and the
    token budget showed up as Claude talking."""
    beats = extract(_context_transcript(tmp_path))["beats"]
    says = [(b["kind"], b["text"]) for b in beats if b["kind"].endswith("_say")]
    assert says == [
        ("user_say", "first ask"),
        ("assistant_say", "ok"),
        ("user_say", "second ask"),
        ("assistant_say", "done"),
    ]
    ctx = [(b["label"], b["arg"]) for b in beats if b["kind"] == "context"]
    assert ctx == [
        ("sysprompt", "You are an agent."),
        ("budget", "15.00M tokens left"),
        ("env", "Environment update"),
        ("budget", "14.95M tokens left"),
        ("env", "Environment update"),
    ]


def test_context_none_keeps_only_the_conversation(tmp_path):
    beats = extract(_context_transcript(tmp_path), context=False)["beats"]
    assert {b["kind"] for b in beats} == {"user_say", "assistant_say"}


def test_injected_user_turns_show_as_context(tmp_path):
    recs = [
        {**_u("real prompt", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
        _u("<system-reminder>\nmind the gap\n</system-reminder>", "2026-07-05T10:00:01Z"),
    ]
    beats = extract(_write(tmp_path, recs))["beats"]
    assert [b["kind"] for b in beats] == ["user_say", "context"]
    assert (beats[1]["label"], beats[1]["arg"]) == ("reminder", "mind the gap")


def test_compaction_summary_is_context_not_your_turn(tmp_path):
    summary = (
        "This session is being continued from a previous conversation.\n\n"
        "<system-reminder>\n<tools-used-before-summary>Bash</tools-used-before-summary>\n"
        "</system-reminder>\nSummary: we built the widget."
    )
    f = _write(
        tmp_path,
        [
            _u("Build the widget", "2026-07-05T10:00:00Z"),
            {**_u(summary, "2026-07-05T11:00:00Z"), "isCompactSummary": True},
            _u("Now test it", "2026-07-05T11:01:00Z"),
        ],
    )
    beats = extract(f)["beats"]
    assert [b["text"] for b in beats if b["kind"] == "user_say"] == [
        "Build the widget",
        "Now test it",
    ]
    assert [b["label"] for b in beats if b["kind"] == "context"] == ["compaction"]
    assert not [b for b in extract(f, context=False)["beats"] if b["kind"] != "user_say"]


def test_paste_folds_inside_your_turn(tmp_path):
    f = _write(
        tmp_path,
        [
            _u(
                '<pasted_content id="ab12">\nTraceback line one\nline two\n</pasted_content id="ab12">\n'
                "why does this fail?",
                "2026-07-05T10:00:00Z",
            ),
            _u('<pasted_content id="cd34">\nonly a paste, cut off', "2026-07-05T10:05:00Z"),
        ],
    )
    deck = curate(extract(f))
    assert [s["title"] for s in deck["slides"]] == ["why does this fail?", "only a paste, cut off"]
    html = render(deck)
    assert "pasted_content" not in html
    assert html.count('<details class="paste">') == 2
    assert "pasted · 2 lines" in html and "pasted · 1 line<" in html


def test_paste_needs_the_real_wrapper_and_keeps_its_indent():
    from claude_reel.extract import split_pastes
    from claude_reel.render_html import _user_body

    typed = "why does `<pasted_content>` leak?"
    assert split_pastes(typed) == [(False, typed)]
    html = _user_body(
        '<pasted_content id="x">\n    def f():\n        return 1\n</pasted_content id="x">'
    )
    assert '<pre class="paste-body">    def f():' in html
    assert _user_body('see <pasted_content id="y">\n</pasted_content id="y">').count("details") == 0


def test_repeated_context_points_back_and_budget_collapses(tmp_path):
    deck = curate(extract(_context_transcript(tmp_path)))
    s0, s1 = deck["slides"]
    ctx0 = [e for e in s0["elements"] if e["type"] == "context"]
    # one budget chip per slide, showing where the budget went
    budgets = [e for e in ctx0 if e["label"] == "budget"]
    assert len(budgets) == 1 and budgets[0]["arg"] == "15.00M → 14.95M tokens left"
    env1 = next(e for e in s1["elements"] if e["type"] == "context" and e["label"] == "env")
    assert env1["seen"] == 1 and env1["text"] is None  # slide 1 carries the text
    html = render(deck)
    assert 'href="#s1">unchanged since beat 1</a>' in html
    s1["elements"].append({**env1, "seen": 2})  # a repeat within its own slide
    assert "repeated above" in render(deck)
    assert 'id="s0"' in html and 'id="s2"' in html  # agenda and slides are addressable
    assert 'id="fq"' in html  # the find box


def test_tail_counts_conversation_not_context():
    from claude_reel.cli import _filter_beats

    beats = [{"kind": "user_say"}, {"kind": "context"}, {"kind": "assistant_say"}] + [
        {"kind": "context"}
    ] * 5
    assert [b["kind"] for b in _filter_beats(beats, 1, None)][0] == "assistant_say"
    assert len(_filter_beats(beats, 2, None)) == len(beats)


# ---- subagents ---------------------------------------------------------------------


def _with_subagents(tmp_path):
    session = _write(
        tmp_path,
        [
            {**_u("review it", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
            _a_blocks(
                [
                    {
                        "type": "tool_use",
                        "id": "tA",
                        "name": "Agent",
                        "input": {"subagent_type": "Explore", "description": "find callers"},
                    }
                ],
                "2026-07-05T10:00:01Z",
            ),
            _result("tA", "callers: a.py", "2026-07-05T10:00:05Z"),
            # a forked skill: no tool call, only a launch line naming the agent
            {
                "type": "system",
                "timestamp": "2026-07-05T10:00:06Z",
                "content": 'launched {"agentId": "bbb222"}',
            },
        ],
    )
    sub_dir = session.with_suffix("") / "subagents"
    sub_dir.mkdir(parents=True)
    agents = {
        "aaa111": (
            {"agentType": "Explore", "description": "find callers", "toolUseId": "tA"},
            "Find the callers of f",
            "Two callers: a.py and b.py.",
        ),
        "bbb222": (
            {"agentType": "general-purpose", "description": "/code-review"},
            "Review target: 15",
            "No findings.",
        ),
    }
    for aid, (meta, prompt, final) in agents.items():
        (sub_dir / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
        (sub_dir / f"agent-{aid}.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    # forked-skill prompts are flagged isMeta; the brief must still find them
                    {**_u(prompt, "2026-07-05T10:00:02Z"), "isMeta": True},
                    _a_blocks(
                        [
                            {
                                "type": "tool_use",
                                "id": f"x{aid}",
                                "name": "Grep",
                                "input": {"pattern": "f("},
                            }
                        ],
                        "2026-07-05T10:00:03Z",
                    ),
                    _a_blocks([{"type": "text", "text": final}], "2026-07-05T10:00:04Z"),
                ]
            )
        )
    return session


def test_subagents_none_links_tool_calls_only(tmp_path):
    beats = extract(_with_subagents(tmp_path))["beats"]
    call = next(b for b in beats if b["kind"] == "tool_call")
    assert call["agentType"] == "Explore" and "subagent" not in call
    assert not any(b["kind"] == "subagent" for b in beats)


def test_subagents_brief_places_every_agent(tmp_path):
    session = _with_subagents(tmp_path)
    beats = extract(session, subagents="brief")["beats"]
    call = next(b for b in beats if b["kind"] == "tool_call")
    assert call["subagent"]["prompt"] == "Find the callers of f"
    assert call["subagent"]["final"] == "Two callers: a.py and b.py."
    assert call["subagent"]["tool_calls"] == {"Grep": 1}
    forked = [b for b in beats if b["kind"] == "subagent"]
    assert len(forked) == 1 and forked[0]["subagent"]["prompt"] == "Review target: 15"
    html = render(curate({"meta": {}, "beats": beats}))
    assert "Two callers: a.py and b.py." in html
    assert 'data-copy="claude-reel ' in html and "agent-bbb222.jsonl --open" in html


def test_blob_copy_arriving_before_its_entry_is_still_skipped(tmp_path):
    """Most blobs follow the entry they copy, but some arrive first (seen on real
    sessions); the copy check has to wait for the whole file."""
    recs = [
        {**_u("first ask", "2026-07-05T10:00:00Z"), "cwd": "/x/r"},
        _blob("user", "second ask"),  # copy, ahead of its entry
        _u("second ask", "2026-07-05T10:01:00Z"),
    ]
    beats = extract(_write(tmp_path, recs))["beats"]
    assert [b["kind"] for b in beats] == ["user_say", "user_say"]
    assert not any("copy_check" in b for b in beats)


def test_sidecar_without_transcript_still_names_the_agent(tmp_path):
    session = _with_subagents(tmp_path)
    (session.with_suffix("") / "subagents" / "agent-aaa111.jsonl").unlink()
    for mode in ("none", "brief"):
        beats = extract(session, subagents=mode)["beats"]
        call = next(b for b in beats if b["kind"] == "tool_call")
        assert call["agentType"] == "Explore" and "subagent" not in call


# ---- credits ------------------------------------------------------------------------


def _lineage():
    def s(sid, start, **kw):
        return {
            "sid": sid,
            "start": start,
            "end": None,
            "cwd": "/x/r",
            "branch": "main",
            "topic": f"topic {sid[:4]}",
            "compactions": [],
            "subagents": [],
            **kw,
        }

    return {
        "seed": "seed0000",
        "sessions": [
            s(
                "seed0000",
                "2026-07-05",
                compactions=[
                    {
                        "timestamp": "2026-07-05T12:00:00Z",
                        "trigger": "auto",
                        "tokens_before": 202458,
                        "tokens_after": 15440,
                    }
                ],
            ),
            s("gran0000", "2026-07-01"),
            s("pare0000", "2026-07-03"),
            s("sibl0000", "2026-07-04"),
            s("next0000", "2026-07-06"),
            s("othr0000", "2026-07-05"),  # a second parent of next0000
            s("nephew00", "2026-07-07"),  # the sibling's own sequel
        ],
        "edges": [
            {"src": "gran0000", "dst": "pare0000", "kind": "handoff"},
            {"src": "pare0000", "dst": "seed0000", "kind": "clear"},
            {"src": "pare0000", "dst": "sibl0000", "kind": "mention"},
            {"src": "seed0000", "dst": "next0000", "kind": "clear+mention"},
            {"src": "othr0000", "dst": "next0000", "kind": "mention"},
            {"src": "sibl0000", "dst": "nephew00", "kind": "handoff"},
        ],
    }


def test_family_sorts_sessions_relative_to_this_one():
    from claude_reel.credits import family

    fam = family(_lineage(), "seed0000")
    assert [(r["sid"], r["hops"], r["kind"]) for r in fam["previously"]] == [
        ("gran0000", 2, "clear"),  # labelled by the link nearest this session
        ("pare0000", 1, "clear"),
    ]
    assert [(r["sid"], r["kind"]) for r in fam["sequels"]] == [("next0000", "clear+mention")]
    assert [(r["sid"], r["hops"], r["direction"], r["via"]) for r in fam["related"]] == [
        ("sibl0000", 2, "from", "pare0000"),
        ("othr0000", 2, "to", "next0000"),
        ("nephew00", 3, "from", "sibl0000"),
    ]
    # the file lineage was given is the seed, even if the transcript's own id differs
    resumed = {**_lineage(), "seed": "pare0000"}
    assert "seed0000" in [r["sid"] for r in family(resumed, "seed0000")["sequels"]]
    assert fam["compactions"][0]["tokens_after"] == 15440
    assert family(None, "seed0000")["previously"] == []


def test_cast_groups_by_type_and_command():
    from claude_reel.credits import cast

    agents = [
        {"id": "a", "type": "general-purpose", "description": "/code-review medium 15"},
        {"id": "b", "type": "general-purpose", "description": "/code-review medium 16"},
        {"id": "c", "type": "Explore", "description": "find callers"},
    ]
    groups = cast(agents, {"a": 7, "c": 3})
    assert [(g["type"], g["role"], g["count"], g["slides"]) for g in groups] == [
        ("general-purpose", "/code-review", 2, [7]),
        ("Explore", "find callers", 1, [3]),
    ]


def test_credits_card_closes_the_deck_and_the_agenda_points_to_it(tmp_path):
    from claude_reel.credits import cast, family

    reel = extract(_with_subagents(tmp_path), subagents="brief")
    deck = curate(reel)
    deck["credits"] = {
        "cast": cast(reel["meta"]["subagents"], deck["agent_slides"]),
        "family": family(_lineage(), "seed0000"),
        "lineage": True,
    }
    html = render(deck)
    last = len(deck["slides"]) + 1
    assert f'id="s{last}"' in html and "The cast &amp; the family" in html
    assert f'2 subagents · 6 related sessions <a href="#s{last}">→ credits</a>' in html
    assert "after /clear · 2 links back" in html  # grandparent, two links up
    assert "mentioned from pare0000" in html
    assert 'data-copy="claude-reel next0000 --open"' in html
    assert "auto 07-05 12:00 202k → 15k" in html
    assert "mentioned to next0000" in html
    assert 'class="beats"' in html
    deck["density"] = "tldr"  # no beat slides rendered, so no links into them
    assert 'class="beats"' not in render(deck)


def test_no_credits_without_anything_to_credit(tmp_path):
    html = render(curate(extract(_transcript(tmp_path))))
    assert 'class="castline"' not in html and 'class="slide credits"' not in html


def test_lineage_is_optional(monkeypatch, tmp_path):
    from claude_reel import credits

    monkeypatch.setattr(credits.shutil, "which", lambda _: None)
    assert credits.fetch_lineage(tmp_path / "x.jsonl") is None


def test_rail_has_a_jump_to_the_last_slide(tmp_path):
    html = render(curate(extract(_transcript(tmp_path))))
    assert 'id="last" aria-label="Last slide"' in html
    assert "lastBtn.addEventListener('click', function(){ go(last); })" in html


def test_chip_density_toggle_ships_with_every_deck(tmp_path):
    html = render(curate(extract(_transcript(tmp_path))))
    assert 'id="dens"' in html and '<span id="densv">rows</span>' in html
    for hook in (
        ".strip.dense-inline",
        ".strip.dense-compact",
        "function cycleDensity",
        "function unfoldStrip",
        "claude-reel.chips",
    ):
        assert hook in html
