FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LIGHT_AUDIT_DB=/data/light_audit.db

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Inside the container the app binds all interfaces (required for Docker's
# port forwarding). Localhost-only access is enforced by docker-compose.yml
# publishing "127.0.0.1:8000:8000". Run the container directly without that
# restriction only if you understand the exposure.
ENV HOST=0.0.0.0
CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port 8000"]
