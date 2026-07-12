"""
doc-graph — Knowledge-Graph-MCP-Server (LightRAG, ein Container, N Projekte).

Pro Projekt existiert ein working_dir unter PROJECTS_DIR mit eigenem
LightRAG-Store (Graph + Vektoren + KV). Instanzen werden lazy geladen.
Dokumentquelle ist primär Paperless-NGX (bereits OCR-ter Text via REST-API),
alternativ lokale Textdateien (ingest_directory).

Transport: streamable HTTP -> Claude Code:
  claude mcp add --transport http doc-graph http://myubuntu:5775/mcp
"""

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from graphview import color_for, graph_html, index_html

import numpy as np

from lightrag import LightRAG, QueryParam
from lightrag.llm.openai import openai_complete_if_cache
from lightrag.utils import EmbeddingFunc
from lightrag.kg.shared_storage import initialize_pipeline_status, get_namespace_data

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
# llama-server (OpenAI-kompatibel). LLM und Embeddings sind getrennte Server/Ports
# (llama-server = ein Modell pro Prozess). num_ctx entfällt — Kontext wird beim
# llama-server-Start gesetzt (-c). api_key ist ein Dummy (llama-server prüft keinen).
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://localhost:11435/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "sk-noauth")
LLM_MODEL = os.environ.get("LLM_MODEL", "mistral-small3.2:24b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))
MAX_ASYNC = int(os.environ.get("MAX_ASYNC", "2"))


async def _llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
    return await openai_complete_if_cache(
        LLM_MODEL, prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
        **kwargs,
    )


# Direkter OpenAI-kompatibler Embedding-Call statt lightrag.llm.openai.openai_embed:
# dessen Decorator erzwingt embedding_dim=1536 (ada-002) und validiert hart dagegen —
# unvereinbar mit bge-m3 (1024). Hier gilt allein EmbeddingFunc(embedding_dim=EMBED_DIM).
async def _embed_func(texts):
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{EMBED_BASE_URL}/embeddings",
            json={"model": EMBED_MODEL, "input": list(texts)},
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        )
        r.raise_for_status()
        data = r.json()["data"]
    return np.array([d["embedding"] for d in data], dtype=np.float32)

PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "").rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")

PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/data/projects"))
INPUTS_DIR = Path("/data/inputs")
MCP_PORT = int(os.environ.get("MCP_PORT", "5775"))
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "5776"))
# Hostname, unter dem der Viewer vom Browser erreichbar ist (für die zurückgegebene URL)
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "localhost")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("doc-graph")

PROJECT_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

# ----------------------------------------------------------------------------
# LightRAG-Instanzverwaltung (eine Instanz pro Projekt, lazy)
# ----------------------------------------------------------------------------
_instances: dict[str, LightRAG] = {}
_init_lock = asyncio.Lock()

# Status laufender/letzter Ingests je Projekt (in-memory; ponytail: bei
# Neustart weg -> nicht gespeicherter Manifest sorgt für Re-Ingest, unkritisch).
_ingest_status: dict[str, dict] = {}


def _validate_project(project: str) -> str:
    if not PROJECT_RE.match(project):
        raise ValueError(
            f"Ungültiger Projektname '{project}' (erlaubt: a-z, 0-9, _, -)"
        )
    return project


async def get_rag(project: str) -> LightRAG:
    project = _validate_project(project)
    if project in _instances:
        return _instances[project]

    async with _init_lock:
        if project in _instances:
            return _instances[project]

        working_dir = PROJECTS_DIR / project
        working_dir.mkdir(parents=True, exist_ok=True)

        rag = LightRAG(
            working_dir=str(working_dir),
            llm_model_func=_llm_model_func,
            llm_model_name=LLM_MODEL,
            llm_model_max_async=MAX_ASYNC,
            embedding_func=EmbeddingFunc(
                embedding_dim=EMBED_DIM,
                max_token_size=8192,
                func=_embed_func,
            ),
        )
        await rag.initialize_storages()
        await initialize_pipeline_status()
        _instances[project] = rag
        log.info("LightRAG-Instanz für Projekt '%s' initialisiert (%s)", project, working_dir)
        return rag


# ----------------------------------------------------------------------------
# Ingest-Manifest: doc_id -> content_hash, verhindert Doppel-Indexierung
# ----------------------------------------------------------------------------
def _manifest_path(project: str) -> Path:
    return PROJECTS_DIR / project / "ingest_manifest.json"


def _load_manifest(project: str) -> dict:
    p = _manifest_path(project)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_manifest(project: str, manifest: dict) -> None:
    _manifest_path(project).write_text(json.dumps(manifest, indent=1))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------
# Paperless-NGX-Anbindung
# ----------------------------------------------------------------------------
def _paperless_client() -> httpx.AsyncClient:
    if not PAPERLESS_URL or not PAPERLESS_TOKEN:
        raise RuntimeError("PAPERLESS_URL / PAPERLESS_TOKEN nicht konfiguriert.")
    return httpx.AsyncClient(
        base_url=PAPERLESS_URL,
        headers={"Authorization": f"Token {PAPERLESS_TOKEN}"},
        timeout=60,
        # ponytail: LAN-interne NGX mit self-signed Cert -> kein Verify.
        # Nur im vertrauenswürdigen Netz vertretbar.
        verify=False,
    )


async def _paperless_documents(client: httpx.AsyncClient, params: dict):
    """Alle Dokumente einer gefilterten Suche (paginiert) liefern."""
    url = "/api/documents/"
    params = {**params, "page_size": 50}
    while url:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        for doc in data["results"]:
            yield doc
        url = data.get("next")
        params = None  # next-URL enthält bereits alle Parameter


def _doc_to_text(doc: dict, correspondent_name: str | None,
                 tag_names: list[str] | None = None,
                 doctype_name: str | None = None) -> str:
    """Metadaten-Header + OCR-Inhalt. Der Header landet mit im Graph und
    verankert Datum/Absender/Typ/Schlagworte als extrahierbare Fakten. Die
    kuratierten Paperless-Metadaten sind verlässlicher als LLM-geratene Entitäten."""
    header = [
        f"Dokument: {doc.get('title', '')}",
        f"Datum: {doc.get('created', '')[:10]}",
    ]
    if correspondent_name:
        header.append(f"Korrespondent: {correspondent_name}")
    if doctype_name:
        header.append(f"Dokumenttyp: {doctype_name}")
    if tag_names:
        header.append(f"Schlagworte: {', '.join(tag_names)}")
    if doc.get("archive_serial_number"):
        header.append(f"ASN: {doc['archive_serial_number']}")
    return "\n".join(header) + "\n\n" + (doc.get("content") or "")


# ----------------------------------------------------------------------------
# MCP-Server + Tools
# ----------------------------------------------------------------------------
mcp = FastMCP("doc-graph", host="0.0.0.0", port=MCP_PORT)


@mcp.tool()
async def list_projects() -> str:
    """Listet alle vorhandenen Knowledge-Graph-Projekte mit Dokumentanzahl."""
    if not PROJECTS_DIR.exists():
        return "Keine Projekte vorhanden."
    lines = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir():
            manifest = _load_manifest(d.name)
            lines.append(f"- {d.name}: {len(manifest)} Dokumente indexiert")
    return "\n".join(lines) or "Keine Projekte vorhanden."


@mcp.tool()
async def query(
    project: str,
    question: str,
    mode: str = "hybrid",
    only_context: bool = False,
) -> str:
    """Fragt den Knowledge Graph eines Projekts ab.

    Args:
        project: Projektname (siehe list_projects).
        question: Frage in natürlicher Sprache (Deutsch ok).
        mode: 'local' (entitätsnah), 'global' (übergreifende Muster),
              'hybrid' (Standard), 'mix' (KG + Vektor), 'naive' (nur Vektor).
        only_context: True liefert die rohen Kontext-Chunks/Entitäten statt
              einer generierten Antwort — nützlich, wenn Claude selbst
              formulieren oder wörtlich zitieren soll.
    """
    rag = await get_rag(project)
    param = QueryParam(mode=mode, only_need_context=only_context)
    result = await rag.aquery(question, param=param)
    return str(result)


@mcp.tool()
async def ingest_paperless(
    project: str,
    tag: str = "",
    document_type: str = "",
    correspondent: str = "",
    query_text: str = "",
) -> str:
    """Indexiert Paperless-Dokumente in den Knowledge Graph des Projekts.
    Bereits indexierte, unveränderte Dokumente werden übersprungen.
    Mindestens ein Filter (tag, document_type, correspondent, query_text)
    muss gesetzt sein.

    Args:
        project: Zielprojekt (wird bei Bedarf angelegt).
        tag: Paperless-Tag-Name (exakt, case-insensitive).
        document_type: Dokumenttyp-Name.
        correspondent: Korrespondent-Name.
        query_text: Freitext-Suche (Paperless-Volltextsuche).
    """
    params: dict = {"fields": "id,title,created,content,correspondent,document_type,tags,archive_serial_number"}
    if tag:
        params["tags__name__iexact"] = tag
    if document_type:
        params["document_type__name__iexact"] = document_type
    if correspondent:
        params["correspondent__name__iexact"] = correspondent
    if query_text:
        params["query"] = query_text
    if len(params) == 1:
        return "Fehler: mindestens einen Filter angeben (tag/document_type/correspondent/query_text)."

    rag = await get_rag(project)
    manifest = _load_manifest(project)

    new, updated, skipped, pending = 0, 0, 0, []  # pending: (doc_key, text, hash)

    async with _paperless_client() as client:
        # Korrespondenten-/Tag-/Dokumenttyp-Namen einmal auflösen (IDs -> Namen)
        async def _id_name_map(path: str, page_size: int) -> dict:
            r = await client.get(path, params={"page_size": page_size})
            if r.status_code == 200:
                return {x["id"]: x["name"] for x in r.json().get("results", [])}
            return {}

        corr_map = await _id_name_map("/api/correspondents/", 1000)
        tag_map = await _id_name_map("/api/tags/", 1000)
        doctype_map = await _id_name_map("/api/document_types/", 1000)

        async for doc in _paperless_documents(client, params):
            doc_key = f"paperless:{doc['id']}"
            tag_names = [tag_map[t] for t in doc.get("tags", []) if t in tag_map]
            text = _doc_to_text(
                doc, corr_map.get(doc.get("correspondent")),
                tag_names=tag_names,
                doctype_name=doctype_map.get(doc.get("document_type")),
            )
            h = _hash(text)
            if manifest.get(doc_key) == h:
                skipped += 1
                continue
            if doc_key in manifest:
                updated += 1
            else:
                new += 1
            # ponytail: Hash NICHT vorab ins Manifest — erst nach erfolgreichem
            # Insert im Hintergrund, damit ein Abbruch das Dokument nicht "erledigt".
            pending.append((doc_key, text, h))

    if not pending:
        return (
            f"Projekt '{project}': nichts zu tun ({skipped} unverändert). "
            f"Gesamt im Index: {len(manifest)} Dokumente."
        )

    if _ingest_status.get(project, {}).get("state") == "running":
        return (
            f"Projekt '{project}': Ingest läuft bereits. "
            f'Fortschritt: ingest_status(project="{project}").'
        )

    total = len(pending)

    # Extraktion ist der teure Teil (viele LLM-Calls, ggf. Stunden). Im
    # Hintergrund + Dokument für Dokument, damit man echten Fortschritt (done/total)
    # sieht und ein Abbruch nur das laufende Dokument kostet (Manifest je Doc gespeichert).
    async def _poll_msg(stop: asyncio.Event):
        # Liest LightRAGs Live-Meldung (z.B. "Chunk 5 of 26 extracted ...") in den
        # Status, damit man Fortschritt auch INNERHALB eines langen Dokuments sieht.
        # ponytail: kooperatives asyncio -> läuft nie echt parallel zum Insert, kein Lock.
        while not stop.is_set():
            try:
                ps = await get_namespace_data("pipeline_status")
                st = _ingest_status.get(project)
                if st and st.get("state") == "running":
                    st["msg"] = str(ps.get("latest_message") or "")[:160]
            except Exception:  # noqa: BLE001 — Status-Anzeige ist best effort
                pass
            await asyncio.sleep(3)

    async def _run():
        done = 0
        stop = asyncio.Event()
        poller = asyncio.create_task(_poll_msg(stop))
        try:
            for doc_key, text, h in pending:
                await rag.ainsert([text], ids=[doc_key])
                manifest[doc_key] = h
                _save_manifest(project, manifest)
                done += 1
                _ingest_status[project]["done"] = done  # in-place: Poller-Feld bleibt
            _ingest_status[project] = {
                "state": "done", "done": done, "total": total, "new": new,
                "updated": updated, "skipped": skipped, "at": _now(),
            }
        except Exception as e:  # noqa: BLE001 — Status festhalten, nicht crashen
            log.exception("Ingest fehlgeschlagen für %s", project)
            _ingest_status[project] = {
                "state": "error", "error": str(e), "done": done, "total": total, "at": _now(),
            }
        finally:
            stop.set()
            poller.cancel()

    task = asyncio.create_task(_run())
    _ingest_status[project] = {
        "state": "running", "done": 0, "total": total, "new": new,
        "updated": updated, "skipped": skipped, "at": _now(), "_task": task,
    }
    return (
        f"Projekt '{project}': Ingest von {total} Dokumenten "
        f"({new} neu, {updated} aktualisiert, {skipped} übersprungen) "
        f"im Hintergrund gestartet — Extraktion läuft, das dauert. "
        f'Fortschritt: ingest_status(project="{project}").'
    )


@mcp.tool()
async def ingest_status(project: str) -> str:
    """Status des laufenden/letzten ingest_paperless-Laufs (läuft im Hintergrund)."""
    project = _validate_project(project)
    st = _ingest_status.get(project)
    if not st:
        return f"Projekt '{project}': kein Ingest bekannt (nichts gestartet oder Server neu gestartet)."
    return json.dumps({k: v for k, v in st.items() if not k.startswith("_")}, ensure_ascii=False)


@mcp.tool()
async def ingest_directory(project: str, subpath: str = "") -> str:
    """Indexiert lokale Textdateien (.txt, .md) aus /data/inputs/<subpath>
    in den Knowledge Graph. Für PDFs Paperless als Quelle nutzen (OCR fertig).

    Args:
        project: Zielprojekt.
        subpath: Unterverzeichnis relativ zum gemounteten inputs-Volume.
    """
    base = (INPUTS_DIR / subpath).resolve()
    if not str(base).startswith(str(INPUTS_DIR)):
        return "Fehler: Pfad außerhalb des inputs-Volumes."
    if not base.exists():
        return f"Fehler: {base} existiert nicht."

    rag = await get_rag(project)
    manifest = _load_manifest(project)
    new, skipped, texts, ids = 0, 0, [], []

    for f in sorted(base.rglob("*")):
        if f.suffix.lower() not in (".txt", ".md") or not f.is_file():
            continue
        text = f"Dokument: {f.name}\n\n" + f.read_text(errors="replace")
        doc_key = f"file:{f.relative_to(INPUTS_DIR)}"
        h = _hash(text)
        if manifest.get(doc_key) == h:
            skipped += 1
            continue
        manifest[doc_key] = h
        texts.append(text)
        ids.append(doc_key)
        new += 1

    if texts:
        await rag.ainsert(texts, ids=ids)
        _save_manifest(project, manifest)

    return f"Projekt '{project}': {new} Dateien indexiert, {skipped} unverändert."


@mcp.tool()
async def get_entity(project: str, entity_name: str) -> str:
    """Liefert Details und Beziehungen zu einer Entität im Graph
    (z. B. Person, Grundstück, Aktenzeichen)."""
    rag = await get_rag(project)
    result = await rag.aquery(
        f"Nenne alle bekannten Fakten und Beziehungen zur Entität '{entity_name}', "
        f"chronologisch geordnet, mit Quellenbezug.",
        param=QueryParam(mode="local"),
    )
    return str(result)


@mcp.tool()
async def graph_view(project: str) -> str:
    """Erzeugt eine interaktive HTML-Ansicht des Knowledge Graphs zum Durchklicken
    (Knoten = Entitäten, gefärbt nach Typ; Kanten = Beziehungen). Details erscheinen
    per Klick in einem Panel am unteren Rand. Gibt die URL zurück, die im Browser zu öffnen ist.

    Args:
        project: Projektname (siehe list_projects).
    """
    import networkx as nx  # via lightrag-hku installiert (NetworkX-Graphstore)

    project = _validate_project(project)

    # Alle Projekte mit Graph — für das Umschalt-Dropdown; jede Seite wird neu
    # gerendert, damit die Dropdowns projektübergreifend konsistent sind.
    # ponytail: alle Projekte je Aufruf rendern; on-demand-Route erst nötig, wenn die Projektzahl groß wird.
    projs = sorted(
        p.name for p in PROJECTS_DIR.iterdir()
        if p.is_dir() and any(p.glob("*.graphml"))
    )
    if project not in projs:
        return f"Kein Graph für '{project}' gefunden — erst ingest_paperless/ingest_directory ausführen."

    def _render(proj: str) -> tuple[int, int]:
        G = nx.read_graphml(str(next((PROJECTS_DIR / proj).glob("*.graphml"))))
        nodes, edges = [], []
        for n, d in G.nodes(data=True):
            etype = d.get("entity_type", "")
            nodes.append({
                "id": n, "label": str(n).strip('"'), "group": etype,
                "color": color_for(etype),
                "desc": d.get("description", "")[:400],
            })
        for u, v, d in G.edges(data=True):
            tip = d.get("description") or d.get("keywords") or ""
            edges.append({"from": u, "to": v, "desc": str(tip)[:400]})
        (PROJECTS_DIR / proj / "graph.html").write_text(
            graph_html(nodes, edges, f"KG: {proj}", projects=projs, current=proj),
            encoding="utf-8")
        return len(nodes), len(edges)

    n_nodes = n_edges = 0
    for p in projs:
        counts = _render(p)
        if p == project:
            n_nodes, n_edges = counts

    url = f"http://{PUBLIC_HOST}:{VIEWER_PORT}/{project}/graph.html"
    return (f"Graph exportiert ({n_nodes} Entitäten, {n_edges} Beziehungen; "
            f"{len(projs)} Projekt(e) im Umschalter).\nÖffnen: {url}")


def _delete_project_dir(project: str) -> bool:
    """Löscht Index-Verzeichnis + gecachte Instanz (nicht die Quelldokumente).
    True, wenn etwas gelöscht wurde. Validiert gegen Path-Traversal."""
    import shutil

    _validate_project(project)
    _instances.pop(project, None)
    target = PROJECTS_DIR / project
    if target.exists():
        shutil.rmtree(target)
        return True
    return False


@mcp.tool()
async def delete_project(project: str, confirm: bool = False) -> str:
    """Löscht einen kompletten Projekt-Index (nicht die Quelldokumente!).
    Erfordert confirm=True."""
    _validate_project(project)
    if not confirm:
        return f"Sicherheitsabfrage: erneut mit confirm=True aufrufen, um '{project}' zu löschen."
    if _delete_project_dir(project):
        return f"Projekt '{project}' gelöscht."
    return f"Projekt '{project}' existiert nicht."


class _ViewerHandler(SimpleHTTPRequestHandler):
    """Statischer Fileserver mit hübscher Landing-Page am Root statt rohem
    Dir-Listing. ponytail: nur '/' abgefangen, Rest bleibt stdlib-static."""

    def do_GET(self):  # noqa: N802 (stdlib-Signatur)
        if self.path in ("/", "/index.html"):
            rendered = {
                p.name: (p / "graph.html").exists()
                for p in PROJECTS_DIR.iterdir()
                if p.is_dir() and any(p.glob("*.graphml"))
            }
            # laufende/erledigte Ingests ohne (noch) gerenderten Graph mit anzeigen
            for name in _ingest_status:
                rendered.setdefault(name, False)
            items = sorted(rendered.items())
            body = index_html(items, _ingest_status).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802 (stdlib-Signatur)
        # nur Projekt-Löschung; Bestätigung passiert im Browser (confirm-Dialog)
        if self.path != "/delete":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        project = (params.get("project") or [""])[0]
        try:
            _delete_project_dir(project)
        except ValueError:
            self.send_error(400, "invalid project name")
            return
        self.send_response(303)  # zurück zur Landing-Page
        self.send_header("Location", "/")
        self.end_headers()


def _start_viewer_server() -> None:
    """Serviert PROJECTS_DIR statisch (nur die generierten graph.html-Ansichten
    interessieren). Daemon-Thread, LAN-intern. ponytail: stdlib-Fileserver reicht,
    kein Auth/HTTPS — hinter dem internen Netz, kein öffentlicher Zugang."""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(_ViewerHandler, directory=str(PROJECTS_DIR))
    httpd = HTTPServer(("0.0.0.0", VIEWER_PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("Graph-Viewer läuft auf Port %s (http://%s:%s/<projekt>/graph.html)",
             VIEWER_PORT, PUBLIC_HOST, VIEWER_PORT)


if __name__ == "__main__":
    log.info(
        "doc-graph startet: Port=%s, LLM=%s@%s, Embed=%s(%s)@%s",
        MCP_PORT, LLM_MODEL, LLM_BASE_URL, EMBED_MODEL, EMBED_DIM, EMBED_BASE_URL,
    )
    _start_viewer_server()
    mcp.run(transport="streamable-http")
