# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY injector.py ./

# Needs Docker socket access to perform real mutations.
ENTRYPOINT ["python", "injector.py"]
CMD ["--help"]
