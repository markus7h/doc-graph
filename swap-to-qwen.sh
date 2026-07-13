#!/usr/bin/env bash
# doc-graph Re-Ingest vorbereiten: mistral temporär raus, qwen3-14b voll auf die
# GPU. Der qwen-Container übernimmt den Netz-Alias 'paperless-llama:11434' —
# doc-graph merkt vom Modellwechsel nichts (LLM_BASE_URL bleibt).
#
# Ablauf gesamt:
#   1. ./swap-to-qwen.sh          <- dieses Skript (paperless-ai pause, qwen rein)
#   2. ingest_paperless(...) via MCP triggern + ingest_status pollen bis "done"
#   3. ./swap-to-mistral.sh       <- qwen raus, mistral + paperless-ai zurück
#
# Warum: 16 GB VRAM fassen mistral-24b nur mit CPU-Offload (13/40 Layer im RAM ->
# langsam, Extraktions-Timeouts). qwen3-14b passt VOLL auf die GPU. Da paperless-ai
# selten läuft, teilen wir die GPU zeitlich statt im Speicher.
set -euo pipefail

QWEN_MODEL="bartowski/Qwen_Qwen3-14B-GGUF:Q5_K_M"   # ~10.5 GB, passt voll auf GPU
IMAGE="ghcr.io/ggml-org/llama.cpp:server-cuda"
CACHE_VOL="paperless-ai_llama_cache"                # geteilt mit paperless-llama
NET="paperless-ai_default"

echo "1/3  paperless-ai pausieren (keine qwen-Antworten in paperless-ai)..."
docker pause paperless-ai 2>/dev/null || echo "     (paperless-ai nicht aktiv — ok)"

echo "2/3  mistral (paperless-llama) stoppen -> 16 GB VRAM frei..."
docker stop paperless-llama

echo "3/3  qwen3-14b voll auf GPU starten (Alias paperless-llama, ngl 99)..."
docker rm -f paperless-llama-qwen 2>/dev/null || true
docker run -d --name paperless-llama-qwen \
  --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=all \
  --network "$NET" --network-alias paperless-llama --network-alias llama \
  -v "$CACHE_VOL":/root/.cache/huggingface \
  "$IMAGE" \
  -hf "$QWEN_MODEL" -c 16384 -fa on -ngl 99 -t 6 -np 1 \
  --host 0.0.0.0 --port 11434 >/dev/null

echo "     warte auf qwen (erster Lauf lädt ~10.5 GB GGUF, kann dauern)..."
for _ in $(seq 1 240); do   # bis 20 min
  if docker logs paperless-llama-qwen 2>&1 | grep -q "listening on http"; then
    echo "     qwen bereit. Jetzt ingest_paperless(...) triggern, dann ./swap-to-mistral.sh"
    exit 0
  fi
  if ! docker ps -q -f name=paperless-llama-qwen | grep -q .; then
    echo "FEHLER: qwen-Container beendet. Logs:"; docker logs --tail 30 paperless-llama-qwen; exit 1
  fi
  sleep 5
done
echo "WARN: qwen nach 20 min nicht bereit — docker logs paperless-llama-qwen"
exit 1
