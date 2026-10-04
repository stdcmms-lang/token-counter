"""Self-contained HTML report.

No CDN, no network, no external fonts: the file is opened from disk and must render with
the machine offline.  Charts are inline SVG; interaction is a few hundred lines of vanilla
JS over an embedded JSON blob.  Under Clinical the chart marks are repainted by a small
WebGL2 layer (GL_JS) whose last pass draws them through a simulated water surface; the SVG
keeps the axes, text and tooltips, and keeps the marks too wherever WebGL2 is missing.

The page ships its styles over one markup (STYLES), cycled by a button in the top bar or the
`[` / `]` keys and remembered per browser.  A style never changes what the charts draw, only
the colours, type and paper they are drawn on -- even Nocturne, which is a 3D scene (SCENE_JS)
built from the very marks the charts record, with the page kept beneath it, unpainted.

The three charts share **one time axis and one viewport**.  The limit chart and the daily
chart are drawn over the same domain with the same margins, so a moment sits at the same x
in both and they can be read against each other; zooming or dragging either one moves both,
and recomposes the category pie over whatever range is in view.  Zoom is horizontal only --
a chart's value axis never changes, so heights stay comparable at every zoom level.

Every figure derived from inference rather than measurement carries a visible marker
(ARCHITECTURE.md section 7).
"""
import datetime
import html
import json
import math
import os
import random
import time

# One geometry for every time chart.  The page re-derives the width from the panel at run
# time (one viewBox unit = one CSS pixel, so axis text is legible on a phone); these are the
# no-JS fallback values, and the proportions the margins are capped at.
CHART_W, CHART_L, CHART_R = 980, 62, 48
RL_H, RL_T, RL_B = 300, 18, 34
DAILY_H, DAILY_T, DAILY_B = 210, 18, 34

# The page styles, in the order the button cycles them; the first is the default.  The page
# reads this list from its payload, so it is written down once.
STYLES = [('clinical', 'Clinical'), ('matisse', 'Matisse'), ('nocturne', 'Nocturne')]

CSS = """
:root{
  --bg:#ffffff; --panel:#f7f8fa; --line:#e3e6ea; --fg:#14171a; --dim:#5b6570;
  --cached:#93b4f5; --uncached:#2563eb; --out:#10b981;
  --warn:#b45309; --warn-bg:#fef3c7;
  --c0:#2563eb;--c1:#7c3aed;--c2:#db2777;--c3:#ea580c;--c4:#ca8a04;--c5:#16a34a;
  --c6:#0891b2;--c7:#4f46e5;--c8:#9333ea;--c9:#e11d48;--c10:#65a30d;--c11:#0d9488;
  --c12:#a16207;--c13:#475569;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0f1115; --panel:#171a21; --line:#272b33; --fg:#e6e9ee; --dim:#9aa4b2;
  --cached:#2b4270; --uncached:#60a5fa; --out:#34d399;
  --warn:#fbbf24; --warn-bg:#3b2f10;
  --c0:#60a5fa;--c1:#a78bfa;--c2:#f472b6;--c3:#fb923c;--c4:#fbbf24;--c5:#4ade80;
  --c6:#22d3ee;--c7:#818cf8;--c8:#c084fc;--c9:#fb7185;--c10:#a3e635;--c11:#2dd4bf;
  --c12:#d6b45b;--c13:#94a3b8;
}}
:root[data-theme="dark"]{
  --bg:#0f1115; --panel:#171a21; --line:#272b33; --fg:#e6e9ee; --dim:#9aa4b2;
  --cached:#2b4270; --uncached:#60a5fa; --out:#34d399;
  --warn:#fbbf24; --warn-bg:#3b2f10;
  --c0:#60a5fa;--c1:#a78bfa;--c2:#f472b6;--c3:#fb923c;--c4:#fbbf24;--c5:#4ade80;
  --c6:#22d3ee;--c7:#818cf8;--c8:#c084fc;--c9:#fb7185;--c10:#a3e635;--c11:#2dd4bf;
  --c12:#d6b45b;--c13:#94a3b8;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 80px}
.sub{color:var(--dim);font-size:13px;margin:0 0 6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px;margin-top:18px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.tile .v{font-size:23px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.tile .n{color:var(--dim);font-size:12px;margin-top:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-top:12px}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 2px;font-size:12px;color:var(--dim)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.legend b{color:var(--fg);font-variant-numeric:tabular-nums}
.legend [data-i]{transition:opacity .12s,color .12s}
.hi .legend [data-i]{opacity:.35}
.hi .legend [data-i].on{opacity:1;color:var(--fg);font-weight:600}
svg{display:block;width:100%;height:auto;overflow:visible}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
/* pan-y, not none: a vertical swipe still scrolls the page on a phone, while a horizontal
   drag and a two-finger pinch reach the chart instead of the browser. */
.chart{touch-action:pan-y;-webkit-user-select:none;user-select:none;
  -webkit-tap-highlight-color:transparent}
.chart.inplot{cursor:grab}
.chart.drag{cursor:grabbing}
.pie{flex:0 0 auto;width:240px;max-width:100%}
.pies{display:flex;flex-wrap:wrap;gap:24px 48px}
.pies>div{flex:1 1 380px;min-width:0}
@media(max-width:640px){.wrap{padding:18px 12px 60px} .tile .v{font-size:19px}}
"""

# The page's styles.  Every style is CSS over the same markup, keyed on `html[data-style]`,
# and every chart colour is a variable, so a style restyles the charts without redrawing
# them.  Nothing here is fetched: fonts are whatever the machine has, and every stack ends in
# a generic family.  Matisse's categorical palette (--c0..--c12, with --c13 a neutral for
# `other`) is checked for colour-vision separation against its own panel: neighbouring slots,
# and every pair among the first four, which is as many models as most corpora have.
# Clinical's predates that check and does not pass it (--c0 and --c1 converge under
# deuteranopia).
STYLE_CSS = r"""
/* ---- shared chrome: the style bar, the masthead, the switch ---------------------------- */
:root{--kicker:"Codex usage, recounted locally"}
.bar{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;
  gap:12px;padding:10px 16px;background:var(--bg);border-bottom:1px solid var(--line)}
.brand{font-weight:700;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brand a{color:inherit;text-decoration:none}
/* The style switch is one dot, drawn in the style it switches *to* -- so its colours are
   fixed here, not read from the page's variables, which belong to the style on screen. */
#stylebtn{display:grid;place-items:center;width:36px;height:36px;padding:0;border:0;
  background:none;cursor:pointer;border-radius:50%;-webkit-tap-highlight-color:transparent}
#stylebtn:focus-visible{outline:2px solid var(--uncached);outline-offset:2px}
.sdot{display:block;width:18px;height:18px;border-radius:50%;background:#8a8f98;
  transition:transform .15s ease}
#stylebtn:hover .sdot{transform:scale(1.15)}
/* Clinical: a crisp system-blue disc on a white panel ring with a hairline. */
#stylebtn[data-next="clinical"] .sdot{background:#2563eb;box-shadow:0 0 0 3px #fff,0 0 0 4px #c9ced6}
/* Matisse: a cobalt gouache cut-out, pinned slightly out of register over a sage sheet. */
#stylebtn[data-next="matisse"] .sdot{width:20px;height:19px;background:#394ca3;
  border-radius:60% 40% 55% 45%/55% 60% 40% 45%;box-shadow:3px 3px 0 #c9d4d2;transform:rotate(-8deg)}
#stylebtn[data-next="matisse"]:hover .sdot{transform:rotate(-8deg) scale(1.15)}
/* Nocturne: a gold lamp on night water -- a lit sphere, not a disc, for the one style in 3D. */
#stylebtn[data-next="nocturne"] .sdot{
  background:radial-gradient(circle at 34% 30%,#fff1c9 0 9%,#e2ae4c 34%,#8a5a16 74%,#4b300a);
  box-shadow:0 0 0 3px #0b1720,0 0 9px 3px rgba(226,178,79,.55)}
.mast{position:relative;padding:26px 0 6px}
.kicker{color:var(--dim);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
.kicker::before{content:var(--kicker)}
.mast h1{margin:4px 0 6px;font-size:30px;line-height:1.05;letter-spacing:-.01em}
.dek{margin:0;color:var(--dim)}
.deco>*{display:none}
/* The switch fades through the page background rather than cutting. */
.wipe{position:fixed;inset:0;z-index:60;pointer-events:none;background:var(--bg)}
.wipe.go{display:block;animation:wipe .56s ease-in-out forwards}
@keyframes wipe{0%{opacity:0}45%,55%{opacity:1}100%{opacity:0}}
@media(max-width:640px){.brand{font-size:13px}}

/* ---- the WebGL layer: marks painted on a canvas behind each panel's content ----------- */
.glc{display:none;position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
[data-gl] .glc{display:block}
[data-gl] .panel{position:relative}
[data-gl] .panel>:not(.glc){position:relative}
/* The SVG marks stay in place, transparent, so their tooltips still answer the pointer. */
[data-gl] .mk{fill-opacity:0!important;stroke-opacity:0!important}
/* The headline numbers, repainted into the GL layer so the water reaches them: the canvas
   sits over the tiles, and the text under it keeps its place (and stays selectable and
   readable to assistive tech) but is not painted. */
.glt{display:none;position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:1}
[data-gl] .tiles{position:relative}
[data-gl] .glt{display:block}
[data-gl] .tiles.gltxt .tile>*{opacity:0}

/* ---- 1. CLINICAL: the base sheet above, light or dark with the system ----------------- */

/* ---- 2. MATISSE: papiers découpés -- gouache paper, cut with scissors, pinned up ------ */
/* Cream paper, a sage and a dusty-rose sheet torn behind the page, white brush dashes and an
   ink flower cut in one piece.  Gouache is matte, so nothing here is glossy: fills are flat,
   and the only depth is a second coloured sheet showing under the edge of a panel. */
:root[data-style="matisse"]{color-scheme:light;
  --bg:#f3efe6;--panel:#faf7f0;--line:#e0d8c8;--fg:#23252f;--dim:#6a655d;
  --cached:#c9d4d2;--uncached:#2f3a63;--out:#139688;--warn:#b4533e;--warn-bg:#f1dcd4;
  --c0:#394ca3;--c1:#bb5135;--c2:#139688;--c3:#a29015;--c4:#a82653;--c5:#1099bf;--c6:#732e7b;
  --c7:#66640c;--c8:#5571d8;--c9:#cb749e;--c10:#00673f;--c11:#c6784a;--c12:#87579d;--c13:#8f887c;
  --sage:#c9d4d2;--rose:#a8807b;--blush:#dcc0ba;--straw:#e9dfc8;--ink:#23252f;
  --serif:"Didot","Bodoni 72","Bodoni MT","Playfair Display","Libre Bodoni",Georgia,"Times New Roman",serif;
  --kicker:"Papiers d\00E9 coup\00E9 s \00B7  Codex usage, cut from local records"}
[data-style="matisse"] body{font:15px/1.55 "Avenir Next",Avenir,Futura,"Century Gothic","Gill Sans",
  "Trebuchet MS",system-ui,sans-serif}
[data-style="matisse"] .wrap{position:relative;z-index:1}
[data-style="matisse"] nav.bar{background:rgba(243,239,230,.86);border-bottom:2px solid var(--ink);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
[data-style="matisse"] .brand{font:italic 400 21px/1 var(--serif);letter-spacing:0}
[data-style="matisse"] .mast{padding:64px 0 40px;min-height:42vh}
[data-style="matisse"] .kicker{font:italic 400 17px/1.3 var(--serif);text-transform:none;letter-spacing:.01em;
  color:var(--fg)}
[data-style="matisse"] .mast h1{font:400 clamp(46px,8.4vw,104px)/.94 var(--serif);letter-spacing:-.02em;
  margin:12px 0 20px;max-width:8.5ch}
[data-style="matisse"] .dek{display:inline-block;background:var(--ink);color:var(--bg);padding:6px 14px 7px;
  font-size:13px;letter-spacing:.03em;transform:rotate(-1deg);
  border-radius:3px 14px 4px 12px/12px 4px 14px 3px}
/* The tiles are the cut-outs: one sheet of gouache each, trimmed by hand and pinned. */
[data-style="matisse"] .tiles{gap:18px;margin-top:8px}
[data-style="matisse"] .tile{position:relative;border:0;padding:18px 18px 18px 20px;background:var(--sage);
  border-radius:28px 12px 34px 16px/18px 30px 14px 26px;transform:rotate(-.7deg);
  transition:transform .25s cubic-bezier(.3,1.4,.5,1)}
[data-style="matisse"] .tile:nth-child(3n+2){background:var(--blush);transform:rotate(.6deg);
  border-radius:14px 32px 18px 28px/26px 12px 30px 16px}
[data-style="matisse"] .tile:nth-child(3n+3){background:var(--straw);transform:rotate(-.3deg);
  border-radius:34px 18px 26px 12px/14px 26px 18px 30px}
[data-style="matisse"] .tile:hover{transform:rotate(0) translateY(-3px)}
[data-style="matisse"] .tile::before{content:"";position:absolute;top:9px;right:13px;width:7px;height:7px;
  border-radius:50%;background:var(--ink)}
[data-style="matisse"] .tile .k{color:var(--fg);font-weight:600;font-size:10.5px;letter-spacing:.16em}
[data-style="matisse"] .tile .v{font:400 42px/1.05 var(--serif);letter-spacing:-.01em;margin-top:6px}
[data-style="matisse"] .tile .n{color:rgba(35,37,47,.78)}
/* Each panel is a paper sheet laid over a coloured one, a few millimetres out of register. */
[data-style="matisse"] .panel{border:0;padding:18px 20px;margin-top:26px;
  border-radius:6px 22px 8px 18px/18px 8px 22px 6px;box-shadow:-10px 10px 0 -2px var(--sage)}
[data-style="matisse"] .panel:nth-child(even){box-shadow:10px 10px 0 -2px var(--blush)}
[data-style="matisse"] .panel.pies{box-shadow:-10px 10px 0 -2px var(--straw)}
[data-style="matisse"] .legend{color:var(--dim)}
[data-style="matisse"] .legend i{width:12px;height:12px;border-radius:60% 40% 55% 45%/55% 60% 40% 45%}
[data-style="matisse"] .pie path{stroke-width:3;stroke-linejoin:round}
/* The area under the cumulative curve is a flat sage sheet, cut along the ink line. */
[data-style="matisse"] #rlchart path[fill-opacity]{fill:var(--sage);fill-opacity:1}
[data-style="matisse"] .deco .mz{display:block}
.mz{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.mz svg{position:absolute;display:block;height:auto;overflow:visible}
.mz .sage{fill:var(--sage)} .mz .rose{fill:var(--rose)} .mz .ink{fill:var(--ink)}
.mz .stem{fill:none;stroke:var(--ink);stroke-width:7;stroke-linecap:round}
.mz .dash{stroke:#fff;stroke-width:11;stroke-linecap:round}
.mz-sage{left:-10vw;top:3vh;width:min(66vw,720px)}
.mz-rose{right:-9vw;bottom:-10vh;width:min(50vw,560px)}
.mz-dash1{right:8vw;top:10vh;width:min(36vw,360px)}
.mz-dash2{left:1vw;bottom:5vh;width:min(24vw,250px)}
.mz-flower{right:max(1vw,calc(50vw - 640px));top:8vh;width:auto!important;height:min(86vh,760px)!important;
  transform-origin:62% 100%;animation:sway 11s ease-in-out infinite alternate}
@keyframes sway{from{transform:rotate(-1.8deg)}to{transform:rotate(1.4deg)}}
@media(prefers-reduced-motion:reduce){.mz-flower{animation:none}}
@media(max-width:640px){
  .mz-flower{right:-24vw;top:12vh;height:48vh!important}
  .mz-sage{left:-30vw;width:96vw} .mz-rose{right:-30vw;width:84vw}
  .mz-dash1{width:44vw;right:-6vw;top:44vh}
  [data-style="matisse"] .mast{padding-top:40px;min-height:0}
  [data-style="matisse"] .kicker{max-width:64%}
  [data-style="matisse"] .tile .v{font-size:32px}}

/* ---- 3. NOCTURNE: the report as sculptures on night water, in blue and gold ----------- */
/* Whistler's Nocturnes -- the Thames after dark, gaslight doubled in still water, and the
   gold sparks of The Falling Rocket.  The page itself is a 3D scene (SCENE_JS); what follows
   is first the sheet it falls back to without WebGL2, then the chrome the scene keeps.  The
   categorical palette is validated on the plinth (#10202a): all thirteen slots inside the
   dark lightness band and over the chroma floor, neighbours clear of the colour-vision and
   normal-vision floors, every pair among the first four as well, and each at 3:1 or more. */
:root[data-style="nocturne"]{color-scheme:dark;
  --bg:#0b1720;--panel:#10202a;--line:#1f3441;--fg:#efe6d2;--dim:#8fa3ad;
  --cached:#2d4c63;--uncached:#7fb2d8;--out:#3fb58f;--warn:#e0b24f;--warn-bg:#3a2e12;
  --c0:#c1821f;--c1:#4174c7;--c2:#b04466;--c3:#14a685;--c4:#a55cc0;--c5:#4f9a5c;--c6:#8f76cc;
  --c7:#5f9234;--c8:#6a78d6;--c9:#878c22;--c10:#7a86e0;--c11:#c96a22;--c12:#3f86c8;--c13:#6f8290;
  --n-zenith:#02070b;--n-sky:#0a1b26;--n-haze:#2f4a55;--n-water:#040c12;--n-shore:#0c1a22;
  --n-lamp:#f3c878;--n-stone:#15242d;--n-trim:#b98f45;--n-spark:#ffc766;
  --serif:"Baskerville","Libre Baskerville","Big Caslon","Palatino Linotype",Palatino,"Book Antiqua",
    Georgia,serif;
  --kicker:"Nocturne in blue and gold \00B7  Codex usage, recounted locally"}
[data-style="nocturne"] body{background:radial-gradient(120% 70% at 50% 0,#10283a,var(--bg) 70%) fixed}
[data-style="nocturne"] nav.bar{background:rgba(11,23,32,.82);border-bottom:1px solid #2a3e49;
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
[data-style="nocturne"] .brand{font:italic 400 19px/1 var(--serif);color:var(--warn);letter-spacing:.01em}
[data-style="nocturne"] .kicker{font:italic 400 15px/1.3 var(--serif);text-transform:none;letter-spacing:.01em}
[data-style="nocturne"] .mast h1{font:400 clamp(38px,6vw,64px)/1 var(--serif);letter-spacing:-.01em;
  margin:10px 0 12px}
[data-style="nocturne"] .tile{background:linear-gradient(#132733,#0f1e28);border:1px solid #2a3e49;
  box-shadow:inset 0 1px 0 rgba(224,178,79,.18)}
[data-style="nocturne"] .tile .v{font-weight:400;color:#f6e7c1}
[data-style="nocturne"] .panel{border-color:#2a3e49;box-shadow:inset 0 1px 0 rgba(224,178,79,.14)}

/* The scene is running: it fills the window, and the page beneath it stays in place for
   assistive tech -- every number is still text there -- but is not painted and takes no
   pointer.  The one control kept is the style switch, whose dot the scene draws itself. */
.s3d{display:none;position:fixed;inset:0;width:100%;height:100%;z-index:10;touch-action:none;
  outline:none;-webkit-tap-highlight-color:transparent}
[data-s3d] .s3d{display:block}
[data-s3d] body{overflow:hidden;background:var(--bg)}
[data-s3d] .wrap{position:fixed;inset:0;opacity:0;pointer-events:none;overflow:hidden}
[data-s3d] nav.bar{background:none;border:0;backdrop-filter:none;-webkit-backdrop-filter:none;
  pointer-events:none}
[data-s3d] .brand{opacity:0}
[data-s3d] #stylebtn{pointer-events:auto}
[data-s3d] #stylebtn .sdot{opacity:0}
.s3d-live{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);
  white-space:nowrap}
"""

JS = """
const D = window.__TC__;
// Previews, model names and cwds come straight from rollout content; never hand them to
// innerHTML raw.
const esc = s => String(s??'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

const big = n => n==null ? '--'
  : Math.abs(n)>=1e9 ? (n/1e9).toFixed(2)+'B'
  : Math.abs(n)>=1e6 ? (n/1e6).toFixed(1)+'M'
  : Math.abs(n)>=1e3 ? (n/1e3).toFixed(1)+'K' : String(n);
const when = t => new Date(t*1000).toLocaleString([], {month:'short', day:'numeric',
                                                      hour:'2-digit', minute:'2-digit'});
const day = t => new Date(t*1000).toLocaleDateString([], {month:'short', day:'numeric'});
const hm  = t => new Date(t*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
const clamp = (v,a,b) => v<a?a:(v>b?b:v);
const byId = id => document.getElementById(id);
/** A duration as render.secs writes it: 0.84s, 8.4s, 34s, 2m 05s, 1h 05m. */
const secs = x => {
  if(x == null) return '--';
  if(x < 0.995) return x.toFixed(2)+'s';
  if(x < 9.95) return x.toFixed(1)+'s';
  if(x < 59.5) return x.toFixed(0)+'s';
  const m = Math.floor(Math.round(x)/60), sc = Math.round(x) % 60;
  return m < 60 ? `${m}m ${String(sc).padStart(2,'0')}s`
                : `${Math.floor(m/60)}h ${String(m%60).padStart(2,'0')}m`;
};

const HOUR = 3600, DAY = 86400;

// ---- marks, for the WebGL layer -----------------------------------------------------
// Every chart also records what it drew as plain shapes -- areas, lines, rects, pies -- in its
// own SVG's units, keyed by the element it drew into.  Under a style that paints its marks
// in WebGL (GL_JS below) the SVG keeps the axes, the text and the tooltips, its marks go
// transparent, and these shapes are drawn instead.  Under a style that is a 3D scene
// (SCENE_JS) the same shapes are built into it, and `plot` -- the plot rectangle, [x, y, w, h]
// in the SVG's units -- is what places them.  Everywhere else nothing reads them.
const SCN = new Map();              // host -> {svg: () => element, vb: [w, h], clip, list, plot}
const GLH = {dirty(){}};            // replaced by the WebGL layer once it is running
const S3H = {dirty(){}};            // replaced by the 3D scene once it is running
const varOf = f => (/var\\((--[\\w-]+)\\)/.exec(f||'') || [])[1] || f;
function marks(host, svg, vb, clip, list, plot){
  if(list) SCN.set(host, {svg, vb, clip, list, plot}); else SCN.delete(host);
  GLH.dirty();
  S3H.dirty();
}

// ---- one viewport, three charts -----------------------------------------------------
// DOM is the whole range the report covers; VIEW is the slice currently drawn.  Every chart
// reads VIEW, so panning or zooming any one of them moves all three -- which is the reason
// the daily bars are rendered in unit-x rather than in pixels.
const G = D.geo || {};
const DOM = D.domain;
let VIEW = DOM ? [DOM[0], DOM[1]] : null;
// Deepest zoom: one day.  Content is placed at the hour its file opened, so below a day the
// composition pie stops resolving and mostly shows the gaps between sessions; a day is also
// the unit the daily chart is drawn in.  (A corpus shorter than a day is shown whole.)
const MIN_SPAN = 86400;

let W = G.w||980, L = G.l||62, RM = G.r||48, PLOT = W-L-RM;

const X   = t  => L + (t-VIEW[0])/(VIEW[1]-VIEW[0])*PLOT;
const Tat = vx => VIEW[0] + (vx-L)/PLOT*(VIEW[1]-VIEW[0]);

function measure(){
  // One viewBox unit = one CSS pixel.  A fixed 980-unit viewBox squeezed onto a 360px phone
  // renders 11px axis text at four; this keeps every label at the size it asks for.
  const host = document.querySelector('.chart');
  const w = host ? Math.round(host.getBoundingClientRect().width) : 0;
  W = Math.max(300, w || 980);
  L = Math.round(Math.min(G.l||62, W*0.17));
  RM = Math.round(Math.min(G.r||48, W*0.13));
  PLOT = W - L - RM;
}

// ---- shared ticks -------------------------------------------------------------------
// Both time charts draw the same ticks at the same x, which is what makes one readable
// against the other.  Day steps are anchored on the domain and stepped with setDate: ticks
// stay on local midnights across a clock change, and do not jump to neighbouring days while
// the chart is being dragged.
const PITCH = 64;                          // smallest gap between two tick labels, in px
                                           // -- a date at 11px is about 40 of them, and a
                                           // phone's plot is only ~230 wide
function ticks(plot = PLOT){               // plot: the width the ticks are laid over, px
  const span = VIEW[1]-VIEW[0];
  const fits = s => s/span*plot >= PITCH;
  const out = [];
  let step = 0;
  for(const s of [300, 600, 900, 1800, HOUR, 2*HOUR, 3*HOUR, 6*HOUR, 12*HOUR])
    if(fits(s)){ step = s; break; }
  if(step){
    const a = new Date(VIEW[0]*1000); a.setHours(0,0,0,0);
    let t = a.getTime()/1000;
    t += Math.ceil((VIEW[0]-t)/step)*step;
    for(; t<=VIEW[1] && out.length<64; t+=step) out.push([t, step]);
    return out;
  }
  let d = 364;
  for(const s of [1,2,3,7,14,28,91,182,364]) if(fits(s*DAY)){ d = s; break; }
  const c = new Date(DOM[0]*1000); c.setHours(0,0,0,0);
  const skip = Math.floor((VIEW[0]-c.getTime()/1000)/(d*DAY));
  if(skip > 0) c.setDate(c.getDate()+skip*d);
  for(let i=0; i<512; i++){
    const t = c.getTime()/1000;
    if(t > VIEW[1]) break;
    if(t >= VIEW[0]) out.push([t, d*DAY]);
    c.setDate(c.getDate()+d);
  }
  return out;
}

const tickLabel = (t, step) =>
  (step >= DAY || new Date(t*1000).getHours()===0) ? day(t) : hm(t);

/** Tick labels along the bottom, and -- unless `grid` is false -- a dashed guide up from each.
 *  The daily chart takes the labels only: its bars already mark the days. */
function axis(h, top, bot, tk, grid = true){
  let s = '';
  for(const [t, step] of tk){
    const xx = X(t);
    if(xx < L-0.5 || xx > W-RM+0.5) continue;
    if(grid) s += `<line x1="${xx.toFixed(1)}" y1="${top}" x2="${xx.toFixed(1)}" y2="${h-bot}" `+
         `stroke="var(--line)" stroke-width="1" stroke-dasharray="2 4"/>`;
    s += `<text x="${xx.toFixed(1)}" y="${h-bot+15}" text-anchor="middle" fill="var(--dim)" `+
         `font-size="11">${esc(tickLabel(t, step))}</text>`;
  }
  return s;
}

// ---- chart 1: cumulative tokens per weekly limit window ------------------------------
const RL = D.rate_limits || {};
const WINS = RL.windows || [];
// The window the limit chart draws: the weekly one, or the longest the logs quote without it.
const LIMIT = (RL.name || 'weekly') + ' limit';
// cum_points are [t, cumulative input, cumulative uncached, cumulative output], and a fifth
// column, cumulative input counted with tiktoken, when the report counted it: the curve then
// draws that input, beside Codex's output.  Input and output are summed rather than drawn
// apart: output is under 1% of input, so a second curve would sit flat on the axis and say
// nothing.
const TK = D.input_source === 'tiktoken';
const INPUT = TK ? 'input' : 'recorded input';
const pick = p => (p.length > 4 ? p[4] : p[1]) + p[3];
/** A window's input, as the curve draws it. */
const winInput = w => (TK && w.tokens.tiktoken_input != null)
  ? `input ${big(w.tokens.tiktoken_input)} (tiktoken)` : `recorded input ${big(w.tokens.input)}`;
// Fixed over the corpus, never over the viewport: zoom moves the time axis and leaves the
// value axis alone, so a curve keeps its height while the window slides under it.
let VMAX = 0;
WINS.forEach(w => (w.cum_points||[]).forEach(p => { VMAX = Math.max(VMAX, pick(p)); }));
VMAX = VMAX || 1;

function drawRL(tk){
  const host = byId('rlchart');
  if(!host) return;
  if(!WINS.length){
    host.innerHTML = '<p class="sub">No weekly-limit snapshots in range.</p>';
    marks(host, null);
    return;
  }
  const H = G.rl_h||300, T = G.rl_t||18, B = G.rl_b||34;
  const y  = v => H-B - (v/VMAX)*(H-B-T);
  const yp = p => H-B - (p/100)*(H-B-T);

  let s = `<svg viewBox="0 0 ${W} ${H}" data-h="${H}" data-t="${T}" data-b="${B}" role="img" `+
          `aria-label="cumulative tokens per ${LIMIT} window">`;
  s += `<defs><clipPath id="tcclip-rl"><rect x="${L}" y="0" width="${PLOT}" height="${H}"/>`+
       `</clipPath></defs>`;
  // horizontal guides + left axis (measured) + right axis (reported)
  [0,.25,.5,.75,1].forEach(f=>{
    const yy = y(VMAX*f);
    s += `<line x1="${L}" y1="${yy.toFixed(1)}" x2="${W-RM}" y2="${yy.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>`;
    s += `<text x="${L-8}" y="${(yy+4).toFixed(1)}" text-anchor="end" fill="var(--dim)" font-size="11">${big(Math.round(VMAX*f))}</text>`;
    s += `<text x="${W-RM+8}" y="${(yp(100*f)+4).toFixed(1)}" fill="var(--warn)" font-size="11">${Math.round(100*f)}%</text>`;
  });
  s += axis(H, T, B, tk);

  s += `<g clip-path="url(#tcclip-rl)">`;
  const mk = [];
  // Window boundaries carry the date they opened.  On a narrow screen, or zoomed out far
  // enough that three windows share fifty pixels, those labels collide into a smear -- so a
  // label is drawn only where there is room for it.  The boundary line is always drawn.
  let lastLbl = -1e9;
  WINS.forEach((w, wi)=>{
    const pts = w.cum_points||[], pcs = w.pct_points||[];
    const start = w.reset_at!=null ? w.reset_at : (pts.length?pts[0][0]:null);
    if(start==null) return;
    let hi = start;
    if(pts.length) hi = Math.max(hi, pts[pts.length-1][0]);
    if(pcs.length) hi = Math.max(hi, pcs[pcs.length-1][0]);
    if(hi < VIEW[0] || start > VIEW[1]) return;    // no part of this window is on screen
    // Reset boundary: the instant the replacement window was first reported.
    s += `<line x1="${X(start).toFixed(1)}" y1="${T}" x2="${X(start).toFixed(1)}" y2="${H-B}" `+
         `stroke="var(--dim)" stroke-dasharray="3 3" stroke-width="1"/>`;
    if(X(start) - lastLbl >= 46){
      lastLbl = X(start);
      s += `<text x="${(X(start)+3).toFixed(1)}" y="${T+10}" fill="var(--dim)" font-size="10">${esc(day(start))}</text>`;
    }
    if(pts.length){
      const line = [[X(start), y(0)]].concat(pts.map(p=>[X(p[0]), y(pick(p))]));
      mk.push({t:'area', pts:line, base:y(0), c:'--uncached', a:.16, win:wi},
              {t:'line', pts:line, w:1.8, c:'--uncached', win:wi});
      const d = [`M ${X(start).toFixed(1)} ${y(0).toFixed(1)}`]
        .concat(pts.map(p=>`L ${X(p[0]).toFixed(1)} ${y(pick(p)).toFixed(1)}`));
      const last = pts[pts.length-1];
      s += `<path d="${d.join(' ')} L ${X(last[0]).toFixed(1)} ${y(0).toFixed(1)} Z" `+
           `fill="var(--uncached)" fill-opacity=".16" class="mk"/>`;
      s += `<path d="${d.join(' ')}" fill="none" stroke="var(--uncached)" stroke-width="1.8" class="mk">`+
           `<title>window opened ${esc(when(start))}\nreset quoted ${esc(w.resets_at_iso||'--')}\n`+
           `peak reported ${w.peak_pct==null?'--':w.peak_pct+'%'}\n`+
           `${winInput(w)} over ${w.tokens.responses} responses\n`+
           `uncached ${big(w.tokens.uncached)} | output ${big(w.tokens.output)}`+
           (TK ? ' (recorded by Codex)' : '')+
           (w.late_points ? `\n${w.late_points} later reading(s) not drawn: the next window `+
                            `had already opened` : '')+`</title></path>`;
    }
    if(pcs.length){
      const d = pcs.map((p,i)=>`${i?'L':'M'} ${X(p[0]).toFixed(1)} ${yp(p[1]).toFixed(1)}`);
      s += `<path d="${d.join(' ')}" fill="none" stroke="var(--warn)" stroke-width="1.4" stroke-dasharray="5 3" class="mk"/>`;
      mk.push({t:'line', pts:pcs.map(p=>[X(p[0]), yp(p[1])]), w:1.4, c:'--warn', dash:[5,3], win:wi});
    }
  });
  s += `</g>`;
  s += `<line x1="${L}" y1="${H-B}" x2="${W-RM}" y2="${H-B}" stroke="var(--line)"/>`;
  s += '</svg>';
  host.innerHTML = s;
  marks(host, ()=>host.querySelector('svg'), [W, H], [L, 0, PLOT, H], mk, [L, T, PLOT, H-B-T]);
}

// ---- chart 2: daily input -------------------------------------------------------------
// The bars are rendered server-side in unit-x -- one unit is one local day -- so only the
// group transform changes here.  Nothing vertical is ever touched.
function drawDaily(tk){
  const host = byId('dailychart');
  if(!host) return;
  const svg = host.querySelector('svg');
  if(!svg) return;
  const H = +svg.getAttribute('data-h') || 210;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const clip = svg.querySelector('.clip');
  if(clip){ clip.setAttribute('x', L); clip.setAttribute('width', PLOT); }
  const mk = [];
  svg.querySelectorAll('g.bar').forEach(g=>{
    const a = +g.getAttribute('data-a'), b = +g.getAttribute('data-b');
    const x = X(a), sx = Math.max(X(b)-x, 0.001);
    g.setAttribute('transform', `translate(${x.toFixed(2)},0) scale(${sx.toFixed(5)},1)`);
    // The segments never change; they are read out of the markup once.
    if(!g.__mk) g.__mk = Array.from(g.querySelectorAll('rect.mk')).map(r=>
      [+r.getAttribute('y'), +r.getAttribute('height'), varOf(r.getAttribute('fill'))]);
    if(x + sx < L || x > W-RM) return;
    for(const [ry, rh, c] of g.__mk) mk.push({t:'rect', x:x+.04*sx, y:ry, w:.92*sx, h:rh, c, day:a});
  });
  const base = svg.querySelector('.base');
  if(base){ base.setAttribute('x1', L); base.setAttribute('x2', W-RM); }
  const peak = svg.querySelector('.peak');
  if(peak) peak.setAttribute('x', L);
  const ax = svg.querySelector('.ax');
  if(ax) ax.innerHTML = axis(H, +svg.getAttribute('data-t') || 18,
                                +svg.getAttribute('data-b') || 34, tk, false);
  const T = +svg.getAttribute('data-t') || 18, B = +svg.getAttribute('data-b') || 34;
  marks(host, ()=>svg, [W, H], [L, 0, PLOT, H], mk, [L, T, PLOT, H-B-T]);
}

// ---- chart 3: response time by day ----------------------------------------------------
// Two lines over the days: the median response and the p90.  Drawn here, like the limit
// chart's curves, because a line's points move with the viewport; its heights never do --
// the value axis is fixed over the corpus.  A day with fewer than LAT_MIN timed responses is
// not a point (one slow response is not a slow day) and breaks the line, as a day with none
// does; it keeps a hover target that says so.
// Behind the lines, on an axis of their own at the right, a bar a day counts the rate-limit
// events Codex logged: snapshots in which a limit was reached.  A day the limit blocked
// outright has events and no timed response, so the bars keep their own list of days.
const LATD = D.latency || {};
const LDAYS = LATD.days || [];                // [start, end, timed responses, median, p90]
const LEV = LATD.events || [];                // [start, end, rate-limit events]
const LAT_MIN = LATD.min || 5;
const latOk = (r, k) => r[2] >= LAT_MIN && r[k] != null;
let LMAX = 0;
LDAYS.forEach(r => { if(latOk(r, 3)) LMAX = Math.max(LMAX, latOk(r, 4) ? r[4] : r[3]); });
const LSHOWN = LMAX > 0;
// A clean ceiling, so the axis reads 0 / 45s / 1m 30s rather than 0 / 34s / 1m 09s.
LMAX = [1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 600, 900, 1200, 1800, 2700, 3600]
  .find(c => c >= LMAX) || LMAX || 1;
// The event axis's ceiling, even, so its middle guide reads a whole number of events.
let EMAX = 0;
LEV.forEach(r => { EMAX = Math.max(EMAX, r[2]); });
EMAX = [2, 4, 6, 8, 10, 12, 16, 20, 24, 30, 40, 50, 60, 80, 100, 120, 160, 200, 300, 400, 500, 600, 800, 1000]
  .find(c => c >= EMAX) || Math.ceil(EMAX/2)*2;
/** Every day the chart says something about, [start, end]: timed responses, events, or both. */
const LSPANS = [...new Map([...LDAYS, ...LEV].map(r => [r[0], [r[0], r[1]]])).values()]
  .sort((p, q) => p[0] - q[0]);
const LROW = new Map(LDAYS.map(r => [r[0], r]));
const LEVN = new Map(LEV.map(r => [r[0], r[2]]));
const evWord = n => `${n.toLocaleString()} rate-limit event${n === 1 ? '' : 's'}`;
/** The two series, as the page and the 3D scene both draw them. */
const LSERIES = [{k: 3, c: '--uncached', w: 2, dash: null},
                 {k: 4, c: '--dim', w: 1.6, dash: [5, 3]}];
/** Runs of consecutive drawable days for series column `k`; a gap or a thin day ends one. */
function latRuns(k){
  const runs = [];
  let cur = [], prevEnd = null;
  for(const r of LDAYS){
    const ok = latOk(r, k);
    if(!ok || (prevEnd != null && r[0] - prevEnd > HOUR)){ if(cur.length) runs.push(cur); cur = []; }
    if(ok){ cur.push(r); prevEnd = r[1]; } else prevEnd = null;
  }
  if(cur.length) runs.push(cur);
  return runs;
}
function drawLat(tk){
  const host = byId('latchart');
  if(!host) return;
  if(!LSHOWN && !LEV.length){
    // Nothing timed at all: the server's own sentence says why, and stays.
    if(LDAYS.length) host.innerHTML = `<p class="sub">No day in range has ${LAT_MIN} or more `+
                                      `timed responses, so none is drawn.</p>`;
    marks(host, null);
    return;
  }
  const H = G.lat_h || 190, T = 18, B = 34;
  const y = v => H-B - (v/LMAX)*(H-B-T);
  const ye = v => H-B - (v/EMAX)*(H-B-T);
  let s = `<svg viewBox="0 0 ${W} ${H}" data-h="${H}" data-t="${T}" data-b="${B}" role="img" `+
          `aria-label="median and p90 response time${LEV.length ? ', and rate-limit events,' : ''} by day">`;
  s += `<defs><clipPath id="tcclip-lat"><rect x="${L}" y="0" width="${PLOT}" height="${H}"/>`+
       `</clipPath></defs>`;
  [0, .5, 1].forEach(f=>{
    const yy = y(LMAX*f);
    s += `<line x1="${L}" y1="${yy.toFixed(1)}" x2="${W-RM}" y2="${yy.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>`;
    if(LSHOWN) s += `<text x="${L-8}" y="${(yy+4).toFixed(1)}" text-anchor="end" fill="var(--dim)" font-size="11">${f ? esc(secs(LMAX*f)) : '0'}</text>`;
    if(LEV.length) s += `<text x="${W-RM+8}" y="${(yy+4).toFixed(1)}" fill="var(--warn)" font-size="11">${EMAX*f}</text>`;
  });
  s += axis(H, T, B, tk);
  s += `<g clip-path="url(#tcclip-lat)">`;
  const mk = [];
  const mid = r => (r[0] + r[1])/2;
  // The bars first, so both lines are drawn over them.
  for(const r of LEV){
    const x0 = X(r[0]), x1 = X(r[1]), bw = .6*(x1 - x0), bx = x0 + .2*(x1 - x0);
    if(x1 < L || x0 > W-RM) continue;
    const yy = ye(r[2]);
    s += `<rect x="${bx.toFixed(1)}" y="${yy.toFixed(1)}" width="${bw.toFixed(1)}" height="${(H-B-yy).toFixed(1)}" `+
         `fill="var(--warn)" fill-opacity=".5" class="mk"/>`;
    mk.push({t:'rect', x: bx, y: yy, w: bw, h: H-B-yy, c: '--warn', a: .5, day: r[0]});
  }
  for(const se of LSERIES){
    for(const run of latRuns(se.k)){
      const pts = run.map(r => [X(mid(r)), y(r[se.k])]);
      if(pts.length > 1){
        s += `<path d="${pts.map((p, i) => `${i ? 'L' : 'M'} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ')}" `+
             `fill="none" stroke="var(${se.c})" stroke-width="${se.w}" stroke-linejoin="round" `+
             `stroke-linecap="round"${se.dash ? ` stroke-dasharray="${se.dash.join(' ')}"` : ''} class="mk"/>`;
        mk.push(se.dash ? {t:'line', pts, w: se.w, c: se.c, dash: se.dash} : {t:'line', pts, w: se.w, c: se.c});
      }
      // A dot on every day, so a day standing alone between two gaps is still seen.
      for(const p of pts)
        s += `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="${se.dash ? 2.5 : 3}" fill="var(${se.c})"/>`;
    }
  }
  for(const [a, b] of LSPANS){
    const x0 = X(a), x1 = X(b);
    if(x1 < L || x0 > W-RM) continue;
    const r = LROW.get(a), n = LEVN.get(a);
    const tip = day(a) +
                (r ? `\n${r[2].toLocaleString()} timed responses` +
                     (latOk(r, 3) ? `\nmedian ${secs(r[3])}, p90 ${secs(r[4])}` : `\ntoo few to show`) : '') +
                (n ? `\n${evWord(n)}` : '');
    s += `<rect x="${x0.toFixed(1)}" y="${T}" width="${Math.max(0, x1-x0).toFixed(1)}" height="${H-T-B}" `+
         `fill="transparent"><title>${esc(tip)}</title></rect>`;
  }
  s += `</g>`;
  s += `<line x1="${L}" y1="${H-B}" x2="${W-RM}" y2="${H-B}" stroke="var(--line)"/>`;
  s += '</svg>';
  host.innerHTML = s;
  marks(host, ()=>host.querySelector('svg'), [W, H], [L, 0, PLOT, H], mk, [L, T, PLOT, H-B-T]);
}

// ---- chart 4: what filled the window -------------------------------------------------
// Tokenized content is deduplicated per rollout file, so the file is the finest unit its
// categories can honestly be placed on: a bucket carries the content of the files that
// opened inside it, and counts here when it overlaps the visible range.
const CATS = D.cats || {};
function drawPie(){
  const host = byId('catpie');
  if(!host) return;
  const series = CATS.series || [], bucket = CATS.bucket || 3600;
  const tot = {};
  let sum = 0;
  for(const row of series){
    const t = row[0], c = row[1];
    if(VIEW && (t+bucket <= VIEW[0] || t >= VIEW[1])) continue;
    for(const k in c){ tot[k] = (tot[k]||0) + c[k]; sum += c[k]; }
  }
  if(!sum){
    // An empty pie has two very different causes -- nothing tokenized at all, or nothing in
    // the range on screen -- and a reader cannot tell them apart from an empty panel.
    const msg = series.length ? 'No tokenized content in the visible range.'
                              : (CATS.note || 'No content in range.');
    host.innerHTML = emptyPie(msg);
    host.__shown = null;
    marks(host, null);
    return;
  }
  // Colour is the category's place in the corpus-wide order, so a slice keeps its colour as
  // the viewport moves and one pie can be read against the last.
  const order = CATS.order || Object.keys(tot);
  const rows = order.map((k,i)=>({k:k, v:tot[k]||0, fill:`var(--c${i%14})`}));
  pieTo(host, rows, sum, 'tokens in view', 'content composition by category');
}

// ---- chart 3b: which models took the input --------------------------------------------
// Input by the charged model, from the same day buckets the daily bars draw, and in the same
// colours: a model past the daily chart's cap folds into `other` here as well.
const MODELS = D.models || {};
function drawModelPie(){
  const host = byId('modelpie');
  if(!host) return;
  const keys = MODELS.order || [], rank = {};
  keys.forEach((m,i)=>{ rank[m] = i; });
  const tot = {};
  let sum = 0;
  for(const row of (MODELS.days || [])){
    const a = row[0], b = row[1], c = row[2];
    if(VIEW && (b <= VIEW[0] || a >= VIEW[1])) continue;
    for(const m in c){
      const k = m in rank ? m : 'other';
      tot[k] = (tot[k]||0) + c[m]; sum += c[m];
    }
  }
  if(!sum){
    host.innerHTML = emptyPie(`No ${INPUT} in the visible range.`);
    host.__shown = null;
    marks(host, null);
    return;
  }
  const rows = keys.concat(['other']).map(k=>({k:k, v:tot[k]||0,
    fill: k in rank ? `var(--c${rank[k]%14})` : 'var(--dim)'}));
  pieTo(host, rows, sum, `${INPUT} in view`, `${INPUT} by model`);
}

// ---- the pies follow the viewport, a beat behind ---------------------------------------
// A drag or a zoom redraws the time charts on every frame; recomposing the pies at that rate
// reads as flicker.  They wait until the viewport has been still for PIE_WAIT ms, then turn
// from the slices they show to the new ones.  A pie's rows are the same keys in the same
// order on every draw -- the corpus-wide order, zero-valued entries included -- so a slice
// can grow from nothing or shrink away rather than jump.
const REDUCE = (()=>{ try{ return matchMedia('(prefers-reduced-motion: reduce)').matches; }
                      catch(_){ return false; } })();
const PIE_WAIT = 180, PIE_MS = 520;
let pieTimer = 0, pieDrawn = false;

function schedulePies(){
  if(!pieDrawn){ pieDrawn = true; drawPie(); drawModelPie(); return; }   // first paint: now
  clearTimeout(pieTimer);
  pieTimer = setTimeout(()=>{ pieTimer = 0; drawPie(); drawModelPie(); }, PIE_WAIT);
}

/** Draw `rows` into `host`, turning from whatever fractions it shows now.  A newer call
 *  cancels an older tween mid-flight and starts from where that one had got to.  A frame
 *  without a timestamp (a stub DOM) lands on the end state at once. */
function pieTo(host, rows, sum, what, label){
  const to = rows.map(r=>r.v/sum);
  const from = host.__shown && host.__shown.length === to.length ? host.__shown : null;
  const tok = host.__tok = (host.__tok||0) + 1;
  if(!from || REDUCE){
    host.__shown = to;
    host.innerHTML = pie(rows, sum, what, label, to);
    pieMarks(host, rows, to);
    pieHover(host);
    return;
  }
  let t0 = null;
  const step = ts=>{
    if(host.__tok !== tok) return;                  // superseded by a newer range
    const k = typeof ts === 'number' ? Math.min(1, (ts - (t0 === null ? (t0 = ts) : t0))/PIE_MS) : 1;
    const e = 1 - Math.pow(1-k, 3);                 // ease out: fast start, soft landing
    host.__shown = from.map((f,i)=>f + (to[i]-f)*e);
    host.innerHTML = pie(rows, sum, what, label, host.__shown);
    pieMarks(host, rows, host.__shown);
    pieHover(host);
    if(k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/** Hovering a slice lights its legend line and dims the rest, in place of a tooltip: the
 *  legend already carries the name and the share.  The hovered index lives on the host, so
 *  it survives the pie being redrawn under the pointer while it turns to a new range. */
function pieHover(host){
  if(!host.addEventListener || !host.querySelectorAll                 // a stub DOM
     || !host.classList || typeof host.classList.toggle !== 'function') return;
  if(!host.__hov){
    host.__hov = true;
    const set = i=>{ if(host.__hi !== i){ host.__hi = i; pieHover(host); } };
    host.addEventListener('pointerover', e=>{
      const sl = e.target && e.target.closest && e.target.closest('.pie [data-i]');
      set(sl ? sl.getAttribute('data-i') : null);
    });
    host.addEventListener('pointerleave', ()=>set(null));
  }
  let lit = false;
  for(const el of host.querySelectorAll('.legend [data-i]')){
    const on = el.getAttribute('data-i') === host.__hi;
    el.classList.toggle('on', on);
    lit = lit || on;
  }
  host.classList.toggle('hi', lit);
}

/** The pie as marks: the same slices `pie` draws, from the same fractions. */
function pieMarks(host, rows, fr){
  const size = 240;
  marks(host, ()=>host.querySelector('.pie svg'), [size, size], null,
        [{t:'pie', cx:size/2, cy:size/2, r:size/2-4, sep:'--panel',
          slices:rows.map((r,i)=>[fr[i], varOf(r.fill)])}]);
}

/** An empty range keeps the pie's place -- a hollow ring and the reason -- so the panel does
 *  not collapse under the reader and spring back on the next range. */
function emptyPie(msg){
  const size = 240, r = size/2-4, c0 = size/2;
  return `<div class="row" style="gap:24px"><div class="pie"><svg viewBox="0 0 ${size} ${size}" `+
    `role="img" aria-label="${esc(msg)}"><circle cx="${c0}" cy="${c0}" r="${r-1}" fill="none" `+
    `stroke="var(--line)" stroke-width="2" stroke-dasharray="6 6"/></svg></div>`+
    `<p class="sub" style="max-width:220px">${esc(msg)}</p></div>`;
}

/** A pie and its legend.  `rows` are {k, v, fill} in draw order and `fr` the fraction of the
 *  circle each one takes -- its share, or a moment of a tween toward it.  The legend always
 *  reads the share; an entry that would read 0.0% keeps its slice but not its legend line. */
function pie(rows, sum, what, label, fr){
  const size = 240, r = size/2-4, c0 = size/2;
  let s = `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="${esc(label)}">`;
  let a = -Math.PI/2;                        // first slice starts at twelve o'clock
  for(let i=0; i<rows.length; i++){
    const row = rows[i], frac = fr ? fr[i] : row.v/sum;
    if(!(frac > 1e-6)) continue;
    if(frac >= 1-1e-12){
      // One entry holding everything: an arc whose ends coincide draws nothing.
      s += `<circle cx="${c0}" cy="${c0}" r="${r}" fill="${row.fill}" class="mk" data-i="${i}"/>`;
      break;
    }
    const b = a + frac*2*Math.PI;
    s += `<path d="M ${c0} ${c0} L ${(c0+r*Math.cos(a)).toFixed(2)} ${(c0+r*Math.sin(a)).toFixed(2)} `+
         `A ${r} ${r} 0 ${frac>0.5?1:0} 1 ${(c0+r*Math.cos(b)).toFixed(2)} ${(c0+r*Math.sin(b)).toFixed(2)} Z" `+
         `fill="${row.fill}" stroke="var(--panel)" stroke-width="1" class="mk" data-i="${i}"/>`;
    a = b;
  }
  s += '</svg>';
  const legend = rows.map((r,i)=>r.v/sum < 0.0005 ? '' :
    `<span data-i="${i}"><i style="background:${r.fill}"></i>${esc(r.k)} ${(100*r.v/sum).toFixed(1)}%</span>`).join('');
  return `<div class="row" style="gap:24px"><div class="pie">${s}</div>`+
    `<div class="legend" style="flex-direction:column;gap:6px">`+
    `<span><b>${big(sum)}</b>&nbsp;${what}</span>${legend}</div></div>`;
}

// ---- viewport ------------------------------------------------------------------------
let raf = 0;
function schedule(){
  if(raf) return;
  raf = requestAnimationFrame(()=>{ raf = 0; redraw(); });
}

function redraw(){
  const tk = VIEW ? ticks() : [];
  drawRL(tk);
  drawDaily(tk);
  drawLat(tk);
  schedulePies();
}

/** The view that puts `tAnchor` under `vxAnchor` at the given span, clamped to the domain. */
function spanned(span, tAnchor, vxAnchor){
  const full = DOM[1]-DOM[0];
  span = clamp(span, Math.min(MIN_SPAN, full), full);
  const v0 = clamp(tAnchor - (clamp(vxAnchor, L, W-RM)-L)/PLOT*span, DOM[0], DOM[1]-span);
  return [v0, v0+span];
}

/** Jump there at once: the hand is on the chart (a pinch), or a caller wants it now. */
function setSpan(span, tAnchor, vxAnchor){
  if(!VIEW) return;
  stopGlide();
  VIEW = spanned(span, tAnchor, vxAnchor);
  schedule();
}

// ---- zoom that glides -----------------------------------------------------------------
// A wheel notch or a double-click moves toward its view instead of jumping to it, easing
// out over about GLIDE_MS.  The move is a true zoom: the one moment that sits at the same x
// in the view it leaves and the view it reaches stays put the whole way, and the span
// changes geometrically, so every frame is the same factor closer.  Wheel turns that land
// mid-glide steer the goal, so a fast spin runs as one smooth zoom.  A drag or a pinch takes
// over at once; prefers-reduced-motion jumps.
const WHEEL_GAP = 250;                        // ms of wheel silence that ends a gesture
const GLIDE_MS = 90;                         // time constant: ~95% there in three of these
let GOAL = null, glideRaf = 0, glideT = 0;

function stopGlide(){ GOAL = null; }

function glideTo(v){
  if(!VIEW) return;
  if(REDUCE || typeof requestAnimationFrame !== 'function'){ VIEW = v; GOAL = null; schedule(); return; }
  GOAL = v;
  if(!glideRaf){ glideT = 0; glideRaf = requestAnimationFrame(glide); }
}

function glide(ts){
  glideRaf = 0;
  if(!GOAL) return;
  const dt = glideT && typeof ts === 'number' ? Math.min(64, ts - glideT) : 16;
  glideT = ts;
  const k = 1 - Math.exp(-dt/GLIDE_MS);
  const a0 = VIEW[0], s0 = VIEW[1]-VIEW[0], a1 = GOAL[0], s1 = GOAL[1]-GOAL[0];
  let a, s;
  if(Math.abs(s1 - s0) > s0*1e-6){
    const f = (a1*s0 - a0*s1)/(s0 - s1);      // the moment both views put at the same x
    s = s0*Math.pow(s1/s0, k);
    a = f - (f - a0)/s0*s;
  } else { s = s0; a = a0 + (a1 - a0)*k; }    // equal spans: a plain pan
  a = clamp(a, DOM[0], DOM[1]-s);
  VIEW = [a, a+s];
  if(Math.abs(s - s1) < s1*1e-3 && Math.abs(a - a1) < s1*5e-4){ VIEW = GOAL; GOAL = null; }
  redraw();
  if(GOAL) glideRaf = requestAnimationFrame(glide);
}

/** Zoom by `factor` about the moment under `vx`, gliding; turns compound on the goal. */
function zoomAt(factor, vx){
  if(!VIEW) return;
  const base = GOAL || VIEW;
  glideTo(spanned((base[1]-base[0])*factor, Tat(vx), vx));
}

function panPx(dvx){
  if(!VIEW) return;
  stopGlide();
  const span = VIEW[1]-VIEW[0];
  const v0 = clamp(VIEW[0] - dvx/PLOT*span, DOM[0], DOM[1]-span);
  VIEW = [v0, v0+span];
  schedule();
}

function reset(){
  if(!DOM) return;
  stopGlide();
  VIEW = [DOM[0], DOM[1]];
  schedule();
}

// One set of gestures, bound to each chart: wheel and trackpad on a desktop, drag and
// two-finger pinch on a touch screen, and a double-click back to the full range.
// Only the plot itself answers a mouse: inside the axes, from the left axis to the right one
// and from the top guide to the baseline.  The tick labels, the legend and the margins stay
// the page's, so a wheel turned over them scrolls it.  (Touch is not limited: pan-y already
// leaves a vertical swipe to the page anywhere on the chart.)
function inPlot(el, e){
  const svg = el.querySelector && el.querySelector('svg');
  if(!svg || !svg.getBoundingClientRect) return false;
  const r = svg.getBoundingClientRect();
  const H = +svg.getAttribute('data-h'), T = +svg.getAttribute('data-t'), B = +svg.getAttribute('data-b');
  if(!r.width || !r.height || !H) return false;
  const x = (e.clientX - r.left)/r.width*W, y = (e.clientY - r.top)/r.height*H;
  return x >= L && x <= W-RM && y >= T && y <= H-B;
}

// A wheel gesture -- events no more than WHEEL_GAP ms apart -- belongs wholly to the chart or
// wholly to the page, whichever it started on.  Kept for the whole page, not per chart: a
// page scroll carries a chart up under a still pointer, and that must go on scrolling.
const WHEEL = {at: -1e9, mode: null};

function bind(el){
  const pts = new Map();                     // live pointers, in viewBox x
  let pinch = null;
  const scale = () => W/(el.getBoundingClientRect().width || W);
  const vxOf = e => {
    const r = el.getBoundingClientRect();
    return (e.clientX - r.left)/(r.width || 1)*W;
  };

  el.addEventListener('wheel', e=>{
    if(!VIEW) return;
    const t = e.timeStamp || Date.now();
    if(t - WHEEL.at > WHEEL_GAP) WHEEL.mode = null;
    if(WHEEL.mode === 'page' || (WHEEL.mode !== 'zoom' && !inPlot(el, e))) return;
    if(Math.abs(e.deltaX) > Math.abs(e.deltaY)){          // trackpad swipe: pan
      WHEEL.mode = 'zoom';
      e.preventDefault();
      panPx(-e.deltaX*scale());
      return;
    }
    if(!e.deltaY) return;
    // Zoomed all the way out, scrolling down scrolls the page -- but a gesture that zoomed
    // out to the full range spends its momentum here, and the *next* one scrolls.
    const base = GOAL || VIEW;
    if(e.deltaY > 0 && base[1]-base[0] >= (DOM[1]-DOM[0])*(1-1e-9)){
      if(WHEEL.mode === 'zoom') e.preventDefault();
      return;
    }
    WHEEL.mode = 'zoom';
    e.preventDefault();
    const unit = e.deltaMode===1 ? 0.05 : (e.deltaMode===2 ? 0.8 : 0.002);
    const vx = clamp(vxOf(e), L, W-RM);
    zoomAt(1/Math.exp(-e.deltaY*unit), vx);
  }, {passive:false});

  el.addEventListener('pointerdown', e=>{
    if(!VIEW || (e.pointerType==='mouse' && e.button!==0)) return;
    if(e.pointerType !== 'touch' && !pts.size && !inPlot(el, e)) return;
    try{ el.setPointerCapture(e.pointerId); }catch(_){}
    pts.set(e.pointerId, vxOf(e));
    stopGlide();
    el.classList.add('drag');
    if(pts.size===2){
      const ids = Array.from(pts.keys());
      pinch = {ia:ids[0], ib:ids[1], ta:Tat(pts.get(ids[0])), tb:Tat(pts.get(ids[1]))};
    }
  });

  el.addEventListener('pointermove', e=>{
    if(!pts.has(e.pointerId)){ el.classList.toggle('inplot', inPlot(el, e)); return; }
    const prev = pts.get(e.pointerId), vx = vxOf(e);
    pts.set(e.pointerId, vx);
    e.preventDefault();
    if(pinch && pts.size>=2){
      const xa = pts.get(pinch.ia), xb = pts.get(pinch.ib);
      if(xa==null || xb==null) return;
      const dx = xb-xa, dt = pinch.tb-pinch.ta;
      // Ignore a pinch that has collapsed or crossed over: the span it implies is
      // meaningless, and a sign flip would turn the axis inside out.
      if(Math.abs(dx) < 4 || dx*dt <= 0) return;
      setSpan(dt*PLOT/dx, pinch.ta, xa);
      return;
    }
    panPx(vx - prev);
  });

  const lift = e=>{
    if(!pts.delete(e.pointerId)) return;
    if(pts.size < 2) pinch = null;
    if(!pts.size) el.classList.remove('drag');
  };
  el.addEventListener('pointerup', lift);
  el.addEventListener('pointercancel', lift);
  el.addEventListener('lostpointercapture', lift);
  el.addEventListener('pointerleave', ()=>el.classList.remove('inplot'));
  el.addEventListener('dblclick', e=>{
    if(!inPlot(el, e)) return;
    e.preventDefault();
    if(DOM) glideTo([DOM[0], DOM[1]]);
  });
}

function init(){
  if(!VIEW){
    drawPie();
    drawModelPie();
    return;
  }
  measure();
  ['rlchart','dailychart','latchart'].forEach(id=>{ const el = byId(id); if(el) bind(el); });
  addEventListener('wheel', e=>{                 // after the charts: whoever this one went to
    WHEEL.at = e.timeStamp || Date.now();
    if(!e.defaultPrevented) WHEEL.mode = 'page';
  }, {passive:true});
  let rt = 0;
  addEventListener('resize', ()=>{
    clearTimeout(rt);
    rt = setTimeout(()=>{ measure(); redraw(); }, 120);
  });
  redraw();
}
init();
"""

# The WebGL layer.  Under a style in GL_STYLES the chart marks -- the limit chart's area and
# curves, the daily bars, the pie slices -- are painted by WebGL2 instead of SVG: one canvas
# per panel, behind the panel's own content, drawing the shapes every chart records in SCN.
# The SVG stays: it carries the axes, the text and the tooltips, and only its marks go
# transparent.  No WebGL2, a context the browser takes back, or any other style, and the SVG
# marks are simply left visible -- the page reads the same with or without this layer.
#
# A frame is drawn multisampled into an offscreen buffer, and reaches the screen through one
# post pass, which draws it through the water (WAVE and SIM in GL_JS).
GL_STYLES = ['clinical']

GL_JS = r"""
// ---- the WebGL layer ------------------------------------------------------------------
// See GL_STYLES in render.py.  Nothing here runs without a real DOM and WebGL2, so the
// stub DOM in scripts/test_page.js never reaches it.
const GL_STYLES = D.gl_styles || [];
// The water: a height field simulated on a coarse grid over the viewport (see SIM below).
//   cell      grid spacing, CSS px -- larger is broader, smoother ripples
//   brush     radius of the disturbance a pointer drags through the water, CSS px
//   substeps  simulation steps per 1/60 s -- how fast a ripple travels
//   damp      energy kept per step -- how long a ripple lasts
//   visc      how much each step evens out velocity with its neighbours -- smooths chop
//   push      height a pointer adds per CSS px it travels (heights are clamped to +-1)
//   v0        pointer speed, px/ms, below which the water is left alone (reading, hovering)
//   slope     how steeply the surface tilts per unit of height difference
//   refract   CSS px the panel is displaced under a fully tilted surface
//   disp      chromatic split, as a fraction of the displacement
//   light     crest and trough lighting; 0 leaves only the refraction
const WAVE = {cell: 6, brush: 24, substeps: 2, damp: .955, visc: .0155, push: 1/45, v0: .3,
              slope: 22, refract: 36, disp: .25, light: 0};
const GLX = (()=>{
  if(typeof document === 'undefined' || !document.createElement || !document.querySelectorAll
     || typeof WebGL2RenderingContext === 'undefined') return null;
  const RT = document.documentElement;
  const layers = new Map();                 // panel -> its canvas and GL state
  let ok = true, on = false, pal = null, queued = false, loop = 0;

  // -- colour: the style's own variables, read once per style and theme ---------------
  function parse(v){
    let m = /^#([0-9a-f]{3,8})$/i.exec(v);
    if(m){
      let h = m[1];
      if(h.length < 5) h = h.split('').map(c=>c+c).join('');
      const n = i => parseInt(h.slice(i, i+2), 16)/255;
      return [n(0), n(2), n(4), h.length >= 8 ? n(6) : 1];
    }
    m = /rgba?\(([^)]+)\)/.exec(v);
    if(m){
      const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(parseFloat);
      return [p[0]/255, p[1]/255, p[2]/255, p.length > 3 ? p[3] : 1];
    }
    return [.5, .5, .5, 1];
  }
  function rgba(name, a){                   // premultiplied, as the blend expects
    pal = pal || {};
    if(!(name in pal)) pal[name] = parse(getComputedStyle(RT).getPropertyValue(name).trim());
    const c = pal[name], al = c[3]*(a == null ? 1 : a);
    return [c[0]*al, c[1]*al, c[2]*al, al];
  }

  // -- geometry: triangles in the SVG's own units, six floats a vertex -----------------
  function tri(V, ax, ay, bx, by, cx, cy, c){
    V.push(ax, ay, c[0], c[1], c[2], c[3], bx, by, c[0], c[1], c[2], c[3],
           cx, cy, c[0], c[1], c[2], c[3]);
  }
  function line(V, pts, hw, c){             // segments as quads, bevelled where they meet
    let pn = null;
    for(let i = 1; i < pts.length; i++){
      const x0 = pts[i-1][0], y0 = pts[i-1][1], x1 = pts[i][0], y1 = pts[i][1];
      const l = Math.hypot(x1-x0, y1-y0);
      if(l < 1e-6) continue;
      const nx = -(y1-y0)/l*hw, ny = (x1-x0)/l*hw;
      tri(V, x0+nx, y0+ny, x1+nx, y1+ny, x1-nx, y1-ny, c);
      tri(V, x0+nx, y0+ny, x1-nx, y1-ny, x0-nx, y0-ny, c);
      if(pn){
        tri(V, x0, y0, x0+pn[0], y0+pn[1], x0+nx, y0+ny, c);
        tri(V, x0, y0, x0-pn[0], y0-pn[1], x0-nx, y0-ny, c);
      }
      pn = [nx, ny];
    }
  }
  function dashes(pts, pat){                // a polyline cut into its dashes, as SVG does
    const out = [];
    let k = 0, left = pat[0], cur = [pts[0]];
    for(let i = 1; i < pts.length; i++){
      let x0 = pts[i-1][0], y0 = pts[i-1][1];
      const x1 = pts[i][0], y1 = pts[i][1];
      let seg = Math.hypot(x1-x0, y1-y0);
      while(seg > left){
        const f = left/seg;
        x0 += (x1-x0)*f; y0 += (y1-y0)*f; seg -= left;
        if(k%2 === 0){ cur.push([x0, y0]); out.push(cur); }
        k++; left = pat[k%pat.length]; cur = [[x0, y0]];
      }
      left -= seg;
      if(k%2 === 0) cur.push([x1, y1]);
    }
    if(k%2 === 0 && cur.length > 1) out.push(cur);
    return out;
  }
  function geo(V, m){
    if(m.t === 'rect'){
      const c = rgba(m.c, m.a);
      tri(V, m.x, m.y, m.x+m.w, m.y, m.x+m.w, m.y+m.h, c);
      tri(V, m.x, m.y, m.x+m.w, m.y+m.h, m.x, m.y+m.h, c);
    } else if(m.t === 'area'){
      const c = rgba(m.c, m.a), p = m.pts;
      for(let i = 1; i < p.length; i++){
        tri(V, p[i-1][0], m.base, p[i-1][0], p[i-1][1], p[i][0], p[i][1], c);
        tri(V, p[i-1][0], m.base, p[i][0], p[i][1], p[i][0], m.base, c);
      }
    } else if(m.t === 'line'){
      const c = rgba(m.c, m.a);
      if(m.pts.length < 2) return;
      for(const run of (m.dash ? dashes(m.pts, m.dash) : [m.pts])) line(V, run, m.w/2, c);
    } else if(m.t === 'pie'){
      let a = -Math.PI/2;
      const seps = [];
      for(const [f, cv] of m.slices){
        if(!(f > 1e-6)) continue;
        const c = rgba(cv), b = a + f*2*Math.PI, n = Math.max(2, Math.ceil(f*160));
        for(let j = 0; j < n; j++){
          const u = a + (b-a)*j/n, w = a + (b-a)*(j+1)/n;
          tri(V, m.cx, m.cy, m.cx+m.r*Math.cos(u), m.cy+m.r*Math.sin(u),
                 m.cx+m.r*Math.cos(w), m.cy+m.r*Math.sin(w), c);
        }
        seps.push(a);
        a = b;
      }
      if(seps.length > 1){                  // the hairline the SVG strokes between slices
        const c = rgba(m.sep);
        for(const s of seps)
          line(V, [[m.cx, m.cy], [m.cx+m.r*Math.cos(s), m.cy+m.r*Math.sin(s)]], .5, c);
      }
    }
  }

  // -- programs --------------------------------------------------------------------------
  const MARK_VS = `#version 300 es
in vec2 p; in vec4 c;
uniform vec2 u_css, u_off; uniform float u_s;
out vec4 v_c;
void main(){
  vec2 q = (u_off + p*u_s)/u_css*2. - 1.;
  v_c = c; gl_Position = vec4(q.x, -q.y, 0., 1.);
}`;
  const MARK_FS = `#version 300 es
precision mediump float;
in vec4 v_c; out vec4 o;
void main(){ o = v_c; }`;
  const POST_VS = `#version 300 es
out vec2 v_uv;
void main(){
  vec2 p = vec2(float((gl_VertexID<<1)&2), float(gl_VertexID&2));
  v_uv = p; gl_Position = vec4(p*2. - 1., 0., 1.);
}`;
  // Water: the panel is seen through one simulated surface (SIM below).  Its height field
  // arrives as a texture over the viewport; the post pass reads the surface's slope where
  // this pixel is, and samples the panel that far away -- a refraction -- splitting red and
  // blue a little either side, so a moving edge fringes as it would through a lens.
  const f1 = x => (+x).toFixed(4);
  const POST_FS = `#version 300 es
precision highp float;
uniform sampler2D u_scene, u_wave; uniform vec2 u_res, u_wo, u_wn; uniform float u_dpr;
in vec2 v_uv; out vec4 o_fx;
vec4 scene(vec2 uv){ return texture(u_scene, uv); }
vec3 w_n;
vec2 water(vec2 uv){
  vec2 cl = u_wo + vec2(uv.x, 1. - uv.y)*u_res/u_dpr;       // this pixel, client CSS px
  vec2 g = (cl/${f1(WAVE.cell)} + .5)/u_wn, e = 1.5/u_wn;    // the grid: row 0 at the top
  float l = texture(u_wave, g - vec2(e.x, 0.)).r, r = texture(u_wave, g + vec2(e.x, 0.)).r;
  float t = texture(u_wave, g - vec2(0., e.y)).r, b = texture(u_wave, g + vec2(0., e.y)).r;
  w_n = normalize(vec3(vec2(r - l, t - b)/3.*${f1(WAVE.slope)}, 1.));
  return w_n.xy*${f1(WAVE.refract)}*u_dpr/u_res;
}
void main(){
  vec2 off = water(v_uv), uv = v_uv + off;
  vec4 c = scene(uv);
  if(${f1(WAVE.disp)} > 0. && dot(off, off) > 1e-10){
    vec2 d = off*${f1(WAVE.disp)};
    vec4 cr = scene(uv - d), cb = scene(uv + d);
    c = vec4(cr.r, c.g, cb.b, max(c.a, max(cr.a, cb.a)));   // still premultiplied
  }
  if(${f1(WAVE.light)} > 0.){
    vec3 L = normalize(vec3(.55, .65, 1.));
    float sp = pow(max(dot(w_n, normalize(L + vec3(0., 0., 1.))), 0.), 32.);
    float k = clamp((dot(w_n, L) - dot(vec3(0., 0., 1.), L))*.35 + sp*1.5, -1., 1.)*${f1(WAVE.light)}*.5;
    c = k > 0. ? c*(1. - k) + vec4(k) : c*(1. + k) + vec4(0., 0., 0., -k);
  }
  o_fx = c;
}`;

  function program(gl, vs, fs){
    const sh = (type, src)=>{
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if(!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if(!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const u = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for(let i = 0; i < n; i++){
      const nm = gl.getActiveUniform(p, i).name;
      u[nm] = gl.getUniformLocation(p, nm);
    }
    return {p, u};
  }

  /** The post pass in this layer, compiled on first use; null if it does not compile. */
  function postProg(Ly){
    if(Ly.pp === undefined){
      try{ Ly.pp = program(Ly.gl, POST_VS, POST_FS); }
      catch(e){ console.warn('token-counter: WebGL post pass did not compile\n'+e.message); Ly.pp = null; }
    }
    return Ly.pp;
  }

  function layer(panel){
    if(layers.has(panel)) return layers.get(panel);
    const cv = document.createElement('canvas');
    cv.className = 'glc';
    cv.setAttribute('aria-hidden', 'true');
    panel.insertBefore(cv, panel.firstChild);
    const gl = cv.getContext('webgl2', {alpha:true, premultipliedAlpha:true, antialias:false,
                                        depth:false, stencil:false});
    if(!gl){ cv.remove(); return null; }
    cv.addEventListener('webglcontextlost', e=>{ e.preventDefault(); ok = false; sync(); });
    let mark;
    try{ mark = program(gl, MARK_VS, MARK_FS); }
    catch(e){ console.warn('token-counter: WebGL marks did not compile\n'+e.message); cv.remove(); return null; }
    const Ly = {cv, gl, mark, pp: undefined, w:0, h:0,
                buf: gl.createBuffer(), vao: gl.createVertexArray(), post: gl.createVertexArray(),
                ms: gl.createFramebuffer(), rb: gl.createRenderbuffer(),
                res: gl.createFramebuffer(), tex: gl.createTexture(),
                samples: Math.min(4, gl.getParameter(gl.MAX_SAMPLES) || 0)};
    gl.bindVertexArray(Ly.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, Ly.buf);
    const pa = gl.getAttribLocation(mark.p, 'p'), ca = gl.getAttribLocation(mark.p, 'c');
    gl.enableVertexAttribArray(pa); gl.vertexAttribPointer(pa, 2, gl.FLOAT, false, 24, 0);
    gl.enableVertexAttribArray(ca); gl.vertexAttribPointer(ca, 4, gl.FLOAT, false, 24, 8);
    gl.bindVertexArray(null);
    layers.set(panel, Ly);
    return Ly;
  }

  function resize(Ly, w, h){
    const gl = Ly.gl;
    Ly.cv.width = Ly.w = w; Ly.cv.height = Ly.h = h;
    gl.bindRenderbuffer(gl.RENDERBUFFER, Ly.rb);
    gl.renderbufferStorageMultisample(gl.RENDERBUFFER, Ly.samples, gl.RGBA8, w, h);
    gl.bindFramebuffer(gl.FRAMEBUFFER, Ly.ms);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, Ly.rb);
    gl.bindTexture(gl.TEXTURE_2D, Ly.tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, Ly.res);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, Ly.tex, 0);
  }

  // -- the water ----------------------------------------------------------------------------
  // A height field on a grid over the viewport, WAVE.cell CSS px a cell: each step, a cell's
  // velocity is pulled toward the mean height of its four neighbours, evened out a little with
  // theirs, and damped; the height follows the velocity.  A fast pointer drags a soft brush
  // through it.  One field serves every canvas, so a ripple crosses the tiles and the charts
  // as one surface; it is kept to the page as it scrolls, a whole cell at a time.  Steps run
  // only while there is motion in it, and stop when it has settled back to flat.
  const SIM = {w: 0, h: 0, H: null, V: null, H2: null, V2: null, live: false, ver: 0,
               sy: 0, acc: 0, t: 0};
  function simFit(){
    const w = Math.ceil(innerWidth/WAVE.cell) + 1, h = Math.ceil(innerHeight/WAVE.cell) + 1;
    if(w === SIM.w && h === SIM.h) return;
    SIM.w = w; SIM.h = h;
    for(const k of ['H', 'V', 'H2', 'V2']) SIM[k] = new Float32Array(w*h);
    SIM.live = false; SIM.ver++;
  }
  function simStep(){
    const {w, h, H, V, H2, V2} = SIM, keep = WAVE.damp, visc = WAVE.visc;
    let e = 0;
    for(let y = 0; y < h; y++){
      const up = (y > 0 ? y-1 : y)*w, dn = (y < h-1 ? y+1 : y)*w, row = y*w;
      for(let x = 0; x < w; x++){
        const i = row + x, lf = x > 0 ? i-1 : i, rt = x < w-1 ? i+1 : i;
        const mh = (H[lf] + H[rt] + H[up+x] + H[dn+x])*.25;
        const mv = (V[lf] + V[rt] + V[up+x] + V[dn+x])*.25;
        let v = V[i] + mh - H[i];
        v = (v + (mv - v)*visc)*keep;
        const hh = Math.max(-1, Math.min(1, (H[i] + v)*keep));
        V2[i] = v; H2[i] = hh;
        e = Math.max(e, Math.abs(hh) + Math.abs(v));
      }
    }
    SIM.H = H2; SIM.H2 = H; SIM.V = V2; SIM.V2 = V;
    return e;
  }
  /** Advance to the present, a fixed step at a time; false once the water is flat again. */
  function simRun(){
    const t = performance.now(), dt = Math.min(64, t - (SIM.t || t));
    SIM.t = t;
    let n = Math.round(dt/1000*60*WAVE.substeps) || 1, e = 1;
    while(n-- > 0) e = simStep();
    SIM.ver++;
    if(e < 2e-3){ SIM.H.fill(0); SIM.V.fill(0); SIM.live = false; }
    return SIM.live;
  }
  /** Push the surface down along a pointer's path from (x0, y0) to (x1, y1), client px. */
  function simStir(x0, y0, x1, y1, amt){
    simFit();
    const {w, h, H} = SIM, c = WAVE.cell, R = WAVE.brush;
    const dx = x1-x0, dy = y1-y0, L2 = dx*dx + dy*dy;
    const gx0 = Math.max(0, Math.floor((Math.min(x0, x1) - R)/c)), gx1 = Math.min(w-1, Math.ceil((Math.max(x0, x1) + R)/c));
    const gy0 = Math.max(0, Math.floor((Math.min(y0, y1) - R)/c)), gy1 = Math.min(h-1, Math.ceil((Math.max(y0, y1) + R)/c));
    for(let gy = gy0; gy <= gy1; gy++) for(let gx = gx0; gx <= gx1; gx++){
      const px = gx*c, py = gy*c;
      const f = L2 > 1e-6 ? Math.max(0, Math.min(1, ((px-x0)*dx + (py-y0)*dy)/L2)) : 0;
      const d = Math.hypot(px - (x0 + dx*f), py - (y0 + dy*f));
      if(d < R){
        const i = gy*w + gx;
        H[i] = Math.max(-1, Math.min(1, H[i] + Math.cos(d/R*Math.PI/2)*amt));
      }
    }
    if(!SIM.live){ SIM.live = true; SIM.t = 0; SIM.sy = scrollY; SIM.acc = 0; }
    SIM.ver++;
  }
  addEventListener('scroll', ()=>{                          // the water stays with the page
    const d = scrollY - SIM.sy;
    SIM.sy = scrollY;
    if(!SIM.live) return;
    SIM.acc += d;
    const n = Math.trunc(SIM.acc/WAVE.cell);
    if(!n) return;
    SIM.acc -= n*WAVE.cell;
    const {w, h} = SIM;
    for(const A of [SIM.H, SIM.V]){
      if(Math.abs(n) >= h){ A.fill(0); continue; }
      if(n > 0){ A.copyWithin(0, n*w); A.fill(0, (h-n)*w); }
      else { A.copyWithin(-n*w, 0, (h+n)*w); A.fill(0, 0, -n*w); }
    }
    SIM.ver++;
  }, {passive: true});

  /** The surface, handed to one canvas whose top-left is at client (bx, by). */
  function water(P, gl, T, bx, by){
    if(!P.u.u_wave) return;
    simFit();
    gl.activeTexture(gl.TEXTURE1);
    if(!T.wtex){
      T.wtex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, T.wtex);
      for(const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
                           [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]])
        gl.texParameteri(gl.TEXTURE_2D, k, v);
    } else gl.bindTexture(gl.TEXTURE_2D, T.wtex);
    if(T.wver !== SIM.ver){
      T.wver = SIM.ver;
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, SIM.w, SIM.h, 0, gl.RED, gl.FLOAT, SIM.H);
    }
    gl.uniform1i(P.u.u_wave, 1);
    gl.uniform2f(P.u.u_wo, bx, by);
    gl.uniform2f(P.u.u_wn, SIM.w, SIM.h);
    gl.activeTexture(gl.TEXTURE0);
  }

  function paint(Ly, panel, groups){
    const gl = Ly.gl, dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cw = panel.clientWidth, ch = panel.clientHeight;
    const pw = Math.max(1, Math.round(cw*dpr)), ph = Math.max(1, Math.round(ch*dpr));
    if(pw !== Ly.w || ph !== Ly.h) resize(Ly, pw, ph);
    const pr = panel.getBoundingClientRect();
    const bx = pr.left + panel.clientLeft, by = pr.top + panel.clientTop;

    // 1. the marks, multisampled
    gl.bindFramebuffer(gl.FRAMEBUFFER, Ly.ms);
    gl.viewport(0, 0, pw, ph);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(Ly.mark.p);
    gl.bindVertexArray(Ly.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, Ly.buf);
    gl.uniform2f(Ly.mark.u.u_css, cw, ch);
    for(const g of groups){
      const svg = g.svg();
      if(!svg) continue;
      const r = svg.getBoundingClientRect();
      if(!r.width) continue;
      const V = [];
      for(const m of g.list) geo(V, m);
      if(!V.length) continue;
      const s = r.width/g.vb[0], ox = r.left - bx, oy = r.top - by;
      if(g.clip){
        const [x, y, w, h] = g.clip;
        gl.enable(gl.SCISSOR_TEST);
        gl.scissor(Math.round((ox + x*s)*dpr), Math.round(ph - (oy + (y+h)*s)*dpr),
                   Math.round(w*s*dpr), Math.round(h*s*dpr));
      } else gl.disable(gl.SCISSOR_TEST);
      gl.uniform2f(Ly.mark.u.u_off, ox, oy);
      gl.uniform1f(Ly.mark.u.u_s, s);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(V), gl.STREAM_DRAW);
      gl.drawArrays(gl.TRIANGLES, 0, V.length/6);
    }
    gl.disable(gl.SCISSOR_TEST);

    // 2. resolved into a texture the post pass can sample
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, Ly.ms);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, Ly.res);
    gl.blitFramebuffer(0, 0, pw, ph, 0, 0, pw, ph, gl.COLOR_BUFFER_BIT, gl.NEAREST);

    // 3. through the water, onto the canvas
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.disable(gl.BLEND);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const P = postProg(Ly);
    if(!P){ ok = false; sync(); return; }
    gl.useProgram(P.p);
    gl.bindVertexArray(Ly.post);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, Ly.tex);
    if(P.u.u_scene) gl.uniform1i(P.u.u_scene, 0);
    if(P.u.u_res) gl.uniform2f(P.u.u_res, pw, ph);
    if(P.u.u_dpr) gl.uniform1f(P.u.u_dpr, dpr);
    water(P, gl, Ly, bx, by);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  const animated = () => on && !REDUCE && SIM.live;
  function tickFrame(){
    loop = 0;
    if(SIM.live && on && !REDUCE) simRun();
    frame(true);
  }

  // -- the headline numbers: their text drawn into a texture, so ripples cross them too ---
  // Each glyph is placed where the browser laid it out (a Range per character), in the
  // element's own computed font, colour, letter-spacing and case; so wrapping, fallback
  // fonts and the theme all come out as the page drew them.  Redrawn only when the text,
  // the size or the style changes; between those, a ripple frame just re-samples it.
  const TX = {cv: null, gl: null, P: null, tex: null, vao: null, pad: null, stamp: ''};
  function textLayer(host){
    if(TX.cv) return TX.gl ? TX : null;
    TX.cv = document.createElement('canvas');
    TX.cv.className = 'glt';
    TX.cv.setAttribute('aria-hidden', 'true');
    host.insertBefore(TX.cv, host.firstChild);
    const gl = TX.cv.getContext('webgl2', {alpha:true, premultipliedAlpha:true, antialias:false,
                                           depth:false, stencil:false});
    try{ TX.P = gl && program(gl, POST_VS, POST_FS); }
    catch(e){ console.warn('token-counter: headline layer did not compile\n'+e.message); TX.P = null; }
    if(!gl || !TX.P){ TX.cv.remove(); return null; }
    TX.gl = gl; TX.tex = gl.createTexture(); TX.vao = gl.createVertexArray();
    TX.pad = document.createElement('canvas');
    return TX;
  }
  function paintText(){
    const host = document.querySelector('.tiles');
    if(!host) return;
    const L = textLayer(host);
    if(!L){ host.classList.remove('gltxt'); return; }
    const gl = L.gl, dpr = Math.min(window.devicePixelRatio || 1, 2);
    const hr = host.getBoundingClientRect();
    const pw = Math.max(1, Math.round(hr.width*dpr)), ph = Math.max(1, Math.round(hr.height*dpr));
    const els = host.querySelectorAll('.tile>*');
    const stamp = [pw, ph, RT.getAttribute('data-style'), matchMedia('(prefers-color-scheme: dark)').matches]
      .concat(Array.from(els, e=>e.textContent)).join('|');
    if(stamp !== L.stamp){
      L.stamp = stamp;
      const pad = L.pad;
      pad.width = pw; pad.height = ph;
      const x = pad.getContext('2d');
      x.scale(dpr, dpr);
      const rg = document.createRange();
      for(const el of els){
        const cs = getComputedStyle(el);
        x.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
        x.fillStyle = cs.color;
        x.textBaseline = 'alphabetic';
        const asc = x.measureText('Hg').fontBoundingBoxAscent || parseFloat(cs.fontSize)*.8;
        const up = cs.textTransform === 'uppercase';
        const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        for(let n = walk.nextNode(); n; n = walk.nextNode()){
          const t = n.nodeValue;
          for(let i = 0; i < t.length; i++){
            if(t[i] === ' ' || t[i] === '\n') continue;
            rg.setStart(n, i); rg.setEnd(n, i+1);
            const r = rg.getBoundingClientRect();
            if(!r.width) continue;
            x.fillText(up ? t[i].toUpperCase() : t[i], r.left - hr.left, r.top - hr.top + asc);
          }
        }
      }
      L.cv.width = pw; L.cv.height = ph;
      gl.bindTexture(gl.TEXTURE_2D, L.tex);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, pad);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      host.classList.add('gltxt');
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, pw, ph);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(L.P.p);
    gl.bindVertexArray(L.vao);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, L.tex);
    if(L.P.u.u_scene) gl.uniform1i(L.P.u.u_scene, 0);
    if(L.P.u.u_res) gl.uniform2f(L.P.u.u_res, pw, ph);
    if(L.P.u.u_dpr) gl.uniform1f(L.P.u.u_dpr, dpr);
    water(L.P, gl, L, hr.left, hr.top);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // A moving pointer stirs the water along its path, harder the faster it went; slower than
  // WAVE.v0 (reading, hovering a tooltip) it leaves the surface alone.  Not a touch, and not
  // a drag of the charts.
  let last = null;
  addEventListener('pointermove', e=>{
    if(!on || REDUCE || e.pointerType === 'touch' || e.buttons){ last = null; return; }
    const t = e.timeStamp || performance.now();
    if(last){
      const dt = Math.max(1, t - last.t), dist = Math.hypot(e.clientX - last.x, e.clientY - last.y);
      last.v = last.v*.6 + dist/dt*.4;
      if(last.v > WAVE.v0 && dist > 0){
        simStir(last.x, last.y, e.clientX, e.clientY,
                Math.min(.6, dist*WAVE.push*Math.min(1, (last.v - WAVE.v0)/WAVE.v0)));
        if(!loop) loop = requestAnimationFrame(tickFrame);
      }
      last.x = e.clientX; last.y = e.clientY; last.t = t;
    } else last = {x: e.clientX, y: e.clientY, t, v: 0};
  }, {passive: true});

  function frame(tick){
    queued = false;
    if(!on) return;
    const byPanel = new Map();
    for(const p of layers.keys()) byPanel.set(p, []);    // a panel left empty is cleared
    for(const [host, g] of SCN){
      const panel = g.svg && host.closest && host.closest('.panel');
      if(!panel) continue;
      if(!byPanel.has(panel)) byPanel.set(panel, []);
      byPanel.get(panel).push(g);
    }
    for(const [panel, groups] of byPanel){
      if(tick){                                           // a clock tick skips what is off screen
        const r = panel.getBoundingClientRect();
        if(r.bottom < 0 || r.top > innerHeight) continue;
      }
      const Ly = layer(panel);
      if(!Ly){ ok = false; sync(); return; }
      paint(Ly, panel, groups);
    }
    const th = document.querySelector('.tiles');
    if(!tick || !th || th.getBoundingClientRect().bottom > 0) paintText();
    if(animated() && !loop) loop = requestAnimationFrame(tickFrame);
  }

  // Drawn in the same task as the SVG it replaces -- a microtask, not a frame later -- so
  // the axes and the marks never part company during a drag.
  function request(){
    if(queued || !on) return;
    queued = true;
    (typeof queueMicrotask === 'function' ? queueMicrotask : f=>Promise.resolve().then(f))(()=>frame(false));
  }

  function sync(){
    on = ok && GL_STYLES.indexOf(RT.getAttribute('data-style')) >= 0;
    if(on) RT.setAttribute('data-gl', ''); else RT.removeAttribute('data-gl');
    pal = null;                                           // a new style is a new palette
    request();
  }

  try{ matchMedia('(prefers-color-scheme: dark)').addEventListener('change', ()=>{ pal = null; request(); }); }
  catch(_){}
  addEventListener('resize', ()=>{ simFit(); request(); });
  GLH.dirty = request;
  return {sync, request};
})();
"""


# The 3D scene.  Under a style in SCENE_STYLES the page is not drawn at all: one canvas fills
# the window with a scene built from the same marks the charts record in SCN (so it cannot
# show a figure the page does not), and the page stays beneath it, unpainted, for assistive
# tech.  Each chart is an exhibit -- the limit windows as panes of glass, the daily input as
# a skyline, the pies as medallions, the headline numbers as words afloat -- and the one
# brought forward stands on the water, square to the camera, while the rest hang in the sky.
# The time charts in front take the same wheel, drag and pinch as on the page, and move the
# one shared viewport.  No WebGL2, a context lost or a shader that will not compile, and the
# page is simply painted again, in the style's own 2D sheet.
SCENE_STYLES = ['nocturne']

SCENE_JS = r"""
// ---- Nocturne: the report as sculptures on night water -----------------------------------
// See SCENE_STYLES in render.py.  Two halves.  N3 is pure -- matrices, the solid each chart
// becomes, where every exhibit stands, what a ray through the pointer touches -- with no DOM
// and no GL, so scripts/test_page.js checks it.  S3D is the WebGL2 runtime around it, and is
// null wherever there is no real DOM or no WebGL2: the page then reads as its 2D sheet.
//
// Every solid is built from what its chart recorded in SCN, in the SVG's own units, placed by
// the plot rectangle it recorded with it: the scene cannot draw a figure the page did not.
const SCENE_STYLES = D.scene_styles || [];

const N3 = (()=>{
  // -- vectors and 4x4 matrices, column-major as GL takes them -------------------------
  const sub = (a, b) => [a[0]-b[0], a[1]-b[1], a[2]-b[2]];
  const add = (a, b) => [a[0]+b[0], a[1]+b[1], a[2]+b[2]];
  const mul3 = (a, k) => [a[0]*k, a[1]*k, a[2]*k];
  const dot = (a, b) => a[0]*b[0] + a[1]*b[1] + a[2]*b[2];
  const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  const norm = a => { const l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0]/l, a[1]/l, a[2]/l]; };
  const lerp = (a, b, k) => a + (b - a)*k;

  function mul(a, b){
    const o = new Array(16);
    for(let c = 0; c < 4; c++) for(let r = 0; r < 4; r++)
      o[c*4+r] = a[r]*b[c*4] + a[4+r]*b[c*4+1] + a[8+r]*b[c*4+2] + a[12+r]*b[c*4+3];
    return o;
  }
  function persp(fy, asp, n, f){
    const t = 1/Math.tan(fy/2), d = n - f;
    return [t/asp,0,0,0, 0,t,0,0, 0,0,(f+n)/d,-1, 0,0,2*f*n/d,0];
  }
  function look(e, t){
    const z = norm(sub(e, t)), x = norm(cross([0, 1, 0], z)), y = cross(z, x);
    return [x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0, -dot(x,e),-dot(y,e),-dot(z,e),1];
  }
  /** T(p) Ry(yaw) Rx(pitch) Rz(roll) S(s) T(-c): an exhibit turned and scaled about its centre c.
   *  Yaw turns its face (+z) toward +x; pitch tips its face down; roll leans it left. */
  function pose(p, yaw, pitch, roll, s, c){
    const cy = Math.cos(yaw), sy = Math.sin(yaw), cx = Math.cos(pitch), sx = Math.sin(pitch);
    const cz = Math.cos(roll), sz = Math.sin(roll);
    const ry = v => [cy*v[0] + sy*v[2], v[1], -sy*v[0] + cy*v[2]];
    const a = ry([cz, sz*cx, sz*sx]), b = ry([-sz, cz*cx, cz*sx]), d = ry([0, -sx, cx]);
    const t = [0, 1, 2].map(i => p[i] - s*(a[i]*c[0] + b[i]*c[1] + d[i]*c[2]));
    return [a[0]*s,a[1]*s,a[2]*s,0, b[0]*s,b[1]*s,b[2]*s,0, d[0]*s,d[1]*s,d[2]*s,0, t[0],t[1],t[2],1];
  }
  function inv(m){
    const [a00,a01,a02,a03,a10,a11,a12,a13,a20,a21,a22,a23,a30,a31,a32,a33] = m;
    const b00 = a00*a11 - a01*a10, b01 = a00*a12 - a02*a10, b02 = a00*a13 - a03*a10;
    const b03 = a01*a12 - a02*a11, b04 = a01*a13 - a03*a11, b05 = a02*a13 - a03*a12;
    const b06 = a20*a31 - a21*a30, b07 = a20*a32 - a22*a30, b08 = a20*a33 - a23*a30;
    const b09 = a21*a32 - a22*a31, b10 = a21*a33 - a23*a31, b11 = a22*a33 - a23*a32;
    const det = b00*b11 - b01*b10 + b02*b09 + b03*b08 - b04*b07 + b05*b06;
    const k = det ? 1/det : 0;
    return [(a11*b11 - a12*b10 + a13*b09)*k, (a02*b10 - a01*b11 - a03*b09)*k,
            (a31*b05 - a32*b04 + a33*b03)*k, (a22*b04 - a21*b05 - a23*b03)*k,
            (a12*b08 - a10*b11 - a13*b07)*k, (a00*b11 - a02*b08 + a03*b07)*k,
            (a32*b02 - a30*b05 - a33*b01)*k, (a20*b05 - a22*b02 + a23*b01)*k,
            (a10*b10 - a11*b08 + a13*b06)*k, (a01*b08 - a00*b10 - a03*b06)*k,
            (a30*b04 - a31*b02 + a33*b00)*k, (a21*b02 - a20*b04 - a23*b00)*k,
            (a11*b07 - a10*b09 - a12*b06)*k, (a00*b09 - a01*b07 + a02*b06)*k,
            (a31*b01 - a30*b03 - a32*b00)*k, (a20*b03 - a21*b01 + a22*b00)*k];
  }
  const xf = (m, v, w = 1) => [0, 1, 2, 3].map(r => m[r]*v[0] + m[4+r]*v[1] + m[8+r]*v[2] + m[12+r]*w);
  function ndc(vp, p){ const q = xf(vp, p); return [q[0]/q[3], q[1]/q[3], q[2]/q[3], q[3]]; }
  const corners = b => [0, 1, 2, 3, 4, 5, 6, 7].map(i =>
    [i&1 ? b[3] : b[0], i&2 ? b[4] : b[1], i&4 ? b[5] : b[2]]);
  const centre = b => [(b[0]+b[3])/2, (b[1]+b[4])/2, (b[2]+b[5])/2];

  // -- solids: triangles, twelve floats a vertex -----------------------------------------
  // position 3, normal 3, colour 4 (straight alpha), element id (-1 for none), emission.
  // Faces wind counter-clockwise seen from outside, so glass can draw its back faces first.
  const VS = 12;
  const Geo = () => ({v: []});
  function tri(G, a, b, c, n, col, id, em){
    for(const p of [a, b, c]) G.v.push(p[0], p[1], p[2], n[0], n[1], n[2],
                                       col[0], col[1], col[2], col[3], id, em);
  }
  function quad(G, a, b, c, d, n, col, id, em){ tri(G, a, b, c, n, col, id, em); tri(G, a, c, d, n, col, id, em); }
  function box(G, x0, y0, z0, x1, y1, z1, col, id = -1, em = 0, bottom = false){
    quad(G, [x0,y0,z1], [x1,y0,z1], [x1,y1,z1], [x0,y1,z1], [0,0,1], col, id, em);
    quad(G, [x1,y0,z0], [x0,y0,z0], [x0,y1,z0], [x1,y1,z0], [0,0,-1], col, id, em);
    quad(G, [x1,y0,z1], [x1,y0,z0], [x1,y1,z0], [x1,y1,z1], [1,0,0], col, id, em);
    quad(G, [x0,y0,z0], [x0,y0,z1], [x0,y1,z1], [x0,y1,z0], [-1,0,0], col, id, em);
    quad(G, [x0,y1,z1], [x1,y1,z1], [x1,y1,z0], [x0,y1,z0], [0,1,0], col, id, em);
    if(bottom) quad(G, [x0,y0,z0], [x1,y0,z0], [x1,y0,z1], [x0,y0,z1], [0,-1,0], col, id, em);
  }
  /** A round wire through `pts`, one short cylinder per segment. */
  function tube(G, pts, r, col, id = -1, em = 0, sides = 7){
    for(let i = 1; i < pts.length; i++){
      const a = pts[i-1], b = pts[i], t = sub(b, a);
      if(Math.hypot(t[0], t[1], t[2]) < 1e-6) continue;
      const tn = norm(t), up = Math.abs(tn[1]) < .9 ? [0, 1, 0] : [1, 0, 0];
      const u = norm(cross(tn, up)), w = cross(tn, u);
      const ring = k => { const q = k/sides*2*Math.PI; return add(mul3(u, Math.cos(q)), mul3(w, Math.sin(q))); };
      for(let k = 0; k < sides; k++){
        const n0 = ring(k), n1 = ring(k+1);
        quad(G, add(a, mul3(n0, r)), add(a, mul3(n1, r)), add(b, mul3(n1, r)), add(b, mul3(n0, r)),
             norm(add(n0, n1)), col, id, em);
      }
    }
  }
  /** A drum standing on its rim, axis along z, from z0 to z1 -- a slice of it when a1 > a0. */
  function wedge(G, cx, cy, r, a0, a1, z0, z1, col, id){
    const n = Math.max(2, Math.ceil((a1 - a0)/(2*Math.PI)*120));
    const P = (a, z) => [cx + r*Math.cos(a), cy + r*Math.sin(a), z], C = z => [cx, cy, z];
    for(let j = 0; j < n; j++){
      const u = a0 + (a1 - a0)*j/n, w = a0 + (a1 - a0)*(j+1)/n;
      tri(G, C(z1), P(u, z1), P(w, z1), [0, 0, 1], col, id, 0);
      tri(G, C(z0), P(w, z0), P(u, z0), [0, 0, -1], col, id, 0);
      quad(G, P(u, z0), P(w, z0), P(w, z1), P(u, z1), [Math.cos((u+w)/2), Math.sin((u+w)/2), 0], col, id, 0);
    }
    if(a1 - a0 < 2*Math.PI - 1e-9){                       // the two cut faces
      quad(G, C(z0), P(a0, z0), P(a0, z1), C(z1), [Math.sin(a0), -Math.cos(a0), 0], col, id, 0);
      quad(G, C(z1), P(a1, z1), P(a1, z0), C(z0), [-Math.sin(a1), Math.cos(a1), 0], col, id, 0);
    }
  }
  /** A round plinth: a drum on its base, axis along y. */
  function disc(G, cx, cz, r, y0, y1, col, segs = 72){
    for(let j = 0; j < segs; j++){
      const u = j/segs*2*Math.PI, w = (j+1)/segs*2*Math.PI;
      const P = (a, y) => [cx + r*Math.sin(a), y, cz + r*Math.cos(a)];
      tri(G, [cx, y1, cz], P(u, y1), P(w, y1), [0, 1, 0], col, -1, 0);
      quad(G, P(w, y0), P(u, y0), P(u, y1), P(w, y1), [Math.sin((u+w)/2), 0, Math.cos((u+w)/2)], col, -1, 0);
    }
  }

  /** The part of a polyline, x increasing, that lies between x = a and x = b. */
  function clipX(pts, a = 0, b = 1){
    const out = [];
    for(let i = 0; i < pts.length; i++){
      const p = pts[i], q = pts[i+1];
      if(p[0] >= a && p[0] <= b) out.push(p);
      if(!q) break;
      for(const e of [a, b]){
        if((p[0] < e && q[0] > e) || (p[0] > e && q[0] < e)){
          const k = (e - p[0])/(q[0] - p[0]);
          out.push([e, p[1] + (q[1] - p[1])*k]);
        }
      }
    }
    return out;
  }
  /** A polyline cut into dashes of `on` world units with `off` between, as SVG dashes it. */
  function dashes(pts, on, off){
    const out = [];
    let k = 0, left = on, cur = [pts[0]];
    for(let i = 1; i < pts.length; i++){
      let p = pts[i-1].slice();
      const q = pts[i];
      let seg = Math.hypot(q[0]-p[0], q[1]-p[1], q[2]-p[2]);
      while(seg > left){
        const f = left/seg;
        p = [p[0] + (q[0]-p[0])*f, p[1] + (q[1]-p[1])*f, p[2] + (q[2]-p[2])*f];
        seg -= left;
        if(k%2 === 0){ cur.push(p); out.push(cur); }
        k++; left = k%2 ? off : on; cur = [p];
      }
      left -= seg;
      if(k%2 === 0) cur.push(q);
    }
    if(k%2 === 0 && cur.length > 1) out.push(cur);
    return out;
  }

  // -- the exhibits ------------------------------------------------------------------------
  // Local units: the plinth's top is y = 0 and the reading face looks down +z.  Both time
  // charts are the same width, so a moment sits at the same x in each, as it does on the
  // page: PW on a landscape screen, and half that on a portrait one, where the page's own
  // charts also keep their height and give up width.
  const PW = 16, WIN_H = 5.4, DAY_H = 4.2, FIN = .5, BLK = 1;
  const plotWidth = asp => asp >= .8 ? PW : PW/2, N3PW = PW;
  const TIP_Y = .78, HEAD_Y = 1.75;           // over a time chart: the hover's answer, the title
  const PIE_R = 2.5, PIE_T = .5, PIE_LIFT = .7;

  /** A word to be drawn: `h` is its em in world units, (x, y) its baseline point. */
  const word = (s, h, x, y, z, o = {}) => Object.assign({s: String(s), h, x, y, z, al: 'l',
                                                         c: '--fg', a: 1, f: 'sans', ext: 0, id: -1}, o);

  function extent(W, measure){                            // the box the words take up
    let b = null;
    for(const w of W){
      const tw = measure(w.s, w.f, w.h);
      const x0 = w.al === 'c' ? w.x - tw/2 : w.al === 'r' ? w.x - tw : w.x;
      const q = [x0, w.y - w.h*.3, w.z - (w.ext||0), x0 + tw, w.y + w.h*.9, w.z];
      b = b ? [Math.min(b[0], q[0]), Math.min(b[1], q[1]), Math.min(b[2], q[2]),
               Math.max(b[3], q[3]), Math.max(b[4], q[4]), Math.max(b[5], q[5])] : q;
    }
    return b;
  }
  function union(a, b){
    if(!a) return b; if(!b) return a;
    return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.min(a[2], b[2]),
            Math.max(a[3], b[3]), Math.max(a[4], b[4]), Math.max(a[5], b[5])];
  }
  /** The stone a time chart stands on, with a gold rule along its top edge; deeper by `rows`
   *  lines of legend below the dates. */
  function stone(G, cx, x0, x1, z0, z1, rows = 1){
    const y0 = -.6 - .35*rows;
    box(G, x0, y0, z0, x1, 0, z1, cx.col('--n-stone'), -1, 0, true);
    box(G, x0, -.05, z1, x1, 0, z1 + .02, cx.col('--n-trim'), -1, .55);
    return [x0, y0, z0, x1, 0, z1 + .02];
  }
  /** A legend set into the stone's face under the dates: a swatch and a name for each entry,
   *  wrapped onto as many lines as the width needs -- never cut short. */
  function legendLines(items, x0, x1, measure){
    const out = [];
    let x = x0, line = 0;
    for(const g of items){
      const tw = measure(g.s, 'sans', .26);
      if(x > x0 && x + .34 + tw > x1){ x = x0; line++; }
      out.push({g, x, line});
      x += .34 + tw + .6;
    }
    return {out, rows: out.length ? line + 1 : 0};
  }
  function legendOn(G, W, cx, lg, z){
    for(const {g, x, line} of lg.out){
      const y = -.83 - .35*line;
      box(G, x, y - .03, z - .02, x + .22, y + .19, z + .04, cx.col(g.c), -1, .35);
      W.push(word(g.s, .26, x + .34, y, z + .025, {c: '--dim'}));
    }
  }
  /** Date ticks cut into the stone's face: as many as fit without touching. */
  function ticksOn(W, tk, view, z, measure, pw){
    let last = -1e9;
    for(const [t, step] of tk || []){
      const u = (t - view[0])/(view[1] - view[0]);
      if(u < -1e-6 || u > 1 + 1e-6) continue;
      const s = tickLabel(t, step), x = u*pw, tw = measure(s, 'sans', .3);
      if(x - tw/2 < last + .35) continue;
      last = x + tw/2;
      W.push(word(s, .3, x, -.48, z + .025, {al: 'c', c: '--n-trim'}));
    }
  }
  function title(W, s, x, y, z){ if(s) W.push(word(s, .46, x, y, z, {f: 'serif', c: '--fg'})); }
  /** A sentence broken into lines of at most n characters, at spaces. */
  function wrap(s, n = 46){
    const out = [];
    let line = '';
    for(const w of String(s).split(/\s+/).filter(Boolean)){
      if(line && line.length + 1 + w.length > n){ out.push(line); line = w; }
      else line = line ? line + ' ' + w : w;
    }
    if(line) out.push(line);
    return out;
  }

  /** Chart 1: each weekly window a pane of glass cut to its cumulative curve, standing on
   *  edge in a row along the time axis; the server's percentage a gold wire strung in front,
   *  in dashes as on the page; a rod at every reset. */
  function windows(rec, info, cx){
    const G = Geo(), W = [], glass = [], hits = [], PW = info.pw || N3PW;
    const [px, py, pw, ph] = rec.plot, H = WIN_H, zb = -FIN/2 - .45;
    const at = p => [(p[0] - px)/pw, (py + ph - p[1])/ph];
    const lg = legendLines([{s: 'cumulative tokens', c: '--uncached'}, {s: LIMIT, c: '--warn'}],
                           0, PW, cx.measure);
    let bx = stone(G, cx, -2.3, PW + 1.5, -1.5, 1.35, lg.rows);
    legendOn(G, W, cx, lg, 1.35);
    for(const f of [0, .25, .5, .75, 1]){
      if(f) box(G, 0, f*H - .012, zb - .015, PW, f*H + .012, zb + .015, cx.col('--line'), -1, .5);
      const y = Math.max(.08, f*H - .1);                    // the zero sits on the stone, not in it
      W.push(word(big(Math.round(info.vmax*f)), .3, -.3, y, zb, {al: 'r', c: '--dim'}));
      W.push(word(Math.round(100*f) + '%', .3, PW + .3, y, zb, {c: '--warn'}));
    }
    let lastLbl = -1e9;
    for(const m of rec.list){
      const pts = clipX(m.pts.map(at));
      if(pts.length < 2) continue;
      if(m.t === 'area'){
        const V = Geo(), c = cx.col('--uncached', .3), z0 = -FIN/2, z1 = FIN/2;
        for(let i = 1; i < pts.length; i++){
          const [xa, ya] = [pts[i-1][0]*PW, pts[i-1][1]*H], [xb, yb] = [pts[i][0]*PW, pts[i][1]*H];
          if(xb - xa < 1e-6) continue;
          quad(V, [xa,0,z1], [xb,0,z1], [xb,yb,z1], [xa,ya,z1], [0,0,1], c, m.win, 0);
          quad(V, [xb,0,z0], [xa,0,z0], [xa,ya,z0], [xb,yb,z0], [0,0,-1], c, m.win, 0);
          quad(V, [xa,ya,z1], [xb,yb,z1], [xb,yb,z0], [xa,ya,z0], norm([ya - yb, xb - xa, 0]), c, m.win, 0);
        }
        const l = pts[pts.length-1], f0 = pts[0];
        if(l[1] > 1e-4) quad(V, [l[0]*PW,0,z1], [l[0]*PW,0,z0], [l[0]*PW,l[1]*H,z0], [l[0]*PW,l[1]*H,z1], [1,0,0], c, m.win, 0);
        if(f0[1] > 1e-4) quad(V, [f0[0]*PW,0,z0], [f0[0]*PW,0,z1], [f0[0]*PW,f0[1]*H,z1], [f0[0]*PW,f0[1]*H,z0], [-1,0,0], c, m.win, 0);
        glass.push({v: V.v, id: m.win, c: [(f0[0] + l[0])/2*PW, H/3, 0]});
        hits.push({id: m.win, box: [f0[0]*PW, 0, -FIN/2 - .3, l[0]*PW, H, FIN/2 + .5]});
        // The reset: a rod where the window opened, and its date where there is room.
        const raw = at(m.pts[0]);
        if(raw[0] >= 0 && raw[0] <= 1){
          box(G, raw[0]*PW - .02, 0, zb - .02, raw[0]*PW + .02, H + .1, zb + .02, cx.col('--dim'), -1, .4);
          const w = (info.wins || [])[m.win], t0 = w && (w.reset_at != null ? w.reset_at
                     : (w.cum_points && w.cum_points.length ? w.cum_points[0][0] : null));
          if(t0 != null && raw[0]*PW - lastLbl >= 1.6){
            lastLbl = raw[0]*PW;
            W.push(word(day(t0), .26, raw[0]*PW + .08, H + .2, zb, {c: '--dim'}));
          }
        }
      } else if(m.t === 'line' && !m.dash){                     // the curve: a lit edge
        tube(G, pts.map(p => [p[0]*PW, p[1]*H, 0]), .05, cx.col('--uncached'), m.win, .85);
      } else if(m.t === 'line'){                               // the percentage: gold beads
        const zf = FIN/2 + .32;
        for(const run of dashes(pts.map(p => [p[0]*PW, p[1]*H, zf]), .2, .12))
          tube(G, run, .05, cx.col('--warn'), m.win, .7);
      }
    }
    title(W, info.title, -2.1, H + HEAD_Y, zb);
    ticksOn(W, info.tk, info.view, 1.35, cx.measure, PW);
    bx = union(union(bx, [-2.3, 0, -1.5, PW + 1.5, H + (info.title ? HEAD_Y + .5 : TIP_Y + 1), 1.35]), extent(W, cx.measure));
    return {solid: G.v, glass, words: W, box: bx, hits, plot: {w: PW, h: H, z: 0}};
  }

  /** Chart 3: response time -- a lit tube for the median, a dashed one for the p90, over the
   *  same stone and dates as the daily skyline, with one hover target a day.  The rate-limit
   *  events stand behind the tubes as slabs, read on their own scale at the right. */
  function lines(rec, info, cx){
    const G = Geo(), W = [], hits = [], PW = info.pw || N3PW;
    const [px, py, pw, ph] = rec.plot, H = DAY_H, zb = -.6;
    const at = p => [(p[0] - px)/pw, (py + ph - p[1])/ph];
    const lg = legendLines(info.legend || [], 0, PW + .9, cx.measure);
    let bx = stone(G, cx, -2.1, PW + 1.2, -1.2, 1.2, lg.rows);
    legendOn(G, W, cx, lg, 1.2);
    for(const f of [0, .5, 1]){
      if(f) box(G, 0, f*H - .012, zb - .015, PW, f*H + .012, zb + .015, cx.col('--line'), -1, .5);
      if(info.ylab) W.push(word(info.ylab(f), .3, -.3, Math.max(.08, f*H - .1), zb, {al: 'r', c: '--dim'}));
      if(info.yrlab) W.push(word(info.yrlab(f), .3, PW + .2, Math.max(.08, f*H - .1), zb, {c: '--warn'}));
    }
    for(const m of rec.list){
      if(m.t !== 'rect') continue;
      const u0 = Math.max(0, (m.x - px)/pw), u1 = Math.min(1, (m.x + m.w - px)/pw);
      if(u1 - u0 < 1e-5) continue;
      box(G, u0*PW, 0, -.35, u1*PW, (py + ph - m.y)/ph*H, -.12, cx.col(m.c));
    }
    for(const m of rec.list){
      if(m.t !== 'line') continue;
      const pts = clipX(m.pts.map(at));
      if(pts.length < 2) continue;
      const P = pts.map(p => [p[0]*PW, p[1]*H, 0]);
      if(!m.dash) tube(G, P, .05, cx.col(m.c), -1, .85);
      else for(const run of dashes(P, .2, .12)) tube(G, run, .035, cx.col(m.c), -1, .6);
    }
    const v = info.view;
    for(const [a, b] of info.days || []){
      if(!v) break;
      const u0 = Math.max(0, (a - v[0])/(v[1] - v[0])), u1 = Math.min(1, (b - v[0])/(v[1] - v[0]));
      if(u1 - u0 < 1e-5) continue;
      hits.push({id: hits.length, day: a, box: [u0*PW, 0, -.6, u1*PW, H, .6]});
    }
    title(W, info.title, -1, H + HEAD_Y, 0);
    ticksOn(W, info.tk, info.view, 1.2, cx.measure, PW);
    bx = union(union(bx, [-2.1, 0, -1.2, PW + 1.2, H + (info.title ? HEAD_Y + .5 : TIP_Y + 1), 1.2]), extent(W, cx.measure));
    return {solid: G.v, glass: [], words: W, box: bx, hits, plot: {w: PW, h: H, z: 0}};
  }

  /** Chart 2: a skyline -- one block a model a day, stacked, with a sliver of night between. */
  function daily(rec, info, cx){
    const G = Geo(), W = [], hits = [], PW = info.pw || N3PW;
    const [px, py, pw, ph] = rec.plot, H = DAY_H;
    const lg = legendLines(info.legend || [], 0, PW + .9, cx.measure);
    let bx = stone(G, cx, -1.2, PW + 1.2, -1.2, 1.2, lg.rows);
    legendOn(G, W, cx, lg, 1.2);
    const cols = new Map();
    for(const m of rec.list){
      if(m.t !== 'rect') continue;
      const u0 = Math.max(0, (m.x - px)/pw), u1 = Math.min(1, (m.x + m.w - px)/pw);
      if(u1 - u0 < 1e-5) continue;
      const v1 = (py + ph - m.y)/ph, v0 = (py + ph - m.y - m.h)/ph;
      if(!cols.has(m.day)) cols.set(m.day, cols.size);
      const id = cols.get(m.day), gap = Math.min(.018, (v1 - v0)*H/4);
      box(G, u0*PW, v0*H + gap, -BLK/2, u1*PW, v1*H - gap, BLK/2, cx.col(m.c), id);
      const h = hits.find(q => q.id === id);
      if(h) h.box[4] = Math.max(h.box[4], v1*H);
      else hits.push({id, day: m.day, box: [u0*PW, 0, -BLK/2, u1*PW, v1*H, BLK/2]});
    }
    for(const h of hits) h.box[4] = Math.max(h.box[4], .6);   // an idle day is still a target
    title(W, info.title, -1, H + HEAD_Y, 0);
    if(info.peak) W.push(word(info.peak, .28, 0, H + .25, 0, {c: '--dim'}));
    ticksOn(W, info.tk, info.view, 1.2, cx.measure, PW);
    bx = union(union(bx, [-1.2, 0, -1.2, PW + 1.2, H + (info.title ? HEAD_Y + .5 : TIP_Y + 1), 1.2]), extent(W, cx.measure));
    return {solid: G.v, glass: [], words: W, box: bx, hits, plot: {w: PW, h: H, z: 0}};
  }

  /** Charts 3 and 4: a medallion standing over a round plinth, cut into its slices, with
   *  its legend beside it.  Every slice is the same thickness: the angle is the only measure. */
  function medal(rec, info, cx){
    const G = Geo(), W = [], R = PIE_R, cy = R + PIE_LIFT;
    disc(G, 0, 0, R*.62, -.8, 0, cx.col('--n-stone'));
    disc(G, 0, 0, R*.62 + .02, -.05, .012, cx.col('--n-trim'));
    const sl = rec && rec.list[0] ? rec.list[0].slices : [];
    let a = Math.PI/2;                                    // twelve o'clock, then clockwise
    const arcs = [];
    sl.forEach(([f, c], i) => {
      if(!(f > 1e-6)) return;
      const b = a - f*2*Math.PI, mid = (a + b)/2, on = i === info.hot;
      const k = f >= 1 - 1e-9 ? 0 : (on ? .3 : .045);
      const ox = Math.cos(mid)*k, oy = Math.sin(mid)*k, oz = on ? .18 : 0;
      wedge(G, ox, cy + oy, R, b, a, -PIE_T/2 + oz, PIE_T/2 + oz, cx.col(c), i);
      arcs.push([i, b, a]);
      a = b;
    });
    if(!arcs.length){                                     // nothing in range: a hollow ring
      tube(G, Array.from({length: 73}, (_, j) => [Math.cos(j/72*2*Math.PI)*R, cy + Math.sin(j/72*2*Math.PI)*R, 0]),
           .04, cx.col('--line'), -1, .5);
    }
    // The legend: the total first, then one line a slice, each with its swatch.
    const lx = R + 1.1, lh = .5, lg = info.legend || {rows: []};
    const n = lg.rows.length + 1;
    let y = cy + (n - 1)*lh/2;
    if(lg.head) W.push(word(lg.head, .34, lx, y, 0, {c: '--fg', w: 600}));
    for(const r of lg.rows){
      y -= lh;
      box(G, lx, y - .02, -.1, lx + .24, y + .22, .1, cx.col(r.c), r.i, .3);
      W.push(word(r.s, .3, lx + .4, y, 0, {c: '--dim', id: r.i}));
    }
    if(lg.empty) wrap(lg.empty).forEach((l, k, a) =>
      W.push(word(l, .3, lx, cy + ((a.length - 1)/2 - k)*.46, 0, {c: '--dim'})));
    title(W, info.title, -R, 2*R + PIE_LIFT + .75, 0);
    const bx = union([-R - .4, -.8, -R*.62, R + .6, 2*R + PIE_LIFT + .3, R*.62], extent(W, cx.measure));
    return {solid: G.v, glass: [], words: W, box: bx, hits: [], arcs, plot: null,
            medal: {cy, r: R}};
  }

  /** The headline numbers, afloat over a low slab: the masthead, then the tiles, three a row. */
  function ledger(info, cx){
    const G = Geo(), W = [];
    const n = info.tiles.length, cols = 3, rows = Math.ceil(n/cols), cw = 4.9;
    const x0 = -cols*cw/2, top = rows*2.25 + 1.05;          // the notes clear the far shore
    W.push(word(info.kicker, .36, x0, top + 2.45, 0, {f: 'serif', c: '--dim'}));
    W.push(word(info.title, 1.15, x0 - .05, top + 1.1, 0, {f: 'serif', c: '--fg', ext: .22}));
    W.push(word(info.dek, .34, x0, top + .38, 0, {c: '--dim'}));
    info.tiles.forEach((t, i) => {
      const x = x0 + (i % cols)*cw, y = top - .6 - Math.floor(i/cols)*2.25;
      W.push(word(t.k.toUpperCase(), .26, x, y, 0, {c: '--dim', f: 'caps'}));
      W.push(word(t.v, .86, x - .03, y - 1.0, 0, {c: i ? '--fg' : '--warn', ext: .14, w: 300}));
      if(t.n) W.push(word(t.n, .3, x, y - 1.5, 0, {c: '--dim'}));
    });
    box(G, x0 - .6, -.6, -1.1, -x0 + .6, 0, 1.1, cx.col('--n-stone'), -1, 0, true);
    box(G, x0 - .6, -.05, 1.1, -x0 + .6, 0, 1.12, cx.col('--n-trim'), -1, .55);
    if(info.brand) W.push(word(info.brand, .26, 0, -.4, 1.13, {al: 'c', c: '--n-trim', f: 'serif'}));
    const bx = union([x0 - .6, -.6, -1.1, -x0 + .6, 0, 1.12], extent(W, cx.measure));
    return {solid: G.v, glass: [], words: W, box: bx, hits: [], plot: null};
  }

  /** Nothing to draw: a stone as wide as its words, the title, and the page's own reason. */
  function empty(msg, info, cx){
    const G = Geo(), W = [], lines = wrap(msg);
    title(W, info.title, 0, .75 + lines.length*.48 + .5, 0);
    lines.forEach((l, k) => W.push(word(l, .32, 0, .75 + (lines.length - 1 - k)*.48, 0, {c: '--dim'})));
    const w = extent(W, cx.measure)[3];
    const bx0 = stone(G, cx, -.8, w + .8, -1.2, 1.2);
    return {solid: G.v, glass: [], words: W, box: union(bx0, extent(W, cx.measure)), hits: [], plot: null};
  }

  /** The style switch's dot as a coin: its face, the white ring round it, and a hairline --
   *  the proportions of the dot on the page (18px, a 3px ring, a 1px line), radius 1. */
  function coin(face, ring, line){
    const G = Geo();
    wedge(G, 0, 0, 1.44, 0, 2*Math.PI, -.14, .06, line, -1);
    wedge(G, 0, 0, 1.33, 0, 2*Math.PI, -.12, .1, ring, -1);
    wedge(G, 0, 0, 1, 0, 2*Math.PI, -.1, .16, face, -1);
    return G.v;
  }

  // -- where everything stands ------------------------------------------------------------
  // The camera does not fly: it stands on the bank at about the height of an exhibit's middle,
  // and the exhibits come to it.  The one in front hovers over the water, as close as it can
  // come while it stays on screen and under the sky's row, so the water below it holds its
  // whole reflection; the others hang in the sky, in a row -- or in two, on a narrow screen.
  const CAM = {eye: [0, 1.45, 27], at: [0, 3.2, 0]}, FLOAT = .6, SKY_Z = -46;
  function fovFor(asp){
    const f = 40*Math.PI/180;                             // vertical, on a landscape screen
    const hf = 2*Math.atan(Math.tan(f/2)*asp);
    return hf >= 56*Math.PI/180 ? f : Math.min(1.45, 2*Math.atan(Math.tan(28*Math.PI/180)/asp));
  }
  function camera(asp, orb){
    const fov = fovFor(asp), o = orb || {yaw: 0, pitch: 0, dolly: 1, pivot: CAM.at};
    const pv = o.pivot || CAM.at;
    const rot = v => {                                    // turn about the pivot: tilt, then swing
      let [x, y, z] = sub(v, pv);
      const cp = Math.cos(o.pitch || 0), sp = Math.sin(o.pitch || 0);
      [y, z] = [y*cp + z*sp, -y*sp + z*cp];
      const cy = Math.cos(o.yaw || 0), sy = Math.sin(o.yaw || 0);
      [x, z] = [x*cy + z*sy, -x*sy + z*cy];
      return add(pv, [x, y, z]);
    };
    const at = rot(CAM.at);
    let eye = rot(CAM.eye);
    eye = add(at, mul3(sub(eye, at), o.dolly || 1));
    const V = look(eye, at), P = persp(fov, asp, .5, 900);
    return {eye, at, V, P, VP: mul(P, V), fov, asp};
  }
  /** Where the horizon crosses the screen, in NDC y. */
  const horizon = cam => ndc(cam.VP, [cam.eye[0], cam.eye[1], cam.eye[2] - 5000])[1];

  function fits(M, box, vp, x0, x1, y0, y1){
    for(const c of corners(box)){
      const q = ndc(vp, xf(M, c));
      if(q[3] <= .5 || q[0] < x0 || q[0] > x1 || q[1] < y0 || q[1] > y1) return false;
    }
    return true;
  }
  /** The front of the stage: squarely facing the camera, resting FLOAT above the water, and as
   *  near as the box allows while it stays on screen and under the sky's row (FORE_TOP). */
  const FORE_TOP = .46;
  function fore(box, cam){
    const c = centre(box), top = FORE_TOP;
    const y = FLOAT + (c[1] - box[1]);
    let lo = -400, hi = cam.eye[2] - 2;
    for(let i = 0; i < 48; i++){
      const z = (lo + hi)/2;
      if(fits(pose([0, y, z], 0, 0, 0, 1, c), box, cam.VP, -.93, .93, -.9, top)) lo = z; else hi = z;
    }
    return {p: [0, y, lo], yaw: 0, pitch: 0, roll: 0, s: 1, lit: 1};
  }
  /** The sky slots, as NDC cells above the horizon, and the world point at each one's centre. */
  function slots(n, cam){
    if(n <= 0) return [];
    const y0 = FORE_TOP + .09, y1 = .95;
    const cols = cam.asp >= 1.15 ? n : Math.min(n, 2), rows = Math.ceil(n/cols);
    const IV = inv(cam.VP), out = [];
    for(let i = 0; i < n; i++){
      const r = Math.floor(i/cols), inRow = Math.min(cols, n - r*cols), c = i - r*cols;
      const cw = 1.84/cols, ch = (y1 - y0)/rows;
      const x = -inRow*cw/2 + (c + .5)*cw, y = y1 - (r + .5)*ch;
      const a = xf(IV, [x, y, -1]), b = xf(IV, [x, y, 1]);
      const pa = mul3(a, 1/a[3]), pb = mul3(b, 1/b[3]);
      const k = (SKY_Z - pa[2])/(pb[2] - pa[2]);
      out.push({p: add(pa, mul3(sub(pb, pa), k)), cw, ch, ndc: [x, y]});
    }
    return out;
  }
  function skyPose(box, slot, cam){
    const c = centre(box), p = slot.p, d = sub(cam.eye, p);
    const yaw = Math.atan2(d[0], d[2]), pitch = Math.atan2(-d[1], Math.hypot(d[0], d[2]))*.8;
    let s = 1;
    for(let i = 0; i < 3; i++){                            // near enough linear this far out
      let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
      const M = pose(p, yaw, pitch, 0, s, c);
      for(const q of corners(box)){
        const v = ndc(cam.VP, xf(M, q));
        x0 = Math.min(x0, v[0]); x1 = Math.max(x1, v[0]); y0 = Math.min(y0, v[1]); y1 = Math.max(y1, v[1]);
      }
      s *= Math.min(slot.cw*.8/(x1 - x0), slot.ch*.66/(y1 - y0));
    }
    return {p, yaw, pitch, roll: 0, s, lit: .62};
  }

  /** A pose between two, at k in [0, 1].  An exhibit coming down falls like a leaf, swinging
   *  side to side as it drops; one going up rises like a lantern let go, drifting wide of it. */
  function tween(a, b, k, kind){
    const e = k < .5 ? 4*k*k*k : 1 - Math.pow(-2*k + 2, 3)/2;
    const o = {p: [0, 1, 2].map(i => lerp(a.p[i], b.p[i], e)),
               yaw: lerp(a.yaw, b.yaw, e), pitch: lerp(a.pitch, b.pitch, e),
               roll: lerp(a.roll || 0, b.roll || 0, e), s: lerp(a.s, b.s, e),
               lit: lerp(a.lit, b.lit, e)};
    const span = Math.hypot(b.p[0] - a.p[0], b.p[1] - a.p[1], b.p[2] - a.p[2]);
    if(kind === 'down'){
      const sw = Math.sin(3*Math.PI*e)*(1 - e);
      o.p[0] += sw*Math.min(4, span*.1);
      o.roll += sw*.32;
      o.yaw += sw*.25;
    } else if(kind === 'up'){
      const arc = Math.sin(Math.PI*e);
      o.p[0] += arc*Math.min(7, span*.16)*(b.p[0] >= 0 ? -1 : 1);
      o.p[2] -= arc*Math.min(8, span*.18);
      o.roll -= arc*.12;
    }
    return o;
  }

  // -- what the pointer touches -------------------------------------------------------------
  function rayBox(o, d, b){
    let t0 = -1e9, t1 = 1e9;
    for(let i = 0; i < 3; i++){
      if(Math.abs(d[i]) < 1e-12){ if(o[i] < b[i] || o[i] > b[i+3]) return null; continue; }
      let a = (b[i] - o[i])/d[i], c = (b[i+3] - o[i])/d[i];
      if(a > c) [a, c] = [c, a];
      t0 = Math.max(t0, a); t1 = Math.min(t1, c);
      if(t0 > t1) return null;
    }
    return t1 < 0 ? null : Math.max(t0, 0);
  }
  /** The ray in an exhibit's own units.  The parameter t is the same in both. */
  function local(Mi, o, d){ return [xf(Mi, o).slice(0, 3), xf(Mi, d, 0).slice(0, 3)]; }
  /** Where the ray crosses the plane z = zp, in local units. */
  function onPlane(o, d, zp){
    if(Math.abs(d[2]) < 1e-9) return null;
    const t = (zp - o[2])/d[2];
    return t < 0 ? null : [o[0] + d[0]*t, o[1] + d[1]*t, t];
  }
  /** The slice under local point (x, y) on a medallion's face, or -1. */
  function sliceAt(x, y, m){
    const dx = x, dy = y - m.medal.cy;
    if(Math.hypot(dx, dy) > m.medal.r + .35) return -1;
    let a = Math.atan2(dy, dx);
    for(const [i, lo, hi] of m.arcs){
      for(const k of [-1, 0, 1]) if(a + k*2*Math.PI >= lo && a + k*2*Math.PI <= hi) return i;
    }
    return -1;
  }

  return {mul, persp, look, pose, inv, xf, ndc, corners, centre, VS,
          windows, daily, lines, medal, ledger, empty, coin, plotWidth, wrap, fore, slots, skyPose, tween, camera, horizon,
          fovFor, rayBox, local, onPlane, sliceAt, clipX, dashes, PW, WIN_H, DAY_H, CAM, FLOAT, FORE_TOP,
          TIP_Y};
})();

// ---- the scene, in WebGL2 -------------------------------------------------------------------
const S3D = (()=>{
  if(typeof document === 'undefined' || !document.createElement || !document.querySelector
     || typeof WebGL2RenderingContext === 'undefined') return null;
  const RT = document.documentElement;
  let cv = null, gl = null, ok = true, on = false, raf = 0, need = true, laid = false;
  let PR = null, pal = {}, serif = 'Georgia,serif', T0 = performance.now();
  let cam = null, base = null, wpx = 0, hpx = 0, dpr = 1;
  let F = 0, hov = null, hint = 1;
  const EX = [], RIP = [], words = new Map();
  const orb = {yaw: 0, pitch: 0, dolly: 1, gy: 0, gp: 0, gd: 1};

  // -- colour: the style's own variables, as the WebGL layer reads them ---------------------
  function parse(v){
    let m = /^#([0-9a-f]{3,8})$/i.exec(v);
    if(m){
      let h = m[1];
      if(h.length < 5) h = h.split('').map(c=>c+c).join('');
      const n = i => parseInt(h.slice(i, i+2), 16)/255;
      return [n(0), n(2), n(4), h.length >= 8 ? n(6) : 1];
    }
    m = /rgba?\(([^)]+)\)/.exec(v);
    if(m){
      const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(parseFloat);
      return [p[0]/255, p[1]/255, p[2]/255, p.length > 3 ? p[3] : 1];
    }
    return [.5, .5, .5, 1];
  }
  function col(name, a = 1){
    if(!(name in pal)) pal[name] = parse(getComputedStyle(RT).getPropertyValue(name).trim());
    const c = pal[name];
    return [c[0], c[1], c[2], c[3]*a];
  }
  const lin = (name, k = 1) => col(name).slice(0, 3).map(v => Math.pow(v, 2.2)*k);

  // -- words: each string drawn once into a texture, white, and tinted as it is placed -------
  const pad = document.createElement('canvas'), pctx = pad.getContext('2d');
  const RASTER = h => h >= .7 ? 150 : 84;
  function font(f, w, px){
    if(f === 'serif') return `italic 400 ${px}px ${serif}`;
    return `${f === 'caps' ? 600 : (w || 400)} ${px}px system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif`;
  }
  function spacing(f){ return f === 'caps' ? .14 : 0; }
  function textW(s, f, w, px){
    pctx.font = font(f, w, px);
    return pctx.measureText(s).width + spacing(f)*px*Math.max(0, s.length - 1);
  }
  const mwidth = (s, f, h, w) => textW(String(s), f, w, RASTER(h))/RASTER(h)*h;
  function tex(wd){
    const px = RASTER(wd.h), key = `${wd.f}|${wd.w||''}|${px}|${wd.s}`;
    let t = words.get(key);
    if(t) return t;
    if(words.size > 700){ for(const v of words.values()) gl.deleteTexture(v.tex); words.clear(); }
    const tw = textW(wd.s, wd.f, wd.w, px), padx = Math.ceil(px*.2);
    const W = Math.max(2, Math.ceil(tw) + 2*padx), H = Math.ceil(px*1.4);
    pad.width = W; pad.height = H;
    pctx.clearRect(0, 0, W, H);
    pctx.font = font(wd.f, wd.w, px);
    pctx.fillStyle = '#fff';
    pctx.textBaseline = 'alphabetic';
    const sp = spacing(wd.f)*px;
    if(sp){
      let x = padx;
      for(const ch of wd.s){ pctx.fillText(ch, x, px*1.05); x += pctx.measureText(ch).width + sp; }
    } else pctx.fillText(wd.s, padx, px*1.05);
    const g = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, g);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, pad);
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    t = {tex: g, qw: W/px, qh: H/px, tw: tw/px, pad: padx/px, base: (H - px*1.05)/px};
    words.set(key, t);
    return t;
  }

  // -- programs ---------------------------------------------------------------------------------
  const FOG = `uniform vec3 u_eye, u_fog; uniform float u_fogk;
vec3 fogged(vec3 c, vec3 w){ return mix(c, u_fog, 1. - exp(-max(0., length(w - u_eye) - 30.)*u_fogk)); }`;
  const MESH_VS = `#version 300 es
in vec3 a_p; in vec3 a_n; in vec4 a_c; in vec2 a_x;
uniform mat4 u_vp, u_m; uniform float u_mir;
out vec3 v_w, v_n, v_l; out vec4 v_c; out vec2 v_x;
void main(){
  vec4 w = u_m*vec4(a_p, 1.);
  vec3 n = mat3(u_m)*a_n;
  if(u_mir > .5){ w.y = -w.y; n.y = -n.y; }
  v_w = w.xyz; v_n = n; v_l = a_p; v_c = a_c; v_x = a_x;
  gl_Position = u_vp*w;
}`;
  // Lit in linear light: a warm lamp from the bank behind the viewer, the sky's blue from above
  // and the water's dark from below, a cool rim where a face turns away.  A face squarely in
  // front reads at its own colour.  Glass takes its opacity from the angle it is seen at.
  const MESH_FS = `#version 300 es
precision highp float;
in vec3 v_w, v_n, v_l; in vec4 v_c; in vec2 v_x;
uniform vec3 u_key, u_lamp, u_sky, u_gnd, u_rim;
uniform float u_lit, u_hi, u_glass, u_mir, u_ao, u_a;
${FOG}
out vec4 o;
void main(){
  vec3 N = normalize(v_n), V = normalize(u_eye - v_w);
  if(u_glass > .5 && (u_mir > .5 ? gl_FrontFacing : !gl_FrontFacing)) N = -N;
  vec3 A = pow(v_c.rgb, vec3(2.2));
  float hi = (u_hi > -.5 && abs(v_x.x - u_hi) < .5) ? 1. : 0.;
  float nl = max(dot(N, u_key), 0.);
  vec3 amb = mix(u_gnd, u_sky, N.y*.5 + .5);
  float sp = pow(max(dot(N, normalize(u_key + V)), 0.), 60.);
  float fr = pow(1. - abs(dot(N, V)), 4.);
  float ao = (u_ao > .5 && v_l.y > -.001) ? mix(.55, 1., smoothstep(0., 1.2, v_l.y)) : 1.;
  vec3 c = A*(amb + u_lamp*nl)*ao + u_lamp*sp*(.1 + .9*u_glass) + u_rim*fr*.45;
  c = mix(c, A*1.3 + .03, v_x.y);
  c = c*u_lit + A*hi*.45;
  c = fogged(pow(c, vec3(1./2.2)), v_w);
  float a = v_c.a*u_a;
  if(u_glass > .5) a = min(.9, a + fr*.45 + sp*.5 + hi*.2);
  if(u_mir > .5) a *= exp(v_w.y*.035);
  o = vec4(c*a, a);
}`;
  const TEXT_VS = `#version 300 es
in vec2 a_q;
uniform mat4 u_vp, u_m; uniform vec3 u_o; uniform vec2 u_sz; uniform float u_mir;
out vec2 v_uv; out vec3 v_w;
void main(){
  vec4 w = u_m*vec4(u_o + vec3(a_q*u_sz, 0.), 1.);
  if(u_mir > .5) w.y = -w.y;
  v_uv = vec2(a_q.x, 1. - a_q.y); v_w = w.xyz;
  gl_Position = u_vp*w;
}`;
  const TEXT_FS = `#version 300 es
precision highp float;
in vec2 v_uv; in vec3 v_w;
uniform sampler2D u_tex; uniform vec4 u_c; uniform float u_mir;
${FOG}
out vec4 o;
void main(){
  float a = texture(u_tex, v_uv).a*u_c.a;
  if(u_mir > .5) a *= exp(v_w.y*.035);
  o = vec4(fogged(u_c.rgb, v_w)*a, a);
}`;
  const FULL_VS = `#version 300 es
out vec2 v_p;
void main(){
  vec2 p = vec2(float((gl_VertexID<<1)&2), float(gl_VertexID&2))*2. - 1.;
  v_p = p; gl_Position = vec4(p, 1., 1.);
}`;
  // The sky: night blue over a band of river mist, and the far shore -- a low dark line with
  // gas lamps along it -- which the water doubles.
  const SKY_FS = `#version 300 es
precision highp float;
in vec2 v_p;
uniform mat4 u_ivp; uniform vec3 u_eye, u_zen, u_sky, u_haze, u_shore, u_lamp; uniform float u_t, u_mir;
out vec4 o;
float h1(float x){ return fract(sin(x*127.1)*43758.5453); }
void main(){
  vec4 f = u_ivp*vec4(v_p, 1., 1.);
  vec3 d = normalize(f.xyz/f.w - u_eye);
  if(u_mir > .5) d.y = -d.y;
  float e = d.y;
  vec3 c = mix(u_haze, u_sky, smoothstep(0., .2, e));
  c = mix(c, u_zen, smoothstep(.16, .8, e));
  c += u_haze*.5*exp(-abs(e)*60.);
  float az = atan(d.x, -d.z);
  float sh = .0045 + .0075*(.5 + .5*sin(az*4.3 + 1.3))*(.7 + .3*sin(az*15.1)) + .0015*sin(az*47.);
  float land = smoothstep(sh + .0006, sh - .0006, e)*step(-.002, e);
  c = mix(c, u_shore, land*.72);
  float k = az*80., id = floor(k);
  if(h1(id) > .55){
    float ax = (id + .5 + (h1(id + 3.) - .5)*.7)/80., ay = sh*(.2 + .6*h1(id + 9.));
    vec2 q = vec2(az - ax, e - ay);
    float g = exp(-dot(q, q)/2.4e-6) + .3*exp(-dot(q, q)/3.e-5);
    c += u_lamp*g*(.75 + .25*sin(u_t*(.7 + 1.6*h1(id + 5.)) + id))*step(-.0015, e);
  }
  float n = fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233)))*43758.5453);
  o = vec4(c + (n - .5)/255., 1.);
}`;
  const WATER_VS = `#version 300 es
in vec2 a_q; uniform mat4 u_vp; out vec3 v_w;
void main(){ v_w = vec3(a_q.x, 0., a_q.y); gl_Position = u_vp*vec4(v_w, 1.); }`;
  // Still water: the scene mirrored in it, wavering with a slow swell and with the rings an
  // exhibit sets going where it comes down; darker where you look into it, brighter at a
  // glance, and lost in the mist toward the far shore.
  const WATER_FS = `#version 300 es
precision highp float;
in vec3 v_w;
uniform sampler2D u_ref; uniform vec2 u_res; uniform vec3 u_water, u_lamp, u_pool;
uniform float u_t; uniform vec4 u_rip[4];
${FOG}
out vec4 o;
void main(){
  vec2 xz = v_w.xz;
  vec2 g = vec2(.8, .3)*sin(dot(xz, vec2(.8, .3))*1.1 + u_t*.7)*.5
         + vec2(-.3, .95)*sin(dot(xz, vec2(-.3, .95))*1.9 + u_t*1.05)*.3
         + vec2(.6, -.8)*sin(dot(xz, vec2(.6, -.8))*3.1 + u_t*1.5)*.2;
  g *= .012;
  for(int i = 0; i < 4; i++){
    vec4 r = u_rip[i];
    float age = u_t - r.z;
    if(r.w <= 0. || age < 0. || age > 8.) continue;
    vec2 q = xz - r.xy;
    float L = length(q) + 1e-4, x = L - age*3.4;
    g += q/L*sin(x*2.6)*exp(-x*x*.22)*exp(-age*.5)*r.w*.05;
  }
  float dist = length(v_w - u_eye);
  vec2 uv = gl_FragCoord.xy/u_res, off = vec2(g.x*.5, g.y*1.6)*14./(10. + dist);
  vec3 r = texture(u_ref, uv + off).rgb*.5 + texture(u_ref, uv + off*1.7 + vec2(0., .0025)).rgb*.25
         + texture(u_ref, uv + off*.4 - vec2(0., .0025)).rgb*.25;
  vec3 V = normalize(u_eye - v_w);
  float fr = pow(1. - max(V.y, 0.), 5.);
  vec3 c = mix(u_water, r, mix(.55, .95, fr));
  vec2 pq = xz - u_pool.xy;
  c += u_lamp*u_pool.z*.05*exp(-dot(pq, pq)/60.);
  o = vec4(fogged(c, v_w), 1.);
}`;
  // The Falling Rocket: gold sparks drifting down the dark, well behind the one in front.
  const SPARK_VS = `#version 300 es
in vec4 a_s;
uniform mat4 u_vp; uniform float u_t, u_mir, u_ps;
out float v_a;
void main(){
  float top = 34., y = mod(a_s.y - u_t*a_s.w, top);
  vec3 p = vec3(a_s.x + sin(u_t*.21 + a_s.z)*1.3, y + .1, a_s.z + cos(u_t*.17 + a_s.x)*.9);
  if(u_mir > .5) p.y = -p.y;
  vec4 c = u_vp*vec4(p, 1.);
  gl_Position = c;
  float tw = .55 + .45*sin(u_t*(1.5 + fract(a_s.x*7.3)*3.) + a_s.z*5.);
  v_a = tw*smoothstep(0., 2., y)*smoothstep(top, top - 5., y)*(u_mir > .5 ? .6 : 1.);
  gl_PointSize = clamp(u_ps/c.w, 1., 5.);
}`;
  const SPARK_FS = `#version 300 es
precision highp float;
in float v_a; uniform vec3 u_c; out vec4 o;
void main(){
  vec2 q = gl_PointCoord*2. - 1.;
  float a = exp(-dot(q, q)*3.5)*v_a;
  o = vec4(u_c*a, a);
}`;

  function program(vs, fs){
    const sh = (type, src)=>{
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if(!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if(!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const u = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for(let i = 0; i < n; i++){
      const nm = gl.getActiveUniform(p, i).name.replace(/\[0\]$/, '');
      u[nm] = gl.getUniformLocation(p, nm);
    }
    return {p, u};
  }

  function mesh(v){                                       // a vertex array, ready to draw
    const vao = gl.createVertexArray(), buf = gl.createBuffer();
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v), gl.STATIC_DRAW);
    const st = N3.VS*4, pr = PR.mesh.p;
    [['a_p', 3, 0], ['a_n', 3, 12], ['a_c', 4, 24], ['a_x', 2, 40]].forEach(([nm, k, off])=>{
      const l = gl.getAttribLocation(pr, nm);
      if(l < 0) return;
      gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l, k, gl.FLOAT, false, st, off);
    });
    gl.bindVertexArray(null);
    return {vao, buf, n: v.length/N3.VS};
  }
  function drop(m){ if(m){ gl.deleteVertexArray(m.vao); gl.deleteBuffer(m.buf); } }

  function init(){
    cv = document.createElement('canvas');
    cv.className = 's3d';
    cv.setAttribute('aria-hidden', 'true');
    document.body.appendChild(cv);
    gl = cv.getContext('webgl2', {antialias: true, alpha: false, depth: true, stencil: false,
                                  premultipliedAlpha: true, powerPreference: 'high-performance'});
    if(!gl){ cv.remove(); cv = null; return false; }
    cv.addEventListener('webglcontextlost', e=>{ e.preventDefault(); ok = false; sync(); });
    try{
      PR = {mesh: program(MESH_VS, MESH_FS), text: program(TEXT_VS, TEXT_FS),
            sky: program(FULL_VS, SKY_FS), water: program(WATER_VS, WATER_FS),
            spark: program(SPARK_VS, SPARK_FS)};
    }catch(e){
      console.warn('token-counter: the Nocturne scene did not compile\n' + e.message);
      cv.remove(); cv = null; gl = null; return false;
    }
    // a unit quad for words, a sheet for the water, the sparks, and the reflection target
    const q = (arr, pr, nm, k)=>{
      const vao = gl.createVertexArray(), b = gl.createBuffer();
      gl.bindVertexArray(vao); gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(arr), gl.STATIC_DRAW);
      const l = gl.getAttribLocation(pr.p, nm);
      gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l, k, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
      return {vao, n: arr.length/k};
    };
    PR.quad = q([0,0, 1,0, 1,1, 0,0, 1,1, 0,1], PR.text, 'a_q', 2);
    const S = 3000;
    PR.sheet = q([-S,-S, S,-S, S,S, -S,-S, S,S, -S,S], PR.water, 'a_q', 2);
    let seed = 7;
    const rnd = () => (seed = (seed*16807) % 2147483647)/2147483647;
    const sp = [];
    for(let i = 0; i < 340; i++) sp.push((rnd() - .5)*150, rnd()*34, -12 - rnd()*95, .25 + rnd()*.7);
    PR.sparks = q(sp, PR.spark, 'a_s', 4);
    PR.full = gl.createVertexArray();
    const btn = byId('stylebtn');
    if(btn){
      btn.addEventListener('pointerenter', ()=>{ SW.goal += Math.PI; req(); });
      btn.addEventListener('pointerleave', ()=>{ SW.goal += Math.PI; req(); });
    }
    PR.ref = {fb: gl.createFramebuffer(), tex: gl.createTexture(), rb: gl.createRenderbuffer(), w: 0, h: 0};
    bind(cv);
    return true;
  }

  // -- the exhibits -------------------------------------------------------------------------------
  const TITLES = {
    windows: '',                                       // the time charts need no heading
    daily: '',
    latency: '',
    content: 'What filled the window',
    models: TK ? 'Input by model' : 'Recorded input by model',
  };
  const NAMES = {ledger: 'The numbers', windows: LIMIT[0].toUpperCase() + LIMIT.slice(1) + ' windows',
                 daily: 'Daily input', latency: 'Response time',
                 content: 'What filled the window', models: 'Input by model'};
  function exhibits(){
    EX.length = 0;
    const panels = document.querySelectorAll('.wrap > .panel');
    const add = (key, host, panel) => EX.push({key, host, panel, name: NAMES[key], src: undefined,
                                               m: null, g: null, glass: [], pose: null, from: null,
                                               to: null, t0: 0, dur: 0, kind: '', slot: EX.length - 1,
                                               phase: EX.length*1.7, hot: -1, built: ''});
    add('ledger', null, null);
    add('windows', byId('rlchart'), panels[0] || null);
    add('daily', byId('dailychart'), byId('dailychart') && byId('dailychart').closest('.panel'));
    if(byId('latchart')) add('latency', byId('latchart'), byId('latchart').closest('.panel'));
    if(byId('catpie')) add('content', byId('catpie'), null);
    if(byId('modelpie')) add('models', byId('modelpie'), null);
  }
  const text = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  const swatch = el => { const i = el.querySelector('i'); return i ? varOf(i.getAttribute('style')) : '--dim'; };
  function legendOf(host){
    const rows = [], out = {head: '', rows};
    if(!host) return out;
    for(const sp of host.querySelectorAll('.legend span')){
      const i = sp.getAttribute('data-i'), sw = sp.querySelector('i');
      if(i === null && !sw){ out.head = text(sp); continue; }
      rows.push({i: i === null ? -1 : +i, s: text(sp), c: swatch(sp)});
    }
    if(!rows.length && !out.head) out.empty = text(host.querySelector('.sub'));
    return out;
  }
  let tkPx = 900, pw = N3.PW;
  function build(ex){
    const cx = {col, measure: mwidth};
    const rec = ex.host ? SCN.get(ex.host) : null;
    const stamp = [rec ? 1 : 0, ex.hot, tkPx, pw, VIEW ? VIEW.join() : ''].join('|');
    if(ex.m && ex.src === rec && ex.built === stamp) return false;
    ex.src = rec; ex.built = stamp;
    const tk = VIEW ? ticks(Math.max(120, tkPx)) : [];
    let m;
    if(ex.key === 'ledger'){
      const k = document.querySelector('.kicker');
      let kick = '';
      try{ kick = getComputedStyle(k, '::before').content.replace(/^["']|["']$/g, '').replace(/\\"/g, '"'); }catch(_){}
      m = N3.ledger({kicker: kick === 'none' ? '' : kick, title: text(document.querySelector('.mast h1')),
                     dek: text(document.querySelector('.dek')), brand: text(document.querySelector('.brand')),
                     tiles: Array.from(document.querySelectorAll('.tiles .tile'), t => ({
                       k: text(t.querySelector('.k')), v: text(t.querySelector('.v')),
                       n: text(t.querySelector('.n'))}))}, cx);
    } else if(ex.key === 'windows'){
      m = rec ? N3.windows(rec, {vmax: VMAX, wins: WINS, tk, view: VIEW, pw, title: TITLES.windows}, cx)
              : N3.empty(text((ex.panel || document).querySelector('.sub')) || 'No weekly-limit snapshots in range.',
                         {title: TITLES.windows, pw}, cx);
    } else if(ex.key === 'latency'){
      m = rec ? N3.lines(rec, {tk, view: VIEW, pw, title: TITLES.latency,
                               ylab: LSHOWN ? (f => f ? secs(LMAX*f) : '0') : null,
                               yrlab: LEV.length ? (f => String(EMAX*f)) : null,
                               days: LSPANS,
                               legend: Array.from((ex.panel || ex.host).querySelectorAll('.legend span'), s => ({
                                 s: text(s), c: swatch(s)}))}, cx)
              : N3.empty(text(ex.host && ex.host.querySelector('.sub')) || 'No data in range.', {title: TITLES.latency, pw}, cx);
    } else if(ex.key === 'daily'){
      const svg = ex.host && ex.host.querySelector('svg');
      m = rec ? N3.daily(rec, {peak: text(svg && svg.querySelector('.peak')), tk, view: VIEW, pw, title: TITLES[ex.key],
                               legend: Array.from(ex.host.querySelectorAll('.legend span'), s => ({
                                 s: text(s), c: swatch(s)}))}, cx)
              : N3.empty(text(ex.host && ex.host.querySelector('.sub')) || 'No data in range.', {title: TITLES[ex.key], pw}, cx);
    } else {
      m = N3.medal(rec, {legend: legendOf(ex.host), hot: ex.hot, title: TITLES[ex.key]}, cx);
    }
    ex.m = m;
    drop(ex.g); ex.glass.forEach(g => drop(g.g));
    ex.g = mesh(m.solid);
    ex.glass = m.glass.map(f => ({g: mesh(f.v), c: f.c, id: f.id}));
    // The box is kept from the first build: a figure changing under the pointer must not
    // make the exhibit jump to a new place on the stage.
    if(!ex.box) ex.box = m.box;
    return true;
  }

  // -- the stage ------------------------------------------------------------------------------------
  function layout(snap){
    base = N3.camera(wpx/hpx);
    const sky = N3.slots(EX.length - 1, base);
    EX.forEach((ex, i)=>{
      ex.rest = i === F ? N3.fore(ex.box, base) : N3.skyPose(ex.box, sky[ex.slot], base);
      if(!ex.pose || snap){ ex.pose = ex.rest; ex.to = null; }
      else if(ex.to) ex.to = ex.rest;
    });
    // Ticks are laid out for the width the front time chart takes on screen.
    const f = EX.find(e => e.key === 'windows') || EX.find(e => e.key === 'daily');
    if(f && f.box){
      const P = N3.fore(f.box, base), c = N3.centre(f.box);
      const M = N3.pose(P.p, 0, 0, 0, 1, c);
      const a = N3.ndc(base.VP, N3.xf(M, [0, 0, 0])), b = N3.ndc(base.VP, N3.xf(M, [pw, 0, 0]));
      const px = Math.round(Math.abs(b[0] - a[0])/2*wpx*.8);
      if(px !== tkPx){ tkPx = px; need = true; req(); }
    }
    laid = true;
  }
  const now = () => (performance.now() - T0)/1000;

  function focus(i){
    if(i === F || i < 0 || i >= EX.length) return;
    const old = F;
    F = i;
    EX[old].slot = EX[i].slot;
    EX[i].slot = -1;
    const t = now();
    for(const [ex, kind] of [[EX[i], 'down'], [EX[old], 'up']]){
      ex.from = ex.pose;
      ex.kind = REDUCE ? 'glide' : kind;
      ex.t0 = t; ex.dur = REDUCE ? 0 : (kind === 'down' ? 1.7 : 1.35);
    }
    for(const ex of EX) if(ex.hot >= 0){ ex.hot = -1; ex.tip = null; need = true; }
    hov = null;
    layout(false);
    EX[i].to = EX[i].rest; EX[old].to = EX[old].rest;
    orb.gy = 0; orb.gp = 0; orb.gd = 1;
    hint = Math.min(hint, .999);
    if(live) live.textContent = EX[i].name + ', brought forward';
    req();
  }

  // -- motion ---------------------------------------------------------------------------------------
  function step(t){
    let busy = false;
    EX.forEach((ex, i)=>{
      if(ex.to){
        const k = ex.dur ? Math.min(1, (t - ex.t0)/ex.dur) : 1;
        ex.pose = N3.tween(ex.from, ex.to, k, ex.kind);
        if(k >= 1){
          ex.pose = ex.to; ex.to = null;
          if(lastPtr) setTimeout(()=>{ if(!drag && lastPtr) hover(lastPtr); }, 0);
          if(ex.kind === 'down' && !REDUCE){               // where it comes down, the water rings
            RIP.unshift([ex.pose.p[0], ex.pose.p[2], t, 1]);
            RIP.length = Math.min(RIP.length, 4);
          }
        } else busy = true;
      } else ex.pose = ex.rest || ex.pose;
      // at rest: the one in front breathes on the water; the others hang and turn a little
      const p = ex.pose, M = REDUCE ? 0 : 1, ph = ex.phase;
      const bob = i === F ? .05*Math.sin(t*.8 + ph) : .35*Math.sin(t*.5 + ph);
      const sway = i === F ? 0 : .07*Math.sin(t*.31 + ph);
      const hot = (hov && hov.ex === ex && i !== F) ? .25 : 0;
      ex.lit = (p.lit || 1) + hot;
      const lean = i === F ? 0 : .015*Math.sin(t*.43 + ph);
      ex.M = N3.pose([p.p[0], p.p[1] + bob*M, p.p[2]], p.yaw + sway*M, p.pitch, (p.roll || 0) + lean*M,
                     p.s, N3.centre(ex.box));
      ex.Mi = N3.inv(ex.M);
    });
    for(const k of ['yaw', 'pitch', 'dolly']){
      const g = orb['g' + k[0]];
      orb[k] += (g - orb[k])*(drag ? 1 : .08);
      if(Math.abs(g - orb[k]) > 1e-4) busy = true;
    }
    if(hint < 1) hint = REDUCE ? 0 : Math.max(0, hint - .02);
    const fx = EX[F], pv = fx && fx.pose ? fx.pose.p : N3.CAM.at;
    const drift = REDUCE ? 0 : 1;
    cam = N3.camera(wpx/hpx, {yaw: orb.yaw + .025*Math.sin(t*.09)*drift,
                              pitch: orb.pitch + .012*Math.sin(t*.13)*drift, dolly: orb.dolly, pivot: pv});
    return busy;
  }

  // -- drawing --------------------------------------------------------------------------------------
  function common(P, mir){
    gl.useProgram(P.p);
    if(P.u.u_vp) gl.uniformMatrix4fv(P.u.u_vp, false, cam.VP);
    if(P.u.u_eye) gl.uniform3fv(P.u.u_eye, cam.eye);
    if(P.u.u_fog) gl.uniform3fv(P.u.u_fog, FOGC);
    if(P.u.u_fogk) gl.uniform1f(P.u.u_fogk, P === PR.water ? .0085 : .0055);
    if(P.u.u_mir) gl.uniform1f(P.u.u_mir, mir ? 1 : 0);
    if(P.u.u_t) gl.uniform1f(P.u.u_t, REDUCE ? 0 : now());    // reduced motion: still water
  }
  let FOGC = [.2, .3, .35];
  function sky(mir){
    const P = PR.sky;
    common(P, mir);
    gl.uniformMatrix4fv(P.u.u_ivp, false, N3.inv(cam.VP));
    gl.uniform3fv(P.u.u_eye, cam.eye);
    gl.uniform3fv(P.u.u_zen, col('--n-zenith').slice(0, 3));
    gl.uniform3fv(P.u.u_sky, col('--n-sky').slice(0, 3));
    gl.uniform3fv(P.u.u_haze, col('--n-haze').slice(0, 3));
    gl.uniform3fv(P.u.u_shore, col('--n-shore').slice(0, 3));
    gl.uniform3fv(P.u.u_lamp, col('--n-lamp').slice(0, 3));
    gl.disable(gl.DEPTH_TEST); gl.depthMask(false); gl.disable(gl.BLEND);
    gl.bindVertexArray(PR.full);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  function lights(P, mir){
    const k = [-.42, .62, .66], l = Math.hypot(...k);
    gl.uniform3fv(P.u.u_key, [k[0]/l, (mir ? -1 : 1)*k[1]/l, k[2]/l]);
    gl.uniform3fv(P.u.u_lamp, lin('--n-lamp').map(v => (.7 + .3*v)*1.3));
    gl.uniform3fv(P.u.u_sky, lin('--n-haze').map(v => v*2.2 + .03));
    gl.uniform3fv(P.u.u_gnd, lin('--n-sky').map(v => v*1.5 + .01));
    gl.uniform3fv(P.u.u_rim, lin('--uncached', .5));
  }
  function solids(mir){
    const P = PR.mesh;
    common(P, mir); lights(P, mir);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(true);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.disable(gl.CULL_FACE);
    gl.uniform1f(P.u.u_glass, 0); gl.uniform1f(P.u.u_ao, 1); gl.uniform1f(P.u.u_a, 1);
    EX.forEach((ex, i)=>{
      if(!ex.g || !ex.M) return;
      gl.uniformMatrix4fv(P.u.u_m, false, ex.M);
      gl.uniform1f(P.u.u_lit, ex.lit);
      gl.uniform1f(P.u.u_hi, i === F ? ex.hot : -1);
      gl.bindVertexArray(ex.g.vao);
      gl.drawArrays(gl.TRIANGLES, 0, ex.g.n);
    });
  }
  const dist = ex => Math.hypot(ex.pose.p[0] - cam.eye[0], ex.pose.p[1] - cam.eye[1], ex.pose.p[2] - cam.eye[2]);
  function glass(mir){
    const P = PR.mesh;
    common(P, mir); lights(P, mir);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(false);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.enable(gl.CULL_FACE);
    gl.uniform1f(P.u.u_glass, 1); gl.uniform1f(P.u.u_ao, 0); gl.uniform1f(P.u.u_a, 1);
    const order = EX.map((ex, i) => [ex, i]).filter(([ex]) => ex.glass.length && ex.M)
      .sort((a, b) => dist(b[0]) - dist(a[0]));
    for(const [ex, i] of order){
      gl.uniformMatrix4fv(P.u.u_m, false, ex.M);
      gl.uniform1f(P.u.u_lit, ex.lit);
      gl.uniform1f(P.u.u_hi, i === F ? ex.hot : -1);
      const fins = ex.glass.map(f => [f, Math.hypot(...[0, 1, 2].map(j => N3.xf(ex.M, f.c)[j] - cam.eye[j]))])
        .sort((a, b) => b[1] - a[1]);
      for(const face of [gl.FRONT, gl.BACK]){
        gl.cullFace(mir ? (face === gl.FRONT ? gl.BACK : gl.FRONT) : face);
        for(const [f] of fins){ gl.bindVertexArray(f.g.vao); gl.drawArrays(gl.TRIANGLES, 0, f.g.n); }
      }
    }
    gl.disable(gl.CULL_FACE);
  }
  /** One word, flat on its exhibit's face; a word with depth is drawn as stacked layers. */
  function drawWord(P, w, rgb, a){
    const t = tex(w), h = w.h;
    const tw = t.tw*h, x0 = w.al === 'c' ? w.x - tw/2 : w.al === 'r' ? w.x - tw : w.x;
    gl.bindTexture(gl.TEXTURE_2D, t.tex);
    gl.uniform2f(P.u.u_sz, t.qw*h, t.qh*h);
    const n = w.ext ? Math.max(2, Math.min(14, Math.round(w.ext*60))) : 0;
    for(let k = n; k >= 0; k--){
      const f = k ? .22 + .2*(1 - k/n) : 1;
      gl.uniform4f(P.u.u_c, rgb[0]*f, rgb[1]*f, rgb[2]*f, a);
      gl.uniform3f(P.u.u_o, x0 - t.pad*h, w.y - t.base*h, w.z - (n ? w.ext*k/n : 0));
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    }
  }
  function lettering(mir){
    const P = PR.text;
    common(P, mir);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(false);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.activeTexture(gl.TEXTURE0);
    gl.uniform1i(P.u.u_tex, 0);
    gl.bindVertexArray(PR.quad.vao);
    const pxPer = hpx/(2*Math.tan(cam.fov/2));             // CSS px per world unit at distance 1
    const order = EX.map((ex, i) => [ex, i]).filter(([ex]) => ex.m && ex.M).sort((a, b) => dist(b[0]) - dist(a[0]));
    for(const [ex, i] of order){
      gl.uniformMatrix4fv(P.u.u_m, false, ex.M);
      const scale = ex.pose.s*pxPer/Math.max(1, dist(ex));
      const lit = Math.min(1, ex.lit);
      const pie = ex.key === 'content' || ex.key === 'models';
      for(const w of ex.m.words){
        const px = w.h*scale;
        if(px < 3.5) continue;
        let a = w.a*Math.min(1, (px - 3.5)/4)*(.35 + .65*lit);
        if(pie && i === F && ex.hot >= 0 && w.id >= 0 && w.id !== ex.hot) a *= .35;
        const c = col(w.c);
        drawWord(P, w, c, a*c[3]);
      }
      if(i === F && ex.tip) for(const w of ex.tip) drawWord(P, w, col(w.c), 1);
    }
  }
  /** The names of the exhibits in the sky, and the one line of help, facing the camera. */
  function labels(){
    const P = PR.text;
    common(P, false);
    gl.disable(gl.DEPTH_TEST); gl.depthMask(false);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(PR.quad.vao);
    const IV = N3.inv(cam.V);
    const R = [IV[0], IV[1], IV[2]], U = [IV[4], IV[5], IV[6]], B = [IV[8], IV[9], IV[10]];
    const place = (p) => [R[0],R[1],R[2],0, U[0],U[1],U[2],0, B[0],B[1],B[2],0, p[0],p[1],p[2],1];
    const unit = d => 2*d*Math.tan(cam.fov/2)/hpx;           // world units a CSS px, at distance d
    EX.forEach((ex, i)=>{
      if(i === F || !ex.M || !ex.box) return;
      const c = N3.centre(ex.box);
      const b = N3.xf(ex.M, [c[0], ex.box[1], ex.box[5]]);
      const d = Math.hypot(b[0] - cam.eye[0], b[1] - cam.eye[1], b[2] - cam.eye[2]);
      const u = unit(d), on = hov && hov.ex === ex;
      gl.uniformMatrix4fv(P.u.u_m, false, place([b[0], b[1] - 14*u, b[2]]));
      const w = {s: ex.name, h: 15*u, x: 0, y: 0, z: 0, al: 'c', f: 'serif', ext: 0};
      const cc = col(on ? '--warn' : '--fg');
      drawWord(P, w, cc, on ? 1 : .78);
    });
    if(hint > 0 && EX.length > 1){
      const IVP = N3.inv(cam.VP);
      const a = N3.xf(IVP, [0, N3.FORE_TOP + .045, .97]);
      const p = [a[0]/a[3], a[1]/a[3], a[2]/a[3]];
      const d = Math.hypot(p[0] - cam.eye[0], p[1] - cam.eye[1], p[2] - cam.eye[2]);
      gl.uniformMatrix4fv(P.u.u_m, false, place(p));
      drawWord(P, {s: 'choose a chart in the sky to bring it down to the water', h: 13*unit(d),
                   x: 0, y: 0, z: 0, al: 'c', f: 'serif', ext: 0}, col('--dim'), .9*hint);
    }
  }
  function sparks(mir){
    if(REDUCE) return;
    const P = PR.spark;
    common(P, mir);
    gl.uniform1f(P.u.u_ps, 100*(cv.width/Math.max(1, wpx))*hpx/900);
    gl.uniform3fv(P.u.u_c, col('--n-spark').slice(0, 3));
    gl.enable(gl.DEPTH_TEST); gl.depthMask(false);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    gl.bindVertexArray(PR.sparks.vao);
    gl.drawArrays(gl.POINTS, 0, PR.sparks.n);
  }
  function water(){
    const P = PR.water;
    common(P, false);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, PR.ref.tex);
    gl.uniform1i(P.u.u_ref, 0);
    gl.uniform2f(P.u.u_res, cv.width, cv.height);
    gl.uniform3fv(P.u.u_water, col('--n-water').slice(0, 3));
    gl.uniform3fv(P.u.u_lamp, col('--n-lamp').slice(0, 3));
    const f = EX[F] && EX[F].pose ? EX[F].pose.p : [0, 0, 0];
    gl.uniform3f(P.u.u_pool, f[0], f[2], 1);
    const r = [];
    for(let i = 0; i < 4; i++) r.push(...(RIP[i] || [0, 0, 0, 0]));
    gl.uniform4fv(P.u.u_rip, r);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(true); gl.disable(gl.BLEND);
    gl.bindVertexArray(PR.sheet.vao);
    gl.drawArrays(gl.TRIANGLES, 0, PR.sheet.n);
  }
  function reflection(){
    const R = PR.ref, w = Math.max(1, cv.width >> 1), h = Math.max(1, cv.height >> 1);
    if(R.w !== w || R.h !== h){
      R.w = w; R.h = h;
      gl.bindTexture(gl.TEXTURE_2D, R.tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.bindRenderbuffer(gl.RENDERBUFFER, R.rb);
      gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, w, h);
      gl.bindFramebuffer(gl.FRAMEBUFFER, R.fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, R.tex, 0);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, R.rb);
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, R.fb);
    gl.viewport(0, 0, w, h);
    gl.depthMask(true);
    gl.clear(gl.DEPTH_BUFFER_BIT);
    sky(true); solids(true); lettering(true); glass(true); sparks(true);
  }

  /** The switch: drawn where the page's button is, a few units in front of the eye, at the
   *  dot's size; it rocks gently, and turns over while the pointer is on the button. */
  const SW = {m: null, face: '', spin: 0, goal: 0, el: null};
  function switcher(t){
    const btn = SW.el || (SW.el = byId('stylebtn'));
    const dot = btn && btn.querySelector('.sdot');
    if(!btn || !dot) return;
    const r = btn.getBoundingClientRect();
    if(!r.width) return;
    const face = getComputedStyle(dot).backgroundColor;
    if(face !== SW.face){
      SW.face = face;
      drop(SW.m);
      SW.m = mesh(N3.coin(parse(face), [1, 1, 1, 1], parse('#c9ced6')));
    }
    SW.spin += (SW.goal - SW.spin)*.12;
    const [, dv] = rayAt({clientX: r.left + r.width/2, clientY: r.top + r.height/2}), dl = Math.hypot(...dv);
    const p = [cam.eye[0] + dv[0]/dl*6, cam.eye[1] + dv[1]/dl*6, cam.eye[2] + dv[2]/dl*6];
    const k = 9*2*6*Math.tan(cam.fov/2)/hpx;               // 9 CSS px, six units out
    const W = N3.inv(cam.V);
    const B = [W[0],W[1],W[2],0, W[4],W[5],W[6],0, W[8],W[9],W[10],0, p[0],p[1],p[2],1];
    const M = N3.mul(B, N3.pose([0, 0, 0], (REDUCE ? 0 : .38*Math.sin(t*1.1)) + SW.spin, .12, 0, k, [0, 0, 0]));
    const P = PR.mesh;
    common(P, false); lights(P, false);
    gl.depthMask(true); gl.clear(gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST); gl.disable(gl.BLEND); gl.disable(gl.CULL_FACE);
    gl.uniform1f(P.u.u_glass, 0); gl.uniform1f(P.u.u_ao, 0); gl.uniform1f(P.u.u_a, 1);
    gl.uniform1f(P.u.u_lit, 1.1); gl.uniform1f(P.u.u_hi, -1);
    gl.uniform1f(P.u.u_fogk, 0);
    gl.uniformMatrix4fv(P.u.u_m, false, M);
    gl.bindVertexArray(SW.m.vao);
    gl.drawArrays(gl.TRIANGLES, 0, SW.m.n);
  }

  function frame(){
    raf = 0;
    if(!on) return;
    const t = now();
    fit();
    if(need){ need = false; EX.forEach(build); }
    if(!laid) layout(true);
    const busy = step(t);
    FOGC = col('--n-haze').slice(0, 3).map(v => v*1.35);
    reflection();
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, cv.width, cv.height);
    gl.depthMask(true);                                   // a clear honours the write mask
    gl.clear(gl.DEPTH_BUFFER_BIT);
    sky(false); water(); solids(false); lettering(false); glass(false); sparks(false); labels();
    switcher(t);
    if(!REDUCE || busy || hint > 0 && hint < 1 || Math.abs(SW.goal - SW.spin) > 1e-3) req();
  }
  function req(){ if(!raf && on) raf = requestAnimationFrame(frame); }

  function fit(){
    const r = cv.getBoundingClientRect();
    const d = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    if(w === wpx && h === hpx && d === dpr) return;
    wpx = w; hpx = h; dpr = d;
    let k = d;
    if(w*h*k*k > 4.2e6) k = Math.sqrt(4.2e6/(w*h));      // keep a large screen interactive
    cv.width = Math.round(w*k); cv.height = Math.round(h*k);
    const p = N3.plotWidth(w/h);
    if(p !== pw){ pw = p; EX.forEach(ex => { ex.box = null; }); }   // rebuilt, and placed anew
    need = true;
    laid = false;                                         // a new size: everything snaps into place
  }

  // -- the pointer --------------------------------------------------------------------------------
  let drag = null, lastPtr = null;
  const pts = new Map();
  /** The ray from the eye through client point (x, y): its origin on the near plane, and a
   *  direction reaching the far one. */
  function rayAt(e){
    const r = cv.getBoundingClientRect();
    const x = (e.clientX - r.left)/r.width*2 - 1, y = 1 - (e.clientY - r.top)/r.height*2;
    const IV = N3.inv(cam.VP), a = N3.xf(IV, [x, y, -1]), b = N3.xf(IV, [x, y, 1]);
    const o = [a[0]/a[3], a[1]/a[3], a[2]/a[3]], f = [b[0]/b[3], b[1]/b[3], b[2]/b[3]];
    return [o, [f[0] - o[0], f[1] - o[1], f[2] - o[2]]];
  }
  /** What is under the pointer: the exhibit, and for the one in front, the element. */
  function pick(e){
    if(!cam) return null;
    const [o, d] = rayAt(e);
    let best = null;
    EX.forEach((ex, i)=>{
      if(!ex.Mi || !ex.box) return;
      const [lo, ld] = N3.local(ex.Mi, o, d);
      const t = N3.rayBox(lo, ld, ex.box);
      if(t !== null && (!best || t < best.t)) best = {ex, i, t, lo, ld};
    });
    if(!best || best.i !== F) return best;
    const ex = best.ex, m = ex.m;
    best.id = -1;
    if(m.plot){
      const q = N3.onPlane(best.lo, best.ld, m.plot.z);
      if(q){
        best.u = q[0]/m.plot.w; best.v = q[1]/m.plot.h;
        best.plot = best.u >= 0 && best.u <= 1 && best.v >= -.15 && best.v <= 1.1;
      }
      for(const h of m.hits){
        if(N3.rayBox(best.lo, best.ld, h.box) !== null || (q && q[0] >= h.box[0] && q[0] <= h.box[3]
           && q[1] >= 0 && q[1] <= h.box[4] + .3)){ best.id = h.id; best.hit = h; break; }
      }
    } else if(m.medal){
      const q = N3.onPlane(best.lo, best.ld, 0);
      if(q) best.id = N3.sliceAt(q[0], q[1], m);
      if(best.id < 0 && q){                                // or its line in the legend
        for(const w of m.words){
          if(w.id < 0) continue;
          const tw = mwidth(w.s, w.f, w.h);
          if(q[0] >= w.x - .5 && q[0] <= w.x + tw && q[1] >= w.y - .15 && q[1] <= w.y + w.h) best.id = w.id;
        }
      }
    }
    return best;
  }
  /** The label that answers a hovered element of the chart in front. */
  function tipFor(ex, hit){
    const H = ex.key === 'windows' ? N3.WIN_H : N3.DAY_H;
    const x = Math.max(0, Math.min(ex.m.plot.w, (hit.box[0] + hit.box[3])/2));
    const at = (s, k, c) => ({s, h: k ? .27 : .32, x, y: H + N3.TIP_Y + (k ? 0 : .42), z: .6, al: 'c', c, f: 'sans', ext: 0, w: k ? 400 : 600});
    if(ex.key === 'windows'){
      const w = WINS[hit.id];
      if(!w) return null;
      const t0 = w.reset_at != null ? w.reset_at : ((w.cum_points || [])[0] || [null])[0];
      return [at(`window opened ${t0 == null ? '--' : when(t0)}  ·  peak reported ${w.peak_pct == null ? '--' : w.peak_pct + '%'}`, 0, '--fg'),
              at(`${winInput(w)} over ${w.tokens.responses.toLocaleString()} responses  ·  uncached ${big(w.tokens.uncached)}`, 1, '--dim')];
    }
    if(ex.key === 'latency'){
      const r = LROW.get(hit.day), n = LEVN.get(hit.day);
      const ev = n ? `  ·  ${evWord(n)}` : '';
      if(!r) return n ? [at(day(hit.day), 0, '--fg'), at(evWord(n), 1, '--warn')] : null;
      return latOk(r, 3)
        ? [at(`${day(hit.day)}  ·  median ${secs(r[3])}  ·  p90 ${secs(r[4])}`, 0, '--fg'),
           at(`${r[2].toLocaleString()} timed responses${ev}`, 1, '--dim')]
        : [at(`${day(hit.day)}  ·  ${r[2].toLocaleString()} timed responses`, 0, '--fg'),
           at(`too few to show${ev}`, 1, '--dim')];
    }
    const row = (D.models.days || []).find(r => r[0] === hit.day);
    if(!row) return null;
    const tot = Object.values(row[2]).reduce((a, b) => a + b, 0);
    const top = Object.entries(row[2]).sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([m, v]) => `${m} ${big(v)}`).join('  ·  ');
    return [at(`${day(hit.day)}  ·  ${big(tot)} ${INPUT}`, 0, '--fg'), at(top, 1, '--dim')];
  }
  function hover(e){
    const h = pick(e);
    hov = h;
    let cur = 'default';
    EX.forEach((ex, i)=>{
      const id = h && h.ex === ex && i === F ? h.id : -1;
      if(ex.hot !== id){
        ex.hot = id;
        ex.tip = id >= 0 && ex.m.plot && h.hit ? tipFor(ex, h.hit) : null;
        if(ex.key === 'content' || ex.key === 'models') need = true;
      }
    });
    if(h && h.i !== F) cur = 'pointer';
    else if(h && h.plot) cur = drag ? 'grabbing' : 'grab';
    cv.style.cursor = cur;
    req();
  }
  function bind(el){
    el.addEventListener('pointerdown', e=>{
      if(e.pointerType === 'mouse' && e.button !== 0) return;
      try{ el.setPointerCapture(e.pointerId); }catch(_){}
      const h = pick(e);
      pts.set(e.pointerId, {x: e.clientX, y: e.clientY, u: h && h.plot ? h.u : null});
      if(pts.size === 2){
        const [a, b] = Array.from(pts.values());
        drag = (a.u != null && b.u != null && VIEW)
          ? {pinch: true, ua: a.u, ub: b.u, ta: Tat(L + a.u*PLOT), tb: Tat(L + b.u*PLOT)} : null;
        return;
      }
      drag = {x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, t: performance.now(),
              pan: !!(h && h.i === F && h.plot && VIEW), u: h ? h.u : 0, moved: false,
              yaw: orb.gy, pitch: orb.gp};
      if(drag.pan) stopGlide();
    });
    el.addEventListener('pointermove', e=>{
      lastPtr = e.pointerType === 'mouse' ? {clientX: e.clientX, clientY: e.clientY} : null;
      const p = pts.get(e.pointerId);
      if(p){ p.x = e.clientX; p.y = e.clientY; }
      if(!drag){ hover(e); return; }
      if(drag.pinch){
        const v = Array.from(pts.keys()).map(id => { const q = pts.get(id); const h = pick({clientX: q.x, clientY: q.y}); return h && h.plot ? h.u : null; });
        if(v[0] == null || v[1] == null) return;
        const dx = (v[1] - v[0])*PLOT, dt = drag.tb - drag.ta;
        if(Math.abs(dx) < 4 || dx*dt <= 0) return;
        setSpan(dt*PLOT/dx, drag.ta, L + v[0]*PLOT);
        return;
      }
      if(Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) > 4) drag.moved = true;
      if(!drag.moved) return;
      if(drag.pan){
        const h = pick(e);
        if(h && h.u != null && h.i === F){ panPx((h.u - drag.u)*PLOT); drag.u = h.u; }
      } else {
        orb.gy = Math.max(-.75, Math.min(.75, drag.yaw - (e.clientX - drag.x0)*.004));
        orb.gp = Math.max(-.08, Math.min(.42, drag.pitch + (e.clientY - drag.y0)*.003));
        req();
      }
      e.preventDefault();
    });
    const lift = e=>{
      pts.delete(e.pointerId);
      if(!drag || pts.size) { if(!pts.size) drag = null; return; }
      const d = drag;
      drag = null;
      if(!d.pinch && !d.moved && performance.now() - d.t < 600){
        const h = pick(e);
        if(h && h.i !== F){ focus(h.i); return; }
      }
      hover(e);                                           // a tap on a bar or a slice answers it
    };
    el.addEventListener('pointerup', lift);
    el.addEventListener('pointercancel', e=>{ pts.delete(e.pointerId); drag = null; });
    el.addEventListener('pointerleave', ()=>{ if(!drag){ hov = null; EX.forEach(ex=>{ if(ex.hot >= 0){ ex.hot = -1; ex.tip = null; need = true; } }); req(); } });
    el.addEventListener('wheel', e=>{
      e.preventDefault();
      const h = pick(e);
      const unit = e.deltaMode === 1 ? .05 : (e.deltaMode === 2 ? .8 : .002);
      if(h && h.i === F && h.plot && VIEW){
        if(Math.abs(e.deltaX) > Math.abs(e.deltaY)) panPx(-e.deltaX*PLOT/Math.max(1, tkPx/.8));
        else if(e.deltaY) zoomAt(1/Math.exp(-e.deltaY*unit), L + Math.max(0, Math.min(1, h.u))*PLOT);
        return;
      }
      orb.gd = Math.max(.72, Math.min(1.3, orb.gd*Math.exp(e.deltaY*unit*.5)));
      req();
    }, {passive: false});
    el.addEventListener('dblclick', e=>{
      const h = pick(e);
      if(h && h.i === F && h.plot && DOM) glideTo([DOM[0], DOM[1]]);
      else { orb.gy = 0; orb.gp = 0; orb.gd = 1; req(); }
    });
    document.addEventListener('keydown', e=>{
      if(!on || e.ctrlKey || e.metaKey || e.altKey) return;
      if(e.key === 'ArrowRight' || e.key === 'ArrowLeft'){
        focus((F + (e.key === 'ArrowRight' ? 1 : -1) + EX.length) % EX.length);
        e.preventDefault();
      } else if(/^[1-9]$/.test(e.key) && +e.key <= EX.length) focus(+e.key - 1);
      else if(e.key === 'Escape'){ orb.gy = 0; orb.gp = 0; orb.gd = 1; req(); }
    });
  }

  let live = null;
  function sync(){
    const want = ok && SCENE_STYLES.indexOf(RT.getAttribute('data-style')) >= 0;
    if(want && !cv){
      if(!init()){ ok = false; }
      else {
        live = document.createElement('div');
        live.className = 's3d-live'; live.setAttribute('aria-live', 'polite');
        document.body.appendChild(live);
        exhibits();
      }
    }
    on = want && ok && !!gl;
    if(on){
      RT.setAttribute('data-s3d', '');
      pal = {};
      serif = getComputedStyle(RT).getPropertyValue('--serif').trim() || serif;
      EX.forEach(ex => { ex.src = undefined; });
      need = true;
      req();
    } else {
      RT.removeAttribute('data-s3d');
      if(raf){ cancelAnimationFrame(raf); raf = 0; }
    }
  }
  try{ matchMedia('(prefers-color-scheme: dark)').addEventListener('change', ()=>{ pal = {}; need = true; req(); }); }catch(_){}
  S3H.dirty = () => { need = true; req(); };
  return {sync, focus, get front(){ return F; }};
})();
"""


# The style switcher.  Kept apart from JS so the charts' script reads as it did; it runs after
# init(), touches the charts only through measure() and redraw(), and does nothing at all
# where there is no real DOM (scripts/test_page.js).
STYLE_JS = r"""
// ---- styles: one page, several readings -----------------------------------------------
const STYLES = D.styles || [['clinical','Clinical']];
const ROOT = document.documentElement;

let SI = 0;
function applyStyle(i){
  SI = (i % STYLES.length + STYLES.length) % STYLES.length;
  const id = STYLES[SI][0];
  ROOT.setAttribute('data-style', id);
  const btn = byId('stylebtn'), next = STYLES[(SI+1)%STYLES.length];
  if(btn){
    btn.setAttribute('data-next', next[0]);                   // the dot is drawn in `next`
    btn.setAttribute('aria-label', 'Switch to the ' + next[1] + ' style');
  }
  try{ localStorage.setItem('tc-style', id); }catch(_){}
  try{ history.replaceState(null, '', '#style=' + id); }catch(_){}
  if(GLX) GLX.sync();
  if(S3D) S3D.sync();
  if(VIEW){ measure(); redraw(); }
}

function cycle(step){
  const w = document.querySelector('.wipe');
  if(!w || REDUCE){ applyStyle(SI + step); return; }
  w.classList.remove('go'); void w.offsetWidth; w.classList.add('go');
  setTimeout(()=>applyStyle(SI + step), 270);
  setTimeout(()=>w.classList.remove('go'), 620);
}

function initStyles(){
  if(!ROOT || !ROOT.setAttribute || !document.addEventListener) return;
  let id = null;
  const m = /style=([a-z]+)/.exec((typeof location !== 'undefined' && location.hash) || '');
  if(m) id = m[1];
  // A page rendered with --style opens in it: the style was asked for by name, and a pick
  // remembered from another report must not override it.  Without one, the remembered pick
  // wins over the default.
  const own = STYLES.findIndex(s=>s[0] === (ROOT.getAttribute && ROOT.getAttribute('data-style')));
  const pinned = ROOT.hasAttribute && ROOT.hasAttribute('data-style-set');
  if(!id && pinned && own >= 0) id = STYLES[own][0];
  if(!id){ try{ id = localStorage.getItem('tc-style'); }catch(_){} }
  // A style this page no longer carries (a bookmark, or one remembered from an older
  // report) falls back to the one the page was rendered in.
  const i = STYLES.findIndex(s=>s[0] === id);
  applyStyle(i >= 0 ? i : Math.max(own, 0));
  const btn = byId('stylebtn');
  if(btn) btn.addEventListener('click', e=>cycle(e.shiftKey ? -1 : 1));
  document.addEventListener('keydown', e=>{
    if(e.ctrlKey || e.metaKey || e.altKey) return;
    if(e.key === ']') cycle(1);
    else if(e.key === '[') cycle(-1);
  });
}
initStyles();
"""


def esc(s):
    return html.escape('' if s is None else str(s), quote=True)


def limit_name(rl):
    """What to call the rate-limit window the report draws: ``weekly``, or, in logs that
    quote no weekly window, the length of the longest one they do (``5-hour``)."""
    wm = rl.get('window_minutes')
    if rl.get('weekly', True) or not isinstance(wm, int) or wm <= 0:
        return 'weekly'
    if wm % 1440 == 0:
        return 'daily' if wm == 1440 else f'{wm // 1440}-day'
    if wm % 60 == 0:
        return f'{wm // 60}-hour'
    return f'{wm}-minute'


def rel(seconds):
    """A duration as ``2d 3h``, for reset distances.  Sign is the caller's to phrase."""
    if seconds is None:
        return '&mdash;'
    s = abs(int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f'{d}d {h}h'
    if h:
        return f'{h}h {m}m'
    return f'{m}m'


def _script_json(obj):
    """JSON safe to embed inside a ``<script>`` element.

    The payload carries item previews taken verbatim from rollout content, so a tool output
    containing ``</script>`` would otherwise close the block and spill the rest of the data
    into the page as markup.  Escaping ``<``, ``>`` and ``&`` as unicode escapes is inert in
    JSON and cannot terminate the element.
    """
    return (json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def big(n):
    if n is None:
        return '&mdash;'
    a = abs(n)
    for div, suf in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if a >= div:
            return f'{n/div:.2f}{suf}' if a < div * 100 else f'{n/div:.1f}{suf}'
    return f'{n:,}'


def pct(x, digits=1):
    return '&mdash;' if x is None else f'{100*x:.{digits}f}%'


def usd(x):
    """Dollars for a headline: cents under a thousand, whole dollars to six figures, then
    K and M so the figure fits a tile in every style."""
    if x is None or x != x:
        return '&mdash;'
    a = abs(x)
    if a >= 1e6:
        return f'${x/1e6:,.2f}M'
    if a >= 1e5:
        return f'${x/1e3:,.1f}K'
    if a >= 1e3:
        return f'${x:,.0f}'
    return f'${x:,.2f}'


def _as_of(day):
    """``2026-10-04`` -> ``Oct 4, 2026``; anything else as it came, escaped."""
    if day is None or day == '':
        return 'an unknown date'
    try:
        d = datetime.date.fromisoformat(str(day))
    except ValueError:
        return esc(str(day))
    return f'{d.strftime("%b")} {d.day}, {d.year}'


def api_tile(av):
    """The API value headline: what the recorded usage would cost at API list prices.

    Worded as a counterfactual on the page itself ("if billed at API list prices"), since a
    plan is not billed per token, and it says what it leaves out: responses it could not
    price.  None when nothing was priced; `--json` and stdout say why.
    """
    if not (av or {}).get('available'):
        return None
    note = f"if billed at API list prices of {_as_of((av.get('prices') or {}).get('as_of'))}"
    if av.get('unpriced'):
        note += f" &middot; {av['unpriced']:,} response{'' if av['unpriced'] == 1 else 's'} unpriced"
    # The tier is unknown for a file with no settings snapshot; say what Fast would make it,
    # when that moves the figure by more than rounding.
    hi = av.get('usd_high')
    if hi is not None and hi - av['usd'] >= max(0.01, 0.005 * av['usd']):
        note += f" &middot; up to {usd(hi)} if untiered responses ran in Fast mode"
    return tile('API value', usd(av['usd']), note)


def tile(k, v, note=''):
    n = f'<div class="n">{note}</div>' if note else ''
    return f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div>{n}</div>'



# Mirrors analyze.LEAD_MIN_GAP, formatted for prose.
DAILY_MODELS = 8        # models stacked in their own colour; the rest fold into `other`


def _domain(model):
    """The time span every chart on the page is drawn over, in unix seconds.

    One domain for every chart: the limit chart, the daily chart and the response-time chart
    then place a moment at the same x and can be read against each other, and the
    composition pie has a range to be recomposed over.  It covers the limit series, the daily
    buckets, the response-time and rate-limit-event days and the content buckets, so nothing
    the page can draw falls outside it.
    """
    lo = hi = None

    def seen(*times):
        nonlocal lo, hi
        for t in times:
            if t is None:
                continue
            lo = t if lo is None else min(lo, t)
            hi = t if hi is None else max(hi, t)

    # Not the wall clock: a report whose data ends in the past, by --until or by a long
    # break, would otherwise stretch to today and squeeze every chart into its left edge.
    rl = model.get('rate_limits') or {}
    if rl.get('available'):
        for w in (rl.get('windows') or []):
            seen(w.get('reset_at'))
            for p in (w.get('cum_points') or []):
                seen(p[0])
            for p in (w.get('pct_points') or []):
                seen(p[0])
    for d in (model.get('daily') or []):
        seen(d.get('start'), d.get('end'))
    for d in ((model.get('latency') or {}).get('daily') or []):
        seen(d.get('start'), d.get('end'))
    for d in ((model.get('limit_events') or {}).get('daily') or []):
        seen(d.get('start'), d.get('end'))
    bucket = model.get('cat_bucket_s') or 3600
    for row in (model.get('cat_series') or []):
        # The bucket's *end* too: a bucket opening on the last instant of the range would
        # otherwise sit outside the page's own domain and drop out of the pie at full zoom.
        seen(row[0], row[0] + bucket)
    if lo is None:
        return None
    return [int(lo), int(max(hi, lo + 3600))]


def _daily_svg(daily, order, domain, tiktoken=False):
    """Daily input, stacked by the model that was charged for it: tiktoken's count when
    `tiktoken`, else Codex's recorded figure.

    `order` is the corpus-wide ranking, not the day's own: stacking in per-day order would
    reshuffle the colours from one bar to the next, and a model's colour would stop meaning
    anything across the chart.

    Bars are drawn in **unit x** -- one unit is the day, and the group's transform places and
    widens it -- so the page rescales the time axis on every zoom step without re-deriving
    any of this geometry, and without touching a height: what a bar's height means is the
    same at every zoom level.  The transform written here is the full-domain one, which is
    what a reader with JavaScript disabled is left with.
    """
    if not daily:
        return '<p class="sub">No data in range.</p>'
    dated = [d for d in daily if d.get('start') is not None and d.get('end') is not None]
    undated = [d for d in daily if d.get('start') is None or d.get('end') is None]
    if not dated:
        return '<p class="sub">No dated days in range.</p>'
    W, L, R, T, B = CHART_W, CHART_L, CHART_R, DAILY_T, DAILY_B
    t0, t1 = domain
    span = (t1 - t0) or 1
    px = lambda t: L + (t - t0) / span * (W - L - R)

    keys = [m for m in (order or []) if m][:DAILY_MODELS]
    rank = {m: i for i, m in enumerate(keys)}
    colour = lambda i: f'var(--c{i % 14})' if i < len(keys) else 'var(--dim)'
    val, split = ('tiktoken_input', 'tiktoken_models') if tiktoken else ('input', 'models')
    mx = max(d[val] for d in dated) or 1
    totals = {}
    parts = [f'<svg viewBox="0 0 {W} {DAILY_H}" data-h="{DAILY_H}" data-t="{T}" data-b="{B}" '
             f'role="img" aria-label="daily {"" if tiktoken else "recorded "}input, stacked '
             f'by model">',
             f'<defs><clipPath id="tcclip-daily"><rect class="clip" x="{L}" y="0" '
             f'width="{W-L-R}" height="{DAILY_H}"/></clipPath></defs>',
             '<g class="ax"></g>',
             '<g class="bars" clip-path="url(#tcclip-daily)">']
    for d in dated:
        a, b = d['start'], d['end']
        x, sx = px(a), max(px(b) - px(a), 0.001)
        parts.append(f'<g class="bar" data-a="{int(a)}" data-b="{int(b)}" '
                     f'transform="translate({x:.2f},0) scale({sx:.5f},1)">')
        ms = d.get(split) or {}
        segs, rest = [], d[val]
        for m, v in ms.items():
            if m in rank:
                segs.append((rank[m], m, v))
                rest -= v
        segs.sort()
        # Whatever the ranking does not name still has to be drawn, or the bar understates
        # the day: models past the cap, and any token the split did not account for.
        if rest > 0:
            segs.append((len(keys), 'other', rest))
        y, rows = DAILY_H - B, []
        for j, m, v in segs:
            sh = (v / mx) * (DAILY_H - B - T)
            y -= sh
            parts.append(f'<rect x="0.04" y="{y:.2f}" width="0.92" height="{sh:.2f}" '
                         f'fill="{colour(j)}" class="mk"></rect>')
            totals[m] = totals.get(m, 0) + v
            rows.append(f'{m} {v:,}')
        tip = ((f'{d["date"]}\ninput {d[val]:,} (tiktoken)\nrecorded by Codex '
                f'{d["input"]:,}, cached {d["cached"]:,}\n' if tiktoken else
                f'{d["date"]}\nrecorded {d["input"]:,}\ncached {d["cached"]:,}\n')
               + f'uncached {d["uncached"]:,}\nresponses {d["responses"]:,}'
               + ('\n' + '\n'.join(rows) if rows else ''))
        parts.append(f'<rect x="0" y="{T}" width="1" height="{DAILY_H-T-B}" '
                     f'fill="transparent"><title>{esc(tip)}</title></rect>')
        parts.append('</g>')
    parts.append('</g>')
    parts.append(f'<line class="base" x1="{L}" y1="{DAILY_H-B}" x2="{W-R}" y2="{DAILY_H-B}" '
                 f'stroke="var(--line)"/>')
    parts.append(f'<text class="peak" x="{L}" y="{T-6}" fill="var(--dim)" font-size="11">'
                 f'peak {mx:,} tokens/day</text>')
    parts.append('</svg>')
    # An entry that would read 0.0% is noise in the legend; its tokens stay in the bars
    # and their tooltips.
    tot = sum(totals.values()) or 1
    legend = ' '.join(
        f'<span><i style="background:{colour(rank.get(m, len(keys)))}"></i>{esc(m)} '
        f'{100*v/tot:.1f}%</span>'
        for m, v in sorted(totals.items(), key=lambda kv: -kv[1]) if 100*v/tot >= 0.05)
    # A day the corpus never dated cannot be placed on a time axis.  It is named rather than
    # dropped in silence, because its tokens are in every total on the page.
    miss = ('' if not undated else
            f'<p class="sub">{len(undated)} undated day(s), '
            f'{sum(d[val] for d in undated):,} {"" if tiktoken else "recorded "}input, '
            f'are not drawn.</p>')
    return ''.join(parts) + f'<div class="legend">{legend}</div>{miss}'


# ---- the Matisse collage ------------------------------------------------------------------
# Drawn once, here, from a fixed seed: the same torn edges every time the page opens, inline
# SVG so nothing is fetched, and hidden by CSS under every other style.

def _curve(pts):
    """A closed Catmull-Rom curve through `pts`, as an SVG path of cubic Beziers."""
    n = len(pts)
    d = [f'M{pts[0][0]:.1f} {pts[0][1]:.1f}']
    for i in range(n):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        d.append(f'C{p1[0] + (p2[0] - p0[0]) / 6:.1f} {p1[1] + (p2[1] - p0[1]) / 6:.1f} '
                 f'{p2[0] - (p3[0] - p1[0]) / 6:.1f} {p2[1] - (p3[1] - p1[1]) / 6:.1f} '
                 f'{p2[0]:.1f} {p2[1]:.1f}')
    return ''.join(d) + 'Z'


def _torn(rng, cx, cy, rx, ry, n=44, lump=.16, tear=.022):
    """A sheet torn into a rough oval: two slow lobes for the shape, a fine tremor for the edge."""
    f1, f2 = rng.uniform(0, 6.3), rng.uniform(0, 6.3)
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 1 + lump * (.65 * math.sin(2 * a + f1) + .35 * math.sin(3 * a + f2)) + rng.uniform(-tear, tear)
        pts.append((cx + rx * r * math.cos(a), cy + ry * r * math.sin(a)))
    return _curve(pts)


def _leaf(x, y, deg, ln, w, slit=True):
    """An almond leaf from (x, y) pointing at `deg`, with the vein cut out of it."""
    d = (f'M0 0C{ln * .25:.1f} {-w:.1f} {ln * .7:.1f} {-w * .9:.1f} {ln:.1f} 0'
         f'C{ln * .7:.1f} {w * .9:.1f} {ln * .25:.1f} {w:.1f} 0 0Z')
    if slit:
        d += (f'M{ln * .34:.1f} {w * .08:.1f}C{ln * .5:.1f} {-w * .32:.1f} {ln * .7:.1f} {-w * .26:.1f} '
              f'{ln * .82:.1f} {-w * .03:.1f}C{ln * .66:.1f} {w * .06:.1f} {ln * .5:.1f} {w * .14:.1f} '
              f'{ln * .34:.1f} {w * .08:.1f}Z')
    return (f'<path class="ink" fill-rule="evenodd" transform="translate({x} {y}) rotate({deg})" '
            f'd="{d}"/>')


def _flower(rng):
    """Six pointed petals with seeds cut out of the heart, on a stem of cut leaves."""
    cx, cy, petals = 150, 150, 6
    turn = rng.uniform(0, 1)
    d = []
    for k in range(petals):
        a = 2 * math.pi * (k + turn) / petals
        tip = rng.uniform(88, 112)
        v0, v1 = a - math.pi / petals, a + math.pi / petals
        r0 = rng.uniform(30, 40)
        p = lambda ang, r: (cx + r * math.cos(ang), cy + r * math.sin(ang))
        start, t, c1, c2, end = (p(v0, r0), p(a + rng.uniform(-.08, .08), tip),
                                 p(a - .3, tip * .78), p(a + .3, tip * .78), p(v1, r0))
        if k == 0:
            d.append(f'M{start[0]:.1f} {start[1]:.1f}')
        d.append(f'Q{c1[0]:.1f} {c1[1]:.1f} {t[0]:.1f} {t[1]:.1f}Q{c2[0]:.1f} {c2[1]:.1f} {end[0]:.1f} {end[1]:.1f}')
    d.append('Z')
    for k in range(5):                         # the seeds: holes, so the sheet behind shows
        a = 2 * math.pi * (k + .35 + turn) / 5
        d.append(_torn(rng, cx + 19 * math.cos(a), cy + 19 * math.sin(a), 6, 4, n=8, lump=.2, tear=.08))
    head = f'<path class="ink" fill-rule="evenodd" d="{"".join(d)}"/>'
    stem = '<path class="stem" d="M162 196C176 262 170 322 196 392S214 520 262 640"/>'
    leaves = ''.join([
        _leaf(178, 300, -128, 96, 30), _leaf(186, 322, -52, 104, 32),
        _leaf(206, 452, -150, 110, 34), _leaf(214, 478, -36, 92, 28),
        _leaf(246, 590, -118, 70, 22, slit=False),
        _leaf(62, 64, -150, 34, 11, slit=False), _leaf(48, 92, 170, 30, 10, slit=False),
        _leaf(78, 40, -110, 28, 9, slit=False),
    ])
    return (f'<svg class="mz-flower" viewBox="-10 20 330 640" aria-hidden="true">'
            f'{head}{stem}{leaves}</svg>')


def _dashes(rng, rows, cols, cls):
    """White brush dashes, leaning the same way, in loose diagonal rows."""
    out, w, h = [], cols * 40 + rows * 16 + 60, rows * 44 + 60
    for r in range(rows):
        for c in range(cols):
            if rng.random() < .12:
                continue
            ln = rng.uniform(34, 56)
            x = 10 + c * 40 + r * 16 + rng.uniform(-5, 5)
            y = 20 + r * 44 + rng.uniform(-6, 6)
            a = math.radians(rng.uniform(56, 64))
            out.append(f'<line class="dash" x1="{x:.1f}" y1="{y + ln * math.sin(a):.1f}" '
                       f'x2="{x + ln * math.cos(a):.1f}" y2="{y:.1f}"/>')
    return f'<svg class="{cls}" viewBox="0 0 {w} {h}" aria-hidden="true">{"".join(out)}</svg>'


def _matisse():
    rng = random.Random(1947)                  # the year of Jazz
    return ('<div class="mz">'
            f'<svg class="mz-sage" viewBox="0 0 700 560"><path class="sage" d="{_torn(rng, 350, 280, 300, 230)}"/></svg>'
            f'<svg class="mz-rose" viewBox="0 0 560 460"><path class="rose" d="{_torn(rng, 280, 230, 240, 190)}"/></svg>'
            f'{_dashes(rng, 4, 7, "mz-dash1")}{_dashes(rng, 5, 4, "mz-dash2")}{_flower(rng)}'
            '</div>')


def secs(x):
    """A duration read by a person: ``0.84s``, ``8.4s``, ``34s``, ``2m 05s``, ``1h 05m``."""
    if x is None:
        return '&mdash;'
    # Each band ends where its own rounding would carry into the next: 0.996 is 1.0s, not
    # 1.00s; 9.96 is 10s, not 10.0s; 59.6 is 1m 00s, not 60s.
    if x < 0.995:
        return f'{x:.2f}s'
    if x < 9.95:
        return f'{x:.1f}s'
    if x < 59.5:
        return f'{x:.0f}s'
    m, sec = divmod(int(round(x)), 60)
    if m < 60:
        return f'{m}m {sec:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h {m:02d}m'


HOUR_MIN = 5            # a day with fewer timed responses is left empty, not drawn tall
LAT_H = 190             # the response-time chart's height, in the same units as RL_H
def render(model, public=False, style=None):
    """The page: the headline numbers, three time charts over one shared, zoomable range
    (limit windows, daily input, response time by day), and the composition pies.

    Everything else the model carries -- sessions, reconciliation, images, the window table,
    the data-quality counters and the disclosures that went with them -- is reported through
    `--json` and the stdout summary, not here -- the per-model response times and pace
    estimates, the hour of day, turns and tools among them.

    `public` is the page token-share publishes: the same page, less the two strings on it
    that come from the machine rather than from counting -- the top session's id and the
    name of its working directory.  `style` is the style the page opens in when the reader
    has none of their own remembered; the first by default.
    """
    t = model['totals']
    rl = model.get('rate_limits') or {}
    domain = _domain(model)

    # The weekly figure is the server's own percentage, not a token count of ours, and it
    # keeps that wording so a reader cannot take it for something this page measured.  Once
    # its window has reset, the last reading describes a week that is over, and nothing has
    # been read since: it is not shown as the current figure.
    cur = rl.get('current') or {}
    limit = limit_name(rl)
    wk_pct = cur.get('last_pct')
    wk_next, wk_now = cur.get('resets_at'), rl.get('now')
    if cur.get('expired'):
        wk_pct = None
        wk_note = (f'reset {rel(wk_now - wk_next)} ago &middot; no reading since'
                   if (wk_now and wk_next) else 'reset since the last reading')
    else:
        wk_note = (f'resets in {rel(wk_next - wk_now)}'
                   if (wk_now and wk_next and wk_next > wk_now) else 'reported by the server')

    sc = model.get('scope') or {}
    cat_note = None
    if sc.get('tokenizer_note'):
        # The note is an exception's first line, and those name paths on this machine: the
        # vocabulary's, the install directory's.  A public page says only what happened.
        why = ('the tokenizer was not available when this page was built.' if public
               else sc['tokenizer_note'])
        cat_note = (f'Not counted: {why} Every other figure on this page '
                    f'comes from the usage records and is unaffected.')
    elif sc.get('metrics_only'):
        cat_note = ('Not counted: this report was produced with --metrics-only, which reads '
                    'the usage records and does not tokenize anything.')

    # Input is shown as tiktoken counted it when the run tokenized (analyze, §5.7), and as
    # Codex recorded it otherwise.  Output and caching are always Codex's: reasoning tokens
    # are encrypted and caching is decided on the server, so neither can be counted here.
    tk = t.get('input_source') == 'tiktoken'
    shown = 'tiktoken_input' if tk else 'input'

    # `sessions` arrives sorted by the input the first tile shows.  The cwd is rollout
    # content, so it is escaped like every other string from there.
    top = (model.get('sessions') or [None])[0]
    top_tile = []
    if top:
        where = os.path.basename((top.get('cwd') or '').rstrip('/\\'))
        top_note = '' if public else ' &middot; '.join(html.escape(x) for x in
                                                       (str(top['session_id'])[:8], where) if x)
        top_tile = [tile('Longest session', big(top[shown]), top_note)]

    # The third time chart: response time by day, on the same axis as the other two.  When
    # nothing was timed its place says why, as the limit chart's does.
    lat_m = model.get('latency') or {}
    lat_days = [d for d in (lat_m.get('daily') or [])
                if d.get('start') is not None and d.get('end') is not None]
    # Rate-limit events stand behind the lines as bars, on an axis of their own.  A day the
    # limit blocked outright has events and no timed response, so they keep their own days,
    # and they alone are enough for the page to draw the chart.
    ev_days = [d for d in ((model.get('limit_events') or {}).get('daily') or [])
               if d.get('n') and d.get('start') is not None and d.get('end') is not None]
    lat_why = (f'Response time not available &mdash; '
               f'{esc(lat_m.get("reason") or "nothing was timed")}' if not lat_m.get('available')
               else 'No timed responses on a dated day in range')
    lat_inner = '' if lat_days or ev_days else f'<p class="sub">{lat_why}.</p>'  # else drawLat
    legend = []
    if lat_days:
        legend += ['<span><i style="background:var(--uncached)"></i>median response time</span>',
                   '<span><i style="background:var(--dim)"></i>p90</span>']
    elif ev_days:
        legend.append(f'<span>{lat_why}</span>')
    if ev_days:
        legend.append('<span><i style="background:var(--warn)"></i>rate-limit events '
                      '(right axis)</span>')
    elif lat_days:
        legend.append('<span>no rate-limit events logged</span>')
    lat_chart = (f'<div class="panel"><div class="chart" id="latchart">{lat_inner}</div>'
                 + (f'<div class="legend">{"".join(legend)}</div>' if legend else '')
                 + '</div>\n')

    # A tile, not only the panel: the tiles are what every style draws, the 3D one included.
    lat = model.get('latency') or {}
    lat_r = lat.get('responses') or {}
    lat_tile = ([tile('Response time', secs(lat_r.get('median_s')),
                      f"median &middot; p90 {secs(lat_r.get('p90_s'))}")]
                if lat.get('available') else [])

    tiles = ''.join([
        (tile('Input', big(t['tiktoken_input']),
              f"counted with tiktoken &middot; {t['responses']:,} responses") if tk else
         tile('Recorded input', big(t['input']), f"{t['responses']:,} responses")),
        tile('Output', big(t['output']),
             f"{big(t['reasoning'])} reasoning" + (' &middot; recorded by Codex' if tk else '')),
        # Measured against Codex's own input, never the tiktoken count: that one misses what
        # the logs do not keep, and cached would then exceed the input it is a share of.
        tile('Cache hit', pct(t['cache_hit']),
             f"{big(t['cached'])} of {big(t['input'])} recorded by Codex" if tk
             else f"{big(t['cached'])} cached"),
    ] + [x for x in (api_tile(model.get('api_value')),) if x] + [
        tile('Sessions', f"{t['sessions']:,}", f"{t['threads']:,} threads"),
    ] + top_tile + lat_tile + ([tile(f'{limit[0].upper()}{limit[1:]} limit used',
               '&mdash;' if wk_pct is None else f'{wk_pct:g}%', wk_note)]
         if rl.get('available') else []))

    if not rl.get('available'):
        rl_chart = (f'<div class="panel"><p class="sub">No rate-limit snapshots in range '
                    f'&mdash; {esc(rl.get("reason") or "none recorded")}.</p></div>')
    else:
        rl_chart = f"""<div class="panel">
  <div class="chart" id="rlchart"></div>
  <div class="legend">
    <span><i style="background:var(--uncached)"></i>cumulative tokens</span>
    <span><i style="background:var(--warn)"></i>{limit} limit</span>
  </div>
</div>"""

    # Only the limit series, the content buckets and the shared geometry are read by the
    # page's JS; the deep-dive data it used to carry went out with the section that drew it.
    payload = _script_json({
        'geo': {'w': CHART_W, 'l': CHART_L, 'r': CHART_R,
                'rl_h': RL_H, 'rl_t': RL_T, 'rl_b': RL_B, 'lat_h': LAT_H},
        'domain': domain,
        'styles': STYLES,
        'gl_styles': GL_STYLES,
        'scene_styles': SCENE_STYLES,
        'rate_limits': {
            'name': limit,
            'now': rl.get('now'),
            'current': rl.get('current'),
            'windows': [{k: w[k] for k in
                         ('index', 'reset_at', 'reset_at_iso', 'resets_at', 'resets_at_iso',
                          'peak_pct', 'last_pct', 'tokens', 'pct_points', 'cum_points',
                          'late_points')}
                        for w in (rl.get('windows') or [])],
        },
        'cats': {
            'series': model.get('cat_series') or [],
            'bucket': model.get('cat_bucket_s') or 3600,
            'order': [c['category'] for c in (model.get('categories') or [])],
            'note': cat_note,
        },
        # Which input the charts draw: 'tiktoken' or 'recorded' (Codex's).  Labels follow it.
        'input_source': t.get('input_source') or 'recorded',
        # The daily chart's own ranking and cap, so the model pie colours match its bars.
        # The response-time chart's days, [start, end, timed responses, median, p90], how
        # many responses a day needs before it is drawn, and the rate-limit events drawn
        # behind them, [start, end, events].
        'latency': {'days': [[d['start'], d['end'], d['n'], d.get('median_s'), d.get('p90_s')]
                             for d in lat_days],
                    'min': HOUR_MIN,
                    'events': [[d['start'], d['end'], d['n']] for d in ev_days]},
        'models': {
            'order': [m['model'] for m in model['models'] if m['model']][:DAILY_MODELS],
            'days': [[d['start'], d['end'], d.get('tiktoken_models' if tk else 'models') or {}]
                     for d in model['daily']
                     if d.get('start') is not None and d.get('end') is not None],
        },
    })

    # The masthead's one line of context, written here so it reads the same with JS off.
    fmt = lambda ts: time.strftime('%b %d, %Y', time.localtime(ts))
    dek = ' &middot; '.join(x for x in (
        f'{fmt(domain[0])} &ndash; {fmt(domain[1])}' if domain else '',
        f"{t['sessions']:,} sessions", f"{t['responses']:,} responses") if x)
    at = next((i for i, (sid, _) in enumerate(STYLES) if sid == style), 0)
    first, nxt = STYLES[at], STYLES[(at + 1) % len(STYLES)]

    # Marked when --style chose it, so the page opens in it over a style remembered from
    # another report (initStyles).
    pinned = ' data-style-set' if any(sid == style for sid, _ in STYLES) else ''

    return f"""<!doctype html>
<html lang="en" data-style="{first[0]}"{pinned}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Token Report</title>
<style>{CSS}{STYLE_CSS}</style></head><body>
<div class="deco" aria-hidden="true">{_matisse()}<div class="wipe"></div></div>
<nav class="bar"><div class="brand"><a href="https://tokenusage.dev">tokenusage.dev</a></div><div><button id="stylebtn" type="button" data-next="{nxt[0]}" aria-label="Switch to the {esc(nxt[1])} style"><span class="sdot" aria-hidden="true"></span></button></div></nav>
<div class="wrap">

<header class="mast">
  <div class="kicker"></div>
  <h1>Codex Token Report</h1>
  <p class="dek">{dek}</p>
</header>

<div class="tiles">{tiles}</div>


{rl_chart}

<div class="panel"><div class="chart" id="dailychart">{_daily_svg(
    model['daily'], [m['model'] for m in model['models']], domain or [0, 1],
    tiktoken=tk)}</div></div>

{lat_chart}
<div class="panel pies"><div id="catpie"></div><div id="modelpie"></div></div>


</div>
<script>window.__TC__ = {payload};</script>
<script>{JS}{GL_JS}{SCENE_JS}{STYLE_JS}</script>
</body></html>
"""
