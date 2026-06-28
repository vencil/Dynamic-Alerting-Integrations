# recipe-preview service — would-fire preview HTTP facade (#657 PR 2).
#
# Build context = repo ROOT (the eval core lives under scripts/tools/). The
# image bundles a SHA-pinned promtool because the firing/inactive verdict is
# version-bound (§6 #5). Runs as non-root, serves POST /preview on :8082.
#   try-local:  docker compose up recipe-preview
FROM python:3.13.13-alpine3.22 AS builder

# Alpine 3.22 CVE fixes (mirrors components/da-tools).
RUN apk --no-cache upgrade

# PyYAML — the eval core's only dependency beyond the stdlib.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir PyYAML==6.0.3

# promtool, SHA-256 pinned PER ARCH (§6 #5 + the #849/#851 supply-chain policy:
# verify every downloaded release binary before use). PROM_VERSION + the amd64
# digest must equal ci.yml's promtool gate — the firing/inactive verdict format
# is version-bound, so a skew would silently misclassify; the equality is
# CI-enforced (tests/preview/test_promtool_pin_parity.py). Multi-arch: buildx
# injects TARGETARCH (amd64 / arm64) per platform; pick the matching tarball +
# its OWN pinned digest. busybox wget/sha256sum/tar (no apk add).
ARG TARGETARCH
ARG PROM_VERSION=3.12.0
ARG PROM_SHA256_amd64=20da47f8e5303f74aecb78edd7f7e39041dac08ac4939dba75efd7a900ae8867
ARG PROM_SHA256_arm64=281492bf04ed171cb09d24377e9777f56e55ccb6445ef197b66bd1693bd9b7f1
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) PROM_SHA256="${PROM_SHA256_amd64}" ;; \
      arm64) PROM_SHA256="${PROM_SHA256_arm64}" ;; \
      *) echo "ERROR: unsupported/empty TARGETARCH='${TARGETARCH}' (need amd64|arm64)" >&2; exit 1 ;; \
    esac; \
    wget -qO /tmp/p.tgz "https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-${TARGETARCH}.tar.gz"; \
    printf '%s  /tmp/p.tgz\n' "${PROM_SHA256}" > /tmp/p.sha256; \
    sha256sum -c /tmp/p.sha256; \
    tar xzf /tmp/p.tgz -C /tmp; \
    cp "/tmp/prometheus-${PROM_VERSION}.linux-${TARGETARCH}/promtool" /usr/local/bin/promtool; \
    /usr/local/bin/promtool --version

# ── runtime ──────────────────────────────────────────────────────────────
FROM python:3.13.13-alpine3.22

LABEL org.opencontainers.image.title="recipe-preview" \
      org.opencontainers.image.description="Would-fire preview service (#657) — POST /preview" \
      org.opencontainers.image.source="https://github.com/vencil/Dynamic-Alerting-Integrations" \
      org.opencontainers.image.vendor="Dynamic Alerting Platform"

# Build provenance — the release/CI build stamps the git SHA so /healthz reports
# exactly which source built this image. recipe-preview bundles a byte-identical
# snapshot of the platform compiler; GIT_SHA lets an operator confirm WHICH
# commit's compiler is running (drift observability, design §6 #5). Local /
# un-stamped builds report "unknown".
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

RUN apk --no-cache upgrade

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/bin/promtool /usr/local/bin/promtool
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /opt/recipe-preview

# Eval core — PRESERVE the repo's dx/ + lint/ sibling layout, because
# compile_custom_alerts resolves its lint helpers via <dir>/../lint.
COPY scripts/tools/dx/_recipe_preview.py        ./core/dx/_recipe_preview.py
COPY scripts/tools/dx/compile_custom_alerts.py  ./core/dx/compile_custom_alerts.py
COPY scripts/tools/dx/custom_alerts             ./core/dx/custom_alerts
COPY scripts/tools/lint/check_rulepack_sync.py  ./core/lint/check_rulepack_sync.py
# _lib_exitcodes + _lib_compat live at scripts/tools/ (the compiler's OTHER
# search path, <dir>/.. = ./core), NOT under lint/ — place them at ./core so
# the `from _lib_exitcodes import ...` / `from _lib_compat import ...` resolve
# exactly as in the repo.
COPY scripts/tools/_lib_exitcodes.py            ./core/_lib_exitcodes.py
COPY scripts/tools/_lib_compat.py               ./core/_lib_compat.py
ENV PREVIEW_CORE_DIR=/opt/recipe-preview/core/dx

COPY components/recipe-preview/app.py           ./app.py

# Non-root.
RUN addgroup -S preview && adduser -S -G preview preview
USER preview

EXPOSE 8082
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -qO- http://127.0.0.1:8082/healthz >/dev/null 2>&1 || exit 1
CMD ["python", "app.py"]
