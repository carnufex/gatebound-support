# syntax=docker/dockerfile:1

FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.10 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

FROM python:3.11-slim AS runtime

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/data \
    PORT=8080 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown -R appuser:appuser /app /data

USER 1000

EXPOSE 8080

CMD ["gatebound-support", "serve"]
