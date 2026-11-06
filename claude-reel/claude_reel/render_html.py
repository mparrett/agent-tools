"""Render: a deck (slides) -> a self-contained HTML slideshow.

One file, no external anything (mirrors an earlier session viewer). The CSS/JS are the validated mock;
this module fills the slides from curated data. Prose goes through the safe Markdown
renderer; tool output is escaped.
"""

import html

from .markdown import render_markdown

CSS = """
:root {
  --bg:#14110d; --surface:#201b15; --surface2:#282017; --line:#322a20;
  --ink:#ece5d8; --ink-soft:#cbc2b2; --muted:#938876;
  --amber:#e0a54e; --amber-d:#7a5f2f; --cyan:#63b6c2;
  --green:#83b06a; --red:#d67f6f; --magenta:#c78fb6;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background:
    radial-gradient(1200px 600px at 78% -10%, #1d1811 0%, rgba(29,24,17,0) 60%),
    radial-gradient(900px 500px at 0% 110%, #1a1610 0%, rgba(26,22,16,0) 55%),
    var(--bg);
  color: var(--ink); font-family: var(--sans); -webkit-font-smoothing: antialiased; line-height: 1.6;
}
.app { display: flex; flex-direction: column; height: 100vh; }
.topbar {
  flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 13px 22px; border-bottom: 1px solid var(--line);
  background: rgba(20,17,13,0.72); backdrop-filter: blur(6px);
}
.brand { display: flex; align-items: baseline; gap: 9px; font-family: var(--mono); }
.brand .disc { color: var(--amber); font-size: 0.9rem; }
.brand .name { font-weight: 600; letter-spacing: 0.02em; color: var(--ink); }
.brand .name b { color: var(--amber); font-weight: 600; }
.brand .tag { color: var(--muted); font-size: 0.74rem; letter-spacing: 0.14em; text-transform: uppercase; }
.session { display: flex; gap: 18px; font-family: var(--mono); font-size: 0.76rem; color: var(--muted); }
.session .t { color: var(--cyan); }
@media (max-width: 760px){ .session { display: none; } }
.counter { font-family: var(--mono); font-size: 0.8rem; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.counter b { color: var(--amber); }
.reel {
  flex: 1 1 auto; min-height: 0; display: flex; overflow-x: auto; overflow-y: hidden;
  scroll-snap-type: x mandatory; scroll-behavior: smooth; scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
}
.reel::-webkit-scrollbar { display: none; }
.slide {
  flex: 0 0 100%; width: 100%; height: 100%; overflow-y: auto; scroll-snap-align: start;
  scrollbar-width: thin; scrollbar-color: var(--line) transparent;
}
.inner { max-width: 730px; margin: 0 auto; padding: 6.5vh 30px 14vh; }
.eyebrow {
  font-family: var(--mono); font-size: 0.73rem; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--cyan); margin-bottom: 16px; display: flex; align-items: center; gap: 10px;
}
.eyebrow .beatno { color: var(--amber); }
.eyebrow .dot { color: var(--amber-d); }
h1.title {
  font-family: var(--serif); font-weight: 600; font-size: clamp(1.6rem, 1.15rem + 1.6vw, 2.5rem);
  line-height: 1.14; letter-spacing: -0.01em; text-wrap: balance; overflow-wrap: anywhere;
  margin: 0 0 22px; color: var(--ink);
}
.say { margin: 0 0 16px; }
.say .who { font-family: var(--mono); font-size: 0.68rem; letter-spacing: 0.15em; text-transform: uppercase; display: inline-block; margin-bottom: 5px; }
.say--user { border-left: 2px solid var(--amber-d); padding: 3px 0 3px 16px; }
.say--user .who { color: var(--amber); }
.say--user .body { color: var(--ink-soft); font-style: italic; }
.say--claude .who { color: var(--muted); }
.say .body { font-size: 1.02rem; overflow-wrap: anywhere; }
.say .body > :first-child { margin-top: 0; }
.say .body > :last-child { margin-bottom: 0; }
.say .body p { margin: 0 0 12px; }
.say .body h3, .say .body h4 { font-family: var(--serif); color: var(--ink); margin: 18px 0 8px; }
.say .body ul, .say .body ol { margin: 0 0 12px; padding-left: 22px; }
.say .body li { margin: 3px 0; }
.say .body strong { color: var(--amber); font-weight: 600; }
.say .body em { color: var(--ink); }
.say .body a { color: var(--cyan); }
.say .body blockquote { border-left: 2px solid var(--line); margin: 0 0 12px; padding-left: 14px; color: var(--muted); }
.say .body code { font-family: var(--mono); font-size: 0.86em; background: var(--surface2); color: var(--cyan); padding: 1px 6px; border-radius: 4px; border: 1px solid var(--line); }
.say .body pre.code { margin: 12px 0; }
.strip { display: flex; flex-direction: column; gap: 7px; margin: 20px 0 6px; }
.chip { border: 1px solid var(--line); border-left: 2px solid var(--amber-d); border-radius: 7px; background: var(--surface); overflow: hidden; }
.chip.err { border-left-color: var(--red); }
.chip > summary { list-style: none; cursor: pointer; display: flex; align-items: center; gap: 10px; padding: 8px 13px; font-family: var(--mono); font-size: 0.82rem; color: var(--ink-soft); user-select: none; }
.chip > summary::-webkit-details-marker { display: none; }
.chip > summary:hover { background: var(--surface2); }
.tri { width: 0; height: 0; flex: 0 0 auto; border-left: 5px solid var(--muted); border-top: 4px solid transparent; border-bottom: 4px solid transparent; transition: transform 0.16s ease; }
.chip[open] > summary .tri { transform: rotate(90deg); }
.ck { font-size: 0.7rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--amber); flex: 0 0 auto; min-width: 40px; }
.ck.run { color: var(--cyan); } .ck.read { color: var(--muted); } .ck.think { color: var(--magenta); } .ck.agent { color: var(--green); }
.chip .arg { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip .meta { color: var(--muted); margin-left: auto; padding-left: 12px; white-space: nowrap; }
.chip .meta.repeat { color: var(--amber); }
.chip-body { border-top: 1px solid var(--line); padding: 12px 14px; background: var(--bg); font-family: var(--mono); font-size: 0.8rem; color: var(--ink-soft); white-space: pre-wrap; word-break: break-word; max-height: 340px; overflow: auto; }
.code { font-family: var(--mono); font-size: 0.8rem; line-height: 1.65; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 13px 15px; overflow-x: auto; white-space: pre; color: var(--ink-soft); }
.badges { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
.badge { font-family: var(--mono); font-size: 0.72rem; padding: 3px 10px; border-radius: 100px; border: 1px solid var(--line); color: var(--muted); background: var(--surface); }
.badge.ok { color: var(--green); } .badge.err { color: var(--red); }
.agenda-head { margin-bottom: 30px; }
.agenda-head .sub { color: var(--muted); font-size: 1.02rem; max-width: 54ch; }
.agenda-list { display: flex; flex-direction: column; gap: 2px; }
.arow { display: flex; align-items: center; gap: 15px; width: 100%; background: none; border: none; border-radius: 8px; padding: 11px 12px; cursor: pointer; text-align: left; color: var(--ink); font: inherit; border-bottom: 1px solid var(--line); }
.arow:last-child { border-bottom: none; }
.arow:hover { background: var(--surface); }
.arow:focus-visible { outline: 2px solid var(--amber); outline-offset: 1px; }
.arow .n { font-family: var(--mono); color: var(--amber); font-size: 0.82rem; width: 24px; flex: 0 0 auto; font-variant-numeric: tabular-nums; }
.arow .lbl2 { flex: 1 1 auto; font-size: 1.02rem; }
.arow .bd { font-family: var(--mono); font-size: 0.7rem; color: var(--muted); white-space: nowrap; }
.arow .bd.ok { color: var(--green); } .arow .bd.err { color: var(--red); }
.kicker { color: var(--muted); font-style: italic; font-family: var(--serif); font-size: 1.02rem; margin-top: 22px; }
/* A progress bar + prev/next scales to any slide count (dots-per-slide don't) and gives
   real tap targets on touch. Horizontal swipe still pages via the reel's scroll-snap. */
.rail { flex: 0 0 auto; display: flex; align-items: center; justify-content: center; gap: 14px; padding: 10px max(16px, env(safe-area-inset-left)) calc(10px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); background: rgba(20,17,13,0.72); }
.nav { width: 44px; height: 44px; flex: 0 0 auto; display: flex; align-items: center; justify-content: center; font-size: 1.5rem; line-height: 1; color: var(--amber); background: var(--surface); border: 1px solid var(--line); border-radius: 11px; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.nav:hover { background: var(--surface2); }
.nav:active { transform: scale(0.93); }
.nav:disabled { opacity: 0.28; cursor: default; }
.nav:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
.track { flex: 1 1 auto; max-width: 480px; height: 4px; background: var(--line); border-radius: 100px; overflow: hidden; }
.fill { height: 100%; width: 0; background: var(--amber); transition: width 0.2s ease; }
.pos { flex: 0 0 auto; min-width: 62px; text-align: right; font-family: var(--mono); font-size: 0.78rem; color: var(--muted); font-variant-numeric: tabular-nums; }
@media (prefers-reduced-motion: reduce) { .reel { scroll-behavior: auto; } .tri, .fill { transition: none; } }
"""

JS = """
(function () {
  var reel = document.getElementById('reel');
  var slides = Array.prototype.slice.call(reel.children);
  var curEl = document.getElementById('cur');
  var fill = document.getElementById('fill');
  var posEl = document.getElementById('pos');
  var prev = document.getElementById('prev');
  var next = document.getElementById('next');
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var last = slides.length - 1;
  var idx = 0;
  function pad(n){ return ('0' + n).slice(-2); }
  function clamp(i){ return Math.max(0, Math.min(last, i)); }
  function go(i){ idx = clamp(i); reel.scrollTo({ left: idx * reel.clientWidth, behavior: reduce ? 'auto' : 'smooth' }); paint(); }
  function paint(){
    curEl.textContent = pad(idx);
    posEl.textContent = pad(idx) + ' / ' + pad(last);
    fill.style.width = (last ? (idx / last) * 100 : 100) + '%';
    prev.disabled = idx === 0;
    next.disabled = idx === last;
  }
  prev.addEventListener('click', function(){ go(idx - 1); });
  next.addEventListener('click', function(){ go(idx + 1); });
  var raf;
  reel.addEventListener('scroll', function () {
    if (raf) return;
    raf = requestAnimationFrame(function () { raf = null; var i = Math.round(reel.scrollLeft / reel.clientWidth); if (i !== idx) { idx = i; paint(); } });
  });
  document.querySelectorAll('.arow').forEach(function (row) { row.addEventListener('click', function () { go(parseInt(row.dataset.goto, 10)); }); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowRight' || e.key === 'j' || e.key === 'PageDown') { e.preventDefault(); go(idx + 1); }
    else if (e.key === 'ArrowLeft' || e.key === 'k' || e.key === 'PageUp') { e.preventDefault(); go(idx - 1); }
    else if (e.key === ' ') { e.preventDefault(); go(idx + (e.shiftKey ? -1 : 1)); }
    else if (e.key === 'g' || e.key === 'Home') { e.preventDefault(); go(0); }
    else if (e.key === 'End') { e.preventDefault(); go(last); }
    else if (e.key === 'e') { var d = slides[idx].querySelectorAll('details'); var anyClosed = Array.prototype.some.call(d, function (x){ return !x.open; }); d.forEach(function (x){ x.open = anyClosed; }); }
  });
  window.addEventListener('resize', function () { reel.scrollLeft = idx * reel.clientWidth; });
  paint();
})();
"""


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "", quote=True)


def _fmt_ts(iso) -> str:
    if not iso:
        return ""
    return iso[5:16].replace("T", " ")  # MM-DD HH:MM


def _chip_html(el, open_by_default: bool) -> str:
    if el["type"] == "thinking":
        label, arg, stat, count, err = "think", "reasoning", None, 1, False
        body = el.get("text") or ""
    else:
        label = el["label"]
        arg = el["arg"]
        stat = el.get("stat")
        count = el.get("count", 1)
        err = el.get("is_error", False)
        body = el.get("result") or "(no recorded output)"

    body = body if len(body) <= 1600 else body[:1600] + "\n… [clipped]"
    ck_cls = label if label in ("run", "read", "think", "agent") else ""
    metaparts = []
    if count > 1:
        metaparts.append(f"{count}×")
    if stat:
        metaparts.append(stat)
    meta_html = ""
    if metaparts:
        cls = "meta repeat" if count > 1 else "meta"
        meta_html = f'<span class="{cls}">{_esc(" · ".join(metaparts))}</span>'
    return (
        f'<details class="chip{" err" if err else ""}"{" open" if open_by_default else ""}>'
        f'<summary><span class="tri"></span>'
        f'<span class="ck {ck_cls}">{_esc(label)}</span>'
        f'<span class="arg">{_esc(arg)}</span>{meta_html}</summary>'
        f'<div class="chip-body">{_esc(body)}</div></details>'
    )


def _slide_html(slide, total_beats, open_chips: bool) -> str:
    beatno = slide["index"] + 1
    when = _fmt_ts(slide["ts_range"][0])
    parts = [
        '<section class="slide"><div class="inner">',
        f'<div class="eyebrow"><span class="beatno">beat {beatno}</span>'
        + (f'<span class="dot">·</span> {_esc(when)}' if when else "")
        + "</div>",
        f'<h1 class="title">{_esc(slide["title"])}</h1>',
    ]
    chip_buf = []

    def flush_chips():
        if chip_buf:
            parts.append('<div class="strip">' + "".join(chip_buf) + "</div>")
            chip_buf.clear()

    for el in slide["elements"]:
        if el["type"] in ("tool", "thinking"):
            chip_buf.append(_chip_html(el, open_chips))
            continue
        flush_chips()
        who = "you" if el["type"] == "user" else "claude"
        cls = "say--user" if el["type"] == "user" else "say--claude"
        parts.append(
            f'<div class="say {cls}"><span class="who">{who}</span>'
            f'<div class="body">{render_markdown(el["text"])}</div></div>'
        )
    flush_chips()

    if slide["badges"]:
        pills = "".join(
            f'<span class="badge {b["cls"]}">{_esc(b["text"])}</span>' for b in slide["badges"]
        )
        parts.append(f'<div class="badges">{pills}</div>')
    parts.append("</div></section>")
    return "".join(parts)


def _agenda_html(deck) -> str:
    meta = deck["meta"]
    n = len(deck["slides"])
    sub = (
        f"{n} beat{'s' if n != 1 else ''} in {_esc(meta.get('project') or 'this session')}"
        + (f" · {_esc(meta['model'])}" if meta.get("model") else "")
        + " · press → or space to move."
    )
    rows = []
    for a in deck["agenda"]:
        badge = a["badge"]
        bd = (
            f'<span class="bd {badge["cls"]}">{_esc(badge["text"])}</span>'
            if badge
            else '<span class="bd"></span>'
        )
        rows.append(
            f'<button class="arow" data-goto="{a["n"] + 1}">'
            f'<span class="n">{a["n"] + 1}</span>'
            f'<span class="lbl2">{_esc(a["title"])}</span>{bd}</button>'
        )
    return (
        '<section class="slide"><div class="inner">'
        '<div class="eyebrow">while you were away</div>'
        '<div class="agenda-head"><h1 class="title">Session recap</h1>'
        f'<p class="sub">{sub}</p></div>'
        f'<div class="agenda-list">{"".join(rows)}</div>'
        "</div></section>"
    )


def render(deck: dict) -> str:
    meta = deck["meta"]
    total = len(deck["slides"])
    open_chips = deck.get("density") == "firehose"
    span = ""
    if meta.get("first_ts"):
        span = _fmt_ts(meta["first_ts"]) + (
            f" → {_fmt_ts(meta['last_ts'])}" if meta.get("last_ts") else ""
        )

    slides_html = [_agenda_html(deck)]
    if deck.get("density") != "tldr":
        slides_html += [_slide_html(s, total, open_chips) for s in deck["slides"]]

    session_meta = (
        f"<span>{_esc(meta.get('project') or 'session')}"
        + (f" · {_esc(meta['branch'])}" if meta.get("branch") else "")
        + "</span>"
        f"<span>{total} beat{'s' if total != 1 else ''}</span>"
        + (f'<span class="t">{_esc(span)}</span>' if span else "")
    )
    counter_total = len(slides_html) - 1  # excludes the agenda

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-reel — {_esc(meta.get("project") or "recap")}</title>
<style>{CSS}</style>
</head><body>
<div class="app">
  <div class="topbar">
    <div class="brand"><span class="disc">◉</span><span class="name">claude<b>·</b>reel</span><span class="tag">recap</span></div>
    <div class="session">{session_meta}</div>
    <div class="counter"><b id="cur">00</b> / {counter_total:02d}</div>
  </div>
  <div class="reel" id="reel">
    {"".join(slides_html)}
  </div>
  <div class="rail">
    <button class="nav" id="prev" aria-label="Previous slide" title="Previous (←)">‹</button>
    <div class="track"><div class="fill" id="fill"></div></div>
    <span class="pos" id="pos">00 / {counter_total:02d}</span>
    <button class="nav" id="next" aria-label="Next slide" title="Next (→)">›</button>
  </div>
</div>
<script>{JS}</script>
</body></html>"""
