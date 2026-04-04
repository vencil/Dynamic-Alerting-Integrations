# da-portal — Self-Hosted Interactive Tools Portal

Lightweight Docker image that bundles the Dynamic Alerting Interactive Tools Hub
(31 JSX tools + Guided Flows) behind an nginx static server. Designed for
**air-gapped** and **enterprise intranet** deployment where accessing the public
GitHub Pages site is not feasible.

## Quick Start

```bash
# Build from repo root (requires `make vendor-download` for offline mode)
make vendor-download            # optional: bundle CDN deps for air-gapped use
make portal-image               # builds ghcr.io/vencil/da-portal:latest

# Run
docker run -p 8080:80 ghcr.io/vencil/da-portal:v2.3.0
# Open http://localhost:8080
```

## Customisation

Override data files via volume mounts — no rebuild needed:

```bash
docker run -p 8080:80 \
  -v ./my-platform-data.json:/usr/share/nginx/html/assets/platform-data.json \
  -v ./my-flows.json:/usr/share/nginx/html/assets/flows.json \
  ghcr.io/vencil/da-portal:v2.3.0
```

### Prometheus Reverse Proxy (CORS-free)

Edit `nginx.conf` to point `/api/v1/` at the tenant's internal Prometheus,
then mount the custom config:

```bash
docker run -p 8080:80 \
  -v ./custom-nginx.conf:/etc/nginx/conf.d/default.conf \
  ghcr.io/vencil/da-portal:v2.3.0
```

This allows the Self-Service Portal's alert preview to query Prometheus
directly without CORS restrictions.

## Image Details

| Property       | Value                  |
|----------------|------------------------|
| Base image     | `nginx:1.27-alpine`    |
| Estimated size | ~11 MB (with vendor)   |
| Health check   | `GET /healthz`         |
| Port           | 80                     |
| Build step     | None (browser-side Babel transpile) |

## Version Governance

The portal follows the project's four-line versioning scheme:

| Tag pattern    | Artifact                     |
|----------------|------------------------------|
| `portal/v*`    | `ghcr.io/vencil/da-portal`   |
| `exporter/v*`  | threshold-exporter + Helm    |
| `tools/v*`     | da-tools CLI image           |
| `v*`           | Platform tag (GitHub Release)|
