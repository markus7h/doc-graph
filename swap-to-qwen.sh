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

# Wartet bis ein /health-Endpoint im llm-net antwortet (python3 statt curl —
# python:3.12-slim hat kein curl). Bricht ab, wenn der Container stirbt.
wait_health() {  # $1=url  $2=container  $3=label
  for _ in $(seq 1 240); do   # bis 20 min
    if python3 -c "import urllib.request; urllib.request.urlopen('$1', timeout=3)" >/dev/null 2>&1; then
      echo "     $3 bereit."; return 0
    fi
    if ! docker ps -q -f "name=^$2\$" | grep -q .; then
      echo "FEHLER: $2 beendet. Logs:"; docker logs --tail 30 "$2"; exit 1
    fi
    sleep 5
  done
  echo "FEHLER: $3 nach 20 min nicht bereit — docker logs $2"; exit 1
}

echo "1/3  mistral (llm-mistral) stoppen -> VRAM frei..."
docker stop llm-mistral 2>/dev/null || echo "     (llm-mistral lief nicht — ok)"

echo "2/3  qwen (llm-qwen) starten..."
docker start llm-qwen
echo "     warte auf qwen (erster Lauf lädt ~10.5 GB GGUF, kann dauern)..."
wait_health "http://llm:11434/health" "llm-qwen" "qwen"

echo "3/3  Embedder auf GPU: llm-embed (CPU) raus, llm-embed-gpu rein..."
docker stop llm-embed 2>/dev/null || echo "     (llm-embed lief nicht — ok)"
docker start llm-embed-gpu
# Alias 'embed' zeigt jetzt auf den GPU-Embedder; bge-m3 lädt schnell (~0.6 GB).
wait_health "http://embed:11435/health" "llm-embed-gpu" "GPU-Embedder"
exit 0
