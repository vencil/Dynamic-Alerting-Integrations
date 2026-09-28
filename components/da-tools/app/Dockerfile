FROM python:3.13.15-alpine3.23 AS builder

# ── Security: Alpine 3.23 (python:3.13.15-alpine3.23 ships libcrypto3 3.5.8-r0
# and libuuid 2.41.6-r1 — #1401 found 3.5.6 / 2.41-r9 on the alpine3.22 base).
# apk upgrade picks up any additional point-release patches.
RUN apk --no-cache upgrade

# ── Build-time dependencies ────────────────────────────────────────
# Install packages into a virtual environment to keep layers separate
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ⛔ pip is REMOVED from the venv once the deps are in (#1278). Every pip
# release through 26.2.1 vendors setuptools 70.3.0 (pkg_resources) and msgpack
# 1.1.2 under pip/_vendor/ — CVE-2025-47273 (fix 78.1.1) and GHSA-6v7p-g79w-8964
# (fix 1.2.1) — so no pip upgrade clears them; a CLI image that never installs
# anything at runtime does not need pip at all.
RUN pip install --no-cache-dir PyYAML==6.0.3 promql-parser==0.7.0 croniter==6.0.0 && \
    python -m pip uninstall -y pip

# ── Runtime Stage ──────────────────────────────────────────────────
FROM python:3.13.15-alpine3.23

LABEL org.opencontainers.image.title="da-tools" \
      org.opencontainers.image.description="Dynamic Alerting CLI Toolkit — portable verification and migration tools" \
      org.opencontainers.image.source="https://github.com/vencil/Dynamic-Alerting-Integrations" \
      org.opencontainers.image.vendor="Dynamic Alerting Platform"

# ── Security: upgrade all packages for CVE fixes ───────────────────
# ── git: the GitLab pipeline `da-tools init` generates runs its blast-radius
# job INSIDE this image and reads the merge request's base commit with git
# (issue 1444). Installing it at job time is not an option: the image runs as
# nonroot, so `apk add` there fails. Unpinned on purpose, like every other
# `apk add` in this repo (docs/internal/iac-lint-baseline.md, DL3018 rows).
# ── pip: the base image's system pip (and ensurepip's bundled pip wheel) carry
# the same vendored setuptools 70.3.0 / msgpack 1.1.2 as the venv's — removed
# for the same reason (#1278; see the builder stage).
RUN apk --no-cache upgrade && \
    apk add --no-cache git && \
    python -m pip uninstall -y pip && \
    rm -rf /usr/local/lib/python3*/ensurepip/_bundled

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

# ── Bundle amtool (issue 2294) ────────────────────────────────────
# `generate-routes` hands the config it writes / applies / --validates to
# `amtool check-config` when amtool is on PATH (#2219 / #2260). Every CI
# template `da-tools init --ci` generates runs inside THIS image, so without
# amtool here customers are not validated by Alertmanager's parser by default.
#
# ⛔ The ref below must stay byte-identical to the Alertmanager image in
# k8s/03-monitoring/deployment-alertmanager.yaml (the version SSOT): amtool
# must be the parser production runs. Enforced by
# tests/ops/test_da_tools_amtool_pin_parity.py; renovate.json bumps both.
# buildx resolves this ref per TARGETPLATFORM, which relies on the digest
# being the multi-arch index (the form Renovate pins). Upstream's Dockerfile
# puts amtool at /bin/amtool; it is a static Go binary, so musl is fine.
COPY --from=prom/alertmanager:v0.34.1@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a /bin/amtool /usr/local/bin/amtool
# Apache-2.0 §4: the upstream image carries no LICENSE / NOTICE, so they are
# vendored verbatim from the same Alertmanager release the COPY --from above pins.
COPY third_party/alertmanager/LICENSE third_party/alertmanager/NOTICE /usr/share/doc/amtool/

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
