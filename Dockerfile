FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

LABEL org.opencontainers.image.title="Gree Local Cloud" \
    org.opencontainers.image.description="Local WB04BRT13 cloud replacement with Home Assistant MQTT Discovery" \
    org.opencontainers.image.version="0.2.0"

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY certs/ ./certs/

RUN uv sync --frozen --no-dev \
    && mkdir -p /data \
    && chown 1000:1000 /data

USER 1000:1000

EXPOSE 1053/udp 1813/tcp 1982/tcp

HEALTHCHECK --interval=20s --timeout=5s --start-period=90s --retries=3 \
    CMD ["/app/.venv/bin/python", "-m", "gree_local.healthcheck"]

ENTRYPOINT ["/app/.venv/bin/gree-local"]
