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
