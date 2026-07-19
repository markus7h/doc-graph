"""
doc-graph — Knowledge-Graph-MCP-Server (LightRAG, ein Container, N Projekte).

Pro Projekt liegt der LightRAG-Store (Graph + Vektoren + KV) unter
PROJECTS_DIR/<project>/ — realisiert über LightRAGs workspace=<project> auf
gemeinsamer working_dir-Wurzel, was zugleich den prozess-globalen shared_storage
je Projekt isoliert (sonst Cross-Projekt-Duplikat-Dedup). Instanzen lazy geladen.
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

from graphview import (
    edge_dict, graph_html, graph_subset, index_html, node_dict,
)

import numpy as np

# Gleaning (LightRAGs "hast du was übersehen?"-Nachfassrunde) aus: verdoppelt
# sonst die LLM-Calls pro Chunk für wenige Zusatz-Entitäten — auf der geteilten
# GPU der halbe Ingest-Durchsatz. MUSS vor dem lightrag-Import stehen (die
# Dataclass liest MAX_GLEANING beim Import). Via Compose-env überschreibbar.
os.environ.setdefault("MAX_GLEANING", "0")

from lightrag import LightRAG, QueryParam
from lightrag.llm.openai import openai_complete_if_cache
from lightrag.utils import EmbeddingFunc
from lightrag.kg.shared_storage import initialize_pipeline_status, get_namespace_data

from clauses import norm_clause, split_clauses

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
# Embedding-Robustheit: bge-m3 läuft auf CPU (GPU hat qwen). LightRAG-Defaults
# (max_async=8, timeout=30s) überfluten den CPU-Embedder -> Worker-Timeout ->
# IndexFlushError -> ganzes Doc failt, obwohl die Extraktion schon durch war.
# Weniger Parallelität + großzügigerer Timeout = robuste Einbettung.
EMBED_MAX_ASYNC = int(os.environ.get("EMBED_MAX_ASYNC", "3"))
EMBED_TIMEOUT = int(os.environ.get("EMBED_TIMEOUT", "180"))
# Timeout (s) für einen einzelnen LLM-Call an den llama-server. Bei CPU-Offload
# (niedriger t/s) reißen dichte Chunks den Default -> hier hochsetzen. Der
# eigentliche Engpass bleibt der Throughput (GPU), das ist nur der Deckel.
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "480"))
# Chunk-Größe (Tokens). Wieder LightRAG-Default 1200: der frühere 600er-Wert
# war ein Workaround gegen 480s-Worker-Timeouts bei CPU-Offload-Extraktion
# (siehe INGEST-FAILURE-ANALYSE.md) — mit Voll-GPU-qwen + Output-Deckel (-n)
# obsolet. Größere Chunks = halb so viele Extraktions-Calls (der ~3-4k-Token-
# Prompt-Overhead fällt PRO Chunk an). Wirkt nur auf neu indexierte Docs.
CHUNK_TOKEN_SIZE = int(os.environ.get("CHUNK_TOKEN_SIZE", "1200"))
# Docs pro ainsert-Batch. >1 lastet LightRAGs Chunk-Parallelität (MAX_ASYNC) auch
# bei vielen kleinen Docs aus; Pause/Stop greifen zwischen Batches. =1 stellt das
# alte, feingranulare Verhalten (Cancel/Fortschritt pro Doc) wieder her.
INGEST_BATCH = int(os.environ.get("INGEST_BATCH", "5"))
# Sicherheits-Guard: Dokumente über dieser Textlänge (Zeichen) werden NICHT
# ingestiert, sondern geflaggt (ingest_flagged.json) und dem Nutzer zur Prüfung
# vorgelegt. Schützt vor Datenmüll wie einem 50-MB-CSV-Export, der zehntausende
# Chunks erzeugt, den Graph flutet und stundenlang die GPU bindet.
# 300k Zeichen ~ 125 Chunks — großzügig über jedem echten Versicherungs-PDF.
MAX_DOC_CHARS = int(os.environ.get("MAX_DOC_CHARS", "300000"))
# Kontext-Budget je Query (Tokens). 12000 statt Default 30000: hält den
# only_context-Dump unter dem MCP-Token-Limit (Issue #2) und fokussiert.
QUERY_MAX_TOKENS = int(os.environ.get("QUERY_MAX_TOKENS", "12000"))
# Sprache der extrahierten Entitäten/Beschreibungen. LightRAG-Default ist
# "English" -> Graph-Einträge landen auf Englisch, obwohl die Docs deutsch sind.
GRAPH_LANGUAGE = os.environ.get("GRAPH_LANGUAGE", "German")
# Obergrenze gleichzeitig im Viewer geladener Entitäten. Der Graph kann tausende
# Knoten haben; vis.js-Physik wird darüber unbrauchbar langsam und das HTML riesig.
# Der /<proj>/nodes-Endpoint deckelt jedes Subset hierauf (Priorisierung: Knotengrad).
MAX_GRAPH_NODES = int(os.environ.get("GRAPH_MAX_NODES", "2500"))


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
# ponytail: ein wiederverwendeter Client statt pro Call ein neuer — spart TCP-/
# Pool-Aufbau je Embedding-Batch (LightRAG ruft das sehr oft). Lazy, da beim
# Import noch kein Event-Loop läuft.
_embed_client: httpx.AsyncClient | None = None


async def _embed_func(texts):
    # httpx-Timeout >= LightRAGs default_embedding_timeout, sonst schlägt der
    # Client zu, bevor LightRAG selbst tolerieren würde.
    global _embed_client
    if _embed_client is None:
        _embed_client = httpx.AsyncClient(timeout=EMBED_TIMEOUT)
    r = await _embed_client.post(
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


# ----------------------------------------------------------------------------
# Regelwerk-Projekte: klauselweises Chunking + deterministischer Klausel-Store
# ----------------------------------------------------------------------------
# clauses.json pro Projekt: doc_key -> {doc_title, clauses: {clause_id -> {title, text}}}.
# Geschrieben beim Ingest mit regelwerk=True, gelesen von get_clause — exakter
# Wortlaut ohne LLM/Retrieval, damit Klausel-Zitate nachprüfbar sind.
def _clauses_path(project: str) -> Path:
    return PROJECTS_DIR / project / "clauses.json"


def _load_clauses(project: str) -> dict:
    p = _clauses_path(project)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_clauses(project: str, store: dict) -> None:
    _clauses_path(project).write_text(json.dumps(store, indent=1, ensure_ascii=False))


def _clause_entry(doc_title: str, content: str) -> dict | None:
    """clauses.json-Eintrag für ein Dokument oder None (keine Klausel-Struktur)."""
    _pre, cls = split_clauses(content or "")
    if not cls:
        return None
    out: dict = {}
    for c in cls:
        cid = c["clause_id"]
        n = 2
        while cid in out:  # ponytail: doppelte §-Nummern (mehrere Werke in einem PDF) -> Suffix
            cid = f"{c['clause_id']} ({n})"
            n += 1
        out[cid] = {"title": c["title"], "text": c["text"]}
    return {"doc_title": doc_title, "clauses": out}


def _regelwerk_chunking(tokenizer, content, split_by_character=None,
                        split_by_character_only=False,
                        chunk_overlap_token_size=100, chunk_token_size=1200):
    """Chunking für Regelwerk-Projekte: ein Chunk = eine Klausel (+ Präambel als
    eigener Chunk). Docs ohne Klausel-Struktur (Anschreiben etc.) fallen aufs
    Standard-Token-Chunking zurück. Legacy-6-Arg-Signatur von LightRAG."""
    from lightrag.chunker import chunking_by_token_size

    preamble, cls = split_clauses(content)
    if not cls:
        return chunking_by_token_size(tokenizer, content, split_by_character,
                                      split_by_character_only,
                                      chunk_overlap_token_size, chunk_token_size)
    chunks: list[dict] = []

    def _add(text: str) -> None:
        toks = len(tokenizer.encode(text))
        if toks > chunk_token_size * 2:
            # überlange "Klausel" (z.B. Tabellenanhang) mit dem Standard-Chunker nachsplitten
            for s in chunking_by_token_size(tokenizer, text, None, False,
                                            chunk_overlap_token_size, chunk_token_size):
                chunks.append({"tokens": s["tokens"], "content": s["content"],
                               "chunk_order_index": len(chunks)})
        else:
            chunks.append({"tokens": toks, "content": text,
                           "chunk_order_index": len(chunks)})

    if preamble:
        _add(preamble)
    for c in cls:
        _add(c["text"])
    return chunks


def _ensure_regelwerk(project_id: str) -> None:
    """regelwerk-Flag in meta.json setzen — VOR get_rag, damit die LightRAG-
    Instanz mit Klausel-Chunking gebaut wird. Eine evtl. schon ohne Flag
    gebaute Instanz fliegt aus dem Cache."""
    (PROJECTS_DIR / project_id).mkdir(parents=True, exist_ok=True)
    meta = _load_meta(project_id)
    if not meta.get("regelwerk"):
        meta["regelwerk"] = True
        _save_meta(project_id, meta)
        _instances.pop(project_id, None)


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

        proj_dir = PROJECTS_DIR / project
        proj_dir.mkdir(parents=True, exist_ok=True)

        # Regelwerk-Projekte (Flag in meta.json): ein Chunk = eine Klausel.
        extra = (
            {"chunking_func": _regelwerk_chunking}
            if _load_meta(project).get("regelwerk") else {}
        )
        # workspace=project isoliert LightRAGs prozess-globalen shared_storage
        # (doc_status/full_docs/pipeline_status) je Projekt. Ohne das teilen sich
        # ALLE Projekte im selben Prozess den Default-Namespace "" -> ein Doc, das
        # schon in Projekt A ingestiert wurde, wird in Projekt B fälschlich als
        # Duplikat (filename/content_hash) still verworfen. working_dir bleibt die
        # PROJECTS_DIR-Wurzel: LightRAG legt seine Stores unter working_dir/<workspace>/
        # = PROJECTS_DIR/<project>/ ab — identisch zum bisherigen On-Disk-Layout,
        # daher keine Migration bestehender Indizes nötig.
        rag = LightRAG(
            working_dir=str(PROJECTS_DIR),
            workspace=project,
            llm_model_func=_llm_model_func,
            llm_model_name=LLM_MODEL,
            llm_model_max_async=MAX_ASYNC,
            chunk_token_size=CHUNK_TOKEN_SIZE,
            addon_params={"language": GRAPH_LANGUAGE},
            # CPU-Embedder nicht überfluten + großzügiger Timeout (siehe oben).
            embedding_func_max_async=EMBED_MAX_ASYNC,
            default_embedding_timeout=EMBED_TIMEOUT,
            embedding_func=EmbeddingFunc(
                embedding_dim=EMBED_DIM,
                max_token_size=8192,
                func=_embed_func,
            ),
            **extra,
        )
        await rag.initialize_storages()
        await initialize_pipeline_status(workspace=project)
        _instances[project] = rag
        log.info("LightRAG-Instanz für Projekt '%s' initialisiert (%s)", project, proj_dir)
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


# Geflaggte Dokumente: doc_key -> {title, chars, est_chunks, reason, at}.
# Vom Sicherheits-Guard beiseitegelegt (nicht ingestiert), bis der Nutzer
# entscheidet. Getrennt vom Manifest, damit ein Reingest sie nicht als
# "erledigt" behandelt.
def _flagged_path(project: str) -> Path:
    return PROJECTS_DIR / project / "ingest_flagged.json"


def _load_flagged(project: str) -> dict:
    p = _flagged_path(project)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_flagged(project: str, flagged: dict) -> None:
    _flagged_path(project).write_text(json.dumps(flagged, indent=1, ensure_ascii=False))


async def _purge_stuck_oversized(project_id: str, rag) -> None:
    """Altlasten-Guard: übergroße Docs, die aus früheren Läufen in LightRAGs
    doc_status-Pipeline hängen (pending/processing/failed), zieht LightRAG bei
    JEDEM ainsert wieder in die Verarbeitung — unabhängig vom Paperless-Tag. Der
    Sammel-Guard in ingest_paperless sieht sie nicht (nicht mehr im Tag-Query).
    Hier einmal per adelete_by_doc_id entfernen und flaggen, sonst frisst so ein
    Poison-Doc (z.B. ein 48-MB-CSV -> 39k Chunks) jeden Ingest-Lauf neu."""
    p = PROJECTS_DIR / project_id / "kv_store_doc_status.json"
    if not p.exists():
        return
    stuck = {
        k: v for k, v in json.loads(p.read_text()).items()
        if v.get("status") != "processed" and (v.get("content_length") or 0) > MAX_DOC_CHARS
    }
    if not stuck:
        return
    flagged = _load_flagged(project_id)
    changed = False
    for key, v in stuck.items():
        # 'approve' des Nutzers respektieren: bewusst freigegebene Docs bleiben in
        # der Pipeline (nicht löschen), auch wenn sie übergroß sind.
        if flagged.get(key, {}).get("decision") == "approve":
            continue
        clen = v.get("content_length") or 0
        try:
            async with _insert_lock:
                await rag.adelete_by_doc_id(key)
        except Exception:  # noqa: BLE001 — best effort; im Fehlerfall bleibt es hängen
            log.exception("adelete_by_doc_id(%s) fehlgeschlagen", key)
            continue
        flagged[key] = {
            "title": (v.get("content_summary") or key)[:80],
            "chars": clen,
            "est_chunks": clen // max(1, CHUNK_TOKEN_SIZE * 4),
            "reason": f"übergroß, aus hängender LightRAG-Pipeline entfernt (>{MAX_DOC_CHARS})",
            # decision behalten (z.B. 'ignore'), sonst 'open' = wartet auf Nutzer.
            "decision": flagged.get(key, {}).get("decision", "open"),
            "at": _now(),
        }
        changed = True
        log.warning("Poison-Doc %s (%d Zeichen) aus LightRAG entfernt und geflaggt", key, clen)
    if changed:
        _save_flagged(project_id, flagged)


def _doc_status_counts(project: str) -> dict:
    """Echte Terminal-Zustände je Dokument aus LightRAGs doc_status-Store
    (pending/processing/processed/failed). Der 'done'-Zustand der Dispatch-
    Schleife heißt nur 'enqueued', nicht 'im Graph' — die Wahrheit steht hier."""
    p = PROJECTS_DIR / project / "kv_store_doc_status.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return dict(Counter(v.get("status", "unknown") for v in data.values()))


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


# ----------------------------------------------------------------------------
# Gemeinsamer Ingest-Kern (von ingest_paperless UND ingest_directory genutzt)
# ----------------------------------------------------------------------------
def _doc_states(project: str, keys: list[str]) -> dict[str, str]:
    """Echte LightRAG-Zustände für mehrere doc_keys mit EINEM Datei-Read
    (statt _doc_state pro Doc -> O(n²) über den Lauf)."""
    p = PROJECTS_DIR / project / "kv_store_doc_status.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {k: data.get(k, {}).get("status", "") for k in keys}


def _prepare_doc(doc_key, text, clause_content, clause_title, is_regelwerk,
                 clause_store, manifest, flagged, counts):
    """Entscheidung für EIN Dokument (gemeinsam für beide Ingest-Pfade):
    aktualisiert clause_store/flagged/counts in-place und liefert
    (doc_key, text, hash) für die pending-Liste oder None (skip/flag)."""
    h = _hash(text)
    # Klausel-Store immer aktualisieren (auch unveränderte Docs) — heilt Projekte,
    # die vor dem regelwerk-Feature ingestiert wurden.
    if is_regelwerk:
        entry = _clause_entry(clause_title, clause_content)
        if entry:
            clause_store[doc_key] = entry
    if manifest.get(doc_key) == h:
        counts["skipped"] += 1
        return None
    # Sicherheits-Guard übergroße Docs. Nutzer-Entscheidung hat Vorrang:
    # 'approve' -> aufnehmen, 'ignore' -> still skippen, sonst flaggen & warten.
    if len(text) > MAX_DOC_CHARS:
        decision = flagged.get(doc_key, {}).get("decision", "open")
        if decision == "ignore":
            counts["skipped"] += 1
            return None
        if decision != "approve":
            flagged[doc_key] = {
                "title": clause_title or doc_key,
                "chars": len(text),
                "est_chunks": len(text) // max(1, CHUNK_TOKEN_SIZE * 4),
                "reason": f"Text {len(text)} Zeichen > MAX_DOC_CHARS ({MAX_DOC_CHARS})",
                "decision": "open",
                "at": _now(),
            }
            counts["flagged_new"] += 1
            return None
        # approve: durchfallen, Flag NICHT poppen (bleibt 'approved' sichtbar)
    else:
        flagged.pop(doc_key, None)  # klein genug -> Flag weg
    if doc_key in manifest:
        counts["updated"] += 1
    else:
        counts["new"] += 1
    # ponytail: Hash NICHT vorab ins Manifest — erst nach erfolgreichem Insert.
    return (doc_key, text, h)


async def _poll_ingest_msg(project_id: str, stop: asyncio.Event):
    # LightRAGs Live-Meldung ("Chunk 5 of 26 extracted …") in den Status spiegeln,
    # damit man Fortschritt auch INNERHALB eines Batches sieht.
    # ponytail: kooperatives asyncio -> läuft nie echt parallel zum Insert, kein Lock.
    while not stop.is_set():
        try:
            ps = await get_namespace_data("pipeline_status", workspace=project_id)
            st = _ingest_status.get(project_id)
            if st and st.get("state") == "running":
                st["msg"] = str(ps.get("latest_message") or "")[:160]
        except Exception:  # noqa: BLE001 — Status-Anzeige ist best effort
            pass
        await asyncio.sleep(3)


async def _run_ingest(project_id: str, rag, pending: list, counts: dict, manifest: dict) -> None:
    """Hintergrund-Insert für beide Ingest-Pfade: GPU-Swap, Pause/Stop, batchweises
    ainsert + per-Batch Manifest-Guard. Batchgröße INGEST_BATCH (=1 -> pro Doc).
    Pause/Stop greifen zwischen Batches; ein Batch wird immer ganz zu Ende geführt."""
    total = len(pending)
    done = 0
    stop = asyncio.Event()
    poller = asyncio.create_task(_poll_ingest_msg(project_id, stop))
    # ponytail-Invariante: swapped == genau ein offenes _swap_begin. Jeder begin
    # bekommt sein _swap_end (Pause/finally), sonst leckt der Refcount und die GPU
    # bleibt bei qwen hängen.
    swapped = False

    def _final(state):  # Endstatus mit aktuellem Zähler (done wird live gelesen)
        return {"state": state, "done": done, "total": total, "new": counts["new"],
                "updated": counts["updated"], "skipped": counts["skipped"], "at": _now()}

    try:
        # Altlasten-Guard vor dem ersten ainsert: hängende übergroße Docs raus,
        # sonst zieht LightRAG sie automatisch wieder in die Verarbeitung.
        await _purge_stuck_oversized(project_id, rag)
        # Swap im Hintergrund-Task (nicht im MCP-Handler) -> kein MCP-Timeout,
        # während qwen lädt. Bei Swap-Fehler bricht der Ingest ab; finally swappt zurück.
        await _swap_begin()
        swapped = True
        stopped = False
        i = 0
        while i < len(pending):
            # Pause hält zwischen Batches und gibt die GPU frei (mistral zurück).
            while _ingest_control.get(project_id) == "pause":
                if swapped:
                    await _swap_end()
                    swapped = False
                _ingest_status[project_id]["state"] = "paused"
                await asyncio.sleep(1)
            if _ingest_control.get(project_id) == "stop":
                _ingest_status[project_id] = _final("stopped")
                stopped = True
                break
            if not swapped:  # erster Lauf / Resume nach Pause: qwen laden
                await _swap_begin()
                swapped = True
                _ingest_status[project_id]["state"] = "running"

            batch = pending[i:i + INGEST_BATCH]
            keys = [k for k, _t, _h in batch]
            texts = [t for _k, t, _h in batch]
            # Insert als eigener Task, damit Stop/Pause ihn SOFORT (mitten im Batch)
            # canceln — sonst greift Stop erst nach dem Batch, bei großen Docs Minuten.
            # ponytail: hartes cancel() eines ainsert kann LightRAG-Teilzustand
            # hinterlassen; der _doc_states-Guard unten schützt das Manifest — ein
            # gecanceltes Doc bleibt 'nicht processed' und wird beim Re-Ingest neu geholt.
            interrupted = None
            async with _insert_lock:
                ins = asyncio.create_task(rag.ainsert(texts, ids=keys))
                while True:
                    finished, _ = await asyncio.wait({ins}, timeout=0.3)
                    if ins in finished:
                        ins.result()  # reguläres Ende / Fehler weiterreichen
                        break
                    if _ingest_control.get(project_id) in ("stop", "pause"):
                        interrupted = _ingest_control.get(project_id)
                        ins.cancel()
                        try:
                            await ins
                        except asyncio.CancelledError:
                            pass
                        break
            if interrupted == "pause":
                continue  # Batch nicht zählen -> Pause-Schleife oben hält, danach neu
            if interrupted == "stop":
                _ingest_status[project_id] = _final("stopped")
                stopped = True
                break
            # Guard: nur wirklich 'processed' Docs ins Manifest (ein Read je Batch).
            states = _doc_states(project_id, keys)
            wrote = False
            for k, _t, h in batch:
                if states.get(k) == "processed":
                    manifest[k] = h
                    wrote = True
                else:
                    log.warning("Doc %s nicht 'processed' nach ainsert — bleibt für Re-Ingest offen", k)
            if wrote:
                _save_manifest(project_id, manifest)
            done += len(batch)
            _ingest_status[project_id]["done"] = done  # in-place: Poller-Feld bleibt
            i += len(batch)
        if not stopped:
            _ingest_status[project_id] = _final("done")
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


def _start_ingest(project_id: str, rag, pending: list, counts: dict,
                  manifest: dict, flagged: dict, tail_note: str = "") -> str:
    """Gemeinsamer Abschluss beider Ingest-Tools: Flag-Hinweis, 'nichts zu tun' /
    'läuft schon', sonst Hintergrund-Task starten und Startmeldung liefern."""
    open_flags = sum(1 for v in flagged.values() if v.get("decision", "open") == "open")
    flag_note = (
        f" ⚠ {open_flags} übergroße(s) Dokument(e) warten auf Entscheidung — "
        f"im Viewer (Port {VIEWER_PORT}) aufnehmen/ignorieren." if open_flags else ""
    )
    if not pending:
        return (
            f"Projekt '{project_id}': nichts zu tun ({counts['skipped']} unverändert)."
            f"{tail_note} Gesamt im Index: {len(manifest)} Dokumente.{flag_note}"
        )
    if _ingest_status.get(project_id, {}).get("state") == "running":
        return (
            f"Projekt '{project_id}': Ingest läuft bereits. "
            f'Fortschritt: ingest_status(project_id="{project_id}").'
        )
    total = len(pending)
    _ingest_control.pop(project_id, None)  # evtl. altes pause/stop verwerfen
    task = asyncio.create_task(_run_ingest(project_id, rag, pending, counts, manifest))
    _ingest_status[project_id] = {
        "state": "running", "done": 0, "total": total, "new": counts["new"],
        "updated": counts["updated"], "skipped": counts["skipped"], "at": _now(), "_task": task,
    }
    return (
        f"Projekt '{project_id}': Ingest von {total} Dokumenten "
        f"({counts['new']} neu, {counts['updated']} aktualisiert, {counts['skipped']} übersprungen) "
        f"im Hintergrund gestartet — Extraktion läuft, das dauert.{tail_note} "
        f'Fortschritt: ingest_status(project_id="{project_id}").{flag_note}'
    )


@mcp.tool()
async def ingest_paperless(
    project_id: str,
    tag: str = "",
    document_type: str = "",
    correspondent: str = "",
    query_text: str = "",
    regelwerk: bool = False,
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
        regelwerk: True für Bedingungswerke/Verträge (AVB, Leistungspläne):
              klauselweises Chunking (ein Chunk = ein §) + Klausel-Store für
              get_clause. Das Flag bleibt am Projekt haften (meta.json).
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

    project_id = _validate_project(project_id)
    if regelwerk:
        _ensure_regelwerk(project_id)  # vor get_rag, sonst chunkt die Instanz normal
    is_regelwerk = regelwerk or _load_meta(project_id).get("regelwerk", False)
    clause_store = _load_clauses(project_id) if is_regelwerk else {}

    rag = await get_rag(project_id)
    manifest = _load_manifest(project_id)
    flagged = _load_flagged(project_id)

    counts = {"new": 0, "updated": 0, "skipped": 0, "flagged_new": 0}
    pending = []

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
            item = _prepare_doc(doc_key, text, doc.get("content") or "",
                                doc.get("title") or doc_key, is_regelwerk,
                                clause_store, manifest, flagged, counts)
            if item:
                pending.append(item)

    _save_flagged(project_id, flagged)
    if is_regelwerk and clause_store:
        _save_clauses(project_id, clause_store)

    return _start_ingest(project_id, rag, pending, counts, manifest, flagged)


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
    # Vom Sicherheits-Guard beiseitegelegte übergroße Dokumente — zur Nutzerprüfung.
    flagged = _load_flagged(project_id)
    if flagged:
        out["flagged"] = flagged
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
async def ingest_control(project_id: str, action: str) -> str:
    """Steuert einen laufenden ingest_paperless-Lauf.

    'pause' hält nach dem aktuellen Batch an und gibt die GPU frei (mistral
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
    return f"Projekt '{project_id}': '{action}' vorgemerkt — greift nach dem aktuellen Batch."


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
async def ingest_directory(project_id: str, subpath: str = "", regelwerk: bool = False) -> str:
    """Indexiert lokale Dateien (.txt, .md, .pdf) aus /data/inputs/<subpath>
    in den Knowledge Graph. PDFs werden per pdftotext extrahiert (kein OCR —
    für gescannte Bilder Paperless als Quelle nutzen). Läuft im Hintergrund
    (steuerbar via ingest_control/ingest_status) und kehrt sofort zurück.

    Args:
        project_id: technischer Projekt-Schlüssel.
        subpath: Unterverzeichnis relativ zum gemounteten inputs-Volume.
        regelwerk: True für Bedingungswerke/Verträge — klauselweises Chunking
              + Klausel-Store für get_clause (Flag haftet am Projekt, meta.json).
    """
    base = (INPUTS_DIR / subpath).resolve()
    if not str(base).startswith(str(INPUTS_DIR)):
        return "Fehler: Pfad außerhalb des inputs-Volumes."
    if not base.exists():
        return (
            f"Fehler: {base} existiert nicht. inputs-Volume im docker-compose.yml "
            f"einkommentieren (- /host/pfad:/data/inputs:ro) und Container neu starten."
        )

    project_id = _validate_project(project_id)
    if regelwerk:
        _ensure_regelwerk(project_id)  # vor get_rag, sonst chunkt die Instanz normal
    is_regelwerk = regelwerk or _load_meta(project_id).get("regelwerk", False)
    clause_store = _load_clauses(project_id) if is_regelwerk else {}

    rag = await get_rag(project_id)
    manifest = _load_manifest(project_id)
    flagged = _load_flagged(project_id)
    counts = {"new": 0, "updated": 0, "skipped": 0, "flagged_new": 0}
    pending, unsupported = [], 0

    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        content = _extract_text(f)
        if content is None:
            unsupported += 1
            continue
        text = f"Dokument: {f.name}\n\n" + content
        doc_key = f"file:{f.relative_to(INPUTS_DIR)}"
        item = _prepare_doc(doc_key, text, content, f.name, is_regelwerk,
                            clause_store, manifest, flagged, counts)
        if item:
            pending.append(item)

    _save_flagged(project_id, flagged)
    if is_regelwerk and clause_store:
        _save_clauses(project_id, clause_store)

    # Läuft jetzt (wie ingest_paperless) im Hintergrund + steuerbar via
    # ingest_control/ingest_status — kehrt sofort zurück.
    tail = f" {unsupported} ignoriert (nur .txt/.md/.pdf)." if unsupported else ""
    return _start_ingest(project_id, rag, pending, counts, manifest, flagged, tail_note=tail)


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


@mcp.tool()
async def get_clause(project_id: str, clause: str, document: str = "") -> str:
    """Exakter Wortlaut einer Klausel aus einem Regelwerk-Projekt — deterministisch
    aus dem Klausel-Store (kein LLM, kein Retrieval; zitierfähig und nachprüfbar).

    Args:
        project_id: technischer Projekt-Schlüssel (mit regelwerk=True ingestiert).
        clause: Klausel-Referenz, tolerant: '§ 2', '§2', '2', 'Artikel 3', 'Ziffer 4'.
        document: optionaler Substring-Filter auf den Dokumenttitel (wenn dieselbe
              §-Nummer in mehreren Bedingungswerken vorkommt).
    """
    project_id = _validate_project(project_id)
    store = _load_clauses(project_id)
    if not store:
        return (
            f"Projekt '{project_id}': kein Klausel-Store vorhanden — Projekt mit "
            f"regelwerk=True ingesten (ingest_paperless/ingest_directory)."
        )
    want_kind, want_num = norm_clause(clause)
    hits, available = [], []
    for doc_key, entry in sorted(store.items()):
        title = entry.get("doc_title", doc_key)
        if document and document.lower() not in title.lower():
            continue
        for cid, c in entry.get("clauses", {}).items():
            label = f"{cid} {c['title']}".strip() if c.get("title") else cid
            available.append(f"{title}: {label}")
            kind, num = norm_clause(cid)
            if num == want_num and (want_kind is None or kind == want_kind):
                hits.append(f"— {title} — {label}\n{c['text']}")
    if hits:
        return "\n\n".join(hits)
    if not available:
        return f"Kein Dokument passt auf document='{document}'."
    return f"Klausel '{clause}' nicht gefunden. Verfügbar:\n" + "\n".join(available)


def _get_project_name(project_id: str) -> str:
    """Liefert display_name (Fallback: project_id)."""
    meta = _load_meta(project_id)
    return meta.get("project_name") or project_id


# ----------------------------------------------------------------------------
# Graph-Viewer: live gedeckelte Knoten/Kanten aus dem GraphML
# ----------------------------------------------------------------------------
# Der Viewer bettet die Knoten nicht mehr komplett ein, sondern lädt sie per
# fetch über /<proj>/nodes — serverseitig auf MAX_GRAPH_NODES gedeckelt (Subset-
# Logik in graphview.graph_subset, stdlib-testbar). Das Parsen der GraphML ist der
# teure Teil und wird pro Projekt über die Datei-mtime gecacht (invalidiert
# automatisch, sobald ein Ingest/Restore die Datei ändert).
_graph_cache: dict[str, tuple[float, dict]] = {}


def _graphml_path(project_id: str) -> Path | None:
    return next((PROJECTS_DIR / project_id).glob("*.graphml"), None)


def _load_graph_data(project_id: str) -> dict | None:
    """Viewer-fertige, gecachte Graphdaten für ein Projekt oder None (kein Graph).
    Liefert {nodes:{id:node_dict}, edges:[edge_dict], adj:{id:set}, degree:{id:int}}.
    Cache-Key ist die graphml-mtime."""
    import networkx as nx

    path = _graphml_path(project_id)
    if path is None:
        return None
    mtime = path.stat().st_mtime
    cached = _graph_cache.get(project_id)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        G = nx.read_graphml(str(path))
    except Exception:  # noqa: BLE001 — z.B. halb geschriebene Datei während eines Ingests
        log.warning("GraphML für %s nicht lesbar (evtl. Ingest läuft)", project_id)
        return cached[1] if cached else None
    nodes = {n: node_dict(n, d) for n, d in G.nodes(data=True)}
    edges = [edge_dict(u, v, d) for u, v, d in G.edges(data=True)]
    adj: dict[str, set] = {n: set() for n in nodes}
    for e in edges:
        if e["from"] in adj:
            adj[e["from"]].add(e["to"])
        if e["to"] in adj:
            adj[e["to"]].add(e["from"])
    degree = dict(G.degree())
    gd = {"nodes": nodes, "edges": edges, "adj": adj, "degree": degree}
    _graph_cache[project_id] = (mtime, gd)
    return gd


def _graph_counts(project_id: str) -> tuple[int, int]:
    """(Anzahl Entities, Anzahl Kanten) für ein Projekt (0,0 ohne Graph)."""
    gd = _load_graph_data(project_id)
    if gd is None:
        return 0, 0
    return len(gd["nodes"]), len(gd["edges"])


def _render_project_graphs(current_id: str | None = None) -> tuple[int, int]:
    """Schreibt für jedes Projekt die Viewer-Shell (graph.html); die Knoten/Kanten
    lädt der Browser live über /<proj>/nodes (serverseitig auf MAX_GRAPH_NODES
    gedeckelt). Liefert die Gesamtzahl (nodes, edges) für current_id, oder (-1, -1)
    wenn current_id keinen Graph hat. SYNCHRON (kein asyncio)."""
    projs = sorted(
        p.name for p in PROJECTS_DIR.iterdir()
        if p.is_dir() and any(p.glob("*.graphml"))
    )
    names = {proj: _get_project_name(proj) for proj in projs}

    def _render(proj_id: str) -> tuple[int, int]:
        proj_name = names[proj_id]
        (PROJECTS_DIR / proj_id / "graph.html").write_text(
            graph_html(f"KG: {proj_name}", projects=projs, current=proj_id, names=names),
            encoding="utf-8")
        return _graph_counts(proj_id)

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
    _ingest_status.pop(project, None)  # sonst bleibt eine Phantom-Karte bis zum Neustart
    _ingest_control.pop(project, None)  # altes stop/pause-Flag würde ein neues gleichnamiges Projekt treffen
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
async def delete_documents(project_id: str, doc_keys: list[str] | None = None,
                           only_failed: bool = False) -> str:
    """Entfernt einzelne Dokumente aus dem Graph-Index (Chunks, Entitäten, Vektoren,
    doc_status) via LightRAGs adelete_by_doc_id. Zum Aufräumen von dup-Leichen oder
    Artefakt-Failures, die der Oversized-Guard (_purge_stuck_oversized) nicht erfasst.
    Das Quelldokument (Paperless) bleibt unberührt.

    Args:
        project_id: technischer Projekt-Schlüssel (siehe list_projects).
        doc_keys: doc_status-Schlüssel, z.B. ["paperless:4193", "dup-ab12…"].
        only_failed: statt doc_keys ALLE Dokumente mit status=='failed' löschen.
    """
    project_id = _validate_project(project_id)
    if not (PROJECTS_DIR / project_id).exists():
        return f"Projekt '{project_id}' existiert nicht."
    if only_failed:
        p = PROJECTS_DIR / project_id / "kv_store_doc_status.json"
        data = json.loads(p.read_text()) if p.exists() else {}
        doc_keys = [k for k, v in data.items() if v.get("status") == "failed"]
    doc_keys = doc_keys or []
    if not doc_keys:
        return "Nichts zu löschen (keine doc_keys angegeben bzw. keine failed-Docs)."
    rag = await get_rag(project_id)
    deleted, errored = 0, []
    for k in doc_keys:
        try:
            async with _insert_lock:
                await rag.adelete_by_doc_id(k)
            deleted += 1
        except Exception as e:  # noqa: BLE001 — pro Key tolerant, Rest weiterlöschen
            log.warning("delete_documents: %s fehlgeschlagen: %s", k, e)
            errored.append(k)
    msg = f"Projekt '{project_id}': {deleted}/{len(doc_keys)} Dokument(e) gelöscht"
    if errored:
        msg += f", {len(errored)} fehlgeschlagen ({', '.join(errored[:5])}…)"
    return msg + ". Quelldokumente unberührt."


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
            # Entitäten/Kanten je gerendertem Projekt (aus dem gecachten Graphen).
            graph_counts = {p: gc for p, has in rendered.items() if has
                            and (gc := _graph_counts(p)) != (0, 0)}
            # Übergroße, vom Sicherheits-Guard beiseitegelegte Docs zur Entscheidung.
            flagged = {p: f for p in rendered if (f := _load_flagged(p))}
            # Echte LightRAG-Zustände in den Status mischen (Kopie, nicht mutieren):
            # die UI zeigt Fortschritt an 'processed', nicht am Dispatch-Zähler 'done'.
            status_view = {}
            for name, s in _ingest_status.items():
                sc = {k: v for k, v in s.items() if not k.startswith("_")}
                if s.get("state") in ("running", "paused"):
                    sc["docs"] = _doc_status_counts(name)
                status_view[name] = sc
            body = index_html(items, status_view, meta, _load_backup_cfg(),
                              project_backups, notice, counts, flagged,
                              graph_counts).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Live-Graph-API: /<proj>/nodes -> serverseitig gedeckeltes JSON-Subset,
        # das der Viewer per fetch lädt (statt eines eingebetteten Voll-Payloads).
        m = re.match(r"^/([^/]+)/nodes$", parsed.path)
        if m:
            self._serve_nodes(urllib.parse.unquote(m.group(1)), parsed.query)
            return
        super().do_GET()

    def _serve_nodes(self, project_id: str, query: str):
        """Antwortet mit dem gedeckelten Knoten/Kanten-JSON für den Viewer."""
        try:
            _validate_project(project_id)
        except ValueError:
            self.send_error(400, "invalid project_id")
            return
        gd = _load_graph_data(project_id)
        if gd is None:
            self.send_error(404, "kein Graph fuer dieses Projekt")
            return
        q = urllib.parse.parse_qs(query)

        def _int(name: str, default: int) -> int:
            try:
                return int((q.get(name) or [str(default)])[0])
            except (ValueError, TypeError):
                return default

        focus = (q.get("focus") or [""])[0] or None
        term = (q.get("q") or [""])[0] or None
        hide = {t for t in (q.get("hide") or [""])[0].split(",") if t}
        limit = max(1, min(_int("limit", MAX_GRAPH_NODES), MAX_GRAPH_NODES))
        sub = graph_subset(
            gd["nodes"], gd["edges"], gd["adj"], gd["degree"],
            limit=limit, focus=focus, depth=_int("depth", 1), q=term, hide=hide,
        )
        body = json.dumps(sub).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if self.path == "/flagged/decide":
            # Nutzerentscheidung zu einem geflaggten (übergroßen) Dokument:
            # approve = trotz Größe aufnehmen, ignore = dauerhaft ausblenden,
            # open = zurücksetzen (wieder unentschieden). Greift beim nächsten Ingest.
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            project_id = (params.get("project_id") or [""])[0]
            doc_key = (params.get("doc_key") or [""])[0]
            decision = (params.get("decision") or [""])[0]
            try:
                _validate_project(project_id)
            except ValueError:
                self.send_error(400, "invalid project_id")
                return
            if decision not in ("open", "approve", "ignore"):
                self.send_error(400, "invalid decision")
                return
            flags = _load_flagged(project_id)
            if doc_key in flags:
                flags[doc_key]["decision"] = decision
                _save_flagged(project_id, flags)
            self.send_response(303)
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
                # name-Filter sind OR, status ist AND: qwen ODER embed-gpu läuft noch.
                # Deckt auch den Restspalt "nur embed-gpu verwaist, qwen schon aus".
                ["docker", "ps", "-q", "-f", "name=^llm-qwen$",
                 "-f", "name=^llm-embed-gpu$", "-f", "status=running"],
                capture_output=True, text=True, timeout=30,
            )
            if r.stdout.strip():
                log.warning("Swap-Container (qwen/embed-gpu) aus früherem Ingest aktiv — swappe zurück auf mistral")
                _run_swap("swap-to-mistral.sh")
        except Exception:  # noqa: BLE001 — best effort, Start nicht blockieren
            log.exception("Startup-Swap-Cleanup fehlgeschlagen")

    mcp.run(transport="streamable-http")
