# Dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN mkdir -p /data

ENV DATABASE_URL=sqlite:////data/demo.db \
    TB_ADDRESS=tb:3000 \
    MOCK_WEBHOOK_URL=http://app:8000/payments/webhook

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD curl -fsS http://127.0.0.1:8000/ || exit 1

CMD ["uvicorn","tigerfans.server:app","--host","0.0.0.0","--port","8000","--workers","1"]
