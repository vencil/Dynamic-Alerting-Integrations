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

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NGINX_PORT` | `80` | Listening port inside the container |
| `NGINX_WORKER_PROCESSES` | `auto` | Worker process count |

Configuration is primarily done via volume mounts (see Customisation above) rather than environment variables.

## Troubleshooting

| 症狀 / Symptom | 可能原因 / Cause | 解法 / Fix |
|----------------|-----------------|-----------|
| Blank page after load | CDN deps not available (air-gapped) | Run `make vendor-download` before `make portal-image` |
| Tools show stale data | Mounted `platform-data.json` outdated | Re-run `make platform-data` and re-mount |
| CORS errors in browser console | Prometheus proxy not configured | Mount custom `nginx.conf` with `/api/v1/` proxy (see above) |
| 404 on `/healthz` | Old image version | Rebuild with `make portal-image` (health check added in v2.3.0) |
| JSX tools fail to render | Babel transpile error | Check browser console; ensure JSX files are valid |

## Related Documentation

- [Interactive Tools Hub](../../docs/interactive-tools.md) — Tool registry and usage guide
- [Platform Data Generation](../../docs/cli-reference.md) — `make platform-data` reference
- [Guided Flows](../../docs/assets/flows.json) — Flow definitions

## Version Governance

The portal follows the project's five-line versioning scheme:

| Tag pattern    | Artifact                     |
|----------------|------------------------------|
| `portal/v*`    | `ghcr.io/vencil/da-portal`   |
| `exporter/v*`  | threshold-exporter + Helm    |
| `tools/v*`     | da-tools CLI image           |
| `tenant-api/v*`| tenant-api REST API          |
| `v*`           | Platform tag (GitHub Release)|
