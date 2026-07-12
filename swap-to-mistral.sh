#!/usr/bin/env bash
# Gegenstück zu swap-to-qwen.sh: qwen raus, mistral + paperless-ai zurück in den
# Normalbetrieb. Nach Abschluss des doc-graph-Ingests ausführen.
set -euo pipefail

echo "1/3  qwen-Container stoppen/entfernen..."
docker rm -f paperless-llama-qwen 2>/dev/null || echo "     (qwen nicht aktiv — ok)"

echo "2/3  mistral (paperless-llama) wieder starten..."
docker start paperless-llama

echo "3/3  paperless-ai fortsetzen..."
docker unpause paperless-ai 2>/dev/null || echo "     (paperless-ai war nicht pausiert — ok)"

echo "     zurück im Normalbetrieb (mistral)."
