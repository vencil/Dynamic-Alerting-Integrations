---
title: "Operator Shadow Monitoring Strategy"
tags: [operator, shadow-monitoring, migration]
audience: [platform-engineer]
version: v2.9.0
lang: en
---
# Operator Shadow Monitoring Strategy

> **Language / 語言：** **English (Current)** | [中文](./operator-shadow-monitoring.md)

> **Audience**: Platform Engineers, SREs
> **Version**: v2.6.0
> **Prerequisite Reading**: [Operator Alertmanager Integration](operator-alertmanager-integration.en.md)

---

## Overview

Shadow Monitoring is a dual-track observation strategy used when changing alert routing in Operator environments. It lets you confirm that a tenant's alerts actually flow through the new routing config without impacting production alerts.

On the Operator path, routing is one `alertmanager.yaml` rendered by `generate-routes` and loaded through the Alertmanager CR's `spec.configSecret` (see [Operator Alertmanager Integration](operator-alertmanager-integration.en.md)), so the shadow route goes into **your source (base)**, not into a separate AlertmanagerConfig CR.

---

## Strategy: a `continue: true` shadow route in the source

```yaml
# alertmanager.base.yaml (your source, not generate-routes output)
route:
  receiver: default
  routes:
    - receiver: db-a-shadow-webhook
      matchers:
        - tenant="db-a"
      continue: true                # ★ Key: keep matching after this route, so the tenant route still receives it
receivers:
  - name: default
  - name: db-a-shadow-webhook
    webhook_configs:
      - url: "http://shadow-collector:8080/collect"
        send_resolved: true
```

> After the merge the order is: the 4 platform routes → the base's own routes (this shadow route is here) → the tenant routes. `continue: true` lets an alert go on to the tenant routes after the shadow route matched it, so it is sent to both the production receiver and the shadow receiver.

⚠️ Do not use an AlertmanagerConfig CR as the shadow route: the Operator adds a `namespace="<CR namespace>"` matcher to a CR's routes, so the shadow route only sees alerts carrying that label and the comparison does not reflect production traffic.

---

## Observation Workflow

### Step 1: Deploy Shadow Collector

Deploy a lightweight webhook receiver to collect shadow alerts:

```bash
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shadow-collector
  namespace: monitoring
spec:
  replicas: 1
  selector:
    matchLabels:
      app: shadow-collector
  template:
    metadata:
      labels:
        app: shadow-collector
    spec:
      containers:
        - name: collector
          image: ghcr.io/vencil/da-tools:v2.9.0
          command: ["python3", "-m", "http.server", "8080"]
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: shadow-collector
  namespace: monitoring
spec:
  selector:
    app: shadow-collector
  ports:
    - port: 8080
EOF
```

### Step 2: Merge the shadow route into the production config

```bash
da-tools generate-routes --config-dir conf.d --output-alertmanager \
  --base-config alertmanager.base.yaml -o alertmanager.yaml
kubectl create secret generic da-alertmanager-config -n monitoring \
  --from-file=alertmanager.yaml --dry-run=client -o yaml | kubectl apply -f -
```

### Step 3: Compare Production vs Shadow

```bash
# Production alert count
curl -s 'http://localhost:9093/api/v2/alerts' | \
  jq '[.[] | select(.labels.tenant=="db-a")] | length'

# Shadow collector logs
kubectl logs -n monitoring deploy/shadow-collector --tail=50
```

### Step 4: Remove the Shadow Route After Confirming No Differences

Delete the shadow route and its receiver from `alertmanager.base.yaml`, rerun Step 2 and update the Secret.

---

## Important Notes

1. **The shadow route does not see alerts the platform routes take**: the 4 platform routes come before the base, so alerts they take (e.g. custom alerts in the `component="custom"` isolation subtree) never reach the shadow route.
2. **Performance Impact**: Shadow routing generates additional HTTP requests. In high-traffic environments, consider limiting shadow observation to specific tenants.
3. **Cleanup**: After observation is complete, always delete the shadow route from the source and regenerate, to avoid long-term extra webhook invocations.

---

## Related Documents

| Document | Description |
|----------|-------------|
| [Operator Alertmanager Integration](operator-alertmanager-integration.en.md) | The `configSecret` routing procedure |
| [Operator GitOps Deployment](operator-gitops-deployment.en.md) | CI/CD integration |
| [Shadow Monitoring SOP](../shadow-monitoring-sop.en.md) | Generic Shadow Monitoring workflow |
