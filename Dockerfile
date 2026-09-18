# ---------- Stage 1: dependency builder (uv, cache-mounted) ----------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first: this layer stays cached unless pyproject/uv.lock change.
# Packaged test suites (~80 MB: numpy/scipy/certifi tests) are stripped —
# runtime code never imports them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev \
    && find /app/.venv -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +

# Application code (changes often; does not invalidate the dependency layer).
COPY app ./app

# ---------- Stage 2: minimal runtime ----------
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system gridwise && useradd --system --gid gridwise gridwise

WORKDIR /app

COPY --from=builder --chown=gridwise:gridwise /app/.venv /app/.venv
COPY --from=builder --chown=gridwise:gridwise /app/app /app/app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER gridwise

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
