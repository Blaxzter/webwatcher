FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first so the browser layer stays cached across code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[web]' \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Chromium needs a writable home for its profile/cache.
RUN useradd --create-home --uid 10001 watcher \
    && mkdir -p /app/data \
    && chown -R watcher:watcher /app /ms-playwright
USER watcher

VOLUME ["/app/data"]

# Nur das Web-UI, und auch das nur wenn es eingeschaltet ist. Bewusst kein
# `ports:` in der docker-compose.yml: der Reverse Proxy erreicht den Container
# über das Docker-Netz, der Host-Port bleibt zu.
EXPOSE 8080

ENTRYPOINT ["webwatcher", "--config", "/app/config.yaml"]
CMD ["run"]
