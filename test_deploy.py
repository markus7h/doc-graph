"""Selbsttest für die Host-Erkennung in deploy.sh. Lauf: python test_deploy.py

Nur die Auswahl lokal-vs-remote wird geprüft (der Teil mit den Fallstricken:
Kommandosubstitution unter `set -e`, leeres DEPLOY_HOST als gültiger Wert).
Sync und Rebuild bleiben ungetestet — die brauchen einen echten Zielhost.
"""
import re
import subprocess
from pathlib import Path

DEPLOY = Path(__file__).parent / "deploy.sh"

# Die Zeilen bis zur FILES-Definition enthalten die gesamte Host-Auswahl.
HEAD = DEPLOY.read_text().split("# Nur Code/Build-Dateien")[0]


def _host(env):
    """DEPLOY_HOST, wie der Script-Kopf ihn unter den gegebenen Env-Vars setzt."""
    r = subprocess.run(
        ["bash", "-c", HEAD + '\necho "[$DEPLOY_HOST]"'],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env},
    )
    assert r.returncode == 0, r.stderr  # set -e darf hier nicht zuschlagen
    return r.stdout.strip()[1:-1]


def test_syntax():
    assert subprocess.run(["bash", "-n", str(DEPLOY)]).returncode == 0


def test_remote_when_not_on_target():
    # Fremder Host -> Zielhost wird angesteuert
    assert _host({"DEPLOY_TARGET": "zielhost"}) == "zielhost"


def test_local_when_on_target():
    # Auf dem Zielhost selbst -> leer = lokaler Modus.
    # hostname wird über einen Stub im PATH vorgetäuscht.
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        stub = Path(td) / "hostname"
        stub.write_text("#!/bin/sh\necho zielhost\n")
        stub.chmod(0o755)
        r = subprocess.run(
            ["bash", "-c", HEAD + '\necho "[$DEPLOY_HOST]"'],
            capture_output=True, text=True,
            env={"PATH": f"{td}:/usr/bin:/bin", "DEPLOY_TARGET": "zielhost"},
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "[]", r.stdout


def test_explicit_empty_forces_local():
    # DEPLOY_HOST="" ist ein gültiger Wert und muss den Default schlagen
    # (${VAR-...} statt ${VAR:-...}).
    assert _host({"DEPLOY_TARGET": "zielhost", "DEPLOY_HOST": ""}) == ""


def test_explicit_host_wins():
    assert _host({"DEPLOY_TARGET": "zielhost", "DEPLOY_HOST": "anderer"}) == "anderer"


def test_dockerfile_copies_are_deployed():
    """Jede im Dockerfile kopierte Projektdatei muss auch in FILES stehen.

    Sonst laeuft der Build auf dem Zielhost in ein "file not found": das
    Dockerfile erwartet eine Datei, die deploy.sh nie hinsynct (2026-08-20 mit
    test_embed_cap.py genau so passiert).
    """
    root = Path(__file__).resolve().parent
    deploy = (root / "deploy.sh").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    m = re.search(r"^FILES=\((.*?)\)", deploy, re.S | re.M)
    assert m, "FILES-Definition in deploy.sh nicht gefunden"
    files = set(m.group(1).split())

    kopiert = set()
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        # letztes Token ist das Ziel, davor die Quellen
        teile = line.split()[1:-1]
        kopiert.update(t for t in teile if not t.startswith("--"))

    # nur Dateien, die es im Repo wirklich gibt (Wildcards/Verzeichnisse raus)
    fehlend = {f for f in kopiert - files if (root / f).is_file()}
    assert not fehlend, f"im Dockerfile kopiert, aber nicht in deploy.sh FILES: {sorted(fehlend)}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("test_deploy: alle OK")
