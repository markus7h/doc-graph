"""Selbsttest für _prepare_doc — die gemeinsame Pro-Dokument-Entscheidung beider
Ingest-Pfade. Lauf: python test_prepare.py

server.py zieht mcp/lightrag nur INNERHALB von Funktionen (get_rag etc.), nicht
beim Import. Wir stubben die Top-Level-Imports, damit `import server` ohne die
schweren Deps durchläuft — _prepare_doc selbst nutzt keinen davon.
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

server.MAX_DOC_CHARS = 100  # kleine Schwelle für die Oversized-Fälle


def _counts():
    return {"new": 0, "updated": 0, "skipped": 0, "flagged_new": 0}


def test_new_and_skip_and_update():
    manifest, flagged, c = {}, {}, _counts()
    item = server._prepare_doc("d1", "hallo welt", "", "d1", False, {}, manifest, flagged, c)
    assert item is not None and item[0] == "d1", item
    assert c["new"] == 1
    h = item[2]
    manifest["d1"] = h  # als verarbeitet markieren

    # unverändert -> skip
    item = server._prepare_doc("d1", "hallo welt", "", "d1", False, {}, manifest, flagged, _counts_ := _counts())
    assert item is None and _counts_["skipped"] == 1, _counts_

    # geändert -> update
    c2 = _counts()
    item = server._prepare_doc("d1", "hallo welt 2", "", "d1", False, {}, manifest, flagged, c2)
    assert item is not None and c2["updated"] == 1, c2


def test_oversized_flagging():
    big = "x" * 200  # > MAX_DOC_CHARS(=100)
    # neu & übergroß -> None, geflaggt (decision open)
    flagged, c = {}, _counts()
    assert server._prepare_doc("big", big, "", "big", False, {}, {}, flagged, c) is None
    assert c["flagged_new"] == 1 and flagged["big"]["decision"] == "open", flagged

    # approve -> durchlassen, Flag bleibt
    flagged = {"big": {"decision": "approve"}}
    c = _counts()
    item = server._prepare_doc("big", big, "", "big", False, {}, {}, flagged, c)
    assert item is not None and "big" in flagged, (item, flagged)

    # ignore -> still skippen
    flagged = {"big": {"decision": "ignore"}}
    c = _counts()
    assert server._prepare_doc("big", big, "", "big", False, {}, {}, flagged, c) is None
    assert c["skipped"] == 1, c


def test_small_pops_stale_flag():
    flagged = {"d": {"decision": "open"}}
    server._prepare_doc("d", "klein", "", "d", False, {}, {}, flagged, _counts())
    assert "d" not in flagged, flagged


def test_regelwerk_clause_store():
    content = ("§ 1 Was ist versichert?\nDer Versicherer zahlt eine Rente.\n"
               "§ 2 Begriffe\nBerufsunfähigkeit liegt vor, wenn ...\n")
    store = {}
    server._prepare_doc("r1", "Dokument\n\n" + content, content, "AVB", True, store, {}, {}, _counts())
    assert "r1" in store and store["r1"]["clauses"], store


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("test_prepare: alle OK")
