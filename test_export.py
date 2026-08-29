"""Selbsttest fuer den Volltext-Export. Lauf: python test_export.py

Hintergrund: case-assist startet Faelle aus Dokumenten, die hier schon
indexiert sind — statt sie ein zweites Mal aus Paperless zu ziehen. Geprueft
wird die Datenaufbereitung (server.export_daten): dass Dokumentschluessel,
Titel, Inhalts-Hash und Volltext zusammenpassen und ein unbekanntes Projekt
sauber scheitert. Die HTTP-Huelle drumherum ist drei Zeilen und wird nicht
mitgetestet.

Stub-Mechanik wie in test_fundstellen.py.
"""
import json
import sys
import tempfile
import types
from pathlib import Path


def _install_stubs():
    def _mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    class _FastMCP:
        def __init__(self, *a, **k): pass
        def tool(self, *a, **k):
            def deco(fn): return fn
            return deco
        def run(self, *a, **k): pass

    fastmcp = _mod("mcp.server.fastmcp")
    fastmcp.FastMCP = _FastMCP
    _mod("mcp.server").fastmcp = fastmcp
    _mod("mcp")

    lr = _mod("lightrag")
    lr.LightRAG = object
    lr.QueryParam = object
    llm = _mod("lightrag.llm.openai")
    llm.openai_complete_if_cache = lambda *a, **k: None
    _mod("lightrag.llm")
    utils = _mod("lightrag.utils")
    utils.EmbeddingFunc = object
    shared = _mod("lightrag.kg.shared_storage")
    shared.initialize_pipeline_status = lambda *a, **k: None
    shared.get_namespace_data = lambda *a, **k: {}
    _mod("lightrag.kg")


_install_stubs()
import server            # noqa: E402

fehler = 0


def pruefe(label, ist, soll):
    global fehler
    if ist == soll:
        print(f"  {label}: OK")
    else:
        print(f"  {label}: FEHLER\n     ist  {ist!r}\n     soll {soll!r}")
        fehler = 1


DOK = ("Dokument: Nachtrag zum Versicherungsausweis\n"
       "Datum: 2023-12-01\n"
       "Korrespondent: ERGO Pensionskasse AG\n\n"
       "Sehr geehrte Damen und Herren ...")

tmp = Path(tempfile.mkdtemp(prefix="doc-graph-export-"))
server.PROJECTS_DIR = tmp
projekt = tmp / "akte"
projekt.mkdir()
(projekt / "kv_store_full_docs.json").write_text(json.dumps({
    "paperless:7": {"content": DOK,
                    "file_path": "Nachtrag zum Versicherungsausweis, "
                                 "2023-12-01, ERGO Pensionskasse AG"},
    # ohne Header und ohne Manifest-Eintrag: darf nicht durchfallen
    "file:notiz.txt": {"content": "formlose Notiz", "file_path": ""},
}))
(projekt / "ingest_manifest.json").write_text(
    json.dumps({"paperless:7": "c9bec9524c24b1e6"}))

daten = server.export_daten("akte")
doks = {d["doc_key"]: d for d in daten["dokumente"]}

pruefe("beide Dokumente", sorted(doks), ["file:notiz.txt", "paperless:7"])
pruefe("Titel aus dem Header", doks["paperless:7"]["titel"],
       "Nachtrag zum Versicherungsausweis")
pruefe("Volltext unveraendert", doks["paperless:7"]["text"], DOK)
# Der Hash ist der Anker, an dem ein Beleg spaeter haengt — faellt er weg,
# ist der Export als Quelle wertlos.
pruefe("Inhalts-Hash aus dem Manifest", doks["paperless:7"]["hash"],
       "c9bec9524c24b1e6")
pruefe("Fundstelle mitgegeben", doks["paperless:7"]["fundstelle"],
       "Nachtrag zum Versicherungsausweis, 2023-12-01, ERGO Pensionskasse AG")
# Ohne Header faellt der Titel auf den Schluessel zurueck, statt leer zu sein:
# case-assist baut daraus seine Dokumentgrenzen.
pruefe("Titel-Rueckfall auf den Schluessel", doks["file:notiz.txt"]["titel"],
       "file:notiz.txt")
pruefe("fehlender Manifest-Eintrag = leerer Hash",
       doks["file:notiz.txt"]["hash"], "")

for label, projekt_id, erwartet in (
        ("unbekanntes Projekt", "gibtsnicht", FileNotFoundError),
        ("Traversal abgewehrt", "../etc", ValueError)):
    try:
        server.export_daten(projekt_id)
        pruefe(label, "kein Fehler", erwartet.__name__)
    except erwartet:
        pruefe(label, erwartet.__name__, erwartet.__name__)

print("test_export: alle OK" if not fehler else "test_export: FEHLGESCHLAGEN")
raise SystemExit(fehler)
