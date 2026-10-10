// Invented Claude CLI pages, executed in Node against a stub DOM. No browser or network.
const {execFileSync} = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), vm = require('vm');
const REPO = path.dirname(__dirname), WIDTH = 1148;
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-page-'));
let bad = 0, count = 0;
function check(name, okay) {
  count++; if (!okay) bad++;
  console.log(`[${okay ? 'PASS' : 'FAIL'}] ${name}`);
}
function node(attrs = {}) {
  return {attrs, innerHTML:'', events:{}, classList:{add(){},remove(){}},
    getAttribute(k){return this.attrs[k] === undefined ? null : this.attrs[k];},
    setAttribute(k,v){this.attrs[k] = String(v);},
    addEventListener(k,fn){this.events[k]=fn;}, setPointerCapture(){},
    getBoundingClientRect(){return {left:0,top:0,width:WIDTH,height:300};},
    querySelector(sel){return (this.kids||[]).find(k=>k.sel===sel)||null;},
    querySelectorAll(sel){return (this.kids||[]).filter(k=>k.sel===sel);}};
}
function execute(html) {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
  if(scripts.length !== 2) throw Error('expected two scripts');
  const dailySource = html.slice(html.indexOf('id="dailychart"'));
  const bars = [...dailySource.matchAll(/<g class="bar" data-a="(\d+)" data-b="(\d+)"[^>]*>([\s\S]*?)<\/g>/g)]
    .map(m=>Object.assign(node({'data-a':m[1],'data-b':m[2]}),{sel:'g.bar',
      kids:[...m[3].matchAll(/<rect x="[^"]*" y="([^"]*)" width="[^"]*" height="([^"]*)" fill="([^"]*)" class="mk">/g)]
        .map(r=>Object.assign(node({y:r[1],height:r[2],fill:r[3]}),{sel:'rect.mk'}))}));
  const axes = ['.clip','.base','.peak','.ax'].map(sel=>Object.assign(node(),{sel}));
  const svg = Object.assign(node({'data-h':'210','data-t':'18','data-b':'34'}),{sel:'svg',kids:[...bars,...axes]});
  const hosts = {rlchart:node(),dailychart:Object.assign(node(),{kids:[svg]}),latchart:node(),
    catpie:node(),modelpie:node(),limitmetric:node(),limitmetriclabel:node()};
  hosts.limitmetric.value = 'tokens';
  const raf=[], timers=new Map(); let tid=0;
  const deny = ()=>{throw Error('runtime network attempted');};
  const ctx={console,requestAnimationFrame(fn){raf.push(fn);return raf.length;},
    setTimeout(fn){timers.set(++tid,fn);return tid;},clearTimeout(id){timers.delete(id);},
    addEventListener(){},fetch:deny,XMLHttpRequest:deny,WebSocket:deny,navigator:{sendBeacon:deny},
    document:{getElementById:id=>hosts[id]||null,querySelector:s=>s==='.chart'?hosts.rlchart:null}};
  ctx.window=ctx;ctx.globalThis=ctx;vm.createContext(ctx);
  scripts.forEach(s=>vm.runInContext(s,ctx));
  const run = code=>vm.runInContext(code,ctx);
  const flush=()=>{for(;;){while(raf.length)raf.shift()();if(!timers.size)return;
    const [id,fn]=timers.entries().next().value;timers.delete(id);fn();}};
  flush();return {hosts,run,flush,bars,axes,ctx};
}
const legacy = ['Codex Token Report','Codex usage','recorded by Codex','Longest session',
  'if billed at API price','reasoning</div>',' threads</div>','What filled the window',
  'tokens in view','content composition by category','No tokenized content in the visible range.',
  'No content in range.','No weekly-limit snapshots in range.','No rate-limit snapshots in range',
  'cumulative API value','reported by the server','reset since the last reading',
  'Not counted: this report was produced with --metrics-only'];
const exact = ['Claude Code Token Report','Claude Code usage, recorded locally',
  'Papiers découpés — Claude Code usage, cut from local records',
  'Nocturne in blue and gold — Claude Code usage, recorded locally',
  'base input + cache writes + cache reads · 7 responses',
  '7 recorded thinking tokens · unavailable for 5 responses',
  '690 read · 40 5m writes · 170 1h writes · 0 writes with unknown TTL',
  '2 streams','Largest session','API list value',
  'captured usage at Anthropic list prices; assumptions and exclusions in JSON and the terminal summary',
  'Visible text inventory','UTF-8 bytes in view','visible text inventory by category',
  'No captured text bytes in the visible range.','Text inventory was skipped with --metrics-only.',
  'weekly all-model','No weekly all-model readings in range.','No structured limit readings in range.',
  'last recorded weekly reading','Reset since the last weekly reading; no current percentage recorded.',
  'nominal seven-day start, inferred from reset',
  'captured usage between the first reading and the first peak reading',
  'recorded /usage and weekly 429 points','weekly 429 refusal: 100%, assumed all-model',
  'Recorded tokens','cumulative recorded input and output per nominal weekly window',
  'Response time is the interval from the last known prompt-side record to the last response record.',
  'Anthropic API price table · 2026-10-10 · vendored locally',
  'Byte shares describe captured text and saved snapshots. They are not Claude token shares or a reconstruction of the full API prompt.',
  'Recorded input includes base input, cache creation and cache reads. Recorded output already includes thinking when that subset is available.',
  'Captured usage can omit calls without transcript usage, activity on other devices, and transcripts removed before the first capture.',
  'Quota points are sparse recorded readings. The report does not derive quota percentages from tokens.',
  'API list value is a comparison at the vendored price table, not a bill. Missing settings, fees and unpriced models are shown separately.',
  'Historical plan labels use captured account observations and subscriptionCreatedAt; they are not plan records recovered from transcripts.',
  'A plan change that leaves subscriptionCreatedAt unchanged is not detectable until a later run observes the new tier. A window that ended before that run can therefore carry the old plan.'];
try {
  const py = `import sys\nsys.path.insert(0, ${JSON.stringify(path.join(REPO,'scripts'))})\nimport test_claude_pipeline as tp\ntp.page_fixtures(${JSON.stringify(temp)})`;
  execFileSync(process.env.PYTHON || 'python',['-I','-S','-B','-c',py],{cwd:REPO,stdio:'inherit'});
  for(const style of ['clinical','matisse','nocturne']) for(const publicPage of [false,true]) {
    const label=style+(publicPage?'-public':'-local');
    const html=fs.readFileSync(path.join(temp,label+'.html'),'utf8');
    const model=JSON.parse(fs.readFileSync(path.join(temp,label+'.json'),'utf8'));
    check(label+' exact Claude strings',exact.every(s=>html.includes(s)));
    if(exact.some(s=>!html.includes(s))) console.log('        missing: '+exact.filter(s=>!html.includes(s)).join(' | '));
    check(label+' legacy strings absent',legacy.every(s=>!html.includes(s)));
    const anchor='<a href="https://tokenusage.dev">tokenusage.dev</a>';
    check(label+' fixed brand anchor exactly once',html.split(anchor).length===2 && (html.match(/<a\s+href=/g)||[]).length===1);
    check(label+' no external runtime reference',!(/https?:\/\/|<script[^>]+src\s*=|\bimport\s*\(|\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|<form[^>]*(?:action|target)/i.test(html.replace(anchor,''))));
    check(label+' counters and diagnostic panels absent',Object.keys(model.quality).every(k=>!html.includes(k)) && !/id="(?:quality|history)/.test(html));
    check(label+' selector has both metrics',/id="limitmetric"/.test(html) && /value="tokens" selected>Recorded tokens/.test(html) && /value="usd">API list value/.test(html));
    check(label+' local identity boundary',publicPage?!html.includes('invented-identity')&&!html.includes('invented-organization'):html.includes('invented-identity&lt;&amp;&gt;'));
    const {hosts,run,flush,bars,axes,ctx}=execute(html);
    const data=run('D'),wins=run('WINS'),scene=()=>run('SCN').get(hosts.rlchart);
    const pointCount=wins.reduce((n,w)=>n+w.pct_points.length,0);
    check(label+' safe embedded chart data',data.profile.sparse_limit_points && data.profile.limit_metric==='tokens'
      && wins.every(w=>w.anchor_inferred&&w.reset_inferred&&w.cum_points.every(p=>p.length===4)
        && w.observation_start!=null&&w.observation_end!=null));
    check(label+' nominal cumulative input plus output',JSON.stringify(run('metricPoints(WINS[0]).map(p=>p[1])'))==='[110,330,660,1100,1650]');
    check(label+' sparse percentage circles, no interpolated quota path',scene().list.filter(m=>m.t==='point').length===pointCount
      && !scene().list.some(m=>m.t==='line'&&m.c==='--warn'&&!m.observation)
      && (hosts.rlchart.innerHTML.match(/data-limit-point=/g)||[]).length===pointCount);
    check(label+' inferred nominal mark and observation mark',scene().list.some(m=>m.nominal)&&scene().list.some(m=>m.observation)
      && hosts.rlchart.innerHTML.includes('data-nominal="inferred"')&&hosts.rlchart.innerHTML.includes('data-observation="span"'));
    check(label+' sourced 429 tooltip',hosts.rlchart.innerHTML.includes('weekly 429 refusal: 100%, assumed all-model'));
    const refusal=wins[0].readings.find(r=>r.source==='quota_429');
    wins[0].readings.unshift({...refusal,source:'usage_report'});run('redraw()');flush();
    check(label+' coincident /usage and 429 retain refusal provenance',hosts.rlchart.innerHTML.includes('data-limit-point="quota_429"'));
    const N3=run('N3'),cx={col:()=>[1,1,1,1],measure:(s,f,h)=>String(s).length*h*.55};
    const mesh=N3.windows(scene(),{vmax:run('VMAX'),wins,tk:[],view:run('VIEW')},cx);
    check(label+' Nocturne discrete point geometry and inferred marks',mesh.hits.filter(h=>h.reading!=null).length===pointCount
      && mesh.hits.some(h=>h.nominal)&&mesh.hits.some(h=>h.observation)&&mesh.solid.length>0);
    const longNotes=[data.profile.input_tile_note,data.profile.output_tile_note,data.profile.cache_tile_note,
      data.profile.api_tile_note,data.profile.sessions_unit,data.profile.api_tile_note];
    const ledgerMesh=N3.ledger({title:data.profile.title,kicker:'k',dek:'d',brand:'tokenusage.dev',
      tiles:longNotes.map((n,i)=>({k:'tile'+i,v:'123',n}))},cx);
    const tileNotes=ledgerMesh.words.filter(w=>w.h===.25&&w.y>0);
    check(label+' Nocturne wraps long tile notes within their columns',tileNotes.length>longNotes.length
      && tileNotes.every(w=>cx.measure(w.s,w.f,w.h)<4.5));
    const secondRow=ledgerMesh.words.filter(w=>/^TILE[345]$/.test(w.s));
    const firstNotes=tileNotes.filter(w=>w.y>Math.max(...secondRow.map(w=>w.y)));
    check(label+' Nocturne leaves space between note rows',firstNotes.length>0
      && Math.min(...firstNotes.map(w=>w.y))-Math.max(...secondRow.map(w=>w.y))>.25);
    const hoverBody=html.match(/  function hover\(e\)\{([\s\S]*?)\n  function bind\(el\)/)[1];
    const ex={hot:-1,m:{plot:true},key:'windows'}, testHit={ex,i:0,id:0,hit:{reading:0}};
    const hoverCtx={EX:[ex],F:0,pick:()=>testHit,tipFor:(_ex,h)=>h.reading,
      cv:{style:{}},req(){},drag:null,hov:null,need:false};
    vm.createContext(hoverCtx);vm.runInContext('function hover(e){'+hoverBody,hoverCtx);
    hoverCtx.hover({});testHit.hit={reading:1};hoverCtx.hover({});
    check(label+' Nocturne updates tooltips between points in one window',ex.tip===1);
    const before=JSON.stringify(run('VIEW')),oldPath=hosts.rlchart.innerHTML;
    hosts.limitmetric.value='usd';hosts.limitmetric.events.change();flush();
    check(label+' API list metric switches the same chart and viewport',run('LIMIT_METRIC')==='usd'&&JSON.stringify(run('VIEW'))===before
      && hosts.rlchart.innerHTML!==oldPath&&run('metricPoints(WINS[0])[4][1]')===.0069);
    run('setSpan((DOM[1]-DOM[0])/2, (DOM[0]+DOM[1])/2, L+PLOT/2)');flush();
    const view=run('VIEW');hosts.limitmetric.value='tokens';hosts.limitmetric.events.change();flush();
    check(label+' token metric retains the zoom',JSON.stringify(run('VIEW'))===JSON.stringify(view));
    check(label+' daily chart shares the viewport',bars.every(b=>Math.abs(+b.getAttribute('transform').match(/translate\(([-\d.]+),/)[1]
      -run(`X(${+b.getAttribute('data-a')})`))<.01)&&axes[3].innerHTML.includes('<text'));
    run('setSpan(86400,DOM[1],W-RM)');flush();
    check(label+' empty-range inventory uses Claude bytes text',hosts.catpie.innerHTML.includes('No captured text bytes in the visible range.'));
  }
  const metrics=execute(fs.readFileSync(path.join(temp,'metrics.html'),'utf8'));
  check('metrics-only inventory explanation',metrics.hosts.catpie.innerHTML.includes('Text inventory was skipped with --metrics-only.'));
  const emptyHtml=fs.readFileSync(path.join(temp,'empty.html'),'utf8');
  execute(emptyHtml);
  check('missing structured limits explanation',emptyHtml.includes('<p class="sub">No structured limit readings in range.</p>'));
  const weekly=execute(fs.readFileSync(path.join(temp,'weekly-missing.html'),'utf8'));
  check('missing weekly all-model explanation',weekly.hosts.rlchart.innerHTML.includes('No weekly all-model readings in range.'));
  const weeklyHtml=fs.readFileSync(path.join(temp,'weekly-missing.html'),'utf8');
  check('missing weekly tile never claims a last weekly reading',weeklyHtml.includes('<div class="n">No weekly all-model readings in range.</div>')
    && !weeklyHtml.includes('<div class="n">last recorded weekly reading</div>'));
  const complete=fs.readFileSync(path.join(temp,'complete.html'),'utf8');
  check('complete recorded thinking note',complete.includes('<div class="n">7 recorded thinking tokens</div>'));
  const expired=fs.readFileSync(path.join(temp,'expired.html'),'utf8');
  check('expired reading tile',expired.includes('<div class="v">&mdash;</div><div class="n">Reset since the last weekly reading; no current percentage recorded.</div>'));
} finally {
  // The only recursive deletion is the exact temporary directory created above.
  if(path.dirname(path.resolve(temp))!==path.resolve(os.tmpdir())) throw Error('temporary boundary');
  fs.rmSync(temp,{recursive:true,force:true});
}
console.log(`\n${count-bad}/${count} assertions passed (six CLI pages; no browser or network)`);
process.exitCode=bad?1:0;
