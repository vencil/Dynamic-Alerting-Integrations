# Audit-log logrotate sidecar image for the federation-gateway (ADR-020 IV-2f, #511).
#
# Caps the gateway's audit-log emptyDir mirror (Envoy access-log sink 2) so it
# can never grow large enough to evict the pod. The durable audit trail is the
# Envoy stdout sink, picked up by the helm/vector DaemonSet and shipped to
# helm/victorialogs (#539 Phase 1 platform log aggregation) — not this file.
#
# Until #1278 D1 this image also built mtail from source for the metrics
# sidecar. mtail's upstream is dormant and its grpc pin carried the image's only
# fixable HIGH/CRITICAL findings; the metrics sidecar is now upstream Vector
# (values.yaml `auditLog.metrics`, program files/audit-metrics.vector.yaml), so
# this image no longer compiles anything.
#
# Build (from this directory):
#   docker build -t <registry>/federation-audit-sidecar:alpine3.23.6-1 .
# ⛔ The tag is `alpine<alpine version>-<build revision>`, and values.yaml pins
# the whole string. Change anything in this file and you MUST bump the tag: there
# is no digest knob for this container and the pod template's checksum
# annotations hash only ConfigMaps, so an unchanged tag renders a byte-identical
# pod spec — no rollout, and `IfNotPresent` keeps serving the previous build
# (#1337). tests/shared/test_federation_audit_metrics.py pins recipe ↔ tag.
# ⚠️ Renovate's dockerfile manager (#1354) DOES see the FROM below and will open
# the alpine bump; that PR goes red on the recipe ↔ tag guard until the tag is
# bumped with it — which is the reminder, not a false alarm.
# CVE coverage: a `scan` matrix entry in .github/workflows/nightly-image-scan.yaml
# (nightly), component-docker-build.yaml (PR) and `make trivy-scan-all` (pre-tag).

# 3.23.6: libcrypto3 3.5.8-r0 (CVE-2026-14456; 3.23.5 ships 3.5.7-r0), #1278.
FROM alpine:3.23.6
# logrotate from the Alpine repo. busybox already supplies sh + wget
# (wget --post-data is enough to POST Envoy's admin /reopen_logs), so the
# image carries no curl.
RUN apk add --no-cache logrotate \
 && rm -rf /etc/periodic /etc/logrotate.d/* /var/cache/apk/*
# Match the gateway pod's non-root uid/gid (podSecurityContext: 101).
USER 101:101
# rationale: sidecar co-scheduled in the gateway pod — health is governed by
# the pod's probes on the primary Envoy container, not an image-level
# HEALTHCHECK on this auxiliary container.
