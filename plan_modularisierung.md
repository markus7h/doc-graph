---
name: doc-graph Ingest modularisieren + beschleunigen
description: Duplizierung zwischen ingest_paperless/ingest_directory über gemeinsamen _prepare_doc + _run_ingest-Kern entfernen, directory angleichen, Embed-Client wiederverwenden, Doc-Status/Manifest batchen; plus messgetriebene LLM-Parameter-Überprüfung
status: offen
---

# doc-graph Ingest: modularisieren + beschleunigen

## Context
Der Insert-Pfad in `server.py` (~1635 Zeilen) ist monolithisch: `ingest_paperless`
(`:569`) und `ingest_directory` (`:889`) implementieren dieselbe Sequenz
(Regelwerk-Setup, Hash/Manifest-Dedup, Klausel-Store, „processed"-Guard, GPU-Swap)
getrennt. `ingest_directory` hat keinen MAX_DOC_CHARS-Guard, keine Pause/Stop, läuft
synchron und blockiert den MCP-Call. Ziel: **ein gemeinsamer Doc-Insert-Kern**,
`ingest_directory` auf denselben Hintergrund-Loop heben (Scope 2), und dabei die
größten Geschwindigkeitsbremsen entfernen.

LightRAG-Kopplung (`kv_store_doc_status.json`) bleibt bewusst wie sie ist
(Scope 3 ausgeschlossen — YAGNI).

**Vorab geprüft/erledigt:** Boilerplate-Preprocessing gemessen und **verworfen**
(2–4 % Chunk-Reduktion, unter Schwelle → kein Speed-Hebel).

---

## Teil A — Modularisierung + App-seitiger Speed (server.py)

### A1. Gemeinsamer Vorbereitungs-Kern `_prepare_doc(...)`
Extrahiert die pro-Dokument-Entscheidung, die aktuell in beiden Schleifen dupliziert
ist (`:636-675` vs. `:929-941`):
```
def _prepare_doc(project_id, doc_key, text, clause_content, clause_title,
                 is_regelwerk, clause_store, manifest, flagged, counts) -> tuple | None
```
Kapselt: Klausel-Store-Update (`_clause_entry`), Hash + Manifest-Skip, MAX_DOC_CHARS-
Flag-Guard (approve/ignore/open), new/updated-Zählung. Rückgabe = `(doc_key, text, h)`
für pending oder `None` (skip/flag). Beide Tools rufen das im Loop.
→ Nebeneffekt: `ingest_directory` erhält Größen-Guard + Flag-Logik gratis.

### A2. Gemeinsamer Hintergrund-Runner `_run_ingest(...)` mit Batching
`_run()` (`:718-810`) wird zur modulweiten Funktion:
```
async def _run_ingest(project_id, rag, pending, counts) -> None
```
Übernimmt `_purge_stuck_oversized`, Poller, Swap-Refcount, Pause/Stop, per-Doc-
Manifest-Guard, Endstatus. `ingest_paperless` **und** `ingest_directory` starten
denselben `asyncio.create_task(_run_ingest(...))` und kehren sofort zurück
(directory wird async + steuerbar).

**Speed — Batching:** LightRAG parallelisiert Extraktion über Chunks bis `MAX_ASYNC`.
Kleine Docs lasten das einzeln nicht aus. `_run_ingest` inserted in Batches von
`INGEST_BATCH` (Env, Default z.B. 5): `rag.ainsert(batch_texts, ids=batch_keys)`
unter `_insert_lock`. Pause/Stop greifen zwischen Batches. `INGEST_BATCH=1` stellt das
alte feingranulare Verhalten wieder her. Der mid-doc-`cancel()`-Block (`:766-787`)
entfällt.

### A3. Doc-Status/Manifest pro Batch statt pro Doc
`_doc_state()` (`:449`) parst `kv_store_doc_status.json` bei **jedem** Doc → O(n²).
In `_run_ingest` die Datei **einmal pro Batch** lesen (`_doc_states(project, keys) ->
dict`), Manifest ebenfalls einmal pro Batch speichern statt pro Doc (`:792`).

### A4. Embed-Client wiederverwenden
`_embed_func` (`:114`) baut pro Call einen neuen `httpx.AsyncClient`. Modulweiten,
lazy erzeugten Client wiederverwenden. Spürbar bei vielen Chunks.

---

## Teil B — LLM-Parameter-Überprüfung (messgetrieben, llm-stack)

Engpass ist die Extraktions-Laufzeit auf der geteilten GPU (qwen3-14b via Swap).
`llm-qwen` startet aktuell (Container-Inspect):
`-hf Qwen3-14B-GGUF:Q5_K_M -c 16384 -n 4096 -fa on -ngl 99 -t 6 -np 1 --reasoning-budget 0 --cache-reuse 256`

**Bereits gesetzt (nur dokumentieren):** `--reasoning-budget 0` (qwen3-Thinking AUS —
großer Hebel schon gezogen), `-fa on`, `-ngl 99`, `--cache-reuse 256`, Gleaning=0
(server.py:44), CHUNK_TOKEN_SIZE=1200.

**Baseline zuerst:** fixes Doc-Set (20–30 Docs), einmal ingestieren, Wall-Time +
Docs/Chunks aus `ingest_status`/Logs festhalten. Jeder Hebel wird gegen diese Baseline
auf **demselben** Set gemessen (Manifest vorher leeren).

**Hebel nach erwarteter Wirkung:**
1. **`-np 1 → 2`** (größter offener). Ein Slot = kein Continuous-Batching → App-seitiges
   `MAX_ASYNC=2` verpufft (Calls queuen). `-np 2` = echte 2-fach-Parallelität, teilt
   aber `-c` (16384 → 8192/Slot; für Extraktion ~8k grenzwertig, auf Truncation prüfen).
   `MAX_ASYNC` ≥ `-np` halten. Wirkt v.a. bei vielen kleinen Docs.
2. **Quant `Q5_K_M → Q4_K_M`** — mehr t/s, geringer Qualitätsverlust; schafft VRAM für `-np 2`.
3. **`CHUNK_TOKEN_SIZE 1200 → ~1800`** — Prompt-Overhead fällt pro Chunk an; +50 % ≈
   –30 % Calls. Ceiling: Chunk+Overhead+Output muss in `-c/np` passen, Recall prüfen.
4. **`-c 16384` Sizing** — nur so groß wie nötig (Extraktion ~8k; `only_context=False`-
   Query ~13k, selten). Größer macht **nicht** schneller (KV-VRAM ↑, Offload-Risiko).

**Befund je Hebel:** übernehmen bei ≥ ~15 % Wall-Time-Gewinn **und** unveränderter
Qualitäts-Stichprobe (`query`/`get_clause` auf bekannte Fakten). Sonst zurückdrehen.

---

## Dateien
- `/home/markus/mystorage/myCode/github/doc-graph/server.py` — Teil A (`_prepare_doc`,
  `_run_ingest`, `_doc_states`, `_embed_func`), ggf. `CHUNK_TOKEN_SIZE`/`MAX_ASYNC` (Teil B).
- `/var/local/mydocker/llm-stack/docker-compose.yml` — qwen-Service (`-np`, Quant, `-c`).
- `README.de.md`/`README.md` — neue Env (`INGEST_BATCH`), directory-Verhaltensänderung,
  geänderte Parameter (Regel: README im selben Commit).

## Wiederverwendete Bausteine (nicht neu bauen)
`_clause_entry` `:247`, `_hash` `:459`, `_load/_save_manifest` `:365/:372`,
`_load/_save_flagged`, `_swap_begin/_end` `:189/:200`, `_purge_stuck_oversized` `:393`,
`_doc_status_counts` `:438`, `_ensure_regelwerk` `:297`, `clauses.split_clauses`.

## Verifikation
- `python3 -c "import server"` (Syntax), `pytest test_clauses.py` (muss grün bleiben).
- Selbstcheck für `_prepare_doc` (assert-basiert, `test_prepare.py`): skip bei gleichem
  Hash, pending bei neu, flag bei >MAX_DOC_CHARS ohne approve.
- End-to-end (Teil A) mit `INGEST_SWAP=0`: `ingest_directory` → sofortige Rückgabe,
  `ingest_status` running→done; `ingest_control` pause/resume/stop; Re-Ingest → skipped.
- Teil B: pro Parameteränderung Container neu, `docker inspect llm-qwen` bestätigen,
  fixes Set re-ingestieren, Wall-Time + `ingest_status.docs` gegen Baseline; llm-qwen-Log
  auf `context shift`/`truncat` prüfen.
