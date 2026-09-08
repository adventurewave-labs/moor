# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY moor/ ./moor/
COPY tests/ ./tests/

EXPOSE 8080

# The control plane needs Docker socket access, so it runs as root by
# design (same trust boundary as every `docker compose` deployment).
ENTRYPOINT ["python", "-m", "moor"]
CMD ["serve"]
