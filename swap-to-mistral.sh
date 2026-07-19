#!/usr/bin/env bash
# Gegenstück zu swap-to-qwen.sh: qwen raus, mistral zurück in den Normalbetrieb.
# Nach Abschluss des doc-graph-Ingests ausführen (auch vom Startup-Reconcile).
set -euo pipefail

# Wartet, bis der qwen-VRAM wirklich frei ist, BEVOR mistral startet — sonst
# startet mistral (~12 GiB) in noch belegtes VRAM und crasht in einen OOM-Loop
# (erlebt 2026-07-17). docker stop ist zwar synchron, aber der CUDA-Teardown gibt
# das VRAM erst beim Prozess-Reap frei. nvidia-smi steht im doc-graph-Container
# (python-slim, kein nvidia-runtime) meist NICHT zur Verfügung -> Fallback auf
# einen festen Puffer. Beim manuellen Aufruf auf dem Host wird real gepollt.
wait_vram_free() {  # $1 = benötigte freie MiB (Default 12000 ~ mistral -ngl 27)
  local need="${1:-12000}"
  if command -v nvidia-smi >/dev/null 2>&1; then
    for _ in $(seq 1 60); do   # bis 60 s
      local free
      free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
      if [ -n "$free" ] && [ "$free" -ge "$need" ]; then
        echo "     VRAM frei: ${free} MiB (>= ${need}) — mistral kann starten."
        return 0
      fi
      sleep 1
    done
    echo "     WARNUNG: VRAM nach 60 s nicht ausreichend frei — starte mistral trotzdem (restart-policy heilt einen evtl. OOM)."
  else
    # Kein nvidia-smi: qwen-Prozess ist nach `docker stop` tot, VRAM-Freigabe
    # folgt binnen ~1-2 s. Puffer großzügig.
    sleep 5
  fi
}

echo "1/3  Embedder zurück auf CPU: llm-embed-gpu raus, llm-embed rein..."
docker stop llm-embed-gpu 2>/dev/null || echo "     (llm-embed-gpu lief nicht — ok)"
docker start llm-embed 2>/dev/null || echo "     (llm-embed schon aktiv — ok)"

echo "2/3  qwen (llm-qwen) stoppen + auf VRAM-Freigabe warten..."
docker stop llm-qwen 2>/dev/null || echo "     (llm-qwen lief nicht — ok)"
wait_vram_free 12000

echo "3/3  mistral (llm-mistral) wieder starten..."
docker start llm-mistral

echo "     zurück im Normalbetrieb (mistral + CPU-Embedder; /health zeigt Bereitschaft)."
