# da-portal Helm Chart

Self-Hosted Interactive Tools Portal for the Dynamic Alerting Platform (v2.5.0).

## Overview

da-portal is an nginx-based static file server that delivers transpiled Interactive Tools (JSX) to users via browser-side Babel transpilation. It serves:

- Static HTML/JavaScript/CSS for the Interactive Tools UI
- `platform-data.json` and `flows.json` configuration
- Reverse proxy to tenant-api at `/api/v1/` for API operations
- oauth2-proxy sidecar for authentication

### Key Features

- **Static File Serving**: Optimized nginx with security headers, caching policies
- **OAuth2 Authentication**: sidecar oauth2-proxy (GitHub, Google, OIDC, GitLab)
- **Reverse Proxy**: Transparent proxy to tenant-api with proper header forwarding
- **Health Checks**: `/healthz` endpoint for liveness/readiness probes
- **Network Policies**: Namespace-based ingress control
- **Security**: Non-root pod security context, read-only filesystems, seccomp
- **Customization**: ConfigMap-based nginx config override, custom data volumes

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Service (port 80) → oauth2-proxy (4180)                         │
│                                                                  │
│ Sidecar Pattern:                                                 │
│ ┌──────────────────────┐         ┌─────────────────────┐       │
│ │  nginx               │◄────────┤  oauth2-proxy       │       │
│ │  (port 80)           │         │  (port 4180)        │       │
│ │  - Static tools      │         │  - Authentication   │       │
│ │  - /healthz          │         │  - Session mgmt     │       │
│ │  - /api/v1/* proxy   │         │  - OAuth2 flow      │       │
│ └──────────────────────┘         └─────────────────────┘       │
│         │                                                        │
│         ├─► /healthz (readiness/liveness)                      │
│         ├─► / (static files from /usr/share/nginx/html)        │
│         └─► /api/v1/* → tenant-api (reverse proxy)             │
└─────────────────────────────────────────────────────────────────┘
```

### Port Mapping

| Port | Service | Audience | Purpose |
|------|---------|----------|---------|
| 80 | Service.port | External, via Ingress | oauth2-proxy entry point |
| 4180 | oauth2-proxy internal | localhost only | Authentication/session |
| 8080 | Service.internalPort | Cluster-internal | Direct nginx access (health, API) |

## Installation

### Basic Install

```bash
# Install with defaults
helm install da-portal helm/da-portal \
  -n monitoring \
  -f helm/da-portal/values.yaml

# Or with environment-specific overrides
helm install da-portal helm/da-portal \
  -n monitoring \
  -f helm/da-portal/values.yaml \
  -f helm/da-portal/values-prod.yaml
```

### Configuration

#### OAuth2 Secrets

You **must** provide valid OAuth2 credentials before deployment:

```bash
# Create placeholder secret
helm install da-portal helm/da-portal -n monitoring

# Edit the secret with real values
kubectl -n monitoring edit secret oauth2-proxy-secrets

# Key fields:
# OAUTH2_PROXY_COOKIE_SECRET: <32-byte base64-encoded random>
# OAUTH2_PROXY_CLIENT_ID: <GitHub OAuth App client ID>
# OAUTH2_PROXY_CLIENT_SECRET: <GitHub OAuth App client secret>
```

Or generate a new secret before install:

```bash
# Generate 32-byte secret
SECRET=$(openssl rand -base64 32)

helm install da-portal helm/da-portal -n monitoring \
  --set oauth2Proxy.secretName=my-oauth-secrets \
  --set oauth2Proxy.createSecret=false \
  -f - <<EOF
---
apiVersion: v1
kind: Secret
metadata:
  name: my-oauth-secrets
type: Opaque
stringData:
  OAUTH2_PROXY_COOKIE_SECRET: "$SECRET"
  OAUTH2_PROXY_CLIENT_ID: "your-client-id"
  OAUTH2_PROXY_CLIENT_SECRET: "your-client-secret"
EOF
```

#### Ingress

Enable ingress for external access:

```bash
helm install da-portal helm/da-portal -n monitoring \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=da-portal.example.com' \
  --set oauth2Proxy.redirectUrl=https://da-portal.example.com/oauth2/callback \
  --set oauth2Proxy.cookieSecure=true
```

#### Tenant API URL

By default, the chart assumes tenant-api is in the same cluster at:
`http://tenant-api.monitoring.svc.cluster.local:8080`

To override:

```bash
helm install da-portal helm/da-portal -n monitoring \
  --set 'portal.tenantApiUrl=http://tenant-api.db-a.svc.cluster.local:8080'
```

#### Custom nginx Config

Provide a custom nginx configuration:

```bash
helm install da-portal helm/da-portal -n monitoring \
  --set 'nginx.customConfig=<path-to-custom-nginx.conf>'
```

Or via values file:

```yaml
# values-custom.yaml
nginx:
  customConfig: |
    server {
      listen 80;
      location / {
        root /usr/share/nginx/html;
        index index.html;
      }
    }
```

#### Custom Data (platform-data.json, flows.json, etc.)

Mount custom data from a ConfigMap:

```bash
# Create ConfigMap with custom files
kubectl -n monitoring create configmap da-portal-custom \
  --from-file=platform-data.json \
  --from-file=flows.json

helm install da-portal helm/da-portal -n monitoring \
  --set customData.enabled=true \
  --set customData.type=configMap \
  --set customData.configMapName=da-portal-custom
```

## Values Reference

### Global

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| replicaCount | int | 1 | Number of portal replicas |
| image.repository | string | `ghcr.io/vencil/da-portal` | Container image |
| image.tag | string | `2.5.0` | Image tag |
| image.pullPolicy | string | `IfNotPresent` | Image pull policy |

### Portal Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| portal.listenPort | int | 80 | nginx listening port |
| portal.tenantApiUrl | string | `http://tenant-api.monitoring.svc.cluster.local:8080` | Reverse proxy target |

### OAuth2 Proxy

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| oauth2Proxy.enabled | bool | true | Enable oauth2-proxy sidecar |
| oauth2Proxy.provider | string | `github` | OAuth2 provider |
| oauth2Proxy.redirectUrl | string | `http://da-portal.example.com/oauth2/callback` | OAuth redirect URI |
| oauth2Proxy.emailDomain | string | `*` | Allowed email domain(s) |
| oauth2Proxy.cookieSecure | bool | true | Secure cookie flag (set false for local dev) |
| oauth2Proxy.cookieName | string | `_da_portal_session` | Session cookie name |
| oauth2Proxy.secretName | string | `oauth2-proxy-secrets` | Secret containing credentials |
| oauth2Proxy.createSecret | bool | true | Auto-create placeholder Secret |

### Service & Ingress

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| service.type | string | `ClusterIP` | Service type |
| service.port | int | 80 | External port |
| service.internalPort | int | 8080 | Internal port (nginx direct) |
| ingress.enabled | bool | false | Enable Ingress |
| ingress.className | string | `""` | Ingress class name |
| ingress.hosts | list | `[]` | Ingress hosts |

### Resources

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| resources.nginx.requests.cpu | string | `30m` | nginx CPU request |
| resources.nginx.requests.memory | string | `64Mi` | nginx memory request |
| resources.oauth2Proxy.requests.cpu | string | `20m` | oauth2-proxy CPU request |
| resources.oauth2Proxy.requests.memory | string | `32Mi` | oauth2-proxy memory request |

### Security

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| podSecurityContext.runAsUser | int | 65534 | Pod run-as UID (nobody) |
| podSecurityContext.runAsGroup | int | 65534 | Pod run-as GID |
| networkPolicy.enabled | bool | true | Enable NetworkPolicy |
| networkPolicy.allowedNamespaces | list | `[monitoring, db-a, db-b]` | Namespaces allowed ingress |

## Deployment Examples

### Local Development (Kind)

```bash
helm install da-portal helm/da-portal \
  -n monitoring \
  --set oauth2Proxy.cookieSecure=false \
  --set oauth2Proxy.redirectUrl=http://localhost:8080/oauth2/callback \
  --set oauth2Proxy.createSecret=false \
  --set-string 'oauth2Proxy.secretName=dev-oauth-secrets'

# Create mock secret
kubectl -n monitoring create secret generic dev-oauth-secrets \
  --from-literal=OAUTH2_PROXY_COOKIE_SECRET=dev-secret-12345678901234567890 \
  --from-literal=OAUTH2_PROXY_CLIENT_ID=dev-client \
  --from-literal=OAUTH2_PROXY_CLIENT_SECRET=dev-secret
```

### Production (HTTPS, Real OAuth)

```bash
# Create real OAuth2 secret (e.g., via Sealed Secrets)
# ... (assume secret created separately)

helm install da-portal helm/da-portal \
  -n monitoring \
  --set replicaCount=3 \
  --set oauth2Proxy.cookieSecure=true \
  --set oauth2Proxy.redirectUrl=https://da-portal.company.com/oauth2/callback \
  --set oauth2Proxy.provider=google \
  --set oauth2Proxy.emailDomain=company.com \
  --set oauth2Proxy.createSecret=false \
  --set 'oauth2Proxy.secretName=oauth2-secrets-prod' \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=da-portal.company.com' \
  --set 'ingress.hosts[0].paths[0].path=/' \
  --set 'ingress.hosts[0].paths[0].pathType=Prefix'
```

## Health Checks

The chart includes readiness and liveness probes configured for the nginx container:

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: 80
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 3

livenessProbe:
  httpGet:
    path: /healthz
    port: 80
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3
```

Test health endpoint:

```bash
# Inside container
curl http://localhost/healthz

# Via port-forward
kubectl -n monitoring port-forward svc/da-portal 8080:8080
curl http://localhost:8080/healthz
```

## Reverse Proxy Configuration

The nginx configuration in `configmap-nginx.yaml` reverse-proxies `/api/v1/` to the tenant-api service:

```nginx
location /api/v1/ {
  proxy_pass http://tenant-api.monitoring.svc.cluster.local:8080/api/v1/;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  # ... (auth headers, timeouts, buffering)
}
```

This allows Interactive Tools to communicate with the tenant-api without cross-origin requests.

## Troubleshooting

### 401 Unauthorized

- Verify oauth2-proxy is running: `kubectl -n monitoring logs -f deployment/da-portal -c oauth2-proxy`
- Check Secret credentials: `kubectl -n monitoring get secret oauth2-proxy-secrets -o yaml`
- Verify OAuth app callback URL matches `oauth2Proxy.redirectUrl`

### 502 Bad Gateway

- Check tenant-api service: `kubectl -n monitoring get svc tenant-api`
- Verify DNS resolution: `kubectl -n monitoring exec -it deployment/da-portal -c nginx -- nslookup tenant-api.monitoring.svc.cluster.local`
- Test proxy target: `kubectl -n monitoring port-forward svc/tenant-api 8080:8080`

### Static Files Not Loading

- Check ConfigMap: `kubectl -n monitoring get cm da-portal-nginx-config -o yaml`
- Verify nginx container logs: `kubectl -n monitoring logs -f deployment/da-portal -c nginx`
- Check image build: `docker inspect ghcr.io/vencil/da-portal:2.5.0`

## Upgrading

```bash
# Fetch latest chart
helm repo update

# Dry-run upgrade
helm upgrade da-portal helm/da-portal \
  -n monitoring \
  --dry-run

# Apply upgrade
helm upgrade da-portal helm/da-portal \
  -n monitoring \
  -f helm/da-portal/values.yaml
```

## Security Considerations

1. **oauth2-proxy Credentials**: Store CLIENT_ID and CLIENT_SECRET in a secure secret backend (Sealed Secrets, Vault, External Secrets Operator). Never commit to Git.

2. **HTTPS**: Always set `cookieSecure=true` and `redirectUrl=https://...` in production.

3. **Email Domain Restriction**: Set `emailDomain` to your organization's domain to restrict access (e.g., `company.com`).

4. **Network Policies**: The chart includes a NetworkPolicy that restricts ingress to specified namespaces. Customize `networkPolicy.allowedNamespaces` for your environment.

5. **Pod Security Context**: Runs as non-root user (UID 65534) with read-only root filesystem. Do not override unless necessary.

## Reference

- [oauth2-proxy Configuration](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/)
- [nginx Documentation](https://nginx.org/en/docs/)
- [Kubernetes Security Best Practices](https://kubernetes.io/docs/concepts/security/)
- [Dynamic Alerting Architecture](../../docs/architecture-and-design.md)

## License

See LICENSE in repository root.
