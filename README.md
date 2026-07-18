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
während des Ingests `llm-qwen`/`qwen3-14b`) + Embeddings via Netz-Alias `embed`
(`bge-m3`, 1024-dim). Der Embedder swappt mit: Normalbetrieb `llm-embed` (CPU),
während des Ingests `llm-embed-gpu` (voll auf die GPU neben qwen) — Chunk-
Embeddings ~10–50× schneller, keine CPU-`Worker execution timeout`-Failures.

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
mehrzeiligen Panel.

**Live geladen, gedeckelt auf max. Knotenzahl.** Große Graphen (tausende
Entitäten) würden vis-network unbrauchbar langsam machen. Die `graph.html` bettet
die Knoten daher nicht mehr komplett ein, sondern lädt sie per `fetch` vom
Endpoint `GET /<project_id>/nodes` — serverseitig auf **`GRAPH_MAX_NODES`**
(default **2500**) gedeckelt. Beim Deckeln gewinnen die **verbindungsstärksten**
Knoten (höchster Knotengrad — es gibt kein Score-Feld im GraphML). Der Zähler oben
zeigt dann „2500 von N Knoten". Der volle Graph bleibt im `.graphml` erhalten und
über Fokus/Suche/Typ-Filter (jeweils ein Server-Roundtrip, s.u.) erreichbar. Das
GraphML wird pro Projekt über seine Datei-mtime gecacht, das Parsen läuft also
nicht bei jedem Klick neu. Bedienung:

- **Typ-Filter:** Legende unten anklicken blendet Entitätstypen aus/ein (lädt das
  gefilterte Subset neu vom Server). Die Legende zeigt alle Typen mit Anzahl —
  auch solche, die im aktuell geladenen Subset gerade nicht sichtbar sind.
- **Physik:** Checkbox schaltet das Force-Layout an/aus.
- **nur Verbundene / Distanz:** Knoten anklicken, dann „nur Verbundene" anhaken —
  lädt vom Server dessen Nachbarschaft bis zur eingestellten `Distanz` (Hops).
  Doppelklick setzt den Anker auf einen anderen Knoten um. So erreicht man auch
  Knoten außerhalb des initialen Top-Sets.
- **← Übersicht:** Link oben links zurück zur Projektübersicht (Landing-Page).
- **Projekt-Umschalter:** Dropdown oben wechselt zur `graph.html` eines anderen
  Projekts (erscheint ab zwei indexierten Projekten, zeigt optional den Anzeigenamen).
- **Aktualisieren-Button:** Rendert die graph.html aus dem vorhandenen `.graphml` neu
  (keine LLM-Extraktion, schnell). Nötig z.B. nach `rename_project`.
- **Umbenennen-Button:** Öffnet ein Eingabefeld für den neuen Anzeigenamen (ersetzt die Notwendigkeit, `rename_project()` im Code aufzurufen).
- **alle an/aus:** Blendet alle Typen der Legende auf einmal ein bzw. aus (Toggle).
- **Suche:** Suchfeld oben — sucht **im ganzen Graphen** (serverseitig, entprellt):
  lädt Treffer (Teiltreffer im Knotennamen) plus deren direkte Nachbarn, hebt sie rot
  hervor, fährt den ersten an und dimmt den Rest. So findet man auch Knoten jenseits
  des initialen Top-Sets. Feld leeren stellt die normale (gedeckelte) Ansicht wieder her.

Das Tool gibt die URL zurück:

```
http://myubuntu:5776/<project_id>/graph.html
```

Der Viewer-Root (`http://myubuntu:5776/`) zeigt eine Landing-Page: alle
indexierten Projekte als Karten mit ihrem Anzeigenamen (falls gesetzt) und ihren
Kennzahlen — **Anzahl indexierter Dokumente** (aus dem Ingest-Manifest) sowie, bei
gerendertem Graph, **Anzahl Entitäten und Kanten** (aus dem `.graphml`). Der
**Projektname selbst ist der Link** zum Graphen. Läuft gerade ein `ingest_paperless`, trägt die betroffene Karte ein
**Import-Status-Badge** (⏳ läuft `done/total` / ⏸ pausiert / ⏹ abgebrochen /
✓ zuletzt indexiert / ✗ Fehler). Bei laufendem/pausiertem Import rutscht das Badge
in eine eigene, vollbreite Fortschrittszeile unter den Buttons — mit **Fortschrittsbalken**
(`done/total`, grün; gelb bei Pause) statt gequetscht neben den Aktionen.
Dokumente werden einzeln extrahiert (Zähler pro fertigem Dokument); zusätzlich zeigt das
Badge LightRAGs aktuelle Live-Meldung (z.B. „Chunk 5 of 26 extracted …"), sodass man
den Fortschritt auch innerhalb eines langen Dokuments sieht. Bei laufendem oder
pausiertem Import lädt die Seite sich alle 5 s selbst neu, ohne dass man ein MCP-Tool
aufrufen muss. Jede Karte hat folgende Buttons (**Icon-only** mit Inline-SVG —
rendern zuverlässig unabhängig vom Emoji-Font; die Beschriftung erscheint als
Tooltip erst nach kurzem Verweilen mit der Maus, Löschen hovert rot, der Rest grün):

- **Pause / Fortsetzen / Stop** (nur bei laufendem/pausiertem Ingest): wirkt
  **sofort** — das laufende Dokument wird mitten in der Verarbeitung abgebrochen
  (POST `/ingest/control`, serverseitig derselbe Weg wie das MCP-Tool
  `ingest_control`). **Stop** bricht ab; das gerade laufende Dokument geht verloren
  (bereits fertig indexierte Dokumente bleiben). **Pause** gibt die GPU frei (mistral
  zurück für paperless-ai); **Fortsetzen** lädt qwen neu und verarbeitet das
  abgebrochene Dokument komplett neu (kein halb-indexierter Stand).
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
| `ingest_paperless(project_id, tag/document_type/correspondent/query_text, regelwerk)` | Delta-Indexierung aus Paperless (Hash-Manifest, nur Neues/Geändertes) — Extraktion läuft im Hintergrund, das Tool kehrt sofort zurück. `regelwerk=True` für Bedingungswerke/Verträge (siehe unten) |
| `ingest_status(project_id)` | Fortschritt/Ergebnis des laufenden bzw. letzten Ingest-Laufs. Feld `docs` zeigt die **echten** LightRAG-Zustände (`processed`/`processing`/`pending`/`failed`) — nur `processed` heißt wirklich im Graph; `state:done` heißt nur „Dispatch fertig" |
| `ingest_control(project_id, action)` | Steuert einen laufenden Ingest: `pause` (bricht das laufende Dokument sofort ab und gibt die GPU frei → mistral zurück für paperless-ai), `resume` (lädt qwen neu, verarbeitet das abgebrochene Dokument neu), `stop` (bricht sofort ab; das laufende Dokument geht verloren, bereits fertig Indexiertes bleibt) |
| `ingest_directory(project_id, subpath, regelwerk)` | .txt/.md/.pdf aus gemountetem Verzeichnis (PDF via pdftotext, kein OCR — gescannte Bilder über Paperless) |
| `query(project_id, question, mode, only_context, max_total_tokens)` | Abfrage: local / global / hybrid / mix / naive. `only_context` ist **default True** (Claude formuliert aus dem Kontext); die lokale LLM-Formulierung ist auf geteilter GPU zu langsam. `max_total_tokens` (default 12000) deckelt den Kontext, damit er das MCP-Token-Limit nicht sprengt |
| `get_entity(project_id, entity_name)` | Alle Fakten/Relationen zu einer Entität |
| `get_clause(project_id, clause, document)` | **Regelwerk-Projekte:** exakter Wortlaut einer Klausel (`'§ 2'`, `'§2'`, `'2'`, `'Artikel 3'`) — deterministisch aus dem Klausel-Store, kein LLM/Retrieval. `document` filtert per Substring auf den Dokumenttitel |
| `graph_view(project_id)` | Interaktive HTML-Graphansicht, gibt Viewer-URL zurück |
| `rename_project(project_id, project_name)` | Setzt den Anzeigenamen (display name) eines Projekts; der technische project_id bleibt unverändert |
| `delete_project(project_id, confirm)` | Index löschen (Quellen bleiben) |

### Regelwerk-Projekte

Für Bedingungswerke/Verträge (AVB, Leistungspläne, AGB) ist normales
Token-Chunking + LLM-Extraktion die falsche Granularität: Klauselgrenzen werden
zerschnitten, und ein Klausel-Zitat aus dem Graph ist nicht nachprüfbar. Deshalb:

```
ingest_paperless(project_id="bu-avb", tag="dx: BU-AVB", regelwerk=True)
get_clause(project_id="bu-avb", clause="§ 2")
```

`regelwerk=True` haftet am Projekt (`meta.json`) und bewirkt zweierlei:

- **Klauselweises Chunking:** ein Chunk = eine Klausel (`§ n` / `Artikel n` /
  `Ziffer n` am Zeilenanfang, Splitter in `clauses.py`). Dokumente ohne
  Klausel-Struktur (Anschreiben etc.) fallen aufs normale Token-Chunking zurück;
  überlange Klauseln werden nachgesplittet.
- **Klausel-Store** (`clauses.json` pro Projekt): exakter Wortlaut je Klausel und
  Dokument. `get_clause` liest ihn deterministisch — kein LLM, kein Retrieval,
  keine Halluzination; kommt dieselbe §-Nummer in mehreren Dokumenten vor, werden
  alle Treffer mit Dokumenttitel geliefert (`document=` filtert). Der Store wird
  bei jedem Ingest auch für unveränderte Dokumente aufgefrischt.

Empfehlung: Regelwerk und Fall-Korrespondenz als **getrennte Projekte** führen —
„was sagen die Bedingungen" (get_clause, zitierfähig) bleibt so sauber getrennt
von „was behauptet die Gegenseite" (query auf dem Fall-Projekt).

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
  unverändert). Dasselbe Skript swappt auch den **Embedder** auf die GPU (`docker
  stop llm-embed` + `docker start llm-embed-gpu`, gemeinsamer Alias `embed`) —
  bge-m3 voll auf die GPU neben qwen, damit die Chunk-Embeddings nicht am CPU-
  Timeout sterben. Nach Abschluss `swap-to-mistral.sh` (qwen + GPU-Embedder raus,
  mistral + CPU-Embedder zurück). paperless-ai
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
- **`CHUNK_TOKEN_SIZE`** (default 1200 = LightRAG-Default): größere Chunks =
  halb so viele Extraktions-Calls (der ~3–4k-Token-Prompt-Overhead fällt pro
  Chunk an). Der frühere 600er-Default war ein Workaround gegen 480s-Worker-
  Timeouts bei CPU-Offload-Extraktion — mit Voll-GPU-qwen + `-n`-Output-Deckel
  obsolet. Wirkt nur auf **neu** indexierte Dokumente — für den Bestand
  `delete_project` + Re-Ingest.
- **`MAX_GLEANING`** (default 0, LightRAG-Default wäre 1): Gleaning ist LightRAGs
  „hast du was übersehen?"-Nachfassrunde pro Chunk — verdoppelt die LLM-Calls
  für wenige Zusatz-Entitäten. Auf der geteilten GPU halber Ingest-Durchsatz,
  deshalb per Default aus (gesetzt in `server.py` vor dem lightrag-Import,
  via Compose-`environment` überschreibbar).
- **`QUERY_MAX_TOKENS`** (default 12000): globaler Default für das Kontext-Budget je
  Query; pro Abfrage via `max_total_tokens` überschreibbar.
- **`LLM_TIMEOUT`** (default 480 s): Timeout je einzelnem LLM-Call. Bei CPU-Offload/
  niedrigem Throughput hochsetzen. **Achtung:** löst nur das Symptom — der Engpass bei
  dichten Docs ist der GPU-Throughput (z. B. ~5,8 t/s im CPU-Offload); dauerhaft hilft
  nur Voll-GPU-Extraktion (`swap-to-qwen.sh`), nicht ein höherer Timeout.
- **`MAX_ASYNC`** (default 2): parallele LLM-Calls. Bei dichten Beständen / knapper
  GPU auf `1` setzen, damit ein Poison-Doc nicht den ganzen Durchsatz frisst.
- **`EMBED_MAX_ASYNC`** (default 3) / **`EMBED_TIMEOUT`** (default 180 s):
  Robustheit des Embedding-Pfads. `bge-m3` läuft auf CPU (die GPU hat während des
  Ingests qwen). Die LightRAG-Defaults (`max_async=8`, `timeout=30 s`) überfluten
  den CPU-Embedder → `Worker execution timeout` → `IndexFlushError` → das **ganze
  Dokument failt**, obwohl die Extraktion längst durch war. Weniger Parallelität +
  großzügigerer Timeout beheben das. Dauerhaft schneller wird es erst mit `bge-m3`
  auf der GPU (Platz neben qwen) statt CPU.
- **`MAX_DOC_CHARS`** (default 300000 ≈ 125 Chunks): Sicherheits-Guard beim
  Ingest. Docs mit mehr Textzeichen werden **nicht** verarbeitet, sondern in
  `ingest_flagged.json` beiseitegelegt und in `ingest_status` unter `flagged`
  ausgewiesen — schützt vor Datenmüll (z. B. einem 48-MB-CSV-Export mit ~39k
  Chunks, der den Graph flutet und stundenlang die GPU bindet). Zwei Ebenen:
  (1) beim Einsammeln aus Paperless werden übergroße Docs gar nicht erst
  eingereiht; (2) ein **Altlasten-Guard** entfernt vor jedem Lauf übergroße Docs,
  die aus früheren Läufen noch in LightRAGs `doc_status`-Pipeline hängen
  (`pending`/`processing`/`failed`) — sonst zieht LightRAG sie bei jedem `ainsert`
  neu in die Verarbeitung, unabhängig vom Paperless-Tag. Ein geflaggtes Doc bleibt
  für Re-Ingest offen; sinkt sein Text unter die Schwelle, hebt sich der Flag beim
  nächsten Ingest automatisch auf.

  **Entscheidung im Viewer:** Geflaggte Docs erscheinen in der Landing-Page des
  Viewers (Port `VIEWER_PORT`) unter ihrer Projekt-Karte mit Buttons. Pro Doc gilt
  eine `decision`: `open` (Default, wartet), `approve` (trotz Übergröße aufnehmen —
  greift beim nächsten Ingest, Altlasten-Guard lässt es dann in der Pipeline) oder
  `ignore` (dauerhaft ausblenden, wird nicht mehr geflaggt). Das Paperless-Quell-
  dokument bleibt in jedem Fall unberührt — geflaggt heißt nur „nicht im Graph".
- **`GRAPH_LANGUAGE`** (default `German`): Sprache der extrahierten Entitäten/
  Beschreibungen. LightRAG-Default wäre `English` (Graph-Einträge landen dann
  englisch trotz deutscher Docs). Wirkt nur auf **neu** indexierte Dokumente —
  Bestand für deutsche Einträge `delete_project` + Re-Ingest.
- **`GRAPH_MAX_NODES`** (default `2500`): Obergrenze gleichzeitig im Viewer
  geladener Entitäten. Der `/<project_id>/nodes`-Endpoint deckelt jedes Subset
  hierauf (Priorisierung nach Knotengrad); schützt Browser und Force-Layout vor
  Graphen mit tausenden Knoten. Höher setzen macht den Viewer träger, nicht kaputt.
- **Backup:** `./data/projects/` sichern; das ist der komplette Zustand
  (Graph GraphML, Vektoren, KV-Store, Manifest — alles Dateien, kein DB-Server).
- **Speicher-Backends:** Default sind Datei-basierte Stores (NetworkX +
  nano-vectordb) — für einige hundert Dokumente ausreichend und am
  wartungsärmsten. Erst bei tausenden Dokumenten pro Projekt lohnt
  PostgreSQL/Neo4j als Backend.
- **Version pinnen:** LightRAG entwickelt sich schnell; nach erfolgreichem
  Test die konkrete Version in `requirements.txt` festschreiben
  (`lightrag-hku==<getestete Version>`).
