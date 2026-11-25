"""Render: a deck (slides) -> a self-contained HTML slideshow.

One file, no external anything (mirrors an earlier session viewer). The CSS/JS are the validated mock;
this module fills the slides from curated data. Prose goes through the safe Markdown
renderer; tool output is escaped.
"""

import html
from pathlib import Path

from .extract import split_pastes
from .markdown import render_markdown

CSS = """
:root {
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background:
    radial-gradient(1200px 600px at 78% -10%, var(--glow1) 0%, transparent 60%),
    radial-gradient(900px 500px at 0% 110%, var(--glow2) 0%, transparent 55%),
    var(--bg);
  color: var(--ink); font-family: var(--sans); -webkit-font-smoothing: antialiased; line-height: 1.6;
}
.app { display: flex; flex-direction: column; height: 100vh; }
.topbar {
  flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 13px 22px; border-bottom: 1px solid var(--line);
  background: var(--bar); backdrop-filter: blur(6px);
}
.brand { display: flex; align-items: baseline; gap: 9px; font-family: var(--mono); }
.brand .disc { color: var(--amber); font-size: 0.9rem; }
.brand .name { font-weight: 600; letter-spacing: 0.02em; color: var(--ink); }
.brand .name b { color: var(--amber); font-weight: 600; }
.brand .tag { color: var(--muted); font-size: 0.74rem; letter-spacing: 0.14em; text-transform: uppercase; }
.session { display: flex; gap: 18px; font-family: var(--mono); font-size: 0.76rem; color: var(--muted); }
.session .t { color: var(--cyan); }
@media (max-width: 760px){ .session { display: none; } }
.theme { font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.06em; color: var(--muted); background: none; border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px; margin-right: 12px; cursor: pointer; }
.theme:hover { color: var(--amber); }
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
.say .paste { margin: 0 0 12px; border: 1px solid var(--line); border-radius: 6px; padding: 4px 12px; font-style: normal; }
.say .paste > summary { cursor: pointer; font-family: var(--mono); font-size: 0.72rem; letter-spacing: 0.08em; color: var(--muted); }
.say .paste[open] > summary { margin-bottom: 8px; }
.say .paste-body { margin: 0 0 6px; white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono); font-size: 0.8rem; color: var(--ink-soft); }
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
.rail { flex: 0 0 auto; display: flex; align-items: center; justify-content: center; gap: 14px; padding: 10px max(16px, env(safe-area-inset-left)) calc(10px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); background: var(--bar); }
.reply { flex: 0 0 auto; display: flex; gap: 10px; align-items: flex-end; max-width: 730px; width: 100%; margin: 0 auto; padding: 10px 30px 12px; box-sizing: border-box; }
.reply textarea { flex: 1 1 auto; resize: none; min-height: 40px; max-height: 30vh; background: var(--surface); color: var(--ink); border: 1px solid var(--line); border-radius: 9px; padding: 9px 12px; font: inherit; font-size: 0.98rem; line-height: 1.4; }
.reply textarea:focus { outline: 1px solid var(--amber); }
.reply button { flex: 0 0 auto; height: 40px; padding: 0 16px; font-family: var(--mono); font-size: 0.76rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--amber); background: var(--surface); border: 1px solid var(--amber-d); border-radius: 9px; cursor: pointer; }
.reply button:disabled { opacity: 0.4; cursor: default; }
.reply .rstat { flex: 0 0 auto; align-self: center; font-family: var(--mono); font-size: 0.7rem; color: var(--muted); }
.reply .rstat:empty { display: none; }
.reply .rstat.err { color: var(--red); }
.reply.off { color: var(--muted); font-family: var(--mono); font-size: 0.74rem; justify-content: center; }
.nav { width: 44px; height: 44px; flex: 0 0 auto; display: flex; align-items: center; justify-content: center; font-size: 1.5rem; line-height: 1; color: var(--amber); background: var(--surface); border: 1px solid var(--line); border-radius: 11px; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.nav:hover { background: var(--surface2); }
.nav:active { transform: scale(0.93); }
.nav:disabled { opacity: 0.28; cursor: default; }
.nav:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
/* Chip density: a strip of chips can show as rows (one per line), inline (chips
   flowing side by side, label and a short argument), or compact (labels only).
   Dense strips hide chip bodies; clicking a chip steps its strip one level less dense. */
.strip.dense-inline, .strip.dense-compact { flex-direction: row; flex-wrap: wrap; gap: 6px; }
.strip.dense-inline .chip, .strip.dense-compact .chip { flex: 0 1 auto; min-width: 0; max-width: 100%; }
.strip.dense-inline .chip > summary, .strip.dense-compact .chip > summary,
.strip.dense-inline .chip.flat .row, .strip.dense-compact .chip.flat .row { padding: 4px 9px; gap: 7px; }
.strip.dense-inline .chip-body, .strip.dense-compact .chip-body, .strip.dense-inline .sub-body, .strip.dense-compact .sub-body,
.strip.dense-inline .tri, .strip.dense-compact .tri, .strip.dense-inline .meta, .strip.dense-compact .meta,
.strip.dense-inline .chip.flat .row::before, .strip.dense-compact .chip.flat .row::before { display: none; }
.strip.dense-inline .ck, .strip.dense-compact .ck { min-width: 0; }
.strip.dense-inline .arg { max-width: 24ch; }
.strip.dense-compact .arg { display: none; }
.dens { flex: 0 0 auto; height: 32px; padding: 0 10px; font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.06em; color: var(--muted); background: var(--surface); border: 1px solid var(--line); border-radius: 8px; cursor: pointer; }
.dens:hover { color: var(--amber); }
/* On a phone the rail's buttons would squeeze the progress bar away. */
@media (max-width: 520px) { .dens-k { display: none; } .rail { gap: 10px; } }
/* The top bar repeats the slide counter, so the rail's copy goes first. */
@media (max-width: 360px) { .rail .pos { display: none; } }
.track { flex: 1 1 auto; max-width: 480px; height: 4px; background: var(--line); border-radius: 100px; overflow: hidden; }
.fill { height: 100%; width: 0; background: var(--amber); transition: width 0.2s ease; }
.pos { flex: 0 0 auto; min-width: 62px; text-align: right; font-family: var(--mono); font-size: 0.78rem; color: var(--muted); font-variant-numeric: tabular-nums; }
@media (prefers-reduced-motion: reduce) { .reel { scroll-behavior: auto; } .tri, .fill { transition: none; } }
/* Context the harness injected: present, but quieter than the work itself. */
.chip.ctx { border-left: 2px dashed var(--line); background: transparent; }
.chip.ctx > summary, .chip.flat .row { color: var(--muted); font-size: 0.76rem; padding: 5px 13px; }
.chip.flat .row { display: flex; align-items: center; gap: 10px; font-family: var(--mono); }
.chip.flat .row::before { content: ""; width: 5px; flex: 0 0 auto; }
.ck.ctx { color: var(--muted); }
.chip.ctx .arg { color: var(--ink-soft); }
.chip .meta a { color: var(--cyan); text-decoration: none; }
.chip .meta a:hover { text-decoration: underline; }
.sub-body { border-top: 1px solid var(--line); padding: 12px 14px; background: var(--bg); font-size: 0.84rem; color: var(--ink-soft); max-height: 420px; overflow: auto; }
.sub-body h4 { font-family: var(--mono); font-size: 0.68rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin: 12px 0 5px; font-weight: 500; }
.sub-body h4:first-child { margin-top: 0; }
.sub-body .txt { white-space: pre-wrap; word-break: break-word; font-family: var(--mono); font-size: 0.78rem; }
.sub-body .cmd { display: flex; gap: 8px; align-items: center; }
.sub-body .cmd code { flex: 1 1 auto; font-family: var(--mono); font-size: 0.76rem; color: var(--cyan); background: var(--surface2); border: 1px solid var(--line); border-radius: 5px; padding: 4px 8px; overflow-x: auto; white-space: nowrap; }
.copy { font-family: var(--mono); font-size: 0.7rem; color: var(--amber); background: var(--surface); border: 1px solid var(--line); border-radius: 5px; padding: 4px 9px; cursor: pointer; }
.find { position: fixed; top: 58px; right: 22px; z-index: 5; display: none; align-items: center; gap: 8px; padding: 7px 10px; background: var(--surface); border: 1px solid var(--line); border-radius: 9px; box-shadow: 0 8px 24px var(--shadow); font-family: var(--mono); font-size: 0.78rem; color: var(--muted); }
.find.on { display: flex; }
.find input { width: min(260px, 50vw); background: var(--bg); color: var(--ink); border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; font: inherit; }
.find input:focus { outline: 1px solid var(--amber); }
/* Credits: the closing card, set like the end of a film. */
.credits .inner { text-align: center; }
.credits h2 { font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.22em; text-transform: uppercase; color: var(--amber); font-weight: 500; margin: 34px 0 12px; }
.credit { margin: 0 auto 14px; max-width: 600px; }
.credit .who { font-family: var(--serif); font-size: 1.12rem; color: var(--ink); }
.credit .who .sid { font-family: var(--mono); font-size: 0.82rem; color: var(--cyan); margin-right: 8px; }
.credit .how { font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.credit .what { color: var(--ink-soft); font-style: italic; font-size: 0.95rem; overflow-wrap: anywhere; }
.credit .when { font-family: var(--mono); font-size: 0.74rem; color: var(--muted); }
.credit .cmd { display: inline-flex; gap: 8px; align-items: center; margin-top: 4px; }
.credit .cmd code { font-family: var(--mono); font-size: 0.72rem; color: var(--cyan); }
.credit .beats { font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.08em; color: var(--muted); }
.credit .beats a { font-family: var(--mono); font-size: 0.74rem; color: var(--cyan); text-decoration: none; margin: 0 3px; }
.credits .fin { font-family: var(--serif); font-style: italic; color: var(--muted); margin-top: 40px; }
.castline { font-family: var(--mono); font-size: 0.8rem; color: var(--muted); margin: -14px 0 22px; }
.castline a { color: var(--cyan); text-decoration: none; }
.castline a:hover { text-decoration: underline; }
.live { font-family: var(--mono); font-size: 0.66rem; letter-spacing: 0.14em; text-transform: uppercase; margin-left: 10px; padding: 2px 7px; border-radius: 4px; border: 1px solid var(--line); color: var(--muted); }
.live.on { color: var(--amber); border-color: var(--amber-d); }
.live.on::before { content: '● '; }
.hit > summary, .hit.say, .hit .row { box-shadow: inset 2px 0 0 var(--amber); background: var(--surface2); }
"""

# Two palettes, dark (the original) and a warm light one. `auto` follows the OS;
# an explicit choice sets data-theme on <html>. Text colors clear WCAG AA (4.5:1)
# on --bg and --surface in both, and --amber-d (the user-turn rule and chip accents)
# clears the 3:1 for non-text UI; the pipeline tests check both.
PALETTES = {
    "dark": {
        "bg": "#14110d",
        "surface": "#201b15",
        "surface2": "#282017",
        "line": "#322a20",
        "ink": "#ece5d8",
        "ink-soft": "#cbc2b2",
        "muted": "#938876",
        "amber": "#e0a54e",
        "amber-d": "#826532",
        "cyan": "#63b6c2",
        "green": "#83b06a",
        "red": "#d67f6f",
        "magenta": "#c78fb6",
        "glow1": "#1d1811",
        "glow2": "#1a1610",
        "bar": "rgba(20,17,13,0.72)",
        "shadow": "rgba(0,0,0,0.4)",
    },
    "light": {
        "bg": "#f5efe3",
        "surface": "#fffaf0",
        "surface2": "#ece3d2",
        "line": "#d9cdb8",
        "ink": "#26201a",
        "ink-soft": "#463d31",
        "muted": "#6f6553",
        "amber": "#9a5c12",
        "amber-d": "#a87a38",
        "cyan": "#1d6f7c",
        "green": "#46702f",
        "red": "#a8402f",
        "magenta": "#87476f",
        "glow1": "#fbf3e2",
        "glow2": "#efe6d4",
        "bar": "rgba(245,239,227,0.82)",
        "shadow": "rgba(60,45,20,0.18)",
    },
}


def _palette(name: str) -> str:
    vars_ = "".join(f"--{k}:{v};" for k, v in PALETTES[name].items())
    return f"{vars_} color-scheme: {name};"


THEME_CSS = (
    f":root, :root[data-theme='dark'] {{ {_palette('dark')} }}\n"
    f":root[data-theme='light'] {{ {_palette('light')} }}\n"
    "@media (prefers-color-scheme: light) {"
    f" :root:not([data-theme='dark']) {{ {_palette('light')} }} }}\n"
)

# Runs in <head>, before the first paint, so a remembered theme doesn't flash.
THEME_HEAD_JS = (
    "try{var t=localStorage.getItem('claude-reel.theme');"
    "if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;}catch(e){}"
)

# `t` or the top bar's button cycles auto -> light -> dark. Its own listener, so it
# stays out of the deck's key handler.
THEME_JS = """
(function () {
  var MODES = ['auto', 'light', 'dark'], root = document.documentElement, btn = document.getElementById('theme');
  function mode(){ return root.dataset.theme || 'auto'; }
  // Auto shows what it resolves to, or one step of the cycle would look like a no-op
  // (auto and the matching explicit mode draw the same page).
  var dark = window.matchMedia('(prefers-color-scheme: dark)');
  function paint(){ var m = mode(); btn.textContent = m === 'auto' ? 'auto · ' + (dark.matches ? 'dark' : 'light') : m; }
  if (dark.addEventListener) dark.addEventListener('change', paint);
  function cycle(){
    var next = MODES[(MODES.indexOf(mode()) + 1) % MODES.length];
    if (next === 'auto') delete root.dataset.theme; else root.dataset.theme = next;
    try { if (next === 'auto') localStorage.removeItem('claude-reel.theme'); else localStorage.setItem('claude-reel.theme', next); } catch (e) {}
    paint();
  }
  btn.addEventListener('click', cycle);
  document.addEventListener('keydown', function (e){
    if (e.key !== 't' || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.closest && e.target.closest('input, textarea, #reply')) return;
    e.preventDefault(); cycle();
  });
  paint();
})();
"""

JS = """
(function () {
  var reel = document.getElementById('reel');
  var slides = Array.prototype.slice.call(reel.children);
  var curEl = document.getElementById('cur');
  var fill = document.getElementById('fill');
  var posEl = document.getElementById('pos');
  var totEl = document.getElementById('tot');
  var prev = document.getElementById('prev');
  var next = document.getElementById('next');
  var lastBtn = document.getElementById('last');
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var last = slides.length - 1;
  var idx = 0;
  function pad(n){ return n < 10 ? '0' + n : '' + n; }
  function clamp(i){ return Math.max(0, Math.min(last, i)); }
  // Glide to a neighbour; jump anywhere further (a link to the credits, a find hit),
  // since gliding past a hundred slides takes seconds and shows nothing useful.
  function go(i){ i = clamp(i); var far = Math.abs(i - idx) > 3; idx = i; reel.style.scrollBehavior = far ? 'auto' : ''; reel.scrollTo({ left: idx * reel.clientWidth, behavior: (reduce || far) ? 'auto' : 'smooth' }); paint(); }
  function fromHash(){ var m = /^#s(\\d+)$/.exec(location.hash); return m ? clamp(parseInt(m[1], 10)) : null; }
  // The hash is written once a move settles, not on every frame of a smooth scroll:
  // mid-scroll writes would leave the URL pointing at a slide passed on the way.
  var settle;
  function remember(){ clearTimeout(settle); settle = setTimeout(function (){ if (location.hash !== '#s' + idx) history.replaceState(null, '', '#s' + idx); }, 250); }
  function paint(){
    remember();
    curEl.textContent = pad(idx);
    posEl.textContent = pad(idx) + ' / ' + pad(last);
    totEl.textContent = pad(last);
    fill.style.width = (last ? (idx / last) * 100 : 100) + '%';
    prev.disabled = idx === 0;
    next.disabled = idx === last;
    lastBtn.disabled = idx === last;
  }
  prev.addEventListener('click', function(){ go(idx - 1); });
  next.addEventListener('click', function(){ go(idx + 1); });
  lastBtn.addEventListener('click', function(){ go(last); });
  var raf;
  reel.addEventListener('scroll', function () {
    if (raf) return;
    raf = requestAnimationFrame(function () { raf = null; var i = Math.round(reel.scrollLeft / reel.clientWidth); if (i !== idx) { idx = i; paint(); } });
  });
  // Delegated, so agenda rows a live update adds navigate too.
  document.addEventListener('click', function (e){ var row = e.target.closest && e.target.closest('.arow'); if (row) go(parseInt(row.dataset.goto, 10)); });
  window.addEventListener('hashchange', function () { var h = fromHash(); if (h !== null && h !== idx) go(h); });

  // Find: "/" opens; Enter / n go to the next slide whose text matches, Shift+Enter / N back.
  // Matching uses textContent, so folded chips count; matched chips open on arrival.
  var find = document.getElementById('find'), fq = document.getElementById('fq'), fc = document.getElementById('fc');
  var texts = null, query = '', hits = [];
  function search(q){
    query = q.toLowerCase();
    if (!texts) texts = slides.map(function (s){ return s.textContent.toLowerCase(); });
    hits = [];
    if (query) texts.forEach(function (t, i){ if (t.indexOf(query) >= 0) hits.push(i); });
    fc.textContent = query ? hits.length + ' slide' + (hits.length === 1 ? '' : 's') : '';
  }
  function mark(i){
    document.querySelectorAll('.hit').forEach(function (x){ x.classList.remove('hit'); });
    var first = null;
    slides[i].querySelectorAll('.chip, .say').forEach(function (el){
      if (el.textContent.toLowerCase().indexOf(query) < 0) return;
      el.classList.add('hit');
      // Any chip, not only one with a body: a flat chip's text is its argument,
      // which a dense strip hides or clips.
      if (el.classList.contains('chip')) unfoldStrip(el, 0);
      if (el.tagName === 'DETAILS') el.open = true;
      first = first || el;
    });
    if (first) first.scrollIntoView({ block: 'center' });
  }
  function step(dir){
    if (!hits.length) return;
    var j = dir > 0 ? hits.find(function (h){ return h > idx; }) : hits.slice().reverse().find(function (h){ return h < idx; });
    if (j === undefined) j = dir > 0 ? hits[0] : hits[hits.length - 1];
    go(j); mark(j);
    fc.textContent = (hits.indexOf(j) + 1) + ' / ' + hits.length;
  }
  function closeFind(){ find.classList.remove('on'); fq.blur(); }
  fq.addEventListener('input', function (){ search(fq.value); });
  fq.addEventListener('keydown', function (e){
    if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
    else if (e.key === 'Escape') { e.preventDefault(); closeFind(); }
    e.stopPropagation();
  });

  document.addEventListener('click', function (e){
    var b = e.target.closest && e.target.closest('.copy');
    if (!b) return;
    var t = b.dataset.copy;
    function done(){ b.textContent = 'copied'; setTimeout(function (){ b.textContent = 'copy'; }, 1200); }
    if (navigator.clipboard) navigator.clipboard.writeText(t).then(done, function (){ window.prompt('Copy:', t); });
    else window.prompt('Copy:', t);
  });

  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.closest && e.target.closest('#reply, textarea')) return;  // in the reply bar
    if (e.key === '/') { e.preventDefault(); find.classList.add('on'); fq.focus(); fq.select(); return; }
    if (e.key === 'n' && query) { e.preventDefault(); step(1); return; }
    if (e.key === 'N' && query) { e.preventDefault(); step(-1); return; }
    if (e.key === 'Escape') { closeFind(); return; }
    if (e.key === 'ArrowRight' || e.key === 'j' || e.key === 'l' || e.key === ']' || e.key === 'PageDown') { e.preventDefault(); go(idx + 1); }
    else if (e.key === 'ArrowLeft' || e.key === 'k' || e.key === 'h' || e.key === '[' || e.key === 'PageUp') { e.preventDefault(); go(idx - 1); }
    else if (e.key === ' ') { e.preventDefault(); go(idx + (e.shiftKey ? -1 : 1)); }
    else if (e.key === 'g' || e.key === 'Home') { e.preventDefault(); go(0); }
    // G and C need Shift: with Caps Lock on, a plain g or c arrives as G or C, and
    // would do the opposite of what was meant.
    else if ((e.key === 'G' && e.shiftKey) || e.key === 'End') { e.preventDefault(); go(last); }
    // Within the slide: $ to its end (in --live, the newest output), 0 back to the top.
    else if (e.key === '$') { e.preventDefault(); scrollSlide(true); }
    else if (e.key === '0') { e.preventDefault(); scrollSlide(false); }
    else if (e.key === 'c') { cycleDensity(1); }
    else if (e.key === 'C' && e.shiftKey) { cycleDensity(-1); }
    else if (e.key === 'e') {
      var d = slides[idx].querySelectorAll('details');
      var anyClosed = Array.prototype.some.call(d, function (x){ return !x.open; });
      // Opening everything needs rows; folding it back returns the strips to the
      // reader's density.
      slides[idx].querySelectorAll('.strip').forEach(function (st){ if (anyClosed) unfoldStrip(st, 0); else { delete st.dataset.unfold; applyStrip(st); } });
      d.forEach(function (x){ x.open = anyClosed; });
    }
  });
  window.addEventListener('resize', function () { reel.scrollLeft = idx * reel.clientWidth; });
  var start = fromHash();
  if (start !== null) {
    // Jump, don't glide: the browser has already begun its own smooth anchor scroll.
    idx = start; reel.style.scrollBehavior = 'auto'; reel.scrollLeft = idx * reel.clientWidth;
    requestAnimationFrame(function (){ reel.style.scrollBehavior = ''; });
  }
  // Chip density. The global level is the reader's (remembered across reloads); a
  // strip the reader unfolded keeps its own, lower level in data-unfold.
  var LEVELS = ['rows', 'inline', 'compact'];
  var densBtn = document.getElementById('dens'), densVal = document.getElementById('densv');
  var level = 0;
  try { level = Math.max(0, LEVELS.indexOf(localStorage.getItem('claude-reel.chips'))); } catch (e) {}
  function stripLevel(st){ var u = st.dataset.unfold; return u === undefined ? level : Math.min(level, parseInt(u, 10)); }
  function applyStrip(st){
    var l = stripLevel(st);
    st.classList.toggle('dense-inline', l === 1);
    st.classList.toggle('dense-compact', l === 2);
    // Dense chips hide their argument (compact) or clip it (inline): hover shows it.
    st.querySelectorAll('.chip').forEach(function (c){
      var arg = c.querySelector('.arg'), tip = c.querySelector('summary, .row');
      if (tip) { if (l > 0 && arg) tip.title = arg.textContent; else tip.removeAttribute('title'); }
    });
  }
  function applyAll(){
    reel.querySelectorAll('.strip').forEach(applyStrip);
    densVal.textContent = LEVELS[level];
  }
  function unfoldStrip(el, to){
    var st = el.classList && el.classList.contains('strip') ? el : el.closest && el.closest('.strip');
    if (st && stripLevel(st) > to) { st.dataset.unfold = to; applyStrip(st); }
  }
  function scrollSlide(toEnd){
    var s = slides[idx];
    s.scrollTo({ top: toEnd ? s.scrollHeight : 0, behavior: reduce ? 'auto' : 'smooth' });
  }
  function cycleDensity(dir){
    level = (level + (dir || 1) + LEVELS.length) % LEVELS.length;
    try { localStorage.setItem('claude-reel.chips', LEVELS[level]); } catch (e) {}
    reel.querySelectorAll('.strip').forEach(function (st){ delete st.dataset.unfold; });
    applyAll();
  }
  densBtn.addEventListener('click', function (){ cycleDensity(1); });
  // A click on a chip in a dense strip steps that strip one level less dense,
  // rather than opening a body the strip can't show yet.
  reel.addEventListener('click', function (e){
    var chip = e.target.closest && e.target.closest('.strip .chip');
    if (!chip) return;
    var st = chip.closest('.strip'), l = stripLevel(st);
    if (l === 0) return;
    e.preventDefault();
    unfoldStrip(st, l - 1);
  }, true);
  applyAll();
  paint();
  // The live page's hook: slides were replaced or appended, so re-read them.
  window.reelNav = {
    sync: function (){ slides = Array.prototype.slice.call(reel.children); last = slides.length - 1; texts = null; if (query) search(query); idx = clamp(idx); applyAll(); paint(); },
    go: go,
    at: function (){ return idx; },
    last: function (){ return last; }
  };
})();
"""

# Live mode: the server pushes changed slides over SSE. The page follows the tail
# only while you are on the last slide, so reading back isn't yanked forward.
LIVE_JS = """
(function () {
  var reel = document.getElementById('reel'), badge = document.getElementById('live');
  var es = new EventSource(badge.dataset.events);
  function swap(i, html){
    var t = document.createElement('template'); t.innerHTML = html.trim();
    var el = t.content.firstElementChild, old = reel.children[i];
    if (!old) { reel.appendChild(el); return; }
    // Keep what the reader opened and how far they scrolled in a slide that grew.
    var was = Array.prototype.map.call(old.querySelectorAll('details'), function (d){ return d.open; });
    el.querySelectorAll('details').forEach(function (d, j){ if (was[j] !== undefined) d.open = was[j]; });
    var unfolded = Array.prototype.map.call(old.querySelectorAll('.strip'), function (st){ return st.dataset.unfold; });
    el.querySelectorAll('.strip').forEach(function (st, j){ if (unfolded[j] !== undefined) st.dataset.unfold = unfolded[j]; });
    var top = old.scrollTop;
    reel.replaceChild(el, old);
    el.scrollTop = top;
  }
  es.onmessage = function (e){
    var d = JSON.parse(e.data), nav = window.reelNav;
    var follow = nav.at() >= nav.last();
    d.changed.forEach(function (c){ swap(c.i, c.html); });
    while (reel.children.length > d.count) reel.removeChild(reel.lastElementChild);
    document.getElementById('sess').innerHTML = d.session;
    nav.sync();
    if (follow && d.changed.length) {
      nav.go(nav.last());
      var s = reel.children[nav.last()]; s.scrollTop = s.scrollHeight;
    }
    badge.className = 'live on'; badge.textContent = 'live';
    document.dispatchEvent(new Event('reel:update'));
  };
  es.onerror = function (){ badge.className = 'live off'; badge.textContent = 'reconnecting'; };
})();
"""

# The reply bar: Enter sends, Shift+Enter is a new line. The server types the text
# into the session's terminal, so it arrives as the human's own message.
REPLY_JS = """
(function () {
  var form = document.getElementById('reply'), box = document.getElementById('rtext');
  var btn = form.querySelector('button'), stat = document.getElementById('rstat');
  function grow(){ box.style.height = 'auto'; box.style.height = box.scrollHeight + 'px'; }
  function say(t, err){ stat.textContent = t; stat.className = 'rstat' + (err ? ' err' : ''); }
  box.addEventListener('input', grow);
  box.addEventListener('keydown', function (e){
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  // A pending reply is typed into the session but not yet in its transcript: the
  // session is busy outside a model turn, or the text never took. Watch the slides
  // for it; once it shows as yours it was sent, and if it never does, say where to look.
  var PENDING_GIVE_UP = 180000;
  var pending = null;
  function key(t){ return t.toLowerCase().replace(/[^a-z0-9]+/g, ''); }
  function arrived(){
    var sel = '#reel > section:nth-last-child(-n+3) .say--user';
    return Array.prototype.some.call(document.querySelectorAll(sel), function (el){ return key(el.textContent).indexOf(pending.key) >= 0; });
  }
  document.addEventListener('reel:update', function (){
    if (pending && arrived()) { clearTimeout(pending.timer); pending = null; say('sent'); setTimeout(function (){ if (stat.textContent === 'sent') say(''); }, 2500); }
  });
  function watch(text, check){
    if (pending) clearTimeout(pending.timer);
    pending = { key: key(text).slice(0, 60) };
    pending.timer = setTimeout(function (){
      pending = null;
      say('not in the session yet after 3 min; check ' + (check || 'its terminal'), true);
    }, PENDING_GIVE_UP);
    say('typed · waiting for the session to pick it up');
  }
  form.addEventListener('submit', function (e){
    e.preventDefault();
    var text = box.value.trim();
    if (!text || btn.disabled) return;
    btn.disabled = true; say('sending…');
    fetch(form.dataset.send, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text }) })
      .then(function (r){ return r.json().then(function (j){ return { ok: r.ok, j: j }; }); })
      .then(function (x){
        if (!x.ok) throw new Error(x.j.error || 'send failed');
        box.value = ''; grow();
        window.reelNav.go(window.reelNav.last());
        if (x.j.pending) { watch(text, x.j.check); return; }
        say('sent');
        setTimeout(function (){ if (stat.textContent === 'sent') say(''); }, 2500);
      })
      .catch(function (err){ say(String(err.message || err), true); })
      .then(function (){ btn.disabled = false; });
  });
})();
"""


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "", quote=True)


def _fmt_ts(iso) -> str:
    if not iso:
        return ""
    return iso[5:16].replace("T", " ")  # MM-DD HH:MM


# Seeing the system prompt whole is the point, so the clip sits well above it: real
# prompt_snapshot text ran ~40k chars as of 2026-11-15.
CONTEXT_CLIP = 60_000


def _context_html(el, open_by_default: bool, slide_no: int = 0) -> str:
    metaparts = []
    if el.get("count", 1) > 1:
        metaparts.append(f"{el['count']}×")
    if el.get("seen") is not None:
        n = el["seen"]
        if n == slide_no:
            metaparts.append("repeated above")
        else:
            metaparts.append(f'<a href="#s{n}">unchanged since beat {n}</a>')
    meta = f'<span class="meta">{" · ".join(metaparts)}</span>' if metaparts else ""
    head = f'<span class="ck ctx">{_esc(el["label"])}</span><span class="arg">{_esc(el["arg"])}</span>{meta}'
    if not el.get("text"):
        return f'<div class="chip ctx flat"><div class="row">{head}</div></div>'
    body = el["text"]
    if len(body) > CONTEXT_CLIP:
        body = body[:CONTEXT_CLIP] + "\n… [clipped]"
    return (
        f'<details class="chip ctx"{" open" if open_by_default else ""}>'
        f'<summary><span class="tri"></span>{head}</summary>'
        f'<div class="chip-body">{_esc(body)}</div></details>'
    )


def _clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "\n… [clipped]"


def _shorthome(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home) :] if p.startswith(home + "/") else p


def _subagent_body(sub: dict, result) -> str:
    """An agent chip's body in --subagents brief: what it was asked, what it did, what
    it came back with, and the command for its own deck."""
    parts = []
    if sub.get("prompt"):
        parts.append(f'<h4>prompt</h4><div class="txt">{_esc(_clip(sub["prompt"], 1200))}</div>')
    tools = sub.get("tool_calls") or {}
    if tools:
        counts = " · ".join(f"{k} {v}" for k, v in sorted(tools.items(), key=lambda kv: -kv[1]))
        span = ""
        if sub.get("first_ts") and sub.get("last_ts"):
            span = f" — {_fmt_ts(sub['first_ts'])} → {_fmt_ts(sub['last_ts'])}"
        parts.append(f'<h4>tools</h4><div class="txt">{_esc(counts + span)}</div>')
    final = sub.get("final") or result
    if final:
        parts.append(f'<h4>result</h4><div class="txt">{_esc(_clip(final, 2400))}</div>')
    if sub.get("path"):
        cmd = f"claude-reel {_shorthome(sub['path'])} --open"
        parts.append(
            f'<h4>full deck</h4><div class="cmd"><code>{_esc(cmd)}</code>'
            f'<button class="copy" data-copy="{_esc(cmd)}">copy</button></div>'
        )
    return f'<div class="sub-body">{"".join(parts)}</div>'


def _chip_html(el, open_by_default: bool, slide_no: int = 0) -> str:
    if el["type"] == "context":
        return _context_html(el, open_by_default, slide_no)
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
        + (
            _subagent_body(el["subagent"], el.get("result"))
            if el.get("subagent")
            else f'<div class="chip-body">{_esc(body)}</div>'
        )
        + "</details>"
    )


def _user_body(text: str) -> str:
    out = []
    for is_paste, part in split_pastes(text):
        if not is_paste:
            out.append(render_markdown(part))
            continue
        # Pastes are mostly terminal output and logs, whose line breaks carry meaning
        # that Markdown would fold into one paragraph.
        # Only blank lines are trimmed: a paste's first-line indent is part of it.
        part = part.strip("\n")
        if not part.strip():
            continue
        n = len(part.splitlines())
        out.append(
            f'<details class="paste"><summary>pasted · {n} line{"s" if n != 1 else ""}</summary>'
            f'<pre class="paste-body">{_esc(part)}</pre></details>'
        )
    return "".join(out)


def _slide_html(slide, total_beats, open_chips: bool) -> str:
    beatno = slide["index"] + 1
    when = _fmt_ts(slide["ts_range"][0])
    parts = [
        f'<section class="slide" id="s{beatno}"><div class="inner">',
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
        if el["type"] in ("tool", "thinking", "context"):
            chip_buf.append(_chip_html(el, open_chips, beatno))
            continue
        flush_chips()
        who = "you" if el["type"] == "user" else "claude"
        cls = "say--user" if el["type"] == "user" else "say--claude"
        body = _user_body(el["text"]) if el["type"] == "user" else render_markdown(el["text"])
        parts.append(
            f'<div class="say {cls}"><span class="who">{who}</span>'
            f'<div class="body">{body}</div></div>'
        )
    flush_chips()

    if slide["badges"]:
        pills = "".join(
            f'<span class="badge {b["cls"]}">{_esc(b["text"])}</span>' for b in slide["badges"]
        )
        parts.append(f'<div class="badges">{pills}</div>')
    parts.append("</div></section>")
    return "".join(parts)


_KIND_WORDS = {"clear": "after /clear", "handoff": "handoff", "mention": "mentioned"}


def _kind_words(kind: str) -> str:
    return " + ".join(_KIND_WORDS.get(k, k) for k in (kind or "").split("+"))


def _session_credit(r: dict, direction: str) -> str:
    how = _kind_words(r["kind"])
    if r.get("via"):
        how += f" {r.get('direction') or 'from'} {r['via'][:8]}"
    if r["hops"] > 1:
        how += f" · {r['hops']} links {direction}"
    span = _fmt_ts(r["start"]) + (f" → {_fmt_ts(r['end'])}" if r.get("end") else "")
    where = r["where"] + (f" @ {r['branch']}" if r.get("branch") else "")
    cmd = f"claude-reel {r['sid'][:8]} --open"
    return (
        '<div class="credit">'
        f'<div class="how">{_esc(how)}</div>'
        f'<div class="who"><span class="sid">{_esc(r["sid"][:8])}</span>{_esc(where)}</div>'
        + (f'<div class="what">“{_esc(r["topic"])}”</div>' if r["topic"] else "")
        + f'<div class="when">{_esc(span)}</div>'
        f'<div class="cmd"><code>{_esc(cmd)}</code>'
        f'<button class="copy" data-copy="{_esc(cmd)}">copy</button></div>'
        "</div>"
    )


def _cast_credit(g: dict, link_beats: bool = True) -> str:
    beats = ""
    if g["slides"] and link_beats:
        links = "".join(f'<a href="#s{n}">{n}</a>' for n in g["slides"][:24])
        more = f" +{len(g['slides']) - 24}" if len(g["slides"]) > 24 else ""
        beats = f'<div class="beats">beats {links}{_esc(more)}</div>'
    role = g["role"] or "(no description)"
    return (
        '<div class="credit">'
        f'<div class="how">{_esc(g["type"])}{" ×" + str(g["count"]) if g["count"] > 1 else ""}</div>'
        f'<div class="who">{_esc(role)}</div>{beats}</div>'
    )


def _compactions_line(comps: list) -> str:
    def one(c):
        t = _fmt_ts(c.get("timestamp"))
        b, a = c.get("tokens_before"), c.get("tokens_after")
        size = f" {round(b / 1000)}k → {round(a / 1000)}k" if b and a else ""
        return f"{c.get('trigger') or 'compact'} {t}{size}"

    return "<br>".join(_esc(one(c)) for c in comps)


def credits_count(credits: dict | None) -> tuple[int, int]:
    """(subagents, related sessions) the credits card lists."""
    if not credits:
        return 0, 0
    fam = credits.get("family") or {}
    sessions = sum(len(fam.get(k) or []) for k in ("previously", "sequels", "related"))
    return sum(g["count"] for g in credits.get("cast") or []), sessions


def _credits_html(deck, slide_no: int) -> str:
    # A tldr deck renders no beat slides, so links to them would all land here.
    link_beats = deck.get("density") != "tldr"
    c = deck.get("credits") or {}
    fam = c.get("family") or {}
    parts = [
        f'<section class="slide credits" id="s{slide_no}"><div class="inner">',
        '<div class="eyebrow" style="justify-content:center">credits</div>',
        '<h1 class="title">The cast &amp; the family</h1>',
    ]
    if fam.get("previously"):
        parts.append("<h2>Previously</h2>")
        parts += [_session_credit(r, "back") for r in reversed(fam["previously"])]
    if c.get("cast"):
        parts.append("<h2>Featuring</h2>")
        parts += [_cast_credit(g, link_beats) for g in c["cast"]]
    if fam.get("compactions"):
        n = len(fam["compactions"])
        parts.append(f"<h2>Compacted {n}×</h2>")
        parts.append(
            f'<div class="credit"><div class="when">{_compactions_line(fam["compactions"])}</div></div>'
        )
    if fam.get("sequels"):
        parts.append("<h2>Sequels</h2>")
        parts += [_session_credit(r, "on") for r in fam["sequels"]]
    if fam.get("related"):
        parts.append("<h2>Also in the family</h2>")
        parts += [_session_credit(r, "") for r in fam["related"]]
    if not c.get("lineage"):
        parts.append(
            '<p class="fin">Sessions before and after this one need claude-ls on PATH.</p>'
        )
    parts.append('<p class="fin">fin.</p></div></section>')
    return "".join(parts)


def _castline_html(deck, credits_slide: int | None) -> str:
    agents, sessions = credits_count(deck.get("credits"))
    if credits_slide is None or not (agents or sessions):
        return ""
    bits = []
    if agents:
        bits.append(f"{agents} subagent{'s' if agents != 1 else ''}")
    if sessions:
        bits.append(f"{sessions} related session{'s' if sessions != 1 else ''}")
    return f'<p class="castline">{" · ".join(bits)} <a href="#s{credits_slide}">→ credits</a></p>'


def _agenda_html(deck, credits_slide: int | None = None) -> str:
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
        '<section class="slide" id="s0"><div class="inner">'
        '<div class="eyebrow">while you were away</div>'
        '<div class="agenda-head"><h1 class="title">Session recap</h1>'
        f'<p class="sub">{sub}</p></div>'
        f"{_castline_html(deck, credits_slide)}"
        f'<div class="agenda-list">{"".join(rows)}</div>'
        "</div></section>"
    )


def render_parts(deck: dict) -> tuple[list, str]:
    """(every slide's HTML, the topbar's session line). The live server diffs these
    between builds and sends only the slides that changed."""
    meta = deck["meta"]
    total = len(deck["slides"])
    open_chips = deck.get("density") == "firehose"
    span = ""
    if meta.get("first_ts"):
        span = _fmt_ts(meta["first_ts"]) + (
            f" → {_fmt_ts(meta['last_ts'])}" if meta.get("last_ts") else ""
        )

    body = (
        [_slide_html(s, total, open_chips) for s in deck["slides"]]
        if deck.get("density") != "tldr"
        else []
    )
    agents, sessions = credits_count(deck.get("credits"))
    credits_slide = len(body) + 1 if (agents or sessions) else None
    slides_html = [_agenda_html(deck, credits_slide), *body]
    if credits_slide is not None:
        slides_html.append(_credits_html(deck, credits_slide))

    session_meta = (
        f"<span>{_esc(meta.get('project') or 'session')}"
        + (f" · {_esc(meta['branch'])}" if meta.get("branch") else "")
        + "</span>"
        f"<span>{total} beat{'s' if total != 1 else ''}</span>"
        + (f'<span class="t">{_esc(span)}</span>' if span else "")
    )
    return slides_html, session_meta


def _reply_html(live_send, reply_note) -> str:
    if live_send:
        return (
            f'<form class="reply" id="reply" data-send="{_esc(live_send)}">'
            '<textarea id="rtext" rows="1" aria-label="Reply to Claude"'
            ' placeholder="Reply to Claude · Enter sends, Shift+Enter for a new line"></textarea>'
            '<span class="rstat" id="rstat" role="status"></span>'
            "<button>send</button></form>"
        )
    if reply_note:
        return f'<div class="reply off">{_esc(reply_note)}</div>'
    return ""


def render(
    deck: dict,
    live_events: str | None = None,
    live_send: str | None = None,
    reply_note: str | None = None,
) -> str:
    """The whole deck. With `live_events` (the live server's stream URL, relative to
    the page), the page subscribes to it and applies the slides it sends. With
    `live_send` too, a reply bar posts there; `reply_note` says why there is none."""
    meta = deck["meta"]
    slides_html, session_meta = render_parts(deck)
    counter_total = len(slides_html) - 1  # excludes the agenda
    live = live_events is not None
    badge = (
        f'<span class="live" id="live" data-events="{_esc(live_events)}">connecting</span>'
        if live
        else ""
    )
    scripts = (
        f"<script>{JS}</script><script>{THEME_JS}</script>"
        + (f"<script>{LIVE_JS}</script>" if live else "")
        + (f"<script>{REPLY_JS}</script>" if live and live_send else "")
    )
    reply = _reply_html(live_send, reply_note) if live else ""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-reel — {_esc(meta.get("project") or "recap")}</title>
<script>{THEME_HEAD_JS}</script>
<style>{THEME_CSS}{CSS}</style>
</head><body>
<div class="app">
  <div class="topbar">
    <div class="brand"><span class="disc">◉</span><span class="name">claude<b>·</b>reel</span><span class="tag">recap</span>{badge}</div>
    <div class="session" id="sess">{session_meta}</div>
    <div><button class="theme" id="theme" title="Theme: auto, light, dark (t)">auto</button><span class="counter"><b id="cur">00</b> / <span id="tot">{counter_total:02d}</span></span></div>
  </div>
  <div class="reel" id="reel">
    {"".join(slides_html)}
  </div>
  {reply}
  <div class="rail">
    <button class="dens" id="dens" title="Chip density: rows, inline, compact (c, C back)"><span class="dens-k">chips: </span><span id="densv">rows</span></button>
    <button class="nav" id="prev" aria-label="Previous slide" title="Previous (← k h [)">‹</button>
    <div class="track"><div class="fill" id="fill"></div></div>
    <span class="pos" id="pos">00 / {counter_total:02d}</span>
    <button class="nav" id="next" aria-label="Next slide" title="Next (→ j l ])">›</button>
    <button class="nav" id="last" aria-label="Last slide" title="Last (End G); $ scrolls to the slide's end">»</button>
  </div>
</div>
<div class="find" id="find"><span>/</span><input id="fq" type="search" placeholder="find in session" aria-label="Find in session"><span id="fc"></span></div>
{scripts}
</body></html>"""
