---
title: "Operator Alertmanager Integration Guide"
tags: [operator, alertmanager, integration, receiver]
audience: [platform-engineer]
version: v2.6.0
lang: en
---
# Operator Alertmanager Integration Guide

> **Audience**: Platform Engineers, SREs
> **Version**: v2.6.0
> **Related Document**: [BYO Alertmanager Integration Guide](byo-alertmanager-integration.en.md) (ConfigMap Path)
> **Related ADR**: [ADR-008 — Operator CRD Path](../adr/008-operator-native-integration-path.en.md)

---

## Overview

This guide covers how to manage per-tenant alert routing, receiver configuration, and inhibition rules using **AlertmanagerConfig CRD** in a Prometheus Operator environment. This is the Operator equivalent of the `generate_alertmanager_routes.py` tool in the ConfigMap path.

**Strict Mutual Exclusion**: Alertmanager in a single cluster cannot simultaneously manage routing via ConfigMap and AlertmanagerConfig CRD.

---

## Prerequisites

Verify Alertmanager is enabled and configured to accept AlertmanagerConfig:

```yaml
# kube-prometheus-stack values.yaml
alertmanager:
  enabled: true
  alertmanagerSpec:
    alertmanagerConfigSelector: {}    # Empty selector = include all AlertmanagerConfig
```

```bash
kubectl get crd alertmanagerconfigs.monitoring.coreos.com
kubectl get alertmanager -n monitoring
```

---

## 6 Receiver Templates

v2.6.0's `operator-generate` supports 6 receiver templates. All sensitive information is referenced via `secretKeyRef` to K8s Secrets—**plaintext must never be written to YAML** (Enterprise audit requirement).

### Slack

```yaml
receivers:
  - name: db-a-slack
    slackConfigs:
      - apiURL:
          secret:
            name: da-db-a-slack        # K8s Secret name
            key: webhook-url           # Key within Secret
        channel: "#alerts-db-a"
        title: '{{ template "slack.default.title" . }}'
        text: '{{ or .CommonAnnotations.summary_zh .CommonAnnotations.summary "Alert triggered" }}'
        sendResolved: true
```

### PagerDuty

```yaml
receivers:
  - name: db-a-pagerduty
    pagerdutyConfigs:
      - routingKey:
          secret:
            name: da-db-a-pagerduty
            key: routing-key
        description: '{{ template "pagerduty.default.description" . }}'
        severity: '{{ if eq .CommonLabels.severity "critical" }}critical{{ else }}warning{{ end }}'
        sendResolved: true
```

### Email

```yaml
receivers:
  - name: db-a-email
    emailConfigs:
      - to: "alerts-db-a@example.com"
        from: "dynamic-alerting@example.com"
        smarthost: "smtp.example.com:587"
        authUsername: "da-alerts-db-a"
        authPassword:
          secret:
            name: da-db-a-email
            key: smtp-password
        requireTLS: true
        sendResolved: true
```

### Microsoft Teams

```yaml
receivers:
  - name: db-a-teams
    webhookConfigs:
      - url: "http://prometheus-msteams:2000/alertmanager"
        httpConfig:
          authorization:
            credentials:
              secret:
                name: da-db-a-teams
                key: webhook-url
        sendResolved: true
```

### OpsGenie

```yaml
receivers:
  - name: db-a-opsgenie
    opsgenieConfigs:
      - apiKey:
          secret:
            name: da-db-a-opsgenie
            key: api-key
        message: '{{ .CommonAnnotations.summary | default "DA Alert" }}'
        priority: '{{ if eq .CommonLabels.severity "critical" }}P1{{ else }}P3{{ end }}'
        tags: "dynamic-alerting,db-a"
        sendResolved: true
```

### Webhook (Generic)

```yaml
receivers:
  - name: db-a-webhook
    webhookConfigs:
      - url: "http://alert-receiver:5001/webhook/db-a"
        httpConfig:
          authorization:
            credentials:
              secret:
                name: da-db-a-webhook
                key: auth-token
        sendResolved: true
```

---

## Auto-generate with operator-generate

```bash
# Slack receiver template, specify Secret
da-tools operator-generate \
  --components alertmanager \
  --receiver-template slack \
  --secret-name da-slack-alerts \
  --secret-key webhook-url \
  --output-dir ./alertmanager-crd \
  --gitops

# PagerDuty receiver template
da-tools operator-generate \
  --components alertmanager \
  --receiver-template pagerduty \
  --secret-name da-pd-alerts \
  --output-dir ./alertmanager-crd
```

---

## Three-State Mode CRD Inhibition Rules

### Severity Dedup (Critical Suppresses Warning)

```yaml
inhibitRules:
  - sourceMatch:
      - name: tenant
        value: db-a
      - name: severity
        value: critical
    targetMatch:
      - name: tenant
        value: db-a
      - name: severity
        value: warning
    equal: ["alertname", "instance"]
```

### Silent Mode (Sentinel Suppresses Warning / Critical)

```yaml
inhibitRules:
  # Sentinel suppresses warning
  - sourceMatch:
      - name: tenant
        value: db-a
      - name: alertname
        value: TenantSilentWarning
    targetMatch:
      - name: tenant
        value: db-a
      - name: severity
        value: warning
    equal: ["tenant"]
  # Sentinel suppresses critical
  - sourceMatch:
      - name: tenant
        value: db-a
      - name: alertname
        value: TenantSilentCritical
    targetMatch:
      - name: tenant
        value: db-a
      - name: severity
        value: critical
    equal: ["tenant"]
```

### Maintenance Mode (Sentinel Suppresses All Alerts)

```yaml
inhibitRules:
  - sourceMatch:
      - name: tenant
        value: db-a
      - name: alertname
        value: TenantMaintenanceMode
    targetMatch:
      - name: tenant
        value: db-a
    equal: ["tenant"]
```

> In v2.6.0, `operator-generate` automatically generates the complete set of inhibition rules for each tenant (severity dedup + silent + maintenance).

---

## Complete AlertmanagerConfig Example

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
    receiver: db-a-slack
    groupBy: ["alertname", "instance"]
    groupWait: 5s
    groupInterval: 5m
    repeatInterval: 12h
    matchers:
      - name: tenant
        value: db-a
  receivers:
    - name: db-a-slack
      slackConfigs:
        - apiURL:
            secret:
              name: da-db-a-slack
              key: webhook-url
          channel: "#alerts-db-a"
          sendResolved: true
  inhibitRules:
    # Severity dedup
    - sourceMatch:
        - {name: tenant, value: db-a}
        - {name: severity, value: critical}
      targetMatch:
        - {name: tenant, value: db-a}
        - {name: severity, value: warning}
      equal: ["alertname", "instance"]
    # Silent mode - warning
    - sourceMatch:
        - {name: tenant, value: db-a}
        - {name: alertname, value: TenantSilentWarning}
      targetMatch:
        - {name: tenant, value: db-a}
        - {name: severity, value: warning}
      equal: ["tenant"]
    # Silent mode - critical
    - sourceMatch:
        - {name: tenant, value: db-a}
        - {name: alertname, value: TenantSilentCritical}
      targetMatch:
        - {name: tenant, value: db-a}
        - {name: severity, value: critical}
      equal: ["tenant"]
    # Maintenance mode
    - sourceMatch:
        - {name: tenant, value: db-a}
        - {name: alertname, value: TenantMaintenanceMode}
      targetMatch:
        - {name: tenant, value: db-a}
      equal: ["tenant"]
```

---

## Create K8s Secret

Receivers use `secretKeyRef`, so you must create corresponding Secrets:

```bash
# Slack
kubectl create secret generic da-db-a-slack \
  --from-literal=webhook-url='https://hooks.slack.com/services/T.../B.../xxx' \
  -n monitoring

# PagerDuty
kubectl create secret generic da-db-a-pagerduty \
  --from-literal=routing-key='your-pagerduty-routing-key' \
  -n monitoring
```

> **Security Note**: In GitOps environments, use Sealed Secrets or External Secrets Operator to manage Secrets, preventing plaintext from entering the Git repository.

---

## Troubleshooting

### AlertmanagerConfig Not Taking Effect

```bash
kubectl get alertmanagerconfig -n monitoring
kubectl get alertmanager -n monitoring \
  -o jsonpath='{.items[0].spec.alertmanagerConfigSelector}' | jq .
```

If `alertmanagerConfigSelector` is not empty, verify AlertmanagerConfig labels match.

### Secret Not Found

```bash
kubectl get secret -n monitoring | grep da-
```

Ensure Secret and AlertmanagerConfig are in the same namespace.

---

## Related Documents

| Document | Description |
|----------|-------------|
| [Operator Prometheus Integration](operator-prometheus-integration.en.md) | ServiceMonitor + PrometheusRule |
| [Operator GitOps Deployment](operator-gitops-deployment.en.md) | ArgoCD / Flux integration |
| [BYO Alertmanager Integration](byo-alertmanager-integration.en.md) | ConfigMap path (Path A) |
| [ADR-008](../adr/008-operator-native-integration-path.en.md) | Architecture Decision Record |
