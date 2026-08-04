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
# CVE coverage is AUTOMATED as of #1337: this image is a `scan` matrix entry in
# .github/workflows/nightly-image-scan.yaml (nightly) and in
# component-docker-build.yaml (PR) + `make trivy-scan-all` (pre-tag). Ad-hoc
# `trivy image` is no longer the only thing standing between this image and a
# regression.

# --- Stage 1: build mtail from source ---------------------------------
# ⛔ We build rather than download the upstream release binary, and that is a
# SECURITY decision, not a preference. mtail's last release is v3.0.8
# (2024-08-08); its prebuilt binary is linked against Go stdlib 1.21.1, which
# by 2026 carries 20 fixable HIGH/CRITICAL stdlib CVEs plus grpc ones — and no
# upstream release exists to bump to. Measured on this image: the prebuilt
# binary on Alpine 3.23.4 scores 23 fixable HIGH/CRITICAL, this from-source
# build on 3.23.5 scores 2 (both from mtail's OWN go.mod grpc pin, which only
# upstream can move). ⚠️ TWO levers, not one — rebuilding the SAME source with a
# current toolchain clears the 20 Go-stdlib findings and introduces 0, and the
# base bump below clears the remaining libcrypto3 one. 23 − 20 − 1 = 2.
#
# The supply-chain control is PRESERVED, not dropped: the tarball's SHA-256 is
# replaced by a COMMIT pin, which is strictly stronger — git verifies object
# hashes end to end, and a commit id cannot be re-pointed the way a tag can.
# d4b8a71 is what `v3.0.8` dereferences to AND what the official release binary
# self-reports as its revision, so this builds the release's exact source tree.
FROM golang:1.26.5-alpine3.23 AS build
ARG MTAIL_VERSION=3.0.8
ARG MTAIL_COMMIT=d4b8a7111c756f22f568f04c0d1ea3f3ec161494
RUN apk add --no-cache git
WORKDIR /src
# `fetch --depth 1 <sha>` (not a tag/branch): content-addressed and shallow.
RUN git init -q . \
 && git remote add origin https://github.com/google/mtail \
 && git fetch -q --depth 1 origin "${MTAIL_COMMIT}" \
 && git checkout -q FETCH_HEAD
# ldflags mirror upstream's Makefile (GO_LDFLAGS): without them the binary
# reports `invalid:-use-make-to-build` for both Version and Revision, and
# `mtail --version` is what an operator reads to confirm what is running.
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags "-s -w -extldflags '-static' \
                -X main.Version=${MTAIL_VERSION} \
                -X main.Revision=${MTAIL_COMMIT} \
                -X main.Branch=v${MTAIL_VERSION}" \
      -o /out/mtail ./cmd/mtail

# --- Stage 2: runtime -------------------------------------------------
FROM alpine:3.23.5
# logrotate from the Alpine repo. busybox already supplies sh + wget
# (wget --post-data is enough to POST Envoy's admin /reopen_logs), so the
# runtime image carries no curl.
RUN apk add --no-cache logrotate \
 && rm -rf /etc/periodic /etc/logrotate.d/* /var/cache/apk/*
COPY --from=build /out/mtail /usr/local/bin/mtail
# Match the gateway pod's non-root uid/gid (podSecurityContext: 101).
USER 101:101
# rationale: sidecar (mtail + logrotate) co-scheduled in the gateway pod —
# health is governed by the pod's probes on the primary Envoy container, not
# an image-level HEALTHCHECK on this auxiliary container.
