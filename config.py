"""Gemeinsame Basis: Pfade, Logger, Projektnamen-Validierung.

Bewusst klein gehalten. Hier steht nur, was mehr als ein Modul braucht — die
LLM-/Embedding-/Viewer-Konstanten bleiben in server.py, solange sie dort allein
verwendet werden. Ein Modul ohne schwere Importe (kein lightrag, kein mcp), damit
Module wie backup.py und ihre Tests ohne den ganzen Serverstack laufen.
"""

import logging
import os
import re
import time
from pathlib import Path

PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/data/projects"))
INPUTS_DIR = Path("/data/inputs")
# Backup-Ziel (gemountet, i.d.R. OneDrive/doc-graph). Rotation auf die letzten
# MAX_BACKUPS Archive; Intervall/An-Aus kommen aus .config.json (via Web-UI).
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "10"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("doc-graph")

PROJECT_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def validate_project(project: str) -> str:
    """Projektname als Pfadbestandteil absichern (Traversal, Sonderzeichen)."""
    if not PROJECT_RE.match(project):
        raise ValueError(
            f"Ungültiger Projektname '{project}' (erlaubt: a-z, 0-9, _, -)"
        )
    return project


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
