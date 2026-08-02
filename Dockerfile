FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first so the browser layer stays cached across code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Chromium needs a writable home for its profile/cache.
RUN useradd --create-home --uid 10001 watcher \
    && mkdir -p /app/data \
    && chown -R watcher:watcher /app /ms-playwright
USER watcher

VOLUME ["/app/data"]

ENTRYPOINT ["webwatcher", "--config", "/app/config.yaml"]
CMD ["run"]
