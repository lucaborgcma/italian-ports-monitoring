FROM python:3.11-slim

# Chromium + chromedriver dallo stesso pacchetto apt → versioni sempre allineate
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "sync", "--timeout", "300", "--keep-alive", "5", "--max-requests", "50", "--max-requests-jitter", "5", "app:app"]
