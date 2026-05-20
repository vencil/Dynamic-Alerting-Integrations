# Audit-log sidecar image for the federation-gateway (ADR-020 IV-2f, #511).
#
# One image, two container roles in the gateway pod:
#   * mtail     - tails the Envoy audit access log (the emptyDir mirror)
#                 and exposes tenant_federation_requests_total on :3903.
#   * logrotate - caps that emptyDir mirror so it can never grow large
#                 enough to evict the pod. The durable audit trail is the
#                 Envoy stdout sink, picked up by the helm/vector
#                 DaemonSet and shipped to helm/victorialogs (#539 Phase 1
#                 platform log aggregation) — not this file.
#
# Build (from this directory):
#   docker build -t <registry>/federation-audit-sidecar:3.0.8 .
# Scan before publishing (release discipline):
#   trivy image --severity HIGH,CRITICAL <registry>/federation-audit-sidecar:3.0.8

# --- Stage 1: fetch + checksum-verify the mtail binary ----------------
FROM alpine:3.23.4 AS fetch
ARG MTAIL_VERSION=3.0.8
# sha256 of mtail_3.0.8_linux_amd64.tar.gz, from the release checksums.txt.
ARG MTAIL_SHA256=123c2ee5f48c3eff12ebccee38befd2233d715da736000ccde49e3d5607724e4
RUN apk add --no-cache curl \
 && curl -fsSL -o /tmp/mtail.tar.gz \
      "https://github.com/google/mtail/releases/download/v${MTAIL_VERSION}/mtail_${MTAIL_VERSION}_linux_amd64.tar.gz" \
 && echo "${MTAIL_SHA256}  /tmp/mtail.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/mtail.tar.gz -C /usr/local/bin mtail \
 && chmod 0755 /usr/local/bin/mtail

# --- Stage 2: runtime -------------------------------------------------
FROM alpine:3.23.4
# logrotate from the Alpine repo. busybox already supplies sh + wget
# (wget --post-data is enough to POST Envoy's admin /reopen_logs), so the
# runtime image carries no curl.
RUN apk add --no-cache logrotate \
 && rm -rf /etc/periodic /etc/logrotate.d/* /var/cache/apk/*
COPY --from=fetch /usr/local/bin/mtail /usr/local/bin/mtail
# Match the gateway pod's non-root uid/gid (podSecurityContext: 101).
USER 101:101
