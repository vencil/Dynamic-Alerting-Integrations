# da-portal — Self-hosted Interactive Tools & Tenant Self-Service Portal
#
# Zero build step: all JSX files are transpiled in the browser via Babel standalone.
# Vendor JS files are bundled for air-gapped / offline deployment.
#
# Build:  docker build -t da-portal:v2.1.0 -f components/da-portal/Dockerfile .
# Run:    docker run -p 8080:80 da-portal:v2.1.0
#
# Customisation via volume mounts:
#   -v ./my-platform-data.json:/usr/share/nginx/html/assets/platform-data.json
#   -v ./my-flows.json:/usr/share/nginx/html/assets/flows.json
#   -v ./my-nginx.conf:/etc/nginx/conf.d/default.conf

FROM nginx:1.28-alpine

LABEL org.opencontainers.image.title="da-portal" \
      org.opencontainers.image.description="Dynamic Alerting Interactive Tools Portal" \
      org.opencontainers.image.source="https://github.com/vencil/Dynamic-Alerting-Integrations" \
      org.opencontainers.image.vendor="Platform Engineering Team"

# Security: upgrade all packages for CVE fixes at build time
# nginx 1.28 stable fixes CVE-2026-1642 (SSL upstream injection)
# Alpine 3.22+ ships libavif >= 1.3.0, fixing CVE-2025-48174
RUN apk --no-cache upgrade

# Remove default nginx page
RUN rm -rf /usr/share/nginx/html/*

# ── Nginx config ──────────────────────────────────────────────
COPY components/da-portal/nginx.conf /etc/nginx/conf.d/default.conf

# ── Interactive Tools Hub + 23 JSX tools ──────────────────────
COPY docs/interactive/ /usr/share/nginx/html/interactive/

# ── Getting Started Wizard ────────────────────────────────────
COPY docs/getting-started/wizard.jsx /usr/share/nginx/html/getting-started/wizard.jsx

# ── Shared assets (loader, data, registry, flows) ─────────────
COPY docs/assets/jsx-loader.html     /usr/share/nginx/html/assets/jsx-loader.html
COPY docs/assets/platform-data.json  /usr/share/nginx/html/assets/platform-data.json
COPY docs/assets/tool-registry.yaml  /usr/share/nginx/html/assets/tool-registry.yaml
COPY docs/assets/flows.json          /usr/share/nginx/html/assets/flows.json

# ── Vendor JS (offline mode) ───────────────────────────────────
# Run `make vendor-download` before build for air-gapped deployments.
# CI does this automatically; for local builds, Makefile ensures the dir exists.
# Image works via CDN fallback when vendor files are absent.
COPY docs/assets/vendor/ /usr/share/nginx/html/assets/vendor/

# ── GitHub Pages compat ───────────────────────────────────────
COPY docs/.nojekyll /usr/share/nginx/html/.nojekyll

# ── Non-root user (nginx runs as nginx user on Alpine) ─────────
# nginx:alpine image supports running as non-root via NGINX_ENTRYPOINT_WORKER_PROCESSES_AUTOTUNE
# Static content serving does not require root after startup
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://localhost/healthz || exit 1
