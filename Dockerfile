FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PORT=8080

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY cpa_warden.py ./
COPY docker/ ./docker/

RUN chmod +x ./docker/entrypoint.sh && \
    mkdir -p /data && chown 1000:1000 /data

RUN useradd --uid 1000 --no-create-home appuser
USER appuser

EXPOSE 8080

ENTRYPOINT ["./docker/entrypoint.sh"]
