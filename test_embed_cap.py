"""Selbsttest fuer den Embedding-Input-Cap. Lauf: python test_embed_cap.py

Hintergrund: llama-server lehnt Inputs > -ub mit HTTP 500 ab, LightRAG cancelt
daraufhin den ganzen Ingest-Lauf. _cap_embed_input kappt vorher, _embed_func
versucht bei einem trotzdem auftretenden "too large" nochmal halbiert.

Stub-Mechanik wie in test_ingest_extras.py.
"""
import asyncio
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


class _Resp:
    """Minimal-Attrappe einer httpx-Response."""

    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code} nicht abgefangen: {self.text}")


class _Client:
    """Sammelt die gesendeten Inputs und spielt eine Antwortfolge ab."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []

    async def post(self, url, json=None, headers=None):
        self.sent.append(json["input"])
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _ok(n):
    return _Resp(200, {"data": [{"embedding": [0.0] * server.EMBED_DIM} for _ in range(n)]})


def _too_large():
    return _Resp(500, text='input (3002 tokens) is too large to process')


# ------------------------------------------------------------------ 1: Cap
def test_cap_laesst_kurze_texte_unveraendert():
    text = "kurz" * 10
    assert server._cap_embed_input(text) == text


def test_cap_kuerzt_lange_texte():
    limit = server.EMBED_MAX_TOKENS * 3
    text = "x" * (limit + 5000)
    out = server._cap_embed_input(text)
    assert len(out) == limit, len(out)
    assert out == text[:limit]


def test_cap_grenzfall_exakt_auf_limit():
    limit = server.EMBED_MAX_TOKENS * 3
    text = "y" * limit
    assert server._cap_embed_input(text) == text, "exakt auf Limit darf nicht kappen"


# ------------------------------------------------- 2: _embed_func kappt wirklich
def test_embed_func_kappt_vor_dem_senden():
    limit = server.EMBED_MAX_TOKENS * 3
    client = _Client([_ok(2)])
    server._embed_client = client
    asyncio.run(server._embed_func(["a" * (limit + 9999), "kurz"]))

    gesendet = client.sent[0]
    assert len(gesendet[0]) == limit, len(gesendet[0])
    assert gesendet[1] == "kurz"


# ------------------------------------------------------- 3: Retry statt Abbruch
def test_embed_func_retryt_bei_too_large():
    client = _Client([_too_large(), _ok(1)])
    server._embed_client = client
    # kurzer Input: der Cap greift NICHT, nur der Retry kann hier retten
    out = asyncio.run(server._embed_func(["abcdefgh"]))

    assert len(client.sent) == 2, "kein Retry ausgeloest"
    assert client.sent[1][0] == "abcd", client.sent[1]
    assert out.shape == (1, server.EMBED_DIM), out.shape


def test_embed_func_kein_retry_bei_anderem_fehler():
    """500 ohne 'too large' ist ein echter Serverfehler — nicht wegretryen."""
    client = _Client([_Resp(500, text="CUDA out of memory")])
    server._embed_client = client
    try:
        asyncio.run(server._embed_func(["abc"]))
    except AssertionError:
        pass  # raise_for_status hat zugeschlagen, wie gewollt
    else:
        raise AssertionError("Fehler haette durchschlagen muessen")
    assert len(client.sent) == 1, "es haette kein Retry stattfinden duerfen"


# ------------------------------------------------------ 3b: Batching der Menge
def _zaehl_ok(werte):
    """Antwort mit unterscheidbaren Vektoren, um Reihenfolge pruefen zu koennen."""
    return _Resp(200, {"data": [{"embedding": [float(v)] * server.EMBED_DIM}
                                for v in werte]})


def test_embed_func_zerlegt_grosse_mengen():
    """EMBED_MAX_TOKENS deckelt die Laenge, nicht die Menge. Ohne Portionierung
    ging beim Merge alles in EINEM Request raus — gemessen 100s fuer 1024 Inputs,
    mal EMBED_MAX_ASYNC parallel ueber dem Timeout. Dann bricht der Server die
    Verbindung ab und LightRAG haelt die ganze Pipeline an."""
    n = server.EMBED_BATCH * 2 + 7
    rest = n - server.EMBED_BATCH * 2
    client = _Client([_ok(server.EMBED_BATCH), _ok(server.EMBED_BATCH), _ok(rest)])
    server._embed_client = client
    out = asyncio.run(server._embed_func(["text"] * n))

    assert len(client.sent) == 3, f"{len(client.sent)} Requests statt 3"
    assert [len(p) for p in client.sent] == [server.EMBED_BATCH,
                                             server.EMBED_BATCH, rest]
    assert all(len(p) <= server.EMBED_BATCH for p in client.sent)
    assert out.shape == (n, server.EMBED_DIM), out.shape


def test_embed_func_behaelt_reihenfolge_ueber_haeppchen():
    """LightRAG ordnet Vektoren positionsweise zu — verrutscht die Reihenfolge,
    haengen die Embeddings an den falschen Entitaeten."""
    n = server.EMBED_BATCH + 3
    client = _Client([_zaehl_ok(range(server.EMBED_BATCH)),
                      _zaehl_ok(range(server.EMBED_BATCH, n))])
    server._embed_client = client
    out = asyncio.run(server._embed_func([f"t{i}" for i in range(n)]))

    assert out.shape == (n, server.EMBED_DIM), out.shape
    assert [row[0] for row in out] == [float(i) for i in range(n)], "Reihenfolge verrutscht"
    # Die Eingaben muessen ebenfalls in Originalreihenfolge portioniert sein
    assert client.sent[0][0] == "t0" and client.sent[1][0] == f"t{server.EMBED_BATCH}"


def test_embed_func_kleine_menge_bleibt_ein_request():
    """Kein unnoetiges Zerstueckeln — der Normalfall bleibt ein einziger Request."""
    client = _Client([_ok(3)])
    server._embed_client = client
    asyncio.run(server._embed_func(["a", "b", "c"]))
    assert len(client.sent) == 1, "kleine Mengen duerfen nicht aufgeteilt werden"


def test_retry_wirkt_innerhalb_eines_haeppchens():
    """Der too-large-Retry muss pro Haeppchen greifen, nicht nur beim ersten."""
    n = server.EMBED_BATCH + 1
    client = _Client([_ok(server.EMBED_BATCH), _too_large(), _ok(1)])
    server._embed_client = client
    out = asyncio.run(server._embed_func(["abcdefgh"] * n))

    assert len(client.sent) == 3, f"{len(client.sent)} Requests statt 3"
    assert client.sent[2][0] == "abcd", client.sent[2]
    assert out.shape == (n, server.EMBED_DIM), out.shape


# ------------------------------------- 3c: Retry bei abgerissener Verbindung
def _disconnect():
    import httpx
    return httpx.RemoteProtocolError("Server disconnected without sending a response.")


def test_embed_func_wiederholt_bei_verbindungsabbruch():
    """Der haeufigste Fehler im Betrieb: der Server schliesst eine Keep-Alive-
    Verbindung, httpx meldet RemoteProtocolError. Ohne Wiederholung reisst das
    den Ingest des ganzen Dokuments ab."""
    client = _Client([_disconnect(), _ok(1)])
    server._embed_client = client
    out = asyncio.run(server._embed_func(["abc"]))

    assert len(client.sent) == 2, "kein Wiederholversuch"
    assert out.shape == (1, server.EMBED_DIM), out.shape


def test_embed_func_gibt_nach_drei_versuchen_auf():
    """Kein endloses Wiederholen — ein dauerhaft toter Endpunkt muss durchschlagen."""
    import httpx
    client = _Client([_disconnect(), _disconnect(), _disconnect()])
    server._embed_client = client
    try:
        asyncio.run(server._embed_func(["abc"]))
    except httpx.RemoteProtocolError:
        pass
    else:
        raise AssertionError("Fehler haette nach 3 Versuchen durchschlagen muessen")
    assert len(client.sent) == 3, f"{len(client.sent)} Versuche statt 3"


def _upstream_weg(status=500):
    """So meldet LiteLLM ein weggefallenes Backend."""
    return _Resp(status, text=("litellm.InternalServerError: InternalServerError: "
                               "OpenAIException - Connection error.. Received "
                               "Model Group=bge-m3"))


def test_embed_func_wiederholt_bei_totem_backend_hinter_dem_router():
    """myubuntu ist Burst-Backend und darf weg sein. Der Router schickt trotz
    eigenem Health-Check weiter Verkehr dorthin und reicht den Ausfall als
    HTTP 500 durch — ohne Wiederholung verliert das Dokument den ganzen Lauf."""
    client = _Client([_upstream_weg(), _ok(1)])
    server._embed_client = client
    out = asyncio.run(server._embed_func(["abc"]))

    assert len(client.sent) == 2, "kein Wiederholversuch nach Upstream-Ausfall"
    assert out.shape == (1, server.EMBED_DIM), out.shape


def test_embed_func_wiederholt_bei_gateway_fehler():
    """502/503/504 kommen vom Router selbst, nie vom llama-server."""
    client = _Client([_Resp(503, text="Service Unavailable"), _ok(1)])
    server._embed_client = client
    out = asyncio.run(server._embed_func(["abc"]))

    assert len(client.sent) == 2, "kein Wiederholversuch bei 503"
    assert out.shape == (1, server.EMBED_DIM), out.shape


def test_embed_func_wiederholt_nicht_bei_serverfehler():
    """HTTP 500 ist kein Verbindungsproblem — hier greift nur der too-large-Pfad."""
    client = _Client([_Resp(500, text="CUDA out of memory")])
    server._embed_client = client
    try:
        asyncio.run(server._embed_func(["abc"]))
    except AssertionError:
        pass  # raise_for_status der Attrappe
    assert len(client.sent) == 1, "Verbindungs-Retry haette nicht greifen duerfen"


# ------------------------------------------- 4: Konsistenz mit LightRAG-Setup
def test_max_token_size_matcht_cap():
    """max_token_size in get_rag muss EMBED_MAX_TOKENS sein, nicht bge-m3s 8192."""
    src = (server.__file__ or "server.py")
    with open(src, encoding="utf-8") as fh:
        code = fh.read()
    assert "max_token_size=EMBED_MAX_TOKENS" in code, "max_token_size haengt nicht am Cap"
    assert "max_token_size=8192" not in code, "alter Hardcode 8192 noch da"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("test_embed_cap: alle OK")
