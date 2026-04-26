# E2E-bench webhook receiver — captures Alertmanager webhooks for the
# B-1 Phase 2 5-anchor measurement (T4).
#
# Same distroless pattern as components/threshold-exporter/app/Dockerfile
# for consistency: nonroot UID 65532, no shell, static binary.

FROM golang:1.26.2-alpine3.22 AS builder
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /receiver .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /receiver /usr/local/bin/receiver
EXPOSE 5001
ENTRYPOINT ["/usr/local/bin/receiver"]
