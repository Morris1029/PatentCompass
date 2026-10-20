FROM node:24-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5 AS renderer
WORKDIR /renderer
COPY api/chart-renderer/package.json api/chart-renderer/package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/workspace/api/.venv/bin:$PATH" \
    UV_LINK_MODE=copy
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends fonts-noto-cjk fontconfig ca-certificates libreoffice-writer \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.7.12
COPY --from=renderer /usr/local/bin/node /usr/local/bin/node
WORKDIR /workspace/api
COPY api/pyproject.toml api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --from=renderer /renderer/node_modules ./chart-renderer/node_modules
COPY api/chart-renderer/render.mjs ./chart-renderer/render.mjs
COPY api/app ./app
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home app \
    && mkdir -p /workspace/storage \
    && chown app:app /workspace/storage
USER app
CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=INFO", "--concurrency=2", "--prefetch-multiplier=1"]
