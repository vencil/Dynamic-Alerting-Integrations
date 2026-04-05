---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.3.0
lang: zh
---

# ADR-006: 租戶映射拓撲 (1:1, N:1, 1:N)

## 狀態

✅ **Accepted** (v2.1.0) — 工具鏈已完成，1:N 端到端整合待生產驗證

## 背景

企業環境中，資料庫實例與租戶之間的映射關係並非總是一對一。隨著平台規模擴大，我們遇到三種常見的拓撲模式：

### 拓撲分類

| 拓撲 | 說明 | 典型場景 |
|:-----|:-----|:---------|
| **1:1** | 一個 namespace/實例對應一個租戶 | 獨立部署的微服務、專用 DB |
| **N:1** | 多個 namespace/實例聚合為一個租戶 | 多叢集同租戶、DR 對、讀寫分離 |
| **1:N** | 單一實例內含多個邏輯租戶 | Oracle 多 schema、DB2 多 tablespace、共享 RDS |

### 問題陳述

目前平台的 1:1 映射是隱式的（`namespace` label = tenant ID），N:1 已透過 `scaffold_tenant.py --namespaces` 的 `relabel_configs` regex 實現。但 **1:N 拓撲**缺乏原生支援：

- 單一 DB 實例的 metrics 中僅包含 `instance` label，不含租戶維度
- Exporter 無法感知實例內部的 schema/tablespace 劃分
- 告警規則無法區分同一實例上不同租戶的閾值

需要一個通用方案將實例級指標拆分為租戶級指標，而不修改 threshold-exporter 核心邏輯。

## 決策

**在資料平面 (Data Plane) 透過 Prometheus Recording Rules 解決映射，控制平面 (Control Plane) 的 threshold-exporter 保持零變更。**

具體機制：

1. **1:1（預設）**：維持現行 `namespace` label = tenant ID 的隱式映射
2. **N:1（已實現）**：Prometheus `relabel_configs` regex 聚合多個 namespace 至單一 tenant label
3. **1:N（新增）**：Config-driven `instance_tenant_mapping` → 自動產生 Rule Pack Part 1 Recording Rules

### 1:N 實現架構

```yaml
# _instance_mapping.yaml — 新配置檔（位於 config-dir）
instance_tenant_mapping:
  oracle-prod-01:
    - tenant: db-a
      filter: 'schema=~"app_a_.*"'
    - tenant: db-b
      filter: 'schema=~"app_b_.*"'
  db2-shared-01:
    - tenant: db-c
      filter: 'tablespace="ts_client_c"'
    - tenant: db-d
      filter: 'tablespace="ts_client_d"'
```

```yaml
# 自動產生的 Recording Rule (Rule Pack Part 1: Data Normalization)
groups:
  - name: tenant_mapping_oracle-prod-01
    rules:
      - record: tenant_mapped:oracle_sessions:current
        expr: oracle_sessions{instance="oracle-prod-01", schema=~"app_a_.*"}
        labels:
          tenant: db-a
      - record: tenant_mapped:oracle_sessions:current
        expr: oracle_sessions{instance="oracle-prod-01", schema=~"app_b_.*"}
        labels:
          tenant: db-b
```

## 基本原理

### 為何選擇資料平面映射

**Exporter 零變更**：threshold-exporter 的職責是「YAML → Metrics」，不應承擔實例到租戶的映射邏輯。映射屬於資料正規化，應在 Prometheus 層級處理。

**Recording Rule 的天然優勢**：
- 產生的時間序列自帶 `tenant` label，後續的告警規則、Dashboard 查詢無需任何修改
- TSDB 同時保留原始實例指標與映射後的租戶指標，支援雙視角分析
- Recording Rule 的計算在 Prometheus 評估週期內完成，無額外延遲

**統一管理**：三種拓撲的最終表現一致 — 所有告警規則都能假設 `tenant` label 存在。

### 為何拒絕在 Exporter 中解決

- **職責越界**：Exporter 變成了「資料正規化引擎」，違反單一職責
- **重啟成本**：映射變更需重啟 exporter，影響所有租戶的指標採集
- **多 exporter 同步**：HA 部署下兩個 exporter 需一致映射，增加分佈式一致性問題

### 為何拒絕在 Alertmanager 中解決

- **太晚**：Alertmanager 只處理已觸發的告警，無法在指標層級進行拆分
- **Dashboard 盲區**：Grafana 等工具直接查詢 Prometheus，繞過 Alertmanager

## 後果

### 正面影響

✅ threshold-exporter 完全不感知拓撲複雜度，保持簡潔
✅ 三種拓撲透過統一的 `tenant` label 收斂，下游規則和 Dashboard 零修改
✅ Recording Rule 支援熱更新（configmap-reload），無需重啟 Prometheus
✅ TSDB 同時保留原始與映射後指標，支援多維度回溯分析
✅ 與現有 Rule Pack 架構（ADR-005 Projected Volume）完美整合

### 負面影響

⚠️ Recording Rule 產生額外時間序列，增加 TSDB 存儲 (~2× per mapped metric)
⚠️ 需開發 `generate_tenant_mapping_rules.py` 工具自動產生 Recording Rules
⚠️ 1:N 映射的 filter 語法需租戶理解底層 DB 的 schema/tablespace 結構

### 運維考量

- `generate_tenant_mapping_rules.py` 產出為 Rule Pack Part 1 ConfigMap，走 Projected Volume 分發
- CI 驗證：映射中引用的 tenant ID 必須在 `config-dir` 中存在對應的 tenant YAML
- Cardinality 評估：每個映射 entry 產生 M 條 recording rules（M = mapped metric 數量），需評估對 TSDB 的影響
- 建議為 1:N 租戶設定 `_cardinality_multiplier` 標記，供 capacity planning 參考

## 替代方案考量

### 方案 A：Exporter 內建映射 (已拒絕)
- 優點：單一元件處理
- 缺點：職責越界、重啟影響大、HA 一致性問題

### 方案 B：Alertmanager 層級映射 (已拒絕)
- 優點：僅影響通知路徑
- 缺點：指標層級無法拆分、Dashboard 盲區

### 方案 C：外部 Proxy (已考量)
- 優點：完全解耦
- 缺點：引入新元件、延遲增加、運維複雜度高

## v2.1.0 實作摘要

- `generate_tenant_mapping_rules.py` — 從 `_instance_mapping.yaml` 自動產生 Recording Rules，支援 Oracle/DB2/通用 filter 語法（36 tests）
- `discover_instance_mappings.py` — 自動偵測 Prometheus 中的實例拓撲（1:1/N:1/1:N），輸出建議映射配置
- `scaffold_tenant.py --topology=1:N` — Onboarding 整合（含 `--mapping-instance`, `--mapping-filter`）
- 範例配置 `conf.d/examples/_instance_mapping.yaml`
- Go/Python 雙端 reserved key 同步

## 後續方向

- 在實際多 schema Oracle 環境驗證端到端流程
- Schema validation（`_instance_mapping.yaml` JSON Schema）
- 與 Federation 場景 B 結合驗證邊緣/中央分層架構下的行為

## 相關決策

- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — Recording Rules 透過同一 Projected Volume 機制分發
- [ADR-004: Federation 場景 A 優先](./004-federation-scenario-a-first.md) — Federation 下的映射一致性
- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) — 映射後指標仍適用 inhibit dedup

## 參考資料

- [`docs/architecture-and-design.md`](../architecture-and-design.md) §2.3 — Tenant-Namespace 映射模式
- [`scaffold_tenant.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/scaffold_tenant.py) — 現行 `--namespaces` N:1 支援
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — 路由產生器（ADR-007 相關）
- [Prometheus Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — 官方文件

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐ |
| [004-federation-scenario-a-first](004-federation-scenario-a-first.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐⭐⭐ |
| [006-tenant-mapping-topologies](006-tenant-mapping-topologies.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](../architecture-and-design.md) | ⭐⭐⭐ |
| ["架構與設計 — 附錄 A"](../architecture-and-design.md#附錄-a角色與工具速查) | ⭐⭐ |
