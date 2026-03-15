FROM golang:1.26.1-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /threshold-exporter .

FROM alpine:3.23
RUN apk --no-cache add ca-certificates && \
    addgroup -g 65534 -S nonroot && \
    adduser -S -D -H -u 65534 -h /nonexistent -s /sbin/nologin -G nonroot nonroot
COPY --from=builder /threshold-exporter /usr/local/bin/threshold-exporter

USER nonroot:nonroot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1
ENTRYPOINT ["threshold-exporter"]
