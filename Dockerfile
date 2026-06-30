FROM python:3.12-slim

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# ── Dependency layer (cached unless pyproject.toml / uv.lock change) ──
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-editable

# ── Source layer ──────────────────────────────────────────────────────
COPY src/ src/
RUN uv sync --frozen --no-editable

# ── Entrypoint ─────────────────────────────────────────────────────────
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Hugging Face cache location (mounted as a volume for persistence)
ENV HF_HOME=/hf_cache
# Default DB path (volume-mounted directory)
ENV OFF_UPC_DDB_PATH=/data/food.parquet

EXPOSE 5000

ENTRYPOINT ["./docker-entrypoint.sh"]
