#!/usr/bin/env node
'use strict';
// Run the very same inline page on invented Python fixtures, without packages.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const vm=require('node:vm'),crypto=require('node:crypto'),{spawnSync}=require('node:child_process');
const repo=path.resolve(__dirname,'..');
const selected=process.argv.includes('--section')?process.argv[process.argv.indexOf('--section')+1]:null;
if(selected&&!['model','draw','runtime'].includes(selected))throw Error('invalid section');
const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'token-film-page-'));
const made=spawnSync(process.env.PYTHON||'python',['-I','-S','-B','scripts/test_claude_film.py','--emit-fixtures',temporary],{cwd:repo,encoding:'utf8'});
if(made.status!==0){process.stdout.write(made.stdout||'');process.stderr.write(made.stderr||'');fs.rmSync(temporary,{recursive:true,force:true});process.exit(1);}
const fixtures={};
function load(name,extra={}){
  const html=fs.readFileSync(path.join(temporary,name+'.html'),'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const context=vm.createContext({Uint8Array,Uint16Array,Float32Array,Float64Array,ArrayBuffer,DataView,TextDecoder,TextEncoder,Blob,
    setTimeout,clearTimeout,AbortController,DOMException,...extra});
  vm.runInContext(script,context,{filename:'film.html',timeout:10000});
  return {api:context.TokenFilm,model:context.TokenFilm.buildModel(context.TokenFilm.FILM_DATA),context,html};
}
for(const name of fs.readdirSync(temporary).map(n=>n.replace('.html','')))fixtures[name]=load(name);
function plain(value){return JSON.parse(JSON.stringify(value));}
function near(actual,expected,epsilon=1e-9){assert.ok(Math.abs(actual-expected)<=epsilon,`${actual} != ${expected}`);}
function sum(values){return values.reduce((a,b)=>a+b,0);}
function pcmHash(pcm){const h=crypto.createHash('sha256');for(const channel of [pcm.left,pcm.right]){
  const bytes=Buffer.alloc(channel.length*4);for(let i=0;i<channel.length;i++)bytes.writeFloatLE(channel[i],i*4);h.update(bytes);}return h.digest('hex');}
const tests={model:[],draw:[],runtime:[]};
function test(section,name,fn){tests[section].push({name,fn});}
test('model','primary schedule, counts, contexts, phases and crossings',()=>{
  const {api:a,model:m}=fixtures.primary;
  assert.deepEqual(plain(m.TOTAL),{read:370,write:80155,thinking:15,writing:35,input:22});assert.equal(m.GRAND,80597);
  assert.deepEqual(plain(m.CUM),{read:[0,40,90,140,200,270,370],write:[0,50,150,80150,80150,80155,80155],input:[0,10,15,17,20,21,22],
    thinking:[0,3,7,12,14,14,15],writing:[0,5,11,26,30,34,35]});
  assert.deepEqual(plain(m.SPANS.filter(s=>s.kind==='steps').map(s=>[s.start,s.dur])),[[48,64],[112,44],[202,32],[276,20],[302,13],[315,10]]);
  assert.deepEqual(plain(m.SPANS.filter(s=>s.kind==='compact').map(s=>[s.start,s.dur])),[[156,46],[234,42],[296,6]]);
  assert.deepEqual([m.ACT2,m.END2,m.OVER,m.FINALE,m.TOTAL_AT,m.TAKEAWAY,m.BRAND,m.DURATION],[325,325,329,375,807,813,909,975]);
  assert.deepEqual(plain(m.ITEM_START),{read:409,write:501,thinking:593,writing:663,input:733});
  assert.deepEqual(plain(m.geometry),[{displayPre:155,setup:50,summary:10,displayPost:60},{displayPre:80052,setup:60,summary:20,displayPost:80},{displayPre:63,setup:63,summary:0,displayPost:63}]);
  assert.deepEqual([m.L,m.top0,m.top1,m.U,m.BALLS.length,m.R],[200000,100000,100000,100,804,9]);
  assert.deepEqual(plain(m.RECACHE),[2]);
  const writes=m.BALLS.filter(b=>b.cat==='write'),reads=m.BALLS.filter(b=>b.cat==='read');
  near(writes[0].frame,118.16);near(writes[1].frame,202.0056);near(writes.at(-1).frame,210.9544);
  [208.144,285.6,317.28].forEach((v,i)=>near(reads[i].frame,v));
  for(const [cat,s,value] of [['write',2.14,40150],['read',2.30,115],['input',2.30,16],['thinking',2.56,9.5],['writing',2.74,18.5]])near(a.valueAt(m,cat,s),value);
  near(a.pileAt(m,2.14),40056);near(a.frameOfStep(m,2),156);near(a.frameOfStep(m,2.01),202.32);
  assert.equal(m.CHAT_ITEMS.length,24);assert.deepEqual(plain(m.CHAT_ITEMS.map(i=>i.seed).sort((a,b)=>a-b)),Array.from({length:24},(_,i)=>i+1));
  assert.equal(m.CHAT_ITEMS.find(i=>i.label==='Compacting').kind,'compact');
  assert.equal(m.stamp,'2 minutes · 6 steps');assert.equal(m.subtitle,'2 minutes, 6 steps');
  [48,112,203,277,303,316].forEach((f,i)=>assert.equal(a.formatTimeHeader(m,f),`${i===5?'Minute 2':'Minute 1'} · step ${i+1}`));
});
test('model','threshold categories, money and gumballs',()=>{
  const above=fixtures.missing_thinking.model,a=fixtures.primary.api,under=fixtures.under_threshold.model;
  assert.deepEqual(plain(above.CATS),['read','write','writing','input']);assert.equal(above.TOTAL.writing,50);
  assert.deepEqual(plain(above.CUM.writing),[0,8,18,38,44,48,50]);near(a.valueAt(above,'writing',2.74),28);
  assert.deepEqual([above.DURATION,above.DURATION*1600,above.BALLS.length],[905,1448000,804]);
  assert.deepEqual(plain(above.ITEM_START),{read:409,write:501,writing:593,input:663});
  assert.deepEqual(plain(under.CUM.thinking),[0,3,7,12,14,14,14]);assert.deepEqual(plain(under.CUM.writing),[0,5,11,226,230,234,236]);
  assert.deepEqual([under.GRAND,under.U,under.BALLS.length,under.R,under.DURATION],[80797,100,806,9,975]);near(a.valueAt(under,'writing',2.74),118.5);
  const writes=under.BALLS.filter(b=>b.cat==='writing');near(writes[0].frame,225.01916279069766);near(writes[1].frame,228.5912558139535);
  assert.equal(under.SPECKS.writing,undefined);assert.equal(above.SPECKS.thinking,undefined);assert.equal(fixtures.zero_input.model.SPECKS.input,undefined);
  assert.ok(under.SPECKS.thinking);assert.equal(fixtures.no_prices.model.data.pricing.available,false);
});
test('model','small, dense and consecutive boundary schedules',()=>{
  const a=fixtures.primary.api,m=fixtures.twentyone.model,d=fixtures.dense.model,e=fixtures.edges.model;
  assert.deepEqual([m.ACT2,m.B0,m.B,m.END2,m.DURATION,m.DURATION*1600,m.U,m.BALLS.length],[292,38,38,330,980,1568000,1,21]);
  assert.deepEqual([d.ACT2,d.pressure,d.B0,d.B,d.P,d.END2,d.DURATION,d.DURATION*1600],[292,3,500,750,300,1042,1692,2707200]);
  assert.equal(d.SPANS.at(-1).dur,450);
  assert.deepEqual(plain(e.SPANS.filter(s=>s.kind==='compact').map(s=>s.start)),[48,118,124,174]);
  assert.deepEqual(plain(e.SPANS.filter(s=>s.kind==='steps').map(s=>s.start)),[54,130]);assert.equal(e.END2,180);assert.equal(e.DURATION,830);
  assert.equal(a.compactAt(e,124).index,2);assert.equal(a.compactAt(e,124).p,0);
  assert.equal(a.pileState(e,181).height,1);
  const beforeFirst=plain(fixtures.primary.model.data);
  beforeFirst.compactions=[{after:0,trigger:'manual',pre:1000000,post:900000}];beforeFirst.steps[0].read=300000;
  assert.equal(a.pileState(a.buildModel(beforeFirst),47).height,0,'intro must not expose a future boundary level');
  for(const f of [m,d]){const sampled=f.CHAT_ITEMS.filter(i=>i.frame>=f.ACT2+2&&i.kind==='text');
    for(const item of sampled)near((item.frame-f.ACT2-2)%4,0);}
  assert.deepEqual(plain(a.allocateRuns([1,10,1],[38,7,7],70)),[38,25,7]);
  for(const budget of [52,53,70,99,123]){const run=a.allocateRuns([1,10,1],[38,7,7],budget);assert.equal(sum(run),budget);run.forEach((n,i)=>assert.ok(n>=[38,7,7][i]));}
});
test('model','caption slots, reference-derived window arithmetic and formatting',()=>{
  const {api:a,model:m}=fixtures.primary;
  for(const [slot,expected] of Object.entries({teach:[[54,108],[112,162],[166,315]],compact:[[160,212],[236,286]],recache:[[156,178]],ball:[[122.16,170.16]],speck:[[607,653],[677,723],[747,797]]})){
    const list=m.CAPTIONS.filter(c=>c.slot===slot);assert.equal(list.length,expected.length);expected.forEach((p,i)=>{near(list[i].start,p[0]);near(list[i].end,p[1]);});}
  // Act 1 uses top0 = 100,000: 1586 - 80052*888/100000 - 30 = 845.13824.
  assert.equal(a.scaleTop(m,m.STEP_FRAME[2]),100000);near(m.CAPTIONS.find(c=>c.slot==='recache').y,845.13824);
  assert.ok(a.captionsAt(m,168).length>=4,'different slots coexist');assert.equal(fixtures.physical_prompt.model.CHAT_ITEMS.length,25);
  const derived=[[563,24,207,383,575.8451375400655],[593,16,108,258,599.8645596071947],[673,16,221,248,686.8497658573397],[745,32,445,515,771.7667913374753]];
  for(const [start,dur,n,d,expected] of derived)near(start+dur*a.runEaseInv(n/d),expected);
  const data=plain(m.data);data.steps.forEach(s=>{s.input+=s.read;s.read=0;});const noReads=a.buildModel(data);
  assert.ok(noReads.CAPTIONS.some(c=>JSON.stringify(c.lines).includes('Every step ')));
  const interrupted=plain(m);interrupted.CAPTIONS=[{slot:'teach',start:0,end:4},{slot:'teach',start:4,end:20}];
  assert.equal(a.captionsAt(interrupted,6).length,2,'out-animation tail survives interruption');
  assert.equal(a.formatSpeck(1,1000),'<0.01 of one gumball');assert.equal(a.formatSpeck(999,1000),'0.99 of one gumball');
  assert.equal(a.formatUnit(1),'1 token');assert.equal(a.formatUnit(1000000),'1 million tokens');assert.equal(a.formatUnit(100),'100 tokens');
  assert.equal(a.formatShare(370,80597),'0.46');assert.equal(a.formatSpan(4*86400000),'5 days');
  assert.equal(a.formatShare(0,80597),'0');
  assert.equal(a.formatCounter(1677200181,1677200181).width,207);assert.ok(a.formatCounter(9007199254740991,9007199254740991).suffix);
  assert.equal(a.roundRatio(35792,67112,100),53);
  const single=fixtures.r1_standard_global.model;
  assert.equal(single.END2,112);assert.equal(single.DURATION,762);assert.equal(single.DURATION*1600,1219200);
  assert.equal(single.stamp,'1 minute · 1 steps');
  const close=plain(m.data);close.compactions[1].after=close.compactions[0].after;
  const slotted=a.buildModel(close),compact=slotted.CAPTIONS.filter(c=>c.slot==='compact');
  assert.equal(compact[0].end,compact[1].start);assert.ok(a.captionsAt(slotted,compact[1].start+3).includes(compact[0]));
});
test('model','score length, level, sample indices and repeatability',()=>{
  for(const name of ['primary','missing_thinking','under_threshold','twentyone','dense','edges','r1_standard_global']){
    const {api:a,model:m}=fixtures[name],pcm=a.score(m),again=a.score(m);assert.equal(pcm.left.length,m.DURATION*1600);assert.equal(pcm.right.length,m.DURATION*1600);
    assert.ok(pcm.left instanceof Float32Array);assert.equal(pcm.left[0],0);assert.equal(pcm.right[0],0);
    let energy=0,peak=0;for(let i=0;i<pcm.left.length;i++){assert.ok(Number.isFinite(pcm.left[i])&&Number.isFinite(pcm.right[i]));energy+=pcm.left[i]**2+pcm.right[i]**2;peak=Math.max(peak,Math.abs(pcm.left[i]),Math.abs(pcm.right[i]));}
    assert.ok(peak<=.7);const rms=Math.sqrt(energy/(pcm.left.length*2));assert.ok(rms>.08&&rms<.14,rms);assert.equal(pcmHash(pcm),pcmHash(again));
  }
});

function canvasHarness(patternTransform=false){
  const contexts=[],calls=[];let recording=false;
  const properties=['globalAlpha','globalCompositeOperation','fillStyle','strokeStyle','lineWidth','lineCap','lineJoin','miterLimit','font','textAlign','textBaseline','lineDashOffset'];
  function record(name,args){for(const x of args)if(typeof x==='number')assert.ok(Number.isFinite(x),`${name}: nonfinite ${x}`);
    if(recording)calls.push([name,...args.map(x=>x&&typeof x==='object'?(x.width!==undefined?['canvas',x.width,x.height]:x.kind||'object'):x)]);}
  function multiply(a,b){return [a[0]*b[0]+a[2]*b[1],a[1]*b[0]+a[3]*b[1],a[0]*b[2]+a[2]*b[3],a[1]*b[2]+a[3]*b[3],a[0]*b[4]+a[2]*b[5]+a[4],a[1]*b[4]+a[3]*b[5]+a[5]];}
  class Context{
    constructor(canvas){this.canvas=canvas;this.state={globalAlpha:1,globalCompositeOperation:'source-over',font:'10px Fredoka',lineWidth:1,lineCap:'butt',lineJoin:'miter',lineDashOffset:0};this.matrix=[1,0,0,1,0,0];this.stack=[];this.dash=[];contexts.push(this);}
    save(){this.stack.push({state:{...this.state},matrix:this.matrix.slice(),dash:this.dash.slice()});record('save',[]);}
    restore(){assert.ok(this.stack.length,'unbalanced restore');const s=this.stack.pop();this.state=s.state;this.matrix=s.matrix;this.dash=s.dash;record('restore',[]);}
    setTransform(...a){this.matrix=a;record('setTransform',a);}
    getTransform(){const [a,b,c,d,e,f]=this.matrix;return {a,b,c,d,e,f};}
    translate(x,y){this.matrix=multiply(this.matrix,[1,0,0,1,x,y]);record('translate',[x,y]);}
    scale(x,y){this.matrix=multiply(this.matrix,[x,0,0,y,0,0]);record('scale',[x,y]);}
    rotate(t){this.matrix=multiply(this.matrix,[Math.cos(t),Math.sin(t),-Math.sin(t),Math.cos(t),0,0]);record('rotate',[t]);}
    setLineDash(a){this.dash=Array.from(a);record('setLineDash',a);}
    getLineDash(){return this.dash.slice();}
    measureText(s){return {width:Array.from(s).length*(parseFloat(this.font.match(/([\d.]+)px/)[1])*.6)};}
    createPattern(canvas,repeat){record('createPattern',[canvas,repeat]);const p={kind:'pattern'};if(patternTransform)p.setTransform=t=>record('patternTransform',[t]);return p;}
    createLinearGradient(...a){record('createLinearGradient',a);return {kind:'linear',addColorStop:(...a)=>record('addColorStop',a)};}
    createRadialGradient(...a){record('createRadialGradient',a);return {kind:'radial',addColorStop:(...a)=>record('addColorStop',a)};}
  }
  for(const key of properties)Object.defineProperty(Context.prototype,key,{get(){return this.state[key];},set(v){record(key,[v]);this.state[key]=v;}});
  for(const name of ['beginPath','closePath','moveTo','lineTo','quadraticCurveTo','bezierCurveTo','rect','arc','ellipse','clip','fill','stroke','fillRect','clearRect','fillText','strokeText','drawImage'])
    Context.prototype[name]=function(...args){record(name,args);};
  function factory(width,height){const canvas={width,height};canvas.getContext=()=>canvas.ctx||(canvas.ctx=new Context(canvas));return canvas;}
  return {factory,contexts,calls,setRecording(v){recording=v;calls.length=0;},balanced(){contexts.forEach(c=>assert.equal(c.stack.length,0));}};
}
test('draw','stylesheet keeps playback and export controls visible on load',()=>{
  const stylesheet=fixtures.primary.html.match(/<style>([\s\S]*?)<\/style>/)[1];
  assert.ok(stylesheet.includes('max-width:min(560px,calc((100vh - 150px) * 9 / 16));'));
});
test('draw','every integer fixture frame: finite drawing, root reset and balanced state',()=>{
  for(const fixture of Object.values(fixtures)){
    const {api:a,model:m}=fixture,h=canvasHarness(),canvas=h.factory(1080,1920),ctx=canvas.getContext('2d');
    const resources=a.prepareResources(m,h.factory,ctx),r=a.createRenderer(m,resources);
    for(let f=0;f<m.DURATION;f++){r.drawFrame(ctx,f);h.balanced();assert.equal(ctx.globalAlpha,1);assert.equal(ctx.globalCompositeOperation,'source-over');assert.deepEqual(ctx.matrix,[1,0,0,1,0,0]);}
    assert.ok(resources.layers.length>=2,'nested groups need distinct layers');
    for(const f of [0,60,112,156,188,202,234,260,296,m.OVER+8,m.ITEM_START.read+20,m.ITEM_START.write+20,m.TOTAL_AT+24,m.BRAND+36]){
      h.setRecording(true);r.drawFrame(ctx,f);const first=JSON.stringify(h.calls);h.setRecording(false);r.drawFrame(ctx,Math.max(0,f-1));
      h.setRecording(true);r.drawFrame(ctx,f);assert.equal(JSON.stringify(h.calls),first,`frame ${f} changed`);h.setRecording(false);
    }
  }
});
test('draw','text stroke order, offscreen clipping, rotated patterns and caption tails',()=>{
  const {api:a,model:m}=fixtures.primary,h=canvasHarness(),ctx=h.factory(1080,1920).getContext('2d'),resources=a.prepareResources(m,h.factory,ctx),r=a.createRenderer(m,resources);
  h.setRecording(true);r.drawChat(ctx,48);const header=h.calls.filter(c=>c[0]==='fillText').map(c=>c[1]);
  assert.ok(header.includes('Minute 1'));assert.ok(header.includes(' · step 1'));assert.ok(header.every(s=>!/ {2,}/.test(s)));
  // SVG rect corners are exact quarter ellipses, not quadratic approximations.
  h.setRecording(true);a.roundedRect(ctx,10,20,100,80,26);
  assert.deepEqual(h.calls.find(c=>c[0]==='ellipse'),['ellipse',84,46,26,26,0,-Math.PI/2,0]);
  h.setRecording(true);a.roundedRect(ctx,10,20,100,4,26);
  assert.deepEqual(h.calls.find(c=>c[0]==='ellipse'),['ellipse',84,22,26,2,0,-Math.PI/2,0]);
  h.setRecording(true);r.drawText(ctx,'AB',100,100,{size:30},'middle',2,{color:'#000',width:4});
  assert.deepEqual(h.calls.filter(c=>['strokeText','fillText'].includes(c[0])).map(c=>c[0]),['strokeText','strokeText','fillText','fillText']);
  h.setRecording(true);ctx.save();ctx.beginPath();ctx.rect(0,0,20,20);ctx.clip();r.withOpacity(ctx,.5,c=>{r.drawCard(c,0,0,20,20);r.withOpacity(c,.5,c=>r.drawCard(c,2,2,20,20));});ctx.restore();h.balanced();
  assert.ok(h.calls.filter(c=>c[0]==='clearRect').length>=2);assert.ok(h.calls.some(c=>c[0]==='globalAlpha'&&c[1]===.5));
  h.setRecording(true);r.drawFrame(ctx,190);assert.ok(h.calls.some(c=>c[0]==='rotate'&&Math.abs(c[1]-45*Math.PI/180)<1e-12));
  const t=canvasHarness(true),mt=load('primary',{DOMMatrix:class {rotate(n){this.angle=n;return this;}}}),tc=t.factory(1080,1920).getContext('2d'),tr=mt.api.createRenderer(mt.model,mt.api.prepareResources(mt.model,t.factory,tc));
  t.setRecording(true);tr.drawFrame(tc,190);assert.ok(t.calls.some(c=>c[0]==='patternTransform'));t.balanced();
  const interrupted={...m,CAPTIONS:[{start:2,end:4,slot:'teach',x:0,y:0,w:200,size:30,lines:['old']},{start:4,end:20,slot:'teach',x:0,y:0,w:200,size:30,lines:['new']}]};
  assert.equal(a.captionsAt(interrupted,6).length,2);assert.equal(a.captionsAt(interrupted,13).length,1);
});
const AVC=Uint8Array.from(Buffer.from('0142E028FFE100046742E02801000268CE','hex')),ASC=new Uint8Array([0x11,0x90]);
function opusHead(preSkip=0){const b=new Uint8Array(19);b.set(Buffer.from('OpusHead'));b[8]=1;b[9]=2;const d=new DataView(b.buffer);d.setUint16(10,preSkip,true);d.setUint32(12,48000,true);return b;}
function syntheticTracks(a,opus=false,preSkip=0,absent=false){
  const video=a.normalizeVideoSamples([5,7,4].map((size,i)=>({data:new Uint8Array(size),timestamp:Math.round(i*1000000/30),key:i!==1})),[{codec:'avc1.42E028',description:AVC}],3);
  const desc={codec:opus?'opus':'mp4a.40.2',sampleRate:48000,numberOfChannels:2};if(!absent)desc.description=opus?opusHead(preSkip):ASC;
  const audio=a.normalizeAudioSamples([3,2,6,1,8].map((size,i)=>{const data=new Uint8Array(size);if(opus)data[0]=0x98;return {data,timestamp:Math.round(i*(opus?960:1024)*1000000/48000)};}),[desc]);
  return {video,audio};
}
function boxes(bytes,start=0,end=bytes.length){const out=[];for(let at=start;at<end;){const size=bytes.readUInt32BE(at),type=bytes.toString('ascii',at+4,at+8);assert.ok(size>=8&&at+size<=end,`invalid ${type} size`);
    const b={type,start:at,size,end:at+size,children:[]};if(['moov','trak','mdia','minf','dinf','stbl'].includes(type))b.children=boxes(bytes,at+8,at+size);out.push(b);at+=size;}return out;}
function flatten(list){return list.flatMap(b=>[b,...flatten(b.children)]);}
function within(parent,type){return flatten(parent.children).find(b=>b.type===type);}
function table(bytes,b,fields){const n=bytes.readUInt32BE(b.start+12),out=[];for(let i=0;i<n;i++)out.push(Array.from({length:fields},(_,k)=>bytes.readUInt32BE(b.start+16+(i*fields+k)*4)));return out;}
function duration(bytes,b){const version=bytes[b.start+8];return version?Number(bytes.readBigUInt64BE(b.start+32)):bytes.readUInt32BE(b.start+24);}
test('runtime','independent AAC/Opus box parser: sizes, offsets, durations and descriptions',()=>{
  const a=fixtures.primary.api;
  for(const [opus,preSkip,absent] of [[false,0,false],[true,0,false],[true,312,false],[true,0,true]]){
    const {video,audio}=syntheticTracks(a,opus,preSkip,absent),bytes=Buffer.from(a.muxMp4(video,audio)),top=boxes(bytes),all=flatten(top);
    assert.deepEqual(top.map(b=>b.type),['ftyp','moov','mdat']);assert.deepEqual(top.map(b=>b.size),[32,opus?1074:1094,44]);assert.equal(bytes.length,opus?1150:1170);
    assert.equal(top[2].start+8,opus?1114:1134);assert.equal(bytes.toString('ascii',8,12),'isom');assert.equal(bytes.readUInt32BE(12),0x200);
    assert.equal(all.some(b=>['edts','elst','ctts','udta'].includes(b.type)),false);
    const tracks=top[1].children.filter(b=>b.type==='trak');assert.equal(tracks.length,2);
    assert.deepEqual(tracks.map(t=>t.size),[513,opus?445:465]);assert.equal(duration(bytes,within(top[1],'mvhd')),opus?9000:9600);
    tracks.forEach((t,i)=>{const stco=within(t,'stco'),stts=within(t,'stts'),stsz=within(t,'stsz'),stsc=within(t,'stsc'),mdhd=within(t,'mdhd'),tkhd=within(t,'tkhd'),hdlr=within(t,'hdlr');
      const expected=opus?[[1114,1119,1126],[1130,1133,1135,1141,1142]]:[[1134,1139,1146],[1150,1153,1155,1161,1162]];
      assert.deepEqual(table(bytes,stco,1).flat(),expected[i]);assert.deepEqual(table(bytes,stts,2),[[i?5:3,i?(opus?960:1024):3000]]);
      assert.deepEqual(table(bytes,stsc,3),[[1,1,1]]);assert.equal(bytes.readUInt32BE(stsz.start+12),0);
      const count=bytes.readUInt32BE(stsz.start+16);assert.deepEqual(Array.from({length:count},(_,j)=>bytes.readUInt32BE(stsz.start+20+4*j)),i?[3,2,6,1,8]:[5,7,4]);
      assert.equal(duration(bytes,mdhd),i?(opus?4800:5120):9000);assert.equal(bytes.readUInt32BE(tkhd.start+28),i?(opus?9000:9600):9000);
      assert.equal(bytes.readUInt32BE(mdhd.start+20),i?48000:90000);assert.equal(bytes.readUInt16BE(mdhd.start+28),0x55c4);
      assert.equal(bytes.toString('ascii',hdlr.start+16,hdlr.start+20),i?'soun':'vide');assert.equal(bytes.toString('ascii',hdlr.end-6,hdlr.end),i?'Audio\0':'Video\0');
      for(const b of [mdhd,tkhd])assert.equal(bytes.readBigUInt64BE(b.start+12),0n);
      const stsd=within(t,'stsd'),entry=stsd.start+16;assert.equal(bytes.toString('ascii',entry+4,entry+8),i?(opus?'Opus':'mp4a'):'avc1');
      const nested=boxes(bytes,entry+(i?36:86),entry+bytes.readUInt32BE(entry));
      if(!i){assert.deepEqual(bytes.subarray(nested[0].start+8,nested[0].end),Buffer.from(AVC));assert.deepEqual(table(bytes,within(t,'stss'),1).flat(),[1,3]);}
      else if(opus){const dops=nested[0];assert.equal(dops.type,'dOps');assert.equal(dops.size,19);assert.equal(bytes.readUInt16BE(dops.start+10),preSkip);assert.equal(bytes.readUInt32BE(dops.start+12),48000);}
      else {assert.equal(nested[0].type,'esds');assert.equal(nested[0].size,39);assert.ok(bytes.subarray(nested[0].start,nested[0].end).includes(Buffer.from([5,2,0x11,0x90])));}
    });
  }
});
test('runtime','co64, version-one duration boxes and packet validation',()=>{
  const a=fixtures.primary.api,{video}=syntheticTracks(a),offsets=[4294967296,4294967301,4294967308],bytes=Buffer.from(a.sampleTable(video,offsets,true));
  const co=flatten(boxes(bytes)).find(b=>b.type==='co64');assert.equal(bytes.readUInt32BE(co.start+12),3);
  assert.deepEqual(offsets.map((n,i)=>Number(bytes.readBigUInt64BE(co.start+16+i*8))),offsets);
  for(const b of [a.movieHeader(4294967296,3),a.mediaHeader(48000,4294967296)]){const raw=Buffer.from(b),root=boxes(raw)[0];assert.equal(raw[8],1);assert.equal(duration(raw,root),4294967296);assert.equal(raw.readBigUInt64BE(12),0n);assert.equal(raw.readBigUInt64BE(20),0n);}
  const tk=Buffer.from(a.trackHeader(video,4294967296));assert.equal(tk[8],1);assert.equal(Number(tk.readBigUInt64BE(36)),4294967296);
  for(const [toc,expected] of [[0x80,120],[0x88,240],[0x90,480],[0x98,960],[0x00,480],[0x18,2880],[0x99,1920]])assert.equal(a.opusPacketSamples(new Uint8Array([toc])),expected);
  assert.throws(()=>a.opusPacketSamples(new Uint8Array([0x9b,63])));assert.throws(()=>a.parseAacConfig(new Uint8Array([0x11,0x94])));
  const bad=AVC.slice();bad[0]=2;assert.throws(()=>a.parseAvcConfig(bad));assert.throws(()=>a.parseOpusHead(new Uint8Array([0])));
});
function eventTarget(){return {listeners:{},addEventListener(name,fn){(this.listeners[name]||(this.listeners[name]=new Set())).add(fn);},removeEventListener(name,fn){if(this.listeners[name])this.listeners[name].delete(fn);},dispatch(name,event={}){for(const fn of this.listeners[name]||[])fn(event);}};}
test('runtime','poster, explicit hashes, sound gestures, clock, scrubbing and keyboard',async()=>{
  for(const [hash,start,afterPlay] of [['',60,0],['#f=60',60,60],['#f=840',840,840],['#f=999999',974,0],['#f=oops',60,0],['#f=-1',0,0]]){
    const {api:a,model:m}=fixtures.primary,h=canvasHarness(),canvas=h.factory(1080,1920),drawn=[],pcm={left:new Float32Array(1600),right:new Float32Array(1600)},created=[];
    const audio={currentTime:0,destination:{},resume:async()=>{},createBuffer(){return {copyToChannel(){}};},createBufferSource(){const s={...eventTarget(),connect(){},disconnect(){},stop(){this.stopped=true;},start(at,offset){this.offset=offset;}};created.push(s);return s;}};
    let gestures=0;const dom={canvas,play:eventTarget(),scrub:eventTarget(),frame:{},hash,keyTarget:eventTarget(),requestAnimationFrame:fn=>1,cancelAnimationFrame(){},audioContextFactory(){gestures++;return audio;}};
    const p=a.createPlayer(m,{drawFrame(ctx,f){drawn.push(f);}},pcm,dom);assert.equal(p.frame,start);assert.equal(p.playing,false);assert.equal(gestures,0);assert.equal(created.length,0);assert.equal(dom.play.textContent,'Play');
    await p.play();assert.equal(gestures,1);assert.equal(p.frame,afterPlay);assert.equal(created[0].offset,afterPlay/30);
    audio.currentTime=.1;p.animationTick();assert.equal(p.frame,Math.min(974,afterPlay+3));p.pause();assert.equal(p.playing,false);
    p.seek(60);await p.play();assert.equal(p.frame,60);p.onKey({key:'ArrowRight',shiftKey:true,preventDefault(){}});assert.equal(p.frame,90);
    p.onScrub({target:{value:'840'}});assert.equal(p.frame,840);p.pause();assert.equal(dom.play.textContent,'Play');
    p.onKey({key:'Home',preventDefault(){}});assert.equal(p.frame,0);p.onKey({key:'End',preventDefault(){}});assert.equal(p.frame,974);
    p.onEnded();assert.equal(p.playing,false);assert.equal(p.frame,974);assert.ok(created.every(s=>s.stopped));
    await p.play();assert.equal(p.frame,0);assert.equal(created.at(-1).offset,0);assert.equal(p.playing,true);assert.equal(dom.play.textContent,'Pause');p.pause();
  }
});
function fakeCodecs(options={}){
  const closed={video:0,audio:0,frames:0,data:0},configured=[],submitted={video:[],audio:[]};
  class Encoder{
    constructor(callbacks,kind){Object.assign(this,eventTarget());this.callbacks=callbacks;this.kind=kind;this.state='unconfigured';this.encodeQueueSize=0;this.pending=new Set();this.sharedDescription=AVC.slice();}
    configure(config){this.config=config;this.state='configured';configured.push(config);if(options.constructAacFails&&config.codec==='mp4a.40.2')throw Error('AAC construction unsupported');}
    encode(input,settings){this.encodeQueueSize++;submitted[this.kind].push(input.init);const index=submitted[this.kind].length-1,config={codec:this.config.codec};
      if(this.kind==='video'){config.description=options.reusing?this.sharedDescription:AVC;}else {Object.assign(config,{sampleRate:48000,numberOfChannels:2});if(!options.absentOpus)config.description=this.config.codec==='opus'?opusHead(options.preSkip||0):ASC;}
      if(options.changing&&index===1)config.description=new Uint8Array([0]);
      let resolve;const wait=new Promise(r=>resolve=r);this.pending.add(wait);
      setTimeout(()=>{if(this.state!=='closed'){
        if(options.reusing&&this.kind==='video'&&index===1)this.sharedDescription[2]=0;
        if(options.errorAt===index)this.callbacks.error(Error('fake encoder failure'));
        else {const data=this.kind==='video'?new Uint8Array(5):new Uint8Array([0x98,0,0]);const timestamp=options.reorder&&this.kind==='video'&&index===1?0:input.init.timestamp;
          this.callbacks.output({byteLength:options.large?512*1024*1024+1:data.length,timestamp,duration:this.kind==='audio'?input.init.numberOfFrames*1000000/48000:input.init.duration,type:settings&&settings.keyFrame?'key':'delta',copyTo(target){target.set(data);}}, options.absentMetadata&&this.kind==='audio'?{}:{decoderConfig:config});}}
        this.encodeQueueSize--;this.dispatch('dequeue');this.pending.delete(wait);resolve();},options.delay||0);
    }
    flush(){return Promise.all([...this.pending]);}
    close(){this.state='closed';closed[this.kind]++;this.dispatch('dequeue');}
  }
  class VideoEncoder extends Encoder {constructor(cb){super(cb,'video');}static async isConfigSupported(c){return {supported:!options.unsupported,config:c};}}
  class AudioEncoder extends Encoder {constructor(cb){super(cb,'audio');}static async isConfigSupported(c){return {supported:!options.unsupported&&!(options.noAac&&c.codec==='mp4a.40.2'),config:c};}}
  class VideoFrame {constructor(canvas,init){this.init=init;}close(){closed.frames++;}}
  class AudioData {constructor(init){this.init=init;}close(){closed.data++;}}
  const h=canvasHarness();class OffscreenCanvas {constructor(w,hg){return h.factory(w,hg);}}
  return {globals:{VideoEncoder,AudioEncoder,VideoFrame,AudioData,OffscreenCanvas},closed,configured,submitted};
}
test('runtime','capability probes, AAC preference, Opus defaults/copy and offline cleanup',async()=>{
  for(const options of [{},{noAac:true},{constructAacFails:true},{noAac:true,absentOpus:true},{noAac:true,absentMetadata:true},{noAac:true,preSkip:312}]){
    const fake=fakeCodecs(options),{api:a}=load('primary',fake.globals),support=await a.probeExportSupport();
    assert.equal(support.video.latencyMode,'quality');assert.equal(support.audio.codec,options.noAac||options.constructAacFails?'opus':'mp4a.40.2');
    const frames=[],m={DURATION:3},pcm={left:new Float32Array(4800),right:new Float32Array(4800)},blob=await a.exportMp4(m,{drawFrame(ctx,f){frames.push(f);}},pcm,()=>{},new AbortController().signal);
    assert.deepEqual(frames,[0,1,2]);assert.equal(blob.type,'video/mp4');assert.equal(fake.closed.frames,3);assert.equal(fake.closed.data,5);
    assert.deepEqual(fake.submitted.video.map(f=>f.timestamp),[0,33333,66667]);assert.deepEqual(fake.submitted.video.map(f=>f.duration),[33333,33334,33333]);
    const bytes=Buffer.from(await blob.arrayBuffer()),all=flatten(boxes(bytes));assert.equal(all.some(b=>b.type==='edts'),false);
    if(support.audio.codec==='opus'){const track=all.filter(b=>b.type==='trak')[1],stsd=within(track,'stsd'),entry=stsd.start+16,dops=entry+36;assert.equal(bytes.readUInt16BE(dops+10),options.preSkip||0);}
  }
  const unavailable=load('primary',fakeCodecs({unsupported:true}).globals).api;assert.equal((await unavailable.probeExportSupport()).message,'MP4 export is unavailable in this browser. Playback still works.');
  await assert.rejects(unavailable.exportMp4({DURATION:3},{drawFrame(){}},{left:new Float32Array(4800),right:new Float32Array(4800)}),/MP4 export is unavailable/);
});
test('runtime','backpressure, cancellation, failures, padding and payload bound',async()=>{
  const fake=fakeCodecs({delay:2}),{api:a}=load('primary',fake.globals),encoder=new fake.globals.VideoEncoder({output(){},error(){}});encoder.configure(a.videoConfigCandidates()[0]);encoder.encodeQueueSize=8;
  let released=false;const pending=a.waitForQueue(encoder,8,new AbortController().signal).then(()=>released=true);await new Promise(r=>setTimeout(r,1));assert.equal(released,false);
  encoder.encodeQueueSize=7;encoder.dispatch('dequeue');await pending;assert.equal(released,true);encoder.close();
  const aborted=new AbortController(),waiting=new fake.globals.VideoEncoder({output(){},error(){}});waiting.encodeQueueSize=8;const wait=a.waitForQueue(waiting,8,aborted.signal);aborted.abort();await assert.rejects(wait,e=>e.name==='AbortError');waiting.close();
  for(const options of [{changing:true},{reusing:true},{reorder:true},{large:true},{errorAt:0,delay:2}]){
    const f=fakeCodecs(options),api=load('primary',f.globals).api;await assert.rejects(api.encodeVideo({DURATION:10},{drawFrame(){}},api.videoConfigCandidates()[0],()=>{},new AbortController().signal));
    assert.equal(f.closed.video,1);assert.ok(f.closed.frames>0);
  }
  const cancelFake=fakeCodecs(),cancelApi=load('primary',cancelFake.globals).api,cancel=new AbortController();
  await assert.rejects(cancelApi.exportMp4({DURATION:60},{drawFrame(){}},{left:new Float32Array(4800),right:new Float32Array(4800)},p=>{if(p>.02)cancel.abort();},cancel.signal),e=>e.name==='AbortError');
  assert.ok(cancelFake.closed.video>=2);assert.ok(cancelFake.closed.frames>0);
  const audioCancelFake=fakeCodecs(),audioCancelApi=load('primary',audioCancelFake.globals).api,audioCancel=new AbortController();
  await assert.rejects(audioCancelApi.exportMp4({DURATION:3},{drawFrame(){}},{left:new Float32Array(4800),right:new Float32Array(4800)},p=>{if(p>.85)audioCancel.abort();},audioCancel.signal),e=>e.name==='AbortError');
  assert.equal(audioCancelFake.closed.frames,3);assert.equal(audioCancelFake.closed.data,1);assert.ok(audioCancelFake.closed.audio>=2);
  const padFake=fakeCodecs(),padApi=load('primary',padFake.globals).api;
  await padApi.encodeAudio({left:new Float32Array([1,2,3]),right:new Float32Array([4,5,6])},padApi.audioConfigCandidates()[0],()=>{},new AbortController().signal);
  const input=padFake.submitted.audio[0];assert.equal(input.numberOfFrames,1024);assert.deepEqual(Array.from(input.data.slice(0,4)),[1,2,3,0]);assert.deepEqual(Array.from(input.data.slice(1024,1028)),[4,5,6,0]);
  assert.equal(padFake.closed.data,1);assert.equal(padFake.closed.audio,1);
});
test('runtime','binary fonts, Blob-only temporary anchors and no audio on startup',async()=>{
  const anchors=[],revoked=[],timers=[],faces=[],added=[];const doc={getElementById(){return null;},fonts:{add(face){added.push(face);}},body:{appendChild(a){anchors.push(a);}},createElement(name){assert.equal(name,'a');return {click(){this.clicked=true;},remove(){this.removed=true;}};}};
  class FontFace {constructor(name,buffer,options){this.name=name;this.buffer=buffer;this.options=options;faces.push(this);}async load(){return this;}}
  const {api:a}=load('primary',{document:doc,FontFace,setTimeout(fn,delay){timers.push({fn,delay});},atob:s=>Buffer.from(s,'base64').toString('binary'),URL:{createObjectURL(){return 'blob:invented';},revokeObjectURL(url){revoked.push(url);}}});
  await a.loadFonts(a.FONT_DATA);assert.equal(faces.length,2);assert.deepEqual(plain(faces[0].options),{weight:'300 700',stretch:'75% 125%'});assert.ok(faces[0].buffer.byteLength>1000);assert.equal(added.length,2);
  a.downloadBlob(new Blob(['invented']),'token-film-test.mp4');a.saveFontLicenses(a.LICENSE_DATA);assert.equal(anchors.length,3);assert.equal(revoked.length,0);
  assert.ok(anchors.every(x=>x.href==='blob:invented'&&x.clicked&&x.removed));
  assert.equal(timers.length,3);assert.ok(timers.every(t=>t.delay===60000));timers.forEach(t=>t.fn());assert.deepEqual(revoked,['blob:invented','blob:invented','blob:invented']);
});
test('runtime','startup, font failure and canceled UI export create no audio or downloads',async()=>{
  for(const failFont of [false,true]){
    const h=canvasHarness(),fake=fakeCodecs({delay:2}),elements={film:h.factory(1080,1920),play:eventTarget(),save:eventTarget(),cancel:eventTarget(),scrub:eventTarget(),frame:{},status:{},progress:{},licenses:eventTarget()},anchors=[];
    let enabled=false,audioCreated=0;const doc={...eventTarget(),fonts:{add(){}},getElementById(name){return enabled?elements[name]:null;},body:{appendChild(a){anchors.push(a);}},createElement(name){
      if(name==='canvas')return h.factory(1080,1920);if(name==='a')return {click(){},remove(){}};throw Error('unexpected element '+name);}};
    class FontFace {async load(){if(failFont)throw Error('font load failure');return this;}}
    class AudioContext {constructor(){audioCreated++;throw Error('audio attempted without gesture');}}
    const {api:a}=load('primary',{...fake.globals,document:doc,FontFace,AudioContext,atob:s=>Buffer.from(s,'base64').toString('binary'),location:{hash:''},requestAnimationFrame:()=>1,cancelAnimationFrame(){},URL:{createObjectURL(){return 'blob:invented';},revokeObjectURL(){}}});
    enabled=true;await a.bootstrap();assert.equal(audioCreated,0);assert.equal(anchors.length,0);
    if(failFont){assert.equal(elements.play.disabled,true);assert.equal(elements.save.disabled,true);assert.match(elements.status.textContent,/fonts or resources/);continue;}
    assert.equal(elements.play.textContent,'Play');assert.equal(elements.scrub.value,'60');assert.equal(elements.save.disabled,false);
    const exporting=[...elements.save.listeners.click][0]();assert.equal(elements.play.disabled,true);
    await new Promise(resolve=>setTimeout(resolve,10));elements.cancel.dispatch('click');await exporting;
    assert.equal(elements.status.textContent,'Export canceled.');assert.equal(anchors.length,0);assert.equal(audioCreated,0);assert.equal(elements.play.disabled,false);assert.ok(fake.closed.frames>0);h.balanced();
  }
});
async function main(){let count=0,failed=0;for(const section of selected?[selected]:['model','draw','runtime'])for(const t of tests[section]){
  count++;try{await t.fn();}catch(error){failed++;console.log('[FAIL] '+t.name);console.log(error.stack||error);}}
  console.log(`[${failed?'FAIL':'PASS'}] Claude film page ${selected||'model/draw/runtime'}: ${count} checks, ${failed} failures`);return failed?1:0;}
main().then(code=>{fs.rmSync(temporary,{recursive:true,force:true});process.exitCode=code;},err=>{console.error(err);fs.rmSync(temporary,{recursive:true,force:true});process.exitCode=1;});
