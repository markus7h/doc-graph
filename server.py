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
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from graphview import color_for, graph_html, index_html

from lightrag import LightRAG, QueryParam
from lightrag.llm.ollama import ollama_model_complete, ollama_embed
from lightrag.utils import EmbeddingFunc
from lightrag.kg.shared_storage import initialize_pipeline_status

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "mistral-small3.2:24b")
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "32768"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))
MAX_ASYNC = int(os.environ.get("MAX_ASYNC", "2"))

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
            llm_model_func=ollama_model_complete,
            llm_model_name=LLM_MODEL,
            llm_model_max_async=MAX_ASYNC,
            llm_model_kwargs={
                "host": OLLAMA_HOST,
                "options": {"num_ctx": LLM_NUM_CTX},
            },
            embedding_func=EmbeddingFunc(
                embedding_dim=EMBED_DIM,
                max_token_size=8192,
                func=lambda texts: ollama_embed(
                    texts, embed_model=EMBED_MODEL, host=OLLAMA_HOST
                ),
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

    new, updated, skipped, texts, ids = 0, 0, 0, [], []

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
            manifest[doc_key] = h
            texts.append(text)
            ids.append(doc_key)

    if texts:
        # Batch-Insert; LightRAG übernimmt Chunking, Extraktion, Embedding.
        # ids sorgen für Upsert-Verhalten bei geänderten Dokumenten.
        await rag.ainsert(texts, ids=ids)
        _save_manifest(project, manifest)

    return (
        f"Projekt '{project}': {new} neu, {updated} aktualisiert, "
        f"{skipped} unverändert übersprungen. "
        f"Gesamt im Index: {len(manifest)} Dokumente."
    )


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
            items = sorted(
                (p.name, (p / "graph.html").exists())
                for p in PROJECTS_DIR.iterdir()
                if p.is_dir() and any(p.glob("*.graphml"))
            )
            body = index_html(items).encode("utf-8")
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
        "doc-graph startet: Port=%s, LLM=%s@%s, Embed=%s(%s)",
        MCP_PORT, LLM_MODEL, OLLAMA_HOST, EMBED_MODEL, EMBED_DIM,
    )
    _start_viewer_server()
    mcp.run(transport="streamable-http")
