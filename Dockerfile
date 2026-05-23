FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENSTD_HOST=0.0.0.0 \
    OPENSTD_PORT=8000 \
    OPENSTD_DOWNLOAD_DIR=/app/downloads

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gosu \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY openstd_spider ./openstd_spider
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install .

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/downloads \
    && chown -R appuser:appuser /app \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["openstd_spider_web"]
