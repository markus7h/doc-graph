"""Backup und Restore der Projekt-Stores (tar.gz je Projekt) samt Scheduler.

Lag bis 2026-08-23 mitten in server.py. Hier braucht es weder LightRAG noch MCP:
das Modul kennt nur Pfade aus config und zwei Rueckfragen an den Server, die
dieser beim Start einhaengt (siehe unten). Dadurch laeuft test_backup.py ohne den
kompletten Serverstack.
"""

import json
import os
import shutil
import tarfile
import time
from datetime import datetime
from pathlib import Path

from config import BACKUP_DIR, MAX_BACKUPS, PROJECTS_DIR, log, now, validate_project

# --- Rueckfragen an den Server -----------------------------------------------
# Statt server.py zu importieren (Zyklus) haengt server.py hier beim Start die
# echten Implementierungen ein. Die Defaults machen das Modul allein lauffaehig
# und sind genau das, was ohne laufenden Ingest bzw. ohne Instanz-Cache gilt.

def ingest_laeuft() -> bool:
    """Laeuft gerade ein Ingest? Dann schreibt LightRAG in die Stores und ein
    tar gaebe einen halben Stand."""
    return False


def instanz_verwerfen(project: str) -> None:
    """Nach einem Restore zeigt eine gecachte LightRAG-Instanz auf den alten
    Stand und muss verworfen werden."""


# ----------------------------------------------------------------------------
# Backup: tar.gz von PROJECTS_DIR nach BACKUP_DIR, Rotation + Scheduler.
# Nach Vorbild ai-rem (gleiche Dateinamen-Konvention, .config.json als Status).
# ponytail: unverschlüsselt — die Quelldokumente liegen im selben OneDrive
# ebenfalls im Klartext, ein Key schützte hier nichts.
# ----------------------------------------------------------------------------
BACKUP_INTERVALS = {"hourly": 3600, "daily": 86400, "weekly": 604800}
_CONFIG_PATH = BACKUP_DIR / ".config.json"
# Obergrenze für hochgeladene Restore-Archive (Projektdaten inkl. Embeddings).
MAX_RESTORE_UPLOAD = int(os.environ.get("MAX_RESTORE_UPLOAD", str(2 * 1024**3)))


def load_cfg() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"enabled": True, "interval": "daily", "last_backup": None}


def save_cfg(cfg: dict) -> None:
    # ponytail: kein flock wie in ai-rem — hier schreiben nur Scheduler-Thread
    # und Viewer-Thread desselben Prozesses, atomares replace reicht.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(_CONFIG_PATH)


def project_signature(project: str) -> dict:
    """Fingerabdruck EINES Projekts — ändert er sich nicht, ist ein neues Backup
    dieses Projekts sinnlos."""
    files = [p for p in (PROJECTS_DIR / project).rglob("*") if p.is_file()]
    return {
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "max_mtime": max((p.stat().st_mtime for p in files), default=0),
    }


def existing_projects() -> list[str]:
    """Alle Projekt-Verzeichnisse (Storage-Keys) unter PROJECTS_DIR."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir())


def project_backup_dir(project: str) -> Path:
    return BACKUP_DIR / project


def list_project_backups(project: str) -> list[Path]:
    """Archive EINES Projekts, neueste zuerst."""
    return sorted(project_backup_dir(project).glob("backup_*.tar.gz"), reverse=True)


def backup_project(project: str) -> str:
    """Sichert EIN Projekt als tar.gz (Archiv-Wurzel = project_id, damit die Datei
    für sich allein wiederherstellbar ist), rotiert alte Stände weg."""
    project = validate_project(project)
    bdir = project_backup_dir(project)
    bdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"backup_{ts}.tar.gz"
    path = bdir / name

    tmp = path.with_suffix(".tar.gz.tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(PROJECTS_DIR / project, arcname=project)
    tmp.replace(path)

    cfg = load_cfg()
    cfg.setdefault("projects", {})[project] = {
        "last_backup": now(), "signature": project_signature(project),
    }
    save_cfg(cfg)

    for old in list_project_backups(project)[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)

    log.info("Backup Projekt '%s': %s (%.1f MB)", project, name, path.stat().st_size / 1024 / 1024)
    return name


def restore_project(project: str, name: str) -> None:
    """Restore eines gelisteten Projekt-Archivs (Name aus dem Projekt-Ordner)."""
    project = validate_project(project)
    restore_from_archive(project_backup_dir(project) / name)


def restore_from_archive(path: Path) -> str:
    """Spielt ein Projekt-Archiv zurück und legt das Projekt bei Bedarf NEU an.
    Archiv-Wurzel = project_id (Legacy: 'projects/' mit allen Projekten darin).
    Datenverlust-sicher — erst temp-extrahieren, dann der alte Stand je Projekt
    weggemovt (nicht gelöscht), bis der neue drin ist. Gibt die Projekt-IDs zurück."""
    tmp = PROJECTS_DIR.parent / ".restore_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(tmp, filter="data")  # Python 3.12 -> traversal-sicher
        tops = [p for p in tmp.iterdir() if p.is_dir()]
        # Legacy-Gesamtarchiv: Wurzel 'projects/' -> die enthaltenen Projekte.
        if len(tops) == 1 and tops[0].name == "projects":
            tops = [p for p in tops[0].iterdir() if p.is_dir()]
        if not tops:
            raise ValueError("Archiv enthält kein Projekt-Verzeichnis")
        restored = []
        for src in tops:
            project = validate_project(src.name)  # Path-Traversal-Schutz
            dst = PROJECTS_DIR / project
            old = PROJECTS_DIR.parent / f".{project}_old"
            PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(old, ignore_errors=True)
            if dst.exists():
                dst.rename(old)  # alten Stand erst wegmoven, nicht löschen
            src.rename(dst)
            shutil.rmtree(old, ignore_errors=True)
            instanz_verwerfen(project)  # gecachte Instanz zeigt auf alten Stand
            restored.append(project)
        log.info("Restore aus %s: %s", path.name, ", ".join(restored))
        return ", ".join(restored)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scheduler() -> None:
    """Prüft minütlich, ob je Projekt ein geplantes Backup fällig ist."""
    while True:
        time.sleep(60)
        try:
            cfg = load_cfg()
            if not cfg.get("enabled"):
                continue
            # Läuft ein Ingest, schreibt LightRAG gerade in die Stores — dann
            # gäbe das tar einen halben Stand. Nächster Tick versucht es erneut.
            if ingest_laeuft():
                continue
            interval = BACKUP_INTERVALS.get(cfg.get("interval", "daily"), 86400)
            projs = cfg.get("projects", {})
            for project in existing_projects():
                pm = projs.get(project, {})
                last = pm.get("last_backup")
                if last:
                    delta = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
                    if delta < interval:
                        continue
                if pm.get("signature") == project_signature(project):
                    continue  # nichts geändert seit dem letzten Backup
                backup_project(project)
        except Exception:  # noqa: BLE001 — Scheduler darf nie sterben
            log.exception("Geplantes Backup fehlgeschlagen")
