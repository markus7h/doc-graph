"""Selbsttest für die drei Ingest-Erweiterungen: Datei-Metadaten-Header,
Entity-Typ-Whitelist und Failure-Deckel. Lauf: python test_ingest_extras.py

Stub-Mechanik wie in test_prepare.py — server.py zieht mcp/lightrag erst
innerhalb von Funktionen, daher genügen leere Modul-Attrappen.
"""
import asyncio
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
import server  # noqa: E402


# ---------------------------------------------------------------- 1: Header
def test_file_header_has_metadata():
    with tempfile.TemporaryDirectory() as td:
        server.INPUTS_DIR = Path(td)
        f = Path(td) / "versicherung" / "2024" / "police.txt"
        f.parent.mkdir(parents=True)
        f.write_text("Inhalt der Police.")
        text = server._file_to_text(f, f.read_text())

    assert text.startswith("Dokument: police\n"), text
    assert "Datum: " in text, text
    # Ordnerpfad wird zu Schlagworten, Dateiname NICHT (der steht schon oben)
    assert "Schlagworte: versicherung, 2024" in text, text
    assert "Quelle: Datei versicherung/2024/police.txt" in text, text
    # Inhalt bleibt hinter einer Leerzeile erhalten
    assert text.endswith("\n\nInhalt der Police."), text


def test_file_header_without_subfolder():
    with tempfile.TemporaryDirectory() as td:
        server.INPUTS_DIR = Path(td)
        f = Path(td) / "brief.md"
        f.write_text("Text")
        text = server._file_to_text(f, "Text")
    # keine Ordner -> keine leere Schlagworte-Zeile
    assert "Schlagworte:" not in text, text
    assert "Quelle: Datei brief.md" in text, text


# ------------------------------------------------------- 2: Entity-Whitelist
def test_entity_types_configured():
    # Whitelist ist nicht leer und enthält keine Leerstrings (split-Fallen)
    assert server.ENTITY_TYPES, server.ENTITY_TYPES
    assert all(t and t == t.strip() for t in server.ENTITY_TYPES), server.ENTITY_TYPES
    assert "Person" in server.ENTITY_TYPES, server.ENTITY_TYPES


def test_graphview_colors_cover_whitelist():
    import graphview
    # Jeder Whitelist-Typ braucht eine feste Farbe, sonst greift der
    # md5-Fallback und gleiche Typen sehen bei jedem Projekt anders aus.
    missing = [t for t in server.ENTITY_TYPES
               if t.lower() not in graphview._TYPE_COLORS]
    assert not missing, f"ohne feste Farbe: {missing}"
    # LightRAG steckt alles Unpassende nach "Other" -> auch das braucht Farbe.
    assert "other" in graphview._TYPE_COLORS


# ---------------------------------------------------- 3: Failure-Deckel
class _FakeRag:
    def __init__(self, fail=()):
        self.deleted = []
        self._fail = set(fail)

    async def adelete_by_doc_id(self, key):
        if key in self._fail:
            raise RuntimeError("boom")
        self.deleted.append(key)


def _with_project(fn):
    """Führt fn(project_id) mit PROJECTS_DIR in einem Temp-Verzeichnis aus."""
    with tempfile.TemporaryDirectory() as td:
        server.PROJECTS_DIR = Path(td)
        (Path(td) / "p").mkdir()
        return fn("p")


def test_attempts_roundtrip():
    def body(proj):
        assert server._load_attempts(proj) == {}
        server._save_attempts(proj, {"d1": {"attempts": 2, "last_error": "Timeout"}})
        assert server._load_attempts(proj)["d1"]["attempts"] == 2
    _with_project(body)


def test_purge_failed_docs_removes_over_limit():
    def body(proj):
        server.MAX_DOC_ATTEMPTS = 3
        rag = _FakeRag()
        attempts = {
            "ok": {"attempts": 2, "last_error": "Timeout", "title": "ok"},
            "poison": {"attempts": 3, "last_error": "LLM-Timeout", "title": "poison"},
        }
        flagged = {}
        changed = asyncio.run(server._purge_failed_docs(proj, rag, attempts, flagged))
        assert changed is True
        # nur das Doc über der Grenze fliegt raus
        assert rag.deleted == ["poison"], rag.deleted
        assert "ok" not in flagged, flagged
        assert flagged["poison"]["decision"] == "open", flagged
        # Fehlergrund landet in der Begründung — sonst ist er nach dem
        # nächsten ainsert weg (LightRAG leert error_msg beim Reset)
        assert "LLM-Timeout" in flagged["poison"]["reason"], flagged
    _with_project(body)


def test_purge_respects_approve():
    def body(proj):
        server.MAX_DOC_ATTEMPTS = 3
        rag = _FakeRag()
        attempts = {"poison": {"attempts": 9, "title": "poison"}}
        flagged = {"poison": {"decision": "approve"}}
        changed = asyncio.run(server._purge_failed_docs(proj, rag, attempts, flagged))
        # bewusst freigegeben -> nicht anfassen
        assert changed is False and rag.deleted == [], (changed, rag.deleted)
    _with_project(body)


def test_purge_disabled_by_zero():
    def body(proj):
        server.MAX_DOC_ATTEMPTS = 0
        rag = _FakeRag()
        attempts = {"poison": {"attempts": 99, "title": "poison"}}
        flagged = {}
        assert asyncio.run(server._purge_failed_docs(proj, rag, attempts, flagged)) is False
        assert rag.deleted == [], rag.deleted
    _with_project(body)


def test_purge_survives_delete_error():
    def body(proj):
        server.MAX_DOC_ATTEMPTS = 3
        rag = _FakeRag(fail={"poison"})
        attempts = {"poison": {"attempts": 5, "title": "poison"}}
        flagged = {}
        # Löschen scheitert -> nicht flaggen (sonst gälte es als erledigt,
        # obwohl das Doc noch in LightRAGs Pipeline hängt) und nicht crashen
        assert asyncio.run(server._purge_failed_docs(proj, rag, attempts, flagged)) is False
        assert flagged == {}, flagged
    _with_project(body)


def test_doc_errors_reads_error_msg():
    def body(proj):
        (server.PROJECTS_DIR / proj / "kv_store_doc_status.json").write_text(json.dumps({
            "a": {"status": "failed", "error_msg": "Timeout nach 480s"},
            "b": {"status": "processed"},
        }))
        errs = server._doc_errors(proj, ["a", "b", "fehlt"])
        assert errs["a"] == "Timeout nach 480s", errs
        assert errs["b"] == "" and errs["fehlt"] == "", errs
    _with_project(body)


# ------------------------------------------------- 4: Verzeichnis-Scan im Thread
def test_extract_text_survives_pdftotext_timeout():
    """Hängendes pdftotext blockiert den Scan nicht, sondern gilt als 'nicht lesbar'."""
    import subprocess
    orig = subprocess.run
    subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(cmd="pdftotext", timeout=k.get("timeout", 0)))
    try:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "haengt.pdf"
            f.write_bytes(b"%PDF-1.4")
            assert server._extract_text(f) is None
    finally:
        subprocess.run = orig


def test_ingest_directory_scans_off_the_event_loop():
    """Der Scan darf den Event-Loop nicht blockieren und muss trotzdem
    pending + unsupported korrekt liefern (Regression zum _scan-Refactor)."""
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as pd:
        server.INPUTS_DIR = Path(td)
        server.PROJECTS_DIR = Path(pd)
        (Path(pd) / "p").mkdir()
        (Path(td) / "a.txt").write_text("Vertrag mit der Musterversicherung AG.")
        (Path(td) / "b.md").write_text("Zweite Notiz.")
        (Path(td) / "c.xlsx").write_bytes(b"nope")  # nicht unterstützt

        captured = {}
        orig_rag, orig_start = server.get_rag, server._start_ingest

        async def _fake_rag(_p):
            return object()

        def _fake_start(project_id, rag, pending, counts, manifest, flagged, tail_note=""):
            captured["pending"] = pending
            captured["tail"] = tail_note
            return "ok"

        # Läuft der Scan im Thread, kommt der Ticker währenddessen dran.
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        async def body():
            nonlocal ticks
            t = asyncio.create_task(_ticker())
            await asyncio.sleep(0)
            await server.ingest_directory("p")
            t.cancel()

        server.get_rag, server._start_ingest = _fake_rag, _fake_start
        try:
            asyncio.run(body())
        finally:
            server.get_rag, server._start_ingest = orig_rag, orig_start

        keys = sorted(k for k, _t, _h in captured["pending"])
        assert keys == ["file:a.txt", "file:b.md"], keys
        assert "1 ignoriert" in captured["tail"], captured["tail"]
        assert ticks > 0, "Scan lief im Event-Loop statt in einem Thread"


# --------------------------------------------- Swap-Refcount bei Hook-Fehler
def test_swap_begin_leckt_refcount_nicht_bei_hookfehler():
    """Der Backup-Scheduler liest _active_ingests > 0 als "Ingest laeuft". Bleibt
    der Zaehler nach einem gescheiterten Wake-Hook oben, laeuft nie wieder ein
    Backup — ohne dass irgendetwas fehlschlaegt. Stiller Dauerausfall."""
    server.SWAP_ENABLED = True
    server._active_ingests = 0

    def _boom(script):
        raise RuntimeError("ingest-begin.sh rc=1")

    original = server._run_hook
    server._run_hook = _boom
    try:
        try:
            asyncio.run(server._swap_begin())
        except RuntimeError:
            pass
        else:
            raise AssertionError("Hook-Fehler haette durchschlagen muessen")
        assert server._active_ingests == 0, (
            f"Refcount leckt: {server._active_ingests} statt 0")
    finally:
        server._run_hook = original


def test_swap_begin_zaehlt_bei_erfolg_hoch():
    """Gegenprobe — der Fix darf den Normalfall nicht kaputtmachen."""
    server.SWAP_ENABLED = True
    server._active_ingests = 0
    original = server._run_hook
    server._run_hook = lambda script: None
    try:
        asyncio.run(server._swap_begin())
        assert server._active_ingests == 1, server._active_ingests
        asyncio.run(server._swap_end())
        assert server._active_ingests == 0, server._active_ingests
    finally:
        server._run_hook = original


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("test_ingest_extras: alle OK")
