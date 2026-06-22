"""
Build a single self-contained HTML file from webapp/data/.

Embeds every geography's profile (gzip + base64) directly in one .html that
anyone can double-click - no server, no Python, no API key, works offline.
Decompression happens in the browser via the native DecompressionStream API
(Chrome/Edge/Firefox/Safari, modern versions).

    py build_standalone.py            # -> Demographics_Explorer.html

Re-run after  py census_bulk.py  to refresh the embedded data.
"""

import os
import json
import gzip
import base64

DATA = os.path.join("webapp", "data")
OUT = "Demographics_Explorer.html"

STANDALONE_JS = r"""
const $=s=>document.querySelector(s);
let INDEX=[],META={},PROFILES={},ACTIVE=-1,CUR=[];

async function inflate(b64){
  const bin=atob(b64),bytes=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
  const ds=new DecompressionStream('gzip');
  const buf=await new Response(new Blob([bytes]).stream().pipeThrough(ds)).arrayBuffer();
  return new TextDecoder().decode(buf);
}

async function boot(){
  try{
    const blob=JSON.parse(await inflate(DATA_B64));
    META=blob.meta;INDEX=blob.index;PROFILES=blob.profiles;
    for(const r of INDEX)r._l=r.name.toLowerCase();
    const c=META.counts||{};
    $('#subtitle').innerHTML=
      `Census ACS 5-year ${META.year} &middot; ${(META.total||INDEX.length).toLocaleString()} geographies `
      +`(${(c.Place||0).toLocaleString()} cities, ${(c.County||0).toLocaleString()} counties, ${c.State||0} states)`;
    $('#q').focus();
  }catch(e){
    $('#subtitle').textContent='Could not load embedded data ('+e+'). Try a modern browser (Chrome/Edge/Firefox/Safari).';
  }
}

function search(term){
  term=term.trim().toLowerCase();
  if(!term){CUR=[];return render([]);}
  const starts=[],has=[];
  for(const r of INDEX){
    const i=r._l.indexOf(term);
    if(i===0)starts.push(r);else if(i>0)has.push(r);
    if(starts.length>=60)break;
  }
  const rankLvl={Nation:0,State:1,County:2,Place:3};
  const cmp=(a,b)=>(rankLvl[a.level]-rankLvl[b.level])||a.name.length-b.name.length;
  CUR=starts.sort(cmp).concat(has.sort(cmp)).slice(0,50);
  render(CUR);
}

function render(list){
  const box=$('#results');ACTIVE=-1;
  if(!list.length){box.style.display='none';box.innerHTML='';return;}
  box.innerHTML=list.map((r,i)=>
    `<div class="res" data-i="${i}">
       <span class="badge b-${r.level}">${r.level==='Place'?'City':r.level}</span>
       <span class="nm">${r.name}</span></div>`).join('');
  box.style.display='block';
  box.querySelectorAll('.res').forEach(el=>{el.onclick=()=>choose(list[+el.dataset.i]);});
}

function move(d){
  const els=$('#results').querySelectorAll('.res');if(!els.length)return;
  ACTIVE=(ACTIVE+d+els.length)%els.length;
  els.forEach((e,i)=>e.classList.toggle('active',i===ACTIVE));
  els[ACTIVE].scrollIntoView({block:'nearest'});
}

function choose(r){
  $('#q').value=r.name;$('#results').style.display='none';
  const prof=PROFILES[r.id];
  if(!prof){$('#panel').innerHTML='<div class="empty">No profile for this place.</div>';return;}
  draw(r,prof);
}

function barRows(items,color,maxOverride){
  const max=maxOverride||Math.max(...items.map(d=>d[1]),1);
  return items.map(([lab,pct])=>{
    const w=Math.max(1,pct/max*100);
    return `<div class="bar"><span class="lab" title="${lab}">${lab}</span>
      <span class="track"><span class="fill" style="width:${w}%;background:${color}"></span></span>
      <span class="pct">${pct.toFixed(1)}%</span></div>`;
  }).join('');
}

function draw(r,p){
  const g=Object.fromEntries(p.insurance.groups);
  const stat=(k,v,c)=>`<div class="stat" style="background:${c}"><div class="v">${v==null?'–':v.toFixed(1)+'%'}</div><div class="k">${k}</div></div>`;
  $('#panel').innerHTML=`
    <div class="head"><h2>${r.name}</h2>
      <span class="badge b-${r.level} lvl">${r.level==='Place'?'City / Town':r.level}</span></div>
    <div class="popline">Population: <b>${p.pop?p.pop.toLocaleString():'–'}</b>
       <span style="color:#94a3b8">&middot; ACS 5-year ${META.year}</span></div>
    <div class="grid">
      <div class="card"><h3>Diversity</h3>
        <p class="note">Share of population. Hispanic shown as one group; all others are non-Hispanic.</p>
        ${barRows(p.diversity,'var(--div)')}</div>
      <div class="card"><h3>Household income</h3>
        <p class="note">Share of households in each income bracket.</p>
        ${barRows(p.income,'var(--inc)')}</div>
      <div class="card" style="grid-column:1/-1"><h3>Health insurance coverage</h3>
        <p class="note">Uninsured is an exclusive share. Private &amp; public overlap (a person can have both), so type bars need not sum to 100%.</p>
        <div class="ins-groups">
          ${stat('Private',g['Private'],'var(--private)')}
          ${stat('Public',g['Public'],'var(--public)')}
          ${stat('Uninsured',g['Uninsured'],'var(--unins)')}</div>
        ${barRows(p.insurance.types,'#64748b',Math.max(...p.insurance.types.map(d=>d[1]),1))}</div>
    </div>
    <div class="foot">Source: U.S. Census Bureau, American Community Survey 5-year estimates (${META.year}).</div>`;
}

$('#q').addEventListener('input',e=>search(e.target.value));
$('#q').addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){e.preventDefault();move(1);}
  else if(e.key==='ArrowUp'){e.preventDefault();move(-1);}
  else if(e.key==='Enter'){if(CUR.length)choose(CUR[ACTIVE>=0?ACTIVE:0]);}
  else if(e.key==='Escape')$('#results').style.display='none';
});
document.addEventListener('click',e=>{if(!e.target.closest('.searchbox'))$('#results').style.display='none';});
boot();
"""


def main():
    idx_path = os.path.join(DATA, "index.json")
    if not os.path.exists(idx_path):
        print("No data in webapp/data/. Run  py census_bulk.py  first.")
        return
    index = json.load(open(idx_path, encoding="utf-8"))
    meta = json.load(open(os.path.join(DATA, "meta.json"), encoding="utf-8"))
    profiles = {}
    pdir = os.path.join(DATA, "profiles")
    for fn in os.listdir(pdir):
        profiles.update(json.load(open(os.path.join(pdir, fn), encoding="utf-8")))

    blob = {"meta": meta, "index": index, "profiles": profiles}
    raw = json.dumps(blob, separators=(",", ":")).encode()
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode()

    # reuse the exact markup + CSS from the served app; swap only the script
    page = open(os.path.join("webapp", "index.html"), encoding="utf-8").read()
    pre = page.split("<script>")[0]
    html = (pre
            + "<script>\nconst DATA_B64=\"" + b64 + "\";\n"
            + STANDALONE_JS + "\n</script>\n</body>\n</html>\n")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    mb = os.path.getsize(OUT) / 1e6
    print(f"Wrote {OUT}  ({mb:.1f} MB, {len(index):,} geographies, "
          f"ACS {meta['year']})")
    print("Double-click it to open in any modern browser - no server needed.")


if __name__ == "__main__":
    main()
