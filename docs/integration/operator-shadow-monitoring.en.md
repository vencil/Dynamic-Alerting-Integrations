---
title: "Operator Shadow Monitoring Strategy"
tags: [operator, shadow-monitoring, migration]
audience: [platform-engineer]
version: v2.7.0
lang: en
---
# Operator Shadow Monitoring Strategy

> **Language / 語言：** **English (Current)** | [中文](./operator-shadow-monitoring.md)

> **Audience**: Platform Engineers, SREs
> **Version**: v2.6.0
> **Prerequisite Reading**: [Operator Alertmanager Integration](operator-alertmanager-integration.en.md)

---

## Overview

Shadow Monitoring is a dual-track observation strategy used during routing migration in Operator environments. It allows you to validate new AlertmanagerConfig routing configuration without impacting production alerts.

---

## Strategy: Dual AlertmanagerConfig in Parallel

### Production Route (Existing)

```yaml
apiVersion: monitoring.coreos.com/v1beta1
kind: AlertmanagerConfig
metadata:
  name: da-tenant-db-a
  namespace: monitoring
  labels:
    app.kubernetes.io/part-of: dynamic-alerting
    tenant: db-a
spec:
  route:
    receiver: db-a-pagerduty
    matchers:
      - name: tenant
        value: db-a
    groupBy: ["alertname", "instance"]
  receivers:
    - name: db-a-pagerduty
      pagerdutyConfigs:
        - routingKey:
            secret:
              name: da-db-a-pagerduty
              key: routing-key
```

### Shadow Route (Observation)

```yaml
apiVersion: monitoring.coreos.com/v1beta1
kind: AlertmanagerConfig
metadata:
  name: da-shadow-db-a
  namespace: monitoring
  labels:
    app.kubernetes.io/part-of: dynamic-alerting
    tenant: db-a
    shadow: "true"
spec:
  route:
    receiver: db-a-shadow-webhook
    matchers:
      - name: tenant
        value: db-a
    continue: true                  # ★ Key: don't stop routing, continue matching next route
  receivers:
    - name: db-a-shadow-webhook
      webhookConfigs:
        - url: "http://shadow-collector:8080/collect"
          sendResolved: true
```

> The `continue: true` setting allows Alertmanager to continue matching subsequent routes after this one, ensuring alerts are sent to both production and shadow receivers.

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
          image: ghcr.io/vencil/da-tools:v2.7.0
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

### Step 2: Deploy Shadow AlertmanagerConfig

```bash
kubectl apply -f shadow-alertmanagerconfig-db-a.yaml
```

### Step 3: Compare Production vs Shadow

```bash
# Production alert count
curl -s 'http://localhost:9093/api/v1/alerts' | \
  jq '[.data[] | select(.labels.tenant=="db-a")] | length'

# Shadow collector logs
kubectl logs -n monitoring deploy/shadow-collector --tail=50
```

### Step 4: Switch After Confirming No Differences

```bash
# Delete shadow route
kubectl delete alertmanagerconfig da-shadow-db-a -n monitoring

# Update production route (if changes needed)
kubectl apply -f alertmanagerconfig-db-a-new.yaml
```

---

## Important Notes

1. **Shadow route must come before production route**: Alertmanager matches routes sequentially, and `continue: true` must be set on an earlier route. Within the same namespace, AlertmanagerConfigs are ordered alphabetically by metadata.name.
2. **Performance Impact**: Shadow routing generates additional HTTP requests. In high-traffic environments, consider limiting shadow observation to specific tenants.
3. **Cleanup**: After observation is complete, always delete the shadow AlertmanagerConfig to avoid long-term extra webhook invocations.

---

## Related Documents

| Document | Description |
|----------|-------------|
| [Operator Alertmanager Integration](operator-alertmanager-integration.en.md) | Complete AlertmanagerConfig configuration |
| [Operator GitOps Deployment](operator-gitops-deployment.en.md) | CI/CD integration |
| [Shadow Monitoring SOP](../shadow-monitoring-sop.en.md) | Generic Shadow Monitoring workflow |
