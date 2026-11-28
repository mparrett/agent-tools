"""Live mode's screenshots: found in the transcript, served behind the token, and
rendered as lazy thumbnails only when the page has somewhere to fetch them from."""

import base64
import http.client
import json
import struct
import threading
from http.server import ThreadingHTTPServer

from claude_reel import live
from claude_reel.curate import curate
from claude_reel.extract import extract
from claude_reel.render_html import render, render_parts
from claude_reel.shots import Shots

PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 4, 3) + b"\0" * 64


def _img(data):
    return {"type": "image", "source": {"type": "base64", "data": base64.b64encode(data).decode()}}


def _transcript(tmp_path, results):
    """One Read call per entry of `results` (a list of image-byte lists)."""
    recs = [{"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "look"}}]
    for i, images in enumerate(results):
        tid = f"toolu_{i}"
        recs.append(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tid,
                            "name": "Read",
                            "input": {"file_path": f"/x/{i}.png"},
                        }
                    ]
                },
            }
        )
        content = [_img(d) for d in images]
        # A request-log copy of the same result comes first, as the real store has.
        recs.append(
            {
                "type": "api-request-blob",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": [_img(b"GIF89a-not-this")],
                        }
                    ]
                },
            }
        )
        recs.append(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]
                },
            }
        )
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return f


def test_shots_finds_the_conversations_copy_and_refuses_the_rest(tmp_path):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    f = _transcript(tmp_path, [[PNG, svg]])
    s = Shots(f)
    assert s.get("toolu_0", 0) == (PNG, "image/png")  # not the request log's GIF
    assert s.get("toolu_0", 1) is None  # an SVG is never served
    assert s.get("toolu_0", 2) is None and s.get("toolu_9", 0) is None
    assert s.get("../../etc/passwd", 0) is None


def test_thumbnails_only_with_an_image_url_and_lazy(tmp_path):
    deck = curate(extract(_transcript(tmp_path, [[PNG, PNG]])))
    plain, _ = render_parts(deck)
    assert "[image · png · 4×3" in "".join(plain) and 'class="shots"' not in "".join(plain)
    html = "".join(render_parts(deck, lambda t, n: f"img/{t}/{n}?t=tok")[0])
    assert html.count('loading="lazy" src="img/toolu_0/') == 2
    assert 'src="img/toolu_0/1?t=tok"' in html
    assert 'class="shots"' not in render(deck)  # a written deck stays self-contained


def test_a_folded_repeat_shows_the_latest_results_images(tmp_path):
    # Two identical Read calls fold into one 2× chip; its result is the second one,
    # so its images must be too.
    f = _transcript(tmp_path, [[PNG], [PNG, PNG]])
    recs = [json.loads(x) for x in f.read_text().splitlines()]
    recs[4]["message"]["content"][0]["input"]["file_path"] = "/x/0.png"
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    tools = [
        e for s in curate(extract(f))["slides"] for e in s["elements"] if e.get("type") == "tool"
    ]
    assert len(tools) == 1 and tools[0]["count"] == 2
    assert tools[0]["tool_use_id"] == "toolu_1" and len(tools[0]["images"]) == 2


def test_the_image_route_is_behind_the_token(tmp_path):
    f = _transcript(tmp_path, [[PNG]])
    builder = live.Builder(f, lambda: curate(extract(f)), img=lambda t, n: f"img/{t}/{n}?t=tok")
    port = [0]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), live.make_handler(builder, "tok", port))
    srv.daemon_threads = True
    port[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", port[0], timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{port[0]}"})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    try:
        status, headers, body = get("/img/toolu_0/0?t=tok")
        assert status == 200 and body == PNG
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert get("/img/toolu_0/0")[0] == 403
        assert get("/img/toolu_0/5?t=tok")[0] == 404
        assert get("/img/toolu_0/x?t=tok")[0] == 404
        page = get("/?t=tok")[2].decode()
        assert 'src="img/toolu_0/0?t=tok"' in page
    finally:
        srv.shutdown()
