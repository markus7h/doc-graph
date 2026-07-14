"""Selbsttest für die Backup-Logik in server.py.
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
    name = server._do_backup()
    path = server.BACKUP_DIR / name
    assert path.exists(), "Archiv nicht geschrieben"
    assert not list(server.BACKUP_DIR.glob("*.tmp")), "tmp-Datei nicht aufgeräumt"
    with tarfile.open(path) as t:
        assert "projects/p1/kv_store.json" in t.getnames(), "Projektdatei fehlt im Archiv"

    cfg = server._load_backup_cfg()
    assert cfg["last_backup_file"] == name
    # Signatur muss zum gesicherten Stand passen -> Scheduler skippt danach
    assert cfg["last_backup_signature"] == server._projects_signature()


def test_signature_tracks_changes():
    before = server._projects_signature()
    (_tmp / "projects" / "p1" / "neu.json").write_text('{"b": 2}')
    assert server._projects_signature() != before, "neue Datei ändert die Signatur nicht"


def test_rotation_keeps_max_backups():
    for old in ("backup_2020-01-01_00-00-00.tar.gz", "backup_2020-01-02_00-00-00.tar.gz",
                "backup_2020-01-03_00-00-00.tar.gz", "backup_2020-01-04_00-00-00.tar.gz"):
        (server.BACKUP_DIR / old).write_bytes(b"alt")

    server._do_backup()

    files = server._list_backup_files()
    assert len(files) == 3, f"MAX_BACKUPS=3 nicht eingehalten: {[f.name for f in files]}"
    # neuestes bleibt, ältestes fliegt
    assert not (server.BACKUP_DIR / "backup_2020-01-01_00-00-00.tar.gz").exists()


if __name__ == "__main__":
    test_backup_writes_readable_archive()
    test_signature_tracks_changes()
    test_rotation_keeps_max_backups()
    print("ok — Backup: Archiv lesbar, Signatur reagiert, Rotation greift")
