FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# poppler-utils: pdftotext für ingest_directory (PDF -> Text, kein OCR)
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# docker-CLI (nur Client) für den GPU-Swap: server.py ruft swap-to-*.sh, die
# über den gemounteten Docker-Socket llm-mistral/llm-qwen (llm-stack) steuern.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

COPY server.py graphview.py clauses.py swap-to-qwen.sh swap-to-mistral.sh test_backup.py ./

# Tiktoken-Cache vorab laden, damit der Container offline lauffähig ist
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" || true

EXPOSE 5775 5776
CMD ["python", "server.py"]
