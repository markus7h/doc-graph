#!/usr/bin/env bash
# Wöchentlicher Modell-Check für doc-graph: recherchiert, ob ein besseres
# lokales LLM für die KG-Extraktion existiert als das aktuell genutzte mistral.
# Der Claude-Agent recherchiert nur read-only (WebSearch/WebFetch); die lokale
# Modell-Liste holt dieses Script vorab, damit der Agent kein Bash braucht.
# Cron: siehe `crontab -l`.
set -euo pipefail
export HOME=/home/markus
export PATH="/home/markus/.local/bin:/usr/local/bin:/usr/bin:/bin"

DIR=/var/local/mydocker/doc-graph
REPORT="$DIR/model_check_report.md"
STAMP=$(date '+%Y-%m-%d %H:%M')

# Auf dem llama-server gehostetes Modell (llama.cpp = ein Modell pro Prozess).
# /v1/models am Host-gemappten Port; Fehler tolerieren, dann leer.
MODELS=$(curl -s --max-time 5 http://localhost:11434/v1/models 2>/dev/null \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(chr(10).join(m.get("id") or m.get("name","?") for m in (d.get("data") or d.get("models") or [])))' 2>/dev/null \
  || echo "(llama-server nicht erreichbar)")

PROMPT="WICHTIG: Dies ist eine autonome, nicht-interaktive Recherche-Aufgabe. Erstelle KEINEN Plan, gehe NICHT in den Plan-Modus, nutze KEINE Skills, rufe KEIN ai-rem/memory auf, frage NICHTS zurück. Recherchiere direkt per Websuche und gib am Ende ausschließlich den fertigen Bericht als Text aus.

Du bist ein wöchentlicher Modell-Check für das Projekt doc-graph. doc-graph baut mit LightRAG einen Knowledge Graph aus deutschen Dokumenten (Paperless-NGX) und nutzt dafür aktuell das lokale LLM mistral-small3.2:24b via llama-server (llama.cpp, GGUF) für die Entitäts- und Relationsextraktion.

Randbedingungen (hart):
- GPU: RTX 5080, 16 GB VRAM.
- Der llama-server (Container paperless-llama) wird mit paperless-ai geteilt und hält mistral-small3.2:24b dauerhaft geladen (Teil-GPU-Offload -ngl 27). Neben mistral (~15 GB) passt KEIN zweites großes Modell in 16 GB. Ein Kandidat muss mistral daher als GEMEINSAMES Modell ablösen (dann nutzen es doc-graph UND paperless-ai) oder klein genug sein, um daneben zu passen (praktisch unmöglich).
- Aufgabe des Modells: Entitäten + Beziehungen aus deutschen Behörden-/Rechts-/Geschäftsdokumenten extrahieren; hohe Instruktionstreue, sauberes strukturiertes Format.

Lokal bereits installierte Modelle:
${MODELS}

Aufgabe:
1. Recherchiere per Websuche aktuelle llama.cpp-taugliche LLMs (GGUF) bis ~24B für Entitäts-/Relationsextraktion auf Deutsch. Kriterien: deutsche Sprachqualität, Instruktionstreue/strukturierte Ausgabe, VRAM-Bedarf bei Q4, Erscheinungsdatum.
2. Vergleiche die 2-3 besten Kandidaten mit mistral-small3.2:24b.
3. Gib eine kurze klare Empfehlung: Bleibt mistral-small3.2:24b die beste Wahl, oder gibt es ein klar besseres Modell, das als gemeinsames Modell (mistral-Ersatz) in 16 GB taugt? Nenne das konkrete GGUF-Modell (HF-Repo bzw. llama.cpp-Tag) für einen Testlauf.

Halte den Bericht unter 400 Wörter. Beginne mit genau einer Zeile: 'EMPFEHLUNG: bleiben' ODER 'EMPFEHLUNG: wechseln zu <gguf-modell>'."

{
  echo "## Modell-Check $STAMP"
  echo
  claude -p "$PROMPT" --model sonnet --permission-mode default \
    --allowedTools WebSearch WebFetch 2>&1 || echo "(Check fehlgeschlagen — Exit $?)"
  echo
  echo "---"
} >> "$REPORT"
