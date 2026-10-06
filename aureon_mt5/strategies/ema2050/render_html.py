from __future__ import annotations

import json
import math

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__SYMBOL__ — EMA __FAST__/__SLOW__ replay</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#141922; --panel:#1b2230; --line:#263042; --text:#d6dde8; --muted:#7e8a9c;
  --up:#2fbf9a; --down:#e0635a; --e20:#ff3b3b; --e50:#ffffff; --focus:#8fb3ff;
  --asia:#f2c14e; --london:#5aa9ff; --ny:#ff5a5a;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--text);
  font:14px/1.45 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
body{display:grid;grid-template-rows:auto 1fr auto;gap:12px;padding:14px 16px;height:100vh}
header{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
header h1{font-size:18px;font-weight:600;margin:0}
header .legend{display:flex;gap:14px;color:var(--muted)}
header .legend b{font-weight:600}
header .legend .e20{color:var(--e20)} header .legend .e50{color:var(--e50)}
header .clock{margin-left:auto;text-align:right;line-height:1.3}
header .clock small{color:var(--muted)}
main{display:grid;grid-template-columns:1fr 300px;gap:12px;min-height:0}
.charts{display:grid;grid-template-rows:1fr 1.5fr 1fr;gap:10px;min-height:0}
.charts.single{grid-template-rows:1fr}
.panel{position:relative;background:var(--panel);border-radius:14px;overflow:hidden;min-height:0}
.panel.exec{outline:1.5px solid rgba(255,59,59,.55);outline-offset:-1.5px}
.panel canvas{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:crosshair}
.panel .tag{position:absolute;left:12px;top:9px;font-weight:600;font-size:13px;pointer-events:none;z-index:2}
.panel .tag span{font-weight:400;color:var(--muted);margin-left:8px}
.panel .tag .dir{margin-left:10px;font-weight:500}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px;opacity:.75}
.tot{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:12.5px}
.tot div{background:#232c3b;border-radius:8px;padding:7px 9px}
.tot b{display:block;font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.tot span{color:var(--muted)}
.jr{display:grid;grid-template-columns:44px 1fr auto;gap:8px;padding:5px 8px;border-radius:7px;background:transparent;border:0;color:var(--text);text-align:left;cursor:pointer;font:inherit;font-size:12.5px;font-variant-numeric:tabular-nums}
.jr:hover,.jr.on{background:#232c3b} .jr .m{color:var(--muted);font-size:11.5px}
.tip{position:absolute;z-index:3;pointer-events:none;background:#0f141c;border:1px solid var(--line);
  border-radius:8px;padding:7px 9px;font-size:12px;line-height:1.35;white-space:nowrap;display:none}
.tip .t{color:var(--muted)} .tip b{font-weight:600}
aside{background:var(--panel);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:12px;min-height:0;overflow:auto}
aside h2{font-size:13px;font-weight:600;margin:0;color:var(--muted)}
.sel{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;font-size:13px}
.sel dt{color:var(--muted)} .sel dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.sel .big{font-size:20px;font-weight:600}
.bull{color:var(--up)} .bear{color:var(--down)}
.hint{color:var(--muted);font-size:12.5px}
.crosses{display:flex;flex-direction:column;gap:4px;font-size:12.5px}
.cross{display:grid;grid-template-columns:14px 1fr auto auto;gap:8px;padding:5px 8px;border-radius:7px;
  background:transparent;border:0;color:var(--text);text-align:left;cursor:pointer;font:inherit;font-variant-numeric:tabular-nums}
.cross:hover,.cross.on{background:#232c3b}
.cross:focus-visible{outline:2px solid var(--focus)}
.cross .m{color:var(--muted)}
footer{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
footer button{background:#232c3b;color:var(--text);border:1px solid var(--line);border-radius:8px;
  padding:7px 13px;font:inherit;cursor:pointer}
footer button:hover{background:#2b3648} footer button:focus-visible{outline:2px solid var(--focus)}
footer button.play{min-width:76px}
footer input[type=range]{flex:1;min-width:160px;accent-color:var(--e20)}
footer .pos{font-variant-numeric:tabular-nums;min-width:170px}
footer select{background:#232c3b;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font:inherit}
footer label{color:var(--muted);display:flex;align-items:center;gap:6px}
@media (max-width:900px){main{grid-template-columns:1fr} aside{max-height:260px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head>
<body>
<header>
  <h1>__SYMBOL__ EMA __FAST__/__SLOW__ replay __RANGE__</h1>
  <div class="legend"><b class="e20">— EMA __FAST__</b><b class="e50">— EMA __SLOW__</b><span><i class="sw" style="background:var(--asia)"></i>Asia</span><span><i class="sw" style="background:var(--london)"></i>London</span><span><i class="sw" style="background:var(--ny)"></i>NY</span></div>
  <div class="clock"><div id="clkIst">—</div><small id="clkSrv">—</small></div>
</header>
<main>
  <div class="charts __SINGLE__">__PANELS__</div>
  <aside>
    <div id="status" class="sel" style="margin-bottom:4px"></div>
    <h2>Selected bar</h2>
    <div id="selBox"><p class="hint">Click any M5 candle to see the EMA state there and what move was on offer in the next __HORIZON__ bars. Hover for price and time.</p></div>
    <h2>M5 journeys <span style="font-weight:400">· PB/PS pre-cross, EB/ES entry, CB/ESC exit, WP whipsaw</span></h2>
    <div class="tot" id="totals"></div>
    <div class="crosses" id="crossList"></div>
  </aside>
</main>
<footer>
  <button id="btnFirst" title="Start">⏮</button>
  <button id="btnPrev" title="Previous M5 bar">◀</button>
  <button id="btnPlay" class="play">Play</button>
  <button id="btnNext" title="Next M5 bar">▶</button>
  <button id="btnLast" title="Live end">⏭</button>
  <input id="scrub" type="range" min="0" max="0" value="0">
  <span class="pos" id="pos"></span>
  <label>speed <select id="speed"><option value="900">1×</option><option value="450">2×</option><option value="180" selected>5×</option><option value="60">15×</option></select></label>
  <label>bars <select id="zoom"><option>80</option><option>140</option><option>220</option><option>320</option><option value="all">all</option></select></label>
</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const SRV_OFF = D.server_offset_h*3600, IST_OFF = 5.5*3600, HZ = D.horizon;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const pad2 = n => String(n).padStart(2,'0');
function fmt(ts, off){ const d=new Date((ts+off)*1000);
  return `${pad2(d.getUTCDate())} ${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getUTCMonth()]} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`; }
const istStr = ts => fmt(ts - SRV_OFF, IST_OFF), srvStr = ts => fmt(ts, 0);

const TFS = Object.keys(D.tf);
const SESS=[['asia',0,7],['london',7,12],['ny',12,21]];
function sessionOf(ts){ const h=(((ts-SRV_OFF)%86400)+86400)%86400/3600; for(const [n,a,b] of SESS) if(h>=a&&h<b) return n; return null; }
const J = D.journeys, EV = D.events, PRE = D.pre;
const panels = {};
document.querySelectorAll('.panel').forEach(p => {
  const tf = p.dataset.tf;
  panels[tf] = { el:p, cv:p.querySelector('canvas'), tip:p.querySelector('.tip'), dir:p.querySelector('.dir'),
    bars:D.tf[tf].bars, crosses:D.tf[tf].crosses, hover:null, layout:null };
});
const m5 = panels.M5.bars;
if(TFS.length===1){ document.querySelector('.panel.exec').style.outline='none'; }
let cursor = m5.length-1;         // index into M5 bars = replay time
let selIdx = null;                 // selected M5 bar
let playing = false, timer = null;
let zoom = D.range ? 'all' : 140;
if(D.range){ document.getElementById('zoom').value='all'; }

// bars visible for a tf given replay time t: all bars whose open time <= t
function visibleCount(tf, t){ const b=panels[tf].bars; let lo=0,hi=b.length;
  while(lo<hi){ const m=(lo+hi)>>1; if(b[m].t<=t) lo=m+1; else hi=m; } return lo; }

function draw(tf){
  const P = panels[tf], cv=P.cv, dpr=devicePixelRatio||1;
  const W=cv.clientWidth, H=cv.clientHeight; if(!W||!H) return;
  if(cv.width!==W*dpr||cv.height!==H*dpr){ cv.width=W*dpr; cv.height=H*dpr; }
  const g=cv.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  const t = m5[cursor].t;
  const nVis = visibleCount(tf, t);
  const zc = zoom==='all' ? 1e9 : zoom; let perTf = tf==='M15' ? Math.round(zc/3) : zc;
  if(zoom==='all' && tf==='M1' && D.range){ let k=0; while(k<nVis && P.bars[k].t<D.range.day_start) k++; perTf = Math.max(60, nVis-k+60); }  // M15 covers the M5 span; M1 zooms into the recent end
  const n = Math.min(perTf, nVis), start = nVis - n;
  const bars = P.bars.slice(start, nVis);
  if(!bars.length) return;
  const padL=10, padR=66, padT=36, padB=26;
  const cw=(W-padL-padR)/n;
  let lo=Infinity, hi=-Infinity;
  for(const b of bars){ lo=Math.min(lo,b.l, b.e50??b.l); hi=Math.max(hi,b.h, b.e50??b.h); }
  const r=hi-lo||1; lo-=r*.06; hi+=r*.06;
  const X=i=>padL+(i+.5)*cw, Y=v=>padT+(hi-v)/(hi-lo)*(H-padT-padB);
  P.layout={start,n,cw,padL,padT,padB,padR,lo,hi,X,Y,W,H};
  const up=css('--up'), dn=css('--down');
  // session bands
  g.globalAlpha=0.13; let k0=0, cur=sessionOf(bars[0].t);
  for(let i=1;i<=n;i++){ const sx=i<n?sessionOf(bars[i].t):null; if(i===n||sx!==cur){ if(cur){ g.fillStyle=css('--'+cur); g.fillRect(X(k0)-cw/2,padT,X(i-1)-X(k0)+cw,H-padT-padB); } k0=i; cur=sx; } }
  g.globalAlpha=1;
  // grid
  g.strokeStyle=css('--line'); g.lineWidth=1; g.font='11px IBM Plex Sans, system-ui'; g.fillStyle=css('--muted'); g.textAlign='left';
  for(let k=0;k<5;k++){ const v=lo+(hi-lo)*k/4, y=Y(v); g.beginPath(); g.moveTo(padL,y); g.lineTo(W-padR,y); g.stroke(); g.fillText(v.toFixed(2),W-padR+8,y+4); }
  g.textAlign='center';
  const step=Math.max(1,Math.floor(n/6));
  for(let i=0;i<n;i+=step){ g.fillText(istStr(bars[i].t).slice(7), X(i), H-8); }
  // selection
  if(tf==='M5' && selIdx!=null){ const i=selIdx-start; if(i>=0&&i<n){ const b=bars[i];
    g.fillStyle='rgba(143,179,255,.12)'; g.fillRect(X(i)-cw/2,padT,cw,H-padT-padB);
    g.strokeStyle=css('--focus'); g.lineWidth=1; g.beginPath(); g.moveTo(X(i),padT); g.lineTo(X(i),H-padB); g.stroke();
    const end=Math.min(selIdx+HZ, nVis-1); const e=end-start; if(e>i){ g.fillStyle='rgba(143,179,255,.05)'; g.fillRect(X(i),padT,X(e)-X(i),H-padT-padB);
      const o=outcome(selIdx,end); const dir=o.dir; const c=dir==='bull'?up:dn;
      g.strokeStyle=c; g.setLineDash([4,3]); g.beginPath(); g.moveTo(X(i),Y(b.c)); g.lineTo(X(e),Y(b.c)); g.stroke();
      const yT=Y(dir==='bull'?b.c+o.mfe:b.c-o.mfe); g.beginPath(); g.moveTo(X(i),yT); g.lineTo(X(e),yT); g.stroke(); g.setLineDash([]);
      g.fillStyle=c; g.textAlign='left'; g.fillText(`best ${o.mfe.toFixed(2)} in ${o.b2mfe} bars`, X(i)+6, yT+(dir==='bull'?-6:14)); } } }
  // candles
  const bw=Math.max(1,cw*.62);
  for(let i=0;i<n;i++){ const b=bars[i], c=b.c>=b.o?up:dn; g.strokeStyle=c; g.fillStyle=c;
    g.beginPath(); g.moveTo(X(i),Y(b.h)); g.lineTo(X(i),Y(b.l)); g.stroke();
    g.fillRect(X(i)-bw/2, Y(Math.max(b.o,b.c)), bw, Math.max(1,Math.abs(Y(b.o)-Y(b.c)))); }
  // emas
  const line=(key,color,w)=>{ g.strokeStyle=color; g.lineWidth=w; g.beginPath(); let on=false;
    for(let i=0;i<n;i++){ const v=bars[i][key]; if(v==null){on=false;continue;} on?g.lineTo(X(i),Y(v)):g.moveTo(X(i),Y(v)); on=true; } g.stroke(); };
  line('e50',css('--e50'),1.6); line('e20',css('--e20'),1.6);
  g.font='600 10px IBM Plex Sans, system-ui'; g.textAlign='left';
  for(const [key,v] of [['e50','--e50'],['e20','--e20']]){ const yv=bars[n-1][key]; if(yv==null) continue; g.fillStyle='#1b2230'; g.fillRect(W-padR+2,Y(yv)-8,44,16); g.fillStyle=css(v); g.fillText(key==='e20'?'EMA '+D.ema[0]:'EMA '+D.ema[1],W-padR+5,Y(yv)+4); }
  g.font='11px IBM Plex Sans, system-ui';
  // crosses / journeys
  const pill=(x,y,text,col,above,fg,side)=>{ g.font='600 10px IBM Plex Sans, system-ui'; const w=g.measureText(text).width+10; const yy=above?y-24:y+10;
    const x0= side==='left'? x-8-w : side==='right'? x+8 : x-w/2;
    g.fillStyle=col; g.beginPath(); g.roundRect(x0,yy,w,15,4); g.fill(); g.fillStyle=fg||'#141922'; g.textAlign='left'; g.fillText(text,x0+5,yy+11); g.textAlign='center'; g.font='11px IBM Plex Sans, system-ui'; };
  const tri=(x,y,bull,col)=>{ g.fillStyle=col; g.strokeStyle='#0f141c'; g.lineWidth=1.2; g.beginPath();
    if(bull){ g.moveTo(x,y-9); g.lineTo(x-6,y+1); g.lineTo(x+6,y+1);} else { g.moveTo(x,y+9); g.lineTo(x-6,y-1); g.lineTo(x+6,y-1);} g.closePath(); g.fill(); g.stroke(); };
  if(tf!=='M5'){ for(const cr of P.crosses){ const i=cr.i-start; if(i<0||i>=n) continue; tri(X(i),Y(bars[i].e20),cr.d==='bull',cr.d==='bull'?up:dn); } }
  else {
    for(const p of PRE){ const i=p.i-start; if(i<0||i>=n) continue; pill(X(i),Y(bars[i].e20),p.label,'#4a5568',p.label==='PB','#d6dde8'); }
    for(const e of EV){ const i=e.i-start; if(i<0||i>=n) continue; const y=Y(bars[i].e20);
      if(e.label==='WP'||e.label==='F'){ g.strokeStyle=css('--muted'); g.lineWidth=1.5; g.beginPath(); g.moveTo(X(i)-4,y-4); g.lineTo(X(i)+4,y+4); g.moveTo(X(i)+4,y-4); g.lineTo(X(i)-4,y+4); g.stroke(); pill(X(i),y,(e.label==='WP'?'WP · '+e.reason: e.reason.includes('waiting')?'ext '+e.reason.split(' ')[1]+' · wait pb':'F · '+e.reason),'#4a5568',e.d==='bear','#d6dde8'); }
      else tri(X(i),y,e.d==='bull',e.d==='bull'?up:dn); }
    J.forEach((j,idx)=>{ const num=idx+1; const ie=j.entry_index-start, ix=j.exit_index-start; if(ix<0||ie>=n) return; const col=j.direction==='long'?up:dn; const long=j.direction==='long';
      const done=j.exit_index<=cursor && j.exit_reason!=='open'; const ok=j.result>0||!done; const xcol=ok?col:'#8a5a5a';
      const a=Math.max(0,ie), b=Math.min(n-1,Math.min(ix, nVis-1-start));
      g.setLineDash([2,3]); g.lineWidth=1; if(ie>=0&&ie<n){ g.strokeStyle=col; g.globalAlpha=.55; g.beginPath(); g.moveTo(X(ie),padT); g.lineTo(X(ie),H-padB); g.stroke(); g.globalAlpha=1; }
      if(done&&ix>=0&&ix<n){ g.strokeStyle=xcol; g.globalAlpha=.55; g.beginPath(); g.moveTo(X(ix),padT); g.lineTo(X(ix),H-padB); g.stroke(); g.globalAlpha=1; }
      g.setLineDash([]);
      if(b>a){ g.strokeStyle=col; g.setLineDash([3,3]); g.beginPath(); g.moveTo(X(a),Y(j.entry_price)); g.lineTo(X(b),Y(j.entry_price)); g.stroke(); g.setLineDash([]);
        if(done){ g.strokeStyle=xcol; g.lineWidth=1.4; g.beginPath(); g.moveTo(X(a),Y(j.entry_price)); g.lineTo(X(b),Y(j.exit_price)); g.stroke();
          const ang=Math.atan2(Y(j.exit_price)-Y(j.entry_price), X(b)-X(a)); g.beginPath(); g.moveTo(X(b),Y(j.exit_price)); g.lineTo(X(b)-8*Math.cos(ang-.4),Y(j.exit_price)-8*Math.sin(ang-.4)); g.moveTo(X(b),Y(j.exit_price)); g.lineTo(X(b)-8*Math.cos(ang+.4),Y(j.exit_price)-8*Math.sin(ang+.4)); g.stroke(); g.lineWidth=1; } }
      if(ie>=0&&ie<n){ g.fillStyle=col; g.beginPath(); g.arc(X(ie),Y(j.entry_price),4,0,7); g.fill(); pill(X(ie),Y(j.entry_price),`#${num} ${long?'EB':'ES'}${j.late?'·late':''}${j.reentry?'·re':''}${j.pullback?'·pb':''}${j.pre?'·pre':''} ${j.grade} · TAKEN ${j.entry_price.toFixed(2)}`,col,long,null,'left'); }
      for(const ad of (j.adds||[])){ const ia=ad.index-start; if(ia>=0&&ia<n&&ad.index<=cursor){ g.fillStyle=col; g.beginPath(); g.moveTo(X(ia),Y(ad.price)-5); g.lineTo(X(ia)+5,Y(ad.price)); g.lineTo(X(ia),Y(ad.price)+5); g.lineTo(X(ia)-5,Y(ad.price)); g.closePath(); g.fill(); g.font='10px IBM Plex Sans, system-ui'; g.textAlign='center'; g.fillText(`+add ${ad.price.toFixed(2)}`, X(ia), long?Y(ad.price)+16:Y(ad.price)-9); g.font='11px IBM Plex Sans, system-ui'; } }
      if(j.lock_index!=null && j.lock_index<=cursor){ const il=j.lock_index-start; if(il>=0&&il<n){ const yb=Y(long?bars[il].h:bars[il].l); g.fillStyle='#f2c14e'; g.font='600 10px IBM Plex Sans, system-ui'; g.textAlign='center'; g.fillText('⚡+2', X(il), long?yb-5:yb+13); g.font='11px IBM Plex Sans, system-ui'; } }
      if(j.target_index!=null && j.target_index<=cursor){ const it=j.target_index-start; if(it>=0&&it<n){ const yb=Y(long?bars[it].h:bars[it].l); g.fillStyle=col; g.font='600 10px IBM Plex Sans, system-ui'; g.textAlign='center'; g.fillText('15', X(it), long?yb-5:yb+13); g.font='11px IBM Plex Sans, system-ui'; } }
      if(done&&ix>=0&&ix<n){ g.fillStyle=xcol; g.fillRect(X(ix)-4,Y(j.exit_price)-4,8,8); pill(X(ix),Y(j.exit_price),`#${num} CLOSED ${j.exit_price.toFixed(2)} · ${j.result>=0?'+':''}${j.result.toFixed(1)} · ${j.exit_reason.replace('_',' ')}`,xcol,!long,null, X(ix)>W*0.72?'left':'right'); }
    });
  }
  // day boundary
  if(D.range){ const t=P.bars; let k=start; while(k<nVis && t[k].t<D.range.day_start) k++; const i=k-start;
    if(i>0&&i<n){ g.strokeStyle=css('--muted'); g.setLineDash([4,3]); g.lineWidth=1; g.beginPath(); g.moveTo(X(i)-cw/2,padT); g.lineTo(X(i)-cw/2,H-padB); g.stroke(); g.setLineDash([]);
      g.fillStyle='rgba(20,25,34,.35)'; g.fillRect(padL,padT,X(i)-cw/2-padL,H-padT-padB);
      g.fillStyle=css('--muted'); g.textAlign='right'; g.fillText(D.range.prev_label+' · context', X(i)-cw/2-5, padT+12); g.fillStyle=css('--text'); g.textAlign='left'; g.fillText(D.range.day_label, X(i)-cw/2+5, padT+12); } }
  // last price line
  const last=bars[n-1]; g.setLineDash([3,3]); g.strokeStyle=css('--muted'); g.lineWidth=1;
  g.beginPath(); g.moveTo(padL,Y(last.c)); g.lineTo(W-padR,Y(last.c)); g.stroke(); g.setLineDash([]);
  g.fillStyle=last.c>=last.o?up:dn; g.fillRect(W-padR+2,Y(last.c)-9,62,18); g.fillStyle='#0f141c'; g.font='600 11px IBM Plex Sans, system-ui'; g.textAlign='left'; g.fillText(last.c.toFixed(2),W-padR+8,Y(last.c)+4);
  // hover crosshair
  if(P.hover!=null){ const i=P.hover-start; if(i>=0&&i<n){ const b=bars[i]; g.strokeStyle='rgba(214,221,232,.45)'; g.setLineDash([2,3]); g.lineWidth=1;
    g.beginPath(); g.moveTo(X(i),padT); g.lineTo(X(i),H-padB); g.stroke(); g.setLineDash([]); } }
  // state tag
  const dirEl=P.dir; if(last.e20!=null){ const bull=last.e20>last.e50; let txt=bull?'20 above 50 ↑':'20 below 50 ↓'; if(tf==='M15'&&D.m15_trend){ const tr=D.m15_trend[Math.min(nVis-1,D.m15_trend.length-1)]; txt+=' · trend '+tr.toUpperCase(); } if(tf==='M5'&&D.m5_trend){ txt+=' · trend '+D.m5_trend[Math.min(nVis-1,D.m5_trend.length-1)].toUpperCase(); } dirEl.textContent=txt; dirEl.className='dir '+(bull?'bull':'bear'); }
}

function outcome(i, end){ const b=m5[i], dir=b.e20>b.e50?'bull':'bear'; let mfe=0,mae=0,b2=0;
  for(let k=i+1;k<=end;k++){ const f=dir==='bull'?m5[k].h-b.c:b.c-m5[k].l, a=dir==='bull'?b.c-m5[k].l:m5[k].h-b.c;
    if(f>mfe){mfe=f;b2=k-i;} if(a>mae) mae=a; }
  return {dir,mfe,mae,b2mfe:b2}; }
function barsSinceCross(i){ const s=Math.sign(m5[i].e20-m5[i].e50); let j=i; while(j>0&&Math.sign(m5[j-1].e20-m5[j-1].e50)===s) j--; return i-j; }

function drawAll(){ for(const tf of TFS) draw(tf); const t=m5[cursor].t;
  document.getElementById('clkIst').textContent='IST '+istStr(t);
  document.getElementById('clkSrv').textContent='server '+srvStr(t)+` (UTC${D.server_offset_h>=0?'+':''}${D.server_offset_h})`;
  document.getElementById('pos').textContent=`${cursor+1} / ${m5.length} · ${istStr(t).slice(7)} IST`;
  document.getElementById('scrub').value=cursor; renderCrossList(); }

function renderSel(){ const box=document.getElementById('selBox'); if(selIdx==null){ box.innerHTML='<p class="hint">Click any M5 candle to see the EMA state there and what move was on offer in the next '+HZ+' bars. Hover for price and time.</p>'; return; }
  const b=m5[selIdx], end=Math.min(selIdx+HZ,cursor), o=outcome(selIdx,end), since=barsSinceCross(selIdx);
  const spread=(b.e20-b.e50); const cls=o.dir==='bull'?'bull':'bear';
  const heading = selIdx>=3 ? (Math.abs(spread)>Math.abs(m5[selIdx-3].e20-m5[selIdx-3].e50)?'expanding':'fading') : '—';
  const hidden = end<selIdx+HZ ? `<p class="hint">Only ${end-selIdx} of ${HZ} outcome bars have played yet; press ▶ to reveal the rest.</p>` : '';
  const jj=J.find(j=>j.entry_index===selIdx||j.cross_index===selIdx); const jrow= jj?`<dt>Journey</dt><dd class="${jj.direction==='long'?'bull':'bear'}">${jj.direction} · ${jj.exit_index<=cursor?jj.exit_reason.replace('_',' ')+' '+(jj.result>=0?'+':'')+jj.result.toFixed(2)+' in '+jj.bars+' bars':'open'}<br><span class="hint">best ${jj.mfe.toFixed(2)} · drawdown ${jj.mae.toFixed(2)}</span></dd>`:'';
  box.innerHTML=`<dl class="sel">${jrow}
    <dt>Time</dt><dd>${istStr(b.t)} IST<br><span class="hint">${srvStr(b.t)} server</span></dd>
    <dt>Close</dt><dd class="big">${b.c.toFixed(2)}</dd>
    <dt>EMA state</dt><dd class="${cls}">${o.dir==='bull'?'20 above 50 · long side':'20 below 50 · short side'}</dd>
    <dt>Bars since cross</dt><dd>${since}</dd>
    <dt>EMA gap</dt><dd>${spread.toFixed(2)} · ${heading}</dd>
    <dt>Best move with trend</dt><dd class="${cls}">${o.mfe.toFixed(2)} in ${o.b2mfe} bars</dd>
    <dt>Worst against</dt><dd>${o.mae.toFixed(2)}</dd>
    <dt>Reward / risk</dt><dd>${o.mae>0?(o.mfe/o.mae).toFixed(2):'—'}</dd>
  </dl>${hidden}`; }

function renderCrossList(){ const el=document.getElementById('crossList');
  const lastC=m5[cursor].c; const oj=J.find(j=>j.entry_index<=cursor && j.exit_index>cursor); let st=`<dt>Close</dt><dd class="big">${lastC.toFixed(2)}</dd>`;
  if(oj){ const u=(oj.direction==='long'?1:-1)*(lastC-oj.entry_price); st+=`<dt>Position</dt><dd class="${oj.direction==='long'?'bull':'bear'}">${oj.direction.toUpperCase()} ${oj.grade} from ${oj.entry_price.toFixed(2)}<br><span class="${u>=0?'bull':'bear'}">${u>=0?'+':''}${u.toFixed(2)} unrealised</span></dd>`; }
  else st+=`<dt>Position</dt><dd class="hint">flat</dd>`;
  document.getElementById('status').innerHTML=st;
  const list=J.filter(j=>j.entry_index<=cursor).slice(-14).reverse();
  const closed=J.filter(j=>j.exit_index<=cursor && j.exit_reason!=='open'); const net=closed.reduce((s,j)=>s+j.result,0); const wins=closed.filter(j=>j.result>0).length;
  const dd=closed.reduce((m,j)=>Math.min(m,j.mae),0);
  const hit=closed.filter(j=>j.target_index!=null).length, best=closed.reduce((m,j)=>Math.max(m,j.result),0);
  document.getElementById('totals').innerHTML=`<div><span>net</span><b class="${net>=0?'bull':'bear'}">${net>=0?'+':''}${net.toFixed(1)}</b></div><div><span>win</span><b>${closed.length?Math.round(100*wins/closed.length):0}%</b></div><div><span>worst dd</span><b class="bear">${dd.toFixed(1)}</b></div><div><span>hit 15</span><b>${hit}/${closed.length}</b></div><div><span>best run</span><b class="bull">+${best.toFixed(1)}</b></div><div><span>runners</span><b>${closed.filter(j=>j.exit_reason==='runner').length}</b></div>`;
  el.innerHTML=list.map(j=>{ const long=j.direction==='long'; const done=j.exit_index<=cursor; const cls=long?'bull':'bear';
    return `<button class="jr ${j.entry_index===selIdx?'on':''}" data-i="${j.entry_index}">
      <span class="${cls}">${long?'EB':'ES'}${j.late?'·L':''}${j.reentry?'·R':''}${j.pullback?'·P':''} ${j.grade}</span><span>${istStr(j.entry_time).slice(7)} @ ${j.entry_price.toFixed(2)} <span class="m">${j.session} · ${D.m15_trend?'M15':'trend'} ${j.m15_trend}${done?' · '+j.exit_reason.replace('_',' ')+' @ '+j.exit_price.toFixed(2):' · open'}</span></span>
      <span class="${done?(j.result>0?'bull':'bear'):'m'}">${done?(j.result>=0?'+':'')+j.result.toFixed(1):'…'}</span></button>`; }).join('') || '<p class="hint">No clean entries yet.</p>';
  el.querySelectorAll('.jr').forEach(btn=>btn.onclick=()=>{ selIdx=+btn.dataset.i; const L=panels.M5.layout;
    if(selIdx<L.start||selIdx>cursor) cursor=Math.min(m5.length-1, selIdx+Math.min(HZ, zoom==='all'?HZ:Math.floor(zoom*0.4))); renderSel(); drawAll(); }); }

// interaction
for(const tf of TFS){ const P=panels[tf];
  P.cv.addEventListener('mousemove',e=>{ const L=P.layout; if(!L) return; const r=P.cv.getBoundingClientRect(); const x=e.clientX-r.left, y=e.clientY-r.top;
    const i=Math.floor((x-L.padL)/L.cw); if(i<0||i>=L.n){ P.hover=null; P.tip.style.display='none'; draw(tf); return; }
    P.hover=L.start+i; const b=P.bars[P.hover]; const price=L.hi-(y-L.padT)/(L.H-L.padT-L.padB)*(L.hi-L.lo);
    P.tip.style.display='block'; P.tip.innerHTML=`<b>${istStr(b.t)} IST</b> <span class="t">· ${srvStr(b.t)} server</span><br>
      O ${b.o.toFixed(2)} H ${b.h.toFixed(2)} L ${b.l.toFixed(2)} C <b>${b.c.toFixed(2)}</b><br>
      <span style="color:var(--e20)">EMA20 ${b.e20?.toFixed(2)??'—'}</span> · <span style="color:var(--e50)">EMA50 ${b.e50?.toFixed(2)??'—'}</span><br><span class="t">cursor ${price.toFixed(2)}</span>`;
    const tw=P.tip.offsetWidth; P.tip.style.left=(x+14+tw>L.W? x-tw-14 : x+14)+'px'; P.tip.style.top=Math.max(6,Math.min(y+12,L.H-80))+'px'; draw(tf); });
  P.cv.addEventListener('mouseleave',()=>{ P.hover=null; P.tip.style.display='none'; draw(tf); });
  if(tf==='M5') P.cv.addEventListener('click',()=>{ if(P.hover!=null){ selIdx=P.hover; renderSel(); drawAll(); } }); }

const setCursor=i=>{ cursor=Math.max(50,Math.min(m5.length-1,i)); drawAll(); if(selIdx!=null) renderSel(); };
document.getElementById('btnPrev').onclick=()=>setCursor(cursor-1);
document.getElementById('btnNext').onclick=()=>setCursor(cursor+1);
document.getElementById('btnFirst').onclick=()=>setCursor(50);
document.getElementById('btnLast').onclick=()=>setCursor(m5.length-1);
const scrub=document.getElementById('scrub'); scrub.max=m5.length-1; scrub.min=50; scrub.oninput=()=>setCursor(+scrub.value);
const playBtn=document.getElementById('btnPlay');
function stop(){ playing=false; clearInterval(timer); playBtn.textContent='Play'; }
function tick(){ if(cursor>=m5.length-1){ stop(); return; } setCursor(cursor+1); }
playBtn.onclick=()=>{ if(playing){ stop(); return; } if(cursor>=m5.length-1) cursor=50; playing=true; playBtn.textContent='Pause'; timer=setInterval(tick,+document.getElementById('speed').value); };
document.getElementById('speed').onchange=()=>{ if(playing){ clearInterval(timer); timer=setInterval(tick,+document.getElementById('speed').value); } };
document.getElementById('zoom').onchange=e=>{ zoom=e.target.value==='all'?'all':+e.target.value; drawAll(); };
document.addEventListener('keydown',e=>{ if(e.target.tagName==='SELECT'||e.target.tagName==='INPUT') return;
  if(e.code==='Space'){ e.preventDefault(); playBtn.click(); } if(e.key==='ArrowLeft') setCursor(cursor-1); if(e.key==='ArrowRight') setCursor(cursor+1); });
new ResizeObserver(drawAll).observe(document.querySelector('.charts'));
drawAll();
</script></body></html>
"""


def _bars_json(df):
    out = []
    for r in df.itertuples(index=False):
        out.append({"t": int(r.time), "o": float(r.open), "h": float(r.high), "l": float(r.low), "c": float(r.close),
                    "e20": None if math.isnan(r.ema20) else round(float(r.ema20), 3),
                    "e50": None if math.isnan(r.ema50) else round(float(r.ema50), 3)})
    return out


def render_html(symbol: str, analysed: dict, path: str, server_offset_h: float = 3.0, horizon: int = 24,
                rng: dict | None = None) -> str:
    data = {"symbol": symbol, "server_offset_h": server_offset_h, "horizon": horizon,
            "tf_minutes": {"M1": 1, "M5": 5, "M15": 15}, "tf": {},
            "journeys": [j.to_dict() for j in analysed["M5"]["journeys"]],
            "events": [{"i": e.index, "t": e.time, "d": e.direction, "label": e.label, "reason": e.reason} for e in analysed["M5"]["events"]],
            "pre": [{"i": p.index, "t": p.time, "label": p.label} for p in analysed["M5"]["pre"]],
            "m15_trend": analysed["M15"]["trend"] if "M15" in analysed else None,
            "m5_trend": analysed["M5"]["trend"],
            "ema": list(analysed["M5"].get("ema_periods", (20, 50))),
            "range": {"day_start": rng["day_start"], "prev_label": rng["prev"].strftime("%a %d %b"),
                      "day_label": rng["day"].strftime("%a %d %b")} if rng else None}
    for tf, d in analysed.items():
        data["tf"][tf] = {"bars": _bars_json(d["df"]),
                          "crosses": [{"i": c.index, "t": c.time, "d": c.direction, "mfe": c.mfe, "mae": c.mae,
                                       "heading": c.heading} for c in d["crosses"]]}
    rtxt = f"· {rng['prev']:%a %d %b} → {rng['day']:%a %d %b}" if rng else ""
    panel = lambda tf, cls, sub: (f'<div class="panel {cls}" data-tf="{tf}"><div class="tag">{tf}<span>{sub}</span>'
                                  f'<span class="dir"></span></div><canvas></canvas><div class="tip"></div></div>')
    if len(analysed) == 1:
        panels_html, single = panel("M5", "exec", "every cross · trend from EMA50 slope"), "single"
    else:
        panels_html = panel("M15", "", "context") + panel("M5", "exec", "execution") + panel("M1", "", "context"); single = ""
    html = (TEMPLATE.replace("__SYMBOL__", symbol).replace("__RANGE__", rtxt)
            .replace("__PANELS__", panels_html).replace("__SINGLE__", single)
            .replace("__FAST__", str(analysed["M5"].get("ema_periods", (20, 50))[0])).replace("__SLOW__", str(analysed["M5"].get("ema_periods", (20, 50))[1])).replace("__HORIZON__", str(horizon))
            .replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/")))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
