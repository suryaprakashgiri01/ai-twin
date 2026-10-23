# The public deployment: published knowledge only, no admin routes (DEPLOYMENT_PROFILE=public).
# Google Cloud Run (asia-south1) runs it: Cloud Run sets PORT (8080). Hugging Face Spaces cannot host it:
# Spaces block outbound ports other than 80/443/8080, and MongoDB Atlas (27017) and Redis (6379) need others.
# Nothing from vault/ or .env is ever copied in (see .dockerignore).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

RUN useradd --create-home --uid 1000 app
WORKDIR /app

# Dependencies first, so code changes reuse this layer.
# The README files are package metadata (pyproject `readme`): the build needs them.
COPY pyproject.toml uv.lock README.md ./
COPY packages/llm-orchestrator/pyproject.toml packages/llm-orchestrator/README.md packages/llm-orchestrator/
COPY packages/llm-orchestrator/src packages/llm-orchestrator/src
RUN uv sync --frozen --no-dev --group public --no-install-project

COPY app app
COPY config config
RUN uv sync --frozen --no-dev --group public

# The embedding, sparse and reranking models, fetched at build time so start-up is offline.
ENV MODELS_DIR=/app/models
RUN python - <<'PY'
import asyncio
from pathlib import Path
from app.kb.embedding import FastEmbedder
from app.kb.retrieval.rerank import CrossEncoderReranker
base = Path("/app/models/fastembed")
asyncio.run(FastEmbedder(base).embed(["warm up"]))
CrossEncoderReranker(base)._score_sync("warm up", ["warm up"])
PY
RUN chown -R app:app /app/models

USER app
ENV DEPLOYMENT_PROFILE=public \
    HOST=0.0.0.0 \
    APP_ENV=prod \
    LOG_FORMAT=json \
    PORT=8080
EXPOSE 8080
CMD ["python", "-m", "app.main"]
