FROM golang:1.26-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /threshold-exporter .

FROM gcr.io/distroless/static-debian12:nonroot

# Distroless advantages:
# - Runs as nonroot (UID 65532) by default
# - No shell/apk/package manager → minimal attack surface
# - Static binary (CGO_ENABLED=0) requires no libc
# - CA certificates included in base image
# - No libavif/openssl/glibc → immune to CVE-2025-48174, CVE-2025-15467
# - Go's built-in crypto/tls replaces openssl dependency
#
# CVE posture: Trivy typically reports 0 vulnerabilities on this image.
# Rebuild monthly to pick up CA certificate updates.
#
# For debugging in development, temporarily use: FROM alpine:3.22
# and add: apk add ca-certificates && addgroup -g 10001 -S nonroot && adduser -S -D -H -u 10001 ...

COPY --from=builder /threshold-exporter /usr/local/bin/threshold-exporter

EXPOSE 8080
# HEALTHCHECK removed: distroless has no wget/curl; K8s probes handle health checks
ENTRYPOINT ["/usr/local/bin/threshold-exporter"]
