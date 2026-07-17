#!/usr/bin/env bash
# Gegenstück zu swap-to-qwen.sh: qwen raus, mistral zurück in den Normalbetrieb.
# Nach Abschluss des doc-graph-Ingests ausführen.
set -euo pipefail

echo "1/3  Embedder zurück auf CPU: llm-embed-gpu raus, llm-embed rein..."
docker stop llm-embed-gpu 2>/dev/null || echo "     (llm-embed-gpu lief nicht — ok)"
docker start llm-embed 2>/dev/null || echo "     (llm-embed schon aktiv — ok)"

echo "2/3  qwen (llm-qwen) stoppen..."
docker stop llm-qwen 2>/dev/null || echo "     (llm-qwen lief nicht — ok)"

echo "3/3  mistral (llm-mistral) wieder starten..."
docker start llm-mistral

echo "     zurück im Normalbetrieb (mistral + CPU-Embedder; /health zeigt Bereitschaft)."
