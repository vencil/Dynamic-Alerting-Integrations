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
COPY verify_tenant_projections.py /usr/local/bin/verify_tenant_projections.py
# Non-root. The Vector pod's securityContext (fsGroup) makes the shared config-dir
# emptyDir group-writable so the gate can populate it; see the deployment wiring.
USER 65532:65532
# rationale: an init-container's exit status IS the signal — it must run to
# completion before Vector starts — so there is no image-level HEALTHCHECK.
ENTRYPOINT ["python3", "/usr/local/bin/verify_tenant_projections.py"]
