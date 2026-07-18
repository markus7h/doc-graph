"""Klausel-Splitting für Regelwerk-Projekte (Versicherungs-/Vertragsbedingungen).
Reine stdlib — testbar ohne Container: python test_clauses.py"""

import re

# Klausel-Header am Zeilenanfang: "§ 2 Begriffsbestimmungen", "§2", "Artikel 3 …",
# "Ziffer 4 …" — optionaler Buchstaben-Suffix (§ 12a). Verweise im Fließtext
# ("gemäß § 4 Abs. 2") stehen nicht am Zeilenanfang und matchen nicht.
# ponytail: OCR-Zeilenumbrüche können ein "§ 4" an den Zeilenanfang spülen ->
# vereinzeltes Über-Splitten möglich; get_clause liefert dann trotzdem den
# richtigen Wortlaut (nur ggf. ohne Rest-Satz davor). Feinere Heuristik erst
# bei echten Fehltreffern.
_HEADER_RE = re.compile(
    r"^[ \t]*(?P<kind>§|Artikel|Ziffer)\s*(?P<num>\d+[a-z]?)\b[ \t]*(?P<title>[^\n]*)$",
    re.MULTILINE | re.IGNORECASE,
)

_KIND_CANON = {"§": "§", "artikel": "Artikel", "ziffer": "Ziffer"}


def norm_clause(ref: str) -> tuple[str | None, str]:
    """Klausel-Referenz tolerant zerlegen: '§2' / '§ 2 Abs. 1' / '2' / 'Artikel 3'
    -> (kind, nummer). kind None = offen (Eingabe ohne §/Artikel/Ziffer)."""
    m = re.match(r"\s*(§|Artikel|Ziffer)?\.?\s*(\d+[a-z]?)", ref.strip(), re.IGNORECASE)
    if not m:
        return None, ref.strip().lower()
    kind = _KIND_CANON.get(m.group(1).lower()) if m.group(1) else None
    return kind, m.group(2).lower()


def split_clauses(text: str) -> tuple[str, list[dict]]:
    """(preamble, clauses) — clauses: [{clause_id, title, text}], text jeweils
    inkl. Header-Zeile. Weniger als 2 Header -> (text, []): ein einzelner
    Treffer ist eher Verweis/OCR-Artefakt als Gliederung -> kein Klausel-Doc."""
    matches = list(_HEADER_RE.finditer(text or ""))
    if len(matches) < 2:
        return (text or ""), []
    preamble = text[: matches[0].start()].strip()
    clauses = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        kind = _KIND_CANON[m.group("kind").lower()]
        clauses.append({
            "clause_id": f"{kind} {m.group('num').lower()}",
            "title": m.group("title").strip(" .:-–"),
            "text": text[m.start():end].strip(),
        })
    return preamble, clauses
