FROM golang:1.26.1-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /threshold-exporter .

FROM alpine:3.21

# Security: upgrade all packages to pick up CVE fixes (CVE-2025-15467 openssl, CVE-2025-48174 libavif)
RUN apk --no-cache upgrade && \
    apk --no-cache add ca-certificates && \
    addgroup -g 10001 -S nonroot && \
    adduser -S -D -H -u 10001 -h /nonexistent -s /sbin/nologin -G nonroot nonroot

COPY --from=builder /threshold-exporter /usr/local/bin/threshold-exporter

USER nonroot:nonroot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1
ENTRYPOINT ["threshold-exporter"]
