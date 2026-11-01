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
- record: tenant:alert_threshold:mysql_connections
  expr: |
    sum by(tenant)
      user_threshold{tenant=~".*", metric="connections"}
```

**問題：**
- Prometheus 從兩個副本抓取相同指標 → 雙倍值
- `sum by(tenant)` 將兩個副本的值相加 → **閾值翻倍**
- 警報觸發錯誤

#### ✓ 正確：使用 `max`
```yaml
- record: tenant:alert_threshold:mysql_connections
  expr: |
    max by(tenant)
      user_threshold{tenant=~".*", metric="connections"}
```

**優勢：**
- 取兩個副本中的最大值（邏輯上相同）
- 避免雙倍計算
- HA 下警報閾值準確

#### 同一條規則也適用 `tenant_metadata_info` 的 join

`tenant_metadata_info` 一樣由每個副本各送一份。以 pod 為單位抓取時（Prometheus Operator 的 ServiceMonitor 走 endpoints，一個 pod 一個 target），同一個租戶會有兩份只差 `instance`／`pod` 的序列。告警把 metadata 接進來的那個 join 若直接用原始指標：

```promql
* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info
```

右側每個租戶就有兩筆，Prometheus 報 `many-to-many matching not allowed`，**整條告警停止評估**（不是少幾個 label，而是不觸發）。右側要先收斂：

```promql
* on(tenant) group_left(runbook_url, owner, tier)
  max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)
```

- 用 `by` 列出 exporter 送出的 label，而不是用 `without` 列抓取加上的 label：抓取設定可能再加上每個副本不同的 label（例如 relabel 出來的 `node`），`without` 漏列一個就又撞 many-to-many。`by` 清單由測試對照 `collector.go` 的 label 清單，exporter 新增 label 時測試會紅，不會悄悄被收掉。
- `check_leftouterjoin_enrichment.py` 擋下右側不是這個寫法的 join；`tests/rulepacks/rule-pack-metadata-replicas_test.yaml` 以兩份副本序列驗證每個 pack 的代表告警。
- void 分支的 `unless on(tenant) tenant_metadata_info` 維持原始指標：`unless` 只看有沒有，右側重複不會出錯。

**已知限制：換版空窗。** 兩個副本各自 hot-reload，換版有時間差。空窗內若某租戶的 `owner`／`runbook_url`／`tier` 改了，兩個副本會暫時送出不同值，`max by(...)` 收斂後該租戶仍有兩筆。Prometheus 對右側重複 match group 的檢查是整個運算式一起失敗、不分租戶，所以那幾個評估週期內，帶這個 join 的規則對**所有租戶**都評估失敗，void 分支也一樣（`prometheus_rule_evaluation_failures_total` 會增加，這些告警既不會新觸發、也不會重送）。兩個副本都換到新版後自動恢復。目前只有逐 pod 抓取（Prometheus Operator）會遇到；raw 設定每個 Service 只抓一個 target。這裡不處理：用 `topk` 任選一筆會讓 label 在兩個值之間跳動，改變告警身分。raw 設定改成逐 pod 發現之前，必須先定案。

### 4.4 自監控 (Platform Rule Pack)

4 個專用警報監控 threshold-exporter 本身：

| 警報 | 條件 | 動作 |
|------|------|------|
| ThresholdExporterDown | `up{job="threshold-exporter"} == 0` for 2m | PageDuty → SRE |
| ThresholdExporterAbsent | Metrics absent > 5m | 警告 → 平台團隊 |
| ThresholdExporterTooFewReplicas | `count(up{job="threshold-exporter"}) < 2` | 警告 → SRE |
| ThresholdExporterHighRestarts | `rate(container_last_terminated_reason[5m]) > 0.1` | 調查 |
