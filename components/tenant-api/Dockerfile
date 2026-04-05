# tenant-api Dockerfile
# Uses alpine (not distroless) because the binary shells out to `git`.
#
# Build from the REPO ROOT (not from components/tenant-api/) because go.mod
# uses a replace directive pointing to ../threshold-exporter/app:
#
#   docker build -t ghcr.io/vencil/tenant-api:2.4.0 \
#     -f components/tenant-api/Dockerfile .
#
# Run:
#   docker run -p 8080:8080 -v $(pwd)/conf.d:/conf.d ghcr.io/vencil/tenant-api:2.4.0

# ── Build stage ──────────────────────────────────────────────────────────────
FROM golang:1.26-alpine AS builder

WORKDIR /src

# Copy both modules required by the replace directive
COPY components/tenant-api/go.mod components/tenant-api/go.sum \
     components/tenant-api/
COPY components/threshold-exporter/app/go.mod components/threshold-exporter/app/go.sum \
     components/threshold-exporter/app/

WORKDIR /src/components/tenant-api
RUN go mod download

# Copy full source for both modules
WORKDIR /src
COPY components/tenant-api/ components/tenant-api/
COPY components/threshold-exporter/app/ components/threshold-exporter/app/

WORKDIR /src/components/tenant-api
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w" \
    -o /tenant-api \
    ./cmd/server

# ── Final stage ──────────────────────────────────────────────────────────────
# alpine + git: required because gitops/writer.go shells out to `git`.
FROM alpine:3.22

# Install git and ca-certs (needed for git over HTTPS)
RUN apk add --no-cache git ca-certificates tzdata \
    && addgroup -S -g 65534 nonroot \
    && adduser  -S -u 65534 -G nonroot nonroot

COPY --from=builder /tenant-api /usr/local/bin/tenant-api

USER nonroot

ENTRYPOINT ["/usr/local/bin/tenant-api"]
CMD ["--addr=:8080", "--config-dir=/conf.d"]

EXPOSE 8080

# Labels (OCI image spec)
LABEL org.opencontainers.image.title="tenant-api" \
      org.opencontainers.image.description="Tenant Management API — Dynamic Alerting Platform" \
      org.opencontainers.image.source="https://github.com/vencil/vibe-k8s-lab" \
      org.opencontainers.image.version="2.4.0"
