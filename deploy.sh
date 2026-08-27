#!/usr/bin/env bash
# Deploy: Repo -> Deploy-Verzeichnis (Build-Kontext) -> Rebuild + Verifikation.
# Warum: Der Container baut aus $DST, nicht aus dem Repo. Vergessener Sync =
# alter Code läuft weiter (2026-07-13: Ingest lief ohne qwen-Swap, weil die
# server.py im Deploy-Verzeichnis zwei Commits alt war).
#
# Läuft von überall: doc-graph deployt auf mystorage, entwickelt wird meist auf
# myubuntu. Sind wir nicht auf dem Zielhost, gehen Sync und Rebuild per ssh/scp
# — sonst bleibt alles lokal. DEPLOY_HOST="" erzwingt den lokalen Modus.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
# mystorage-Konvention: Code + Compose unter compose-files/<stack>, persistente
# Daten daneben unter /var/local/mydocker/<stack> (in der Compose absolut gemountet).
DST=/var/local/mydocker/compose-files/doc-graph
TARGET=${DEPLOY_TARGET:-mystorage}   # Zielhost laut Projektkonvention
DEPLOY_HOST=${DEPLOY_HOST-$( [ "$(hostname)" = "$TARGET" ] && echo "" || echo "$TARGET" )}

# Nur Code/Build-Dateien. docker-compose.yml und .env bleiben Deploy-eigen
# (lokale Mounts/Secrets) und werden bewusst NICHT überschrieben.
FILES=(server.py config.py backup.py graphview.py clauses.py backfill_fundstellen.py ingest-begin.sh ingest-end.sh Dockerfile requirements.txt test_backup.py test_ingest_extras.py test_embed_cap.py test_fundstellen.py test_ignore_tags.py)

# Kommando auf dem Zielhost ausführen — lokal direkt, sonst über ssh.
run() {
  if [ -z "$DEPLOY_HOST" ]; then bash -c "$1"; else ssh "$DEPLOY_HOST" "$1"; fi
}

if [ -n "$DEPLOY_HOST" ]; then
  echo "Deploy auf $DEPLOY_HOST (lokal: $(hostname))"
  # BatchMode: lieber sofort scheitern als auf eine Passphrase warten, die im
  # Skript niemand eintippt.
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$DEPLOY_HOST" true \
    || { echo "FEHLER: ssh $DEPLOY_HOST nicht erreichbar (Key/Agent prüfen)."; exit 1; }
  run "test -d '$DST'" || { echo "FEHLER: $DST fehlt auf $DEPLOY_HOST."; exit 1; }
  for f in "${FILES[@]}"; do
    scp -q "$SRC/$f" "$DEPLOY_HOST:$DST/$f"
  done
else
  echo "Deploy lokal auf $(hostname)"
  test -d "$DST" || { echo "FEHLER: $DST fehlt."; exit 1; }
  for f in "${FILES[@]}"; do
    cp "$SRC/$f" "$DST/$f"
  done
fi

# Drift-Hinweis statt Überschreiben: compose-Abweichung ist meist der lokale
# inputs-Mount — nur melden, Abgleich bleibt manuell.
run "cat '$DST/docker-compose.yml'" | diff -q "$SRC/docker-compose.yml" - >/dev/null \
  || echo "HINWEIS: docker-compose.yml weicht vom Repo ab (lokaler inputs-Mount ist normal)."

run "docker compose --project-directory '$DST' up -d --build"

# Verifikation: läuft der Container wirklich mit dem deployten Code?
sleep 2
want="$(md5sum "$SRC/server.py" | cut -d' ' -f1)"
have="$(run "docker exec doc-graph md5sum /app/server.py" | cut -d' ' -f1)"
if [ "$want" = "$have" ]; then
  echo "OK: doc-graph läuft mit aktuellem server.py ($want)"
else
  echo "FEHLER: Container-Code weicht ab (Repo $want, Container $have)"; exit 1
fi
