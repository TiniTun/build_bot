# Production image for the build-bot server.
#
# Two stages share one Python base so the virtualenv built in `builder` is
# binary-compatible with `runtime`. Dependencies resolve from uv.lock only, so a
# rebuild of the same commit produces the same dependency set.

FROM python:3.11-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: cached until pyproject.toml or uv.lock changes.
# No BuildKit cache mounts, so this builds identically under the classic builder.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-cache

# Project layer.
COPY . /app
RUN uv sync --frozen --no-cache


FROM python:3.11-slim-bookworm AS runtime

# Unprivileged runtime user. The workspace is mounted at /workspace and must be
# readable/writable by this uid on the host.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

# `--workspace` is a global Typer option and must precede the subcommand.
CMD ["build-bot", "--workspace", "/workspace", "server"]
