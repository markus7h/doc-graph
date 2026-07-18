"""Selbsttest für clauses — reine stdlib, kein Container nötig.
Lauf: python test_clauses.py"""

from clauses import norm_clause, split_clauses

AVB = """Bedingungen für die Berufsunfähigkeitsrente (Altplan)
Stand: Januar 2012

§ 1 Was ist versichert?
Der Versicherer zahlt eine Rente, wenn die versicherte Person berufsunfähig ist.
Näheres regelt § 2 Abs. 1 dieser Bedingungen.

§ 2 Begriffsbestimmungen
(1) Berufsunfähigkeit liegt vor, wenn die versicherte Person außerstande ist,
ihren Beruf auszuüben.
(2) Maßgeblich ist der zuletzt ausgeübte Beruf.

§ 12a Verjährung
Ansprüche verjähren nach den gesetzlichen Vorschriften.
"""


def test_split():
    pre, cls = split_clauses(AVB)
    assert "Bedingungen für die Berufsunfähigkeitsrente" in pre
    assert [c["clause_id"] for c in cls] == ["§ 1", "§ 2", "§ 12a"]
    assert cls[0]["title"] == "Was ist versichert?"
    # Verweis "§ 2 Abs. 1" mitten im Fließtext darf NICHT splitten:
    assert "Näheres regelt § 2 Abs. 1" in cls[0]["text"]
    assert cls[1]["text"].startswith("§ 2 Begriffsbestimmungen")
    assert "(2) Maßgeblich" in cls[1]["text"]
    assert "Verjährung" in cls[2]["title"]


def test_no_structure():
    # Anschreiben mit einem einzelnen §-Verweis -> keine Klausel-Struktur
    letter = "Sehr geehrte Damen und Herren,\n§ 19 VVG verpflichtet Sie zur Anzeige.\nMfG"
    pre, cls = split_clauses(letter)
    assert cls == [] and pre == letter
    assert split_clauses("")[1] == []


def test_artikel_ziffer():
    text = "Artikel 1 Geltungsbereich\nGilt für alles.\nZiffer 2 Ausnahmen\nKeine."
    _pre, cls = split_clauses(text)
    assert [c["clause_id"] for c in cls] == ["Artikel 1", "Ziffer 2"]


def test_norm():
    assert norm_clause("§ 2") == ("§", "2")
    assert norm_clause("§2") == ("§", "2")
    assert norm_clause("2") == (None, "2")
    assert norm_clause("§ 2 Abs. 1") == ("§", "2")
    assert norm_clause("§ 12A") == ("§", "12a")
    assert norm_clause("Artikel 3") == ("Artikel", "3")
    assert norm_clause("ziffer 4") == ("Ziffer", "4")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok: {name}")
    print("Alle Tests grün.")
