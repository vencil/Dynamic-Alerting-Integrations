---
title: "高可用性 (HA) 設計 — 副本、PDB、防雙倍計算"
tags: [architecture, high-availability, design]
audience: [platform-engineer, devops]
version: v2.9.0
lang: zh
parent: architecture-and-design.md
---
# 高可用性 (HA) 設計

> **Language / 語言：** **中文 (Current)** | [English](./high-availability.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [返回主文件](../architecture-and-design.md)

## 4. 高可用性設計 (High Availability)

### 4.1 部署策略

```yaml
replicas: 2
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0    # 零停機滾動更新
    maxSurge: 1

affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
```

**特性：**
- 2 個副本分散在不同節點
- 滾動更新時，總有 1 個副本可用
- Kind 單節點叢集：軟親和性允許裝箱

### 4.2 Pod 中斷預算 (PodDisruptionBudget)

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: threshold-exporter-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: threshold-exporter
```

**效果：** drain 節點這類自願中斷時，至少留 1 個被選中的 Pod 可用（PDB 不保證 Prometheus 抓得到它）。PDB 只在 `replicaCount` 大於 1 時建立（`helm/threshold-exporter/templates/pdb.yaml`），也擋不了節點故障這類非自願中斷。

### 4.3 臨界：`max by(tenant)` vs `sum`

#### ❌ 錯誤：使用 `sum`
```yaml
- record: tenant:alert_threshold:mysql_connection_usage_percent
  expr: |
    sum by(tenant)
      user_threshold{tenant=~".*", component="mysql", metric="connection_usage_percent"}
```

**問題：**
- Prometheus 從兩個副本抓取相同指標 → 雙倍值
- `sum by(tenant)` 將兩個副本的值相加 → **閾值翻倍**
- 警報觸發錯誤

#### ✓ 正確：使用 `max`
```yaml
- record: tenant:alert_threshold:mysql_connection_usage_percent
  expr: |
    max by(tenant)
      user_threshold{tenant=~".*", component="mysql", metric="connection_usage_percent"}
```

**優勢：**
- 取兩個副本中的最大值（邏輯上相同）
- 避免雙倍計算
- HA 下警報閾值準確

#### `tenant_metadata_info`：不 join，改在 annotation 查詢

`tenant_metadata_info{tenant, runbook_url, owner, tier}` 一樣由每個副本各送一份。平台的 Prometheus 以 pod 為單位抓取（raw 設定的 `platform-components` job 走 `role: endpointslice`，Prometheus Operator 的 ServiceMonitor 也是以 endpoints 探索，一個 pod 一個 target），同一個租戶會有兩份只差 `instance`／`pod` 的序列。

規則**不 join** 這個指標，告警也不帶 runbook_url／owner／tier label。需要顯示 metadata 的 annotation 用同一個查詢樣板讀出來（owner、tier 只換最後的欄位名）：

```yaml
annotations:
  runbook_url: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.runbook_url }}{{ end }}{{ end }}"
```

- `topk(1, …)` 在兩個副本各送一份時只取一筆；租戶沒有 metadata 時查詢結果為空、`with` 不展開，annotation 是空字串，告警照常觸發。
- 為什麼不 join：join 會讓 metadata 成為告警身分的一部分。原始 join 在多副本下撞 many-to-many、整條規則停止評估；把右側收斂成每個租戶一筆可以解決這件事，但收斂挑到哪一筆在換版空窗內會變（見下方已知限制），告警 label 一變身分就變、`for:` 重新計時。VictoriaMetrics 的 `topk` 遇到同值的序列時每次評估可能挑不同的那筆，該租戶在空窗內就不會觸發。
- `check_leftouterjoin_enrichment.py` 擋下任何 join `tenant_metadata_info` 的運算式（`tenant_metadata_info` 只能出現在 `and`／`unless` 的右側；`or` 會把右側序列連同 label 帶出來，也擋），也擋下 `$labels.runbook_url`／`owner`／`tier` 與任何不是這個樣板的 metadata 查詢。`tests/rulepacks/rule-pack-metadata-replicas_test.yaml` 以兩份副本序列、其中一個租戶的兩個副本送出不同的 `runbook_url`，在 promtool 與 vmalert-tool 上都驗證每個租戶的告警恰好一則、不帶 metadata label、annotation 帶出 metadata。

**已知限制：換版空窗內，annotation 可能顯示任一副本的 metadata。** 空窗可長達一兩分鐘：每個副本每隔 `reloadInterval`（helm/threshold-exporter 預設 30s）才檢查一次設定，而 ConfigMap 掛載內容的更新由各副本所在節點的 kubelet 各自同步（通常數十秒，可到一分鐘以上），兩段延遲疊加，兩個副本換到新版的時間點可能差上這麼久。空窗只影響 metadata 改了的那個租戶：它的通知可能顯示舊的或新的 `runbook_url`／`owner`／`tier`。告警身分與觸發時機不受影響，其他租戶也不受影響。

### 4.4 自監控 (Platform Rule Pack)

4 個專用警報監控 threshold-exporter 本身：

| 警報 | 條件 | 動作 |
|------|------|------|
| ThresholdExporterDown | `up{job="threshold-exporter"} == 0` for 2m | PageDuty → SRE |
| ThresholdExporterAbsent | Metrics absent > 5m | 警告 → 平台團隊 |
| ThresholdExporterTooFewReplicas | `count(up{job="threshold-exporter"}) < 2` | 警告 → SRE |
| ThresholdExporterHighRestarts | `rate(container_last_terminated_reason[5m]) > 0.1` | 調查 |
