// The page's own JavaScript, run against a stub DOM.
//
//     node scripts/test_page.js            # renders its own fixture through Python
//     node scripts/test_page.js page.html  # or checks a report you already have
//
// The three charts share one time axis and one viewport (ARCHITECTURE.md section 7), and
// none of that is visible from the Python side: it lives in the script embedded in the
// page.  This loads that script, drives it the way a reader would, and asserts the two time
// charts stay in step with each other and with the composition pie.  Node only -- no npm,
// no browser, no network.
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');

const REPO = path.dirname(__dirname);

function fixture() {
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcpage-')), 'page.html');
  const py = [
    'import sys, io',
    `sys.path.insert(0, r'${path.join(REPO, 'scripts')}')`,
    'import test_pipeline as tp',
    `io.open(r'${out}', 'w', encoding='utf-8').write(tp.page_fixture())`,
  ].join('\n');
  execFileSync(process.env.PYTHON || 'python', ['-c', py], { cwd: REPO, stdio: 'inherit' });
  return out;
}

const file = process.argv[2] || fixture();
const html = fs.readFileSync(file, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length !== 2) throw new Error(`expected 2 scripts in the page, got ${scripts.length}`);

// ---- a DOM with just enough in it ---------------------------------------------------
const WIDTH = 1148;                        // the panel width the charts are measured at

function node(attrs = {}) {
  return {
    attrs,
    innerHTML: '',
    classList: { add() {}, remove() {} },
    getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener() {},
    setPointerCapture() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: WIDTH, height: 300 }; },
    querySelector(sel) { return (this.kids || []).find(k => k.sel === sel) || null; },
    querySelectorAll(sel) { return (this.kids || []).filter(k => k.sel === sel); },
  };
}

// The bar charts -- daily input, and response time by day -- are rendered by Python; the page
// only ever moves their bars, and reads their segments once, for the marks it records.  Each
// stub is built from its own chart's markup and no further: the next chart's bars must not
// be read as this one's.
function chartHtml(id) {
  const i = html.indexOf(`id="${id}"`);
  if (i < 0) return '';
  const rest = html.slice(i);
  const j = rest.indexOf('<div class="chart" id=', 1);
  return j > 0 ? rest.slice(0, j) : rest;
}
function barChart(id) {
  const src = chartHtml(id);
  if (!src) return null;
  const bars = [...src.matchAll(/<g class="bar" data-a="(\d+)" data-b="(\d+)"[^>]*>([\s\S]*?)<\/g>/g)]
    .map(m => Object.assign(node({ 'data-a': m[1], 'data-b': m[2] }), {
      sel: 'g.bar',
      kids: [...m[3].matchAll(/<rect x="[^"]*" y="([^"]*)" width="[^"]*" height="([^"]*)" fill="([^"]*)" class="mk">/g)]
        .map(r => Object.assign(node({ y: r[1], height: r[2], fill: r[3] }), { sel: 'rect.mk' })),
    }));
  const h = (src.match(/data-h="(\d+)"/) || [])[1];
  const parts = ['.clip', '.base', '.peak', '.ax'].map(sel => Object.assign(node(), { sel }));
  const svg = Object.assign(node({ 'data-h': h, 'data-t': '18', 'data-b': '34' }),
    { sel: 'svg', kids: [...bars, ...parts] });
  const [clip, base, peak, ax] = parts;
  return { src, bars, clip, base, peak, ax, h: +h, host: Object.assign(node(), { kids: [svg] }) };
}

const daily = barChart('dailychart');
if (!daily || !daily.bars.length) throw new Error('no daily bars in the page');
const dailyHtml = daily.src;
const { bars, clip, base, ax } = daily;
const latHost = node();                   // drawn by the page itself, like the limit chart
if (!html.includes('id="latchart"')) throw new Error('no response-time chart in the page');

const rlHost = node();
const dailyHost = daily.host;
const pieHost = node();
const modelHost = node();
const els = { rlchart: rlHost, dailychart: dailyHost, latchart: latHost, catpie: pieHost,
              modelpie: modelHost };

const raf = [];
const timers = new Map();                  // id -> fn; run by flush(), never by the clock
let timerId = 0;
const ctx = {
  console,
  requestAnimationFrame(fn) { raf.push(fn); return raf.length; },
  setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
  clearTimeout(id) { timers.delete(id); },
  addEventListener() {},
  document: {
    getElementById: id => els[id] || null,
    querySelector: sel => (sel === '.chart' ? rlHost : null),
  },
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
scripts.forEach(s => vm.runInContext(s, ctx));

const run = code => vm.runInContext(code, ctx);
const frames = () => { while (raf.length) raf.shift()(); };
// Frames, then whatever they left waiting (the pies' debounce), until nothing is pending.
const flush = () => {
  for (;;) {
    frames();
    if (!timers.size) return;
    const [id, fn] = timers.entries().next().value;
    timers.delete(id);
    fn();
  }
};

let bad = 0;
const check = (name, ok, detail) => {
  console.log((ok ? '[PASS] ' : '[FAIL] ') + name + (ok ? '' : '\n        ' + detail));
  if (!ok) bad++;
};

const ticksOf = svg => [...svg.matchAll(
  /<text x="([\d.]+)" y="[\d.]+" text-anchor="middle" fill="var\(--dim\)" font-size="11">([^<]*)</g)]
  .map(m => m[1] + '=' + m[2]);
const transforms = () => bars.map(b => b.getAttribute('transform'));
const pieTotal = () => (pieHost.innerHTML.match(/<b>([^<]+)<\/b>/) || [])[1] || null;
const modelTotal = () => (modelHost.innerHTML.match(/<b>([^<]+)<\/b>/) || [])[1] || null;

flush();                                   // the page drew itself once, at full extent

const DOM = run('DOM'), W = run('W'), L = run('L'), RM = run('RM');
check('the page measures the panel it is in', W === WIDTH && L <= 62 && RM <= 48,
      `W=${W} L=${L} RM=${RM}`);

// 1. one axis: the same ticks, at the same x, in both time charts
const t1 = ticksOf(rlHost.innerHTML), t2 = ticksOf(ax.innerHTML);
check('both charts draw the same ticks at the same x',
      t1.length >= 2 && JSON.stringify(t1) === JSON.stringify(t2),
      JSON.stringify([t1, t2]));

check('the daily chart has tick labels but no vertical guides; the limit chart keeps its guides',
      !/<line/.test(ax.innerHTML) && /stroke-dasharray="2 4"/.test(rlHost.innerHTML),
      ax.innerHTML.slice(0, 120));

// 2. the same instant lands on the same x in both charts
const a0 = +bars[0].getAttribute('data-a');
const xOfDay = +transforms()[0].match(/translate\(([-\d.]+),/)[1];
check('a day bar starts where the limit chart puts that instant',
      Math.abs(xOfDay - run(`X(${a0})`)) < 0.01, `${xOfDay} vs ${run(`X(${a0})`)}`);
check('the plot area is the same in both charts',
      +clip.getAttribute('x') === L && +clip.getAttribute('width') === W - L - RM
      && +base.getAttribute('x1') === L && +base.getAttribute('x2') === W - RM,
      `clip=${clip.getAttribute('x')}/${clip.getAttribute('width')} base=${base.getAttribute('x1')}..${base.getAttribute('x2')}`);

// 2a. the response-time chart is the third chart on the same axis: two lines, a point a day
const LD = run('LDAYS'), LMIN = run('LAT_MIN');
const drawable = LD.filter(r => r[2] >= LMIN && r[3] != null);
const latList = () => (run('SCN').get(latHost) || { list: [] }).list;
const lineMarks = () => latList().filter(m => m.t === 'line');
const evMarks = () => latList().filter(m => m.t === 'rect');
/** Every point of every line sits at the middle of a drawable day, at the page's own X. */
const onDays = () => lineMarks().every(m => m.pts.every(p =>
  drawable.some(r => Math.abs(p[0] - run(`X(${(r[0] + r[1]) / 2})`)) < 0.01)));
check('the response-time chart is two lines: the median solid, the p90 dashed',
      lineMarks().some(m => m.t === 'line' && !m.dash && m.c === '--uncached')
      && lineMarks().some(m => m.t === 'line' && m.dash && m.c === '--dim')
      && latList().every(m => m.t === 'line' || m.t === 'rect'), JSON.stringify(latList().map(m => [m.t, m.c, !!m.dash])));
const d0 = drawable[0], bar0 = bars.find(b => +b.getAttribute('data-a') === d0[0]);
check('a day\'s point sits over that day\'s bar in the daily chart',
      onDays() && !!bar0
      && Math.abs(+bar0.getAttribute('transform').match(/translate\(([-\d.]+),/)[1] - run(`X(${d0[0]})`)) < 0.01,
      JSON.stringify(lineMarks()[0]));
check('the response-time chart draws the same ticks as the limit chart',
      JSON.stringify(ticksOf(latHost.innerHTML)) === JSON.stringify(t1), JSON.stringify(ticksOf(latHost.innerHTML)));
const thin = LD.find(r => r[2] < LMIN);
check('a thin day is not a point, keeps a hover target that says so, and breaks the line',
      !!thin && !lineMarks().some(m => m.pts.some(p => Math.abs(p[0] - run(`X(${(thin[0] + thin[1]) / 2})`)) < 0.01))
      && latHost.innerHTML.includes('too few to show'),
      JSON.stringify(thin));
const gapped = drawable.some((r, i) => i && r[0] - drawable[i - 1][1] > 3600);
const spans = lineMarks().flatMap(m => m.pts.slice(1).map((p, i) => run(`Tat(${p[0]}) - Tat(${m.pts[i][0]})`)));
check('a gap in the days breaks the line rather than bridging it',
      gapped && spans.length >= 1 && spans.every(dt => dt <= 1.5 * 86400)
      && (latHost.innerHTML.match(/<circle /g) || []).length === 2 * drawable.length,
      JSON.stringify(spans));
// 2b. rate-limit events: a bar a day behind the lines, on an axis of its own at the right
const LEV = run('LEV'), EMAX = run('EMAX'), LH = 190 - 18 - 34;
/** Every event day on screen is one bar, centred on its day at the page's own X. */
const evOnDays = () => {
  const shown = LEV.filter(r => run(`X(${r[1]})`) >= L && run(`X(${r[0]})`) <= W - RM);
  return shown.length > 0 && evMarks().length === shown.length && evMarks().every(m => {
    const r = LEV.find(q => q[0] === m.day);
    return !!r && Math.abs(m.x + m.w / 2 - run(`X(${(r[0] + r[1]) / 2})`)) < 0.01;
  });
};
check('each day with rate-limit events is one bar, centred on its day',
      LEV.length === 3 && evOnDays() && evMarks().every(m => m.c === '--warn'),
      JSON.stringify(evMarks()));
check('a bar is drawn before the lines, so the lines stay on top of it',
      latHost.innerHTML.indexOf('fill-opacity=".5" class="mk"') < latHost.innerHTML.indexOf('<path'),
      '');
check('a bar is as tall as its count on the event axis, whose ceiling is even',
      EMAX === 8 && evMarks().every(m => Math.abs(m.h - LEV.find(r => r[0] === m.day)[2] / EMAX * LH) < 0.01),
      `EMAX=${EMAX} ${JSON.stringify(evMarks().map(m => m.h))}`);
const evAxis = [...latHost.innerHTML.matchAll(/fill="var\(--warn\)" font-size="11">([^<]*)</g)].map(m => m[1]);
check('the event axis is labelled at the right, in the limit colour, and the time axis keeps its own',
      JSON.stringify(evAxis) === '["0","4","8"]'
      && (latHost.innerHTML.match(/text-anchor="end" fill="var\(--dim\)"/g) || []).length === 3,
      JSON.stringify(evAxis));
const evW = new Map(evMarks().map(m => [m.day, m.w]));
const blocked = LEV.find(r => !LD.some(q => q[0] === r[0]));
check('a day with events and no timed response still gets its bar, and a hover target that says so',
      !!blocked && evMarks().some(m => m.day === blocked[0])
      && latHost.innerHTML.includes('7 rate-limit events</title>'), JSON.stringify(blocked));
check('a day\'s hover target names its events beside its response time, one event in the singular',
      latHost.innerHTML.includes('too few to show\n3 rate-limit events</title>')
      && latHost.innerHTML.includes('1 rate-limit event</title>'), '');

const lMarks = run('SCN').get(latHost);
check('the response-time chart records its marks inside its own plot',
      !!lMarks && JSON.stringify(lMarks.plot) === JSON.stringify([L, 18, W - L - RM, 190 - 18 - 34]),
      JSON.stringify(lMarks && lMarks.plot));

const fullPie = pieTotal();
check('the pie sums the whole range at full extent', !!fullPie, String(fullPie));

// The model pie is the daily bars regrouped: same total, same colour per model.
const fullModel = modelTotal();
const dailyTotal = run('(D.models.days||[]).reduce((s,r)=>s+Object.values(r[2]).reduce((a,b)=>a+b,0),0)');
check('the model pie sums every day the daily chart draws',
      fullModel === run(`big(${dailyTotal})`), `${fullModel} vs ${dailyTotal}`);
const firstModel = run('D.models.order[0]');
const barHasC0 = /<rect x="0.04"[^>]*fill="var\(--c0\)"/.test(dailyHtml);
check('a model keeps its daily-chart colour in the pie',
      !!firstModel && barHasC0
      && modelHost.innerHTML.includes(`background:var(--c0)"></i>${firstModel} `),
      `${firstModel} barHasC0=${barHasC0}`);

// 2b. the WebGL layer's marks are the SVG's marks: a GL style draws exactly what SVG did
const scn = host => run('SCN').get(host);
const rlMarks = scn(rlHost), dMarks = scn(dailyHost), pMarks = scn(pieHost);
check('the limit chart records its marks for the WebGL layer, clipped to the plot',
      !!rlMarks && rlMarks.list.some(m => m.t === 'area') && rlMarks.list.some(m => m.t === 'line')
      && JSON.stringify(rlMarks.clip) === JSON.stringify([L, 0, W - L - RM, 300]),
      JSON.stringify(rlMarks && rlMarks.clip));
const firstRect = dMarks && dMarks.list.find(m => m.t === 'rect');
check('the daily chart records its marks, and none outside the plot',
      !!dMarks && dMarks.list.length > 0 && dMarks.list.every(m => m.x + m.w >= L && m.x <= W - RM),
      JSON.stringify(firstRect));
const slices = pMarks && pMarks.list[0].slices;
check('the pie records one slice per row, summing to the whole circle',
      !!slices && Math.abs(slices.reduce((a, s) => a + s[0], 0) - 1) < 1e-9
      && slices.every(s => /^--c\d+$/.test(s[1])), JSON.stringify(slices));
check('without WebGL2 the layer stays out of the way', run('GLX') === null, String(run('GLX')));

// 2c. the 3D scene (Nocturne) builds its solids from those same marks.  Its runtime needs
// WebGL2 and stays null here; its geometry and its stage (N3) are pure, and checked.
check('without WebGL2 the 3D scene stays out of the way, and the page is its 2D sheet',
      run('S3D') === null && JSON.stringify(run('SCENE_STYLES')) === '["nocturne"]',
      String(run('S3D')));
check('the marks carry what the scene places them by: the plot, the window, the day',
      JSON.stringify(rlMarks.plot) === JSON.stringify([L, 18, W - L - RM, 300 - 18 - 34])
      && JSON.stringify(dMarks.plot) === JSON.stringify([L, 18, W - L - RM, 210 - 18 - 34])
      && rlMarks.list.every(m => Number.isInteger(m.win))
      && dMarks.list.every(m => m.day === +bars.find(b => b.getAttribute('data-a') == m.day).getAttribute('data-a')),
      JSON.stringify([rlMarks.plot, dMarks.plot]));
check('ticks can be laid over any width, and over the chart\'s own they are the page\'s',
      JSON.stringify(run('ticks(PLOT)')) === JSON.stringify(run('ticks()'))
      && run('ticks(PLOT*3)').length >= run('ticks()').length, '');
const N3 = run('N3');
const cx3 = { col: () => [1, 1, 1, 1], measure: (s, f, h) => String(s).length * h * .55 };
const verts = v => { const o = []; for (let i = 0; i < v.length; i += N3.VS) o.push(v.slice(i, i + N3.VS)); return o; };
{
  const info = { vmax: run('VMAX'), wins: run('WINS'), tk: run('ticks()'), view: run('VIEW'), title: 't' };
  const m = N3.windows(rlMarks, info, cx3);
  const areas = rlMarks.list.filter(m => m.t === 'area');
  const [px, py, pw, ph] = rlMarks.plot;
  const top = a => Math.max(...a.pts.map(p => (py + ph - p[1]) / ph)) * N3.WIN_H;
  check('every weekly window becomes one pane of glass, as tall as its curve',
        m.glass.length === areas.length && m.glass.every((g, i) =>
          Math.abs(Math.max(...verts(g.v).map(q => q[1])) - top(areas[i])) < 1e-6),
        JSON.stringify(m.glass.map(g => g.id)));
  const pct = run('Math.max(...WINS.flatMap(w => (w.pct_points||[]).map(p => p[1])))');
  const wire = verts(m.solid).filter(q => q[10] >= 0 && q[2] > .4);
  check('the gold wire reaches the reported peak, on the percentage axis',
        wire.length && Math.abs(Math.max(...wire.map(q => q[1])) - pct / 100 * N3.WIN_H) < .06,
        `${Math.max(...wire.map(q => q[1]))} vs ${pct / 100 * N3.WIN_H}`);
}
{
  const info = { peak: 'peak', tk: run('ticks()'), view: run('VIEW'), title: 't', legend: [] };
  const m = N3.daily(dMarks, info, cx3);
  const rects = dMarks.list.filter(r => r.t === 'rect');
  const [px, py, pw, ph] = dMarks.plot;
  const blocks = verts(m.solid).filter(q => q[10] >= 0);
  check('every stacked segment becomes one block of the skyline', rects.length > 0 && blocks.length === rects.length * 30,
        `${blocks.length / 30} blocks for ${rects.length} segments`);
  const tallest = Math.max(...rects.map(r => (py + ph - r.y) / ph)) * N3.DAY_H;
  check('the tallest column stands at the height its bar reaches on the page, less its sliver',
        Math.abs(Math.max(...blocks.map(q => q[1])) - tallest) <= .0181, `${Math.max(...blocks.map(q => q[1]))} vs ${tallest}`);
  check('and every block stands inside the plot, on the stone',
        blocks.every(q => q[0] >= -1e-9 && q[0] <= N3.PW + 1e-9 && q[1] >= 0), '');
}
{
  // The scene's response-time exhibit is built from the page's own line and bar marks.
  const lm = run('SCN').get(latHost);
  const v = run('VIEW'), spans = run('LSPANS');
  const info = { tk: [], view: v, pw: N3.PW, ylab: f => String(f), yrlab: f => String(f),
                 days: spans, legend: [] };
  const m = N3.lines(lm, info, cx3);
  const inView = spans.filter(r => r[1] > v[0] && r[0] < v[1]).length;
  check('the 3D scene builds the response-time lines, with one hover target a day, events or not',
        m.solid.length > 0 && m.hits.length === inView && inView === LD.length + 1
        && m.hits.every(h => h.box[0] >= -1e-9 && h.box[3] <= N3.PW + 1e-9 && spans.some(r => r[0] === h.day)),
        `${m.hits.length} hits for ${inView} days`);
  const bare = N3.lines(Object.assign({}, lm, { list: lm.list.filter(q => q.t !== 'rect') }), info, cx3);
  check('and every event bar becomes one slab standing behind the lines',
        (m.solid.length - bare.solid.length) / N3.VS === 30 * evMarks().length
        && verts(m.solid).filter(q => q[2] > -.36 && q[2] < -.11).length >= 30 * evMarks().length,
        `${(m.solid.length - bare.solid.length) / N3.VS} vertices for ${evMarks().length} bars`);
}
{
  const m = N3.medal(pMarks, { legend: { rows: [] }, hot: -1, title: 't' }, cx3);
  const sl = pMarks.list[0].slices.filter(s => s[0] > 1e-6);
  const turn = m.arcs.reduce((a, [, lo, hi]) => a + hi - lo, 0);
  check('a medallion\'s slices close the circle, one arc a slice', m.arcs.length === sl.length
        && Math.abs(turn - 2 * Math.PI) < 1e-9, `${m.arcs.length} arcs, ${turn}`);
  const [i, lo, hi] = m.arcs[0], mid = (lo + hi) / 2;
  check('pointing at a slice names it', N3.sliceAt(Math.cos(mid), m.medal.cy + Math.sin(mid), m) === i
        && N3.sliceAt(0, m.medal.cy + 10, m) === -1, '');
}
{
  const box = [-2.3, -.95, -1.5, 17.5, 7.65, 1.38];
  for (const asp of [16 / 9, 390 / 844]) {
    const cam = N3.camera(asp), P = N3.fore(box, cam);
    const M = N3.pose(P.p, P.yaw, P.pitch, P.roll, P.s, N3.centre(box));
    const q = N3.corners(box).map(c => N3.ndc(cam.VP, N3.xf(M, c)));
    check(`the chart in front fits the screen, square to it, at aspect ${asp.toFixed(2)}`,
          P.yaw === 0 && P.pitch === 0 && P.s === 1
          && q.every(v => v[0] >= -.94 && v[0] <= .94 && v[1] >= -.91 && v[1] <= N3.FORE_TOP + 1e-6)
          && P.p[1] - (N3.centre(box)[1] - box[1]) >= N3.FLOAT - 1e-9,
          JSON.stringify(P));
    const sl = N3.slots(4, cam);
    const cells = sl.map(s => [s.ndc[0] - s.cw / 2, s.ndc[0] + s.cw / 2, s.ndc[1] - s.ch / 2, s.ndc[1] + s.ch / 2]);
    const apart = cells.every((a, i) => cells.every((b, j) => i === j
      || a[1] <= b[0] + 1e-9 || b[1] <= a[0] + 1e-9 || a[3] <= b[2] + 1e-9 || b[3] <= a[2] + 1e-9));
    check(`the sky's slots sit above the front one and do not overlap, at aspect ${asp.toFixed(2)}`,
          sl.length === 4 && apart && cells.every(c => c[2] > N3.FORE_TOP), JSON.stringify(cells));
  }
  const a = { p: [1, 2, 3], yaw: .1, pitch: .2, roll: 0, s: 1, lit: 1 };
  const b = { p: [9, 8, -7], yaw: -.3, pitch: 0, roll: 0, s: .5, lit: .6 };
  const same = (x, y) => ['yaw', 'pitch', 'roll', 's', 'lit'].every(k => Math.abs(x[k] - y[k]) < 1e-9)
    && x.p.every((v, i) => Math.abs(v - y.p[i]) < 1e-9);
  check('a flight starts where the exhibit was and ends where it goes, whichever way it flies',
        ['down', 'up', 'glide'].every(k => same(N3.tween(a, b, 0, k), a) && same(N3.tween(a, b, 1, k), b)), '');
}

// 3. zoom: horizontal only, both charts, pie included
const before = { rl: rlHost.innerHTML, bars: transforms() };
const mid = (DOM[0] + DOM[1]) / 2;
// A quarter: the fixture spans six days, and an eighth would be under the one-day floor.
run(`setSpan((VIEW[1]-VIEW[0])/4, ${mid}, L+PLOT/2)`);
flush();
const view = run('VIEW');
check('zooming shrinks the visible span',
      (DOM[1] - DOM[0]) / 4 > run('MIN_SPAN')
      && Math.abs((view[1] - view[0]) - (DOM[1] - DOM[0]) / 4) < 1, `span=${view[1] - view[0]}`);
const axisOf = s => (s.match(/text-anchor="end"[^>]*>([^<]+)</g) || []).join('|');
check('the value axis is untouched by zooming',
      axisOf(before.rl) === axisOf(rlHost.innerHTML),
      `${axisOf(before.rl)}  vs  ${axisOf(rlHost.innerHTML)}`);
check('zooming one chart moved the other', transforms()[0] !== before.bars[0],
      transforms()[0]);
check('the day bars got wider, not taller',
      +transforms()[0].match(/scale\(([\d.]+),1\)/)[1]
      > +before.bars[0].match(/scale\(([\d.]+),1\)/)[1]
      && !/scale\([\d.]+,[^1]/.test(transforms()[0]), transforms()[0]);
const emptyPie = pieHost.innerHTML;
run(`setSpan((DOM[1]-DOM[0])/8, DOM[0], L)`);            // over the content, not a quiet gap
frames();
check('the pies wait while the viewport is still moving',
      pieHost.innerHTML === emptyPie && timers.size === 1, `${timers.size} timer(s) pending`);
run('panPx(-1)'); frames(); run('panPx(1)'); frames();
check('each move restarts the wait rather than stacking another', timers.size === 1,
      `${timers.size} timer(s) pending`);
flush();
check('the pie recomposes for the visible range',
      !!pieTotal() && pieTotal() !== fullPie && emptyPie !== pieHost.innerHTML,
      `${fullPie} -> ${pieTotal()}`);
const t1z = ticksOf(rlHost.innerHTML), t2z = ticksOf(ax.innerHTML);
check('the two charts still share ticks when zoomed',
      t1z.length >= 1 && JSON.stringify(t1z) === JSON.stringify(t2z),
      JSON.stringify([t1z, t2z]));
check('zooming moves the response-time chart with the other two',
      onDays() && JSON.stringify(ticksOf(latHost.innerHTML)) === JSON.stringify(t1z),
      JSON.stringify(lineMarks()[0] || null));
run(`setSpan((DOM[1]-DOM[0])/2, ${(LEV[0][0] + LEV[0][1]) / 2}, L+PLOT/2)`); flush();
check('zooming moves the event bars with their days, wider and no taller',
      evOnDays() && evMarks().every(m => m.w > 1.5 * evW.get(m.day)
                                         && Math.abs(m.h - LEV.find(r => r[0] === m.day)[2] / EMAX * LH) < 0.01),
      JSON.stringify(evMarks()));

// 4. drag moves the range by exactly the distance dragged
const v0 = run('VIEW').slice();
run('panPx(-100)');
flush();
const v1 = run('VIEW');
const expect = (v0[1] - v0[0]) * 100 / run('PLOT');
check('dragging moves the range by the distance dragged',
      Math.abs((v1[0] - v0[0]) - expect) < 1
      && Math.abs((v1[1] - v1[0]) - (v0[1] - v0[0])) < 1,
      `moved ${v1[0] - v0[0]}, expected ${expect}`);

// 5. the viewport cannot leave the range it was given
run('panPx(-1e9)'); flush();
check('panning stops at the end of the range', Math.abs(run('VIEW')[1] - DOM[1]) < 1,
      String(run('VIEW')));
run('panPx(1e9)'); flush();
check('panning stops at the start of the range', Math.abs(run('VIEW')[0] - DOM[0]) < 1,
      String(run('VIEW')));
run('setSpan(1, VIEW[0], L)'); flush();
check('zoom stops at one day, the finest range the content pie resolves',
      run('MIN_SPAN') === 86400
      && Math.abs((run('VIEW')[1] - run('VIEW')[0]) - 86400) < 1, String(run('VIEW')));
run('setSpan(1e12, VIEW[0], L)'); flush();
check('zooming out stops at the full range',
      Math.abs(run('VIEW')[0] - DOM[0]) < 1 && Math.abs(run('VIEW')[1] - DOM[1]) < 1,
      String(run('VIEW')));
check('the pie is whole again at full extent', pieTotal() === fullPie,
      `${pieTotal()} vs ${fullPie}`);
check('the model pie is whole again at full extent', modelTotal() === fullModel,
      `${modelTotal()} vs ${fullModel}`);

// 6. a window with nothing in it says so, rather than drawing an empty pie
run('VIEW = [DOM[0], DOM[0]+600]'); run('redraw()'); flush();
check('an empty slice is explained, not left blank',
      /No tokenized content in the visible range|<b>/.test(pieHost.innerHTML),
      pieHost.innerHTML.slice(0, 160));

// 7. a phone: the geometry is re-derived, and the labels keep their size
run(`document.querySelector = () => ({ getBoundingClientRect: () => ({left:0,top:0,width:334,height:210}) })`);
run('measure(); reset();'); flush();
check('the charts re-measure for a narrow screen',
      run('W') === 334 && run('L') < 62 && run('PLOT') > 200,
      `W=${run('W')} L=${run('L')} RM=${run('RM')} PLOT=${run('PLOT')}`);
const tm1 = ticksOf(rlHost.innerHTML), tm2 = ticksOf(ax.innerHTML);
check('a phone still gets readable ticks, the same ones in every time chart',
      tm1.length >= 2 && JSON.stringify(tm1) === JSON.stringify(tm2)
      && JSON.stringify(ticksOf(latHost.innerHTML)) === JSON.stringify(tm2), JSON.stringify([tm1, tm2]));
check('nothing is drawn outside the narrow plot area',
      tm1.every(t => +t.split('=')[0] >= run('L') - 0.5
                     && +t.split('=')[0] <= run('W') - run('RM') + 0.5), JSON.stringify(tm1));

// 8. the style the page opens in: one named with --style beats one remembered from another
// report; without one, the remembered style beats the default; the URL hash beats both.
function openedIn(rootAttrs, remembered, hash) {
  const stored = remembered ? { 'tc-style': remembered } : {};
  const root = Object.assign(node(Object.assign({}, rootAttrs)),
    { hasAttribute(k) { return this.attrs[k] !== undefined; } });
  const c = Object.assign({}, ctx, {
    document: Object.assign({}, ctx.document, { documentElement: root, addEventListener() {} }),
    localStorage: { getItem: k => (k in stored ? stored[k] : null), setItem(k, v) { stored[k] = v; } },
    location: { hash: hash || '' },
    history: { replaceState() {} },
  });
  c.window = c; c.globalThis = c;
  vm.createContext(c);
  scripts.forEach(src => vm.runInContext(src, c));
  return root.getAttribute('data-style');
}
const chosen = { 'data-style': 'matisse', 'data-style-set': '' };
check('a page rendered with --style opens in it, over a style remembered elsewhere',
      openedIn(chosen, 'nocturne') === 'matisse', openedIn(chosen, 'nocturne'));
check('without --style, the remembered style wins over the default',
      openedIn({ 'data-style': 'clinical' }, 'nocturne') === 'nocturne',
      openedIn({ 'data-style': 'clinical' }, 'nocturne'));
check('the style in the URL hash wins over both',
      openedIn(chosen, 'nocturne', '#style=clinical') === 'clinical',
      openedIn(chosen, 'nocturne', '#style=clinical'));

console.log(bad ? `\n${bad} FAILED` : `\n${'all page checks passed'}`);
process.exit(bad ? 1 : 0);
