---
title: "Platform Engineer Quick Start Guide"
tags: [getting-started, platform-setup]
audience: [platform-engineer]
version: v2.2.0
lang: en
---
# Platform Engineer Quick Start Guide

> **v2.1.0** | Audience: Platform Engineers, SREs, Infrastructure Managers
>
> Related docs: [Architecture](../architecture-and-design.md) · [Benchmarks](../architecture-and-design.md) · [GitOps Deployment](../gitops-deployment.md) · [Rule Packs](../rule-packs/README.md)

## Three Things You Need to Know

**1. threshold-exporter is the core.** It reads YAML config, emits Prometheus metrics, and supports SHA-256 hot-reload. Two replicas run HA on port 8080.

**2. Rule Packs are self-contained units.** 15 Rule Packs mount via Projected Volume into Prometheus, each covering a database or service type (MariaDB, PostgreSQL, Redis, etc.). Use the `optional: true` mechanism to safely uninstall unwanted Rule Packs.

**3. Everything is config-driven.** `_defaults.yaml` controls global platform behavior, tenant YAML overrides defaults, and `_profiles.yaml` provides inheritance chains. No hardcoding, no secrets.

## 30-Second Quick Deploy

Minimal viable platform config:

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_cpu: "75"
  mysql_memory: "85"
  # Other default thresholds...
```

### Deploy threshold-exporter ×2 HA

```bash
kubectl apply -f k8s/02-threshold-exporter/
# Verify replicas are running
kubectl get pod -n monitoring | grep threshold-exporter
```

### Mount Rule Packs

```bash
# Prometheus StatefulSet uses Projected Volume
# Confirm volume section in k8s/03-monitoring/prometheus-statefulset.yaml
kubectl get configmap -n monitoring | grep rule-pack
```

## Common Operations

### Managing Global Defaults

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_connections_critical: "95"
  container_cpu: "70"
  container_memory: "80"
  # Dimension threshold omitted (will use default)
  redis_memory: "disable"      # Suppress entirely
  _routing_defaults:
    group_wait: "30s"
    group_interval: "5m"
    repeat_interval: "12h"
```

Validate defaults syntax:

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/ --schema
```

### Managing Rule Packs

List mounted Rule Packs:

```bash
kubectl get configmap -n monitoring | grep rule-pack
# Possible output: rule-pack-mariadb, rule-pack-postgresql, rule-pack-redis...
```

Remove unwanted Rule Pack (edit Prometheus StatefulSet):

```bash
kubectl edit statefulset prometheus -n monitoring
# Remove corresponding configMapRef from volumes.projected.sources
# Or set Projected Volume optional: true for safe uninstallation
```

### Setting Up Platform Enforced Routing (_routing_enforced)

Enable dual-channel notifications (NOC + Tenant):

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_enforced:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/B/xxx"
      channel: "#noc-alerts"
    group_wait: "10s"
    repeat_interval: "2h"
```

NOC receives notifications using `platform_summary` annotation, focused on capacity planning and escalation decisions. Tenants still receive their own `summary` unaffected.

### Setting Up Routing Defaults (_routing_defaults)

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_defaults:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/{{tenant}}-alerts"
      channel: "#{{tenant}}-team"
    group_wait: "30s"
    repeat_interval: "4h"
```

The `{{tenant}}` placeholder expands to each tenant's name. Tenant YAML's `_routing` can override this default.

### Configuring Tenant Profiles

```yaml
# conf.d/_profiles.yaml
profiles:
  standard-db:
    mysql_connections: "80"
    mysql_cpu: "75"
    container_memory: "85"
  high-load-db:
    mysql_connections: "60"     # Stricter
    mysql_cpu: "60"
    container_memory: "80"
```

Tenants inherit via `_profile`:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    _profile: "standard-db"
    mysql_connections: "70"     # Overrides profile value
```

### Configuring Routing Profiles & Domain Policies (v2.1.0 ADR-007)

When multiple tenants share the same routing configuration, create `_routing_profiles.yaml` to define named routing profiles:

```yaml
# conf.d/_routing_profiles.yaml
routing_profiles:
  team-sre-apac:
    receiver:
      type: slack
      api_url: "https://hooks.slack.com/sre-apac"
    group_wait: 30s
    repeat_interval: 4h
  team-dba-global:
    receiver:
      type: pagerduty
      service_key: "dba-key-123"
    repeat_interval: 1h
```

Tenants reference profiles via `_routing_profile`. Four-layer merge order: `_routing_defaults` → profile → tenant `_routing` → `_routing_enforced`.

**Domain Policies** define business-domain compliance constraints in `_domain_policy.yaml` (e.g., finance domain forbids Slack):

```yaml
# conf.d/_domain_policy.yaml
domain_policies:
  finance:
    tenants: [db-finance, db-audit]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

Validate: `da-tools check-routing-profiles --config-dir conf.d/`. Debug: `da-tools explain-route --config-dir conf.d/ --tenant db-finance`. JSON Schema available in `docs/schemas/` for VS Code validation.

### Setting Up Webhook Domain Allowlist

Restrict webhook receiver target domains:

```bash
python3 scripts/tools/ops/generate_alertmanager_routes.py \
  --config-dir conf.d/ \
  --policy "*.example.com" \
  --policy "hooks.slack.com" \
  --validate
```

fnmatch patterns support wildcards. ⚠️ Empty list means no restriction — **production environments should always configure an allowlist** to prevent tenants from routing alerts to unauthorized external endpoints.

## Validation Tools

### One-Stop Configuration Validation

```bash
python3 scripts/tools/ops/validate_config.py \
  --config-dir conf.d/ \
  --schema
```

Checked items:
- YAML syntax correctness
- Parameter schema conformance
- Route generation success
- Policy checks pass
- Version consistency

### Alert Quality Scoring (v2.1.0)

```bash
# Scan all tenants for alert quality (Noise / Stale / Latency / Suppression)
da-tools alert-quality --prometheus http://localhost:9090 --config-dir conf.d/

# CI gate: exit 1 if score below 60
da-tools alert-quality --prometheus http://localhost:9090 --ci --min-score 60
```

### Policy-as-Code Validation (v2.1.0)

```bash
# Evaluate all tenants against _policies DSL in _defaults.yaml
da-tools evaluate-policy --config-dir conf.d/

# CI gate: exit 1 on error violations
da-tools evaluate-policy --config-dir conf.d/ --ci
```

### Cardinality Forecasting (v2.1.0)

```bash
# Predict per-tenant cardinality growth trend and days-to-limit
da-tools cardinality-forecast --prometheus http://localhost:9090

# CI gate: exit 1 on critical risk
da-tools cardinality-forecast --prometheus http://localhost:9090 --ci
```

### Configuration Difference Analysis

```bash
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline \
  --new-dir conf.d/ \
  --format json
```

Output: added tenants, removed tenants, changed defaults, changed profiles. Use for GitOps PR review.

### Version Consistency Check

```bash
make version-check
python3 scripts/tools/dx/bump_docs.py --check
```

Ensure versions in CLAUDE.md, README, and CHANGELOG are synchronized.

## Performance Monitoring

### Run Benchmarks

```bash
make benchmark ARGS="--under-load --routing-bench --alertmanager-bench --reload-bench --json"
```

Output metrics:
- Idle memory footprint
- Scaling curve (QPS vs memory/latency)
- Routing throughput
- Alertmanager response time
- ConfigMap reload latency

Results saved as JSON for CI comparison.

### Platform Rule Pack Self-Monitoring

The platform itself provides Rule Pack alerts (e.g., exporter offline, Alertmanager delay > 1m):

```bash
kubectl get alerts -n monitoring | grep platform
```

## Production Security Hardening

### Lifecycle Endpoint Protection

Prometheus and Alertmanager's `--web.enable-lifecycle` exposes `/-/reload` and `/-/quit` endpoints **without any authentication**. Anyone with access to the port can shut down the service via `POST /-/quit`.

Recommended approach:

```yaml
# NetworkPolicy: restrict lifecycle endpoints to configmap-reload sidecar only
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: prometheus-lifecycle-restrict
  namespace: monitoring
spec:
  podSelector:
    matchLabels:
      app: prometheus
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: prometheus  # sidecar within same pod
      ports:
        - port: 9090
```

Alternatively, deploy an auth proxy (e.g., oauth2-proxy) to protect `/-/` paths.

### Grafana Default Password

This project's `deployment-grafana.yaml` uses `admin:admin` as initial password, **for development environments only**. Before production deployment, you **must** set a strong password via K8s Secret, and consider integrating auth proxy or SSO.

### Webhook Domain Allowlist

An empty `--policy` list in `generate_alertmanager_routes.py` means no restriction. **Production environments should always configure an allowlist** to prevent tenants from routing alert notifications to unauthorized external endpoints.

### Port-forward Security

Local `kubectl port-forward` binds to `127.0.0.1` (localhost only) by default. **Never use `--address 0.0.0.0`** — this exposes Prometheus/Alertmanager/Grafana to all network interfaces, allowing anyone with network access to the machine to reach these services directly.

### Secrets Management — Migrating from ConfigMap to K8s Secret

Sensitive information in Alertmanager receiver configs (Slack tokens, webhook URLs, PagerDuty service keys, etc.) should not be stored in plaintext within ConfigMaps. `kubectl get configmap -o yaml` reveals all contents, while K8s Secrets provide at least base64 encoding and support fine-grained RBAC access control.

**Basic approach — K8s Secret + secretKeyRef:**

```yaml
# 1. Create Secret (one-time, or managed by CI)
kubectl create secret generic alertmanager-secrets \
  --from-literal=slack-api-url='https://hooks.slack.com/services/T.../B.../xxx' \
  --from-literal=pagerduty-key='your-service-key' \
  -n monitoring

# 2. Reference in Alertmanager Deployment
env:
  - name: SLACK_API_URL
    valueFrom:
      secretKeyRef:
        name: alertmanager-secrets
        key: slack-api-url
  - name: PAGERDUTY_KEY
    valueFrom:
      secretKeyRef:
        name: alertmanager-secrets
        key: pagerduty-key
```

In receiver configs generated by `generate_alertmanager_routes.py`, use `<secret>` or environment variable references instead of plaintext values.

**Advanced approach — External Secrets Operator + HashiCorp Vault:**

For production environments requiring centralized secrets management, automatic rotation, and audit logs, integrate External Secrets Operator (ESO):

```yaml
# 1. Install External Secrets Operator
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace

# 2. Configure SecretStore (connect to Vault)
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: vault-backend
  namespace: monitoring
spec:
  provider:
    vault:
      server: "https://vault.internal:8200"
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "alertmanager"
          serviceAccountRef:
            name: alertmanager

# 3. Define ExternalSecret (auto-sync Vault → K8s Secret)
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: alertmanager-secrets
  namespace: monitoring
spec:
  refreshInterval: 1h          # Sync hourly, supports automatic rotation
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: alertmanager-secrets  # Generated K8s Secret name
    creationPolicy: Owner
  data:
    - secretKey: slack-api-url
      remoteRef:
        key: dynamic-alerting/alertmanager
        property: slack-api-url
    - secretKey: pagerduty-key
      remoteRef:
        key: dynamic-alerting/alertmanager
        property: pagerduty-key
```

Vault-side configuration:

```bash
# Enable KV v2 engine
vault secrets enable -path=secret kv-v2

# Write secrets
vault kv put secret/dynamic-alerting/alertmanager \
  slack-api-url="https://hooks.slack.com/services/T.../B.../xxx" \
  pagerduty-key="your-service-key"

# Create policy (least privilege)
vault policy write alertmanager - <<EOF
path "secret/data/dynamic-alerting/alertmanager" {
  capabilities = ["read"]
}
EOF

# Bind K8s ServiceAccount
vault write auth/kubernetes/role/alertmanager \
  bound_service_account_names=alertmanager \
  bound_service_account_namespaces=monitoring \
  policies=alertmanager \
  ttl=1h
```

Benefits of this architecture: secrets never enter Git, automatic rotation via `refreshInterval`, Vault provides full audit logs, RBAC precisely controls who can access which secrets.

### TLS Encrypted Communication Guide

In production environments, communication between threshold-exporter, Prometheus, and Alertmanager should use TLS to prevent metrics data and alert content from being intercepted during network transmission.

**Step 1 — Issue certificates with cert-manager (recommended):**

```yaml
# Install cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.0/cert-manager.yaml

# Create self-signed CA (dev) or reference production CA
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: monitoring-ca
spec:
  selfSigned: {}  # Use ACME or internal CA in production

# Issue Prometheus TLS certificate
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: prometheus-tls
  namespace: monitoring
spec:
  secretName: prometheus-tls
  issuerRef:
    name: monitoring-ca
    kind: ClusterIssuer
  commonName: prometheus.monitoring.svc.cluster.local
  dnsNames:
    - prometheus.monitoring.svc.cluster.local
    - prometheus.monitoring.svc
    - prometheus
```

**Step 2 — Enable TLS on threshold-exporter:**

```yaml
# Add TLS args to Deployment
args:
  - "--tls-cert-file=/etc/tls/tls.crt"
  - "--tls-key-file=/etc/tls/tls.key"
volumeMounts:
  - name: tls
    mountPath: /etc/tls
    readOnly: true
volumes:
  - name: tls
    secret:
      secretName: exporter-tls
```

**Step 3 — Configure Prometheus scrape_configs with TLS:**

```yaml
scrape_configs:
  - job_name: "dynamic-thresholds"
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/tls/ca.crt
      # For mTLS, add client cert
      # cert_file: /etc/prometheus/tls/tls.crt
      # key_file: /etc/prometheus/tls/tls.key
```

**Step 4 — Alertmanager HTTP config TLS:**

```yaml
# Add TLS to webhook_configs in alertmanager.yml
receivers:
  - name: "secure-webhook"
    webhook_configs:
      - url: "https://internal-webhook.example.com/alert"
        http_config:
          tls_config:
            ca_file: /etc/alertmanager/tls/ca.crt
```

### Config Reload Endpoint Security

Prometheus's `/-/reload` and Alertmanager's `/-/reload` are HTTP POST endpoints for triggering configuration reload. This project uses `configmap-reload` sidecar to automatically call these endpoints.

**Security implications:** These endpoints require no authentication. If an attacker can reach the Prometheus/Alertmanager port, they can repeatedly trigger reloads causing performance impact, or shut down the service via `/-/quit` if enabled.

**Production recommendation:** Use the NetworkPolicy from the "Lifecycle Endpoint Protection" section above to restrict access. Ensure Prometheus and Alertmanager use ClusterIP Services (not NodePort/LoadBalancer), reachable only within the cluster.

## FAQ

**Q: How do I add a new Rule Pack?**
A: Create a new YAML file in `rule-packs/` directory and mount the corresponding ConfigMap in Prometheus's Projected Volume config. See the Rule Pack README for templates.

**Q: How do I force NOC to receive all notifications?**
A: Set `_routing_enforced` in `_defaults.yaml`. Notifications go to the NOC channel and each tenant's receiver independently.

**Q: Why does the webhook allowlist reject my domain?**
A: Check if your webhook URL matches the fnmatch pattern using `--policy`. For example, `*.example.com` won't match `webhook.internal.example.com` (multi-level subdomain).

**Q: How do I validate that a new tenant's config won't cause alert noise?**
A: First use `validate_config.py` to check syntax and schema, then `config_diff.py` to see blast radius, finally test in a shadow monitoring environment (see shadow-monitoring-sop.md).

**Q: What is Rule Pack optional: true?**
A: A Kubernetes Projected Volume feature. With `optional: true`, if the ConfigMap doesn't exist, Prometheus still starts (volume mount is empty). Use for safe Rule Pack uninstallation.

**Q: Do I need to customize rules within a Rule Pack?**
A: Don't modify Rule Packs directly. Use `_routing.overrides[]` in tenant YAML to override routing for single rules, or add custom rules via custom rule governance (lint_custom_rules.py).

> 💡 **Interactive Tools** — Validate configs with [Config Lint](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-lint.jsx). Compare config changes with [Config Diff](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-diff.jsx). View Rule Pack dependencies with [Dependency Graph](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/dependency-graph.jsx). Track onboarding progress with [Onboarding Checklist](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/onboarding-checklist.jsx). See all tools at [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/). For enterprise intranet deployment, use the `da-portal` Docker image: `docker run -p 8080:80 ghcr.io/vencil/da-portal` ([deployment guide](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Platform Engineer Quick Start Guide"](for-platform-engineers.en.md) | ⭐⭐⭐ |
| ["Domain Expert (DBA) Quick Start Guide"](for-domain-experts.en.md) | ⭐⭐ |
| ["Tenant Quick Start Guide"](for-tenants.en.md) | ⭐⭐ |
| ["Migration Guide — From Traditional Monitoring to Dynamic Alerting Platform"](../migration-guide.en.md) | ⭐⭐ |
