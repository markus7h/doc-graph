# Ingest-Befunde — 2026-07-13

**Kontext:** Beim Aufnehmen eines neuen Dokuments („Vergleichsangebot Fehmarn 2026",
`paperless:11335`) in Projekt `doc-graph` traten drei unabhängige Probleme auf. Ergänzt
`INGEST-FAILURE-ANALYSE.md` (2026-07-11), deren Kernbefund (Chunk-Timeout) hier bestätigt und
um Tooling-Befunde erweitert wird.

## Zusammenfassung

| # | Befund | Schwere | Kern-Fix |
|---|---|---|---|
| A | `ingest_directory` praktisch unbenutzbar (kein Mount, kein PDF) | mittel | Mount + PDF-Extraktion |
| B | `ingest_status` meldet `done`, obwohl Doc `pending`/`failed` | **hoch** | Status an echten Terminal-Zustand koppeln |
| C | Timeout→Reset→Retry-Schleife blockiert Pipeline | **hoch** | Timeout/Throughput + Failure-Deckel |

---

## A. `ingest_directory` unbrauchbar für den realen Fall

`server.py:448` `ingest_directory(project_id, subpath)`:

1. **Kein Input-Mount.** `INPUTS_DIR = Path("/data/inputs")` (`server.py:91`), aber im Compose ist
   die Mount-Zeile auskommentiert:
   ```yaml
   # - /home/markus/dokumente:/data/inputs:ro
   ```
   → `base.exists()` False → `Fehler: /data/inputs existiert nicht.` Der Weg ist tot, egal was drin liegt.
2. **Kein PDF-Support.** `server.py:467` nimmt nur `.txt`/`.md`:
   ```python
   if f.suffix.lower() not in (".txt", ".md") or not f.is_file():
       continue
   ```
   PDFs werden **still übersprungen**. Ein magic3-PDF hätte auch mit Mount nichts erzeugt.
3. **Keine Diagnostik.** Bei 0 Treffern kommt `0 Dateien indexiert` ohne Hinweis „N nicht
   unterstützte Dateien ignoriert".

**Fix:**
- Compose-Mount einkommentieren, auf realen Eingangsordner zeigen.
- PDF-Extraktion via poppler/`pdftotext` einbauen (ist im tools-Server als `pdf_to_text` vorhanden)
  oder zumindest `.html` akzeptieren.
- Übersprungene Nicht-Text-Dateien zählen und in der Rückgabe ausweisen.

**Workaround (genutzt):** Paperless-Pipeline — PDF via `/api/documents/post_document/` mit Tag
`dx: Erbe Papa` (ID 82) hochgeladen, Consume abgewartet, dann `ingest_paperless(tag=...)`.

---

## B. `ingest_status` ist von der echten Extraktion entkoppelt

`ingest_paperless` lieferte „1 neu, 0 aktualisiert, 28 übersprungen", und `ingest_status` meldete:
```json
{"state":"done","done":1,"total":1,"new":1,"skipped":28}
```
Zeitgleich stand in `kv_store_doc_status.json` aber:
```
paperless:11335  Vergleichsangebot Fehmarn 2026  → pending
```
`state=done` bezieht sich also auf die **Enqueue-/Dispatch-Schleife**, nicht auf den
LightRAG-Extraktionszustand pro Dokument. Ein „done" heißt nicht „im Graph".

**Folge:** Falsche Erfolgsmeldung nach außen — das Doc kann pending sein oder (wie in der
Failure-Analyse) still auf `failed` laufen, ohne dass `ingest_status` das zeigt.

**Fix:** `ingest_status` (und Rückgabetext von `ingest_paperless`) an die tatsächlichen
Terminal-Zustände `processed`/`failed` je Doc koppeln, inkl. Zählung `failed`.

---

## C. Timeout→Reset→Retry-Schleife blockiert die Pipeline

Bestätigt den Kernbefund der Failure-Analyse (480 s-`WorkerTimeoutError` auf dichten Chunks),
zeigt aber ein **Stall-Muster** im Live-Betrieb. Log-Auszug:
```
WARNING: extract LLM func: Worker timeout ... after 480s
ERROR:   Failed to extract entities and relationships:
         C[1/6]: paperless:689-chunk-001: ... timeout after 480s
INFO:    Reset 1 documents from PARSING/ANALYZING/PROCESSING/FAILED to PENDING status
INFO:    Parsing (native): paperless:11335
INFO:    Chunking F(legacy): size=600 ...
```
`paperless:689` (dichtes Vertragsdokument) reißt den Timeout, wird auf `PENDING` zurückgesetzt,
erneut versucht — und blockiert dabei das nachgelagerte `paperless:11335`, das dauerhaft `pending`
bleibt und **nicht im Graph landet**.

Zwei Verschärfungen gegenüber dem Stand vom 11.07.:
1. **Chunk-Fix ist deployed** (`size=600` im Log) — reicht aber nicht mehr, weil …
2. … **Throughput eingebrochen** ist: seit Migration auf llama.cpp/mistral-small3.2 generiert der
   Server mit **~5,8 t/s** (`tg = 5.80 t/s`) — CPU-Offload-Niveau. Dichte Chunks laufen damit
   trotz 600er-Chunks weiter in die 480 s.

**Fix (nach Wirkung):**
- **Throughput zuerst:** mistral-small3.2 voll auf GPU bringen (5,8 t/s ist zu langsam), sonst
  bleibt jeder andere Fix Symptombehandlung.
- **Failure-Deckel:** nach N Fehlversuchen ein Doc endgültig `failed` markieren statt endlos
  `→ PENDING` zu resetten (Poison-Doc darf den Rest nicht blockieren).
- **Worker-Timeout hoch** (480→900 s) und/oder **MAX_ASYNC=1** für dichte Bestände.
- **`error` in `doc_status` persistieren** (Analyse-Punkt, Feld ist weiterhin `None`).

---

## Aktueller Stand (13.07.2026, Ende der Session)
- `paperless:11335` = `pending`, **nicht im Graph** — hängt hinter `paperless:689` (Timeout-Loop).
- `paperless:689` = `processing`/Retry, reißt wiederholt den 480 s-Timeout.
- Nächster Schritt: Throughput/Failure-Deckel (C) zuerst; danach `11335` re-ingesten.
