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

`tenant_metadata_info` 一樣由每個副本各送一份。平台的 Prometheus 以 pod 為單位抓取（raw 設定的 `platform-components` job 走 `role: endpointslice`，Prometheus Operator 的 ServiceMonitor 也是以 endpoints 探索，一個 pod 一個 target），同一個租戶會有兩份只差 `instance`／`pod` 的序列。告警把 metadata 接進來的那個 join 若直接用原始指標：

```promql
* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info
```

右側每個租戶就有兩筆，Prometheus 報 `many-to-many matching not allowed`，**整條告警停止評估**（不是少幾個 label，而是不觸發）。右側要先收斂成每個租戶一筆：

```promql
* on(tenant) group_left(runbook_url, owner, tier)
  topk by(tenant) (1, max by(tenant, runbook_url, owner, tier) (tenant_metadata_info))
```

- 內層 `max by(...)` 收掉兩個副本送出的相同 metadata。
- 外層 `topk by(tenant) (1, …)` 處理換版空窗：兩個副本各自 hot-reload、有時間差（見下方已知限制），空窗內某租戶的 `owner`／`runbook_url`／`tier` 若改了，兩個副本會暫時送出不同值，`max by(...)` 收斂後該租戶仍有兩筆。Prometheus 對右側重複 match group 的檢查是整個運算式一起失敗、不分租戶，少了這層，那幾個評估週期內帶這個 join 的規則會對**所有租戶**評估失敗——一個租戶改 metadata，所有租戶的告警一起停。

- 用 `by` 列出 exporter 送出的 label，而不是用 `without` 列抓取加上的 label：抓取設定可能再加上每個副本不同的 label（例如 relabel 出來的 `node`），`without` 漏列一個就又撞 many-to-many。`by` 清單由測試對照 `collector.go` 的 label 清單，exporter 新增 label 時測試會紅，不會悄悄被收掉。
- `check_leftouterjoin_enrichment.py` 檢查每個以 `on(...)`／`ignoring(...)` 比對、右側運算元用到 `tenant_metadata_info` 的運算（不論 `group_left` 列了哪些 label）：右側必須就是這個寫法（忽略空白與大小寫），只有 `max by(...)`、沒有外層 `topk` 的寫法也擋；`and`／`or`／`unless` 只看有沒有，不在此限；`tests/rulepacks/rule-pack-metadata-replicas_test.yaml` 以兩份副本序列驗證每個 pack 的代表告警，其中一個租戶的兩個副本送出不同的 `runbook_url`，另一個租戶的告警必須照常觸發。
- void 分支的 `unless on(tenant) tenant_metadata_info` 維持原始指標：`unless` 只看有沒有，右側重複不會出錯。

**已知限制：換版空窗內，該租戶的 metadata 可能是舊的。** 空窗可長達一兩分鐘：每個副本每隔 `reloadInterval`（helm/threshold-exporter 預設 30s）才檢查一次設定，而 ConfigMap 掛載內容的更新由各副本所在節點的 kubelet 各自同步（通常數十秒，可到一分鐘以上），兩段延遲疊加，兩個副本換到新版的時間點可能差上這麼久。空窗只影響 metadata 改了的那個租戶：這段期間它的告警帶的是其中一個副本的 metadata，可能還是舊的 `runbook_url`／`owner`／`tier`。所有值都是 1，`topk` 平手時保留先看到的那一筆；兩筆都在的期間序列順序不變，所以不會每次評估來回跳（promtool 在空窗內多個時間點量到同一筆）。兩個副本都換到新版後只剩新值；告警 label 從舊值換到新值時，告警身分跟著換（舊的 resolve、新的重新走 `for`），這和單一副本改 metadata 時一樣。其他租戶不受影響。

### 4.4 自監控 (Platform Rule Pack)

4 個專用警報監控 threshold-exporter 本身：

| 警報 | 條件 | 動作 |
|------|------|------|
| ThresholdExporterDown | `up{job="threshold-exporter"} == 0` for 2m | PageDuty → SRE |
| ThresholdExporterAbsent | Metrics absent > 5m | 警告 → 平台團隊 |
| ThresholdExporterTooFewReplicas | `count(up{job="threshold-exporter"}) < 2` | 警告 → SRE |
| ThresholdExporterHighRestarts | `rate(container_last_terminated_reason[5m]) > 0.1` | 調查 |
