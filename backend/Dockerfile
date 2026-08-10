# syntax=docker/dockerfile:1
#
# Backend image for both the API and the worker (`app.workers.run_worker`) —
# they share the same dependency set, so one image with two entrypoints avoids
# building and shipping the image twice. Which process runs is decided by the
# CMD override in docker-compose.yml / the systemd unit, not by this file.

FROM python:3.12-slim AS builder

WORKDIR /build

# Compilers are needed to build a couple of C-extension wheels (e.g.
# cryptography, asyncpg) on platforms without a prebuilt wheel; they never
# reach the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

FROM python:3.12-slim AS runtime

RUN groupadd --system voicedesk && useradd --system --gid voicedesk --create-home voicedesk

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

USER voicedesk

EXPOSE 3010

# Curl isn't in the slim base; a stdlib request avoids adding a package for
# the sole purpose of the healthcheck. Only meaningful for the API process —
# compose overrides this to a no-op for the worker, which serves no HTTP port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:3010/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3010"]
