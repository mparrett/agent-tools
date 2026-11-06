"""Tests for the preview-diff review server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path

import pytest
import review_server as rs

PAGE = (
    "<!doctype html><html><body><h1>diff</h1>"
    '<script type="application/json" id="review-data"></script>'
    "</body></html>"
)

NOW = datetime(2026, 7, 27, 21, 5, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def test_validate_fills_defaults():
    out = rs.validate({"comments": [{"path": "a.py", "line": 4, "body": "nit"}]})
    assert out["reviewer"] == ""
    assert out["body"] == ""
    assert out["comments"] == [
        {
            "path": "a.py",
            "file_id": None,
            "side": "new",
            "start_line": None,
            "line": 4,
            "body": "nit",
        }
    ]


def test_validate_keeps_span_and_side():
    out = rs.validate(
        {
            "reviewer": " Matt ",
            "body": "  looks fine  ",
            "comments": [{"path": "a.py", "side": "old", "start_line": 2, "line": 9, "body": "x"}],
        }
    )
    assert out["reviewer"] == "Matt"
    assert out["body"] == "looks fine"
    c = out["comments"][0]
    assert (c["side"], c["start_line"], c["line"]) == ("old", 2, 9)


def test_validate_keeps_file_id():
    out = rs.validate(
        {"comments": [{"path": "a.py", "line": 4, "body": "x", "file_id": "file-12"}]}
    )
    assert out["comments"][0]["file_id"] == "file-12"


def test_validate_defaults_file_id_to_none():
    out = rs.validate({"comments": [{"path": "a.py", "line": 4, "body": "x"}]})
    assert out["comments"][0]["file_id"] is None


@pytest.mark.parametrize("bad", ["file-", "files-1", "file-1x", 3, "", "../file-1"])
def test_validate_rejects_a_bad_file_id(bad):
    with pytest.raises(ValueError, match="file_id"):
        rs.validate({"comments": [{"path": "a.py", "line": 4, "body": "x", "file_id": bad}]})


def test_validate_general_comment_only_is_a_review():
    out = rs.validate({"body": "ship it"})
    assert out["comments"] == []


@pytest.mark.parametrize(
    "payload, msg",
    [
        ([], "expected a JSON object"),
        ({}, "either a general comment or a line comment"),
        ({"comments": {}}, "expected a list"),
        ({"comments": [1]}, "expected an object"),
        ({"comments": [{"path": "a", "line": 1, "body": "b", "side": "both"}]}, "side"),
        ({"comments": [{"path": "", "line": 1, "body": "b"}]}, "must not be empty"),
        ({"comments": [{"path": "a", "line": 1, "body": ""}]}, "must not be empty"),
        ({"comments": [{"path": "a", "line": 0, "body": "b"}]}, "start at 1"),
        ({"comments": [{"path": "a", "line": "4", "body": "b"}]}, "integer"),
        (
            {"comments": [{"path": "a", "start_line": 9, "line": 4, "body": "b"}]},
            "must not exceed line",
        ),
        ({"reviewer": 7, "body": "x"}, "expected a string"),
    ],
)
def test_validate_rejects(payload, msg):
    with pytest.raises(ValueError) as exc:
        rs.validate(payload)
    assert msg in str(exc.value)


def test_validate_rejects_bool_line():
    # bool subclasses int; True must not slide through as line 1.
    with pytest.raises(ValueError, match="integer"):
        rs.validate({"comments": [{"path": "a", "line": True, "body": "b"}]})


def test_validate_caps_comment_count():
    many = [{"path": "a", "line": 1, "body": "b"}] * (rs.MAX_COMMENTS + 1)
    with pytest.raises(ValueError, match="more than"):
        rs.validate({"comments": many})


def test_validate_caps_text_length():
    with pytest.raises(ValueError, match="longer than"):
        rs.validate({"body": "x" * (rs.MAX_TEXT + 1)})


# --------------------------------------------------------------------------
# bake
# --------------------------------------------------------------------------


def test_bake_roundtrips_through_json():
    out = rs.bake(PAGE, {"body": "hi", "comments": []})
    blob = out.split('id="review-data">')[1].split("</script>")[0]
    assert json.loads(blob) == {"body": "hi", "comments": []}


def test_bake_neutralizes_script_close_in_a_comment():
    hostile = {"body": "</script><img src=x onerror=alert(1)>", "comments": []}
    out = rs.bake(PAGE, hostile)
    # Exactly one closing tag survives: the block's own.
    assert out.count("</script>") == 1
    assert "<img" not in out
    blob = out.split('id="review-data">')[1].split("</script>")[0]
    assert json.loads(blob)["body"] == hostile["body"]


def test_bake_requires_the_slot():
    with pytest.raises(ValueError, match="no review-data block"):
        rs.bake("<html><body>nothing here</body></html>", {"body": "x"})


def test_bake_leaves_the_rest_of_the_page_alone():
    out = rs.bake(PAGE, {"body": "x"})
    assert "<h1>diff</h1>" in out


# --------------------------------------------------------------------------
# render_markdown
# --------------------------------------------------------------------------


def test_markdown_leads_with_the_body_verbatim():
    # Whatever structure the author gave the review is theirs; it must survive
    # untouched, since that is the whole point of the freeform body.
    body = "**[P1]** `app.py:42` drops the error\n\n```python\nreturn None\n```"
    md = rs.render_markdown({"body": body, "comments": [], "reviewer": "Matt"})
    assert md.startswith(body)


def test_markdown_is_postable_with_no_comments():
    md = rs.render_markdown(
        {
            "body": "ship it",
            "comments": [],
            "reviewer": "Matt",
            "created": "2026-07-27T12:00:00+00:00",
        }
    )
    assert "### Line comments" not in md
    assert md.rstrip().endswith("*Review · Matt · 2026-07-27 · via `preview-diff`*")
    assert md.endswith("\n")


def test_markdown_appends_line_comments_rather_than_dropping_them():
    md = rs.render_markdown(
        {
            "body": "general",
            "reviewer": "",
            "comments": [
                {"path": "a.py", "side": "new", "start_line": None, "line": 4, "body": "nit"},
                {"path": "b.py", "side": "old", "start_line": 2, "line": 9, "body": "span note"},
            ],
        }
    )
    assert "### Line comments" in md
    assert "- `a.py:4` — nit" in md
    assert "- `b.py:2-9` (old) — span note" in md


def test_markdown_indents_a_multiline_comment_into_its_bullet():
    md = rs.render_markdown(
        {
            "body": "",
            "comments": [
                {"path": "a.py", "side": "new", "start_line": None, "line": 4, "body": "one\ntwo"}
            ],
        }
    )
    assert "- `a.py:4` — one\n  two" in md


def test_markdown_survives_a_body_only_review():
    md = rs.render_markdown({"body": "just prose", "comments": []})
    assert md.startswith("just prose")
    assert "preview-diff" in md


def test_markdown_names_the_title_when_present():
    md = rs.render_markdown({"body": "x", "comments": [], "title": "my branch"})
    assert 'Review of "my branch"' in md


# --------------------------------------------------------------------------
# ids and paths
# --------------------------------------------------------------------------


def test_new_review_id_sorts_chronologically():
    a = rs.new_review_id(datetime(2026, 1, 2, 3, 4, 5), "aaaaaa")
    b = rs.new_review_id(datetime(2026, 1, 2, 3, 4, 6), "000000")
    assert a == "20260102T030405-aaaaaa"
    assert a < b


def test_xdg_data_home_prefers_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert rs.xdg_data_home() == tmp_path
    assert rs.default_out_root() == tmp_path / "preview-diff" / "reviews"


def test_xdg_data_home_falls_back_to_spec_default(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "   ")
    assert rs.xdg_data_home() == Path.home() / ".local" / "share"


# --------------------------------------------------------------------------
# save_review
# --------------------------------------------------------------------------


def test_save_review_writes_the_bundle(tmp_path):
    sub = rs.validate(
        {"reviewer": "Matt", "comments": [{"path": "a.py", "line": 3, "body": "nit"}]}
    )
    res = rs.save_review(
        sub,
        out_root=tmp_path,
        page_html=PAGE,
        meta={"title": "t", "base": "main", "head": "HEAD"},
        diff_text="--- a\n+++ b\n",
        now=NOW,
        review_id="rid",
    )

    out = tmp_path / "rid"
    assert res["dir"] == str(out)
    assert res["comments"] == 1
    assert res["url"].startswith("file://")

    data = json.loads((out / "review.json").read_text())
    assert data["id"] == "rid"
    assert data["created"] == "2026-07-27T21:05:00+00:00"
    assert data["title"] == "t" and data["base"] == "main"
    assert data["reviewer"] == "Matt"
    assert (out / "source.diff").read_text() == "--- a\n+++ b\n"

    # The postable view lands beside the page.
    assert res["review_md"] == str(out / "review.md")
    assert "nit" in (out / "review.md").read_text()

    # The saved page carries the review, so it renders standalone.
    baked = (out / "review.html").read_text()
    blob = baked.split('id="review-data">')[1].split("</script>")[0]
    assert json.loads(blob)["comments"][0]["body"] == "nit"


def test_save_review_skips_an_empty_diff(tmp_path):
    sub = rs.validate({"body": "ok"})
    rs.save_review(sub, out_root=tmp_path, page_html=PAGE, meta={}, review_id="rid", now=NOW)
    assert not (tmp_path / "rid" / "source.diff").exists()


def test_save_review_resolves_a_relative_out_root(tmp_path, monkeypatch):
    # A relative --review-out used to write every file and *then* fail in
    # as_uri(), reporting a 500 for a review that was already on disk.
    monkeypatch.chdir(tmp_path)
    sub = rs.validate({"body": "ok"})
    res = rs.save_review(
        sub, out_root=Path("myreviews"), page_html=PAGE, meta={}, review_id="rid", now=NOW
    )
    assert res["url"].startswith("file://")
    assert Path(res["review_html"]).is_absolute()
    assert (tmp_path / "myreviews" / "rid" / "review.json").is_file()


def test_save_review_creates_nested_roots(tmp_path):
    sub = rs.validate({"body": "ok"})
    root = tmp_path / "deep" / "nest"
    rs.save_review(sub, out_root=root, page_html=PAGE, meta={}, review_id="rid", now=NOW)
    assert (root / "rid" / "review.json").is_file()


# --------------------------------------------------------------------------
# the HTTP surface
# --------------------------------------------------------------------------


@pytest.fixture()
def server(tmp_path):
    """A live review server on an ephemeral port."""
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    (serve_dir / "index.html").write_text(PAGE)
    (serve_dir / "source.diff").write_text("diff text\n")
    out_root = tmp_path / "reviews"

    rs.ReviewHandler.serve_dir = serve_dir
    rs.ReviewHandler.out_root = out_root
    rs.ReviewHandler.diff_path = serve_dir / "source.diff"
    rs.ReviewHandler.meta = {"title": "t", "base": "main", "head": "HEAD"}

    httpd = HTTPServer(("127.0.0.1", 0), rs.ReviewHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", out_root
    httpd.shutdown()
    httpd.server_close()


def _post(url, body, ctype="application/json"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_get_serves_the_page(server):
    base, _ = server
    with urllib.request.urlopen(base + "/") as r:
        assert r.status == 200
        assert b"<h1>diff</h1>" in r.read()


def test_get_serves_a_page_not_named_index(tmp_path):
    # With -o <file> the page is not index.html; it must still answer at /,
    # since that is the URL preview-diff prints.
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    (serve_dir / "q.html").write_text(PAGE)
    rs.ReviewHandler.serve_dir = serve_dir
    rs.ReviewHandler.page_name = "q.html"
    rs.ReviewHandler.out_root = tmp_path / "reviews"
    rs.ReviewHandler.diff_path = None
    rs.ReviewHandler.meta = {}

    httpd = HTTPServer(("127.0.0.1", 0), rs.ReviewHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for path in ("/", "/q.html"):
            with urllib.request.urlopen(base + path) as r:
                assert r.status == 200, path
    finally:
        httpd.shutdown()
        httpd.server_close()
        rs.ReviewHandler.page_name = "index.html"


def test_get_unknown_path_404s(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/nope")
    assert exc.value.code == 404


def test_post_saves_and_reports_paths(server):
    base, out_root = server
    status, body = _post(
        base + "/review",
        {
            "reviewer": "Matt",
            "body": "general",
            "comments": [{"path": "a.py", "line": 2, "body": "nit"}],
        },
    )
    assert status == 201
    assert body["comments"] == 1

    saved = Path(body["review_json"])
    assert saved.is_file() and saved.parent.parent == out_root
    data = json.loads(saved.read_text())
    assert data["reviewer"] == "Matt"
    assert data["title"] == "t"
    # The diff under review travels with the review.
    assert (saved.parent / "source.diff").read_text() == "diff text\n"
    assert Path(body["review_html"]).is_file()


def test_post_rejects_a_form_content_type(server):
    # The CSRF guard: a cross-origin form POST cannot set a JSON content type.
    base, _ = server
    status, body = _post(base + "/review", b"body=x", ctype="application/x-www-form-urlencoded")
    assert status == 415
    assert "application/json" in body["error"]


def test_post_rejects_malformed_json(server):
    base, _ = server
    status, body = _post(base + "/review", b"{not json")
    assert status == 400
    assert "invalid JSON" in body["error"]


def test_post_reports_validation_errors(server):
    base, _ = server
    status, body = _post(base + "/review", {"comments": [{"path": "a", "line": 0, "body": "b"}]})
    assert status == 422
    assert "start at 1" in body["error"]


def test_post_rejects_an_empty_review(server):
    base, _ = server
    status, body = _post(base + "/review", {})
    assert status == 422
    assert "general comment or a line comment" in body["error"]


def test_post_to_an_unknown_path_404s(server):
    base, _ = server
    status, _body = _post(base + "/elsewhere", {"body": "x"})
    assert status == 404


def test_two_submissions_land_in_separate_directories(server):
    base, out_root = server
    a = _post(base + "/review", {"body": "one"})[1]
    b = _post(base + "/review", {"body": "two"})[1]
    assert a["dir"] != b["dir"]
    assert len(list(out_root.iterdir())) == 2
