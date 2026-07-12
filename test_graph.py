"""Selbsttest für graphview.graph_html — reine stdlib, kein Container nötig.
Lauf: python test_graph.py"""

import json
import re

from graphview import color_for, graph_html, index_html


def test_embedding_and_escaping():
    nodes = [
        {"id": "Müller", "label": "Müller", "group": "person", "color": color_for("person")},
        # bösartig: enthält </script>, darf das eingebettete Script nicht sprengen
        {"id": "x", "label": "</script><b>hack</b>", "group": "", "color": color_for("")},
    ]
    edges = [{"from": "Müller", "to": "x", "title": "kennt <& >"}]
    html = graph_html(nodes, edges, "Test")

    # nur die zwei echten Template-Tags (CDN + Daten); das </script> aus den
    # Daten muss escaped sein, sonst wären es drei
    assert html.count("</script>") == 2, "eingebettetes </script> nicht escaped"
    assert "\\u003c/script>" in html, "bösartiges </script> nicht als \\u003c escaped"
    # das eingebettete JSON ist wieder parsebar (< als < zurückübersetzen)
    m = re.search(r"const data = (\{.*\});", html)
    assert m, "data-Objekt nicht gefunden"
    data = json.loads(m.group(1).replace("\\u003c", "<"))
    assert data["nodes"][0]["id"] == "Müller"
    assert data["nodes"][1]["label"] == "</script><b>hack</b>"
    assert data["edges"][0]["title"] == "kennt <& >"


def test_color_deterministic():
    assert color_for("person") == "#e6550d"
    assert color_for("PERSON") == "#e6550d"          # case-insensitiv
    assert color_for("foo") == color_for("foo")      # stabil
    assert re.fullmatch(r"#[0-9a-f]{6}", color_for("foo"))


def test_index_html():
    html = index_html([("fehmarn", True), ("bö<se", False)])
    assert 'href="./fehmarn/graph.html"' in html          # gerendertes Projekt verlinkt
    assert 'graph_view("bö&lt;se")' in html                # todo-Projekt: Name escaped, kein Link
    assert 'href="./bö' not in html
    # Löschen: jede Karte hat ein Delete-Form mit escaptem Projektnamen
    assert html.count('action="/delete"') == 2
    assert '<input type="hidden" name="project" value="fehmarn">' in html
    assert '<input type="hidden" name="project" value="bö&lt;se">' in html
    assert "Noch keine Projekte" in index_html([])         # Leerzustand


def test_index_status():
    # Import-Status: laufender Ingest -> Badge + Auto-Refresh
    st = {"fehmarn": {"state": "running", "pending": 28, "at": "2026-07-12 07:00:00"}}
    h = index_html([("fehmarn", True)], st)
    assert "Ingest läuft" in h and "28" in h
    assert 'http-equiv="refresh"' in h                      # pollt nur bei running
    # done/error: kein Auto-Refresh, aber Badge sichtbar
    h2 = index_html([("fehmarn", True)], {"fehmarn": {"state": "done", "new": 0, "updated": 28}})
    assert "zuletzt indexiert" in h2 and 'http-equiv="refresh"' not in h2
    assert 'http-equiv="refresh"' not in index_html([("fehmarn", True)])  # ohne Status


if __name__ == "__main__":
    test_embedding_and_escaping()
    test_color_deterministic()
    test_index_html()
    test_index_status()
    print("ok")
