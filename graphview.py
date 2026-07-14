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


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _status_badge(st: dict) -> str:
    """Ingest-Status als Badge für eine Projekt-Karte (leer, wenn kein Status)."""
    state = st.get("state")
    total = st.get("total", "?")
    if state == "running":
        msg = st.get("msg")
        detail = f' · {_esc(msg)}' if msg else ""
        return (f'<span class="badge run">⏳ Ingest läuft — {st.get("done", 0)}/{total} '
                f'Dokumente fertig{detail}</span>')
    if state == "done":
        return (f'<span class="badge done">✓ zuletzt indexiert: {st.get("new", 0)} neu, '
                f'{st.get("updated", 0)} aktualisiert ({_esc(st.get("at", ""))})</span>')
    if state == "error":
        return f'<span class="badge err">✗ Ingest-Fehler: {_esc(st.get("error", ""))}</span>'
    return ""


def _backup_section(cfg: dict, backups: list[dict]) -> str:
    """Backup-Karte: Schedule-Dropdown, Jetzt-sichern-Button, letzte Archive."""
    interval = cfg.get("interval", "daily") if cfg.get("enabled") else "off"
    labels = {"off": "aus", "hourly": "stündlich", "daily": "täglich", "weekly": "wöchentlich"}
    opts = "".join(f'<option value="{k}"{" selected" if k == interval else ""}>{v}</option>'
                   for k, v in labels.items())
    last = cfg.get("last_backup")
    last_txt = (f"Letztes Backup: {_esc(last.replace('T', ' ')[:16])}" if last
                else "Noch kein Backup gelaufen")
    if backups:
        rows = "\n".join(
            f'<li><code>{_esc(b["name"])}</code> · {b["size"] / 1024 / 1024:.1f} MB</li>'
            for b in backups[:5])
        files = f'<ul class="bk">{rows}</ul>'
    else:
        files = '<p class="hint">Noch keine Archive im Backup-Ordner.</p>'
    return f"""<h2>Backup</h2>
<div class="steps">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
    <form method="post" action="/backup/config" style="display:flex;align-items:center;gap:6px;margin:0">
      <label for="iv">Zeitplan:</label>
      <select id="iv" name="interval" style="font:inherit;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--card);color:var(--text)">{opts}</select>
      <button class="del" type="submit" title="Zeitplan speichern">Speichern</button>
    </form>
    <form method="post" action="/backup/now" style="margin:0">
      <button class="del" type="submit" title="Sofort ein Backup schreiben">Jetzt sichern</button>
    </form>
    <span class="hint">{last_txt}</span>
  </div>
  {files}
</div>"""


def index_html(items: list[tuple[str, bool]], status: dict | None = None, meta: dict | None = None,
               backup_cfg: dict | None = None, backups: list[dict] | None = None) -> str:
    """Landing-Page für den Viewer-Root. items = (projekt_id, hat_graph_html).
    status = {projekt_id: ingest_status_dict} — zeigt Import-Fortschritt pro Karte.
    meta = {projekt_id: {"project_name": "..."}} — Anzeigenamen.
    backup_cfg/backups — Backup-Zeitplan und vorhandene Archive.
    Erklärt, was zu sehen ist und wie es weitergeht (statt rohem Dir-Listing)."""
    status = status or {}
    meta = meta or {}
    running = any(s.get("state") == "running" for s in status.values())

    def _row(p: str, has: bool) -> str:
        e = _esc(p)
        m = meta.get(p, {})
        display_name = m.get("project_name") or p
        badge = _status_badge(status.get(p, {}))
        left = (f'<a class="nm open" href="./{e}/graph.html">{_esc(display_name)}'
                '<span class="go"> · Graph öffnen →</span></a>' if has else
                f'<span class="nm">{_esc(display_name)}</span>'
                f'<span class="hint">noch nicht gerendert</span>') + badge
        # Buttons: Erstellen/Aktualisieren (POST /refresh) + Umbenennen + Löschen
        refresh_form = (f'<form method="post" action="/refresh" class="del" style="margin-right:6px">'
                       f'<input type="hidden" name="project_id" value="{e}">'
                       f'<button title="{"Graph aktualisieren" if has else "Graph erstellen"}">{"Aktualisieren" if has else "Erstellen"}</button></form>')
        rename_form = (f'<form method="post" action="/rename" class="del" style="margin-right:6px" '
                      f'onsubmit="const n=prompt(\'Neuer Anzeigename für &quot;{e}&quot;:\', \'{_esc(display_name)}\'); '
                      f'if(n===null) return false; document.querySelector(\'input[name=project_name]\').value=n; return true;">'
                      f'<input type="hidden" name="project_id" value="{e}">'
                      f'<input type="hidden" name="project_name" value="">'
                      '<button type="submit" title="Anzeigenamen ändern">Umbenennen</button></form>')
        delete_form = (f'<form method="post" action="/delete" class="del" '
                      f"onsubmit=\"return confirm('Projekt &quot;{e}&quot; löschen? "
                      "Der Index wird entfernt, die Quelldokumente bleiben.')\">"
                      f'<input type="hidden" name="project_id" value="{e}">'
                      '<button title="Projekt-Index löschen">Löschen</button></form>')
        forms = refresh_form + rename_form + delete_form
        cls = "card" if has else "card todo"
        return f'<div class="{cls}"><div class="left">{left}</div><div style="display:flex;gap:6px">{forms}</div></div>'

    if items:
        rows = "\n".join(_row(p, has) for p, has in items)
    else:
        rows = ('<p class="empty">Noch keine Projekte indexiert. Erst '
                "<code>ingest_paperless(...)</code> oder <code>ingest_directory(...)</code> "
                "aufrufen, dann <code>graph_view(projekt)</code>.</p>")
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{'<meta http-equiv="refresh" content="5">' if running else ''}
<title>doc-graph · Knowledge Graphs</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{{--bg:#fafafa;--card:#fff;--border:#ececec;--accent:#388e3c;--ah:#2e7d32;--text:#333;--muted:#666}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:"Source Sans 3","Source Sans Pro",Arial,sans-serif;letter-spacing:.15pt;font-size:14px;padding:32px;max-width:760px;margin:0 auto}}
  h1{{font-size:22px;font-weight:700;margin-bottom:4px}}
  .sub{{color:var(--muted);font-size:13px;margin-bottom:24px}}
  h2{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 12px}}
  .grid{{display:grid;gap:10px;margin-bottom:28px}}
  .card{{display:flex;align-items:center;justify-content:space-between;gap:12px;
    background:var(--card);border:1px solid var(--border);border-left:3px solid var(--accent);
    border-radius:10px;padding:14px 18px;transition:box-shadow .15s}}
  .card:hover{{box-shadow:0 3px 14px rgba(0,0,0,.08)}}
  .card.todo{{border-left-color:#bbb}}
  .left{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;min-width:0}}
  .nm{{font-weight:600;font-size:15px;color:var(--text);text-decoration:none}}
  a.nm.open:hover .go{{text-decoration:underline}}
  .go{{color:var(--accent);font-size:13px;font-weight:600;white-space:nowrap}}
  .hint,.empty{{color:var(--muted);font-size:12px}}
  .badge{{font-size:12px;font-weight:600;padding:2px 9px;border-radius:20px;white-space:nowrap}}
  .badge.run{{background:#fff8e1;color:#8a6d00;border:1px solid #ffe082}}
  .badge.done{{background:#edf7ee;color:var(--ah);border:1px solid #c8e6c9}}
  .badge.err{{background:#fff5f5;color:#c62828;border:1px solid #ffcdd2}}
  .del, .del button{{background:none;border:1px solid var(--border);color:var(--muted);
    border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer;white-space:nowrap;transition:all .15s;margin:0;display:inline-block}}
  .del:hover, .del button:hover{{border-color:#dd3333;color:#dd3333;background:#fff5f5}}
  code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:1px 5px}}
  .steps{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px 22px;font-size:13px;line-height:1.7}}
  .steps ol{{margin:8px 0 0 18px}}
  .bk{{list-style:none;margin:0;font-size:12px;color:var(--muted)}}
</style></head><body>
<h1>doc-graph</h1>
<p class="sub">Knowledge Graphs aus deinen Dokumenten — pro Projekt ein Graph. Klick ein Projekt an, um den interaktiven Graphen zu öffnen.</p>
<h2>Projekte</h2>
<div class="grid">
{rows}
</div>
{_backup_section(backup_cfg or {}, backups or [])}
<h2>Wie es weitergeht</h2>
<div class="steps">
  Neue Dokumente in den Graphen bringen — im Claude-Code-Prompt:
  <ol>
    <li><code>ingest_paperless(project="x", tag="…")</code> bzw. <code>ingest_directory(project="x", subpath="…")</code> — indexieren</li>
    <li><code>graph_view(project="x")</code> — Graph rendern, erscheint dann oben als Karte</li>
    <li><code>query(project="x", question="…")</code> — den Graphen befragen</li>
  </ol>
</div>
</body></html>"""


def _project_select(projects: list[str] | None, current: str, names: dict[str, str] | None = None) -> str:
    """Dropdown zum Umschalten zwischen Projekt-Graphen (navigiert zur graph.html
    des gewählten Projekts). Leer, wenn nur ein/kein Projekt vorliegt.
    names = {project_id: display_name} für schönere Labels."""
    if not projects or len(projects) < 2:
        return ""
    names = names or {}
    opts = "".join(
        f'<option value="{p}"{" selected" if p == current else ""}>{_esc(names.get(p) or p)}</option>'
        for p in projects
    )
    return ('<label class="muted">Projekt '
            "<select id=\"proj\" onchange=\"location.href='../'+this.value+'/graph.html'\">"
            f"{opts}</select></label>")


def graph_html(nodes: list[dict], edges: list[dict], title: str,
               projects: list[str] | None = None, current: str = "", names: dict[str, str] | None = None) -> str:
    """Baut aus Knoten/Kanten-Dicts ein eigenständiges vis-network-HTML.
    names = {project_id: display_name} für Dropdown und Refresh-Button."""
    # json.dumps escaped '<' nicht; </script> in Daten würde das Script sprengen.
    payload = json.dumps({"nodes": nodes, "edges": edges}).replace("<", "\\u003c")
    proj_select = _project_select(projects, current, names)
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
  <form method="post" action="../refresh" style="margin:0;display:inline;margin-right:6px">
    <input type="hidden" name="project_id" value="{current}">
    <button type="submit" style="background:none;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer;white-space:nowrap;transition:all .15s" title="Graph aus .graphml neu rendern">Aktualisieren</button>
  </form>
  <form method="post" action="../rename" style="margin:0;display:inline;margin-right:6px" onsubmit="const n=prompt('Neuer Anzeigename:'); if(n===null) return false; document.querySelector('input[name=project_name]').value=n; return true;">
    <input type="hidden" name="project_id" value="{current}">
    <input type="hidden" name="project_name" value="">
    <button type="submit" style="background:none;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer;white-space:nowrap;transition:all .15s" title="Anzeigenamen ändern">Umbenennen</button>
  </form>
  <label class="muted"><input type="checkbox" id="phys" checked onchange="net&&net.setOptions({{physics:{{enabled:this.checked}}}})"> Physik</label>
  <label class="muted" title="Knoten anklicken, dann anhaken: zeigt nur dessen Nachbarschaft (Doppelklick setzt Anker um)"><input type="checkbox" id="focus" onchange="setFocus()"> nur Verbundene</label>
  <label class="muted" title="Nachbarschafts-Tiefe in Hops">Distanz <input type="number" id="depth" value="1" min="1" style="width:3em" onchange="build()"></label>
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
  let net=null, SEL=null, FOCUS=null;   // SEL=angeklickt, FOCUS=Fokus-Anker

  function showInfo(header, group, body){{
    const chip=group?`<span class="chip" style="background:${{COL[group]||'#636363'}}">${{esc(group)}}</span>`:'';
    const d=body?`<div class="d">${{esc(body)}}</div>`:'';
    $('info').innerHTML=`<div class="hd">${{chip}}<span class="nm">${{esc(header)}}</span></div>${{d}}`;
    $('info').style.display='block';
  }}

  function build(){{
    let ents=data.nodes.filter(n=>!HIDE.has(n.group));
    if($('focus').checked&&FOCUS){{  // Anker + Nachbarn bis Distanz n (BFS)
      const depth=Math.max(1,+$('depth').value||1);
      const nb=new Set([FOCUS]);
      for(let d=0;d<depth;d++){{
        const cur=new Set(nb);  // Snapshot: genau eine Distanz pro Runde
        data.edges.forEach(e=>{{if(cur.has(e.from))nb.add(e.to);if(cur.has(e.to))nb.add(e.from);}});
      }}
      ents=ents.filter(n=>nb.has(n.id));
    }}
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
      if(p.nodes.length){{SEL=p.nodes[0];const src=data.nodes.find(x=>x.id===SEL);showInfo(src.label,src.group,src.desc);}}
      else if(p.edges.length){{const e=edgesDS.get(p.edges[0]);const u=nodesDS.get(e.from),v=nodesDS.get(e.to);
        showInfo((u?u.label:e.from)+' → '+(v?v.label:e.to),'',e.desc);}}
      else{{SEL=null;$('info').style.display='none';}}
    }});
    net.on('doubleClick',p=>{{  // Doppelklick im Fokus-Modus: Anker umsetzen
      if(p.nodes.length&&$('focus').checked){{FOCUS=SEL=p.nodes[0];
        const src=data.nodes.find(x=>x.id===FOCUS);showInfo(src.label,src.group,src.desc);build();}}
    }});
  }}
  function setFocus(){{FOCUS=$('focus').checked?SEL:null;build();}}  // Anker = aktuelle Auswahl

  function toggleType(t){{HIDE.has(t)?HIDE.delete(t):HIDE.add(t);renderLeg();build();}}
  function renderLeg(){{
    $('leg').innerHTML=Object.entries(COL).map(([t,c])=>
      `<span onclick="toggleType('${{esc(t)}}')" style="opacity:${{HIDE.has(t)?0.35:1}}" title="${{HIDE.has(t)?'einblenden':'ausblenden'}}"><i class="dot" style="background:${{c}}"></i>${{esc(t)}}</span>`).join('');
  }}

  renderLeg(); build();
</script></body></html>"""
