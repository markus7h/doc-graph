"""Selbsttest für die Host-Erkennung in deploy.sh. Lauf: python test_deploy.py

Nur die Auswahl lokal-vs-remote wird geprüft (der Teil mit den Fallstricken:
Kommandosubstitution unter `set -e`, leeres DEPLOY_HOST als gültiger Wert).
Sync und Rebuild bleiben ungetestet — die brauchen einen echten Zielhost.
"""
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("test_deploy: alle OK")
