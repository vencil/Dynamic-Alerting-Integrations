FROM golang:1.26.3-alpine3.22 AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

# main package files live at app root; pkg/ holds the config library
# that config_hierarchy.go / config_inheritance.go / config_parse.go
# import (extracted as a proper library in PR-7/PR-8 during v2.8.0).
# Both must be COPY'd or `go build` fails with "no required module
# provides package github.com/vencil/threshold-exporter/pkg/config".
COPY *.go ./
COPY pkg ./pkg
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
