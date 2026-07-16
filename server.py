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
import shutil
import subprocess
import tarfile
import threading
import time
from collections import Counter
from datetime import datetime
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
# Timeout (s) für einen einzelnen LLM-Call an den llama-server. Bei CPU-Offload
# (niedriger t/s) reißen dichte Chunks den Default -> hier hochsetzen. Der
# eigentliche Engpass bleibt der Throughput (GPU), das ist nur der Deckel.
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "480"))
# Chunk-Größe (Tokens). 600 statt LightRAG-Default 1200: weniger Entitäten pro
# Chunk -> kürzere Extraktion, beseitigt den 480s-Worker-Timeout bei dichten
# Tabellen-/Listen-Docs (siehe INGEST-FAILURE-ANALYSE.md).
CHUNK_TOKEN_SIZE = int(os.environ.get("CHUNK_TOKEN_SIZE", "600"))
# Kontext-Budget je Query (Tokens). 12000 statt Default 30000: hält den
# only_context-Dump unter dem MCP-Token-Limit (Issue #2) und fokussiert.
QUERY_MAX_TOKENS = int(os.environ.get("QUERY_MAX_TOKENS", "12000"))
# Sprache der extrahierten Entitäten/Beschreibungen. LightRAG-Default ist
# "English" -> Graph-Einträge landen auf Englisch, obwohl die Docs deutsch sind.
GRAPH_LANGUAGE = os.environ.get("GRAPH_LANGUAGE", "German")


async def _llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
    return await openai_complete_if_cache(
        LLM_MODEL, prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
        timeout=LLM_TIMEOUT,
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
# Backup-Ziel (gemountet, i.d.R. OneDrive/doc-graph). Rotation auf die letzten
# MAX_BACKUPS Archive; Intervall/An-Aus kommen aus .config.json (via Web-UI).
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "10"))
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
# Kooperative Steuerung eines laufenden Ingests: project_id -> "pause" | "stop".
# Vom HTTP-Thread/MCP-Handler gesetzt, vom Ingest-Loop zwischen zwei Dokumenten
# gelesen (einzelne Dict-Zuweisung -> GIL genügt, kein Lock).
_ingest_control: dict[str, str] = {}

# LightRAG-Instanzen teilen im selben Prozess den pipeline_status-Lock: läuft
# die Pipeline von Projekt A, kehrt ainsert() von Projekt B SOFORT zurück
# (Doc nur enqueued, "request pending") — B hielte das Doc fälschlich für
# verarbeitet. Ein globaler Insert-Lock serialisiert alle ainsert-Aufrufe;
# bei geteilter GPU ist sequenziell ohnehin richtig.
_insert_lock = asyncio.Lock()

# ----------------------------------------------------------------------------
# GPU-Swap: für die Dauer eines Ingests qwen3-14b VOLL auf die GPU, mistral raus
# (sonst nur Teil-GPU-Offload -> langsam/Timeouts). Beide sind Services im
# llm-stack-Compose-Projekt mit geteiltem Netz-Alias 'llm' — LLM_BASE_URL bleibt
# beim Wechsel unverändert. swap-to-*.sh liegen im Image und steuern via
# gemountetem Docker-Socket llm-mistral/llm-qwen (stop/start, kein pause mehr:
# paperless-ai ist auf llm-mistral gepinnt und bekommt nie qwen-Antworten).
# Refcount unter Lock: paralleler Ingest über mehrere Projekte swappt EINMAL.
# ponytail: globaler Lock reicht — Ingests laufen selten und selten parallel.
# INGEST_SWAP=0 schaltet den Swap ab (lokale Dev-Umgebung ohne Docker-Socket).
SWAP_ENABLED = os.environ.get("INGEST_SWAP", "1") == "1"
_swap_lock = asyncio.Lock()
_active_ingests = 0

def _run_swap(script: str) -> None:
    p = Path(__file__).parent / script
    r = subprocess.run(["bash", str(p)], capture_output=True, text=True, timeout=1500)
    if r.returncode != 0:
        log.error("%s fehlgeschlagen (rc=%s): %s", script, r.returncode, (r.stderr or r.stdout)[-800:])
        raise RuntimeError(f"{script} rc={r.returncode}")
    log.info("GPU-Swap: %s ok", script)

async def _swap_begin() -> None:
    """Vor dem ersten parallelen Ingest zu qwen swappen (blockt bis qwen bereit)."""
    global _active_ingests
    if not SWAP_ENABLED:
        return
    async with _swap_lock:
        _active_ingests += 1
        if _active_ingests == 1:
            log.info("GPU-Swap: mistral raus, qwen laden…")
            await asyncio.to_thread(_run_swap, "swap-to-qwen.sh")

async def _swap_end() -> None:
    """Nach dem letzten laufenden Ingest zurück zu mistral."""
    global _active_ingests
    if not SWAP_ENABLED:
        return
    async with _swap_lock:
        _active_ingests = max(0, _active_ingests - 1)
        if _active_ingests == 0:
            log.info("GPU-Swap: zurück auf mistral…")
            await asyncio.to_thread(_run_swap, "swap-to-mistral.sh")


# ponytail: Meta-Datei pro Projekt (project_name, etc.), analog Manifest-Pattern
def _meta_path(project_id: str) -> Path:
    return PROJECTS_DIR / project_id / "meta.json"


def _load_meta(project_id: str) -> dict:
    p = _meta_path(project_id)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_meta(project_id: str, meta: dict) -> None:
    _meta_path(project_id).write_text(json.dumps(meta, indent=1, ensure_ascii=False))


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
            chunk_token_size=CHUNK_TOKEN_SIZE,
            addon_params={"language": GRAPH_LANGUAGE},
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


def _doc_status_counts(project: str) -> dict:
    """Echte Terminal-Zustände je Dokument aus LightRAGs doc_status-Store
    (pending/processing/processed/failed). Der 'done'-Zustand der Dispatch-
    Schleife heißt nur 'enqueued', nicht 'im Graph' — die Wahrheit steht hier."""
    p = PROJECTS_DIR / project / "kv_store_doc_status.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return dict(Counter(v.get("status", "unknown") for v in data.values()))


def _doc_state(project: str, doc_key: str) -> str:
    """Echter LightRAG-Zustand EINES Dokuments (''=unbekannt). Guard nach
    ainsert: nur 'processed' darf ins Manifest — ainsert kann zurückkehren,
    ohne verarbeitet zu haben (geteilter Pipeline-Lock, Kill mittendrin)."""
    p = PROJECTS_DIR / project / "kv_store_doc_status.json"
    if not p.exists():
        return ""
    return json.loads(p.read_text()).get(doc_key, {}).get("status", "")


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
        # Paperless liefert die Paginierungs-'next'-URL absolut als http://,
        # der Proxy antwortet darauf mit 308 -> https. Ohne Folgen des Redirects
        # bricht der Ingest ab Seite 2 ab (0 Docs bei >50 Treffern).
        follow_redirects=True,
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
            meta = _load_meta(d.name)
            name = meta.get("project_name") or d.name
            label = f"{d.name} ({name})" if meta.get("project_name") else d.name
            lines.append(f"- {label}: {len(manifest)} Dokumente indexiert")
    return "\n".join(lines) or "Keine Projekte vorhanden."


@mcp.tool()
async def query(
    project_id: str,
    question: str,
    mode: str = "hybrid",
    only_context: bool = True,
    max_total_tokens: int = QUERY_MAX_TOKENS,
) -> str:
    """Fragt den Knowledge Graph eines Projekts ab.

    Args:
        project_id: technischer Projekt-Schlüssel (siehe list_projects), stabil — NICHT der Anzeigename.
        question: Frage in natürlicher Sprache (Deutsch ok).
        mode: 'local' (entitätsnah), 'global' (übergreifende Muster),
              'hybrid' (Standard), 'mix' (KG + Vektor), 'naive' (nur Vektor).
        only_context: DEFAULT True — liefert die rohen Kontext-Chunks/Entitäten,
              Claude formuliert die Antwort selbst. False lässt stattdessen das
              lokale LLM formulieren; auf geteilter GPU aktuell sehr langsam
              (>300 s -> MCP-Timeout), daher nur bewusst setzen.
        max_total_tokens: Kontext-Budget (Default 12000). Deckelt den
              only_context-Dump, damit er das MCP-Token-Limit nicht sprengt
              (Issue #2). Höher setzen für breitere Aggregationsfragen.
    """
    rag = await get_rag(project_id)
    param = QueryParam(
        mode=mode, only_need_context=only_context,
        max_total_tokens=max_total_tokens,
    )
    result = await rag.aquery(question, param=param)
    return str(result)


@mcp.tool()
async def ingest_paperless(
    project_id: str,
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
        project_id: technischer Projekt-Schlüssel (wird bei Bedarf angelegt).
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

    rag = await get_rag(project_id)
    manifest = _load_manifest(project_id)

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
            f"Projekt '{project_id}': nichts zu tun ({skipped} unverändert). "
            f"Gesamt im Index: {len(manifest)} Dokumente."
        )

    if _ingest_status.get(project_id, {}).get("state") == "running":
        return (
            f"Projekt '{project_id}': Ingest läuft bereits. "
            f'Fortschritt: ingest_status(project_id="{project_id}").'
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
                st = _ingest_status.get(project_id)
                if st and st.get("state") == "running":
                    st["msg"] = str(ps.get("latest_message") or "")[:160]
            except Exception:  # noqa: BLE001 — Status-Anzeige ist best effort
                pass
            await asyncio.sleep(3)

    async def _run():
        done = 0
        stop = asyncio.Event()
        poller = asyncio.create_task(_poll_msg(stop))
        # ponytail-Invariante: swapped == "ein _swap_begin ohne passendes _swap_end
        # offen". Jeder begin bekommt genau ein end (finally oder Pause), sonst
        # leckt der Refcount und die GPU bleibt bei qwen hängen.
        swapped = False
        try:
            # Swap läuft im Hintergrund-Task (nicht im MCP-Handler) -> kein
            # MCP-Timeout, während qwen lädt. Bei Swap-Fehler bricht der Ingest
            # kontrolliert ab; das finally swappt zurück.
            await _swap_begin()
            swapped = True
            for doc_key, text, h in pending:
                # Pause/Stop kooperativ ZWISCHEN zwei Dokumenten — das laufende
                # Doc wird fertig, Manifest ist je Doc gesichert (kein Datenverlust).
                while _ingest_control.get(project_id) == "pause":
                    if swapped:  # GPU freigeben: mistral zurück für paperless-ai
                        await _swap_end()
                        swapped = False
                    _ingest_status[project_id]["state"] = "paused"
                    await asyncio.sleep(1)
                if _ingest_control.get(project_id) == "stop":
                    _ingest_status[project_id] = {
                        "state": "stopped", "done": done, "total": total, "new": new,
                        "updated": updated, "skipped": skipped, "at": _now(),
                    }
                    break
                if not swapped:  # Resume nach Pause: qwen wieder laden
                    await _swap_begin()
                    swapped = True
                    _ingest_status[project_id]["state"] = "running"
                async with _insert_lock:
                    await rag.ainsert([text], ids=[doc_key])
                # Guard: Hash nur ins Manifest, wenn LightRAG das Doc wirklich
                # verarbeitet hat — sonst holt der nächste Ingest es nach.
                if _doc_state(project_id, doc_key) == "processed":
                    manifest[doc_key] = h
                    _save_manifest(project_id, manifest)
                else:
                    log.warning("Doc %s nicht 'processed' nach ainsert — bleibt für Re-Ingest offen", doc_key)
                done += 1
                _ingest_status[project_id]["done"] = done  # in-place: Poller-Feld bleibt
            else:
                # for..else: nur wenn NICHT via break gestoppt -> regulär fertig.
                _ingest_status[project_id] = {
                    "state": "done", "done": done, "total": total, "new": new,
                    "updated": updated, "skipped": skipped, "at": _now(),
                }
        except Exception as e:  # noqa: BLE001 — Status festhalten, nicht crashen
            log.exception("Ingest fehlgeschlagen für %s", project_id)
            _ingest_status[project_id] = {
                "state": "error", "error": str(e), "done": done, "total": total, "at": _now(),
            }
        finally:
            stop.set()
            poller.cancel()
            if swapped:
                await _swap_end()
            _ingest_control.pop(project_id, None)

    _ingest_control.pop(project_id, None)  # evtl. altes pause/stop verwerfen
    task = asyncio.create_task(_run())
    _ingest_status[project_id] = {
        "state": "running", "done": 0, "total": total, "new": new,
        "updated": updated, "skipped": skipped, "at": _now(), "_task": task,
    }
    return (
        f"Projekt '{project_id}': Ingest von {total} Dokumenten "
        f"({new} neu, {updated} aktualisiert, {skipped} übersprungen) "
        f"im Hintergrund gestartet — Extraktion läuft, das dauert. "
        f'Fortschritt: ingest_status(project_id="{project_id}").'
    )


@mcp.tool()
async def ingest_status(project_id: str) -> str:
    """Status des laufenden/letzten ingest_paperless-Laufs (läuft im Hintergrund)."""
    project_id = _validate_project(project_id)
    st = _ingest_status.get(project_id)
    docs = _doc_status_counts(project_id)
    if not st and not docs:
        return f"Projekt '{project_id}': kein Ingest bekannt (nichts gestartet oder Server neu gestartet)."
    out = {k: v for k, v in (st or {}).items() if not k.startswith("_")}
    # docs = echte LightRAG-Zustände: nur 'processed' heißt wirklich im Graph.
    # processing/pending/failed hier sichtbar, auch wenn state='done' (Dispatch fertig).
    if docs:
        out["docs"] = docs
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
async def ingest_control(project_id: str, action: str) -> str:
    """Steuert einen laufenden ingest_paperless-Lauf.

    'pause' hält nach dem aktuellen Dokument an und gibt die GPU frei (mistral
    zurück für paperless-ai). 'resume' lädt qwen neu und macht weiter. 'stop'
    bricht ab — bereits Indexiertes bleibt im Graph.

    Args:
        project_id: technischer Projekt-Schlüssel.
        action: 'pause' | 'resume' | 'stop'.
    """
    project_id = _validate_project(project_id)
    if action not in ("pause", "resume", "stop"):
        return "Fehler: action muss 'pause', 'resume' oder 'stop' sein."
    state = _ingest_status.get(project_id, {}).get("state")
    if state not in ("running", "paused"):
        return f"Projekt '{project_id}': kein laufender Ingest (Status: {state or 'keiner'})."
    if action == "resume":
        _ingest_control.pop(project_id, None)
    else:
        _ingest_control[project_id] = action
    return f"Projekt '{project_id}': '{action}' vorgemerkt — greift nach dem aktuellen Dokument."


def _extract_text(f: Path) -> str | None:
    """Textinhalt einer Datei. PDF via pdftotext (poppler, im Container installiert).
    None = nicht unterstütztes Format."""
    suffix = f.suffix.lower()
    if suffix in (".txt", ".md"):
        return f.read_text(errors="replace")
    if suffix == ".pdf":
        r = subprocess.run(
            ["pdftotext", "-layout", str(f), "-"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            log.warning("pdftotext fehlgeschlagen für %s: %s", f, r.stderr[:200])
            return None
        return r.stdout
    return None


@mcp.tool()
async def ingest_directory(project_id: str, subpath: str = "") -> str:
    """Indexiert lokale Dateien (.txt, .md, .pdf) aus /data/inputs/<subpath>
    in den Knowledge Graph. PDFs werden per pdftotext extrahiert (kein OCR —
    für gescannte Bilder Paperless als Quelle nutzen).

    Args:
        project_id: technischer Projekt-Schlüssel.
        subpath: Unterverzeichnis relativ zum gemounteten inputs-Volume.
    """
    base = (INPUTS_DIR / subpath).resolve()
    if not str(base).startswith(str(INPUTS_DIR)):
        return "Fehler: Pfad außerhalb des inputs-Volumes."
    if not base.exists():
        return (
            f"Fehler: {base} existiert nicht. inputs-Volume im docker-compose.yml "
            f"einkommentieren (- /host/pfad:/data/inputs:ro) und Container neu starten."
        )

    rag = await get_rag(project_id)
    manifest = _load_manifest(project_id)
    new, skipped, unsupported, texts, ids = 0, 0, 0, [], []

    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        content = _extract_text(f)
        if content is None:
            unsupported += 1
            continue
        text = f"Dokument: {f.name}\n\n" + content
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
        # ponytail: synchron -> der MCP-Call blockt, während qwen lädt (~min).
        # ingest_directory ist Nebenpfad (primär Paperless); falls störend, wie
        # ingest_paperless in einen Hintergrund-Task ziehen.
        await _swap_begin()
        try:
            async with _insert_lock:
                await rag.ainsert(texts, ids=ids)
            # Guard wie bei ingest_paperless: nur wirklich Verarbeitetes ins Manifest.
            for doc_key in ids:
                if _doc_state(project_id, doc_key) != "processed":
                    manifest.pop(doc_key, None)
                    log.warning("Doc %s nicht 'processed' nach ainsert — bleibt für Re-Ingest offen", doc_key)
            _save_manifest(project_id, manifest)
        finally:
            await _swap_end()

    return (
        f"Projekt '{project_id}': {new} Dateien indexiert, {skipped} unverändert, "
        f"{unsupported} ignoriert (nur .txt/.md/.pdf unterstützt)."
    )


@mcp.tool()
async def get_entity(project_id: str, entity_name: str) -> str:
    """Liefert Details und Beziehungen zu einer Entität im Graph
    (z. B. Person, Grundstück, Aktenzeichen).

    Args:
        project_id: technischer Projekt-Schlüssel (siehe list_projects).
        entity_name: Name der Entität.
    """
    rag = await get_rag(project_id)
    result = await rag.aquery(
        f"Nenne alle bekannten Fakten und Beziehungen zur Entität '{entity_name}', "
        f"chronologisch geordnet, mit Quellenbezug.",
        param=QueryParam(mode="local"),
    )
    return str(result)


def _get_project_name(project_id: str) -> str:
    """Liefert display_name (Fallback: project_id)."""
    meta = _load_meta(project_id)
    return meta.get("project_name") or project_id


def _render_project_graphs(current_id: str | None = None) -> tuple[int, int]:
    """Rendert alle Projekt-Graphen aus ihren .graphml-Dateien (keine LLM-Extraktion).
    Nutzt project_name aus Meta für Titel. Liefert (nodes, edges) für current_id,
    oder (-1, -1) wenn current_id keinen Graph hat. SYNCHRON (kein asyncio)."""
    import networkx as nx

    projs = sorted(
        p.name for p in PROJECTS_DIR.iterdir()
        if p.is_dir() and any(p.glob("*.graphml"))
    )
    names = {proj: _get_project_name(proj) for proj in projs}

    def _render(proj_id: str) -> tuple[int, int]:
        G = nx.read_graphml(str(next((PROJECTS_DIR / proj_id).glob("*.graphml"))))
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
        proj_name = names[proj_id]
        (PROJECTS_DIR / proj_id / "graph.html").write_text(
            graph_html(nodes, edges, f"KG: {proj_name}", projects=projs, current=proj_id, names=names),
            encoding="utf-8")
        return len(nodes), len(edges)

    n_nodes = n_edges = -1
    for p in projs:
        counts = _render(p)
        if p == current_id:
            n_nodes, n_edges = counts

    return (n_nodes, n_edges)


@mcp.tool()
async def graph_view(project_id: str) -> str:
    """Erzeugt eine interaktive HTML-Ansicht des Knowledge Graphs zum Durchklicken
    (Knoten = Entitäten, gefärbt nach Typ; Kanten = Beziehungen). Details erscheinen
    per Klick in einem Panel am unteren Rand. Gibt die URL zurück, die im Browser zu öffnen ist.

    Args:
        project_id: technischer Projekt-Schlüssel (siehe list_projects).
    """
    project_id = _validate_project(project_id)

    # Prüfe, ob Graph existiert
    if not any((PROJECTS_DIR / project_id).glob("*.graphml")):
        return f"Kein Graph für '{project_id}' gefunden — erst ingest_paperless/ingest_directory ausführen."

    n_nodes, n_edges = _render_project_graphs(project_id)
    url = f"http://{PUBLIC_HOST}:{VIEWER_PORT}/{project_id}/graph.html"
    return (f"Graph exportiert ({n_nodes} Entitäten, {n_edges} Beziehungen).\nÖffnen: {url}")


def _delete_project_dir(project: str) -> bool:
    """Löscht Index-Verzeichnis + gecachte Instanz (nicht die Quelldokumente).
    True, wenn etwas gelöscht wurde. Validiert gegen Path-Traversal."""
    _validate_project(project)
    _instances.pop(project, None)
    target = PROJECTS_DIR / project
    if target.exists():
        shutil.rmtree(target)
        return True
    return False


@mcp.tool()
async def rename_project(project_id: str, project_name: str) -> str:
    """Setzt/ändert den Anzeigenamen eines Projekts (display name).
    Der technische project_id (Storage-Key, Viewer-URL) bleibt unverändert.

    Args:
        project_id: technischer Projekt-Schlüssel (siehe list_projects).
        project_name: neuer Anzeigename.
    """
    project_id = _validate_project(project_id)
    if not (PROJECTS_DIR / project_id).exists():
        return f"Projekt '{project_id}' existiert nicht."
    meta = _load_meta(project_id)
    meta["project_name"] = project_name
    _save_meta(project_id, meta)
    return f"Projekt '{project_id}': Anzeigename gesetzt auf '{project_name}'."


@mcp.tool()
async def delete_project(project_id: str, confirm: bool = False) -> str:
    """Löscht einen kompletten Projekt-Index (nicht die Quelldokumente!).
    Erfordert confirm=True.

    Args:
        project_id: technischer Projekt-Schlüssel (siehe list_projects).
        confirm: Löschbestätigung (muss True sein).
    """
    _validate_project(project_id)
    if not confirm:
        return f"Sicherheitsabfrage: erneut mit confirm=True aufrufen, um '{project_id}' zu löschen."
    if _delete_project_dir(project_id):
        return f"Projekt '{project_id}' gelöscht."
    return f"Projekt '{project_id}' existiert nicht."


# ----------------------------------------------------------------------------
# Backup: tar.gz von PROJECTS_DIR nach BACKUP_DIR, Rotation + Scheduler.
# Nach Vorbild ai-rem (gleiche Dateinamen-Konvention, .config.json als Status).
# ponytail: unverschlüsselt — die Quelldokumente liegen im selben OneDrive
# ebenfalls im Klartext, ein Key schützte hier nichts.
# ----------------------------------------------------------------------------
BACKUP_INTERVALS = {"hourly": 3600, "daily": 86400, "weekly": 604800}
_BACKUP_CONFIG = BACKUP_DIR / ".config.json"
# Obergrenze für hochgeladene Restore-Archive (Projektdaten inkl. Embeddings).
MAX_RESTORE_UPLOAD = int(os.environ.get("MAX_RESTORE_UPLOAD", str(2 * 1024**3)))


def _load_backup_cfg() -> dict:
    try:
        return json.loads(_BACKUP_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {"enabled": True, "interval": "daily", "last_backup": None}


def _save_backup_cfg(cfg: dict) -> None:
    # ponytail: kein flock wie in ai-rem — hier schreiben nur Scheduler-Thread
    # und Viewer-Thread desselben Prozesses, atomares replace reicht.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _BACKUP_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(_BACKUP_CONFIG)


def _project_signature(project: str) -> dict:
    """Fingerabdruck EINES Projekts — ändert er sich nicht, ist ein neues Backup
    dieses Projekts sinnlos."""
    files = [p for p in (PROJECTS_DIR / project).rglob("*") if p.is_file()]
    return {
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "max_mtime": max((p.stat().st_mtime for p in files), default=0),
    }


def _existing_projects() -> list[str]:
    """Alle Projekt-Verzeichnisse (Storage-Keys) unter PROJECTS_DIR."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir())


def _project_backup_dir(project: str) -> Path:
    return BACKUP_DIR / project


def _list_project_backups(project: str) -> list[Path]:
    """Archive EINES Projekts, neueste zuerst."""
    return sorted(_project_backup_dir(project).glob("backup_*.tar.gz"), reverse=True)


def _do_backup_project(project: str) -> str:
    """Sichert EIN Projekt als tar.gz (Archiv-Wurzel = project_id, damit die Datei
    für sich allein wiederherstellbar ist), rotiert alte Stände weg."""
    project = _validate_project(project)
    bdir = _project_backup_dir(project)
    bdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"backup_{ts}.tar.gz"
    path = bdir / name

    tmp = path.with_suffix(".tar.gz.tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(PROJECTS_DIR / project, arcname=project)
    tmp.replace(path)

    cfg = _load_backup_cfg()
    cfg.setdefault("projects", {})[project] = {
        "last_backup": _now(), "signature": _project_signature(project),
    }
    _save_backup_cfg(cfg)

    for old in _list_project_backups(project)[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)

    log.info("Backup Projekt '%s': %s (%.1f MB)", project, name, path.stat().st_size / 1024 / 1024)
    return name


def _do_restore_project(project: str, name: str) -> None:
    """Restore eines gelisteten Projekt-Archivs (Name aus dem Projekt-Ordner)."""
    project = _validate_project(project)
    _restore_from_archive(_project_backup_dir(project) / name)


def _restore_from_archive(path: Path) -> str:
    """Spielt ein Projekt-Archiv zurück und legt das Projekt bei Bedarf NEU an.
    Archiv-Wurzel = project_id (Legacy: 'projects/' mit allen Projekten darin).
    Datenverlust-sicher — erst temp-extrahieren, dann der alte Stand je Projekt
    weggemovt (nicht gelöscht), bis der neue drin ist. Gibt die Projekt-IDs zurück."""
    tmp = PROJECTS_DIR.parent / ".restore_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(tmp, filter="data")  # Python 3.12 -> traversal-sicher
        tops = [p for p in tmp.iterdir() if p.is_dir()]
        # Legacy-Gesamtarchiv: Wurzel 'projects/' -> die enthaltenen Projekte.
        if len(tops) == 1 and tops[0].name == "projects":
            tops = [p for p in tops[0].iterdir() if p.is_dir()]
        if not tops:
            raise ValueError("Archiv enthält kein Projekt-Verzeichnis")
        restored = []
        for src in tops:
            project = _validate_project(src.name)  # Path-Traversal-Schutz
            dst = PROJECTS_DIR / project
            old = PROJECTS_DIR.parent / f".{project}_old"
            PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(old, ignore_errors=True)
            if dst.exists():
                dst.rename(old)  # alten Stand erst wegmoven, nicht löschen
            src.rename(dst)
            shutil.rmtree(old, ignore_errors=True)
            _instances.pop(project, None)  # gecachte Instanz zeigt auf alten Stand
            restored.append(project)
        log.info("Restore aus %s: %s", path.name, ", ".join(restored))
        return ", ".join(restored)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _backup_scheduler() -> None:
    """Prüft minütlich, ob je Projekt ein geplantes Backup fällig ist."""
    while True:
        time.sleep(60)
        try:
            cfg = _load_backup_cfg()
            if not cfg.get("enabled"):
                continue
            # Läuft ein Ingest, schreibt LightRAG gerade in die Stores — dann
            # gäbe das tar einen halben Stand. Nächster Tick versucht es erneut.
            if _active_ingests > 0:
                continue
            interval = BACKUP_INTERVALS.get(cfg.get("interval", "daily"), 86400)
            projs = cfg.get("projects", {})
            for project in _existing_projects():
                pm = projs.get(project, {})
                last = pm.get("last_backup")
                if last:
                    delta = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
                    if delta < interval:
                        continue
                if pm.get("signature") == _project_signature(project):
                    continue  # nichts geändert seit dem letzten Backup
                _do_backup_project(project)
        except Exception:  # noqa: BLE001 — Scheduler darf nie sterben
            log.exception("Geplantes Backup fehlgeschlagen")


class _ViewerHandler(SimpleHTTPRequestHandler):
    """Statischer Fileserver mit hübscher Landing-Page am Root statt rohem
    Dir-Listing. ponytail: nur '/', '/refresh', '/delete' abgefangen, Rest bleibt stdlib-static."""

    def do_GET(self):  # noqa: N802 (stdlib-Signatur)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            q = urllib.parse.parse_qs(parsed.query)
            notice = None
            if q.get("backup"):
                notice = f"backup:{q['backup'][0]}"
            elif q.get("restore"):
                notice = f"restore:{q['restore'][0]}"
            rendered = {
                p.name: (p / "graph.html").exists()
                for p in PROJECTS_DIR.iterdir()
                if p.is_dir() and any(p.glob("*.graphml"))
            }
            # laufende/erledigte Ingests ohne (noch) gerenderten Graph mit anzeigen
            for name in _ingest_status:
                rendered.setdefault(name, False)
            items = sorted(rendered.items())
            # ponytail: Meta-Daten nur für sichtbare Projekte laden
            meta = {}
            for proj_id in rendered.keys():
                if (PROJECTS_DIR / proj_id / "meta.json").exists():
                    meta[proj_id] = _load_meta(proj_id)
            # Backup-Archive je Projekt (neueste zuerst) für die Restore-Auswahl.
            project_backups = {
                p: [{"name": f.name, "size": f.stat().st_size} for f in _list_project_backups(p)]
                for p in rendered
            }
            # Anzahl indexierter Dokumente je Projekt aus dem Ingest-Manifest.
            counts = {p: len(_load_manifest(p)) for p in rendered}
            body = index_html(items, _ingest_status, meta, _load_backup_cfg(),
                              project_backups, notice, counts).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802 (stdlib-Signatur)
        if self.path == "/refresh":
            # Graph-Render aus vorhandenem .graphml (keine LLM-Extraktion)
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            project_id = (params.get("project_id") or [""])[0]
            try:
                _validate_project(project_id)
                _render_project_graphs(project_id)
            except ValueError:
                self.send_error(400, "invalid project_id")
                return
            self.send_response(303)
            self.send_header("Location", f"/{project_id}/graph.html")
            self.end_headers()
            return
        if self.path == "/rename":
            # Projekt umbenennen (project_name setzen)
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            project_id = (params.get("project_id") or [""])[0]
            project_name = (params.get("project_name") or [""])[0]
            try:
                _validate_project(project_id)
                meta = _load_meta(project_id)
                meta["project_name"] = project_name
                _save_meta(project_id, meta)
            except ValueError:
                self.send_error(400, "invalid project_id")
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/backup/now":
            if _active_ingests > 0:
                self.send_error(409, "Ingest laeuft — Backup waere unvollstaendig")
                return
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            project_id = (params.get("project_id") or [""])[0]
            try:
                _validate_project(project_id)
            except ValueError:
                self.send_error(400, "invalid project_id")
                return
            # Nur sichern, wenn sich das Projekt seit dem letzten Backup geändert hat.
            pm = _load_backup_cfg().get("projects", {}).get(project_id, {})
            if pm.get("signature") == _project_signature(project_id):
                self.send_response(303)
                self.send_header("Location", "/?backup=nochange")
                self.end_headers()
                return
            try:
                _do_backup_project(project_id)
            except Exception:  # noqa: BLE001 — Fehler gehört in die UI, nicht in einen 500er-Trace
                log.exception("Manuelles Backup fehlgeschlagen")
                self.send_error(500, "Backup fehlgeschlagen — siehe Server-Log")
                return
            self.send_response(303)
            self.send_header("Location", "/?backup=ok")
            self.end_headers()
            return
        if self.path == "/backup/restore-upload":
            # Restore aus per Datei-Dialog hochgeladenem Archiv (roher Body, kein
            # multipart — JS postet die Datei-Bytes direkt, siehe _backup_section).
            if _active_ingests > 0:
                self.send_error(409, "Ingest laeuft — Restore waere inkonsistent")
                return
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_RESTORE_UPLOAD:
                self.send_error(400, "leerer oder zu großer Upload")
                return
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            tmp = BACKUP_DIR / ".upload_restore.tar.gz"
            try:
                tmp.write_bytes(self.rfile.read(length))
                _restore_from_archive(tmp)  # liest project_id aus dem Archiv, legt bei Bedarf neu an
            except Exception:  # noqa: BLE001 — meist ungültige Datei; in die UI, nicht 500
                log.exception("Restore aus Upload fehlgeschlagen")
                tmp.unlink(missing_ok=True)
                self.send_error(400, "kein gültiges Backup-Archiv")
                return
            tmp.unlink(missing_ok=True)
            self.send_response(303)
            self.send_header("Location", "/?restore=ok")
            self.end_headers()
            return
        if self.path == "/backup/config":
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            interval = (params.get("interval") or [""])[0]
            if interval not in BACKUP_INTERVALS and interval != "off":
                self.send_error(400, "invalid interval")
                return
            cfg = _load_backup_cfg()
            cfg["enabled"] = interval != "off"
            if interval != "off":
                cfg["interval"] = interval
            _save_backup_cfg(cfg)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/backup/restore":
            if _active_ingests > 0:
                self.send_error(409, "Ingest laeuft — Restore waere inkonsistent")
                return
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            project_id = (params.get("project_id") or [""])[0]
            name = (params.get("name") or [""])[0]
            try:
                _validate_project(project_id)
            except ValueError:
                self.send_error(400, "invalid project_id")
                return
            if name not in {f.name for f in _list_project_backups(project_id)}:  # kein Path-Traversal
                self.send_error(400, "unbekanntes Backup")
                return
            try:
                _do_restore_project(project_id, name)
            except Exception:  # noqa: BLE001 — Fehler gehört in die UI, nicht in einen Trace
                log.exception("Restore fehlgeschlagen")
                self.send_error(500, "Restore fehlgeschlagen — siehe Server-Log")
                return
            self.send_response(303)
            self.send_header("Location", "/?restore=ok")
            self.end_headers()
            return
        if self.path == "/ingest/control":
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            project_id = (params.get("project_id") or [""])[0]
            action = (params.get("action") or [""])[0]
            try:
                _validate_project(project_id)
            except ValueError:
                self.send_error(400, "invalid project_id")
                return
            if action not in ("pause", "resume", "stop"):
                self.send_error(400, "invalid action")
                return
            # Nur setzen, wenn wirklich ein Ingest läuft/pausiert (sonst ignorieren).
            if _ingest_status.get(project_id, {}).get("state") in ("running", "paused"):
                if action == "resume":
                    _ingest_control.pop(project_id, None)
                else:
                    _ingest_control[project_id] = action
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/delete":
            # Projekt-Löschung; Bestätigung passiert im Browser (confirm-Dialog)
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            project_id = (params.get("project_id") or [""])[0]
            try:
                _delete_project_dir(project_id)
            except ValueError:
                self.send_error(400, "invalid project_id")
                return
            self.send_response(303)  # zurück zur Landing-Page
            self.send_header("Location", "/")
            self.end_headers()
            return
        self.send_error(404)


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
    threading.Thread(target=_backup_scheduler, daemon=True, name="backup-scheduler").start()
    log.info("Backup-Scheduler läuft (Ziel=%s, max=%s)", BACKUP_DIR, MAX_BACKUPS)

    # Safety-Net: hat ein Crash mitten im Ingest qwen geladen gelassen (finally
    # lief nicht), bliebe mistral gestoppt und paperless-ai ohne LLM. Beim Start
    # prüfen und zurückswappen. llm-qwen EXISTIERT immer als gestoppter Container
    # (llm-stack legt ihn an) — daher explizit auf status=running filtern.
    if SWAP_ENABLED:
        try:
            r = subprocess.run(
                ["docker", "ps", "-q", "-f", "name=^llm-qwen$", "-f", "status=running"],
                capture_output=True, text=True, timeout=30,
            )
            if r.stdout.strip():
                log.warning("qwen-Container aus früherem Ingest aktiv — swappe zurück auf mistral")
                _run_swap("swap-to-mistral.sh")
        except Exception:  # noqa: BLE001 — best effort, Start nicht blockieren
            log.exception("Startup-Swap-Cleanup fehlgeschlagen")

    mcp.run(transport="streamable-http")
