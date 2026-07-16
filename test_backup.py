"""Selbsttest für die Backup-Logik in server.py (per-Projekt-Backups).
Lauf im Container (dort liegt lightrag): docker exec doc-graph python test_backup.py"""

import os
import tarfile
import tempfile
from pathlib import Path

# Env vor dem Import setzen — server.py liest die Pfade beim Modul-Laden.
_tmp = Path(tempfile.mkdtemp())
os.environ["PROJECTS_DIR"] = str(_tmp / "projects")
os.environ["BACKUP_DIR"] = str(_tmp / "backups")
os.environ["MAX_BACKUPS"] = "3"
os.environ["INGEST_SWAP"] = "0"
(_tmp / "projects" / "p1").mkdir(parents=True)
(_tmp / "projects" / "p1" / "kv_store.json").write_text('{"a": 1}')

import server  # noqa: E402


def test_backup_writes_readable_archive():
    name = server._do_backup_project("p1")
    path = server._project_backup_dir("p1") / name
    assert path.exists(), "Archiv nicht geschrieben"
    assert not list(server._project_backup_dir("p1").glob("*.tmp")), "tmp-Datei nicht aufgeräumt"
    with tarfile.open(path) as t:
        # Archiv-Wurzel = project_id, damit die Datei allein wiederherstellbar ist
        assert "p1/kv_store.json" in t.getnames(), "Projektdatei fehlt im Archiv"

    cfg = server._load_backup_cfg()
    # Signatur muss zum gesicherten Stand passen -> Scheduler skippt danach
    assert cfg["projects"]["p1"]["signature"] == server._project_signature("p1")


def test_signature_is_per_project():
    before = server._project_signature("p1")
    (_tmp / "projects" / "p1" / "neu.json").write_text('{"b": 2}')
    assert server._project_signature("p1") != before, "neue Datei ändert die Signatur nicht"
    # zweites Projekt ohne Dateien hat eine andere Signatur als p1
    (_tmp / "projects" / "p2").mkdir(exist_ok=True)
    assert server._project_signature("p2") != server._project_signature("p1")


def test_rotation_keeps_max_backups():
    bdir = server._project_backup_dir("p1")
    bdir.mkdir(parents=True, exist_ok=True)
    for old in ("backup_2020-01-01_00-00-00.tar.gz", "backup_2020-01-02_00-00-00.tar.gz",
                "backup_2020-01-03_00-00-00.tar.gz", "backup_2020-01-04_00-00-00.tar.gz"):
        (bdir / old).write_bytes(b"alt")

    server._do_backup_project("p1")

    files = server._list_project_backups("p1")
    assert len(files) == 3, f"MAX_BACKUPS=3 nicht eingehalten: {[f.name for f in files]}"
    assert not (bdir / "backup_2020-01-01_00-00-00.tar.gz").exists()  # ältestes fliegt


def test_restore_roundtrip_is_project_scoped():
    marker = _tmp / "projects" / "p1" / "kv_store.json"
    original = marker.read_text()
    name = server._do_backup_project("p1")  # nur p1 sichern

    marker.write_text('{"zerstoert": true}')         # Bestand verändern
    (_tmp / "projects" / "other").mkdir(exist_ok=True)  # fremdes Projekt

    server._do_restore_project("p1", name)

    assert marker.read_text() == original, "alter p1-Inhalt nicht wiederhergestellt"
    assert (_tmp / "projects" / "other").exists(), "fremdes Projekt fälschlich entfernt"
    assert not (_tmp / ".restore_tmp").exists(), "temp nicht aufgeräumt"
    assert not (_tmp / ".p1_old").exists(), "alt-Verzeichnis nicht aufgeräumt"


def test_restore_creates_missing_project():
    """Restore muss auch gehen, wenn das Projekt noch gar nicht existiert."""
    import shutil
    (_tmp / "projects" / "fresh").mkdir()
    (_tmp / "projects" / "fresh" / "data.json").write_text('{"x": 42}')
    name = server._do_backup_project("fresh")
    path = server._project_backup_dir("fresh") / name

    shutil.rmtree(_tmp / "projects" / "fresh")        # Projekt komplett entfernen
    assert not (_tmp / "projects" / "fresh").exists()

    restored = server._restore_from_archive(path)      # aus dem Archiv wiederanlegen
    assert restored == "fresh"
    assert (_tmp / "projects" / "fresh" / "data.json").read_text() == '{"x": 42}'


if __name__ == "__main__":
    test_backup_writes_readable_archive()
    test_signature_is_per_project()
    test_rotation_keeps_max_backups()
    test_restore_roundtrip_is_project_scoped()
    test_restore_creates_missing_project()
    print("ok — Backup je Projekt: Archiv lesbar, Signatur je Projekt, Rotation, "
          "projektbezogener Restore, Restore legt fehlendes Projekt neu an")
