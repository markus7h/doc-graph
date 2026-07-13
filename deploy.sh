#!/usr/bin/env bash
# Deploy: Repo -> Deploy-Verzeichnis (Build-Kontext) -> Rebuild + Verifikation.
# Warum: Der Container baut aus $DST, nicht aus dem Repo. Vergessener Sync =
# alter Code läuft weiter (2026-07-13: Ingest lief ohne qwen-Swap, weil die
# server.py im Deploy-Verzeichnis zwei Commits alt war).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/var/local/mydocker/doc-graph

# Nur Code/Build-Dateien. docker-compose.yml und .env bleiben Deploy-eigen
# (lokale Mounts/Secrets) und werden bewusst NICHT überschrieben.
FILES=(server.py graphview.py swap-to-qwen.sh swap-to-mistral.sh Dockerfile requirements.txt)

for f in "${FILES[@]}"; do
  cp "$SRC/$f" "$DST/$f"
done

# Drift-Hinweis statt Überschreiben: compose-Abweichung ist meist der lokale
# inputs-Mount — nur melden, Abgleich bleibt manuell.
diff -q "$SRC/docker-compose.yml" "$DST/docker-compose.yml" >/dev/null \
  || echo "HINWEIS: docker-compose.yml weicht vom Repo ab (lokaler inputs-Mount ist normal)."

docker compose --project-directory "$DST" up -d --build

# Verifikation: läuft der Container wirklich mit dem deployten Code?
sleep 2
want="$(md5sum "$SRC/server.py" | cut -d' ' -f1)"
have="$(docker exec doc-graph md5sum /app/server.py | cut -d' ' -f1)"
if [ "$want" = "$have" ]; then
  echo "OK: doc-graph läuft mit aktuellem server.py ($want)"
else
  echo "FEHLER: Container-Code weicht ab (Repo $want, Container $have)"; exit 1
fi
