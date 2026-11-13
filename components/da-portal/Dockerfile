# da-portal — Self-hosted Interactive Tools & Tenant Self-Service Portal
#
# Tools are pre-built into ESM bundles (esbuild) and served as static files —
# no runtime build/transpile in the browser. The bundles are built from
# tools/portal/ in the `portal-build` stage below (#3738); docs/assets/dist/ is
# not in git and .dockerignore keeps a host-built copy out of the context. Vendor JS (React / Tailwind /
# Lucide) is bundled for air-gapped / offline deployment, with CDN fallback
# otherwise.
#
# Build:  docker build -t da-portal:v2.8.0 -f components/da-portal/Dockerfile .
# Run:    docker run -p 8080:80 da-portal:v2.8.0
#
# Customisation via volume mounts:
#   -v ./my-platform-data.json:/usr/share/nginx/html/assets/platform-data.json
#   -v ./my-flows.json:/usr/share/nginx/html/assets/flows.json
#   -v ./my-nginx.conf:/etc/nginx/conf.d/default.conf

# ── Stage 1: build the portal bundles from source (#3738) ──────────
# --platform=$BUILDPLATFORM: the output is plain JS, identical for every target
# arch, so the multi-arch release build runs this stage once, natively, instead
# of under QEMU. Node major = the dev container's
# (docs/internal/toolchain-versions.md);
# tests/shared/test_toolchain_pin_parity.py binds it.
# The digest pins the exact image (Renovate's "Dockerfile base images" group
# refreshes it); its major is not Renovate's to move — see renovate.json.
FROM --platform=$BUILDPLATFORM node:24-alpine3.23@sha256:3946fb783fdce8a2812136ea694bc95c698c4f4096f3fd18568dc27a8ac389dc AS portal-build

# build.mjs writes ../../docs/assets/dist relative to itself, so keep the
# repo layout: the bundles land in /src/docs/assets/dist/.
WORKDIR /src/tools/portal
# Lockfile first so the dependency layer is cached across source-only changes.
# node_modules never comes from the context (.dockerignore excludes it).
COPY tools/portal/package.json tools/portal/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tools/portal/ ./
RUN npm run build

# ── Stage 2: the nginx image that serves them ───────────────────────
FROM nginx:1.31-alpine3.23

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
# Static server block only (ADR-027 §5): the stock main config is used as is.
# The tenant-api chart runs this image with its own full config (`nginx -c`).
COPY --chown=nginx:nginx components/da-portal/nginx.conf /etc/nginx/conf.d/default.conf

# ── Interactive Tools Hub (index + changelog) ─────────────────
COPY --chown=nginx:nginx docs/interactive/ /usr/share/nginx/html/interactive/

# ── Tool bundles (esbuild ESM output from the portal-build stage) ──
# jsx-loader.html loads /assets/dist/<tool>.js at runtime; the legacy
# in-browser Babel transpile path is gone, so these bundles MUST be in the
# image or every tool 404s. Taken from the stage above, never from a
# docs/assets/dist/ in the build context (#3738).
COPY --from=portal-build --chown=nginx:nginx /src/docs/assets/dist/ /usr/share/nginx/html/assets/dist/

# ── Getting Started Wizard ────────────────────────────────────
# Path follows the portal monorepo restructure (PR #279, TD-042, 2026-05-07):
# wizard.jsx moved from docs/getting-started/ to tools/portal/src/getting-started/.
COPY --chown=nginx:nginx tools/portal/src/getting-started/wizard.jsx /usr/share/nginx/html/getting-started/wizard.jsx

# ── Shared assets (loader, data, registry, flows) ─────────────
# ⛔ This is an ALLOWLIST: an asset the portal fetches at runtime but that is
# missing here 404s in the image while still working on GitHub Pages (mkdocs
# publishes docs/ wholesale) — a defect visible only to self-hosters. Pinned by
# scripts/tools/lint/check_portal_asset_shipping.py; add the COPY, don't
# silence the lint.
COPY --chown=nginx:nginx docs/assets/jsx-loader.html     /usr/share/nginx/html/assets/jsx-loader.html
COPY --chown=nginx:nginx docs/assets/platform-data.json  /usr/share/nginx/html/assets/platform-data.json
COPY --chown=nginx:nginx docs/assets/tool-registry.yaml  /usr/share/nginx/html/assets/tool-registry.yaml
COPY --chown=nginx:nginx docs/assets/flows.json          /usr/share/nginx/html/assets/flows.json
# design-tokens.css — <link> of BOTH shells (jsx-loader.html:7,
# interactive/index.html:7); every --da-* token resolves through it.
COPY --chown=nginx:nginx docs/assets/design-tokens.css   /usr/share/nginx/html/assets/design-tokens.css
# recommendation-data.json — self-service-portal's Alert Preview tab.
COPY --chown=nginx:nginx docs/assets/recommendation-data.json /usr/share/nginx/html/assets/recommendation-data.json
# template-data.json — Config Template Gallery.
COPY --chown=nginx:nginx docs/assets/template-data.json  /usr/share/nginx/html/assets/template-data.json

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
# Probe via 127.0.0.1, NOT localhost: nginx listens on IPv4 :80 only, but in a
# dual-stack container `localhost` resolves to ::1 (IPv6) first → the probe would
# never succeed and the container stays `unhealthy`. K8s ignores this docker-native
# HEALTHCHECK (it runs its own readinessProbe against the pod IP), but `docker run`
# and docker-compose rely on it. We keep nginx IPv4-only and make the probe IPv4-
# explicit rather than adding `listen [::]:80;`, which would crash nginx at startup
# on IPv6-disabled hosts (`Address family not supported by protocol`).
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1/healthz || exit 1
