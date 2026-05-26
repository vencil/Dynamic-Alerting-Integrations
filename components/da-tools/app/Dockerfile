FROM python:3.13.13-alpine3.22 AS builder

# ── Security: Alpine 3.22 includes fixes for: ─────────────────────
#   CVE-2025-48174 (libavif >= 1.3.0, integer overflow → buffer overflow)
#   CVE-2025-48175 (libavif >= 1.3.0, integer overflow in row bytes)
#   CVE-2025-15467 (openssl 3.x, CVSS 9.8 pre-auth RCE via CMS parsing)
# apk upgrade picks up any additional point-release patches.
RUN apk --no-cache upgrade

# ── Build-time dependencies ────────────────────────────────────────
# Install packages into a virtual environment to keep layers separate
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir PyYAML==6.0.3 promql-parser==0.7.0 croniter==6.0.0

# ── Runtime Stage ──────────────────────────────────────────────────
FROM python:3.13.13-alpine3.22

LABEL org.opencontainers.image.title="da-tools" \
      org.opencontainers.image.description="Dynamic Alerting CLI Toolkit — portable verification and migration tools" \
      org.opencontainers.image.source="https://github.com/vencil/Dynamic-Alerting-Integrations" \
      org.opencontainers.image.vendor="Dynamic Alerting Platform"

# ── Security: upgrade all packages for CVE fixes ───────────────────
RUN apk --no-cache upgrade

# ── Copy venv from builder ─────────────────────────────────────────
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ── Copy tools ─────────────────────────────────────────────────────
WORKDIR /opt/da-tools

# Entrypoint dispatcher
COPY entrypoint.py .
COPY VERSION .

# Tool scripts + metric-dictionary.yaml (assembled by build.sh from scripts/tools/)
COPY tools/ ./

# ── Bundle da-guard / da-batchpr / da-parser binaries ─────────────
# `da-tools guard|batch-pr|parser <subcommand>` shells out to these Go
# binaries (see the *_dispatch.py shims). Bundling them at
# /usr/local/bin lets the image work air-gapped (no $PATH lookup
# outside the container, no extra install step).
#
# #463 multi-arch: build.sh cross-compiles a per-arch suffix
# (da-guard.amd64 / da-guard.arm64); TARGETARCH (set by buildx per
# platform, or the host arch on a plain `docker build`) selects the
# matching binary so the image is native on amd64 AND arm64.
ARG TARGETARCH
COPY da-guard.${TARGETARCH} /usr/local/bin/da-guard
RUN chmod +x /usr/local/bin/da-guard

COPY da-batchpr.${TARGETARCH} /usr/local/bin/da-batchpr
RUN chmod +x /usr/local/bin/da-batchpr

COPY da-parser.${TARGETARCH} /usr/local/bin/da-parser
RUN chmod +x /usr/local/bin/da-parser

# ── Entrypoint ────────────────────────────────────────────────────
# Create a symlink so 'da-tools' works as a command
RUN ln -s /opt/da-tools/entrypoint.py /usr/local/bin/da-tools && \
    chmod +x /opt/da-tools/entrypoint.py

ENV PROMETHEUS_URL=""

# rationale: CLI toolkit (one-shot `da-tools <cmd>` invocations), not a
# long-running service — a container HEALTHCHECK is not meaningful.

RUN addgroup -g 10001 -S nonroot && \
    adduser -S -D -H -u 10001 -h /nonexistent -s /sbin/nologin -G nonroot nonroot
USER nonroot:nonroot

ENTRYPOINT ["python3", "/opt/da-tools/entrypoint.py"]
CMD ["--help"]
