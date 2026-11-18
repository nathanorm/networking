# Build context: repo root (needs shared/ and uv.lock). See docs/SPEC.md §1.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY shared shared
COPY apps/backend-collector apps/backend-collector
# Installs only this app + the shared libs it declares, non-editable, into /app/.venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package backend-collector --no-editable

FROM python:3.13-slim
RUN useradd --system --uid 10001 app && mkdir -p /var/lib/edgelab && chown app /var/lib/edgelab
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER app
CMD ["python", "-m", "backend_collector"]
