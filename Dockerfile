FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-ind poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN groupadd -g 10001 app && useradd -u 10001 -g app app && install -d -m 0700 -o app -g app /private-documents
COPY api.py common.py processing.py worker.py ./
USER app
