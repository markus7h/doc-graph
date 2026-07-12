# doc-graph — Knowledge Graph pro Projekt als MCP-Server

Ein Container auf `myubuntu` (RTX 5080), der pro "Kontext"/Projekt einen
abfragbaren Knowledge Graph (LightRAG: Graph + Vektoren, hybrid) bereithält und
ihn als MCP-Server für Claude Code exponiert. Dokumentquelle ist primär
Paperless-NGX — der OCR-Text kommt fertig über die REST-API, die kuratierten
Metadaten (Korrespondent, Datum, Dokumenttyp, Schlagworte/Tags) wandern als
Fakten mit in den Graph und werden so zu abfragbaren Knoten. Ein
mitgelieferter Graph-Viewer macht den Graphen im Browser durchklickbar.

## Architektur

```
Claude Code ──MCP (streamable HTTP :5775)──> doc-graph ──> Ollama (Extraktion + Embeddings)
Browser     ──HTTP  (Graph-Viewer   :5776)──> doc-graph ──> Paperless-NGX API (Dokumentquelle)

doc-graph
├─ Projekt "fehmarn"   → /data/projects/fehmarn/
├─ Projekt "rabot"     → /data/projects/rabot/
└─ Projekt "silbersee" → /data/projects/silbersee/
```

Bewusste Entscheidung: **LightRAG als Library, nicht als LightRAG-Server.**
Der offizielle LightRAG-Server bindet einen Workspace fest pro Prozess —
Multi-Projekt hieße dort ein Container pro Projekt. Hier verwaltet der
MCP-Server stattdessen selbst eine lazy geladene LightRAG-Instanz pro
`working_dir`, Projektname ist einfach ein Tool-Parameter.

## Modell: geteiltes mistral-small3.2:24b

Extraktion und Embeddings laufen über das **mit paperless-ai geteilte Ollama**
(`paperless-ollama`). Genutzt wird `mistral-small3.2:24b` (Extraktion) + `bge-m3`
(Embeddings, 1024-dim).

Warum geteilt statt eigenes Modell: Auf 16 GB VRAM hält paperless-ai mistral
dauerhaft gepinnt (`OLLAMA_KEEP_ALIVE=-1`, `num_ctx=32768`, ~15 GB). Ein zweites
großes Modell daneben führt zu OOM (empirisch: `qwen3:14b` → Exit 137). doc-graph
nutzt daher dasselbe gepinnte mistral. Die eigentliche **Antwortformulierung
übernimmt ohnehin Claude** (via `only_context=True` liefert LightRAG nur die
Roh-Chunks/Entitäten) — das lokale Modell ist nur für Extraktion und
Kontext-Retrieval zuständig.

## Setup

```bash
# Auf dem Ollama-Host (myubuntu/RTX 5080) — teilt sich Ollama mit paperless-ai:
ollama pull mistral-small3.2:24b
ollama pull bge-m3

# Im Run-Verzeichnis (/var/local/mydocker/doc-graph):
cp .env.example .env   # PAPERLESS_TOKEN eintragen
docker compose up -d --build
```

Das externe Docker-Netz `paperless-ai_default` verbindet doc-graph mit
`paperless-ollama` (Ollama) und `paperless` (NGX via LAN-DNS) — dieselben
Namen wie paperless-ai. Bei abweichendem Setup den Netzwerk-Block in
`docker-compose.yml` anpassen oder `PAPERLESS_URL=http://<IP>:8010` verwenden.

## Claude Code anbinden

```bash
claude mcp add --transport http doc-graph http://myubuntu:5775/mcp
```

Da die Konfiguration über `CLAUDE_CONFIG_DIR` zentral liegt, ist der Server
danach von allen Clients gleichermaßen nutzbar.

## Graph-Viewer

`graph_view(project)` rendert den Graphen als interaktive HTML-Ansicht
(vis-network, Optik an den ai-rem-Graphen angelehnt: heller Hintergrund,
grüner Akzent): Knoten = Entitäten (gefärbt nach Typ), Kanten = Beziehungen.
Details (Beschreibung) erscheinen per Klick auf Knoten/Kante in einem
mehrzeiligen Panel. Bedienung:

- **Typ-Filter:** Legende unten anklicken blendet Entitätstypen aus/ein.
- **Physik:** Checkbox schaltet das Force-Layout an/aus.
- **Projekt-Umschalter:** Dropdown oben wechselt zur `graph.html` eines anderen
  Projekts (erscheint ab zwei indexierten Projekten). Jeder `graph_view`-Aufruf
  rendert alle Projektseiten neu, damit die Umschalter überall konsistent sind.

Das Tool gibt die URL zurück:

```
http://myubuntu:5776/<projekt>/graph.html
```

Der Viewer-Root (`http://myubuntu:5776/`) zeigt eine Landing-Page: alle
indexierten Projekte als Karten (Klick öffnet den Graphen) plus eine Kurz-
anleitung, wie es weitergeht. Läuft gerade ein `ingest_paperless`, trägt die
betroffene Karte ein **Import-Status-Badge** (⏳ läuft `done/total` / ✓ zuletzt
indexiert / ✗ Fehler). Dokumente werden einzeln extrahiert (Zähler pro fertigem
Dokument); zusätzlich zeigt das Badge LightRAGs aktuelle Live-Meldung (z.B.
„Chunk 5 of 26 extracted …"), sodass man den Fortschritt auch innerhalb eines
langen Dokuments sieht. Bei laufendem Import lädt die Seite sich alle 5 s selbst
neu, ohne dass man ein MCP-Tool aufrufen muss. Jede Karte
hat einen **Löschen**-Button, der den
Projekt-Index nach Browser-Bestätigung entfernt (Quelldokumente bleiben) —
serverseitig derselbe Weg wie das MCP-Tool `delete_project`. Der Viewer ist ein
stdlib-Fileserver (LAN-intern, kein Auth/HTTPS).

## Typischer Workflow

```
1. Indexieren (einmalig / bei neuen Dokumenten):
   ingest_paperless(project="fehmarn", tag="Teilungsversteigerung")

2. Abfragen:
   query(project="fehmarn",
         question="Welche Fristen wurden vom AG Oldenburg gesetzt und welche laufen noch?")

   query(project="fehmarn",
         question="Chronologie aller Schreiben zur Grundschuld",
         mode="global")

3. Für wörtliche Zitate / juristische Präzision:
   query(..., only_context=True)   → liefert Roh-Chunks + Entitäten,
                                     Claude formuliert selbst

4. Visuell verstehen:
   graph_view(project="fehmarn")   → URL im Browser öffnen
```

### Tools

| Tool | Zweck |
|---|---|
| `list_projects()` | Projekte + Dokumentzahl |
| `ingest_paperless(project, tag/document_type/correspondent/query_text)` | Delta-Indexierung aus Paperless (Hash-Manifest, nur Neues/Geändertes) — Extraktion läuft im Hintergrund, das Tool kehrt sofort zurück |
| `ingest_status(project)` | Fortschritt/Ergebnis des laufenden bzw. letzten Ingest-Laufs |
| `ingest_directory(project, subpath)` | .txt/.md aus gemountetem Verzeichnis |
| `query(project, question, mode, only_context)` | Abfrage: local / global / hybrid / mix / naive |
| `get_entity(project, entity_name)` | Alle Fakten/Relationen zu einer Entität |
| `graph_view(project)` | Interaktive HTML-Graphansicht, gibt Viewer-URL zurück |
| `delete_project(project, confirm)` | Index löschen (Quellen bleiben) |

## Betriebshinweise

- **Indexierung ist der teure Teil:** ~150 Dokumente ≈ mehrere hundert
  LLM-Calls für die Extraktion. Danach nur Delta. Erstlauf am besten nachts starten.
  Der teure Teil läuft **im Hintergrund**: `ingest_paperless` startet die Extraktion
  und kehrt sofort zurück (sonst liefe der MCP-Call ins Timeout); Fortschritt/Ergebnis
  liefert `ingest_status(project)` (`running`/`done`/`error`). Der Status liegt nur im
  RAM — ein Container-Neustart mitten im Lauf verwirft ihn, das noch nicht gespeicherte
  Manifest sorgt dann beim nächsten Ingest für sauberes Nachholen.
- **Modellqualität = Graphqualität.** Wenn der Graph zu dünn wirkt
  (wenige Relationen), Extraktion mit größerem/anderem Modell wiederholen:
  `delete_project` + erneuter Ingest mit geändertem `LLM_MODEL`.
- **Wöchentlicher Modell-Check:** `model_check.sh` (via cron) lässt einen
  Claude-Agenten read-only recherchieren, ob es ein besseres lokales LLM für die
  Extraktion gibt als das aktuelle mistral, und schreibt das Ergebnis nach
  `model_check_report.md` (`EMPFEHLUNG: bleiben` / `EMPFEHLUNG: wechseln zu <tag>`).
- **EMBED_DIM darf sich nachträglich nicht ändern** — Embedding-Modell pro
  Projekt festnageln, sonst Index neu aufbauen.
- **Backup:** `./data/projects/` sichern; das ist der komplette Zustand
  (Graph GraphML, Vektoren, KV-Store, Manifest — alles Dateien, kein DB-Server).
- **Speicher-Backends:** Default sind Datei-basierte Stores (NetworkX +
  nano-vectordb) — für einige hundert Dokumente ausreichend und am
  wartungsärmsten. Erst bei tausenden Dokumenten pro Projekt lohnt
  PostgreSQL/Neo4j als Backend.
- **Version pinnen:** LightRAG entwickelt sich schnell; nach erfolgreichem
  Test die konkrete Version in `requirements.txt` festschreiben
  (`lightrag-hku==<getestete Version>`).
