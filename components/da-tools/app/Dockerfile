FROM python:3.13.2-alpine3.21 AS base

# ── Metadata ────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="da-tools" \
      org.opencontainers.image.description="Dynamic Alerting CLI Toolkit — portable verification and migration tools" \
      org.opencontainers.image.source="https://github.com/vencil/Dynamic-Alerting-Integrations" \
      org.opencontainers.image.vendor="Dynamic Alerting Platform"

# ── Security: upgrade all packages for CVE fixes ───────────────────
# CVE-2025-15467 (openssl 3.x RCE), CVE-2025-48174 (libavif overflow)
RUN apk --no-cache upgrade

# ── Dependencies ──────────────────────────────────────────────────────
RUN pip install --no-cache-dir PyYAML==6.0.3 promql-parser==0.7.0 croniter==6.0.0

# ── Copy tools ──────────────────────────────────────────────────────
WORKDIR /opt/da-tools

# Entrypoint dispatcher
COPY entrypoint.py .
COPY VERSION .

# Tool scripts + metric-dictionary.yaml (assembled by build.sh from scripts/tools/)
COPY tools/ ./

# ── Entrypoint ──────────────────────────────────────────────────────
# Create a symlink so 'da-tools' works as a command
RUN ln -s /opt/da-tools/entrypoint.py /usr/local/bin/da-tools && \
    chmod +x /opt/da-tools/entrypoint.py

ENV PROMETHEUS_URL=""

RUN addgroup -g 10001 -S nonroot && \
    adduser -S -D -H -u 10001 -h /nonexistent -s /sbin/nologin -G nonroot nonroot
USER nonroot:nonroot

ENTRYPOINT ["python3", "/opt/da-tools/entrypoint.py"]
CMD ["--help"]
