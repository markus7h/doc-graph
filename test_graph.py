"""Selbsttest für graphview — reine stdlib, kein Container nötig.
Lauf: python test_graph.py"""

import re

from graphview import color_for, graph_html, graph_subset, index_html, node_dict


def test_graph_shell_fetches_live():
    # graph.html ist jetzt eine Shell: KEIN eingebetteter Knoten-Payload, sondern
    # ein relativer fetch auf den serverseitig gedeckelten /nodes-Endpoint.
    html = graph_html("KG: Test", projects=["a", "b"], current="a")
    assert "KG: Test" in html
    assert "const data = {" not in html and "const data ={" not in html, "Knoten dürfen nicht mehr eingebettet sein"
    assert "fetch('nodes?" in html, "Viewer lädt Knoten nicht live per fetch"
    # nur die zwei echten Script-Tags: CDN-Einbindung + eigener Viewer-Block
    assert html.count("</script>") == 2


def test_node_dict():
    nd = node_dict('"Müller"', {"entity_type": "person", "description": "x" * 500})
    assert nd["label"] == "Müller"            # umgebende Quotes weg
    assert nd["group"] == "person"
    assert nd["color"] == color_for("person")
    assert len(nd["desc"]) == 400             # description gekappt


def _star():
    """Sterngraph: hub (Grad 6) + 6 leafN (Grad 1) + 2 iso (Grad 0)."""
    nodes = {"hub": {"id": "hub", "label": "hub", "group": "hub", "color": "#000", "desc": ""}}
    for i in range(6):
        nodes[f"leaf{i}"] = {"id": f"leaf{i}", "label": f"leaf{i}", "group": "leaf", "color": "#111", "desc": ""}
    for i in range(2):
        nodes[f"iso{i}"] = {"id": f"iso{i}", "label": f"iso{i}", "group": "iso", "color": "#222", "desc": ""}
    edges = [{"from": "hub", "to": f"leaf{i}", "desc": ""} for i in range(6)]
    adj = {n: set() for n in nodes}
    for e in edges:
        adj[e["from"]].add(e["to"])
        adj[e["to"]].add(e["from"])
    degree = {n: len(a) for n, a in adj.items()}
    return nodes, edges, adj, degree


def test_graph_subset_cap_by_degree():
    nodes, edges, adj, degree = _star()
    sub = graph_subset(nodes, edges, adj, degree, limit=3)
    assert sub["total"] == 9 and sub["shown"] == 3 and sub["capped"] is True
    ids = {n["id"] for n in sub["nodes"]}
    assert "hub" in ids, "höchster Grad muss beim Deckeln überleben"
    # types immer über den GANZEN Graph (stabile Legende), unabhängig vom Cap
    tset = {t["type"]: t["count"] for t in sub["types"]}
    assert tset == {"hub": 1, "leaf": 6, "iso": 2}


def test_graph_subset_focus_and_hide_and_search():
    nodes, edges, adj, degree = _star()
    # Fokus hub, 1 Hop -> hub + 6 leaves, iso bleiben draußen
    foc = graph_subset(nodes, edges, adj, degree, limit=2500, focus="hub", depth=1)
    assert foc["shown"] == 7 and len(foc["edges"]) == 6
    assert not any(n["id"].startswith("iso") for n in foc["nodes"])
    # Typ ausblenden
    hid = graph_subset(nodes, edges, adj, degree, limit=2500, hide={"leaf"})
    assert {n["group"] for n in hid["nodes"]} == {"hub", "iso"}
    # Volltextsuche zieht Treffer + direkte Nachbarn (leafN -> hub)
    ser = graph_subset(nodes, edges, adj, degree, limit=2500, q="leaf")
    ids = {n["id"] for n in ser["nodes"]}
    assert "hub" in ids and all(f"leaf{i}" in ids for i in range(6))
    assert not any(i in ids for i in ("iso0", "iso1"))


def test_color_deterministic():
    assert color_for("person") == "#e6550d"
    assert color_for("PERSON") == "#e6550d"          # case-insensitiv
    assert color_for("foo") == color_for("foo")      # stabil
    assert re.fullmatch(r"#[0-9a-f]{6}", color_for("foo"))


def test_index_html():
    html = index_html([("fehmarn", True), ("bö<se", False)])
    assert 'href="./fehmarn/graph.html"' in html          # gerendertes Projekt verlinkt
    assert "noch nicht gerendert" in html                  # todo-Projekt ohne Link
    assert 'bö&lt;se' in html and 'href="./bö' not in html  # Name escaped, kein Link
    # Löschen: jede Karte hat ein Delete-Form mit escaptem project_id
    assert html.count('action="/delete"') == 2
    assert '<input type="hidden" name="project_id" value="fehmarn">' in html
    assert '<input type="hidden" name="project_id" value="bö&lt;se">' in html
    assert "Noch keine Projekte" in index_html([])         # Leerzustand


def test_index_status():
    # Import-Status: laufender Ingest -> Badge (Fortschritt done/total) + Auto-Refresh
    st = {"fehmarn": {"state": "running", "done": 7, "total": 28, "at": "2026-07-12 07:00:00"}}
    h = index_html([("fehmarn", True)], st)
    assert "Ingest läuft" in h and "7/28" in h
    assert 'http-equiv="refresh"' in h                      # pollt nur bei running
    # running -> Pause- und Stop-Button (aber kein Fortsetzen)
    assert 'value="pause"' in h and 'value="stop"' in h and 'value="resume"' not in h
    # paused -> Auto-Refresh bleibt, Badge + Fortsetzen/Stop statt Pause
    hp = index_html([("fehmarn", True)], {"fehmarn": {"state": "paused", "done": 7, "total": 28}})
    assert "pausiert" in hp and 'http-equiv="refresh"' in hp
    assert 'value="resume"' in hp and 'value="stop"' in hp and 'value="pause"' not in hp
    # stopped -> Badge, kein Auto-Refresh, keine Steuer-Buttons mehr
    hs = index_html([("fehmarn", True)], {"fehmarn": {"state": "stopped", "done": 7, "total": 28}})
    assert "abgebrochen" in hs and 'http-equiv="refresh"' not in hs
    assert 'action="/ingest/control"' not in hs
    # done/error: kein Auto-Refresh, aber Badge sichtbar
    h2 = index_html([("fehmarn", True)], {"fehmarn": {"state": "done", "new": 0, "updated": 28}})
    assert "zuletzt indexiert" in h2 and 'http-equiv="refresh"' not in h2
    assert 'http-equiv="refresh"' not in index_html([("fehmarn", True)])  # ohne Status


def test_index_graph_counts():
    # Entitäten/Kanten-Kennzahlen erscheinen zusätzlich zur Dokumentzahl.
    html = index_html([("fehmarn", True)], counts={"fehmarn": 12},
                      graph_counts={"fehmarn": (2500, 4200)})
    assert "12 Dokumente" in html
    assert "2.500 Entitäten" in html and "4.200 Kanten" in html


if __name__ == "__main__":
    test_graph_shell_fetches_live()
    test_node_dict()
    test_graph_subset_cap_by_degree()
    test_graph_subset_focus_and_hide_and_search()
    test_color_deterministic()
    test_index_html()
    test_index_status()
    test_index_graph_counts()
    print("ok")
