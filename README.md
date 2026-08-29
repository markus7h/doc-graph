# doc-graph — Knowledge Graph pro Projekt als MCP-Server

Ein Container auf `mystorage`, der pro "Kontext"/Projekt einen
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

**Code-Aufteilung** (Stand 2026-08-23, laufende Entflechtung von `server.py`):

| Modul | Inhalt |
|---|---|
| `config.py` | Pfade, Logger, Projektnamen-Validierung. Bewusst ohne schwere Importe, damit Module und ihre Tests ohne LightRAG/MCP laufen. |
| `backup.py` | Backup/Restore je Projekt samt Scheduler. Kennt `server.py` nicht; die beiden Rückfragen an den laufenden Dienst (läuft ein Ingest? Instanz-Cache verwerfen) hängt `server.py` beim Start ein. |
| `graphview.py` | HTML-Rendering des Graph-Viewers. |
| `clauses.py` | Klausel-Splitting für Regelwerke. |
| `backfill_fundstellen.py` | Einmal-Migration: trägt Fundstellen in Projekte nach, die vor dem `file_paths`-Fix ingestiert wurden. |
| `server.py` | Rest: MCP-Tools, Ingest-Pipeline, LightRAG-Setup, Embedding-Client, HTTP-Viewer. Weiterhin zu groß — siehe Verbesserungsplan. |

### Fundstellen

`query` liefert am Ende eine **Reference Document List**, mit der sich jede
Aussage auf ein Dokument zurückführen lässt. LightRAG baut sie aus dem
`file_path` der Chunks und überspringt dabei seinen eigenen Default
`unknown_source` (`utils.py`, `generate_reference_list_from_chunks`). Wird beim
`ainsert` kein `file_paths` übergeben, bleibt die Liste deshalb **leer** und
jede `reference_id` ist `""` — die Antwort ist dann nicht belegbar.

doc-graph übergibt als Fundstelle `Titel, Datum, Korrespondent`, gelesen aus dem
Metadaten-Header, den `_doc_to_text` (Paperless) und `_file_to_text` (Datei)
ohnehin an den Textanfang setzen. Fehlt der Header, steht dort `ohne Titel` —
bewusst, statt einen plausiblen Beleg zu erfinden.

Projekte, die vor diesem Fix ingestiert wurden, tragen überall
`unknown_source`. Das repariert `backfill_fundstellen.py` **ohne Neu-Ingest**,
weil die Herkunft schon in jedem Volltext steht: ein reines Metadaten-Update
über `kv_store_full_docs`, `kv_store_text_chunks`, `kv_store_doc_status` und
`vdb_chunks`, ohne Embeddings und ohne LLM-Calls, Sekunden statt Stunden.

```bash
docker exec doc-graph python /app/backfill_fundstellen.py --dry-run   # Bericht
docker exec doc-graph python /app/backfill_fundstellen.py             # schreibend
```

Idempotent, legt vor jedem Schreiben ein `.bak-<Zeitstempel>` an und fasst
bereits gesetzte Werte nicht an. **Grenze:** Entities und Relationen tragen ihre
Provenienz ebenfalls als `file_path`, aber zur Extraktionszeit im Graph
eingefroren — die bleiben `unknown_source`, bis sie neu extrahiert werden. Für
die Referenzliste ist das unerheblich, die kommt ausschließlich aus den Chunks.

Bewusste Entscheidung: **LightRAG als Library, nicht als LightRAG-Server.**
Der offizielle LightRAG-Server bindet einen Workspace fest pro Prozess —
Multi-Projekt hieße dort ein Container pro Projekt. Hier verwaltet der
MCP-Server stattdessen selbst eine lazy geladene LightRAG-Instanz pro Projekt,
jede mit eigenem `workspace=<project>` (Store unter `PROJECTS_DIR/<project>/`).
Der Projektname ist einfach ein Tool-Parameter. Der eigene Workspace ist dabei
nicht nur Kosmetik: LightRAGs `shared_storage` (doc_status/full_docs) ist
prozess-global und nur nach Workspace getrennt — ohne ihn würde ein Dokument,
das schon in Projekt A liegt, in Projekt B still als Duplikat verworfen.

## Modell: geteilter llm-stack

LLM und Embeddings laufen über den **geteilten llm-stack** (eigenes
Compose-Projekt, Repo `llm-stack`), seit 2026-08-23 über dessen LiteLLM-Router:
Extraktion, Queries UND Embeddings via `http://mystorage.lan:11437/v1`
(`qwen3-14b` + `bge-m3`, 1024-dim; qwen-only nach A/B-Test 2026-08-03 — auf
Augenhöhe bis besser als mistral bei deutschen Antworten).

Hinter dem Router liegen **zwei** GPU-Hosts: myai (RTX 3070+2060, Layer-Split,
36,7 tok/s) als Basis und myubuntu (RTX 5080, Modell am Stück, 86,9 tok/s) als
Burst. Der Router verteilt gewichtet und nimmt einen ausgefallenen Host
innerhalb eines Requests aus der Rotation — myai schläft 23:00–06:00, myubuntu
ist ein Desktop-Rechner und darf jederzeit weg sein. Für den Ingest heißt das:
läuft myubuntu mit, geht es rund dreimal so schnell; läuft es nicht, ist alles
wie vorher. `ingest-begin.sh` weckt myai weiterhin vor jedem Ingest.

Der Router braucht `LLM_API_KEY` (Master-Key) — `server.py` schickt ihn im LLM-
wie im Embedding-Pfad ohnehin schon mit.

Der frühere GPU-Swap auf myubuntu (nur EIN Chat-Modell in 16 GB, Wechsel per
stop/start + Alias-Trick) ist damit obsolet. doc-graph selbst braucht seit dem
Split gar keine GPU mehr — deshalb läuft der Container seit 2026-08-04 auf
mystorage statt auf myubuntu.
Die eigentliche **Antwortformulierung übernimmt ohnehin meist Claude** (via
`only_context=True` liefert LightRAG nur die Roh-Chunks/Entitäten) — das lokale
Modell ist primär für Extraktion und Kontext-Retrieval zuständig.

## Setup

```bash
# Voraussetzung: der llm-stack (Repo llm-stack) läuft — llm-qwen + llm-embed
# auf myai (./deploy.sh myai dort) — doc-graph nutzt ihn mit, kein eigener
# Modell-Download nötig (GGUF wird beim Start der llm-stack-Container
# automatisch via `-hf` geladen).

# Im Run-Verzeichnis (/var/local/mydocker/compose-files/doc-graph):
cp .env.example .env   # PAPERLESS_TOKEN eintragen
docker compose up -d --build
```

**Updates deployen: immer `./deploy.sh`** — von überall, auch vom
Entwicklungsrechner aus:

```bash
./deploy.sh                      # von myubuntu: Sync + Rebuild per ssh/scp
DEPLOY_HOST="" ./deploy.sh       # erzwingt lokalen Modus
DEPLOY_TARGET=anderer ./deploy.sh  # anderer Zielhost
```

Das Script erkennt am Hostnamen, ob es schon auf dem Zielhost (`mystorage`)
läuft: wenn ja, arbeitet es lokal wie bisher; wenn nein, gehen Sync und
Rebuild über ssh/scp. Vorher prüft es die SSH-Verbindung (`BatchMode`, damit
es bei fehlendem Key sofort scheitert statt auf eine Passphrase zu warten)
und dass das Deploy-Verzeichnis existiert. Es
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

LLM und Embedder (`mystorage.lan:11437`) und `paperless` (NGX) kommen alle
via LAN-DNS — kein Docker-Netz zwischen den Stacks nötig. **`mystorage.lan`,
nicht `mystorage`**: Docker erbt den `/etc/hosts`-Eintrag des Hosts für dessen
eigenen Namen, im Container löst der kurze Name auf `127.0.1.1` auf und der
Call landet im Container selbst. Bei abweichendem
Setup `LLM_BASE_URL`/`EMBED_BASE_URL` bzw.
`PAPERLESS_URL=https://<host>/` (bzw. `http://<IP>:8010`) setzen. Der
compose-Default ist `https://paperless/`; der Client akzeptiert das
self-signed LAN-Cert (`verify=False`).

## Claude Code anbinden

```bash
claude mcp add --transport http doc-graph http://mystorage:5775/mcp
```

Da die Konfiguration über `CLAUDE_CONFIG_DIR` zentral liegt, ist der Server
danach von allen Clients gleichermaßen nutzbar.

## Graph-Viewer

`graph_view(project_id)` rendert den Graphen als interaktive HTML-Ansicht
(vis-network, Optik an den ai-rem-Graphen angelehnt: heller Hintergrund,
Akzent in gedecktem Indigo `#3a5a9b`): Knoten = Entitäten (gefärbt nach Typ),
Kanten = Beziehungen. Die Typ-Palette der Knoten ist davon unabhängig — sie
kodiert Entitätstypen und ändert sich nicht mit der Oberflächenfarbe.
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
http://mystorage:5776/<project_id>/graph.html
```

Der Viewer-Root (`http://mystorage:5776/`) zeigt eine Landing-Page: alle
indexierten Projekte als Karten mit ihrem Anzeigenamen (falls gesetzt) und ihren
Kennzahlen — **Anzahl indexierter Dokumente** (aus dem Ingest-Manifest) sowie, bei
gerendertem Graph, **Anzahl Entitäten und Kanten** (aus dem `.graphml`). Der
**Projektname selbst ist der Link** zum Graphen. Läuft gerade ein `ingest_paperless`, trägt die betroffene Karte ein
**Import-Status-Badge** (⏳ läuft `done/total` / ⏸ pausiert / ⏹ abgebrochen /
✓ zuletzt indexiert / ✗ Fehler). Bei laufendem/pausiertem Import rutscht das Badge
in eine eigene, vollbreite Fortschrittszeile unter den Buttons — mit **Fortschrittsbalken**
(`done/total`, im Akzent; gelb bei Pause) statt gequetscht neben den Aktionen.
Dokumente werden einzeln extrahiert (Zähler pro fertigem Dokument); zusätzlich zeigt das
Badge LightRAGs aktuelle Live-Meldung (z.B. „Chunk 5 of 26 extracted …"), sodass man
den Fortschritt auch innerhalb eines langen Dokuments sieht. Bei laufendem oder
pausiertem Import lädt die Seite sich alle 5 s selbst neu, ohne dass man ein MCP-Tool
aufrufen muss. Jede Karte hat folgende Buttons (**Icon-only** mit Inline-SVG —
rendern zuverlässig unabhängig vom Emoji-Font; die Beschriftung erscheint als
Tooltip erst nach kurzem Verweilen mit der Maus, Löschen hovert rot, der Rest im Akzent):

- **Pause / Fortsetzen / Stop** (nur bei laufendem/pausiertem Ingest): greift
  **nach dem aktuellen Batch** (`INGEST_BATCH` Docs, default 5 — ein Batch wird
  immer ganz zu Ende geführt; POST `/ingest/control`, serverseitig derselbe Weg
  wie das MCP-Tool `ingest_control`). **Stop** bricht danach ab (bereits fertig
  indexierte Dokumente bleiben). **Pause** hält an;
  **Fortsetzen** weckt myai bei Bedarf neu und macht beim nächsten Batch weiter.
- **Erstellen/Aktualisieren:** Rendert den Graphen aus `.graphml` (POST `/refresh`).
- **Umbenennen:** Öffnet ein Eingabefeld für den neuen Anzeigenamen (POST `/rename`).
- **Löschen:** Entfernt den Projekt-Index nach Browser-Bestätigung (Quelldokumente
  bleiben) — serverseitig derselbe Weg wie das MCP-Tool `delete_project`.

Darunter liegt die **Backup**-Karte (siehe unten): Zeitplan-Dropdown, „Jetzt sichern"
und die letzten Archive.

Der Viewer ist ein stdlib-Fileserver (LAN-intern, kein Auth/HTTPS).

Er bindet per Default auf `::` (`VIEWER_BIND`), der Socket bleibt dabei
**dual-stack** — der Container ist also sowohl unter seiner IPv6 aus
`fd00:24:9:68::/64` erreichbar (Caddy proxyt `docgraph.lan` direkt dorthin) als
auch weiterhin über den published IPv4-Port. `VIEWER_BIND=0.0.0.0` erzwingt
reines IPv4. Der MCP-Port (`MCP_PORT`) ist davon nicht betroffen.

Diese IPv6 wird in der Compose **fest zugewiesen**
(`web_net.ipv6_address: fd00:24:9:68:23::5776`, Konvention: letztes Segment =
Port). Ohne die Zuweisung vergibt Docker beim Recreate eine beliebige Adresse,
Caddy dialt weiter auf die alte und liefert 502 — der Viewer bleibt dabei über
`http://<host>:5776` erreichbar, nur `https://docgraph` nicht.

## Hilfe-Seite

`http://<host>:5776/hilfe` (Link auf der Projektübersicht) — beginnt mit
**Chat-Beispielen**: was man in Claude Code sagt, was daraufhin läuft und was
es erspart. Darunter, was doc-graph für ein Projekt kann, mit aufrufbaren
Beispielen: `get_clause`, `query`,
`get_entity`, `ingest_paperless`/`ingest_status`, `delete_documents` und der
Volltext-Export. Dazu der Lücken-Loop mit case-assist als Acht-Zeilen-Tabelle
und die zwei Stellen, an denen er üblicherweise hängt. Dieselbe Seite liegt
unter `https://case-assist.lan/hilfe`; die vollständige Referenz steht im
case-assist-Repo in `luecken-loop.md`.

## Volltext-Export

`GET /<project_id>/export` (Viewer-Port, default 5776) gibt die **indexierten
Dokumente im Volltext** zurück — als JSON, je Dokument `doc_key`, `titel`,
`hash` (aus dem Ingest-Manifest), `fundstelle` und `text` samt Metadaten-Header.

Der Endpunkt existiert für Systeme, die **dieselben Dokumente ein zweites Mal
brauchen**: case-assist startet Fälle daraus, statt sie erneut aus Paperless zu
ziehen. Beide Seiten arbeiten damit auf derselben Textbasis, und mit dem Text
kommen Dokumentschlüssel und Inhalts-Hash mit — die Anker, an denen ein Beleg
später hängen kann.

```bash
curl -s http://doc-graph:5776/akte/export | jq '.dokumente | length'
```

Bewusst **kein MCP-Tool**: eine Akte hat Megabytes und sprengt jedes
Token-Limit. Gelesen wird `kv_store_full_docs.json` direkt, ohne die
LightRAG-Instanz hochzufahren; während eines laufenden Ingests fehlt deshalb
das zuletzt eingefügte Dokument.

## Backup

Backups laufen **je Projekt** als eigenes `tar.gz` in einen gemounteten Ordner —
je Projekt ein Unterordner, analog ai-rem im selben OneDrive-Verzeichnis daneben:

```yaml
# docker-compose.yml
- ${DOC_GRAPH_BACKUP_PATH:-/shares/data/homes/markus/OneDrive/doc-graph}:/backups
```

Ablage: `<Backup-Ordner>/<project_id>/backup_<YYYY-MM-DD_HH-MM-SS>.tar.gz`.
Die Archiv-Wurzel ist die `project_id`, damit eine einzelne Datei für sich allein
wiederherstellbar ist (auch in ein noch nicht existierendes Projekt).

Bedienung komplett über die Viewer-Landing-Page (`http://mystorage:5776/`):

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
docker compose -f /var/local/mydocker/compose-files/doc-graph/docker-compose.yml down
tar -xzf /home/markus/mystorage/OneDrive/doc-graph/<project_id>/backup_<ts>.tar.gz \
    -C /var/local/mydocker/doc-graph/data/projects   # <project_id>/ -> data/projects/<project_id>
docker compose -f /var/local/mydocker/compose-files/doc-graph/docker-compose.yml up -d
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
| `ingest_control(project_id, action)` | Steuert einen laufenden Ingest: `pause` (hält nach dem Batch an), `resume` (weckt myai bei Bedarf, macht weiter), `stop` (bricht ab, bereits fertig Indexiertes bleibt). `stop`/`pause` wirken **sofort** — der laufende `ainsert` wird mitten im Batch abgebrochen; das abgebrochene Doc wird beim Re-Ingest neu geholt |
| `ingest_directory(project_id, subpath, regelwerk)` | .txt/.md/.pdf aus gemountetem Verzeichnis (PDF via pdftotext, kein OCR — gescannte Bilder über Paperless). Bekommt wie der Paperless-Pfad einen Metadaten-Header (Dateiname, Änderungsdatum, Ordnerpfad als Schlagworte), damit Datei-Dokumente im Graph nicht schlechter verankert sind. Läuft wie `ingest_paperless` im Hintergrund (steuerbar via `ingest_control`/`ingest_status`) und kehrt sofort zurück |
| `query(project_id, question, mode, only_context, max_total_tokens)` | Abfrage: local / global / hybrid / mix / naive. `only_context` ist **default True** (Claude formuliert aus dem Kontext); die lokale LLM-Formulierung ist auf geteilter GPU zu langsam. `max_total_tokens` (default 12000) deckelt den Kontext, damit er das MCP-Token-Limit nicht sprengt |
| `get_entity(project_id, entity_name, max_total_tokens)` | Alle Fakten/Relationen zu einer Entität — liefert wie `query` den **Kontext** (`only_need_context`), nicht die vom lokalen Modell formulierte Antwort; sonst läuft es auf geteilter GPU in den MCP-Timeout. `max_total_tokens` (default 12000) deckelt den Dump wie bei `query` |
| `get_clause(project_id, clause, document)` | **Regelwerk-Projekte:** exakter Wortlaut einer Klausel (`'§ 2'`, `'§2'`, `'2'`, `'Artikel 3'`) — deterministisch aus dem Klausel-Store, kein LLM/Retrieval. `document` filtert per Substring auf den Dokumenttitel |
| `graph_view(project_id)` | Interaktive HTML-Graphansicht, gibt Viewer-URL zurück |
| `rename_project(project_id, project_name)` | Setzt den Anzeigenamen (display name) eines Projekts; der technische project_id bleibt unverändert |
| `delete_documents(project_id, doc_keys, only_failed)` | Einzelne Dokumente aus dem Index entfernen (`adelete_by_doc_id`: Chunks/Entitäten/Vektoren/doc_status) — z.B. dup-Leichen oder Artefakt-Failures aufräumen. `only_failed=True` löscht alle `status==failed`. Quellen bleiben |
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
  LLM-Calls für die Extraktion — LightRAG macht **einen Call je Chunk**
  (`CHUNK_TOKEN_SIZE`, default 1200 Token). Gemessen 2026-08-20 auf dem
  llm-stack (qwen3-14B Q4, über RTX 3070 + 2060 layer-gesplittet, ein Slot):
  **~31 Token/s** Generierung, ~1000–3000 Ausgabe-Token je Call → **30–100 s
  pro Chunk**, ein 10k-Zeichen-Dokument also grob 2–5 Minuten. Ein Modell, das
  ganz auf **eine** Karte passt, spart den PCIe-Split und ist der größte Hebel.
  Danach nur Delta. Erstlauf am besten nachts starten.
  Der teure Teil läuft **im Hintergrund**: `ingest_paperless` startet die Extraktion
  und kehrt sofort zurück (sonst liefe der MCP-Call ins Timeout); Fortschritt/Ergebnis
  liefert `ingest_status(project_id)` (`running`/`done`/`error`). Der Status liegt nur im
  RAM — ein Container-Neustart mitten im Lauf verwirft ihn, das noch nicht gespeicherte
  Manifest sorgt dann beim nächsten Ingest für sauberes Nachholen.
- **Durchsatz messen statt raten.** `ingest_status` liefert während des Laufs
  `sec_per_doc` und `eta_min`, am Ende `elapsed_min`; pro Batch steht zusätzlich
  eine Zeile im Container-Log (`docker logs doc-graph`):
  `Ingest <projekt> Batch: 5 Docs / 48213 Zeichen in 812.4s (162.5s pro Doc)`.
  Die Uhr startet **nach** dem Wake-Hook, ein myai-Boot verfälscht die Zahl also
  nicht. Das ist die Referenz für jede Tuning-Änderung: Baseline auf einem festen
  Doc-Set nehmen, `delete_project`, Änderung, gleiches Set erneut.
- **Modellqualität = Graphqualität.** Wenn der Graph zu dünn wirkt
  (wenige Relationen), Extraktion mit größerem/anderem Modell wiederholen:
  `delete_project` + erneuter Ingest mit geändertem `LLM_MODEL`.
- **Voll-GPU-Extraktion auf myai — automatisch.** qwen3-14b + bge-m3 laufen
  dauerhaft auf myai (RTX 3070+2060, Repo `llm-stack`); myai darf schlafen.
  Vor jedem Ingest ruft der Server `ingest-begin.sh`: weckt myai per
  Wake-on-LAN (Magic Packet, Python-stdlib) und wartet, bis qwen (`:11436`)
  und der Embedder (`:11435`) antworten. `ingest-end.sh` ist ein No-op —
  es gibt nichts mehr zurückzuswappen, und paperless-ai bleibt vom Ingest
  komplett unberührt. Paralleler Ingest
  über mehrere Projekte weckt per Refcount nur einmal.
  Inserts laufen global serialisiert (LightRAG-Instanzen teilen
  den Pipeline-Lock — paralleles `ainsert` kehrt sonst unverarbeitet zurück), und
  ein Dokument gilt erst als indexiert, wenn LightRAG es wirklich `processed`
  meldet — sonst holt der nächste Ingest es automatisch nach.
  Abschaltbar via `INGEST_SWAP=0` (z. B. lokale Dev-Umgebung).
- **Wöchentlicher Modell-Check:** `model_check.sh` (via cron) ermittelt das
  aktuell geladene Extraktions-Modell per `docker exec` am laufenden Chat-Container
  (`llm-*` ohne `-embed`, `/v1/models`) ab. Der Claude-Agent recherchiert dann read-only, ob es ein besseres
  lokales LLM als das geladene gibt, und schreibt das Ergebnis nach
  `model_check_report.md` (`EMPFEHLUNG: bleiben` / `EMPFEHLUNG: wechseln zu <tag>`).
- **EMBED_DIM darf sich nachträglich nicht ändern** — Embedding-Modell pro
  Projekt festnageln, sonst Index neu aufbauen.
- **`INGEST_IGNORE_TAGS`** (default `paperless-ai`): Paperless-Tags, die **nicht**
  in den Metadaten-Header wandern. Der Header geht in den Manifest-Hash, und ein
  Dokument wird nur bei gleichem Hash übersprungen. paperless-ai schreibt seinen
  Bookkeeping-Tag in jedes verarbeitete Dokument zurück — ohne diesen Filter gilt
  jedes davon als geändert und läuft erneut durch die LLM-Extraktion. Gemessen
  2026-08-27 an `future-fund`: **230 von 263** Dokumenten wären bei *jedem* Lauf
  neu extrahiert worden, mit Filter sind es 33 (die echt geänderten). Komma-Liste;
  `""` schaltet den Filter ab. Fachliche Tags ändern den Hash weiterhin, eine
  nachträgliche Verschlagwortung wird also nach wie vor indexiert.
- **`EMBED_MAX_TOKENS`** (default 1800): Obergrenze pro Embedding-Input, muss
  `<=` dem `-ub` des llama-servers sein (llm-stack: 2048 auf beiden Hosts).
  1800 statt 2048, seit die Embeddings über den Router laufen: der Zeichen-Cap
  rechnet `EMBED_MAX_TOKENS * 3` und läge bei 2048 exakt auf der Grenze. Ein
  Überlauf-500 ist für den Router nicht von einem toten Backend zu
  unterscheiden — er würde den gesunden Host in den Cooldown schicken. Längere Inputs
  quittiert der Server mit HTTP 500 (`input ... is too large`), woraufhin
  LightRAG den **gesamten** Ingest-Lauf abbricht — nicht nur das betroffene
  Dokument. Der Cap greift für alles, was eingebettet wird: Chunks *und* die
  von LightRAG erzeugten Entity-/Relation-Beschreibungen, die
  `CHUNK_TOKEN_SIZE` prinzipiell nicht deckeln kann.
- **`EMBED_BATCH`** (default 64): Obergrenze für die *Anzahl* Inputs pro
  Embedding-Request — `EMBED_MAX_TOKENS` deckelt nur deren Länge. LightRAG
  übergibt beim Merge alle Entity-/Relation-Beschreibungen auf einmal; in einem
  gefüllten Index sind das schnell vierstellig viele. Gemessen: 1024 Inputs à
  6144 Zeichen = 100 s für einen einzigen Request, mal `EMBED_MAX_ASYNC`
  parallel deutlich über `EMBED_TIMEOUT`. Der Server bricht dann die Verbindung
  ab (*Server disconnected without sending a response*) und LightRAG hält die
  **gesamte** Pipeline an — alle noch offenen Dokumente bleiben liegen.
  Häppchenweises Senden hält jeden Request im Sekundenbereich. Nur erhöhen,
  wenn der Embedder auf einer GPU läuft.
- **`CHUNK_TOKEN_SIZE`** (default 1200 = LightRAG-Default): größere Chunks =
  halb so viele Extraktions-Calls (der ~3–4k-Token-Prompt-Overhead fällt pro
  Chunk an). Der frühere 600er-Default war ein Workaround gegen 480s-Worker-
  Timeouts bei CPU-Offload-Extraktion — mit Voll-GPU-qwen + `-n`-Output-Deckel
  obsolet. Wirkt nur auf **neu** indexierte Dokumente — für den Bestand
  `delete_project` + Re-Ingest.
- **`INGEST_BATCH`** (default 5): Dokumente pro `ainsert`-Batch. `>1` lastet
  LightRAGs Chunk-Parallelität (`MAX_ASYNC`) auch bei vielen kleinen Docs aus;
  Pause/Stop greifen zwischen Batches (ein Batch wird immer ganz zu Ende geführt).
  `=1` stellt das alte feingranulare Verhalten (Cancel/Fortschritt pro Doc) wieder her.
- **`PDFTOTEXT_TIMEOUT`** (default 120 s): Deckel je `pdftotext`-Aufruf beim
  Verzeichnis-Ingest. Ein kaputtes oder riesiges PDF hängt sonst den Scan
  unbegrenzt auf; nach dem Timeout gilt die Datei als nicht lesbar und zählt
  wie ein nicht unterstütztes Format. Der Scan selbst läuft in einem Thread
  (`asyncio.to_thread`) — sonst stünde während `rglob` + `pdftotext` der ganze
  MCP-Server, inklusive Queries auf anderen Projekten.
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
  nur Voll-GPU-Extraktion (qwen@myai), nicht ein höherer Timeout.
- **`MAX_ASYNC`** (default 3): parallele LLM-Calls. Jeder GPU-Host läuft mit
  `-np 1` (ein Slot; bewusst so, LightRAG-Query-Prompts sprengen mit ~14k Token
  einen 12288er-Slot), aber hinter dem Router sind es zwei Hosts — parallele
  Calls verteilen sich also auf beide statt zu serialisieren. Ist myubuntu aus,
  wartet der Überschuss auf myais einem Slot; das schadet nicht. Bei dichten
  Beständen / knapper GPU auf `1` setzen, damit ein Poison-Doc nicht den ganzen
  Durchsatz frisst.
- **`EMBED_MAX_ASYNC`** (default 3) / **`EMBED_TIMEOUT`** (default 180 s):
  Robustheit des Embedding-Pfads. Historisch lief `bge-m3` auf CPU; die
  LightRAG-Defaults (`max_async=8`, `timeout=30 s`) überfluteten ihn →
  `Worker execution timeout` → `IndexFlushError` → das **ganze Dokument failte**.
  Seit dem myai-Split läuft `bge-m3` dauerhaft auf myais RTX 2060 — die
  konservativen Werte bleiben trotzdem, sie kosten auf GPU praktisch nichts.
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
- **`MAX_DOC_ATTEMPTS`** (default `3`, `0` schaltet ab): **Failure-Deckel.**
  LightRAG setzt `failed`-Dokumente bei *jedem* `ainsert` selbsttätig auf
  `pending` zurück und leert dabei `error_msg` — ein Dokument, das reproduzierbar
  den LLM-Timeout reißt, wird damit endlos neu versucht und bremst jeden
  Folgelauf aus, ohne dass die Ursache irgendwo stehen bliebe. doc-graph zählt
  die Fehlversuche deshalb selbst (`ingest_attempts.json`, mit dem zuletzt
  gesehenen Fehlergrund) und legt ein Dokument nach `MAX_DOC_ATTEMPTS`
  erfolglosen Läufen endgültig beiseite: es wird aus LightRAG entfernt und wie
  ein übergroßes Doc in `ingest_flagged.json` geflaggt, mit dem Fehlergrund in
  der Begründung. Ein erfolgreicher Ingest löscht den Zähler wieder. Die
  Entscheidung im Viewer gilt genauso: `approve` nimmt das Doc vom Deckel aus
  (wird weiter versucht), `ignore` blendet es dauerhaft aus.
- **`ENTITY_TYPES`** (default `Person,Organisation,Ort,Datum,Dokument,Vorgang,`
  `Rechtsnorm,Betrag,Sache,Begriff`): Whitelist der Entity-Typen im
  Extraktions-Prompt. Ohne sie nimmt LightRAG seine elf englischen Defaults
  (u. a. `Creature`, `NaturalObject` — für Aktenkorpora sinnlos) und das Modell
  erfindet zusätzlich freie Typen, die der Viewer nur noch per Hash-Fallback
  einfärben kann. Eine knappe, zum Korpus passende Liste macht Typen zwischen
  Dokumenten vergleichbar; alles Übrige sortiert LightRAGs Prompt selbst nach
  `Other` ein. **Pro Projekt überschreibbar** über `entity_types` in der
  `meta.json` des Projekts (ein Bauakten-Projekt braucht andere Typen als eine
  Vertragssammlung). Wirkt nur auf **neu** indexierte Dokumente.
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
