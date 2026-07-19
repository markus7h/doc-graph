# Handover: Fall-zu-Referenzgraph-Assistenz — Architektur-Findings & Umsetzungsauftrag

Stand: Juli 2026 · Übergabe-Dokument für Claude Code
Ziel-Repo: bestehendes doc-graph Repository

---

## AUFTRAG AN CLAUDE CODE

Dieses Dokument enthält die konsolidierten Architektur-Entscheidungen einer Design-Session. Deine Aufgabe:

1. **Analysiere die bestehende doc-graph-Architektur** und mappe sie gegen das hier spezifizierte Ziel-Datenmodell (Abschnitt B).
2. **Erstelle einen Gap-Report**: Was existiert schon, was muss angepasst werden, was fehlt komplett. Bewerte pro Punkt: trivial / mittel / strukturell.
3. **Passe die Architektur an** — in dieser Prioritätsreihenfolge:
   - Datenmodell (Ereignis-Knoten mit 6-Slot-Schema, Regelungs-Knoten, typisierte Kanten mit Streitstand-Status)
   - Trennung Sachverhaltskern / Einordnungs-Schicht (harte Invariante, siehe B.4)
   - Zwei-Pass-Extraktion mit Fakt-Level-Dedup (Abschnitt C)
   - Deterministische Vollständigkeitsprüfung als eigene Komponente (Abschnitt D.3)
4. **Nicht umsetzen, nur als Interface vorsehen**: Graph-Distanz-Schwelle (Kalibrierungsparameter, braucht echte Fälle), Rechtsprechungs-Cluster an terminalen Prüfknoten.
5. Halte alle Invarianten aus Abschnitt E ein. Bei Konflikt zwischen bestehender Architektur und diesem Dokument: dieses Dokument gewinnt, aber dokumentiere den Konflikt.

---

## A. Vision & Positionierung

**Kernidee:** Nicht der statische Rechtswissens-Graph ist der Mehrwert (Noxtua/Beck bauen das bereits), sondern die **Verknüpfung des individuellen Falls mit dem Referenzgraphen** — strukturierte Subsumtionsunterstützung.

**Zielgruppe:** Volljuristen. Die Wertung bleibt explizit beim Menschen; das System liefert Struktur, Vollständigkeit, Absicherung („hast du an § X gedacht?"). Rechtlich sauberer (Haftung) und technisch ehrlicher.

**Übertragbarkeit als Designziel:** Dieselbe Maschine funktioniert in der Medizin (Patientenakte → medizinischer Referenzgraph aus Leitlinien/SNOMED/ICD). Der wiederverwendbare Kern ist der **Extraktions- und Matching-Apparat**, nicht die Fachschicht. Übertragen wird das Skelett (Kern-Schema + Vergleichsmechanik), nicht die Organe (Referenzgraphen, Normalisierer). Achtung Medizin: MDR/Medizinprodukt-Regulatorik ab diagnostischer Entscheidungsunterstützung.

**Marktlücke (Recherche Juli 2026):** Noxtua 5 / Beck-Noxtua = recherche-getriebener KG (digitaler Zwilling, beck-online, 60M+ Dokumente). Wolters Kluwer = KG-Suche über Urteile (Forschung). Breiter Markt = LLM/RAG ohne Graph. **Niemand baut fallgetriebene Subsumtionsassistenz mit Vollständigkeitsgarantie.**

## B. Ziel-Datenmodell (Ergebnis des Stresstests)

Zentrale Erkenntnis: **Ein Fall ist kein Set von Fakten, sondern ein Graph von Fakten.** Fall-Graph und Referenzgraph sind strukturell gleichartig (Knoten + typisierte Kanten); das Andocken ist Graph-zu-Graph-Matching.

### B.1 Knotentyp 1: Ereignis (6-Slot-Schema)

Slots sind **ereignis-logisch, nicht fachlich** — sie beschreiben Geschehen, keine Rechts-/Medizinbegriffe:

| Slot | Inhalt | Hinweise |
|---|---|---|
| 1. Agens | wer/was löst aus | Person, Stoff, Prozess — nicht nur „Handelnder" |
| 2. Aktion/Vorgang | was geschieht | Verb-Kern; auch Zustandsereignisse („Gewinn entgeht") |
| 3. Patiens/Objekt | woran/an wem | |
| 4. Gegenpartei/Betroffener | zu wessen Gunsten/Lasten | Recht: fast immer besetzt; Medizin: oft leer — zulässig |
| 5. Zeit | wann / Reihenfolge | muss **relative Zeit** tragen („nach Gefahrübergang") — oft das entscheidende Merkmal |
| 6. Modalität/Qualifikatoren | wie/Umfang/Umstände | „heimlich", „30.000 €", „therapieresistent" — **trägt oft die Zweitlesung**, nie bei Äquivalenzprüfung ignorieren |

Rollen (Agens/Gegenpartei) gelten **pro Ereignis**; Entitäts-Identität ist global (V kann in E1 Gegenpartei, in E2 Agens sein). Entitätsauflösung ist Normalisierungsaufgabe, kein Schema-Problem.

### B.2 Knotentyp 2: Regelung/Disposition

Klauseln, Vereinbarungen, Verfügungen, Verordnungen sind **keine Ereignisse** (kein Geschehen, kein Zeitpunkt des Passierens), sondern von Parteien gesetzte konditionale Normsätze. Eigene innere Struktur:

- **Bedingung** (Wenn-Teil)
- **Folge** (Dann-Teil)
- **Geltungsbereich / Parteien**
- Kante `entstammt` → zum Vereinbarungs-Ereignis (der Vertragsschluss selbst IST ein Ereignis)

Beispiel: „Lieferverzögerungen durch Vorlieferanten gehen nicht zu Lasten des V" → Bedingung: Verzögerung durch Vorlieferant; Folge: Haftungsausschluss V. Nur so kann AGB-Kontrolle (§§ 305 ff.) deterministisch andocken. Medizin-Pendant: konditionale Verordnung („nimm X falls Schmerz > Stufe 5").

### B.3 Typisierte Kanten (zwischen Fall-Knoten)

Kausalität ist **keine Modalität, sondern eine Kante**. Mindest-Kantentypen:

- `verursacht` (starke Kausalbehauptung)
- `folgt-zeitlich` (schwächer; oft weiß der Extraktor nur die Abfolge sicher)
- `bedingt` / `ermöglicht`
- `entstammt` (Regelung ← Ereignis)

**Jede Kante und jeder Knoten trägt Streitstand-Status:** `unstreitig` / `behauptet(von wem)` / `bestritten`. Juristisch essentiell (Beweislast operiert genau darauf, vgl. § 477 Beweislastumkehr); Medizin-Pendant: „nach Medikationsbeginn aufgetreten" (Abfolge) vs. „durch Medikament verursacht" (Hypothese).

### B.4 Einordnungs-Schicht (strikt getrennt)

**Fakt = Sachverhaltskern (B.1–B.3) + rechtliche/fachliche Einordnung.** Die Einordnung ist ein Verweis auf Knoten im Referenzgraphen, NIEMALS Inhalt der Kern-Slots.

**Harte Invariante:** Kern-Slots nur wertungsfrei-deskriptiv. „Heimlich 30.000 € entnommen" erlaubt (Tatsache), „unterschlagen" verboten (Subsumtion). Einordnung im Kern = Zweitlesungs-Erkennung tot. Diese Grenze muss im Extraktor-Prompt hart gezogen und idealerweise durch einen Validator geprüft werden (Blacklist subsumierender Begriffe pro Domäne als pragmatischer Start).

### B.5 Referenzgraph (BGB-Seite)

- Größenordnung mittlere Granularität: ~15–20k Knoten, ~40–70k Kanten. Graphentechnisch klein; Aufwand liegt in Extraktion/Ontologie-Design.
- **Tatbestandsmerkmale als eigene Knoten** — ohne sie keine Andockpunkte.
- **Unbestimmte Rechtsbegriffe** („Fahrlässigkeit", „Treu und Glauben") als **terminale wertende Prüfknoten**: Graph übergibt dort bewusst an LLM+Mensch, idealerweise mit angehängtem Rechtsprechungs-Cluster (Interface vorsehen, Befüllung später).
- Prüfgerüst-Knoten haben drei Zustände zur Laufzeit: **gedeckt** / **offen** (Tatsachenlücke → Rückfrage erzeugen, nie stillschweigend übergehen) / **terminal wertend**.

## C. Zwei-Pass-Extraktion mit Delta-Speicherung

### C.1 Mechanik

- **Pass 1** (kontextgeleitet, spezifisches Schema) und **Pass 2** (kontextfrei, breit) sehen **beide den vollen Text** — finden soll jeder alles.
- Pass 2 **speichert nur das Delta**: was Pass 1 noch nicht als Fakt abgelegt hat.
- **Herkunft = Flag**: Pass-1-Einträge = Regelfall/hohe Konfidenz. Pass-2-Einträge = **per Konstruktion der Eskalations-Feed** („passt nicht ins erwartete Bild").
- Bei Dubletten: „auch von Pass 2 bestätigt"-Häkchen am Pass-1-Eintrag (Konfidenzsignal nicht verlieren).
- **Voller Pass-2-Feed = Signal, dass der Kontext selbst falsch sein könnte** → im UI als Gegenhypothese behandeln, nicht als Randnotizen.
- Beide Extraktionen + Herkunft vollständig persistieren (Audit-Trail).
- Kontext-Vorwissen generell: als **widerlegbare Hypothese, nie als Filter**. System soll die Vorannahme des Experten herausfordern können.

### C.2 Fakt-Identität (Dedup-Kern — kritischste Komponente)

Die „schon gespeichert?"-Prüfung arbeitet auf **Fakt-Ebene, nie auf Textspan-Ebene**. Derselbe Span kann zwei Fakten tragen (Konto-Entnahme = Zugewinn-Position UND Delikts-Handlung). Span-Dedup tötet die Zweitlesung.

**Dublette nur wenn: Kern äquivalent UND Einordnung äquivalent.**

| Kern | Einordnung | Kategorie | Behandlung |
|---|---|---|---|
| gleich | gleich | echte Dublette | nicht speichern, Konfidenz-Häkchen |
| gleich | verschieden | **Zweitlesung** | speichern, höchste Eskalationspriorität |
| verschieden | gleich | paralleler Fakt | speichern, selbes Prüfraster |
| verschieden | verschieden | unabhängiger Neufund | speichern |

**Zweitlesung** (Kern-Match + Einordnungs-Mismatch) = formale Signatur des übersehenen Nebenschauplatzes — die Kategorie, für die das System existiert. Gilt auch für **Kanten**: gleiche Ereignisse, aber Pass 2 zieht eine Kausal-Kante, die Pass 1 nicht sah = Kanten-Zweitlesung, ebenfalls eskalieren.

**Kern-Äquivalenz:** Slot-Alignment + Normalisierung (Beträge, Daten, Entitätsauflösung). LLM stark, wenn Slot-Struktur vorgegeben (nie Freitext-gegen-Freitext vergleichen lassen). **Vorsichtsregel:** Slot-Subsumtion (ein Fakt spezifischer, z. B. + „heimlich") ≠ Äquivalenz — der spezifischere Fakt kann die neue Einordnung erst tragen. Im Zweifel neuer Fakt.

**Einordnungs-Äquivalenz = Graph-Distanz im Referenzgraphen.** Norm-Identität ist zu brüchig (§ 437/§ 323 = derselbe Rücktritts-Komplex; § 823 I / § 823 II i.V.m. § 266 StGB = derselbe Delikts-Angriff). Nahe/verbundene Knoten = gleiche Einordnung. Wiederverwendet den Referenzgraphen — kein separater Mechanismus.

**Operativer Ablauf (Pass 2, pro Kandidat):**
1. Kandidat in Slots + Einordnung zerlegen
2. Pass-1-Fakten mit Kern-Slot-Überlappung vorfiltern (nicht gegen Gesamtbestand testen)
3. Kern-Äquivalenz prüfen (Slot-Alignment, Normalisierung, Subsumtions-Vorsichtsregel)
4. Bei Kern-Match: Graph-Distanz der Einordnungen prüfen
5. Kategorie zuweisen, entsprechend speichern/flaggen

**Kalibrierungsparameter (NICHT hart implementieren, als Config):** Graph-Distanz-Schwelle. Braucht echte Fälle. Grundeinstellung bewusst **zu weit** — Fehlerkosten asymmetrisch: überflüssige Eskalation = ein Klick, verschluckter Nebenschauplatz = Haftungsfall.

## D. Architekturprinzipien (GraphRAG / neuro-symbolisch)

### D.1 Rollenteilung

- **Graph** = Quelle der Wahrheit + präzises Retrieval-Substrat. Verweisketten/Konkurrenzen sind Kanten, keine semantischen Ähnlichkeiten — Vektor-RAG findet sie nicht zuverlässig.
- **LLM** = Reasoning-Engine für Extraktion und Wertung. Darf **nur abgerufene Knoten** verarbeiten, nichts aus Trainingswissen ergänzen (Halluzinations-Einhegung; Aktualität via Graph-Update statt Retraining).
- Rollen-Trennung erwägen: ein Modell für Sprachverständnis/Subsumtion, eines für Query-/Code-Präzision.

### D.2 Kein klassischer Reasoner

Kein OWL-DL-Reasoner, kein separater Datalog-Layer. OWL ist monotone Open-World-Logik; juristische Subsumtion ist defeasible (Regel-Ausnahme, „es sei denn", Beweislastumkehr). Das LLM traversiert und erklärt Verweisketten selbst. Taxonomie-Hierarchien schlicht als Kanten modellieren.

### D.3 Vollständigkeitsprüfung: deterministisch, eigene Komponente

**Der Verkaufswert „du übersiehst nichts" darf nie an nicht-deterministischem LLM-Traversal hängen.** Feste Graph-Queries / SHACL-artige Constraints beantworten: „Welche Tatbestandsmerkmale von § X sind noch nicht adressiert?" LLM wertet einzelne Merkmale; ob ALLE Merkmale abgearbeitet sind, prüft Code. Diese Komponente als eigenes, testbares Modul bauen.

### D.4 Validierter Kernwert (Kaufrechts-Testfall)

Gebrauchtwagen, „wie gesehen", verdeckter Getriebeschaden, V arglistig, K Verbraucher, Begehren Rücktritt. Das deterministische Auffächern zog automatisch mit: § 477 (Beweislastumkehr) und § 476 (Klauselgrenze) — **obwohl im Sachverhalt nicht erwähnt**. Zwei unabhängige Begründungswege sichtbar (Arglist § 444 UND § 476). Fristsetzungs-Lücke als Rückfrage markiert. Das ist der Kernwert; Regressionstest daraus bauen.

**Verletzlichste Stelle:** die Fall-Extraktion. Übersieht der Extraktor die Verbrauchereigenschaft, fallen §§ 474 ff. komplett und unbemerkt weg. Extraktionsfehler propagieren durch die ganze Kette. → Extraktions-Disziplin: Rohtatsachen notieren, Einordnung offen lassen, fehlende entscheidungserhebliche Tatsachen als Lücke mit Rückfrage erzeugen.

## E. Invarianten (bei jeder Änderung einhalten)

1. **Kern/Einordnung-Trennung:** Kein subsumierender Begriff in Kern-Slots. Validator vorsehen.
2. **Dedup nur auf Fakt-Ebene** (Kern + Einordnung), nie auf Textspan-Ebene.
3. **Vollständigkeitsprüfung deterministisch**, nie LLM-Ermessen.
4. **LLM verarbeitet nur abgerufene Graph-Knoten**, ergänzt nichts aus Trainingswissen.
5. **Lücken werden Rückfragen**, nie stillschweigend übergangen.
6. **Streitstand-Status** auf allen Fall-Knoten und -Kanten.
7. **Eskalations-Feed (Pass-2-Herkunft) nie unterdrücken** oder wegfiltern; voller Feed = Kontext-Gegenhypothese.
8. **Domänen-Trennung:** Ereignis-Slots und Vergleichsmechanik bleiben fachsprachfrei; alles Fachliche lebt in Einordnungs-Schicht, Referenzgraph und Werte-Normalisierern.

## F. Offene Punkte (nicht in diesem Durchgang lösen)

1. Graph-Distanz-Schwelle kalibrieren (braucht echte Fälle; Config-Parameter mit bewusst weiter Grundeinstellung)
2. Regelungs-Knoten-Schema verfeinern (minimale innere Struktur für AGB-Kontroll-Andocken)
3. Extraktor-Prompt-Design: harte deskriptiv/subsumierend-Grenze + Validator-Blacklist
4. BGB-Referenzgraph Pilotgebiet Kaufrecht konkret modellieren
5. Kontext-Herkunft: Nutzer wählt vs. System erschließt (unterschiedliche Fehlerprofile)
6. Prototyp-Reihenfolge: erst Einzelpass + Andocken beweisen, Zwei-Pass danach (≈3x Extraktionskosten)
7. Rechtsprechungs-Cluster an terminalen Prüfknoten (Interface jetzt, Befüllung später)
