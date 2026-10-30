"""Render the Evidence Atlas as one self-contained HTML page (no external scripts, nothing fetched).

The page embeds the catalogue and a validated snapshot. A meter with no reading is drawn neutral with no
needle; a reading older than its meter's ``stale_after_s`` is amber, re-checked every minute. LOAD SNAPSHOT
parses a snapshot file in the page with the same rules as ``validate_snapshot`` (one bad reading rejects
the whole file); nothing is uploaded.
"""
from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from trading_live_claude.atlas.catalog import Meter

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#6b7280;--line:#e3e6eb;--ok:#1f7a4d;--stale:#b26b00;
--none:#9aa1ad;--arc:#e3e6eb;--needle:#1d2330;--accent:#2f5bea}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#11141a;--card:#1a1f27;--ink:#e6e9ef;
--muted:#9aa3b2;--line:#2a313c;--ok:#4cc38a;--stale:#f0a43a;--none:#5d6573;--arc:#2a313c;--needle:#e6e9ef;--accent:#7c9cff}}
:root[data-theme="dark"]{--bg:#11141a;--card:#1a1f27;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a313c;--ok:#4cc38a;
--stale:#f0a43a;--none:#5d6573;--arc:#2a313c;--needle:#e6e9ef;--accent:#7c9cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}header{padding:20px 16px 8px;max-width:1280px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px;letter-spacing:.04em}.sub{color:var(--muted);font-size:13px}
.bar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin-top:10px}
.legend span{display:inline-flex;align-items:center;gap:6px;margin-right:12px;font-size:12px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
label.btn{border:1px solid var(--line);background:var(--card);padding:6px 10px;border-radius:6px;cursor:pointer;
font-size:12px;font-weight:600;letter-spacing:.04em}label.btn input{display:none}
#notice{margin:10px 0 0;font-size:13px;padding:8px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card)}
#notice.err{border-color:#d33;color:#d33}main{max-width:1280px;margin:0 auto;padding:8px 16px 40px}
section{margin-top:22px}h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
.m{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-height:128px;
display:flex;flex-direction:column}.m .id{font:11px ui-monospace,Consolas,monospace;color:var(--muted)}
.m .name{font-weight:600;margin:2px 0 4px}.m .val{font:600 20px ui-monospace,Consolas,monospace}
.m .unit{font-size:12px;color:var(--muted);margin-left:4px}.m .state{font-size:11px;font-weight:700;letter-spacing:.05em;margin-top:auto}
.m.none .val{color:var(--none)}.m.none .state{color:var(--none)}.m.ok .state{color:var(--ok)}
.m.stale{border-color:var(--stale)}.m.stale .state{color:var(--stale)}.m svg{display:block;margin:2px 0}
.m .scope{font-size:11px;color:var(--muted);margin-top:2px}footer{max-width:1280px;margin:0 auto;padding:0 16px 30px;
color:var(--muted);font-size:12px}
"""

_JS = r"""
const CAT=JSON.parse(document.getElementById('catalog').textContent);
const CAT_ID=document.getElementById('catalog').dataset.id;
let SNAP=JSON.parse(document.getElementById('snapshot').textContent);
const byId=Object.fromEntries(CAT.map(m=>[m.id,m]));
function validate(s){const p=[];const now=Date.now();const seen=new Set();
 if(!s||typeof s!=='object')return['not a JSON object'];
 if(s.catalog_id!==CAT_ID)p.push('taken against catalogue '+s.catalog_id+', not '+CAT_ID);
 for(const r of (s.readings||[])){const m=byId[r.id];if(!m){p.push(r.id+': not a catalogued meter');continue}
  if(!String(r.source||'').trim()||!String(r.scope||'').trim())p.push(r.id+': no source or scope');
  const t=Date.parse(r.observed_utc);if(!isFinite(t)||t<Date.parse('2015-01-01')||t>now+300000)p.push(r.id+': implausible timestamp');
  if(typeof r.value!=='number'||!isFinite(r.value))p.push(r.id+': value is not a finite number');
  if(r.unit!==m.unit)p.push(r.id+': unit differs from the catalogue');if(seen.has(r.id))p.push(r.id+': two readings');seen.add(r.id)}
 return p}
function fmt(v,u){if(u==='flag')return v?'yes':'no';const a=Math.abs(v);
 const d=a>=1000?0:a>=100?1:a>=1?2:3;return v.toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:0})}
function dial(m,r){if(m.lo===null||m.hi===null)return'';const W=150,H=78,cx=75,cy=72,R=60;
 const arc=`<path d="M${cx-R},${cy} A${R},${R} 0 0 1 ${cx+R},${cy}" fill="none" stroke="var(--arc)" stroke-width="9" stroke-linecap="round"/>`;
 let needle='';if(r){const f=Math.max(0,Math.min(1,(r.value-m.lo)/(m.hi-m.lo)));const a=Math.PI*(1-f);
  needle=`<line x1="${cx}" y1="${cy}" x2="${(cx+(R-6)*Math.cos(a)).toFixed(1)}" y2="${(cy-(R-6)*Math.sin(a)).toFixed(1)}" stroke="var(--needle)" stroke-width="2.5" stroke-linecap="round"/><circle cx="${cx}" cy="${cy}" r="4" fill="var(--needle)"/>`}
 return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${m.name} dial ${m.lo} to ${m.hi}">${arc}${needle}<text x="${cx-R}" y="${H}" font-size="10" fill="var(--muted)" text-anchor="middle">${m.lo}</text><text x="${cx+R}" y="${H}" font-size="10" fill="var(--muted)" text-anchor="middle">${m.hi}</text></svg>`}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function draw(){const rs=Object.fromEntries((SNAP.readings||[]).map(r=>[r.id,r]));const now=Date.now();
 const secs={};for(const m of CAT)(secs[m.section]=secs[m.section]||[]).push(m);let n=0,st=0;
 document.getElementById('meters').innerHTML=Object.keys(secs).sort().map(sec=>`<section><h2>${esc(sec)} &middot; ${secs[sec].length}</h2><div class="grid">`+
  secs[sec].map(m=>{const r=rs[m.id];let cls='none',state='NO DATA &middot; NO NEEDLE',val='&mdash;',scope='';
   if(r){n++;const age=(now-Date.parse(r.observed_utc))/1000;const stale=age>m.stale_after_s;if(stale)st++;
    cls=stale?'stale':'ok';state=stale?'STALE &middot; '+Math.round(age/3600*10)/10+' h old':'OBSERVED';
    val=fmt(r.value,m.unit);scope=esc(r.scope)}
   return `<div class="m ${cls}" title="${esc(m.definition)} &#10;Source: ${esc(m.source)}"><div class="id">${m.id}</div><div class="name">${esc(m.name)}</div>${dial(m,r)}<div><span class="val">${val}</span>${m.unit&&m.unit!=='flag'?`<span class="unit">${esc(m.unit)}</span>`:''}</div><div class="scope">${scope}</div><div class="state">${state}</div></div>`}).join('')+'</div></section>').join('');
 document.getElementById('count').textContent=`${CAT.length} defined meters · ${n} readings · ${st} stale · snapshot ${SNAP.generated_utc||'none'}`}
document.getElementById('load').addEventListener('change',e=>{const f=e.target.files[0];if(!f)return;const rd=new FileReader();
 rd.onload=()=>{const note=document.getElementById('notice');try{const s=JSON.parse(rd.result);const p=validate(s);
  if(p.length){note.className='err';note.textContent='Snapshot rejected (whole file): '+p.slice(0,6).join('; ');return}
  SNAP=s;note.className='';note.textContent='Loaded '+f.name+' (parsed in this page; nothing was uploaded).';draw()}
  catch(err){note.className='err';note.textContent='Not a snapshot: '+err.message}};rd.readAsText(f)});
draw();setInterval(draw,60000);
"""


def _embed(obj: Any) -> str:
    return json.dumps(obj).replace("</", "<\\/")


def render(meters: Sequence[Meter], catalog_id: str, snapshot: Mapping[str, Any] | None) -> str:
    snap = dict(snapshot or {"generated_utc": None, "catalog_id": catalog_id, "readings": []})
    errors = snap.get("extractor_errors") or []
    notice = ("Readings come only from a snapshot of the journals. A meter without one is drawn neutral, "
              "with no needle, and nothing is substituted for it.")
    if errors:
        notice += " Extractors that failed this time: " + "; ".join(html.escape(e) for e in errors)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Evidence Atlas</title>
<style>{_CSS}</style></head><body>
<header><h1>EVIDENCE ATLAS</h1><div class="sub" id="count"></div>
<div class="bar"><div class="legend"><span><i class="dot" style="background:var(--ok)"></i>Observed</span>
<span><i class="dot" style="background:var(--stale)"></i>Stale</span>
<span><i class="dot" style="background:var(--none)"></i>No data, no needle</span></div>
<label class="btn">LOAD SNAPSHOT<input type="file" id="load" accept=".json,application/json"></label></div>
<div id="notice">{notice}</div></header><main id="meters"></main>
<footer>Catalogue {html.escape(catalog_id)} (config/meter_catalog.csv): what the code defines. Snapshot from
{html.escape(str(snap.get("state_dir", "")))}. Hover a meter for its definition and source.</footer>
<script type="application/json" id="catalog" data-id="{html.escape(catalog_id)}">{_embed([asdict(m) for m in meters])}</script>
<script type="application/json" id="snapshot">{_embed(snap)}</script>
<script>{_JS}</script></body></html>"""
