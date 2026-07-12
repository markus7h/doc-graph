# doc-graph verproben — Ergebnis

**Datum:** 2026-07-11
**Case:** Teilungsversteigerung / Erbengemeinschaft Thomanek
**Korpus:** Paperless, Tag `doc-graph`
**Graph:** http://myubuntu:5776/doc-graph/graph.html (86 Entitäten, 117 Relationen)

## Golden Questions (VERPROBEN.md)

| Frage | Typ | Antwort (Auszug) | Korrekt? | Belegt? | Schneller? |
|---|---|---|---|---|---|
| Beteiligte der Erbengemeinschaft? | Aggregation | Erben Markus/Annemarie/Petra/Anna Maria Thomanek; Stenger Mediation (Dr. Stenger), WÆRK (RA Reclam), Dr. Baartz, SKN von Geyso (Iris Paduck) | zu prüfen | quervernetzt, ohne Einzel-Ref | ja |
| Welche Fristen/Termine? | Aggregation | 31.01.2025, 15.04.2025, 01.07.2025, Versteigerung **07.08.2026** AG Oldenburg i.H. | zu prüfen | **ja** – `[REF]` auf datierte Schreiben | ja |
| Aktenzeichen + Immobilien? | Fakt-Lookup | **19/26 RC14 SR**; Glüsinger Str. 49b Seevetal (VW 150.000 €), Ferienwohnung Fehmarn | zu prüfen | **ja** – Refs auf Quelldokumente | ja |

## Urteil

Auf dem **indexierten Teil** liefert doc-graph genau den KG-Mehrwert aus VERPROBEN.md:
Beteiligte und Fristen werden **über mehrere Schreiben aggregiert und belegt**.
Deutlicher Kontrast zum vorherigen Index (halluzinierte „VR-Nr. 203864" aus einem
Rentendokument) — jetzt konkrete, quellenverankerte Fakten.

Die genauen Zahlen/Namen muss der Nutzer gegen die eigene Ground Truth prüfen.

## Kernvorbehalt: Index unvollständig

Der Ingest läuft nicht durch. Letzter `doc_status`:

```
processed: 25, failed: 33, processing: 3, analyzing: 9, parsing: 6
```

Rund die Hälfte der Docs scheitert (u.a. Stenger-Mediation-Schreiben paperless:2159).
Damit sind **vollständigkeitsabhängige** Fragen („welche Fristen laufen *noch*")
noch nicht verlässlich.

## Fehlermodi (VERPROBEN.md §Zwei Fehlermodi)

- **Dünner Graph:** nein — 86 Entitäten / 117 Relationen bilden den Sachverhalt ab.
- **Halluzination bei Lücken:** aktiv relevant — nur belegte Antworten (mit `[REF]`)
  sind vertrauenswürdig, solange der Index Lücken hat.
- `only_context=True` als Pflicht-Gegencheck ist derzeit unbrauchbar (Antwort
  73.547 Zeichen > MCP-Token-Limit → Issue #2). Ersatzweise `[REF]`-Zitate geprüft.

## Empfehlung

Vor produktiver Nutzung:

1. Kleineres, voll GPU-taugliches `LLM_MODEL` setzen (24b OOM auf 16 GB → Issue #4).
2. Ingest von der MCP-Verbindung entkoppeln (300s-Timeout killt den Lauf → Issue #1).
3. `delete_project` + sauber neu indexieren, dann Test wiederholen.

**Fazit:** doc-graph funktioniert und liefert echten Mehrwert gegenüber manuellem
Paperless-Blättern — sobald der Ingest zuverlässig durchläuft.

---

## Nachtrag 2026-07-12 — Retrieval-Blocker behoben + Ersparnis-Einordnung

**Behobene Blocker (v0.1.8):**

- **Daten-Mount-Footgun:** Der Container mountete versehentlich das leere
  `./data` des Git-Repos statt das Deploy-Verzeichnis → **jede** Query lieferte
  `no-context`, obwohl der Index (154 Entitäten) existierte. `docker-compose.yml`
  nutzt jetzt einen **absoluten** Mount-Pfad. Danach greift das Retrieval sauber
  (Fehmarn-Fall: Parteien, Anwälte, AG Oldenburg, Objekte, Beträge belegbar da).
- **Query-Default `only_context=True`:** Die lokale LLM-Formulierung (llama-server)
  läuft auf geteilter GPU >300 s → MCP-Timeout. Default liefert jetzt nur den
  Kontext, Claude formuliert. (Löst das „73.547 Zeichen > Token-Limit"-Problem
  konzeptionell: der große Kontext-Dump wird vom Harness in eine Datei ausgelagert.)

**Wieviel Ersparnis? (gemessen am aktuellen Bestand: 28 Docs, 175 KB Volltext ≈ 44.000 Tokens, Ø 1.566 Tok/Schreiben)**

Die Ersparnis hängt am Fragetyp, nicht pauschal am Tool:

| Szenario | Ohne doc-graph | Mit doc-graph | Ersparnis |
|---|---|---|---|
| Erwiderung auf *ein konkretes* Schreiben (Bezüge bekannt) | Schreiben + 2–3 Refs reinkopieren ≈ 5–6K Tok | Query-Kontext ≈ 25K Tok | **negativ** — direkt reinkopieren ist billiger |
| Erwiderung mit *verstreuten* Fakten (Fristen/Zusagen/Beträge über alle Schreiben) | ganzer Bestand ≈ 44K Tok, *jede* Frage neu | fokussierter Kontext ≈ 25K Tok | **~40 %/Frage**, summiert über 5–10 Fragen |
| Großer Bestand (10×, ~280 Docs ≈ 440K Tok) | passt nicht ins Fenster → vorselektieren/mehrfach lesen | weiter ≈ 25K Kontext | **>90 %, und überhaupt erst machbar** |

**Der eigentliche Hebel ist nicht Token-Sparen**, sondern:
1. **Vollständigkeit** — ein Query zieht über alle Schreiben verstreute Fakten in
   einem Schritt zusammen, statt 28 PDFs durchzublättern und eins zu übersehen.
2. **Amortisierte Extraktion** — der teure Teil (Fakten aus jedem Doc ziehen) läuft
   **einmalig lokal auf der GPU** (kostet keine Claude-Tokens), nicht bei jeder Erwiderung neu.

**Ehrliche Grenze:** Bei kleinem Bestand + Punktfrage bringt doc-graph token-mäßig
nichts oder kostet sogar mehr; der Nutzen (Vollständigkeit, kein Neu-Durchsuchen)
und die Token-Ersparnis wachsen erst mit der Bestandsgröße deutlich.
