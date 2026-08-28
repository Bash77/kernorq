# Kernorq — Cloud Run production image
# Python 3.12 slim; no dev tooling; no secrets baked in.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (layer caching). uv is fast and reads pyproject/uv.lock.
# Dev group IS installed: run_test_suite executes pytest inside the container,
# so pytest must exist at runtime. httpx (dev) is required by API tests.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Application source + test suite + demo workload — never .env, never credentials.
# tests/ is a product requirement: objectives like "Run my test suite" must
# discover and execute the real suite inside this image. A build guard test
# (tests/test_deployment_build.py) enforces COPY lines.
# demo/ contains demo/workloads/golden_demo.csv — must be present in the
# deployed image so /workloads/golden/run works both locally and in Cloud Run
# regardless of working directory (resolved via project-relative Path(__file__)).
COPY app ./app
COPY tests ./tests
COPY demo ./demo
COPY README.md ./README.md

# Cloud Run injects $PORT
ENV PORT=8080
EXPOSE 8080

# Non-root user
RUN useradd -m -u 10001 kernorq && chown -R kernorq /app
USER kernorq

# Production ASGI startup — venv-managed uvicorn, honors Cloud Run $PORT
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT}"]
