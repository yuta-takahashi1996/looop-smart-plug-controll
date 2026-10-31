# --- Tailwind CSS のビルド ---
FROM debian:bookworm-slim AS css

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY scripts/tailwind.sh scripts/tailwind.sh
COPY app/static app/static
COPY app/templates app/templates
RUN scripts/tailwind.sh

# --- アプリケーション ---
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations migrations
COPY app app
COPY --from=css /build/app/static/css/app.css app/static/css/app.css

EXPOSE 8080

# マイグレーション適用後に起動する。exec により SIGTERM を uvicorn へ直接渡し、正常終了処理を走らせる
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host \"${WEB_HOST:-0.0.0.0}\" --port \"${WEB_PORT:-8080}\""]
