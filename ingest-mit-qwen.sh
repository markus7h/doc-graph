#!/usr/bin/env bash
# Ein-Kommando-Ingest mit GPU-Swap. Ersetzt die manuelle 3-Schritt-Abfolge
# (swap-to-qwen -> MCP-Ingest -> swap-to-mistral): lädt qwen3-14b voll auf die
# GPU, pausiert paperless-ai, triggert ingest_paperless über MCP, pollt bis
# fertig und swappt danach GARANTIERT zurück auf mistral (auch bei Fehler/Ctrl-C).
#
# Läuft auf dem Docker-Host (braucht docker + Netzzugang zum doc-graph-MCP).
#
#   ./ingest-mit-qwen.sh <project_id> <filter=wert> [weitere filter …]
# Filter (mind. einer): tag= | document_type= | correspondent= | query_text=
# Beispiel:
#   ./ingest-mit-qwen.sh dx-erbe "tag=dx: Erbe Papa"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_URL="${MCP_URL:-http://localhost:5775/mcp}"
POLL_SECS="${POLL_SECS:-15}"

[ $# -ge 2 ] || { echo "Usage: $0 <project_id> tag=… | document_type=… | correspondent=… | query_text=…"; exit 2; }
PROJECT="$1"; shift

# key=value-Filter -> JSON-Argumente für ingest_paperless
args_json=$(PROJECT="$PROJECT" python3 -c '
import json, os, sys
d = {"project_id": os.environ["PROJECT"]}
for a in sys.argv[1:]:
    k, _, v = a.partition("=")
    d[k] = v
print(json.dumps(d, ensure_ascii=False))
' "$@")
status_args=$(PROJECT="$PROJECT" python3 -c 'import json,os;print(json.dumps({"project_id":os.environ["PROJECT"]}))')

# --- MCP-Helfer (streamable-http, eine Session wiederverwenden) --------------
HDR=$(mktemp); SID=""
mcp_init() {
  local init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"ingest-mit-qwen","version":"1"}}}'
  curl -sS -D "$HDR" -o /dev/null -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -X POST "$MCP_URL" -d "$init"
  SID=$(grep -i '^mcp-session-id:' "$HDR" | tr -d '\r' | awk '{print $2}')
  [ -n "$SID" ] || { echo "FEHLER: keine MCP-Session — läuft doc-graph auf $MCP_URL?"; exit 1; }
  curl -sS -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H "mcp-session-id: $SID" -X POST "$MCP_URL" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
}
# mcp_call <tool> <args-json> -> result.content[0].text auf stdout
mcp_call() {
  local body="{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"
  curl -sS -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H "mcp-session-id: $SID" -X POST "$MCP_URL" -d "$body" \
    | sed -n 's/^data: //p' | python3 -c 'import sys,json;print(json.loads(sys.stdin.read())["result"]["content"][0]["text"])'
}

# swap-back GARANTIEREN — trap läuft bei jedem Exit (Erfolg, Fehler, Ctrl-C)
cleanup() { echo; echo ">>> zurück auf mistral + paperless-ai …"; "$SCRIPT_DIR/swap-to-mistral.sh" || true; rm -f "$HDR"; }
trap cleanup EXIT

# 1) qwen voll auf die GPU, paperless-ai pausieren
"$SCRIPT_DIR/swap-to-qwen.sh"

# 2) Ingest triggern
mcp_init
echo ">>> ingest_paperless $args_json"
resp=$(mcp_call ingest_paperless "$args_json")
echo "$resp"

# Kein Hintergrund-Lauf gestartet (nichts zu tun / Filter fehlt / läuft schon)?
# -> nicht pollen, trap swappt gleich zurück.
if ! printf '%s' "$resp" | grep -q "Hintergrund gestartet"; then
  echo ">>> kein laufender Ingest — fertig."
  exit 0
fi

# 3) pollen bis done/error
echo ">>> pollen (alle ${POLL_SECS}s) …"
while true; do
  sleep "$POLL_SECS"
  st=$(mcp_call ingest_status "$status_args" || true)
  echo "  $st"
  state=$(printf '%s' "$st" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("state",""))
except Exception: print("")' || true)
  case "$state" in
    done)  echo ">>> Ingest fertig."; break;;
    error) echo ">>> Ingest FEHLER (Status oben)."; break;;
  esac
done
