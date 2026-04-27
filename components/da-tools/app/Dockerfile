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

# ── Bundle da-guard binary (v2.8.0 C-11) ──────────────────────────
# `da-tools guard <subcommand>` shells out to da-guard (see
# scripts/tools/ops/guard_dispatch.py). Bundling the binary at
# /usr/local/bin/da-guard lets the image work in air-gapped
# environments (no $PATH lookup outside the container, no extra
# install step). The binary is built linux/amd64 by build.sh from
# components/threshold-exporter/app/cmd/da-guard before docker
# build runs.
COPY da-guard /usr/local/bin/da-guard
RUN chmod +x /usr/local/bin/da-guard

# ── Entrypoint ────────────────────────────────────────────────────
# Create a symlink so 'da-tools' works as a command
RUN ln -s /opt/da-tools/entrypoint.py /usr/local/bin/da-tools && \
    chmod +x /opt/da-tools/entrypoint.py

ENV PROMETHEUS_URL=""

RUN addgroup -g 10001 -S nonroot && \
    adduser -S -D -H -u 10001 -h /nonexistent -s /sbin/nologin -G nonroot nonroot
USER nonroot:nonroot

ENTRYPOINT ["python3", "/opt/da-tools/entrypoint.py"]
CMD ["--help"]
