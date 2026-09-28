# E2E-bench webhook receiver — captures Alertmanager webhooks for the
# B-1 Phase 2 5-anchor measurement (T4).
#
# Same distroless pattern as components/threshold-exporter/app/Dockerfile
# for consistency: nonroot UID 65532, no shell, static binary.

# Builder Go version == go.mod `go` directive (tests/ops/test_go_toolchain_parity.py).
FROM golang:1.26.8-alpine3.23 AS builder
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /receiver .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /receiver /usr/local/bin/receiver
EXPOSE 5001
ENTRYPOINT ["/usr/local/bin/receiver"]
