"""Reine Graph-HTML-Erzeugung (nur stdlib) — vom Server getrennt, damit ohne
LightRAG/MCP-Deps testbar (siehe test_graph.py). Optik/Feature-Set an den
ai-rem-Graphen angelehnt: heller BG, grüner Akzent, klickbare Typ-Legende zum
Filtern, Physik-Toggle, Typ-Chip im Info-Panel."""

import hashlib
import json

# vis-network per CDN (der Browser braucht Internet). Bewusst kein Inline-Bundle:
# ponytail: CDN reicht im LAN; ~1 MB inline lohnt nur bei echtem Offline-Zwang.
_VIS_CDN = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"

# stabile Farben pro entity_type (Fallback: aus Namen abgeleitet)
_TYPE_COLORS = {
    "person": "#e6550d", "organization": "#3182bd", "location": "#31a354",
    "geo": "#31a354", "event": "#756bb1", "category": "#636363",
    "date": "#e7ba52", "concept": "#843c39",
}


def color_for(t: str) -> str:
    t = (t or "").strip().lower()
    if t in _TYPE_COLORS:
        return _TYPE_COLORS[t]
    # deterministische Fallback-Farbe im mittleren Helligkeitsbereich
    return "#%06x" % (int(hashlib.md5(t.encode()).hexdigest(), 16) & 0xAAAAAA | 0x333333)


def _project_select(projects: list[str] | None, current: str) -> str:
    """Dropdown zum Umschalten zwischen Projekt-Graphen (navigiert zur graph.html
    des gewählten Projekts). Leer, wenn nur ein/kein Projekt vorliegt."""
    if not projects or len(projects) < 2:
        return ""
    opts = "".join(
        f'<option value="{p}"{" selected" if p == current else ""}>{p}</option>'
        for p in projects
    )
    return ('<label class="muted">Projekt '
            "<select id=\"proj\" onchange=\"location.href='../'+this.value+'/graph.html'\">"
            f"{opts}</select></label>")


def graph_html(nodes: list[dict], edges: list[dict], title: str,
               projects: list[str] | None = None, current: str = "") -> str:
    """Baut aus Knoten/Kanten-Dicts ein eigenständiges vis-network-HTML."""
    # json.dumps escaped '<' nicht; </script> in Daten würde das Script sprengen.
    payload = json.dumps({"nodes": nodes, "edges": edges}).replace("<", "\\u003c")
    proj_select = _project_select(projects, current)
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="{_VIS_CDN}"></script>
<style>
  :root{{--bg:#fafafa;--card:#fff;--border:#ececec;--accent:#388e3c;--ah:#2e7d32;--text:#333;--muted:#666}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:"Source Sans 3","Source Sans Pro",Arial,sans-serif;letter-spacing:.15pt;font-size:14px;padding:20px}}
  h1{{font-size:22px;font-weight:700;margin-bottom:4px}}
  .sub{{color:var(--muted);font-size:13px;margin-bottom:12px}}
  .bar{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:10px}}
  .muted{{color:var(--muted);font-size:12px}}
  #netwrap{{position:relative}}
  #net{{height:78vh;background:var(--card);border:1px solid var(--border);border-radius:10px}}
  #info{{position:absolute;left:12px;right:12px;bottom:12px;max-height:38%;overflow:auto;
    background:var(--card);border:1px solid var(--border);border-radius:8px;
    padding:10px 13px;box-shadow:0 3px 16px rgba(0,0,0,.10);font-size:13px;line-height:1.5;
    display:none;pointer-events:none}}
  #info .hd{{display:flex;align-items:center;gap:8px;margin-bottom:5px;flex-wrap:wrap}}
  #info .chip{{color:#fff;font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px}}
  #info .nm{{font-weight:700;font-size:14px}}
  #info .d{{color:var(--text);white-space:pre-wrap;word-break:break-word}}
  #leg{{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}}
  #leg span{{font-size:12px;display:inline-flex;align-items:center;gap:5px;cursor:pointer}}
  .dot{{width:11px;height:11px;border-radius:50%;display:inline-block}}
</style></head><body>
<h1>{title}</h1>
<p class="sub"><span id="cnt">lädt…</span> &nbsp;·&nbsp; ziehen/scrollen zum Navigieren, Knoten/Kante anklicken für Details, Legende anklicken zum Filtern</p>
<div class="bar">
  {proj_select}
  <label class="muted"><input type="checkbox" id="phys" checked onchange="net&&net.setOptions({{physics:{{enabled:this.checked}}}})"> Physik</label>
  <span class="muted">Typ-Filter: Legende anklicken</span>
</div>
<div id="netwrap"><div id="net"></div><div id="info"></div></div>
<div id="leg"></div>
<script>
  const $=id=>document.getElementById(id);
  const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
  const data = {payload};
  const COL={{}};                       // Typ -> Farbe (aus den Node-Farben)
  data.nodes.forEach(n=>{{ if(n.group && !(n.group in COL)) COL[n.group]=n.color||'#636363'; }});
  const HIDE=new Set();                 // ausgeblendete Typen (Legende)
  let net=null;

  function showInfo(header, group, body){{
    const chip=group?`<span class="chip" style="background:${{COL[group]||'#636363'}}">${{esc(group)}}</span>`:'';
    const d=body?`<div class="d">${{esc(body)}}</div>`:'';
    $('info').innerHTML=`<div class="hd">${{chip}}<span class="nm">${{esc(header)}}</span></div>${{d}}`;
    $('info').style.display='block';
  }}

  function build(){{
    const ents=data.nodes.filter(n=>!HIDE.has(n.group));
    const ok=new Set(ents.map(n=>n.id));
    const nodes=ents.map(n=>({{id:n.id,label:n.label,color:n.color,
      shape:'dot',size:14,font:{{size:13,color:'#333'}}}}));
    const edges=data.edges.filter(e=>ok.has(e.from)&&ok.has(e.to)).map(e=>({{
      from:e.from,to:e.to,desc:e.desc,arrows:'to',
      smooth:{{type:'continuous'}},color:{{color:'#ccc'}}}}));
    $('cnt').textContent=`${{nodes.length}} Knoten · ${{edges.length}} Kanten`;
    const nodesDS=new vis.DataSet(nodes), edgesDS=new vis.DataSet(edges);
    net=new vis.Network($('net'),{{nodes:nodesDS,edges:edgesDS}},{{
      physics:{{enabled:$('phys').checked,stabilization:{{iterations:150}},barnesHut:{{gravitationalConstant:-8000,springLength:130}}}},
      interaction:{{hover:true}}}});
    net.on('click',p=>{{
      if(p.nodes.length){{const src=data.nodes.find(x=>x.id===p.nodes[0]);showInfo(src.label,src.group,src.desc);}}
      else if(p.edges.length){{const e=edgesDS.get(p.edges[0]);const u=nodesDS.get(e.from),v=nodesDS.get(e.to);
        showInfo((u?u.label:e.from)+' → '+(v?v.label:e.to),'',e.desc);}}
      else{{$('info').style.display='none';}}
    }});
  }}

  function toggleType(t){{HIDE.has(t)?HIDE.delete(t):HIDE.add(t);renderLeg();build();}}
  function renderLeg(){{
    $('leg').innerHTML=Object.entries(COL).map(([t,c])=>
      `<span onclick="toggleType('${{esc(t)}}')" style="opacity:${{HIDE.has(t)?0.35:1}}" title="${{HIDE.has(t)?'einblenden':'ausblenden'}}"><i class="dot" style="background:${{c}}"></i>${{esc(t)}}</span>`).join('');
  }}

  renderLeg(); build();
</script></body></html>"""
