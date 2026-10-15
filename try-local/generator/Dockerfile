# try-local only (#2903): the generator container's service
# (`da-tools generator-service`, ADR-036 §3) for the demo stack. tenant-api
# asks it before every write that changes conf.d and refuses to start without
# it. The published da-tools image cannot stand in yet: tools/v2.9.0 has no
# generator-service, git or amtool.
#
# The service's code is NOT copied in — compose bind-mounts the checkout's
# scripts/tools at /opt/da-tools, so the demo judges with what the checkout
# holds. This image is only the runtime the da-tools image gives it:
# Python + PyYAML (same pins as components/da-tools/app/Dockerfile), git,
# amtool, and the nonroot user at uid 10001 — which tenant-api runs as too
# (docker-compose.yaml `user:`): the service answers only a peer of its own
# uid.
FROM python:3.13.15-alpine3.23

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

# amtool: without it the service answers every write 503 amtool_missing
# (ADR-036 §3). The same ref the da-tools image copies it from.
COPY --from=prom/alertmanager:v0.34.1@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a /bin/amtool /usr/local/bin/amtool

USER nonroot:nonroot

# GET /v1/status over the socket (the chart's probe does the same): 200 = up
# and answering, not necessarily `ready`.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(4); s.connect('/run/da-generator/generator.sock'); s.sendall(b'GET /v1/status HTTP/1.1\\r\\nHost: h\\r\\n\\r\\n'); sys.exit(0 if s.recv(64).startswith(b'HTTP/1.1 200 ') else 1)"]

ENTRYPOINT ["python3", "/opt/da-tools/ops/generator_service.py"]
