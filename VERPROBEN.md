# doc-graph verproben — bringt es was?

Der einzige ehrliche Test ist ein **A/B gegen den Ist-Zustand**: heute
beantwortest du Dokumentfragen durch manuelles Durchblättern in Paperless.
doc-graph ist genau dann hilfreich, wenn es dieselben Fragen **korrekt, belegt
und schneller** beantwortet. Alles andere ist Bauchgefühl.

## 30-Minuten-Test

### 1. Echten Case wählen, nicht Spieldaten
Nimm einen Paperless-Bestand, den du wirklich kennst und der wehtut — viele
Dokumente, Fakten verstreut über Schreiben und Zeit. Der
Teilungsversteigerungs-Fall ist der ideale Typ: Fristen, Chronologie,
wer-schrieb-wann.

```
ingest_paperless(project="doc-graph", tag="doc-graph")
```

### 2. 5–8 Golden Questions, wo DU die Antwort kennst
Mischung aus drei Fragetypen:

| Typ | Beispiel | Modus |
|---|---|---|
| Fakt-Lookup | „Welches Aktenzeichen hat das AG Oldenburg vergeben?" | `hybrid` |
| Aggregation über Dokumente | „Welche Fristen laufen noch?" | `hybrid` |
| Chronologie | „Reihenfolge aller Schreiben zur Grundschuld" | `global` |

Aggregation und Chronologie sind der Punkt, an dem ein KG gegen die
Volltextsuche gewinnt — dort zuerst hinschauen.

### 3. Bewerten — drei Spalten reichen

| Frage | Korrekt? (ja/teilw./falsch) | Belegt? | Schneller als selbst suchen? |
|---|---|---|---|
| … | | | |

- **Belegt** heißt: mit `only_context=True` gegenprüfen — nennt die Antwort die
  echte Quell-Chunk, oder hat das Modell frei formuliert?
  ```
  query(project="doc-graph", question="…", only_context=True)
  ```

### 4. Entscheidungsregel
**Hilfreich**, wenn Aggregations-/Chronologie-Fragen zuverlässig stimmen UND
belegt sind. Reine Einzelfakt-Lookups kann die Paperless-Volltextsuche oft schon
— dafür lohnt kein KG.

## Zwei Fehlermodi, auf die du achten musst

- **Dünner Graph:** mistral extrahiert zu wenige Relationen → Aggregationsfragen
  fallen auseinander. Gegenmittel: `graph_view(project)` öffnen und schauen, ob
  die Kanten den Sachverhalt abbilden. Zu dünn → Ingest mit größerem `LLM_MODEL`
  wiederholen (`delete_project` + erneut ingesten).
- **Halluzination bei Lücken:** fehlt die Info im Index, formuliert das Modell
  trotzdem etwas. Deshalb `only_context=True` als Pflicht-Gegencheck bei allem,
  was juristisch/faktisch zählt.
