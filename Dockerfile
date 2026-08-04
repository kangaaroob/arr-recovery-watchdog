# syntax=docker/dockerfile:1

FROM python:3.12.11-slim-bookworm AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

COPY --from=builder /install /usr/local

# Non-root user; /data is the only persistent write location (mount a volume).
# With read_only: true, mount a tmpfs at /tmp.
RUN groupadd --system --gid 65532 watchdog \
    && useradd --system --uid 65532 --gid watchdog \
        --home-dir /nonexistent --shell /usr/sbin/nologin watchdog \
    && mkdir /data \
    && chown watchdog:watchdog /data

USER watchdog:watchdog
WORKDIR /

ENTRYPOINT ["python", "-m", "arr_recovery_watchdog"]
