#!/usr/bin/env bash
# Gegenstück zu swap-to-qwen.sh: qwen raus, mistral zurück in den Normalbetrieb.
# Nach Abschluss des doc-graph-Ingests ausführen.
set -euo pipefail

echo "1/2  qwen (llm-qwen) stoppen..."
docker stop llm-qwen 2>/dev/null || echo "     (llm-qwen lief nicht — ok)"

echo "2/2  mistral (llm-mistral) wieder starten..."
docker start llm-mistral

echo "     zurück im Normalbetrieb (mistral; lädt im Hintergrund, /health zeigt Bereitschaft)."
