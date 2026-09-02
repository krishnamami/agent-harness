# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 - builder. Resolves and installs dependencies into a virtualenv.
# Nothing from this stage reaches the final image except /app/.venv, so the
# compilers, caches and uv binary never ship.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

# uv is pinned. "latest" in a build stage means the image you ship on Friday
# was not built by the same toolchain as the one you tested on Monday.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are their own layer, installed before the source is copied.
# Source changes many times a day; dependencies change a few times a month.
# In this order an ordinary code edit reuses the whole dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the project itself.
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---------------------------------------------------------------------------
# Stage 2 - runtime.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

# A fixed uid/gid, not a name. Kubernetes runAsUser takes a number, and
# matching it to the image's user is what keeps a read-only mount working.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --no-create-home --home-dir /app app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/ ./src/

USER app

EXPOSE 8000

# Liveness, not readiness -- see ADR-0004. Docker restarts an unhealthy
# container, and a container that restarts because a downstream is slow turns a
# degraded dependency into an outage.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# Exec form, so uvicorn is PID 1 and receives SIGTERM directly. Wrapping this
# in a shell gives you a shell as PID 1 that swallows the signal, and every
# deploy then waits out the 10-second kill timeout.
#
# The port is fixed rather than read from APP_PORT: exec form does not expand
# variables, and remapping belongs to the host (-p 9000:8000) anyway.
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
