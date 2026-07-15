#!/usr/bin/env bash
# doc-graph-Ingest vorbereiten: mistral raus, qwen3-14b voll auf die GPU.
# Beide sind reguläre Services im llm-stack-Compose-Projekt (Repo llm-stack) und
# teilen den Netz-Alias 'llm' — Docker-DNS zeigt nach dem Wechsel automatisch
# auf qwen, doc-graphs LLM_BASE_URL (http://llm:11434/v1) bleibt unverändert.
# paperless-ai ist auf llm-mistral gepinnt und bekommt nie qwen-Antworten —
# das frühere pause/unpause von paperless-ai ist damit obsolet.
#
# Warum: 16 GB VRAM fassen mistral-24b nur mit CPU-Offload (langsam, Timeouts).
# qwen3-14b passt VOLL auf die GPU. Zurück mit ./swap-to-mistral.sh.
set -euo pipefail

echo "1/2  mistral (llm-mistral) stoppen -> VRAM frei..."
docker stop llm-mistral 2>/dev/null || echo "     (llm-mistral lief nicht — ok)"

echo "2/2  qwen (llm-qwen) starten..."
docker start llm-qwen

echo "     warte auf qwen (erster Lauf lädt ~10.5 GB GGUF, kann dauern)..."
for _ in $(seq 1 240); do   # bis 20 min
  # Läuft im doc-graph-Container: der ist selbst im llm-net -> direkter Health-Check.
  # python3 statt curl (python:3.12-slim hat kein curl/wget).
  if python3 -c "import urllib.request; urllib.request.urlopen('http://llm:11434/health', timeout=3)" >/dev/null 2>&1; then
    echo "     qwen bereit."
    exit 0
  fi
  if ! docker ps -q -f 'name=^llm-qwen$' | grep -q .; then
    echo "FEHLER: llm-qwen beendet. Logs:"; docker logs --tail 30 llm-qwen; exit 1
  fi
  sleep 5
done
echo "FEHLER: qwen nach 20 min nicht bereit — docker logs llm-qwen"
exit 1
