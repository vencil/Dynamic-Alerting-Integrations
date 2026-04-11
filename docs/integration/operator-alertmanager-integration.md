---
title: "Operator Alertmanager 整合指南"
tags: [operator, alertmanager, integration, receiver]
audience: [platform-engineer]
version: v2.6.0
lang: zh
---
# Operator Alertmanager 整合指南

> **受眾**：Platform Engineers、SREs
> **版本**：v2.6.0
> **對應文件**：[BYO Alertmanager 整合指南](byo-alertmanager-integration.md)（ConfigMap 路徑）
> **相關 ADR**：[ADR-008 — Operator CRD 路徑](../adr/008-operator-native-integration-path.md)

---

## Overview

本指南涵蓋 Prometheus Operator 環境下，如何使用 **AlertmanagerConfig CRD** 管理 per-tenant 的告警路由、receiver 配置和抑制規則。這是 ConfigMap 路徑中 `generate_alertmanager_routes.py` 的 Operator 等價方案。

**嚴格互斥**：同一叢集的 Alertmanager 不可同時使用 ConfigMap 和 AlertmanagerConfig CRD 管理路由。

---

## Prerequisites

確認 Alertmanager 已啟用且設定為接納 AlertmanagerConfig：

```yaml
# kube-prometheus-stack values.yaml
alertmanager:
  enabled: true
  alertmanagerSpec:
    alertmanagerConfigSelector: {}    # 空 selector = 納入所有 AlertmanagerConfig
```

```bash
kubectl get crd alertmanagerconfigs.monitoring.coreos.com
kubectl get alertmanager -n monitoring
```

---

## 6 種 Receiver 模板

v2.6.0 的 `operator-generate` 支援 6 種 receiver 模板。所有機密資訊透過 `secretKeyRef` 引用 K8s Secret——**禁止明文寫入 YAML**（Enterprise 稽核必查項目）。

### Slack

```yaml
receivers:
  - name: db-a-slack
    slackConfigs:
      - apiURL:
          secret:
            name: da-db-a-slack        # K8s Secret 名稱
            key: webhook-url           # Secret 中的 key
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

### Webhook（通用）

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

## 使用 operator-generate 自動產出

```bash
# Slack receiver 模板，指定 Secret
da-tools operator-generate \
  --components alertmanager \
  --receiver-template slack \
  --secret-name da-slack-alerts \
  --secret-key webhook-url \
  --output-dir ./alertmanager-crd \
  --gitops

# PagerDuty receiver 模板
da-tools operator-generate \
  --components alertmanager \
  --receiver-template pagerduty \
  --secret-name da-pd-alerts \
  --output-dir ./alertmanager-crd
```

---

## 三態模式 CRD 抑制規則

### Severity Dedup（Critical 抑制 Warning）

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

### Silent Mode（Sentinel 抑制 Warning / Critical）

```yaml
inhibitRules:
  # Sentinel 抑制 warning
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
  # Sentinel 抑制 critical
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

### Maintenance Mode（Sentinel 抑制所有告警）

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

> `operator-generate` 在 v2.6.0 自動為每個 tenant 產出上述完整的抑制規則集（severity dedup + silent + maintenance）。

---

## 完整 AlertmanagerConfig 範例

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

## 建立 K8s Secret

Receiver 使用 `secretKeyRef`，需先建立對應的 Secret：

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

> **安全提醒**：在 GitOps 環境中，使用 Sealed Secrets 或 External Secrets Operator 管理 Secret，避免明文進入 Git repo。

---

## Troubleshooting

### AlertmanagerConfig 未生效

```bash
kubectl get alertmanagerconfig -n monitoring
kubectl get alertmanager -n monitoring \
  -o jsonpath='{.items[0].spec.alertmanagerConfigSelector}' | jq .
```

若 `alertmanagerConfigSelector` 不為空，確認 AlertmanagerConfig 的 labels 匹配。

### Secret 不存在

```bash
kubectl get secret -n monitoring | grep da-
```

確認 Secret 與 AlertmanagerConfig 在同一 namespace。

---

## 相關文件

| 文件 | 說明 |
|------|------|
| [Operator Prometheus 整合](operator-prometheus-integration.md) | ServiceMonitor + PrometheusRule |
| [Operator GitOps 部署](operator-gitops-deployment.md) | ArgoCD / Flux 整合 |
| [BYO Alertmanager 整合](byo-alertmanager-integration.md) | ConfigMap 路徑（Path A） |
| [ADR-008](../adr/008-operator-native-integration-path.md) | 架構決策記錄 |
