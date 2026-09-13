# syntax=docker/dockerfile:1

FROM node:20-slim AS frontend-build

WORKDIR /build/frontend

# Install from the lockfile before copying source so dependency downloads stay
# cached when only application code changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/postcss.config.js frontend/tailwind.config.js ./
COPY frontend/src ./src
RUN npm run build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    FEVER_DB_PATH=/var/lib/pronoia/fever.db \
    FEVER_DATA_DIR=/var/lib/pronoia/data \
    PRONOIA_RUNTIME_DIR=/tmp/pronoia \
    XDG_CACHE_HOME=/tmp/pronoia/cache

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install --no-cache-dir -r backend/requirements.txt

# Keep the runtime image limited to executable source and the compiled UI.
# Secrets, databases, datasets and logs are supplied only at runtime.
COPY backend/app ./backend/app
COPY --from=frontend-build /build/frontend/dist ./frontend/dist

RUN groupadd --system --gid 10001 pronoia \
    && useradd --system --uid 10001 --gid pronoia \
        --home-dir /home/pronoia --create-home --shell /usr/sbin/nologin pronoia \
    && mkdir -p /var/lib/pronoia/data /tmp/pronoia/cache \
    && chown -R pronoia:pronoia /var/lib/pronoia /tmp/pronoia /app

USER 10001:10001
WORKDIR /app/backend

EXPOSE 8000
STOPSIGNAL SIGTERM

# A single worker is deliberate: the current application uses SQLite and
# in-process run state. PORT is a trusted deployment setting, not user input.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]
