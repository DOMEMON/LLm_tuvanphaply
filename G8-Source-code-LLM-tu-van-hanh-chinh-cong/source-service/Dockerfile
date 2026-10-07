FROM python:3.12.11-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.10 \
    && uv sync --frozen --no-dev --no-install-project

FROM python:3.12.11-slim-bookworm
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY app ./app
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/lib/g5-source \
    && chown -R appuser:appuser /var/lib/g5-source
USER appuser
EXPOSE 8091
CMD ["python", "-m", "app.server"]
