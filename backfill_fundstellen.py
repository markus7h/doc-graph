#!/usr/bin/env python3
"""Traegt Fundstellen in bestehende Projekte nach. Lauf im Container:

    python backfill_fundstellen.py --dry-run          # alle Projekte, nur Bericht
    python backfill_fundstellen.py future-fund        # ein Projekt, schreibend
    python backfill_fundstellen.py                    # alle, schreibend

Hintergrund: bis zum Fix rief _run_ingest LightRAGs ainsert ohne file_paths auf.
LightRAG setzt dann 'unknown_source' und ueberspringt genau diesen Wert beim
Bauen der Reference Document List — jede query-Antwort war damit unbelegbar.
Der Fix wirkt nur fuer NEU ingestierte Dokumente; dieses Script repariert den
Bestand.

Warum kein Re-Ingest: die Herkunft steht bereits in jedem Volltext (Metadaten-
Header aus _doc_to_text). Es ist ein reines Metadaten-Update — keine Embeddings,
keine LLM-Calls, Sekunden statt Stunden.

GRENZE: nur Chunks und Dokumente. Entities und Relationen tragen ihre Provenienz
ebenfalls als file_path, aber zur Extraktionszeit eingefroren im Graph; die
bleiben 'unknown_source', bis sie neu extrahiert werden. Fuer die Referenzliste
von query() ist das unerheblich — die kommt ausschliesslich aus den Chunks.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path("/data/projects")
UNKNOWN = "unknown_source"
# vdb_chunks liegt als {"data": [...]} vor, die uebrigen als {id: {...}}.
KV_STORES = ("kv_store_text_chunks.json", "kv_store_doc_status.json",
             "kv_store_full_docs.json")


def fundstelle(text: str) -> str:
    """Identisch zu server._fundstelle — bewusst dupliziert.

    ponytail: das Script laeuft als Einmal-Migration im Container, wo server.py
    einen MCP-Server hochfaehrt und Netz-Clients baut. Ein Import dafuer waere
    teurer als acht Zeilen Kopie, die nach dem Backfill ohnehin niemand mehr
    anfasst. Aendert sich das Header-Format, ist dieses Script Geschichte.
    """
    kopf: dict[str, str] = {}
    for zeile in text.split("\n\n", 1)[0].splitlines():
        schluessel, _, wert = zeile.partition(": ")
        if wert:
            kopf[schluessel.strip()] = wert.strip()
    teile = [kopf.get("Dokument") or "ohne Titel"]
    for feld in ("Datum", "Korrespondent"):
        if kopf.get(feld):
            teile.append(kopf[feld])
    return ", ".join(teile)


def _laden(pfad: Path):
    return json.loads(pfad.read_text()) if pfad.exists() else None


def _sichern(pfad: Path, daten, stempel: str) -> None:
    shutil.copy2(pfad, pfad.with_suffix(f".json.bak-{stempel}"))
    pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=2))


def backfill(projekt: Path, dry: bool) -> dict:
    volltexte = _laden(projekt / "kv_store_full_docs.json")
    if not volltexte:
        return {"projekt": projekt.name, "uebersprungen": "kein kv_store_full_docs.json"}

    # doc_id -> Fundstelle, aus dem Metadaten-Header des Volltexts.
    labels = {doc_id: fundstelle(eintrag.get("content") or "")
              for doc_id, eintrag in volltexte.items()}
    ohne_header = sum(1 for v in labels.values() if v == "ohne Titel")

    stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
    bericht = {"projekt": projekt.name, "docs": len(labels),
               "ohne_header": ohne_header, "geaendert": {}}

    for name in KV_STORES:
        pfad = projekt / name
        daten = _laden(pfad)
        if daten is None:
            continue
        n = 0
        for eintrag_id, eintrag in daten.items():
            if not isinstance(eintrag, dict) or eintrag.get("file_path") != UNKNOWN:
                continue
            # full_docs sind selbst das Dokument, Chunks zeigen per full_doc_id darauf.
            label = labels.get(eintrag.get("full_doc_id", eintrag_id))
            if label:
                eintrag["file_path"] = label
                n += 1
        if n:
            bericht["geaendert"][name] = n
            if not dry:
                _sichern(pfad, daten, stempel)

    # Vektorstore: gleiche Felder, andere Struktur ({"data": [...]}).
    pfad = projekt / "vdb_chunks.json"
    daten = _laden(pfad)
    if daten and isinstance(daten.get("data"), list):
        n = 0
        for eintrag in daten["data"]:
            if eintrag.get("file_path") != UNKNOWN:
                continue
            label = labels.get(eintrag.get("full_doc_id"))
            if label:
                eintrag["file_path"] = label
                n += 1
        if n:
            bericht["geaendert"]["vdb_chunks.json"] = n
            if not dry:
                _sichern(pfad, daten, stempel)
    return bericht


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("projekte", nargs="*", help="leer = alle")
    ap.add_argument("--dry-run", action="store_true", help="nur berichten, nichts schreiben")
    args = ap.parse_args()

    ziele = ([PROJECTS_DIR / p for p in args.projekte] if args.projekte
             else sorted(p for p in PROJECTS_DIR.iterdir() if p.is_dir()))
    for ziel in ziele:
        if not ziel.is_dir():
            print(f"{ziel.name}: kein Projektverzeichnis", file=sys.stderr)
            return 2
        b = backfill(ziel, args.dry_run)
        if b.get("uebersprungen"):
            print(f"{b['projekt']}: uebersprungen ({b['uebersprungen']})")
            continue
        summe = sum(b["geaendert"].values())
        warn = f"  ACHTUNG {b['ohne_header']} Docs ohne Header" if b["ohne_header"] else ""
        print(f"{b['projekt']}: {b['docs']} Docs, {summe} Eintraege "
              f"{'wuerden geaendert' if args.dry_run else 'geaendert'}{warn}")
        for name, n in b["geaendert"].items():
            print(f"    {name:34s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
