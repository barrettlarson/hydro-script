FROM node:22-alpine AS client-build

WORKDIR /client

COPY client/package.json client/package-lock.json ./
RUN npm ci

COPY client/ ./
RUN npm run build

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server/ ./server/
COPY --from=client-build /client/dist ./client/dist

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/server

RUN useradd --create-home appuser \
    && mkdir -p /app/.data \
    && chown appuser /app/.data
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]