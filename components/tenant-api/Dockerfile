# tenant-api Dockerfile
# Uses alpine (not distroless) because the binary shells out to `git`.
#
# Build from the REPO ROOT (not from components/tenant-api/) because go.mod
# uses a replace directive pointing to ../threshold-exporter/app:
#
#   docker build -t ghcr.io/vencil/tenant-api:v2.9.24 \
#     -f components/tenant-api/Dockerfile .
#
# Run:
#   docker run -p 8080:8080 -v $(pwd)/conf.d:/conf.d ghcr.io/vencil/tenant-api:v2.9.24

# ── Build stage ──────────────────────────────────────────────────────────────
# #463 multi-arch: builder pinned to the native BUILDPLATFORM, cross-compiles
# to TARGETARCH below (CGO_ENABLED=0 → no cross-toolchain, no QEMU for the Go
# compile). The final alpine stage's `apk add` still runs under QEMU for arm64.
# Trivy reads the toolchain version embedded by the compiler, so a stdlib CVE in
# the SHIPPED binary is fixed purely by which builder compiles it (#1058). The
# builder's Go version MUST equal this module's go.mod `go` directive — go.mod is
# the SSOT, and tests/ops/test_go_toolchain_parity.py fails on any skew (#1278:
# 1.26.8 clears the 1.26.5 stdlib CVE set). The builder's alpine suffix is INERT
# here: CGO_ENABLED=0 below, and this stage is discarded.
FROM --platform=$BUILDPLATFORM golang:1.26.8-alpine3.23 AS builder

WORKDIR /src

# Copy both modules required by the replace directive
COPY components/tenant-api/go.mod components/tenant-api/go.sum \
     components/tenant-api/
COPY components/threshold-exporter/app/go.mod components/threshold-exporter/app/go.sum \
     components/threshold-exporter/app/
# Both go.mod files replace gopkg.in/yaml.v3 with this directory (#2681), and
# `go mod download` reads the replacement's go.mod.
COPY components/threshold-exporter/app/third_party/ \
     components/threshold-exporter/app/third_party/

WORKDIR /src/components/tenant-api
RUN go mod download

# Copy full source for both modules
WORKDIR /src
COPY components/tenant-api/ components/tenant-api/
COPY components/threshold-exporter/app/ components/threshold-exporter/app/

# The build flags live in ONE place, scripts/ops/go_build.sh (#3425): the
# image, the tests and the release binaries are built the same way. The root
# .dockerignore re-includes scripts/ops/ for this COPY.
COPY scripts/ops/go_build.sh scripts/ops/go_build.sh
ARG TARGETOS TARGETARCH
# The version `tenant-api state show` / `config check` print as
# tenant_api_version (ADR-036 §12): release.yaml passes the tenant-api/v* tag
# (as `v<version>`, the image tag), the way da-tools' build.sh sets
# main.Version on da-guard. A local build keeps "(devel)".
ARG VERSION="(devel)"
RUN sh scripts/ops/go_build.sh -X "main.version=${VERSION}" \
    --goos "${TARGETOS}" --goarch "${TARGETARCH}" \
    components/tenant-api ./cmd/server /tenant-api

# ── Final stage ──────────────────────────────────────────────────────────────
# alpine + git: required because gitops/writer.go shells out to `git`.
# 3.23.6: ships libcrypto3/libssl3 3.5.8-r0 (#1278; CVE-2026-14456 is fixed in
# 3.5.8, and 3.23.5 ships 3.5.7).
FROM alpine:3.23.6

# Install git and ca-certs (needed for git over HTTPS)
# Note: alpine already has nobody(65534); create nonroot with system-assigned IDs
# /state: the default home of the dir state store (--state-store=dir, #2856).
# Created here, owned by nonroot, so an empty named volume mounted on it
# (try-local) inherits that ownership instead of root's.
RUN apk add --no-cache git ca-certificates tzdata \
    && addgroup -S nonroot \
    && adduser  -S -G nonroot nonroot \
    && mkdir -p /state && chown nonroot:nonroot /state

COPY --from=builder /tenant-api /usr/local/bin/tenant-api

USER nonroot

ENTRYPOINT ["/usr/local/bin/tenant-api"]
CMD ["--addr=:8080", "--config-dir=/conf.d"]

EXPOSE 8080

# rationale: K8s liveness/readiness probes own health checking for this
# service (see helm/tenant-api); an image-level HEALTHCHECK would duplicate
# them and is skipped under the orchestrator.

# Labels (OCI image spec)
LABEL org.opencontainers.image.title="tenant-api" \
      org.opencontainers.image.description="Tenant Management API — Dynamic Alerting Platform" \
      org.opencontainers.image.source="https://github.com/vencil/vibe-k8s-lab" \
      org.opencontainers.image.version="2.9.24"
