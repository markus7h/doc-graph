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
Claude Code ──MCP (streamable HTTP :5775)──> doc-graph ──> llama-server (Extraktion + Embeddings)
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

## Modell: geteilter llm-stack

Extraktion und Embeddings laufen über den **geteilten llm-stack** (eigenes
Compose-Projekt, Repo `llm-stack`): Extraktion via Netz-Alias `llm` (zeigt auf
das aktive Chat-Modell — Normalbetrieb `llm-mistral`/`mistral-small3.2:24b`,
während des Ingests `llm-qwen`/`qwen3-14b`) + Embeddings via `llm-embed`
(`bge-m3`, CPU, 1024-dim).

Warum geteilt statt eigenes Modell: Auf ~16 GB VRAM (RTX 5080, geteilt mit dem
Desktop) läuft mistral dauergeladen (`-c 32768`, partial offload `-ngl 27`,
~12–13 GB). Ein zweites großes Modell daneben führt zu OOM (empirisch:
`qwen3:14b` → Exit 137) — deshalb läuft immer nur EIN Chat-Modell, Wechsel per
stop/start. Die eigentliche **Antwortformulierung
übernimmt ohnehin Claude** (via `only_context=True` liefert LightRAG nur die
Roh-Chunks/Entitäten) — das lokale Modell ist nur für Extraktion und
Kontext-Retrieval zuständig.

## Setup

```bash
# Voraussetzung: der llm-stack (Repo llm-stack: llm-mistral, llm-qwen, llm-embed)
# läuft bereits auf demselben Host (myubuntu/RTX 5080) — doc-graph nutzt ihn mit,
# kein eigener Modell-Download nötig (GGUF wird beim Start der llm-stack-Container
# automatisch via `-hf` von Hugging Face geladen).

# Im Run-Verzeichnis (/var/local/mydocker/doc-graph):
cp .env.example .env   # PAPERLESS_TOKEN eintragen
docker compose up -d --build
```

**Updates deployen: immer `./deploy.sh`** (aus dem Git-Repo). Das Script
kopiert die Code-/Build-Dateien ins Deploy-Verzeichnis, rebuildet den
Container und verifiziert per md5, dass der Container wirklich mit dem
deployten Code läuft. `docker-compose.yml` und `.env` werden bewusst nicht
überschrieben (lokale Mounts/Secrets); Abweichungen zum Repo meldet das
Script nur. Hintergrund: ein manuell vergessener Sync ließ am 2026-07-13
einen Ingest mit zwei Commits altem Code (ohne qwen-Swap) laufen.

Der Daten-Mount in `docker-compose.yml` ist ein **absoluter Pfad**
(`/var/local/mydocker/doc-graph/data/projects`), bewusst kein `./data`: ein
`docker compose up` aus dem Git-Repo (falsches CWD) würde sonst dessen leeres
`./data` mounten — der Index wäre „weg" und alle Queries lieferten no-context.
Der kanonische Datenort ist immer das Deploy-Verzeichnis.

Das externe Docker-Netz `llm-net` (gehört dem llm-stack-Compose-Projekt)
verbindet doc-graph mit `llm` (aktives Chat-Modell) und `llm-embed` (bge-m3);
`paperless` (NGX) kommt via LAN-DNS. Bei
abweichendem Setup den Netzwerk-Block in `docker-compose.yml` anpassen oder
`PAPERLESS_URL=https://<host>/` (bzw. `http://<IP>:8010`) verwenden. Der
compose-Default ist `https://paperless/`; der Client akzeptiert das
self-signed LAN-Cert (`verify=False`).

## Claude Code anbinden

```bash
claude mcp add --transport http doc-graph http://myubuntu:5775/mcp
```

Da die Konfiguration über `CLAUDE_CONFIG_DIR` zentral liegt, ist der Server
danach von allen Clients gleichermaßen nutzbar.

## Graph-Viewer

`graph_view(project_id)` rendert den Graphen als interaktive HTML-Ansicht
(vis-network, Optik an den ai-rem-Graphen angelehnt: heller Hintergrund,
grüner Akzent): Knoten = Entitäten (gefärbt nach Typ), Kanten = Beziehungen.
Details (Beschreibung) erscheinen per Klick auf Knoten/Kante in einem
mehrzeiligen Panel. Bedienung:

- **Typ-Filter:** Legende unten anklicken blendet Entitätstypen aus/ein.
- **Physik:** Checkbox schaltet das Force-Layout an/aus.
- **nur Verbundene / Distanz:** Knoten anklicken, dann „nur Verbundene" anhaken —
  zeigt nur dessen Nachbarschaft bis zur eingestellten `Distanz` (Hops). Doppelklick
  setzt den Anker auf einen anderen Knoten um.
- **← Übersicht:** Link oben links zurück zur Projektübersicht (Landing-Page).
- **Projekt-Umschalter:** Dropdown oben wechselt zur `graph.html` eines anderen
  Projekts (erscheint ab zwei indexierten Projekten, zeigt optional den Anzeigenamen).
- **Aktualisieren-Button:** Rendert die graph.html aus dem vorhandenen `.graphml` neu
  (keine LLM-Extraktion, schnell). Nötig z.B. nach `rename_project`.
- **Umbenennen-Button:** Öffnet ein Eingabefeld für den neuen Anzeigenamen (ersetzt die Notwendigkeit, `rename_project()` im Code aufzurufen).
- **alle an/aus:** Blendet alle Typen der Legende auf einmal ein bzw. aus (Toggle).
- **Suche:** Suchfeld oben — Treffer (Teiltreffer im Knotennamen) werden rot hervorgehoben,
  der erste wird angefahren, der Rest gedimmt. Feld leeren stellt die normale Ansicht wieder her.

Das Tool gibt die URL zurück:

```
http://myubuntu:5776/<project_id>/graph.html
```

Der Viewer-Root (`http://myubuntu:5776/`) zeigt eine Landing-Page: alle
indexierten Projekte als Karten mit ihrem Anzeigenamen (falls gesetzt) und der
**Anzahl indexierter Dokumente** (aus dem Ingest-Manifest). Klick öffnet
den Graphen. Läuft gerade ein `ingest_paperless`, trägt die betroffene Karte ein
**Import-Status-Badge** (⏳ läuft `done/total` / ⏸ pausiert / ⏹ abgebrochen /
✓ zuletzt indexiert / ✗ Fehler). Bei laufendem/pausiertem Import rutscht das Badge
in eine eigene, vollbreite Fortschrittszeile unter den Buttons — mit **Fortschrittsbalken**
(`done/total`, grün; gelb bei Pause) statt gequetscht neben den Aktionen.
Dokumente werden einzeln extrahiert (Zähler pro fertigem Dokument); zusätzlich zeigt das
Badge LightRAGs aktuelle Live-Meldung (z.B. „Chunk 5 of 26 extracted …"), sodass man
den Fortschritt auch innerhalb eines langen Dokuments sieht. Bei laufendem oder
pausiertem Import lädt die Seite sich alle 5 s selbst neu, ohne dass man ein MCP-Tool
aufrufen muss. Jede Karte hat folgende Buttons:

- **Pause / Fortsetzen / Stop** (nur bei laufendem/pausiertem Ingest): steuert den
  Import kooperativ **zwischen zwei Dokumenten** (POST `/ingest/control`) — serverseitig
  derselbe Weg wie das MCP-Tool `ingest_control`. Pause gibt die GPU frei (mistral
  zurück für paperless-ai), Fortsetzen lädt qwen neu. Bereits Indexiertes bleibt.
- **Erstellen/Aktualisieren:** Rendert den Graphen aus `.graphml` (POST `/refresh`).
- **Umbenennen:** Öffnet ein Eingabefeld für den neuen Anzeigenamen (POST `/rename`).
- **Löschen:** Entfernt den Projekt-Index nach Browser-Bestätigung (Quelldokumente
  bleiben) — serverseitig derselbe Weg wie das MCP-Tool `delete_project`.

Darunter liegt die **Backup**-Karte (siehe unten): Zeitplan-Dropdown, „Jetzt sichern"
und die letzten Archive.

Der Viewer ist ein stdlib-Fileserver (LAN-intern, kein Auth/HTTPS).

## Backup

Backups laufen **je Projekt** als eigenes `tar.gz` in einen gemounteten Ordner —
je Projekt ein Unterordner, analog ai-rem im selben OneDrive-Verzeichnis daneben:

```yaml
# docker-compose.yml
- ${DOC_GRAPH_BACKUP_PATH:-/home/markus/mystorage/OneDrive/doc-graph}:/backups
```

Ablage: `<Backup-Ordner>/<project_id>/backup_<YYYY-MM-DD_HH-MM-SS>.tar.gz`.
Die Archiv-Wurzel ist die `project_id`, damit eine einzelne Datei für sich allein
wiederherstellbar ist (auch in ein noch nicht existierendes Projekt).

Bedienung komplett über die Viewer-Landing-Page (`http://myubuntu:5776/`):

Global (Backup-Karte):
- **Zeitplan:** `aus` / `stündlich` / `täglich` / `wöchentlich`, „Speichern" übernimmt.
  Der Scheduler sichert **jedes geänderte Projekt einzeln**. Die Einstellung liegt in
  `<Backup-Ordner>/.config.json` und überlebt Neustarts.
- **Projekt aus Datei wiederherstellen…:** Datei-Öffnen-Dialog für ein beliebiges
  Projekt-Archiv vom Rechner (z. B. aus dem synchronisierten OneDrive-Ordner). Die Datei
  wird hochgeladen, auf gültiges Format geprüft und zurückgespielt — **legt das Projekt
  neu an, falls es noch nicht existiert**.

Je Projekt-Karte:
- **Sichern:** Sichert dieses Projekt sofort — **nur wenn es sich seit dem letzten
  Backup geändert hat** (sonst kurze Rückmeldung „nichts geändert").
- **Wiederherstellen:** Auswahl der **letzten 5** Stände (Zeitpunkt · Größe) + Button —
  ersetzt nur dieses Projekt durch den gewählten Stand (Bestätigung im Browser).

Verhalten:

- Rotation je Projekt auf die letzten `MAX_BACKUPS` (Default 10) — ältere gelöscht.
- **Kein Backup während eines Ingests** (das Archiv wäre ein Zwischenstand); der
  Scheduler prüft minütlich und holt es danach nach. Manuelle Aktionen melden Konflikt.
- **Unverändert = kein Backup:** Signatur je Projekt (Dateizahl/Größe/mtime); ohne
  Änderung wird der Lauf übersprungen.
- Restore ist datenverlust-sicher: erst temp-extrahiert, dann der alte Projektstand
  weggemovt, bis der neue drin ist. Alt-Archive mit Wurzel `projects/` (Gesamt-Backups
  vor v0.1.21) werden beim „aus Datei"-Restore weiterhin erkannt.
- **Unverschlüsselt** — bewusst: die Quelldokumente liegen im selben OneDrive
  ohnehin im Klartext (bei ai-rem ist das anders, dort ist der Graph das Original).

Restore von Hand: Container stoppen, Projekt-Archiv ins Datenverzeichnis entpacken,
Container starten.

```bash
docker compose -f /var/local/mydocker/doc-graph/docker-compose.yml down
tar -xzf /home/markus/mystorage/OneDrive/doc-graph/<project_id>/backup_<ts>.tar.gz \
    -C /var/local/mydocker/doc-graph/data/projects   # <project_id>/ -> data/projects/<project_id>
docker compose -f /var/local/mydocker/doc-graph/docker-compose.yml up -d
```

## Typischer Workflow

```
1. Indexieren (einmalig / bei neuen Dokumenten):
   ingest_paperless(project_id="fehmarn", tag="Teilungsversteigerung")

2. Optional: Anzeigenamen setzen (project_id bleibt unverändert):
   rename_project(project_id="fehmarn", project_name="Teilung Eckernförde")

3. Abfragen:
   query(project_id="fehmarn",
         question="Welche Fristen wurden vom AG Oldenburg gesetzt und welche laufen noch?")

   query(project_id="fehmarn",
         question="Chronologie aller Schreiben zur Grundschuld",
         mode="global")

4. query liefert per Default nur den Kontext (Roh-Chunks + Entitäten),
   Claude formuliert selbst. Lokale LLM-Formulierung nur bewusst:
   query(..., only_context=False)  → langsam auf geteilter GPU

5. Visuell verstehen:
   graph_view(project_id="fehmarn")   → URL im Browser öffnen
   (Viewer zeigt den Anzeigenamen im Titel und Dropdown)
```

### Tools

| Tool | Zweck |
|---|---|
| `list_projects()` | Projekte + Dokumentzahl (zeigt project_id, optional Anzeigename in Klammern) |
| `ingest_paperless(project_id, tag/document_type/correspondent/query_text)` | Delta-Indexierung aus Paperless (Hash-Manifest, nur Neues/Geändertes) — Extraktion läuft im Hintergrund, das Tool kehrt sofort zurück |
| `ingest_status(project_id)` | Fortschritt/Ergebnis des laufenden bzw. letzten Ingest-Laufs. Feld `docs` zeigt die **echten** LightRAG-Zustände (`processed`/`processing`/`pending`/`failed`) — nur `processed` heißt wirklich im Graph; `state:done` heißt nur „Dispatch fertig" |
| `ingest_control(project_id, action)` | Steuert einen laufenden Ingest: `pause` (hält nach dem aktuellen Dokument an, gibt die GPU frei → mistral zurück für paperless-ai), `resume` (lädt qwen neu, macht weiter), `stop` (bricht ab, bereits Indexiertes bleibt) |
| `ingest_directory(project_id, subpath)` | .txt/.md/.pdf aus gemountetem Verzeichnis (PDF via pdftotext, kein OCR — gescannte Bilder über Paperless) |
| `query(project_id, question, mode, only_context, max_total_tokens)` | Abfrage: local / global / hybrid / mix / naive. `only_context` ist **default True** (Claude formuliert aus dem Kontext); die lokale LLM-Formulierung ist auf geteilter GPU zu langsam. `max_total_tokens` (default 12000) deckelt den Kontext, damit er das MCP-Token-Limit nicht sprengt |
| `get_entity(project_id, entity_name)` | Alle Fakten/Relationen zu einer Entität |
| `graph_view(project_id)` | Interaktive HTML-Graphansicht, gibt Viewer-URL zurück |
| `rename_project(project_id, project_name)` | Setzt den Anzeigenamen (display name) eines Projekts; der technische project_id bleibt unverändert |
| `delete_project(project_id, confirm)` | Index löschen (Quellen bleiben) |

## Betriebshinweise

- **Indexierung ist der teure Teil:** ~150 Dokumente ≈ mehrere hundert
  LLM-Calls für die Extraktion. Danach nur Delta. Erstlauf am besten nachts starten.
  Der teure Teil läuft **im Hintergrund**: `ingest_paperless` startet die Extraktion
  und kehrt sofort zurück (sonst liefe der MCP-Call ins Timeout); Fortschritt/Ergebnis
  liefert `ingest_status(project_id)` (`running`/`done`/`error`). Der Status liegt nur im
  RAM — ein Container-Neustart mitten im Lauf verwirft ihn, das noch nicht gespeicherte
  Manifest sorgt dann beim nächsten Ingest für sauberes Nachholen.
- **Modellqualität = Graphqualität.** Wenn der Graph zu dünn wirkt
  (wenige Relationen), Extraktion mit größerem/anderem Modell wiederholen:
  `delete_project` + erneuter Ingest mit geändertem `LLM_MODEL`.
- **Voll-GPU-Extraktion via qwen-Swap — automatisch.** mistral-24b passt nur mit
  CPU-Offload in die 16 GB (13/40 Layer im RAM → langsam, Extraktions-Timeouts).
  Da paperless-ai selten läuft, teilt man die GPU **zeitlich**. Das passiert jetzt
  **automatisch bei jedem Ingest**: sobald `ingest_paperless`/`ingest_directory`
  Dokumente extrahiert, ruft der Server `swap-to-qwen.sh` (`docker stop llm-mistral`
  + `docker start llm-qwen`; beide teilen den Netz-Alias `llm`, LLM_BASE_URL bleibt
  unverändert); nach Abschluss `swap-to-mistral.sh` (zurück auf mistral). paperless-ai
  wird dabei NICHT mehr pausiert — es ist auf `llm-mistral` gepinnt und bekommt nie
  qwen-Antworten; seine UI zeigt während des Swaps „Modell offline".
  Paralleler Ingest über mehrere Projekte swappt per Refcount nur
  einmal rein/raus; ein Crash mitten im Ingest wird beim nächsten Serverstart
  zurückgeswappt. Inserts laufen global serialisiert (LightRAG-Instanzen teilen
  den Pipeline-Lock — paralleles `ainsert` kehrt sonst unverarbeitet zurück), und
  ein Dokument gilt erst als indexiert, wenn LightRAG es wirklich `processed`
  meldet — sonst holt der nächste Ingest es automatisch nach.
  Voraussetzung: `/var/run/docker.sock` ist in den doc-graph-
  Container gemountet (compose) — root-äquivalent auf dem Host, bewusst, das Netz
  ist intern. Abschaltbar via `INGEST_SWAP=0` (z. B. lokale Dev-Umgebung ohne Socket).
- **Wöchentlicher Modell-Check:** `model_check.sh` (via cron) ermittelt das
  aktuell geladene Extraktions-Modell per `docker exec` am laufenden Chat-Container
  (`llm-*` ohne `-embed`, `/v1/models`) ab. Der Claude-Agent recherchiert dann read-only, ob es ein besseres
  lokales LLM als das geladene gibt, und schreibt das Ergebnis nach
  `model_check_report.md` (`EMPFEHLUNG: bleiben` / `EMPFEHLUNG: wechseln zu <tag>`).
- **EMBED_DIM darf sich nachträglich nicht ändern** — Embedding-Modell pro
  Projekt festnageln, sonst Index neu aufbauen.
- **`CHUNK_TOKEN_SIZE`** (default 600, war LightRAG-Default 1200): kleinere Chunks
  = weniger Entitäten pro Extraktions-Call, verhindert den 480s-Worker-Timeout bei
  dichten Tabellen-Docs. Wirkt nur auf **neu** indexierte Dokumente — für den
  Bestand `delete_project` + Re-Ingest.
- **`QUERY_MAX_TOKENS`** (default 12000): globaler Default für das Kontext-Budget je
  Query; pro Abfrage via `max_total_tokens` überschreibbar.
- **`LLM_TIMEOUT`** (default 480 s): Timeout je einzelnem LLM-Call. Bei CPU-Offload/
  niedrigem Throughput hochsetzen. **Achtung:** löst nur das Symptom — der Engpass bei
  dichten Docs ist der GPU-Throughput (z. B. ~5,8 t/s im CPU-Offload); dauerhaft hilft
  nur Voll-GPU-Extraktion (`swap-to-qwen.sh`), nicht ein höherer Timeout.
- **`MAX_ASYNC`** (default 2): parallele LLM-Calls. Bei dichten Beständen / knapper
  GPU auf `1` setzen, damit ein Poison-Doc nicht den ganzen Durchsatz frisst.
- **`GRAPH_LANGUAGE`** (default `German`): Sprache der extrahierten Entitäten/
  Beschreibungen. LightRAG-Default wäre `English` (Graph-Einträge landen dann
  englisch trotz deutscher Docs). Wirkt nur auf **neu** indexierte Dokumente —
  Bestand für deutsche Einträge `delete_project` + Re-Ingest.
- **Backup:** `./data/projects/` sichern; das ist der komplette Zustand
  (Graph GraphML, Vektoren, KV-Store, Manifest — alles Dateien, kein DB-Server).
- **Speicher-Backends:** Default sind Datei-basierte Stores (NetworkX +
  nano-vectordb) — für einige hundert Dokumente ausreichend und am
  wartungsärmsten. Erst bei tausenden Dokumenten pro Projekt lohnt
  PostgreSQL/Neo4j als Backend.
- **Version pinnen:** LightRAG entwickelt sich schnell; nach erfolgreichem
  Test die konkrete Version in `requirements.txt` festschreiben
  (`lightrag-hku==<getestete Version>`).
