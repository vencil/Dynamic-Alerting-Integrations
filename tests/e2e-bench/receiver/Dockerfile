# E2E-bench webhook receiver — captures Alertmanager webhooks for the
# B-1 Phase 2 5-anchor measurement (T4).
#
# Same distroless pattern as components/threshold-exporter/app/Dockerfile
# for consistency: nonroot UID 65532, no shell, static binary.

# Builder Go version == go.mod `go` directive (tests/ops/test_go_toolchain_parity.py).
# Build context: the REPO ROOT (#3425), so the build runs
# scripts/ops/go_build.sh — the one place the repo's `go build` flags are
# written. Dockerfile.dockerignore next to this file narrows the context back
# to this module plus that script (BuildKit).
FROM golang:1.26.8-alpine3.23 AS builder
WORKDIR /src
COPY tests/e2e-bench/receiver/go.mod ./
COPY tests/e2e-bench/receiver/*.go ./
COPY scripts/ops/go_build.sh /opt/go_build.sh
RUN sh /opt/go_build.sh --goos linux . . /receiver

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /receiver /usr/local/bin/receiver
EXPOSE 5001
ENTRYPOINT ["/usr/local/bin/receiver"]
