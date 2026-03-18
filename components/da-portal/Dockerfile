# da-portal — Self-hosted Interactive Tools & Tenant Self-Service Portal
#
# Zero build step: all JSX files are transpiled in the browser via Babel standalone.
# Vendor JS files are bundled for air-gapped / offline deployment.
#
# Build:  docker build -t da-portal:v2.2.0 -f components/da-portal/Dockerfile .
# Run:    docker run -p 8080:80 da-portal:v2.2.0
#
# Customisation via volume mounts:
#   -v ./my-platform-data.json:/usr/share/nginx/html/assets/platform-data.json
#   -v ./my-flows.json:/usr/share/nginx/html/assets/flows.json
#   -v ./my-nginx.conf:/etc/nginx/conf.d/default.conf

FROM nginx:1.28.2-alpine3.23

LABEL org.opencontainers.image.title="da-portal" \
      org.opencontainers.image.description="Dynamic Alerting Interactive Tools Portal" \
      org.opencontainers.image.source="https://github.com/vencil/Dynamic-Alerting-Integrations" \
      org.opencontainers.image.vendor="Platform Engineering Team"

# ── Security hardening ─────────────────────────────────────────────
# 1. Upgrade all packages for CVE fixes at build time
# 2. Remove nginx modules not needed by a static file server:
#    - nginx-module-image-filter → pulls in libavif, libgd, libpng, libjpeg, aom, dav1d (28 packages)
#    - nginx-module-xslt → pulls in libxml2, libxslt
#    Removing the modules lets apk cascade-remove all transitive dependencies,
#    dropping image from 72 to 44 packages (~23MB saved).
# 3. Pin Alpine 3.23 (libavif >= 1.3.0, openssl 3.x CVE-2025-15467 fix)
#
# IMPORTANT: Enterprise registries should rebuild images monthly.
# CI workflow includes Trivy scan on every release (CRITICAL+HIGH block).
RUN apk --no-cache upgrade && \
    apk --no-cache del \
      nginx-module-image-filter \
      nginx-module-xslt \
    && \
    if apk info -e libavif 2>/dev/null; then \
      echo "ERROR: libavif still present — CVE-2025-48174 not mitigated" >&2; \
      exit 1; \
    fi

# Remove default nginx page
RUN rm -rf /usr/share/nginx/html/*

# ── Nginx config ──────────────────────────────────────────────
COPY --chown=nginx:nginx components/da-portal/nginx.conf /etc/nginx/conf.d/default.conf

# ── Interactive Tools Hub + 29 JSX tools ──────────────────────
COPY --chown=nginx:nginx docs/interactive/ /usr/share/nginx/html/interactive/

# ── Getting Started Wizard ────────────────────────────────────
COPY --chown=nginx:nginx docs/getting-started/wizard.jsx /usr/share/nginx/html/getting-started/wizard.jsx

# ── Shared assets (loader, data, registry, flows) ─────────────
COPY --chown=nginx:nginx docs/assets/jsx-loader.html     /usr/share/nginx/html/assets/jsx-loader.html
COPY --chown=nginx:nginx docs/assets/platform-data.json  /usr/share/nginx/html/assets/platform-data.json
COPY --chown=nginx:nginx docs/assets/tool-registry.yaml  /usr/share/nginx/html/assets/tool-registry.yaml
COPY --chown=nginx:nginx docs/assets/flows.json          /usr/share/nginx/html/assets/flows.json

# ── Vendor JS (offline mode) ───────────────────────────────────
# Run `make vendor-download` before build for air-gapped deployments.
# CI does this automatically; for local builds, Makefile ensures the dir exists.
# Image works via CDN fallback when vendor files are absent.
COPY --chown=nginx:nginx docs/assets/vendor/ /usr/share/nginx/html/assets/vendor/

# ── GitHub Pages compat ───────────────────────────────────────
COPY --chown=nginx:nginx docs/.nojekyll /usr/share/nginx/html/.nojekyll

# ── Non-root user (nginx runs as nginx user on Alpine) ─────────
# nginx:alpine image supports running as non-root via NGINX_ENTRYPOINT_WORKER_PROCESSES_AUTOTUNE
# Static content serving does not require root after startup
# nginx-alpine is preferred over distroless here because nginx requires config reload capability
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://localhost/healthz || exit 1
