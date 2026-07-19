# Ontologie, Inferenz & Knowledge-Graph-Findings

## 1. Begriff Ontologie

**Philosophie:** Lehre vom Sein (Teilgebiet der Metaphysik). Beschäftigt sich mit der Frage, was existiert und welche Arten von Dingen es gibt (Objekte, Eigenschaften, Beziehungen, Ereignisse).

**Informatik:** Formale, explizite Spezifikation einer gemeinsamen Konzeptualisierung eines Wissensbereichs. Ein strukturiertes Modell, das definiert:

- **Konzepte/Klassen** – die relevanten Entitätstypen einer Domäne
- **Beziehungen** zwischen ihnen
- **Eigenschaften/Attribute**
- **Regeln/Axiome** – logische Einschränkungen

Ziel: Menschen *und* Maschinen können die Bedeutung von Daten eindeutig interpretieren. Sprachen: RDF, RDFS, OWL (Semantic Web).

**Unterschied zum einfachen Datenmodell:** Eine Ontologie ist reichhaltiger, weil sie logisches Schließen (Inferenz) erlaubt – aus explizit modelliertem Wissen lassen sich implizite Fakten ableiten.

## 2. Beispiele für Inferenzen

| Inferenzart | Beispiel |
|---|---|
| **Transitivität** | Rad `istTeilVon` Motor, Motor `istTeilVon` Auto → Rad `istTeilVon` Auto |
| **Klassenhierarchie (Subsumption)** | Bello ist `Hund`, `Hund` ⊆ `Säugetier` ⊆ `Tier` → Bello ist `Säugetier` und `Tier` |
| **Vererbung von Eigenschaften** | `Säugetier` „hat Rückgrat", Bello ist `Hund` → Bello hat Rückgrat |
| **Inverse Beziehungen** | `hatElternteil` invers zu `hatKind`; Anna `hatKind` Tom → Tom `hatElternteil` Anna |
| **Symmetrie** | `istVerheiratetMit` symmetrisch; Anna–Ben → Ben–Anna |
| **Domain/Range-Restriktion (Typinferenz)** | `unterrichtet` hat Domain `Lehrer`; Frau Schmidt `unterrichtet` Mathe → Frau Schmidt ist `Lehrer` |
| **Property Chains** | `hatOnkel` = `hatElternteil` + `hatBruder`; Tom→Anna→Klaus → Tom `hatOnkel` Klaus |
| **Disjunktheit (Konsistenzprüfung)** | `Mann` und `Frau` disjunkt; X ist beides → Reasoner meldet Inkonsistenz |

Die ersten sieben erzeugen neues Wissen, die letzte prüft die Konsistenz. Ausführung durch einen Reasoner (HermiT, Pellet, ELK) über OWL-Axiome.

## 3. Reasoner vs. Graph-Datenbank

Reasoner und Graph-DB sind **grundsätzlich getrennte Komponenten**, auch wenn manche Produkte sie zusammen ausliefern.

- **Graph-DB** (z. B. Neo4j, Amazon Neptune): Speicherung und Traversierung/Querying von Knoten und Kanten. Kein logisches Schließen im OWL-Sinne.
- **OWL-Reasoner** (HermiT, Pellet, ELK): separate Inferenz-Engine auf Basis formaler Semantik (Description Logic). Leitet neue Fakten ab oder prüft Konsistenz.

**Zwei Welten:**

- **Property-Graph-DBs** (Neo4j etc.): typischerweise *kein* eingebauter Reasoner, kennen keine OWL-Axiome. „Inferenz" über Query-Logik (Cypher-Pattern), Graph-Algorithmen oder anwendungsseitig.
- **RDF-Triplestores** (GraphDB/Ontotext, Stardog, AllegroGraph, Apache Jena/Fuseki): bringen Reasoning-Fähigkeiten mit, oft als integrierte oder zuschaltbare Komponente. Der Reasoner bleibt logisch eine eigene Schicht.

**Zwei Reasoning-Ansätze:**

- **Materialisierung (forward chaining):** Alle ableitbaren Tripel werden beim Laden vorab berechnet und gespeichert. Schnelle Queries, mehr Speicher, Aufwand bei Updates (z. B. GraphDB).
- **Query-Zeit-Reasoning (backward chaining):** Ableitungen erst zur Abfragezeit. Kein Zusatzspeicher, langsamere Queries (z. B. Stardog).

Fazit: Der Reasoner ist konzeptionell eine eigenständige Inferenzschicht. „Bestandteil der DB" hängt vom Produkt ab – bei RDF-Triplestores meist ja, bei Property-Graph-DBs meist nein.

## 4. Isolierte Entitäten (ohne Kanten) löschen?

Pauschales Löschen ist riskant. Isolierte Knoten sind meist ein **Symptom, kein Problem an sich**.

**Wann legitim:**
- Eigenständiges Wissen, das nur über Attribute/Properties beschrieben ist
- Im Aufbau befindlicher Graph: „noch nicht verknüpft" ≠ „irrelevant"

**Wann Löschen sinnvoll – vorher Ursache prüfen:**
- **Fehlgeschlagenes Entity Linking/Matching** → Merging statt Löschen (Duplikat, Schreibvariante)
- **Unvollständige Extraktion** → Fehler in der Pipeline beheben
- **Verwaiste Reste** nach Löschungen/Teilimporten → meist echt entfernbar

**Praktisches Vorgehen:**
- Isolierte Knoten als **Qualitäts-Signal** behandeln
- Identifizieren – Cypher: `MATCH (n) WHERE NOT (n)--() RETURN n`; RDF analog über SPARQL
- Triagieren: reparieren, mergen oder gezielt entfernen
- Löschen reversibel bzw. protokolliert halten

**Wichtiger Vorbehalt bei RDF/OWL:** „Keine Kante" ist trügerisch – Kanten können durch Inferenz erst entstehen. Vor dem Aufräumen erst materialisieren bzw. den inferierten Graphen betrachten, sonst löscht man eingebundene Entitäten.

**Fazit:** Als automatischer, blinder Cleanup-Schritt eher nein. Als kuratierter Triage-Schritt mit vorheriger Ursachenanalyse durchaus ja.
