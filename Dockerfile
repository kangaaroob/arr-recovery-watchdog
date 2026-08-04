# syntax=docker/dockerfile:1

# Base image pinned by digest for reproducible builds; Dependabot keeps the
# tag and digest current. The digest is the multi-arch index, so adding
# linux/arm64 later needs no Dockerfile change.
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

# Static fallback so even locally built images link back to the repository;
# the release workflow adds the full OCI label set (revision, version, ...).
LABEL org.opencontainers.image.source="https://github.com/kangaaroob/arr-recovery-watchdog" \
      org.opencontainers.image.licenses="MIT"

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
