#!/usr/bin/env bash
# Ingest-Vorlauf: myai (traegt llm-qwen + llm-embed, Repo llm-stack) per
# Wake-on-LAN wecken und warten, bis beide Services antworten. Ersetzt den
# frueheren GPU-Swap auf myubuntu (mistral raus / qwen rein) — seit dem Split
# laufen qwen und der Embedder dauerhaft auf myai, mistral bleibt unangetastet.
# Idempotent: ist myai wach, kostet das nur die zwei Health-Checks.
set -euo pipefail

MAC="94:de:80:25:c1:8a"          # enp3s0 auf myai (siehe llm-stack/wake-myai.sh)
BROADCAST="192.168.2.255"        # WoL braucht Broadcast — DNS hilft hier nicht,
                                 # der Host ist ja aus.
QWEN_URL="http://myai:11436/health"
EMBED_URL="http://myai:11435/health"

up() { python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=3)" "$1" >/dev/null 2>&1; }

if ! up "$QWEN_URL"; then
  echo "myai wecken (Magic Packet an $MAC)..."
  python3 - "$MAC" "$BROADCAST" <<'EOF'
import socket, sys
mac = bytes.fromhex(sys.argv[1].replace(":", ""))
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.sendto(b"\xff" * 6 + mac * 16, (sys.argv[2], 9))
EOF
fi

for svc in "qwen $QWEN_URL" "embed $EMBED_URL"; do
  set -- $svc
  echo "warte auf $1 ($2)..."
  ok=0
  for _ in $(seq 1 60); do   # bis 5 min (Boot + Modell-Load aus hf-cache)
    up "$2" && { echo "  $1 bereit."; ok=1; break; }
    sleep 5
  done
  [ "$ok" = 1 ] || { echo "FEHLER: $1 nicht bereit ($2)" >&2; exit 1; }
done
