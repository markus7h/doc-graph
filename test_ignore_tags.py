"""Selbsttest fuer INGEST_IGNORE_TAGS. Lauf: python test_ignore_tags.py

Hintergrund: der Metadaten-Header geht in _hash, und _entscheide_doc ueberspringt
ein Dokument nur bei gleichem Hash. paperless-ai schreibt seinen Bookkeeping-Tag
in jedes verarbeitete Dokument zurueck — ohne Filter gilt damit jedes davon als
geaendert und laeuft erneut durch die LLM-Extraktion (gemessen 2026-08-27:
230 von 263 Dokumenten in future-fund).

Stub-Mechanik wie in test_embed_cap.py.
"""
import sys
import types


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
import server  # noqa: E402

fehler = 0


def pruefe(label, ist, soll):
    global fehler
    if ist == soll:
        print(f"  {label}: OK")
    else:
        print(f"  {label}: FEHLER\n     ist  {ist!r}\n     soll {soll!r}")
        fehler = 1


DOC = {"title": "Brief", "created": "2026-07-07T00:00:00+02:00", "content": "Inhalt"}

def gefiltert(namen):
    """Nachbau der Filterzeile aus dem Paperless-Ingest-Pfad."""
    return [t for t in namen if t not in server.INGEST_IGNORE_TAGS]

pruefe("Default enthaelt paperless-ai", "paperless-ai" in server.INGEST_IGNORE_TAGS, True)
pruefe("Fachliche Tags bleiben", gefiltert(["Versicherung", "Gesundheit"]),
       ["Versicherung", "Gesundheit"])
pruefe("Bookkeeping-Tag raus", gefiltert(["Versicherung", "paperless-ai"]), ["Versicherung"])
pruefe("Reihenfolge bleibt", gefiltert(["A", "paperless-ai", "B"]), ["A", "B"])

# DAS ist die eigentliche Zusicherung: identischer Hash mit und ohne den Tag.
mit    = server._doc_to_text(DOC, None, tag_names=gefiltert(["Versicherung", "paperless-ai"]))
ohne   = server._doc_to_text(DOC, None, tag_names=gefiltert(["Versicherung"]))
pruefe("Hash bleibt trotz Bookkeeping-Tag gleich", mit == ohne, True)

# Ein echter fachlicher Tag MUSS den Hash weiterhin aendern, sonst wuerde eine
# nachtraegliche Verschlagwortung stillschweigend nicht mehr indexiert.
anders = server._doc_to_text(DOC, None, tag_names=gefiltert(["Versicherung", "Rente"]))
pruefe("Fachlicher Tag aendert den Hash", mit != anders, True)

# Leere Variable schaltet den Filter ab (Notausgang ohne Code-Aenderung).
alt = server.INGEST_IGNORE_TAGS
server.INGEST_IGNORE_TAGS = set()
pruefe("leer schaltet ab", gefiltert(["Versicherung", "paperless-ai"]),
       ["Versicherung", "paperless-ai"])
server.INGEST_IGNORE_TAGS = alt

print("test_ignore_tags: alle OK" if not fehler else "test_ignore_tags: FEHLGESCHLAGEN")
raise SystemExit(fehler)
