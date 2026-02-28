FROM python:3.12-alpine AS base

# ── Metadata ────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="da-tools" \
      org.opencontainers.image.description="Dynamic Alerting CLI Toolkit — portable verification and migration tools" \
      org.opencontainers.image.source="https://github.com/vencil/vibe-k8s-lab" \
      org.opencontainers.image.vendor="Dynamic Alerting Platform"

# ── Dependencies ──────────────────────────────────────────────────────
RUN pip install --no-cache-dir PyYAML==6.0.2 promql-parser==0.7.0

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

ENTRYPOINT ["python3", "/opt/da-tools/entrypoint.py"]
CMD ["--help"]
