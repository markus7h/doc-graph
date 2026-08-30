"""Reine Graph-HTML-Erzeugung (nur stdlib) — vom Server getrennt, damit ohne
LightRAG/MCP-Deps testbar (siehe test_graph.py). Optik/Feature-Set an den
ai-rem-Graphen angelehnt: heller BG, grüner Akzent, klickbare Typ-Legende zum
Filtern, Physik-Toggle, Typ-Chip im Info-Panel."""

import hashlib
import re

# vis-network per CDN (der Browser braucht Internet). Bewusst kein Inline-Bundle:
# ponytail: CDN reicht im LAN; ~1 MB inline lohnt nur bei echtem Offline-Zwang.
_VIS_CDN = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"

# stabile Farben pro entity_type (Fallback: aus Namen abgeleitet).
# Deutsch UND englisch, weil die Typ-Whitelist (server.ENTITY_TYPES) deutsch
# ist, ältere Projekte aber noch mit LightRAGs englischen Defaults indexiert
# wurden — gleiche Bedeutung soll gleiche Farbe bekommen.
_TYPE_COLORS = {
    "person": "#e6550d",
    "organization": "#3182bd", "organisation": "#3182bd",
    "location": "#31a354", "ort": "#31a354", "geo": "#31a354",
    "event": "#756bb1", "vorgang": "#756bb1",
    "category": "#636363", "begriff": "#636363", "concept": "#843c39",
    "date": "#e7ba52", "datum": "#e7ba52",
    "dokument": "#9e7bb5", "content": "#9e7bb5",
    "rechtsnorm": "#a63603",
    "betrag": "#17becf",
    "sache": "#8c6d31", "artifact": "#8c6d31",
    "other": "#999999",
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


def _svg(paths: str, solid: bool = False) -> str:
    """Inline-SVG-Icon (16er-Viewport, currentColor) — rendert überall zuverlässig,
    anders als Emoji-Glyphen (die je nach Font fehlen, z.B. Pause/Stop)."""
    attr = ('fill="currentColor"' if solid else
            'fill="none" stroke="currentColor" stroke-width="1.5" '
            'stroke-linecap="round" stroke-linejoin="round"')
    return (f'<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" '
            f'{attr}>{paths}</svg>')


# Icons für die Karten-Schaltflächen (Icon-only, Text kommt als CSS-Tooltip).
_ICON = {
    "refresh": _svg('<path d="M13 8a5 5 0 1 1-1.46-3.54"/><path d="M13 2.8v2.4h-2.4"/>'),
    "create": _svg('<path d="M8 3.3v9.4M3.3 8h9.4"/>'),
    "rename": _svg('<path d="M3.2 12.8l.85-2.7 6.4-6.4 1.85 1.85-6.4 6.4z"/>'
                   '<path d="M9.6 4.1l1.85 1.85"/>'),
    "delete": _svg('<path d="M3.2 4.6h9.6M6.4 4.6V3.2h3.2v1.4M4.7 4.6l.5 8.2h5.6l.5-8.2"/>'),
    "pause": _svg('<rect x="4.4" y="3" width="2.5" height="10" rx=".4"/>'
                  '<rect x="9.1" y="3" width="2.5" height="10" rx=".4"/>', solid=True),
    "resume": _svg('<path d="M5 3.4l7.2 4.6L5 12.6z"/>', solid=True),
    "stop": _svg('<rect x="3.9" y="3.9" width="8.2" height="8.2" rx="1.1"/>', solid=True),
    "save": _svg('<path d="M8 2.7v6.6M5.3 6.6 8 9.3l2.7-2.7"/><path d="M3.4 12.6h9.2"/>'),
    "restore": _svg('<path d="M4.3 6.1H2.7V4.5"/>'
                    '<path d="M3 6A5.2 5.2 0 1 1 3.4 10.2"/>'),
}


def _icon_btn(icon: str, tip: str, submit: bool = True) -> str:
    """Icon-only-Button: SVG + Tooltip (data-tip, per CSS nach ~1,8 s) + aria-label."""
    typ = ' type="submit"' if submit else ""
    return (f'<button{typ} class="ib" data-tip="{_esc(tip)}" aria-label="{_esc(tip)}">'
            f'{_ICON[icon]}</button>')


def node_dict(n, d: dict) -> dict:
    """GraphML-Knoten -> Viewer-Dict (id/label/group/color/desc)."""
    etype = d.get("entity_type", "")
    return {"id": n, "label": str(n).strip('"'), "group": etype,
            "color": color_for(etype), "desc": d.get("description", "")[:400]}


def edge_dict(u, v, d: dict) -> dict:
    """GraphML-Kante -> Viewer-Dict (from/to/desc)."""
    tip = d.get("description") or d.get("keywords") or ""
    return {"from": u, "to": v, "desc": str(tip)[:400]}


def graph_subset(nodes: dict, edges: list, adj: dict, degree: dict, *,
                 limit: int, focus: str | None = None, depth: int = 1,
                 q: str | None = None, hide: set | None = None) -> dict:
    """Baut ein auf `limit` gedeckeltes Knoten/Kanten-Subset für den Viewer.
    Reine Datenstruktur-Transformation (kein networkx) -> stdlib-testbar.

    nodes  = {id: node_dict}, edges = [edge_dict], adj = {id: set(nachbar_ids)},
    degree = {id: grad}. Priorisierung beim Deckeln: Knotengrad (kein Score im
    GraphML). Liefert {nodes, edges, total, shown, capped, types}: `total` = Größe
    der (gefilterten) Kandidatenmenge vor dem Deckeln; `types` immer über den
    GANZEN Graph, damit die Legende stabil bleibt."""
    hide = hide or set()

    types: dict[str, int] = {}
    for nd in nodes.values():
        types[nd["group"]] = types.get(nd["group"], 0) + 1

    def _visible(nid) -> bool:
        return nodes[nid]["group"] not in hide

    if focus and focus in nodes:
        # BFS-Nachbarschaft bis `depth` Hops um den Anker.
        seen = {focus}
        frontier = {focus}
        for _ in range(max(1, depth)):
            nxt: set = set()
            for u in frontier:
                nxt |= adj.get(u, set())
            nxt -= seen
            seen |= nxt
            frontier = nxt
        selected = [n for n in seen if _visible(n)]
    elif q:
        ql = q.strip().lower()
        hits = [n for n in nodes if _visible(n) and ql in nodes[n]["label"].lower()]
        ctx = set(hits)  # Treffer + direkte Nachbarn als Kontext
        for h in hits:
            ctx |= {nb for nb in adj.get(h, set()) if _visible(nb)}
        selected = list(ctx)
    else:
        selected = [n for n in nodes if _visible(n)]

    total = len(selected)
    capped = total > limit
    if capped:
        selected.sort(key=lambda n: degree.get(n, 0), reverse=True)
        selected = selected[:limit]

    sel = set(selected)
    out_nodes = [nodes[n] for n in selected]
    out_edges = [e for e in edges if e["from"] in sel and e["to"] in sel]
    return {
        "nodes": out_nodes, "edges": out_edges,
        "total": total, "shown": len(out_nodes), "capped": capped,
        "types": [{"type": t, "color": color_for(t), "count": c}
                  for t, c in sorted(types.items())],
    }


def _status_badge(st: dict) -> str:
    """Ingest-Status als Badge für eine Projekt-Karte (leer, wenn kein Status)."""
    state = st.get("state")
    total = st.get("total", "?")
    if state == "running":
        # 'done' zählt nur die Dispatch-Schleife und unterschätzt massiv, weil
        # LightRAG im Batch verarbeitet. Echter Fortschritt = doc_status.processed
        # gegen alle getaggten Docs (new+updated+skipped).
        docs = st.get("docs") or {}
        proc = docs.get("processed", st.get("done", 0))
        tagged = (st.get("new", 0) + st.get("updated", 0) + st.get("skipped", 0)) or total
        inprog = sum(docs.get(k, 0) for k in ("processing", "analyzing", "parsing", "pending"))
        failed = docs.get("failed", 0)
        extra = f" · {inprog} in Arbeit" if inprog else ""
        extra += f" · {failed} fehlgeschlagen" if failed else ""
        msg = st.get("msg")
        detail = f' · {_esc(msg)}' if msg else ""
        return (f'<span class="badge run">⏳ Ingest läuft — {proc}/{tagged} '
                f'Dokumente im Graph{extra}{detail}</span>')
    if state == "paused":
        return (f'<span class="badge run">⏸ Ingest pausiert — {st.get("done", 0)}/{total} '
                f'Dokumente fertig (GPU freigegeben)</span>')
    if state == "stopped":
        return (f'<span class="badge done">⏹ Ingest abgebrochen bei {st.get("done", 0)}/{total} '
                f'Dokumenten ({_esc(st.get("at", ""))})</span>')
    if state == "done":
        return (f'<span class="badge done">✓ zuletzt indexiert: {st.get("new", 0)} neu, '
                f'{st.get("updated", 0)} aktualisiert ({_esc(st.get("at", ""))})</span>')
    if state == "error":
        return f'<span class="badge err">✗ Ingest-Fehler: {_esc(st.get("error", ""))}</span>'
    return ""


def _progress_row(st: dict, controls: str = "") -> str:
    """Vollbreite Fortschrittszeile für einen laufenden/pausierten Ingest:
    Balken (done/total) + Status-Badge + Ingest-Steuerung (Pause/Stop/Fortsetzen)."""
    # Fortschritt an den echten LightRAG-Zuständen (processed), nicht am
    # Dispatch-Zähler 'done' — LightRAG batcht, 'done' bleibt lange 0.
    docs = st.get("docs") or {}
    done = docs.get("processed", st.get("done", 0))
    total = (st.get("new", 0) + st.get("updated", 0) + st.get("skipped", 0))
    if not total:
        total = st.get("total") if isinstance(st.get("total"), int) else 0
    pct = int(done / total * 100) if total else 0
    fill_cls = "fill paused" if st.get("state") == "paused" else "fill"
    ctl = f'<div class="prog-ctl">{controls}</div>' if controls else ""
    return (f'<div class="prog"><div class="bar"><div class="{fill_cls}" '
            f'style="width:{pct}%"></div></div>{_status_badge(st)}{ctl}</div>')


# doc-graph-Icon (Variante 2): blaues Dokument mit Textzeilen + herausragendem
# Graph-Netzwerk. Inline-SVG, damit ohne externe Assets/CDN.
_LOGO = (
    '<svg class="logo" viewBox="0 0 56 48" width="42" height="36" aria-hidden="true">'
    '<defs><linearGradient id="dg" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#4a6fb5"/><stop offset="1" stop-color="#2c4577"/>'
    '</linearGradient></defs>'
    '<path d="M4 8a4 4 0 0 1 4-4h18l8 8v22a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="url(#dg)"/>'
    '<path d="M26 4l8 8h-8z" fill="#fff" opacity=".35"/>'
    '<g stroke="#fff" stroke-width="2.4" stroke-linecap="round" opacity=".9">'
    '<line x1="9" y1="15" x2="21" y2="15"/><line x1="9" y1="21" x2="19" y2="21"/>'
    '<line x1="9" y1="27" x2="17" y2="27"/></g>'
    '<g stroke="#9db8e0" stroke-width="2.6" stroke-linecap="round">'
    '<line x1="31" y1="29" x2="43" y2="18"/><line x1="31" y1="29" x2="49" y2="32"/></g>'
    '<circle cx="31" cy="29" r="4.5" fill="#fff"/>'
    '<circle cx="43" cy="18" r="4" fill="#1e3050"/>'
    '<circle cx="49" cy="32" r="4.5" fill="#9db8e0"/></svg>'
)

# Browser-Favicon: dasselbe Motiv quadratisch (0 0 48), transparenter Hintergrund,
# als data-URI (# -> %23, sonst bricht die URL). Setzt zugleich den 404 auf
# /favicon.ico still, weil der Browser dann diese Deklaration nimmt.
_FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'>"
    "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
    "<stop offset='0' stop-color='#4a6fb5'/><stop offset='1' stop-color='#2c4577'/>"
    "</linearGradient></defs>"
    "<path d='M6 8a4 4 0 0 1 4-4h16l8 8v24a4 4 0 0 1-4 4H10a4 4 0 0 1-4-4z' fill='url(#g)'/>"
    "<path d='M26 4l8 8h-8z' fill='#fff' opacity='.35'/>"
    "<g stroke='#fff' stroke-width='2.4' stroke-linecap='round' opacity='.9'>"
    "<line x1='11' y1='15' x2='22' y2='15'/><line x1='11' y1='21' x2='20' y2='21'/></g>"
    "<g stroke='#9db8e0' stroke-width='2.6' stroke-linecap='round'>"
    "<line x1='20' y1='31' x2='30' y2='24'/><line x1='20' y1='31' x2='31' y2='34'/></g>"
    "<circle cx='20' cy='31' r='4' fill='#fff'/>"
    "<circle cx='30' cy='24' r='3.5' fill='#1e3050'/>"
    "<circle cx='31' cy='34' r='4' fill='#9db8e0'/></svg>"
).replace("#", "%23")

_FAVICON_LINK = f'<link rel="icon" type="image/svg+xml" href="{_FAVICON}">'

# Kopfzeile: Logo + Wortmarke links, Navigation rechts (eine Zeile, wie ueblich).
# CSS ohne doppelte Klammern -- wird in die f-String-Templates eingesetzt, nicht geparst.
_HEADER_CSS = """
  .brand{display:flex;align-items:center;gap:12px;margin:0 0 20px;padding-bottom:12px;
    border-bottom:1px solid var(--border);flex-wrap:wrap}
  .brand h1,.brand .wm{margin:0;font-size:22px;font-weight:700;color:var(--text);text-decoration:none}
  .logo{flex:none;display:block}
  .brand nav{margin-left:auto;display:flex;gap:18px;font-size:14px}
  .brand nav a{color:var(--muted);text-decoration:none}
  .brand nav a:hover{color:var(--accent)}
  .brand nav a.on{color:var(--accent);font-weight:600}
"""

# Kasten fuer alles, was in den KI-Chat gehoert. Ohne das Etikett sieht er aus
# wie jeder andere Codeblock; pre-wrap, weil dort auch ganze Saetze stehen.
_CHAT_CSS = """
  .chat{position:relative;margin:8px 0 0}
  .chat pre{margin:0;padding-top:22px;white-space:pre-wrap}
  .chat-label{position:absolute;top:5px;right:9px;font-size:10.5px;font-weight:600;
    letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
  .chat + p{margin-top:6px}
"""


def chat_kasten(eingabe: str, hinweis: str = "") -> str:
    """Einheitliche Form für jeden Hinweis auf den KI-Chat, überall gleich:

    Im Kasten steht NUR, was der Mensch selbst eingibt — mit dem Etikett
    „KI Chat", damit erkennbar ist, wo das hingehört. Was Claude daraufhin von
    sich aus tut, steht als Prosa darunter; beides gleichrangig als Code zu
    zeigen las sich wie eine Aufforderung, die Aufrufe nacheinander selbst
    einzutippen. Gleiche Funktion in case-assist (viewer.chat_kasten).

    `eingabe` wird escaped, `hinweis` ist HTML (er trägt <code>-Auszeichnung).
    """
    kasten = (f'<div class="chat"><span class="chat-label">KI Chat</span>'
              f"<pre>{_esc(eingabe)}</pre></div>")
    return kasten + (f"<p>{hinweis}</p>" if hinweis else "")


_WEITER_KASTEN = chat_kasten(
    "Nimm die Dokumente mit dem Paperless-Tag future-fund ins Projekt "
    "future-fund auf und zeig mir danach den Graphen.",
    "Claude indexiert mit <code>ingest_paperless</code> (bzw. "
    "<code>ingest_directory</code> für einen Ordner), verfolgt den Lauf mit "
    "<code>ingest_status</code> und rendert den Graphen mit "
    "<code>graph_view</code> — er erscheint dann oben als Karte. Befragen "
    "lässt er sich danach mit <code>query</code>.")

# Schaltflaechen, Badges und Hinweise — geteilt zwischen Projektuebersicht und
# Backup-Seite. CSS ohne doppelte Klammern, wird in die f-String-Templates
# eingesetzt (Muster: _HEADER_CSS).
_BTN_CSS = """
  /* Icon-only-Schaltflächen: quadratisch, SVG zentriert. */
  .del button.ib,.dec button.ib{position:relative;width:32px;height:30px;padding:0;
    display:inline-flex;align-items:center;justify-content:center;overflow:visible}
  .ib svg{display:block;pointer-events:none}
  /* Tooltip: erscheint per CSS erst nach ~1,8 s Hover (transition-delay), nicht der native title. */
  .ib::after{content:attr(data-tip);position:absolute;top:calc(100% + 7px);left:50%;
    transform:translateX(-50%);background:#333;color:#fff;font-size:12px;font-weight:500;
    line-height:1.1;white-space:nowrap;padding:5px 8px;border-radius:5px;
    box-shadow:0 2px 8px rgba(0,0,0,.18);opacity:0;pointer-events:none;transition:opacity .12s ease;z-index:30}
  .ib:hover::after{opacity:1;transition-delay:1.8s}
  /* Icon-Buttons hovern grün (Aktion), nur Löschen bleibt rot (destruktiv). */
  .del button.ib:hover{border-color:var(--accent);color:var(--accent);background:#e8edf7}
  .del.danger button.ib:hover{border-color:#dd3333;color:#dd3333;background:#fff5f5}
  .hint,.empty{color:var(--muted);font-size:13px}
  .badge{font-size:13px;font-weight:600;padding:2px 9px;border-radius:20px;white-space:nowrap}
  .badge.run{background:#fff8e1;color:#8a6d00;border:1px solid #ffe082}
  .badge.done{background:#e8edf7;color:var(--ah);border:1px solid #c8e6c9}
  .badge.err{background:#fff5f5;color:#c62828;border:1px solid #ffcdd2}
  .del, .del button{background:none;border:1px solid var(--border);color:var(--muted);
    border-radius:6px;padding:5px 11px;font-size:13px;cursor:pointer;white-space:nowrap;
    transition:all .15s;margin:0;display:inline-block}
  .del:hover, .del button:hover{border-color:#dd3333;color:#dd3333;background:#fff5f5}
  .steps{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:18px 22px;font-size:14px;line-height:1.7}
"""

_NAV = (("/", "Projekte", "index"), ("/backup", "Backup", "backup"),
        ("/hilfe", "Hilfe", "hilfe"))


def _header(active: str) -> str:
    """Kopfzeile fuer 'index' bzw. 'hilfe'; die aktive Seite ist im Nav markiert."""
    nav = "".join(
        f'<a href="{href}"{" class=\"on\"" if key == active else ""}>{text}</a>'
        for href, text, key in _NAV
    )
    wm = "<h1>doc-graph</h1>" if active == "index" else '<a class="wm" href="/">doc-graph</a>'
    return f'<header class="brand"><a href="/">{_LOGO}</a>{wm}<nav>{nav}</nav></header>'


def _backup_time(name: str) -> str:
    """'backup_2026-07-16_14-30-05.tar.gz' -> '2026-07-16 14:30' (Fallback: Name)."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})", name)
    return f"{m.group(1)} {m.group(2)}:{m.group(3)}" if m else name


_NOTICES = {
    "backup:ok": ("done", "✓ Backup geschrieben."),
    "backup:nochange": ("run", "Nichts geändert seit dem letzten Backup — kein neues Archiv."),
    "restore:ok": ("done", "✓ Backup wiederhergestellt."),
    "restore:err": ("err", "✗ Restore fehlgeschlagen — keine gültige Backup-Datei."),
}


def _card_backup(e: str, backups: list[dict]) -> str:
    """Backup-Steuerung EINES Projekts für die Karte: 'Sichern' + (falls Archive
    vorhanden) Auswahl der letzten 5 Stände + 'Wiederherstellen'."""
    save = (f'<form method="post" action="/backup/now" class="del" style="margin:0">'
            f'<input type="hidden" name="project_id" value="{e}">'
            f'{_icon_btn("save", "Dieses Projekt sichern (nur bei Änderung)")}</form>')
    if not backups:
        return save
    opts = "".join(f'<option value="{_esc(b["name"])}">{_esc(_backup_time(b["name"]))} '
                   f'· {b["size"] / 1024 / 1024:.1f} MB</option>' for b in backups[:5])
    restore = (f'<form method="post" action="/backup/restore" class="del" style="margin:0;display:flex;gap:4px" '
               f'onsubmit="return confirm(\'Projekt &quot;{e}&quot; durch diesen Stand ERSETZEN? '
               f'Der jetzige Stand geht verloren.\')">'
               f'<input type="hidden" name="project_id" value="{e}">'
               f'<select name="name" style="font:inherit;font-size:13px;border:none;background:none;color:var(--muted);max-width:170px">{opts}</select>'
               f'{_icon_btn("restore", "Gewählten Stand zurückspielen")}</form>')
    return save + restore


def backup_html(cfg: dict, project_backups: dict | None = None,
                notice: str | None = None) -> str:
    """Eigene Seite (/backup): Zeitplan, Restore aus Datei und die vorhandenen
    Stände je Projekt. Stand früher als Abschnitt unter den Projekten und die
    Sicherungs-Knöpfe auf jeder Projektkarte — auf der Übersicht ging es damit
    um zwei Dinge gleichzeitig."""
    project_backups = project_backups or {}
    interval = cfg.get("interval", "daily") if cfg.get("enabled") else "off"
    labels = {"off": "aus", "hourly": "stündlich", "daily": "täglich",
              "weekly": "wöchentlich"}
    opts = "".join(f'<option value="{k}"{" selected" if k == interval else ""}>{v}</option>'
                   for k, v in labels.items())
    lasts = [pm.get("last_backup") for pm in cfg.get("projects", {}).values()
             if pm.get("last_backup")]
    last = max(lasts) if lasts else None
    last_txt = f"Letztes Backup: {_esc(last[:16])}" if last else "Noch kein Backup gelaufen"
    cls, msg = _NOTICES.get(notice or "", ("", ""))
    banner = f'<div class="badge {cls}" style="margin-bottom:10px">{_esc(msg)}</div>' if msg else ""

    if project_backups:
        zeilen = "".join(
            f"<tr><td><b>{_esc(p)}</b></td>"
            f'<td class="hint">{_stand_text(b)}</td>'
            f'<td style="text-align:right"><div class="actions">{_card_backup(_esc(p), b)}</div></td></tr>'
            for p, b in sorted(project_backups.items()))
        tabelle = (f"<table><tr><th>Projekt</th><th>Stände</th><th></th></tr>"
                   f"{zeilen}</table>")
    else:
        tabelle = '<p class="hint">Noch keine Projekte.</p>'

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>doc-graph · Backup</title>
{_FAVICON_LINK}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{{--bg:#fafafa;--card:#fff;--border:#ececec;--accent:#3a5a9b;--ah:#2c4678;--text:#333;--muted:#666}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:"Source Sans 3","Source Sans Pro",Arial,sans-serif;
    letter-spacing:.15pt;font-size:15px;line-height:1.6;padding:32px;max-width:900px;margin:0 auto}}
  h1{{font-size:22px;margin-bottom:4px}}
  h2{{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
    color:var(--muted);margin:28px 0 12px}}
  p.sub{{color:var(--muted);font-size:14px;margin-bottom:8px}}
  a{{color:var(--accent)}}
  code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;
    background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:1px 5px}}
  table{{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);
    border:1px solid var(--border);border-radius:10px;overflow:hidden}}
  th,td{{text-align:left;padding:10px 14px;border-top:1px solid var(--border);vertical-align:middle}}
  th{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);border-top:none}}
  .actions{{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}}
{_HEADER_CSS}
{_BTN_CSS}
</style></head><body>
{_header("backup")}
<h1>Backup</h1>
<p class="sub">Jedes Projekt wird einzeln gesichert — als <code>tar.gz</code> im
Backup-Verzeichnis. Der Zeitplan nimmt nur Projekte mit, die sich seit dem
letzten Lauf geändert haben.</p>

<h2>Zeitplan</h2>
<div class="steps">
  {banner}
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <form method="post" action="/backup/config" style="display:flex;align-items:center;gap:6px;margin:0">
      <label for="iv">Sichern:</label>
      <select id="iv" name="interval" style="font:inherit;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--card);color:var(--text)">{opts}</select>
      <button class="del" type="submit" title="Zeitplan speichern">Speichern</button>
    </form>
    <label class="del" style="cursor:pointer" title="Projekt-Backup vom Rechner wiederherstellen (z. B. aus dem synchronisierten OneDrive-Ordner) — legt das Projekt bei Bedarf neu an">
      Projekt aus Datei wiederherstellen…
      <input type="file" accept=".gz,.tgz,.tar.gz" style="display:none" onchange="restoreFromFile(this)">
    </label>
    <span class="hint">{last_txt}</span>
  </div>
</div>

<h2>Stände je Projekt</h2>
{tabelle}
<p class="hint" style="margin-top:8px">Sichern legt nur an, wenn sich seit dem
letzten Stand etwas geändert hat. Wiederherstellen ersetzt das Projekt durch
den gewählten Stand — der jetzige geht dabei verloren.</p>
<script>
function restoreFromFile(inp){{
  var f = inp.files[0]; if(!f) return;
  if(!confirm('Projekt aus "'+f.name+'" wiederherstellen? Ein bestehendes Projekt gleichen Namens wird ERSETZT.')){{inp.value='';return;}}
  fetch('/backup/restore-upload', {{method:'POST', body:f}})
    .then(function(r){{ location.href = r.ok ? '/backup?restore=ok' : '/backup?restore=err'; }})
    .catch(function(){{ location.href = '/backup?restore=err'; }});
}}
</script>
</body></html>"""


def _stand_text(backups: list[dict]) -> str:
    """'3 Stände · neuester 2026-08-30 14:25 · 222.4 MB' bzw. der Leerfall."""
    if not backups:
        return "noch kein Backup"
    neu = backups[0]
    mb = neu["size"] / 1024 / 1024
    zahl = "1 Stand" if len(backups) == 1 else f"{len(backups)} Stände"
    return f"{zahl} · neuester {_esc(_backup_time(neu['name']))} · {mb:.1f} MB"


def _flagged_section(p: str, flags: dict) -> str:
    """Übergroße, vom Sicherheits-Guard beiseitegelegte Dokumente pro Projekt mit
    Entscheidungs-Buttons (Aufnehmen/Ignorieren/Zurücksetzen). flags = {doc_key: info}."""
    if not flags:
        return ""
    _badge = {"approve": ('done', 'aufgenommen'), "ignore": ('', 'ignoriert')}

    def _btn(doc_key: str, decision: str, label: str, ok: bool) -> str:
        cls = "dec ok" if ok else "dec"
        return (f'<form method="post" action="/flagged/decide" class="{cls}">'
                f'<input type="hidden" name="project_id" value="{_esc(p)}">'
                f'<input type="hidden" name="doc_key" value="{_esc(doc_key)}">'
                f'<input type="hidden" name="decision" value="{decision}">'
                f'<button>{label}</button></form>')

    def _item(doc_key: str, info: dict) -> str:
        title = _esc(str(info.get("title") or doc_key))[:90]
        chars = info.get("chars") or 0
        chunks = info.get("est_chunks") or 0
        dec = info.get("decision", "open")
        meta = f'<span class="hint">{chars:,} Zeichen ≈ {chunks} Chunks · <code>{_esc(doc_key)}</code></span>'.replace(",", ".")
        if dec == "open":
            btns = _btn(doc_key, "approve", "Aufnehmen", True) + _btn(doc_key, "ignore", "Ignorieren", False)
            state = ""
        else:
            bcls, blabel = _badge.get(dec, ('', dec))
            state = f'<span class="badge {bcls}">{blabel}</span>'
            btns = _btn(doc_key, "open", "Zurücksetzen", False)
        return (f'<div class="flagrow"><div class="left">'
                f'<span class="nm" style="font-size:14px">{title}</span>{meta}{state}</div>'
                f'<div class="actions">{btns}</div></div>')
    rows = "\n".join(_item(k, v) for k, v in sorted(flags.items()))
    return (f'<div class="flagged"><div class="flaghead">⚠ Übergroße Dokumente — '
            f'nicht indexiert. „Aufnehmen" greift beim nächsten Ingest, „Ignorieren" '
            f'blendet dauerhaft aus.</div>{rows}</div>')


def index_html(items: list[tuple[str, bool]], status: dict | None = None,
               meta: dict | None = None, counts: dict | None = None,
               flagged: dict | None = None, graph_counts: dict | None = None) -> str:
    """Landing-Page für den Viewer-Root. items = (projekt_id, hat_graph_html).
    status = {projekt_id: ingest_status_dict} — zeigt Import-Fortschritt pro Karte.
    meta = {projekt_id: {"project_name": "..."}} — Anzeigenamen.
    counts = {projekt_id: anzahl_indexierter_dokumente} — pro Karte angezeigt.
    graph_counts = {projekt_id: (anzahl_entities, anzahl_kanten)} — pro Karte angezeigt.
    flagged = {projekt_id: {doc_key: info}} — übergroße Docs zur Nutzerentscheidung.
    Erklärt, was zu sehen ist und wie es weitergeht (statt rohem Dir-Listing)."""
    status = status or {}
    meta = meta or {}
    counts = counts or {}
    graph_counts = graph_counts or {}
    flagged = flagged or {}
    # Auto-Refresh auch bei 'paused', damit Fortsetzen/Fortschritt sichtbar wird.
    running = any(s.get("state") in ("running", "paused") for s in status.values())

    _CTL_ICON = {"pause": "pause", "resume": "resume", "stop": "stop"}

    def _ctl_form(e: str, action: str, label: str) -> str:
        return (f'<form method="post" action="/ingest/control" class="del" style="margin-right:6px">'
                f'<input type="hidden" name="project_id" value="{e}">'
                f'<input type="hidden" name="action" value="{action}">'
                f'{_icon_btn(_CTL_ICON[action], f"Ingest {label.lower()}")}</form>')

    def _row(p: str, has: bool) -> str:
        e = _esc(p)
        m = meta.get(p, {})
        display_name = m.get("project_name") or p
        st = status.get(p, {})
        state = st.get("state")
        live = state in ("running", "paused")
        # Laufender/pausierter Ingest bekommt eine eigene Fortschrittszeile unten;
        # abgeschlossene/fehlerhafte Zustände bleiben als kompaktes Inline-Badge.
        badge = "" if live else _status_badge(st)
        n = counts.get(p)
        # Kennzahlen der Karte: Dokumente + (falls Graph gerendert) Entitäten & Kanten.
        stats = []
        if n:
            stats.append(f'{n} Dokument{"" if n == 1 else "e"}')
        gc = graph_counts.get(p)
        if gc:
            ne, nk = gc
            stats.append(f'{ne:,} Entität{"" if ne == 1 else "en"}'.replace(",", "."))
            stats.append(f'{nk:,} Kante{"" if nk == 1 else "n"}'.replace(",", "."))
        docs = f'<span class="hint">{" · ".join(stats)}</span>' if stats else ""
        # Der Projektname selbst ist der Link zum Graphen (item 2).
        left = (f'<a class="nm open" href="./{e}/graph.html" title="Graph öffnen">{_esc(display_name)} →</a>'
                if has else
                f'<span class="nm">{_esc(display_name)}</span>'
                f'<span class="hint">noch nicht gerendert</span>') + docs + badge
        # Buttons: Erstellen/Aktualisieren (POST /refresh) + Umbenennen + Löschen
        refresh_form = (f'<form method="post" action="/refresh" class="del" style="margin-right:6px">'
                       f'<input type="hidden" name="project_id" value="{e}">'
                       f'{_icon_btn("refresh" if has else "create", "Graph aktualisieren" if has else "Graph erstellen")}</form>')
        rename_form = (f'<form method="post" action="/rename" class="del" style="margin-right:6px" '
                      f'onsubmit="const n=prompt(\'Neuer Anzeigename für &quot;{e}&quot;:\', \'{_esc(display_name)}\'); '
                      f'if(n===null) return false; document.querySelector(\'input[name=project_name]\').value=n; return true;">'
                      f'<input type="hidden" name="project_id" value="{e}">'
                      f'<input type="hidden" name="project_name" value="">'
                      f'{_icon_btn("rename", "Anzeigenamen ändern")}</form>')
        delete_form = (f'<form method="post" action="/delete" class="del danger" '
                      f"onsubmit=\"return confirm('Projekt &quot;{e}&quot; löschen? "
                      "Der Index wird entfernt, die Quelldokumente bleiben.')\">"
                      f'<input type="hidden" name="project_id" value="{e}">'
                      f'{_icon_btn("delete", "Projekt-Index löschen")}</form>')
        # Pause/Fortsetzen + Stop nur, solange ein Ingest läuft oder pausiert ist.
        if state == "running":
            control_forms = _ctl_form(e, "pause", "Pause") + _ctl_form(e, "stop", "Stop")
        elif state == "paused":
            control_forms = _ctl_form(e, "resume", "Fortsetzen") + _ctl_form(e, "stop", "Stop")
        else:
            control_forms = ""
        # Pause/Stop wandern zur Fortschrittszeile (thematisch bei der Ingest-Anzeige),
        # nicht zwischen die Verwaltungsbuttons oben.
        forms = refresh_form + rename_form + delete_form
        cls = "card" if has else "card todo"
        progress = _progress_row(st, control_forms) if live else ""
        flags = _flagged_section(p, flagged.get(p, {}))
        return (f'<div class="{cls}"><div class="cardhead"><div class="left">{left}</div>'
                f'<div class="actions">{forms}</div></div>{progress}{flags}</div>')

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
{_FAVICON_LINK}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{{--bg:#fafafa;--card:#fff;--border:#ececec;--accent:#3a5a9b;--ah:#2c4577;--text:#333;--muted:#666}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:"Source Sans 3","Source Sans Pro",Arial,sans-serif;letter-spacing:.15pt;font-size:15px;padding:32px;max-width:760px;margin:0 auto}}
  h1{{font-size:22px;font-weight:700;margin-bottom:4px}}
  .sub{{color:var(--muted);font-size:14px;margin-bottom:24px}}
  h2{{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 12px}}
{_HEADER_CSS}
  .grid{{display:grid;gap:10px;margin-bottom:28px}}
  .card{{display:flex;flex-direction:column;
    background:var(--card);border:1px solid var(--border);border-left:3px solid var(--accent);
    border-radius:10px;padding:14px 18px;transition:box-shadow .15s}}
  .cardhead{{display:flex;align-items:center;justify-content:space-between;gap:12px}}
  .actions{{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}}
  .card:hover{{box-shadow:0 3px 14px rgba(0,0,0,.08)}}
  .card.todo{{border-left-color:#bbb}}
  .prog{{display:flex;flex-direction:column;align-items:stretch;gap:8px;margin-top:12px;padding-top:12px;
    border-top:1px solid var(--border)}}
  .prog .bar{{width:100%;height:7px;background:var(--bg);border:1px solid var(--border);
    border-radius:20px;overflow:hidden}}
{_BTN_CSS}
  .prog .fill{{height:100%;background:var(--accent);border-radius:20px;transition:width .4s ease}}
  .prog .fill.paused{{background:#ffb300}}
  .prog-ctl{{display:flex;gap:6px;flex-shrink:0}}
  .left{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;min-width:0}}
  .nm{{font-weight:600;font-size:16px;color:var(--text);text-decoration:none}}
  a.nm.open:hover .go{{text-decoration:underline}}
  .go{{color:var(--accent);font-size:14px;font-weight:600;white-space:nowrap}}
  code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:1px 5px}}
  .steps ol{{margin:8px 0 0 18px}}
  .bk{{list-style:none;margin:0;font-size:13px;color:var(--muted)}}
  .bkrow{{display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--border)}}
  .bkrow:first-child{{border-top:none}}
  .bktime{{font-weight:600;color:var(--text);min-width:120px}}
  .bksize{{flex:1;color:var(--muted)}}
  .flagged{{margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}}
  .flaghead{{font-size:13px;color:#8a6d00;background:#fff8e1;border:1px solid #ffe082;
    border-radius:6px;padding:6px 10px;margin-bottom:8px}}
  .flagrow{{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:6px 0;border-top:1px solid var(--border)}}
  .flagrow:first-of-type{{border-top:none}}
  .flagrow .left{{flex-direction:column;align-items:flex-start;gap:2px}}
  .dec, .dec button{{background:none;border:1px solid var(--border);color:var(--muted);
    border-radius:6px;padding:5px 11px;font-size:13px;cursor:pointer;white-space:nowrap;
    transition:all .15s;margin:0;display:inline-block}}
  .dec:hover, .dec button:hover{{border-color:#888;color:var(--text);background:var(--bg)}}
  .dec.ok:hover, .dec.ok button:hover{{border-color:var(--accent);color:var(--accent);background:#e8edf7}}
{_CHAT_CSS}
</style></head><body>
{_header("index")}
<p class="sub">Knowledge Graphs aus deinen Dokumenten — pro Projekt ein Graph. Klick ein Projekt an, um den interaktiven Graphen zu öffnen.</p>
<h2>Projekte</h2>
<div class="grid">
{rows}
</div>
<h2>Wie es weitergeht</h2>
<div class="steps">
  Neue Dokumente in den Graphen bringen — dafür genügt ein Satz:
  {_WEITER_KASTEN}
</div>
</body></html>"""


# Was man in den Chat schreibt, was daraufhin laeuft, und was es erspart. Im
# Kasten steht der Satz — die Aufrufe stehen als Prosa daneben, weil man sie
# nicht selbst tippt (dieselbe Trennung wie in chat_kasten). Die Werkzeuge
# darunter beantworten "und wie ruft man das direkt auf?", nicht umgekehrt.
_HILFE_CHAT = [
    ("Was sagen die Bedingungen zur Befristung eines Anerkenntnisses?",
     'ruft <code>get_clause("bu-avb", "§ 5")</code> auf',
     "Der Wortlaut der Klausel, mit Dokumenttitel darüber. Passt die Nummer "
     "nicht, kommt die Liste aller Klauseln zurück und ich frage nach.",
     "Kein Blättern im Bedingungswerk, und der Text ist der echte — nicht "
     "das, was ein Modell dazu erinnert."),
    ("Was liegt zu der Sache überhaupt vor?",
     "sieht mit <code>list_projects()</code> nach und sucht dann mit "
     "<code>query(…, only_context=True)</code>",
     "Die Projekte mit Dokumentzahl, dann die Fundstellen zur Frage — roh, "
     "gelesen wird von mir im Chat.",
     "Ein Überblick über hunderte Dokumente, ohne eines davon zu öffnen."),
    ("Was weißt du über die ERGO Pensionskasse in dem Bestand?",
     'ruft <code>get_entity("future-fund", "ERGO Pensionskasse AG")</code> auf',
     "Die Entität mit ihren Nachbarn: welche Dokumente, welche Beziehungen, "
     "welche Vorgänge daran hängen.",
     "Zeigt Zusammenhänge, nach denen man nicht gesucht hätte — der Graph "
     "kennt sie schon."),
    ("Neue Post ist in Paperless, Tag future-fund.",
     "startet <code>ingest_paperless</code> und verfolgt den Lauf mit "
     "<code>ingest_status</code>",
     "Nur das Neue wird indexiert, erkannt am Inhalts-Hash. Der Lauf arbeitet "
     "im Hintergrund, der Status zeigt den echten Fortschritt.",
     "Kein Neuaufbau, kein manuelles Nachhalten, was schon drin war."),
    ("Leg mir daraus einen Fall an.",
     'ruft <code>new_case_from_docgraph("future-fund", '
     'gebiet="berufsunfaehigkeit")</code> bei case-assist auf',
     "case-assist holt die Dokumente hier im Volltext ab und baut daraus "
     "einen Fall: Fakten, Regelungen, Auffächerung gegen das Regelwerk.",
     "Dieselbe Textbasis in beiden Systemen, keine zweite Paperless-Runde — "
     "und die Dokumentschlüssel kommen als Beleg-Anker mit."),
]

_HILFE_WERKZEUGE = [
    ("Wortlaut einer Klausel — deterministisch, kein Modell",
     'get_clause("bu-avb", "§ 2")',
     "Exakter Text aus dem Klausel-Store, sofort und zitierfähig. Die "
     "Schreibweise ist tolerant: <code>§ 2</code>, <code>§2</code>, "
     "<code>2</code>, <code>Artikel 3</code>, <code>Ziffer 4</code>. Trifft "
     "nichts, kommt die Liste aller Klausel-IDs zurück — faktisch das "
     "Inhaltsverzeichnis. Nur in Projekten, die mit "
     "<code>regelwerk=True</code> indexiert wurden. Für Bedingungstext "
     "<b>immer</b> dieses Werkzeug, nie eine Suche."),
    ("Tatsache in der Akte suchen",
     'query("future-fund",\n'
     '      "Welche Schreiben des Versicherers liegen vor?",\n'
     '      only_context=True)',
     "Liefert die rohen Fundstellen, <b>keine fertige Antwort</b> — gelesen "
     "wird von dir oder Claude. <code>mode</code>: <code>local</code> "
     "(entitätsnah), <code>global</code> (übergreifende Muster), "
     "<code>hybrid</code> (Standard), <code>naive</code> (nur "
     "Textähnlichkeit). <code>only_context=False</code> nicht setzen: dann "
     "formuliert das lokale Modell auf der geteilten GPU und läuft in den "
     "Timeout."),
    ("Alles zu einer Person, Firma oder Sache",
     'get_entity("future-fund", "ERGO Pensionskasse AG", top_k=15)',
     "Die Entität mit ihren Nachbarn und Beziehungen. <code>top_k</code> ist "
     "die wirksame Stellschraube, nicht das Token-Budget — höher setzen "
     "kostet schnell zehntausende Zeichen."),
    ("Dokumente hineinlegen",
     'ingest_paperless("future-fund", tag="future-fund")\n'
     'ingest_paperless("bu-avb", tag="bu-bedingungen", regelwerk=True)\n'
     'ingest_status("future-fund")',
     "Delta-Indexierung: nur Neues und Geändertes, erkannt am Inhalts-Hash. "
     "Der Lauf arbeitet im Hintergrund — <code>ingest_status</code> zeigt die "
     "echten Zustände (nur <code>processed</code> heißt wirklich im Graph), "
     "<code>ingest_control</code> pausiert, setzt fort oder bricht ab."),
    ("Aufräumen",
     'delete_documents("future-fund", only_failed=True)',
     "Einzelne Dokumente aus dem Index werfen (Chunks, Entitäten, Vektoren) — "
     "etwa Dubletten oder Dokumente, die reproduzierbar scheitern. Die "
     "Quellen in Paperless bleiben unberührt."),
    ("Volltext herausholen",
     'GET /&lt;project_id&gt;/export',
     "Die indexierten Dokumente im Volltext, je Dokument mit "
     "Dokumentschlüssel, Inhalts-Hash und Fundstelle. Damit startet "
     "case-assist Fälle aus einem Projekt, statt die Dokumente ein zweites "
     "Mal aus Paperless zu ziehen."),
]

_HILFE_LOOP = [
    ("case-assist", "list_cases()", "Fall wählen"),
    ("case-assist", "open_questions(fall_id)",
     "die offenen Tatsachenlücken als JSON"),
    ("Mensch", "—", "Frage schärfen — die Frage aus dem Referenzgraphen kennt "
     "weder Namen noch Zeiträume dieses Falls"),
    ("doc-graph", "get_clause(…) oder query(…)",
     "Klauselwortlaut bzw. Fundstellen"),
    ("Claude + Mensch", "—",
     "auswerten: was steht wörtlich da, was ist gefolgert, was bleibt offen"),
    ("Claude", "—", "Fakt-JSON vorschlagen, <b>zur Bestätigung</b>"),
    ("case-assist", "amend_case(fall_id, antwort_json, beleg)",
     "Nachtrag plus frische Auffächerung"),
    ("—", "zurück zu 2", "leere Liste = alle Tatsachenlücken gedeckt"),
]


def hilfe_html() -> str:
    """Was doc-graph für ein Projekt kann, mit aufrufbaren Beispielen — und
    der Loop, in dem es mit case-assist zusammenspielt. Dieselbe Seite liegt
    unter case-assist.lan/hilfe; die volle Referenz steht in luecken-loop.md."""
    chat = "".join(
        '<div class="hcard">' + chat_kasten(frage)
        + f"<p><b>Claude:</b> {laeuft}<br>"
        f"<b>Zurück kommt:</b> {ergebnis}<br>"
        f"<b>Erspart:</b> {spart}</p></div>"
        for frage, laeuft, ergebnis, spart in _HILFE_CHAT)
    karten = "".join(
        f'<div class="hcard"><b>{t}</b>' + chat_kasten(a) + f"<p>{x}</p></div>"
        for t, a, x in _HILFE_WERKZEUGE)
    zeilen = "".join(
        f"<tr><td>{i}</td><td>{wer}</td><td><code>{_esc(aufruf)}</code></td>"
        f"<td>{ergebnis}</td></tr>"
        for i, (wer, aufruf, ergebnis) in enumerate(_HILFE_LOOP, 1))
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>doc-graph · Hilfe</title>
{_FAVICON_LINK}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{{--bg:#fafafa;--card:#fff;--border:#ececec;--accent:#3a5a9b;--text:#333;--muted:#666}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:"Source Sans 3","Source Sans Pro",Arial,sans-serif;
    letter-spacing:.15pt;font-size:15px;line-height:1.6;padding:32px;max-width:760px;margin:0 auto}}
  h1{{font-size:22px;margin-bottom:4px}}
  h2{{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
    color:var(--muted);margin:28px 0 12px}}
  p.sub{{color:var(--muted);font-size:14px;margin-bottom:8px}}
  a{{color:var(--accent)}}
{_HEADER_CSS}
  .hcard{{background:var(--card);border:1px solid var(--border);
    border-left:3px solid var(--accent);border-radius:10px;padding:14px 18px;margin-bottom:10px}}
  .hcard p{{color:var(--muted);font-size:14px;margin-top:8px}}
  pre{{background:var(--bg);border:1px solid var(--border);border-radius:6px;
    padding:8px 10px;margin-top:8px;font-size:13.5px;overflow-x:auto}}
  code{{background:var(--bg);border-radius:4px;padding:1px 4px;font-size:13.5px}}
  table{{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);
    border:1px solid var(--border);border-radius:10px;overflow:hidden}}
  th,td{{text-align:left;padding:8px 12px;border-top:1px solid var(--border);vertical-align:top}}
  th{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);border-top:none}}
  td code{{white-space:nowrap}}
{_CHAT_CSS}
</style></head><body>
{_header("hilfe")}
<h1>Hilfe</h1>
<p class="sub">Zwei Systeme, klare Rollen: <b>doc-graph</b> weiß, <i>wo</i> eine
Tatsache steht. <b>case-assist</b> weiß deterministisch, <i>welche</i> Tatsache
einem Fall noch fehlt. Die Wertung trifft in beiden Fällen der Mensch.</p>

<h2>So läuft das im Gespräch</h2>
<p class="sub">Der normale Weg ist der Chat in Claude Code: im Kasten steht,
was du schreibst — mehr nicht. Was daraufhin läuft, steht darunter, damit
nachvollziehbar bleibt, was passiert ist.</p>
{chat}

<h2>Was doc-graph für ein Projekt kann</h2>
<p class="sub">Diese Aufrufe tippst du selbst, wenn du genau ein Ergebnis
willst — im Gespräch reicht sonst der Satz oben. <code>bu-avb</code> ist hier
ein Bedingungswerk, <code>future-fund</code> eine Akte.</p>
{karten}

<h2>Eine Lücke in einem Fall schließen</h2>
<p class="sub">Der Loop zwischen beiden Systemen — auslösen mit
<code>/fall-luecke</code> oder formlos „Lücke schließen".</p>
<table><tr><th>#</th><th>Wer</th><th>Aufruf</th><th>Ergebnis</th></tr>
{zeilen}</table>

<div class="hcard" style="margin-top:10px"><b>Der häufigste Stolperstein</b>
<p>Ein Merkmal gilt erst als gedeckt, wenn ein Fakt es in
<code>einordnung</code> trägt. Trifft die <code>einordnung</code> die
<code>merkmal_id</code> aus Schritt 2 nicht <i>exakt</i>, bleibt die Lücke
offen — obwohl der Nachtrag durchging.</p></div>

<div class="hcard"><b>Findet doc-graph nichts, ist das das Ergebnis</b>
<p>Die Lücke bleibt dann offen. Eine erfundene Tatsache ist schlimmer als eine
fehlende.</p></div>

<h2>Warum das schneller ist</h2>
<div class="hcard"><p>Drei Dinge verschieben sich, und nur diese drei: Das
<b>Suchen</b> macht ein Index in Sekunden statt du in Ordnern. Der
<b>Wortlaut</b> kommt exakt aus dem Klausel-Store, statt paraphrasiert aus
einem Modell. Und die <b>Vollständigkeit</b> — welche Norm hängt noch an
welcher fehlenden Tatsache — rechnet case-assist deterministisch gegen den
Referenzgraphen.</p>
<p>Was sich <i>nicht</i> verschiebt: die Wertung. Unbestimmte Rechtsbegriffe
bleiben leer, auch wenn ein Dokument sie behauptet; ein Nachtrag wird erst
nach deiner Bestätigung geschrieben. Das System kommt bis an die Entscheidung
heran und hört dort auf.</p></div>

<p class="sub" style="margin-top:20px">Der vollständige Ablauf mit allen
Tool-Signaturen, Wächtern und einem Beispiel-Durchgang steht im case-assist-Repo
in <code>luecken-loop.md</code>.</p>
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


def graph_html(title: str, projects: list[str] | None = None,
               current: str = "", names: dict[str, str] | None = None) -> str:
    """Baut die eigenständige vis-network-Shell. Die Knoten/Kanten sind NICHT
    eingebettet — der Browser lädt sie live über den relativen Endpoint
    `nodes?…` (serverseitig auf max. Knotenzahl gedeckelt, Priorisierung nach
    Knotengrad). Filter/Fokus/Suche sind je ein Server-Roundtrip.
    names = {project_id: display_name} für Dropdown und Refresh-Button."""
    proj_select = _project_select(projects, current, names)
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{_FAVICON_LINK}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="{_VIS_CDN}"></script>
<style>
  :root{{--bg:#fafafa;--card:#fff;--border:#ececec;--accent:#3a5a9b;--ah:#2c4577;--text:#333;--muted:#666}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:"Source Sans 3","Source Sans Pro",Arial,sans-serif;letter-spacing:.15pt;font-size:15px;padding:20px}}
  h1{{font-size:22px;font-weight:700;margin-bottom:4px}}
  .sub{{color:var(--muted);font-size:14px;margin-bottom:12px}}
  .bar{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:10px}}
  .muted{{color:var(--muted);font-size:13px}}
  #netwrap{{position:relative}}
  #net{{height:78vh;background:var(--card);border:1px solid var(--border);border-radius:10px}}
  #info{{position:absolute;left:12px;right:12px;bottom:12px;max-height:38%;overflow:auto;
    background:var(--card);border:1px solid var(--border);border-radius:8px;
    padding:10px 13px;box-shadow:0 3px 16px rgba(0,0,0,.10);font-size:14px;line-height:1.5;
    display:none;pointer-events:none}}
  #info .hd{{display:flex;align-items:center;gap:8px;margin-bottom:5px;flex-wrap:wrap}}
  #info .chip{{color:#fff;font-size:12px;font-weight:600;padding:2px 8px;border-radius:20px}}
  #info .nm{{font-weight:700;font-size:15px}}
  #info .d{{color:var(--text);white-space:pre-wrap;word-break:break-word}}
  #leg{{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}}
  #leg span{{font-size:13px;display:inline-flex;align-items:center;gap:5px;cursor:pointer}}
  .dot{{width:11px;height:11px;border-radius:50%;display:inline-block}}
</style></head><body>
<h1>{title}</h1>
<p class="sub"><span id="cnt">lädt…</span> &nbsp;·&nbsp; ziehen/scrollen zum Navigieren, Knoten/Kante anklicken für Details, Legende anklicken zum Filtern</p>
<div class="bar">
  <a href="../" style="text-decoration:none;background:none;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:5px 11px;font-size:13px;white-space:nowrap;transition:all .15s;margin-right:6px" title="Zurück zur Projektübersicht">← Übersicht</a>
  {proj_select}
  <form method="post" action="../refresh" style="margin:0;display:inline;margin-right:6px">
    <input type="hidden" name="project_id" value="{current}">
    <button type="submit" style="background:none;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:5px 11px;font-size:13px;cursor:pointer;white-space:nowrap;transition:all .15s" title="Graph aus .graphml neu rendern">Aktualisieren</button>
  </form>
  <form method="post" action="../rename" style="margin:0;display:inline;margin-right:6px" onsubmit="const n=prompt('Neuer Anzeigename:'); if(n===null) return false; document.querySelector('input[name=project_name]').value=n; return true;">
    <input type="hidden" name="project_id" value="{current}">
    <input type="hidden" name="project_name" value="">
    <button type="submit" style="background:none;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:5px 11px;font-size:13px;cursor:pointer;white-space:nowrap;transition:all .15s" title="Anzeigenamen ändern">Umbenennen</button>
  </form>
  <label class="muted"><input type="checkbox" id="phys" checked onchange="net&&net.setOptions({{physics:{{enabled:this.checked}}}})"> Physik</label>
  <label class="muted" title="Knoten anklicken, dann anhaken: zeigt nur dessen Nachbarschaft (Doppelklick setzt Anker um)"><input type="checkbox" id="focus" onchange="setFocus()"> nur Verbundene</label>
  <label class="muted" title="Nachbarschafts-Tiefe in Hops">Distanz <input type="number" id="depth" value="1" min="1" style="width:3em" onchange="fetchGraph()"></label>
  <span class="muted">Typ-Filter: Legende anklicken</span>
  <button type="button" onclick="toggleAll()" style="background:none;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:5px 11px;font-size:13px;cursor:pointer;white-space:nowrap;transition:all .15s" title="Alle Typen ein- oder ausblenden">alle an/aus</button>
  <input id="q" oninput="onSearch()" placeholder="Knoten suchen…" title="Sucht im ganzen Graph (Server); Treffer werden rot hervorgehoben und angefahren" style="font:inherit;font-size:13px;padding:5px 9px;border:1px solid var(--border);border-radius:6px;background:var(--card);color:var(--text);width:11em">
</div>
<div id="netwrap"><div id="net"></div><div id="info"></div></div>
<div id="leg"></div>
<script>
  const $=id=>document.getElementById(id);
  const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
  // Knoten/Kanten werden NICHT eingebettet, sondern live vom Server geladen und
  // dort auf die Maximalzahl gedeckelt. data hält nur das aktuelle Subset.
  let data={{nodes:[],edges:[]}};
  let TYPES=[];                         // vollständige Typliste vom Server (stabile Legende)
  let META={{total:0,shown:0,capped:false}};
  const COL={{}};                       // Typ -> Farbe (aus TYPES)
  const HIDE=new Set();                 // ausgeblendete Typen (Legende)
  let net=null, nodesDS=null, NODEMAP=new Map(), SEL=null, FOCUS=null;  // SEL=angeklickt, FOCUS=Fokus-Anker
  let searchTimer=null;

  function showInfo(header, group, body){{
    const chip=group?`<span class="chip" style="background:${{COL[group]||'#636363'}}">${{esc(group)}}</span>`:'';
    const d=body?`<div class="d">${{esc(body)}}</div>`:'';
    $('info').innerHTML=`<div class="hd">${{chip}}<span class="nm">${{esc(header)}}</span></div>${{d}}`;
    $('info').style.display='block';
  }}

  // Zentraler Server-Roundtrip: baut die Query aus dem aktuellen UI-Zustand
  // (Fokus/Distanz/Suche/ausgeblendete Typen) und lädt das gedeckelte Subset.
  async function fetchGraph(){{
    const p=new URLSearchParams();
    if(FOCUS&&$('focus').checked){{p.set('focus',FOCUS);p.set('depth',Math.max(1,+$('depth').value||1));}}
    const q=($('q').value||'').trim();
    if(q) p.set('q',q);
    if(HIDE.size) p.set('hide',[...HIDE].join(','));
    $('cnt').textContent='lädt…';
    let res;
    try{{ res=await fetch('nodes?'+p.toString()); }}
    catch(e){{ $('cnt').textContent='Netzwerkfehler'; return; }}
    if(!res.ok){{ $('cnt').textContent='Fehler '+res.status; return; }}
    const j=await res.json();
    data={{nodes:j.nodes||[],edges:j.edges||[]}};
    TYPES=j.types||[];
    META={{total:j.total||0,shown:j.shown||0,capped:!!j.capped}};
    NODEMAP=new Map(data.nodes.map(n=>[n.id,n]));
    Object.keys(COL).forEach(k=>delete COL[k]);
    TYPES.forEach(t=>{{COL[t.type]=t.color;}});
    renderLeg();
    build();
  }}

  function build(){{  // rendert das bereits geladene (≤ Limit) Subset
    const nodes=data.nodes.map(n=>({{id:n.id,label:n.label,color:n.color,
      shape:'dot',size:14,font:{{size:13,color:'#333'}}}}));
    const ok=new Set(data.nodes.map(n=>n.id));
    const edges=data.edges.filter(e=>ok.has(e.from)&&ok.has(e.to)).map(e=>({{
      from:e.from,to:e.to,desc:e.desc,arrows:'to',
      smooth:{{type:'continuous'}},color:{{color:'#ccc'}}}}));
    const cap=META.capped?` von ${{META.total}}`:'';
    $('cnt').textContent=`${{nodes.length}}${{cap}} Knoten · ${{edges.length}} Kanten`;
    nodesDS=new vis.DataSet(nodes);
    const edgesDS=new vis.DataSet(edges);
    net=new vis.Network($('net'),{{nodes:nodesDS,edges:edgesDS}},{{
      physics:{{enabled:$('phys').checked,stabilization:{{iterations:150}},barnesHut:{{gravitationalConstant:-8000,springLength:130}}}},
      interaction:{{hover:true}}}});
    net.on('click',p=>{{
      if(p.nodes.length){{SEL=p.nodes[0];const src=NODEMAP.get(SEL);if(src)showInfo(src.label,src.group,src.desc);}}
      else if(p.edges.length){{const e=edgesDS.get(p.edges[0]);const u=nodesDS.get(e.from),v=nodesDS.get(e.to);
        showInfo((u?u.label:e.from)+' → '+(v?v.label:e.to),'',e.desc);}}
      else{{SEL=null;$('info').style.display='none';}}
    }});
    net.on('doubleClick',p=>{{  // Doppelklick: Anker setzen und Nachbarschaft nachladen
      if(p.nodes.length){{FOCUS=SEL=p.nodes[0];
        const src=NODEMAP.get(FOCUS);if(src)showInfo(src.label,src.group,src.desc);
        if(!$('focus').checked)$('focus').checked=true;
        fetchGraph();}}
    }});
    highlight();  // aktive Suche im neuen Subset markieren
  }}
  function setFocus(){{FOCUS=$('focus').checked?SEL:null;fetchGraph();}}  // Anker = aktuelle Auswahl
  function onSearch(){{clearTimeout(searchTimer);searchTimer=setTimeout(fetchGraph,300);}}  // debounced Server-Suche

  function highlight(){{  // Treffer rot hervorheben + anfahren, Rest dimmen (im geladenen Subset)
    if(!net||!nodesDS) return;
    const q=($('q').value||'').trim().toLowerCase();
    const upd=[]; let first=null;
    nodesDS.getIds().forEach(id=>{{
      const src=NODEMAP.get(id);
      const base=(src&&src.color)||'#636363';
      const hit=q&&src&&String(src.label||'').toLowerCase().includes(q);
      if(hit&&first===null) first=id;
      upd.push({{id,opacity:(!q||hit)?1:0.15,borderWidth:hit?3:1,
        color:hit?{{border:'#dd3333',background:base}}:base}});
    }});
    nodesDS.update(upd);
    if(first!==null){{net.selectNodes([first]);net.focus(first,{{scale:1.2,animation:true}});
      const src=NODEMAP.get(first);if(src)showInfo(src.label,src.group,src.desc);}}
  }}

  function toggleType(t){{HIDE.has(t)?HIDE.delete(t):HIDE.add(t);renderLeg();fetchGraph();}}
  function toggleAll(){{  // mind. ein Typ sichtbar -> alle aus, sonst alle ein
    if(HIDE.size<Object.keys(COL).length) Object.keys(COL).forEach(t=>HIDE.add(t));
    else HIDE.clear();
    renderLeg();fetchGraph();
  }}
  function renderLeg(){{
    $('leg').innerHTML=TYPES.map(t=>
      `<span onclick="toggleType('${{esc(t.type)}}')" style="opacity:${{HIDE.has(t.type)?0.35:1}}" title="${{HIDE.has(t.type)?'einblenden':'ausblenden'}} (${{t.count}})"><i class="dot" style="background:${{t.color}}"></i>${{esc(t.type)}}</span>`).join('');
  }}

  fetchGraph();  // initiales Laden (Top-Knoten nach Grad, gedeckelt)
</script></body></html>"""
