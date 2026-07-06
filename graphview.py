"""Reine Graph-HTML-Erzeugung (nur stdlib) — vom Server getrennt, damit ohne
LightRAG/MCP-Deps testbar (siehe test_graph.py)."""

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


def graph_html(nodes: list[dict], edges: list[dict], title: str) -> str:
    """Baut aus Knoten/Kanten-Dicts ein eigenständiges vis-network-HTML."""
    # json.dumps escaped '<' nicht; </script> in Daten würde das Script sprengen.
    payload = json.dumps({"nodes": nodes, "edges": edges}).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<title>{title}</title>
<script src="{_VIS_CDN}"></script>
<style>
  html,body{{margin:0;height:100%;font-family:system-ui,sans-serif}}
  #net{{width:100%;height:100vh}}
  #bar{{position:fixed;top:8px;left:8px;z-index:5;background:#fff;
       padding:6px 10px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.2);font-size:13px}}
  #info{{position:fixed;bottom:0;left:0;right:0;z-index:5;background:#fff;
        max-height:35vh;overflow:auto;padding:10px 14px;font-size:13px;
        box-shadow:0 -1px 6px rgba(0,0,0,.25);display:none}}
  #info b{{font-size:14px}}
  #info div{{white-space:pre-wrap;margin-top:6px}}
</style></head><body>
<div id="bar"><b>{title}</b> — ziehen zum Verschieben, scrollen zum Zoomen, Knoten/Kante anklicken für Details</div>
<div id="net"></div>
<div id="info"></div>
<script>
  const data = {payload};
  const nodesDS = new vis.DataSet(data.nodes);
  const edgesDS = new vis.DataSet(data.edges);
  const network = new vis.Network(document.getElementById("net"), {{nodes: nodesDS, edges: edgesDS}}, {{
    physics: {{ stabilization: true, barnesHut: {{ gravitationalConstant: -8000, springLength: 120 }} }},
    nodes: {{ shape: "dot", size: 14, font: {{ size: 14 }} }},
    edges: {{ smooth: {{ type: "continuous" }}, color: {{ opacity: 0.5 }}, arrows: "to" }},
    interaction: {{ hover: true }}
  }});

  // ponytail: Klick statt Hover-Tooltip — Panel unten, mehrzeilig, textContent (kein innerHTML-Risiko)
  const info = document.getElementById("info");
  function showInfo(header, group, body) {{
    info.innerHTML = "";
    const h = document.createElement("b");
    h.textContent = header + (group ? " [" + group + "]" : "");
    const b = document.createElement("div");
    b.textContent = body || "";
    info.appendChild(h); info.appendChild(b);
    info.style.display = "block";
  }}
  network.on("click", params => {{
    if (params.nodes.length) {{
      const n = nodesDS.get(params.nodes[0]);
      showInfo(n.label, n.group, n.desc);
    }} else if (params.edges.length) {{
      const e = edgesDS.get(params.edges[0]);
      const u = nodesDS.get(e.from), v = nodesDS.get(e.to);
      showInfo((u ? u.label : e.from) + " → " + (v ? v.label : e.to), "", e.desc);
    }} else {{
      info.style.display = "none";
    }}
  }});
</script></body></html>"""
