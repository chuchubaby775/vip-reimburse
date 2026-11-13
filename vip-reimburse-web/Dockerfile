FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 vip

COPY requirements.txt requirements-ocr.txt ./
RUN python -m pip install --no-cache-dir -r requirements-ocr.txt
COPY server.py ocr.py ./
COPY public/ ./public/
COPY scripts/prepare_models.py ./scripts/prepare_models.py

# Fetch the same OCR models used by the app at build time. A failed model
# download fails the build instead of silently disabling image recognition.
RUN python scripts/prepare_models.py \
    && mkdir -p /data \
    && chown -R vip:vip /data /app/.runtime

USER vip
ENV HOST=0.0.0.0 PORT=8765 DATA_DIR=/data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).read()"
CMD ["python", "server.py"]
