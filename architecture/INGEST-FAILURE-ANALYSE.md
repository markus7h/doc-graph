# Ingest-Failure-Analyse — warum ein Dokument scheitert

**Datum:** 2026-07-11
**Setup:** doc-graph, Tag `doc-graph`, LLM `qwen3:14b` (voll auf GPU, 100 %), Embed `bge-m3`

## Kurzfassung

Ein Dokument scheitert im Ingest, wenn die **LLM-Entity/Relation-Extraktion eines
Chunks den Worker-Timeout von 480 s überschreitet** → `WorkerTimeoutError` →
LightRAG markiert das ganze Dokument als `failed`. Es ist ein **Timeout**, kein
Parsing- oder Datenfehler.

## Der konkrete Fall

| Feld | Wert |
|---|---|
| Doc-ID | `paperless:11138` |
| Titel | „Erbengemeinschaft Thomanek Vergleich 2026" (Vergleich der Vergleichsvorschläge, April 2026) |
| Länge | 8.950 Zeichen, 3 Chunks |
| Gescheiterter Chunk | `paperless:11138-chunk-000` |
| `error`-Feld in doc_status | **`None` (leer!)** |
| Echte Ursache (nur im Log) | `extract LLM func: Worker execution timeout after 480s` |

Log-Auszug:

```
WARNING: extract LLM func: Worker timeout for task …_197760 after 480s
ERROR: Failed to extract entities and relationships:
       C[1/3]: paperless:11138-chunk-000: extract LLM func: Worker execution timeout after 480s
lightrag.utils.WorkerTimeoutError: Worker execution timeout after 480s
ERROR: Failed to extract document 2/28: unknown_source
```

## Warum genau dieses Dokument?

Es ist ein **inhaltsdichter Vergleichs-/Tabellen-Text** („Vergleich der
Vergleichsvorschläge"). Dichte, listen-/tabellenartige Inhalte erzeugen bei der
Extraktion sehr viele Entitäten/Relationen → sehr lange Generierung. qwen3:14b
schafft auf der GPU einfache Chunks in Sekunden (längste erfolgreiche Antwort im
Cache: 8.555 Zeichen), aber dieser eine Chunk lief >480 s und wurde abgebrochen.

**Ausgeschlossen:** Reasoning-Traces. Hypothese war, qwen3 („Thinking"-Modell)
verbrenne Zeit mit `<think>`-Ketten — der LLM-Response-Cache enthält aber
**0 Einträge mit `<think>`**. qwen3 denkt hier nicht laut, der Timeout kommt rein
aus der Extraktionsmenge/-dauer.

## Zwei Befunde über den Einzelfall hinaus

1. **Modellgröße war der Haupttreiber.** Mit mistral-small3.2:24b (38 % CPU-Offload)
   scheiterten **33 von ~76** Docs; mit qwen3:14b voll auf GPU nur **1** — der Rest
   der Failures waren also throughput-bedingte Timeouts, keine echten Datenprobleme.
2. **doc_status verschluckt den Fehlergrund.** Das `error`-Feld ist `None`; die
   Ursache steht ausschließlich im Ingest-Log. Deshalb sahen frühere Failures
   „grundlos" aus. → Kandidat für einen Fix: Fehlertext in doc_status persistieren
   (vgl. offene Issues).

## Gegenmittel (nach Aufwand)

- **Chunk-Größe senken** (`size=1200` → z. B. 600): weniger Entitäten pro Chunk,
  kürzere Extraktion — beseitigt genau diesen Timeout-Typ am wirksamsten.
- **Worker-Timeout erhöhen** (480 s → z. B. 900 s): lässt dichte Chunks
  durchlaufen, macht den Ingest aber insgesamt langsamer bei echten Hängern.
- **`error` in doc_status schreiben**, damit Failures ohne Log-Graben erklärbar sind.
- **MAX_ASYNC=1** für dichte Bestände: keine GPU-Teilung, mehr tok/s pro Chunk.
