# Tenant-projection gate image for the helm/vector log shipper (ADR-021 Phase 2(a), #908).
#
# Runs as a Vector INIT-CONTAINER: before Vector starts it validates the deployed
# tenantProjections against the authoritative _account_registry.yaml (a read-only
# conf.d mount) and populates Vector's --config-dir — placing the tenant-routing
# fragment ONLY when every projection's accountId matches the registry, and
# degrading to the platform-only (0:0) base config otherwise. This is the only
# un-bypassable enforcement point: Vector cannot load a config the init-container
# did not assemble.
#
# Build (from this directory):
#   docker build -t <registry>/vector-projection-gate:<tag> .
# Scan before publishing (release discipline):
#   trivy image --severity HIGH,CRITICAL <registry>/vector-projection-gate:<tag>

FROM alpine:3.23.4
# python3 + PyYAML (the gate parses the registry + projections YAML). py3-yaml is
# the distro package, so the image carries no pip and no build toolchain.
RUN apk add --no-cache python3 py3-yaml \
 && rm -rf /var/cache/apk/*
# The chart runs these containers with readOnlyRootFilesystem:true (#908 PR-3 m3),
# so the rootfs contract is: NOTHING here may write outside the mounted volumes.
# PYTHONDONTWRITEBYTECODE makes Python's __pycache__ behavior explicit — without it
# Python merely degrades gracefully (a failed .pyc write on the read-only rootfs is
# swallowed); with it the write is never attempted (Gemini #971 review). NB there is
# deliberately NO /tmp volume: neither script uses tempfile/temp dirs today. IF a
# future dependency needs one (C-extensions, archive handling, tempfile.*), the
# container will crash at startup on the read-only rootfs — the fix THEN is an
# emptyDir mounted at /tmp in the DaemonSet, not loosening the read-only rootfs.
ENV PYTHONDONTWRITEBYTECODE=1
COPY verify_tenant_projections.py /usr/local/bin/verify_tenant_projections.py
# serve_metrics.py runs as a long-lived SIDECAR (command override in the DaemonSet)
# that re-serves the init-container's verdict textfile over HTTP for Prometheus
# (#908 PR-3). It never re-evaluates the gate — see its module docstring. Both
# scripts use only the stdlib + PyYAML already installed above.
COPY serve_metrics.py /usr/local/bin/serve_metrics.py
# Non-root. The Vector pod's securityContext (fsGroup) makes the shared config-dir
# emptyDir group-writable so the gate can populate it; see the deployment wiring.
USER 65532:65532
# rationale: an init-container's exit status IS the signal — it must run to
# completion before Vector starts — so there is no image-level HEALTHCHECK.
# Default entrypoint is the one-shot validator (the init-container use); the
# metrics sidecar overrides `command` to run serve_metrics.py.
ENTRYPOINT ["python3", "/usr/local/bin/verify_tenant_projections.py"]
