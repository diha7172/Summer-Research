/* U.S. Demographics Explorer - shared UI.
   Two data modes:
     - embedded  : window.DATA_B64 is set (single-file build) -> decompress in browser.
     - served    : no DATA_B64 -> fetch index/meta and per-state profile shards.
   Profiles are year-keyed: profiles[geoId] = { "2013": {...}, "2018": {...}, ... }. */

const $ = s => document.querySelector(s);
let INDEX = [], META = {}, EMB = null, YEAR = null;
let ACTIVE = -1, CUR = [], PICK_B = false;
let selA = null, profA = null, selB = null, profB = null;
const shardCache = {};

function fipsShard(r){ return (r.level==='Nation'||r.level==='State') ? 'us' : r.state; }

async function inflate(b64){
  const bin=atob(b64),bytes=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
  const ds=new DecompressionStream('gzip');
  const buf=await new Response(new Blob([bytes]).stream().pipeThrough(ds)).arrayBuffer();
  return new TextDecoder().decode(buf);
}

async function loadProfile(r){           // -> { year: profile } map for one geo
  if(EMB) return EMB.profiles[r.id];
  const sh=fipsShard(r);
  if(!shardCache[sh])
    shardCache[sh]=await fetch(`data/profiles/${sh}.json`).then(x=>x.json()).catch(()=>({}));
  return shardCache[sh][r.id];
}

async function boot(){
  try{
    if(typeof DATA_B64!=='undefined'){
      $('#subtitle').textContent='Loading ~35,000 places…';
      EMB=JSON.parse(await inflate(DATA_B64));
      META=EMB.meta; INDEX=EMB.index;
    }else{
      META=await fetch('data/meta.json').then(r=>r.json());
      INDEX=await fetch('data/index.json').then(r=>r.json());
    }
    for(const r of INDEX) r._l=r.name.toLowerCase();
    YEAR=String(META.year);
    const ysel=$('#year');
    ysel.innerHTML=(META.years||[META.year]).slice().sort((a,b)=>b-a)
      .map(y=>`<option ${String(y)===YEAR?'selected':''}>${y}</option>`).join('');
    ysel.onchange=()=>{YEAR=ysel.value;renderAll();};
    $('#controls').classList.remove('hidden');
    const c=META.counts||{};
    $('#subtitle').innerHTML=
      `Census ACS 5-year &middot; ${(META.total||INDEX.length).toLocaleString()} geographies `
      +`(${(c.Place||0).toLocaleString()} cities, ${(c.County||0).toLocaleString()} counties, ${c.State||0} states) `
      +`&middot; years ${(META.years||[META.year]).join(', ')}`;
    $('#q').focus();
  }catch(e){
    $('#subtitle').textContent='Could not load data ('+e+'). '
      +(typeof DATA_B64!=='undefined'?'Try a modern browser (Chrome/Edge/Firefox/Safari).':'Run "py census_bulk.py" then "py serve.py".');
  }
}

/* ---- search ---- */
function search(term){
  term=term.trim().toLowerCase();
  if(!term){CUR=[];return render([]);}
  const starts=[],has=[];
  for(const r of INDEX){
    const i=r._l.indexOf(term);
    if(i===0)starts.push(r);else if(i>0)has.push(r);
    if(starts.length>=60)break;
  }
  const rk={Nation:0,State:1,County:2,Place:3};
  const cmp=(a,b)=>(rk[a.level]-rk[b.level])||a.name.length-b.name.length;
  CUR=starts.sort(cmp).concat(has.sort(cmp)).slice(0,50);
  render(CUR);
}
function render(list){
  const box=$('#results');ACTIVE=-1;
  if(!list.length){box.style.display='none';box.innerHTML='';return;}
  box.innerHTML=list.map((r,i)=>
    `<div class="res" data-i="${i}"><span class="badge b-${r.level}">${r.level==='Place'?'City':r.level}</span><span class="nm">${r.name}</span></div>`).join('');
  box.style.display='block';
  box.querySelectorAll('.res').forEach(el=>{el.onclick=()=>choose(list[+el.dataset.i]);});
}
function move(d){
  const els=$('#results').querySelectorAll('.res');if(!els.length)return;
  ACTIVE=(ACTIVE+d+els.length)%els.length;
  els.forEach((e,i)=>e.classList.toggle('active',i===ACTIVE));
  els[ACTIVE].scrollIntoView({block:'nearest'});
}
async function choose(r){
  $('#q').value='';$('#results').style.display='none';CUR=[];
  if(PICK_B){ selB=r; profB=await loadProfile(r); PICK_B=false; }
  else { selA=r; profA=await loadProfile(r); selB=null; profB=null; }
  renderAll();
  $('#q').blur();
}

/* ---- rendering ---- */
function barRows(items,color,maxOverride){
  if(!items||!items.length)return '<p class="note">No data.</p>';
  const max=maxOverride||Math.max(...items.map(d=>d[1]),1);
  return items.map(([lab,pct])=>{
    const w=Math.max(1,pct/max*100);
    return `<div class="bar"><span class="lab" title="${lab}">${lab}</span>
      <span class="track"><span class="fill" style="width:${w}%;background:${color}"></span></span>
      <span class="pct">${pct.toFixed(1)}%</span></div>`;
  }).join('');
}
function trendHTML(ymap){
  const yrs=Object.keys(ymap).map(Number).sort((a,b)=>a-b);
  if(yrs.length<2)return '';
  const a=ymap[String(yrs[0])], b=ymap[String(yrs[yrs.length-1])];
  if(!a||!b||!a.pop||!b.pop)return '';
  const ch=(b.pop-a.pop)/a.pop*100;
  const cls=ch>0.5?'up':ch<-0.5?'down':'flat';
  const arrow=ch>0.5?'&#9650;':ch<-0.5?'&#9660;':'&#9644;';
  return `<span class="trend ${cls}">${arrow} ${ch>=0?'+':''}${ch.toFixed(1)}% (${yrs[0]}–${yrs[yrs.length-1]})</span>`;
}
function geoPanel(geo,ymap){
  if(!ymap) return `<div class="geopanel"><div class="head"><h2>${geo.name}</h2></div><p class="note">No data.</p></div>`;
  const p=ymap[YEAR];
  const head=`<div class="head"><h2>${geo.name}</h2>
     <span class="badge b-${geo.level}">${geo.level==='Place'?'City / Town':geo.level}</span></div>`;
  if(!p) return `<div class="geopanel">${head}<p class="note">No data for ${YEAR}.</p></div>`;
  const g=Object.fromEntries(p.insurance.groups);
  const stat=(k,v,c)=>`<div class="stat" style="background:${c}"><div class="v">${v==null?'–':v.toFixed(1)+'%'}</div><div class="k">${k}</div></div>`;
  const ageCard = (p.age&&p.age.length)
    ? `<div class="card"><h3>Age</h3>
         <p class="note">Share of population by age group.</p>
         ${barRows(p.age,'#6366f1')}</div>` : '';
  return `<div class="geopanel">
    ${head}
    <div class="popline">Population (${YEAR}): <b>${p.pop?p.pop.toLocaleString():'–'}</b>${trendHTML(ymap)}</div>
    ${kpis(p)}
    <div class="grid">
      ${ageCard}
      <div class="card"><h3>Diversity</h3>
        <p class="note">Share of population. Hispanic is one group; others are non-Hispanic.</p>
        ${barRows(p.diversity,'var(--div)')}</div>
      <div class="card"><h3>Household income</h3>
        <p class="note">Share of households in each bracket.</p>
        ${barRows(p.income,'var(--inc)')}</div>
    </div>
    <div class="card"><h3>Health insurance coverage</h3>
      <p class="note">Uninsured is exclusive; Private &amp; Public overlap, so type bars need not sum to 100%.</p>
      <div class="ins-groups">
        ${stat('Private',g['Private'],'var(--private)')}
        ${stat('Public',g['Public'],'var(--public)')}
        ${stat('Uninsured',g['Uninsured'],'var(--unins)')}</div>
      ${barRows(p.insurance.types,'#64748b',Math.max(...p.insurance.types.map(d=>d[1]),1))}</div>
  </div>`;
}
function fmtStat(v,unit){
  if(unit==='$') return '$'+Number(v).toLocaleString();
  if(unit==='yrs') return v+' yrs';
  if(unit==='%') return v+'%';
  return v;
}
function kpis(p){
  const tiles=[];
  if(p.sex&&p.sex.length>=2) tiles.push(['Sex (F / M)', p.sex[0][1]+'% / '+p.sex[1][1]+'%']);
  for(const s of (p.stats||[])) tiles.push([s[0], fmtStat(s[1],s[2])]);
  if(!tiles.length) return '';
  return '<div class="keystats">'+tiles.map(([k,v])=>
    `<div class="kpi"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('')+'</div>';
}
function toolbar(){
  let t=`<div class="dtoolbar"><span class="chip"><span class="badge b-${selA.level}">${selA.level==='Place'?'City':selA.level}</span> ${selA.name}</span>`;
  if(selB){
    t+=`<span style="color:#94a3b8">vs</span>
        <span class="chip"><span class="badge b-${selB.level}">${selB.level==='Place'?'City':selB.level}</span> ${selB.name}
        <button class="x" id="clearB" title="remove">&times;</button></span>`;
  }else{
    t+=`<button class="btn" id="addB">+ Compare another place</button>`;
  }
  return t+`</div>`;
}
function renderAll(){
  if(!selA){return;}
  const cols=selB?'cols two':'cols';
  $('#panel').innerHTML=
    toolbar()+
    `<div class="${cols}">${geoPanel(selA,profA)}${selB?geoPanel(selB,profB):''}</div>`+
    `<div class="foot">Source: U.S. Census Bureau, American Community Survey 5-year estimates.</div>`;
  const addB=$('#addB'); if(addB) addB.onclick=()=>{PICK_B=true;$('#q').focus();$('#q').placeholder='Search a second place to compare…';};
  const clearB=$('#clearB'); if(clearB) clearB.onclick=()=>{selB=null;profB=null;renderAll();};
}

/* ---- events ---- */
$('#q').addEventListener('input',e=>search(e.target.value));
$('#q').addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){e.preventDefault();move(1);}
  else if(e.key==='ArrowUp'){e.preventDefault();move(-1);}
  else if(e.key==='Enter'){if(CUR.length)choose(CUR[ACTIVE>=0?ACTIVE:0]);}
  else if(e.key==='Escape'){$('#results').style.display='none';}
});
document.addEventListener('click',e=>{if(!e.target.closest('.searchbox'))$('#results').style.display='none';});
boot();
