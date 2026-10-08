# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# uv is the fastest way to build; the binary is self-contained
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
# Install runtime deps only (no dev group) into a dedicated venv
RUN uv sync --frozen --no-dev --no-editable

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN addgroup --system tack && adduser --system --ingroup tack tack

WORKDIR /app

# Copy the venv and source tree from the builder
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY config/ ./config/
COPY policies/ ./policies/
COPY templates/ ./templates/
COPY migrations/ ./migrations/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LOG_JSON=true \
    LOG_LEVEL=INFO

USER tack

EXPOSE 8000

# Health check hits the login page (always 200 even unauthenticated)
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/login')"

CMD ["python", "-c", "from tack_ai.web import main; main()"]
