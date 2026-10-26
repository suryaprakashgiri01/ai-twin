# Warm-up script run at Docker build time so start-up is offline.
# Equivalent of the `RUN python - <<'PY'` heredoc that classic Cloud Build docker
# parsers cannot parse; kept separate so the Dockerfile stays heredoc-free.
import asyncio
from pathlib import Path

from app.kb.embedding import FastEmbedder
from app.kb.retrieval.rerank import CrossEncoderReranker

base = Path("/app/models/fastembed")
asyncio.run(FastEmbedder(base).embed(["warm up"]))
CrossEncoderReranker(base)._score_sync("warm up", ["warm up"])
