# Gap-Report: doc-graph (Ist) vs. Fallassistenz-Ziel-Datenmodell

Stand: 2026-07-21 · Grundlage: [kg-fallassistenz-handover-claude-code.md](kg-fallassistenz-handover-claude-code.md)
Ist-Stand verifiziert am Code (`server.py`, `clauses.py`), nicht aus Gedächtnis.

---

## Kern-Befund (zuerst lesen)

doc-graph und das Ziel-Datenmodell sind **strukturell fast disjunkt**. doc-graph ist
ein generischer **LightRAG-Stock-Wrapper**: freitextliche Entity/Relationship-Extraktion
(ein LLM-Pass, Entitäten = Typ + Freitext-Beschreibung in `GRAPH_LANGUAGE`), plus als
einzige Eigenleistung §-weises Chunking + deterministischer Klausel-Store.

Das Ziel verlangt ein **streng typisiertes, zweischichtiges Fakt-Schema mit
Zwei-Pass-Dedup, Streitstand, Referenzgraph-Matching und deterministischer
Vollständigkeitsprüfung**. Fast nichts davon ist mit LightRAGs freiem Schema
kompatibel.

**Ehrliche Einordnung: Das ist kein „Architektur anpassen", sondern ein neues System,
das doc-graph als _Infrastruktur-Substrat_ wiederverwendet** (Container, N-Projekte-
Isolation, Ingest-Pipeline mit GPU-Swap/Pause/Stop, deterministischer Klausel-Store,
Graph-View). Die Extraktions- und Graph-Schicht ist Neubau/Fork, kein Patch — LightRAGs
Extraktionsmodell steht dem Ziel-Modell fundamental entgegen.

---

## Gap pro Ziel-Komponente

Bewertung: **trivial** (Config/kleiner Patch) · **mittel** (neues Modul, doc-graph-Muster
tragen) · **strukturell** (kollidiert mit LightRAG-Kern / eigener Graph-Layer nötig).

| # | Ziel (Handover-Abschnitt) | Ist in doc-graph | Gap | Bewertung |
|---|---|---|---|---|
| 1 | **Ereignis-Knoten, 6-Slot-Schema** (B.1) | LightRAG: freie Entitäten (Typ+Freitext-Descr), keine Slots | fehlt komplett; LightRAG-Extraktionsprompt kennt keine Slot-Struktur | **strukturell** |
| 2 | **Regelungs-/Dispositions-Knoten** (Wenn/Dann/Geltung) (B.2) | Klausel-Store `clauses.py`: §-Split + Volltext, **flach** (keine Bedingung/Folge) | Klausel-Store ist das nächste Vorbild, aber ohne innere Struktur → erweitern | **mittel** |
| 3 | **Typisierte Kanten** (verursacht/folgt-zeitlich/bedingt/entstammt) (B.3) | LightRAG-Relationships: Freitext-`description` + `keywords`, kein festes Kantentyp-Set | fehlt; LightRAG-Kanten sind untypisiert | **strukturell** |
| 4 | **Streitstand-Status** (unstreitig/behauptet/bestritten) auf Knoten+Kanten (B.3, E.6) | keinerlei Status-Attribut | fehlt komplett; LightRAG-Schema hat kein Feld dafür | **strukturell** |
| 5 | **Kern/Einordnung-Trennung** (harte Invariante) (B.4, E.1) | keine Trennung; LightRAG mischt Deskription + implizite Wertung frei | fehlt; braucht Extraktor-Prompt-Disziplin **+ Validator** (Blacklist subsumierender Begriffe) | **strukturell** |
| 6 | **Referenzgraph (BGB-Seite)**, ~15–20k Knoten, Tatbestandsmerkmale als Knoten (B.5) | existiert nicht (doc-graph indexiert nur den Fall/das Dokument-Korpus) | fehlt komplett; eigener kuratierter Graph = großes eigenes Vorhaben | **strukturell** |
| 7 | **Terminale wertende Prüfknoten** + 3-Zustands-Prüfgerüst (gedeckt/offen/wertend) (B.5) | nichts | fehlt; hängt an #6 | **strukturell** |
| 8 | **Zwei-Pass-Extraktion mit Delta-Speicherung** (C.1) | Single-Pass `ainsert`; Gleaning bewusst AUS (`server.py:42`) | fehlt; ~3× Extraktionskosten, laut Handover F.6 ohnehin _nach_ Einzelpass-Beweis | **strukturell** |
| 9 | **Fakt-Level-Dedup** (Kern ∧ Einordnung, Zweitlesungs-Erkennung) (C.2) | Dedup nur auf **Doc-Ebene** (Hash/Manifest, `_prepare_doc`); LightRAG dedupt Entitäten per Name-Merge | fehlt die Fakt-/Slot-Ebene komplett — die kritischste Zielkomponente | **strukturell** |
| 10 | **Graph-Distanz als Einordnungs-Äquivalenz** (C.2), Config-Schwelle | nichts (hängt an #6) | fehlt; Handover sagt selbst: nur Interface, Kalibrierung braucht echte Fälle | **strukturell** (Interface: mittel) |
| 11 | **Deterministische Vollständigkeitsprüfung** als eigenes Modul (D.3) | nichts; `get_clause` ist deterministisch, aber nur Abruf, keine Constraint-Prüfung | fehlt; eigenes testbares Modul (feste Graph-Queries/SHACL-artig) — baubar im doc-graph-Stil | **mittel** |
| 12 | **LLM nur auf abgerufenen Knoten** (kein Trainingswissen) (D.1, E.4) | LightRAG-Query hält sich grob daran; Extraktion nutzt aber Modellwissen frei | teils vorhanden, aber nicht erzwungen | **mittel** |
| 13 | **Kein OWL/Datalog-Reasoner** (D.2) | erfüllt (kein Reasoner vorhanden) | — | **erfüllt** |
| 14 | **Lücken → Rückfragen** statt stillem Übergehen (E.5) | nichts (Ingest ist fire-and-forget) | fehlt; hängt an #7/#11 | **mittel** |

---

## Was doc-graph mitbringt (wiederverwendbares Substrat)

- **Container + N-Projekte-Isolation** (`workspace=<project>`) — Fall-Graph und
  Referenzgraph als getrennte Projekte im selben Server denkbar.
- **Ingest-Pipeline** gehärtet: GPU-Swap-Refcount, Pause/Stop/Resume, Batch-`ainsert`,
  Manifest-Dedup auf Doc-Ebene, Poison-Doc-Guard. Trägt jede Extraktionslogik.
- **Deterministischer Klausel-Store** (`clauses.py`, `get_clause`) — zitierfähig, kein
  LLM. Direktes Vorbild/Basis für Regelungs-Knoten (#2) und die deterministische
  Vollständigkeitsprüfung (#11).
- **Graph-View** (`graphview.py`) — Rendering/Rename-Infrastruktur.
- **MCP-Tool-Surface** — neue Tools (z.B. `extract_case`, `check_completeness`) fügen
  sich ins bestehende Muster ein.

---

## Empfohlene Reihenfolge (folgt Handover F.6: „erst Andocken beweisen")

1. **Pilotgebiet Kaufrecht** als Referenzgraph-Projekt minimal modellieren (#6) —
   Tatbestandsmerkmale von §§ 434/437/323/476/477 als typisierte Knoten. Klein halten.
2. **Ereignis-/Fakt-Schema** (#1, #3, #4, #5) als **eigenen Graph-Layer neben LightRAG**
   definieren (nicht in LightRAGs freies Schema pressen). Extraktor-Prompt mit harter
   Kern/Einordnung-Grenze + **Validator-Blacklist** (#5, E.1).
3. **Einzelpass-Extraktion + Andocken** (Fall-Fakt → Referenzknoten) beweisen — der
   Kaufrechts-Testfall aus D.4 als **Regressionstest** (§ 476/§ 477 müssen automatisch
   mitziehen).
4. **Deterministische Vollständigkeitsprüfung** (#11) als eigenes Modul auf dem
   Referenzgraph.
5. **Zwei-Pass + Fakt-Dedup** (#8, #9) erst danach (~3× Kosten).
6. Graph-Distanz-Schwelle + Rechtsprechungs-Cluster: nur **Interface** vorsehen (#10, F.7).

---

## Konflikt-Dokumentation (Handover Schritt 5)

**Bestehende Architektur vs. Ziel — der eine strukturelle Konflikt:** LightRAGs
Extraktion erzeugt _freie, name-gemergte_ Entitäten mit Freitext-Beschreibung. Das
Ziel-Modell verlangt _streng typisierte Ereignis-Slots mit zweischichtiger Fakt-Identität
und Streitstand_. Beides im selben Store zu vereinen ist nicht sinnvoll — der Fall-Graph
sollte ein **eigener typisierter Layer** sein, nicht LightRAGs generischer KG. doc-graphs
LightRAG-Teil bleibt nützlich für unstrukturiertes Retrieval/Kontext, trägt aber nicht die
Fakt-Schicht. (Handover E.8 „Domänen-Trennung" stützt das.)

**Nicht in diesem Report gelöst** (= Handover F, bewusst offen): Referenzgraph-Modellierung
Kaufrecht, Regelungs-Knoten-Feinschema, Extraktor-Prompt-Design, Graph-Distanz-Kalibrierung.
