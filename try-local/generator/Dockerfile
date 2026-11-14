# try-local only (#2903): the generator container's service
# (`da-tools generator-service`, ADR-036 §3) for the demo stack. tenant-api
# asks it before every write that changes conf.d and refuses to start without
# it. The published da-tools image cannot stand in yet: tools/v2.9.0 has no
# generator-service or git.
#
# The service's code is NOT copied in — compose bind-mounts the checkout's
# scripts/tools at /opt/da-tools, so the demo judges with what the checkout
# holds. This image is only the runtime the da-tools image gives it:
# Python + PyYAML (same pins as components/da-tools/app/Dockerfile), git,
# the reader binaries a judgement runs (da-guard, da-crdecode, da-read), and
# the nonroot user at uid 10001 — which tenant-api runs as too
# (docker-compose.yaml `user:`): the service answers only a peer of its own
# uid.
#
# Build context = repo ROOT (#3530): da-guard and da-crdecode are built from
# components/threshold-exporter/app, as the recipe-preview image builds
# da-guard, and da-read from components/da-read.
#   docker build -f try-local/generator/Dockerfile .

# ── da-guard, da-crdecode ────────────────────────────────────────────────
# Without da-guard the service answers every write 503 tool_fault (#3862)
# and GET /v1/status then reports `ready: false` (#3530). da-crdecode reads the
# ConfigMap the generator assembles, as kubectl does. Same stage as
# components/recipe-preview/Dockerfile: pinned to the native BUILDPLATFORM and
# cross-compiled to TARGETARCH (CGO_ENABLED=0). The builder's Go version MUST
# equal the module's go.mod `go` directive (tests/ops/test_go_toolchain_parity.py).
# This stage is discarded.
FROM --platform=$BUILDPLATFORM golang:1.26.10-alpine3.23 AS gobuild

WORKDIR /src
COPY components/threshold-exporter/app/go.mod components/threshold-exporter/app/go.sum ./
# go.mod replaces gopkg.in/yaml.v3 with this directory (#2681); `go mod
# download` reads the replacement's go.mod.
COPY components/threshold-exporter/app/third_party ./third_party
RUN go mod download
COPY components/threshold-exporter/app/ ./
# The build flags live in ONE place, scripts/ops/go_build.sh (#3425). The root
# .dockerignore re-includes scripts/ops/ for this COPY.
COPY scripts/ops/go_build.sh /opt/go_build.sh
ARG TARGETOS TARGETARCH
RUN sh /opt/go_build.sh --goos "${TARGETOS}" --goarch "${TARGETARCH}" . ./cmd/da-guard /da-guard && \
    sh /opt/go_build.sh --goos "${TARGETOS}" --goarch "${TARGETARCH}" . ./cmd/da-crdecode /da-crdecode

# ── da-read ──────────────────────────────────────────────────────────────
# Alertmanager's own config loader: the generator asks it whether
# Alertmanager accepts the config it rendered. Without it the service answers
# every write 503 tool_fault and GET /v1/status then reports `ready: false`.
# Its own Go module (components/da-read), same builder and flags as above.
# This stage is discarded.
FROM --platform=$BUILDPLATFORM golang:1.26.10-alpine3.23 AS readbuild

WORKDIR /src
COPY components/da-read/go.mod components/da-read/go.sum ./
RUN go mod download
COPY components/da-read/ ./
COPY scripts/ops/go_build.sh /opt/go_build.sh
ARG TARGETOS TARGETARCH
RUN sh /opt/go_build.sh --goos "${TARGETOS}" --goarch "${TARGETARCH}" . . /da-read

# ── runtime ──────────────────────────────────────────────────────────────
FROM python:3.14.10-alpine3.23

# pip is removed once PyYAML is in, for the reason the da-tools image gives
# (#1278: every pip release vendors a setuptools / msgpack with open CVEs).
# Unpinned apk on purpose, like every other `apk add` in this repo
# (docs/internal/iac-lint-baseline.md, DL3018 rows).
RUN apk --no-cache upgrade && \
    apk add --no-cache git && \
    pip install --no-cache-dir PyYAML==6.0.3 && \
    python -m pip uninstall -y pip && \
    rm -rf /usr/local/lib/python3*/ensurepip/_bundled && \
    addgroup -g 10001 -S nonroot && \
    adduser -S -D -H -u 10001 -h /nonexistent -s /sbin/nologin -G nonroot nonroot && \
    mkdir -p /run/da-generator && chown nonroot:nonroot /run/da-generator

# On PATH, where the service resolves each when its variable
# ($DA_GUARD_BINARY, $DA_CRDECODE_BINARY, $DA_READ_BINARY) is not set.
COPY --from=gobuild /da-guard /da-crdecode /usr/local/bin/
COPY --from=readbuild /da-read /usr/local/bin/da-read

USER nonroot:nonroot

# GET /v1/status over the socket (the chart's probe does the same): 200 = up
# and answering, not necessarily `ready`.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(4); s.connect('/run/da-generator/generator.sock'); s.sendall(b'GET /v1/status HTTP/1.1\\r\\nHost: h\\r\\n\\r\\n'); sys.exit(0 if s.recv(64).startswith(b'HTTP/1.1 200 ') else 1)"]

ENTRYPOINT ["python3", "/opt/da-tools/ops/generator_service.py"]
