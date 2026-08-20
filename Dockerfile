FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# poppler-utils: pdftotext für ingest_directory (PDF -> Text, kein OCR)
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py graphview.py clauses.py ingest-begin.sh ingest-end.sh test_backup.py test_ingest_extras.py test_embed_cap.py ./

# Tiktoken-Cache vorab laden, damit der Container offline lauffähig ist
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" || true

EXPOSE 5775 5776
CMD ["python", "server.py"]
