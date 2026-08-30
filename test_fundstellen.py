"""Selbsttest fuer die Fundstellen. Lauf: python test_fundstellen.py

Hintergrund: LightRAG baut seine Reference Document List aus dem file_path der
Chunks und ueberspringt dabei den Default 'unknown_source'. Ohne file_paths am
ainsert ist deshalb JEDE query-Antwort unbelegbar. Geprueft wird beides: das
Ableiten der Fundstelle aus dem Metadaten-Header (server._fundstelle) und das
Backfill bestehender Stores.

Stub-Mechanik wie in test_embed_cap.py.
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
import backfill_fundstellen as bf   # noqa: E402

fehler = 0


def pruefe(label, ist, soll):
    global fehler
    if ist == soll:
        print(f"  {label}: OK")
    else:
        print(f"  {label}: FEHLER\n     ist  {ist!r}\n     soll {soll!r}")
        fehler = 1


# --------------------------------------------------- 1: Header -> Fundstelle
PAPERLESS = ("Dokument: 2026_05_28_Stellungnahme_an_Ergo\n"
             "Datum: 2026-05-28\n"
             "Korrespondent: ERGO Lebensversicherung AG\n"
             "Dokumenttyp: ausgehend\n"
             "Schlagworte: dx: Microsoft Versicherung\n\n"
             "ERGO Lebensversicherung AG ...")
pruefe("Paperless-Header vollstaendig",
       server._fundstelle(PAPERLESS),
       "2026_05_28_Stellungnahme_an_Ergo, 2026-05-28, ERGO Lebensversicherung AG")

# Datei-Variante kennt keinen Korrespondenten — die Fundstelle bleibt trotzdem
# zitierfaehig, nur kuerzer.
pruefe("Datei-Header ohne Korrespondent",
       server._fundstelle("Dokument: Police_B003\nDatum: 2012-01-01\n"
                          "Quelle: Datei versicherung/Police_B003.pdf\n\nInhalt"),
       "Police_B003, 2012-01-01")

# Kein Header: darf NICHT raten. 'ohne Titel' ist ehrlicher als ein erfundener
# Beleg — und faellt im Backfill-Bericht als Zaehler auf.
pruefe("ohne Header", server._fundstelle("Einfach nur Text ohne alles"), "ohne Titel")

# LightRAG behandelt den file_path als Dateinamen und verwirft Dokumente mit
# gleichem Pfad ("Duplicate document detected"). Titel+Datum+Korrespondent sind
# nicht eindeutig — im Bestand future-fund fielen 30 Dokumente still heraus.
pruefe("doc_key macht die Fundstelle eindeutig",
       server._fundstelle(PAPERLESS, "paperless:4711"),
       "2026_05_28_Stellungnahme_an_Ergo, 2026-05-28, "
       "ERGO Lebensversicherung AG (paperless:4711)")
DOPPELT = "Dokument: Selbstauskunft\nDatum: 2024\n\nInhalt A"
pruefe("gleicher Header, verschiedene Schluessel -> verschiedene Pfade",
       server._fundstelle(DOPPELT, "paperless:1") != server._fundstelle(DOPPELT, "paperless:2"),
       True)
pruefe("ohne Schluessel unveraendert (Backfill-Pfad)",
       server._fundstelle(DOPPELT), "Selbstauskunft, 2024")

# 'Schlagworte: dx: Microsoft' enthaelt einen zweiten Doppelpunkt — partition
# darf nur am ersten trennen, sonst zerfaellt der Header.
pruefe("Doppelpunkt im Wert",
       server._fundstelle("Dokument: A: B\nDatum: 2024-01-01\n\nx"),
       "A: B, 2024-01-01")

# ------------------------------------------------------------- 2: Backfill
with tempfile.TemporaryDirectory() as tmp:
    projekt = Path(tmp) / "testprojekt"
    projekt.mkdir()
    (projekt / "kv_store_full_docs.json").write_text(json.dumps({
        "doc-1": {"content": "Dokument: Brief\nDatum: 2026-07-07\n"
                             "Korrespondent: ERGO\n\nText", "file_path": "unknown_source"},
    }))
    (projekt / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-1": {"full_doc_id": "doc-1", "file_path": "unknown_source"},
        # Schon gesetzt: ein zweiter Lauf darf nicht ueberschreiben.
        "chunk-2": {"full_doc_id": "doc-1", "file_path": "Handgepflegt"},
    }))
    (projekt / "vdb_chunks.json").write_text(json.dumps({
        "embedding_dim": 3,
        "data": [{"__id__": "chunk-1", "full_doc_id": "doc-1", "file_path": "unknown_source"}],
    }))

    trocken = bf.backfill(projekt, dry=True)
    pruefe("dry-run zaehlt", trocken["geaendert"],
           {"kv_store_text_chunks.json": 1, "kv_store_full_docs.json": 1,
            "vdb_chunks.json": 1})
    pruefe("dry-run schreibt nicht",
           json.loads((projekt / "kv_store_text_chunks.json").read_text())["chunk-1"]["file_path"],
           "unknown_source")

    bf.backfill(projekt, dry=False)
    chunks = json.loads((projekt / "kv_store_text_chunks.json").read_text())
    pruefe("Chunk bekommt Fundstelle", chunks["chunk-1"]["file_path"],
           "Brief, 2026-07-07, ERGO")
    pruefe("vorhandener Wert bleibt", chunks["chunk-2"]["file_path"], "Handgepflegt")
    pruefe("Vektorstore mitgezogen",
           json.loads((projekt / "vdb_chunks.json").read_text())["data"][0]["file_path"],
           "Brief, 2026-07-07, ERGO")
    pruefe("Backup angelegt",
           bool(list(projekt.glob("kv_store_text_chunks.json.bak-*"))), True)

    # Zweiter Lauf: alles schon gesetzt, nichts mehr zu tun (idempotent).
    pruefe("idempotent", bf.backfill(projekt, dry=False)["geaendert"], {})

    # Der Wert, an dem LightRAG die Referenzliste abschneidet, darf nicht
    # ueberleben — sonst war das Backfill wirkungslos.
    pruefe("kein unknown_source mehr",
           [c["file_path"] for c in chunks.values() if c["file_path"] == bf.UNKNOWN], [])

print("test_fundstellen: alle OK" if not fehler else "test_fundstellen: FEHLGESCHLAGEN")
raise SystemExit(fehler)
