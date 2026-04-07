---
title: "故障排查與邊界情況"
tags: [troubleshooting, operations]
audience: [platform-engineer, sre, tenant]
version: v2.6.0
lang: zh
---
# 故障排查與邊界情況

> **Language / 語言：** | **中文（當前）**

> 相關文件：[Architecture](architecture-and-design.md) · [HA Design](architecture-and-design.md#4-高可用性設計-high-availability)

## SHA-256 熱重新加載延遲

**情景：** ConfigMap 更新後，threshold-exporter 仍顯示舊值

```bash
# 診斷
$ kubectl get configmap -n monitoring configmap-defaults -o jsonpath='{.metadata.generation}'
5

$ kubectl logs -n monitoring deployment/threshold-exporter | grep "SHA256"
2026-02-26T10:15:32Z SHA256: abc123... (old)
2026-02-26T10:20:45Z SHA256: def456... (updated after 5min)
```

**原因：** Kubernetes 至多每 60 秒同步一次 ConfigMap 掛載

**解決方案：**
1. 強制重新啟動：`kubectl rollout restart deployment/threshold-exporter`
2. 或等待掛載同步（典型 < 1分鐘）

## 空向量警報不觸發

**情景：** Redis 沒有部署匯出器，但 Redis 警報規則仍在評估

```promql
# 問題：
redis_memory_usage_percent{job="redis-exporter"} >= on(tenant) group_left
  user_threshold{metric="redis_memory_usage_percent", severity="warning"}

# 右側為空向量 (user_threshold 無 Redis 資料)
# group_left 匹配失敗 → 警報不觸發 ✓ 預期行為
```

**驗證（非問題）：**
```bash
$ kubectl exec -it prometheus-0 -c prometheus -- \
  promtool query instant 'count(redis_memory_usage_percent)'
0  # 無 Redis 指標 ✓
```

## 雙租戶抓取重複計數

**情景：** Prometheus 從兩個 threshold-exporter 副本抓取，user_threshold 值翻倍

```
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-1)
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-2)
# ↓ sum by(tenant) 會產生 60 （錯誤！）
```

**修正：** 確保所有閾值規則使用 `max by(tenant)`

```yaml
- record: tenant:alert_threshold:slave_lag
  expr: |
    max by(tenant)  # ✓ 不是 sum
      user_threshold{metric="slave_lag"}
```

**閾值 vs 資料——聚合方式的差異：**

此問題僅涉及 **threshold（閾值）recording rules**。閾值本質上是一個設定值（例如「連線上限 100」），無論幾個 exporter 副本回報，數值都相同，因此 `max by(tenant)` 是語義上唯一正確的聚合方式——不存在需要 `sum` 的場景。平台在兩層保證這一點：

1. **Platform Rule Packs**：所有 threshold recording rules 固定使用 `max by(tenant)`
2. **`migrate_rule.py` AST 引擎**：產出的 threshold recording rule 也固定為 `max by(tenant)`，使用者無法覆寫

另一方面，**data（資料）recording rules** 的聚合方式依語義而異。例如 `mysql_threads_connected`（當前連線數）每個副本觀察到的是同一個值，用 `max`；但 `rate(requests_total)`（每秒請求量）若來自不同來源，可能需要 `sum`。Data recording rules 的聚合策略可透過 metric dictionary 指定，不受本節 threshold 聚合約束的影響。

---

> 本文件從 [`architecture-and-design.md`](architecture-and-design.md) 獨立拆分。

## Prometheus Operator 環境常見問題

**情景：** 使用 Prometheus Operator（kube-prometheus-stack）時 PrometheusRule 不生效

**診斷**：
```bash
# 檢查 PrometheusRule 是否已載入
kubectl get prometheusrules -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting

# 檢查 Prometheus 是否 reject rule
kubectl logs prometheus-kube-prometheus-stack-prometheus-0 -c prometheus | grep "rule"

# 確認 ruleSelector 匹配
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'
```

**常見原因與修正**：

1. **ruleSelector label 不匹配**
   - 原因：PrometheusRule 缺少 Prometheus CRD 所要求的 label
   - 診斷：比對 `kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'` 輸出與 PrometheusRule labels
   - 修正：確保 PrometheusRule 同時包含 `prometheus: kube-prometheus` 和 `release: kube-prometheus-stack`
   ```bash
   # 使用 operator-generate 自動產出正確 label
   da-tools operator-generate --tenant <name> --output-dir ./crds/
   # 或手動 patch 現有 CRD
   kubectl label prometheusrule <name> -n monitoring release=kube-prometheus-stack prometheus=kube-prometheus
   ```

2. **namespace 不在 Prometheus 監控範圍**
   - 原因：Prometheus CRD 的 `ruleNamespaceSelector` 未包含目標 namespace
   - 診斷：`kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleNamespaceSelector}'`
   - 修正：擴展 namespace selector 或將 PrometheusRule 部署至已納入監控的 namespace
   ```bash
   # 方案 A：將 CRD 部署到 monitoring namespace
   da-tools operator-generate --tenant <name> --namespace monitoring --output-dir ./crds/
   # 方案 B：修改 Prometheus CRD 的 ruleNamespaceSelector 納入目標 namespace
   kubectl edit prometheus -n monitoring kube-prometheus-stack-prometheus
   # 在 spec.ruleNamespaceSelector.matchLabels 加入目標 namespace label
   ```

3. **CRD API 版本不匹配**
   - 原因：叢集安裝的 Operator 版本與產出的 CRD apiVersion 不一致
   - 診斷：`kubectl api-versions | grep monitoring.coreos.com`
   - 修正：
   ```bash
   # 指定與叢集相符的 API 版本
   da-tools operator-generate --tenant <name> --api-version v1 --output-dir ./crds/
   ```

**Rollback 程序**（從 Operator 退回 ConfigMap 模式）：
```bash
# 1. 停止 Operator 管理：刪除 PrometheusRule / AlertmanagerConfig CRD
kubectl delete prometheusrule -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
# 2. 恢復 ConfigMap 模式：Helm upgrade 切換 rules.mode
helm upgrade threshold-exporter ./helm/threshold-exporter --set rules.mode=configmap
# 3. 驗證 ConfigMap rules 已生效
kubectl get configmap -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
da-tools validate-config --config-dir ./conf.d/
```

> 詳見：[Operator Prometheus 整合](operator-prometheus-integration.md) · [Operator Alertmanager 整合](operator-alertmanager-integration.md) · [Operator GitOps 部署](operator-gitops-deployment.md)

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Troubleshooting and Edge Cases"] | ⭐⭐⭐ |
| ["Grafana Dashboard 導覽"](./grafana-dashboards.md) | ⭐⭐⭐ |
| ["da-tools CLI Reference"](./cli-reference.md) | ⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.md) | ⭐⭐ |
| ["性能分析與基準測試 (Performance Analysis & Benchmarks)"](./benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](./byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](./byo-prometheus-integration.md) | ⭐⭐ |
| ["Operator Prometheus 整合"](prometheus-operator-integration.md) | ⭐⭐ |
| ["Operator Alertmanager 整合"](operator-alertmanager-integration.md) | ⭐⭐ |
| ["Operator GitOps 部署"](operator-gitops-deployment.md) | ⭐⭐ |
| ["進階場景與測試覆蓋"](internal/test-coverage-matrix.md) | ⭐⭐ |
