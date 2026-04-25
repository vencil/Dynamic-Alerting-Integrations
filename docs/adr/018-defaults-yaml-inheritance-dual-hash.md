---
title: "ADR-018: _defaults.yaml 繼承語意 + dual-hash hot-reload"
tags: [adr, defaults, inheritance, hot-reload, dual-hash, phase-b, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.7.0
lang: zh
---

# ADR-018: _defaults.yaml 繼承語意 + dual-hash hot-reload

> **Language / 語言：** **中文 (Current)** | [English](./018-defaults-yaml-inheritance-dual-hash.en.md)

> Phase .b B-1（v2.7.0 Scale Foundation I）。
> 與 [ADR-017](017-conf-d-directory-hierarchy-mixed-mode.md)（目錄分層）為一組。

## 狀態

🟡 **Proposed**（v2.7.0 Phase .b, 2026-04-17）

## 背景

v2.6.x 的 `_defaults.yaml` 僅在 flat `conf.d/` 根目錄存在一份全局 defaults。
引入 ADR-017 的分層目錄後，需要定義多層 `_defaults.yaml` 的繼承語意：

- 哪些層級可以放 `_defaults.yaml`？
- 父子層 defaults 如何 merge？
- `_defaults.yaml` 變動時，哪些 tenant 需要 reload？如何避免 reload 風暴？

v2.5.0 已有 SHA-256 hot-reload（`source_hash` 比對），但只追蹤 tenant YAML 本身。
現在 tenant 的 **effective config** 同時取決於自身 YAML + 繼承的 defaults，
需要第二層 hash 來判斷「effective config 是否真的變了」。

## 決策

### 繼承層級

`_defaults.yaml` 可出現在以下任意層級（皆為選填）：

```
conf.d/
├── _defaults.yaml              ← L0: 全局 defaults
├── {domain}/
│   ├── _defaults.yaml          ← L1: domain-level defaults
│   └── {region}/
│       ├── _defaults.yaml      ← L2: region-level defaults（少見）
│       └── {env}/
│           ├── _defaults.yaml  ← L3: env-level defaults
│           └── tenant-001.yaml
```

繼承順序：**L0 → L1 → L2 → L3 → tenant YAML**（後者覆蓋前者）。

### Merge 語意：Deep Merge with Override

- **Dict/Map 欄位**：deep merge（子層新增的 key 會保留，相同 key 子層覆蓋父層）
- **Array/List 欄位**：**replace，不 concat**（避免語意歧義 — "我覆蓋了 group_by，怎麼多出舊值？"）
- **Scalar 欄位**：子層覆蓋父層
- **Null / 空值**：顯式 `null` 會刪除父層的值（opt-out pattern）
- **`_metadata` 欄位不繼承**：每個 tenant 的 `_metadata` 僅來自自身 YAML + 路徑推斷（ADR-017）

```yaml
# L0 _defaults.yaml
defaults:
  pg_stat_activity_count: 500
  pg_replication_lag_seconds: 30
  _routing:
    group_wait: 60s
    group_interval: 5m

# L1 finance/_defaults.yaml
defaults:
  pg_stat_activity_count: 200     # override: 金融 domain 更嚴格
  pg_locks_count: 100             # 新增: domain-specific

# tenant YAML
tenants:
  fin-db-001:
    pg_stat_activity_count: 150   # override: 單一 tenant 最嚴格
    # pg_replication_lag_seconds: 繼承 L0 = 30
    # pg_locks_count: 繼承 L1 = 100
    # _routing.group_wait: 繼承 L0 = 60s
```

**Effective config 計算**：
```
effective = deep_merge(L0, L1, L2, L3, tenant_yaml)
```

### Dual-Hash 機制

每個 tenant 維護兩個 hash：

| Hash | 定義 | 用途 |
|:-----|:-----|:-----|
| `source_hash` | SHA-256 of tenant YAML file bytes | 判斷 tenant 原始檔案是否變動 |
| `merged_hash` | SHA-256 of effective config (merge 後的 canonical JSON) | 判斷最終生效設定是否變動 |

**Reload 判斷邏輯**：

```
if source_hash changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        trigger reload  ← 真正的 alerting config 變了
    else:
        increment da_config_defaults_change_noop_total  ← defaults 改了但此 tenant 不受影響
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        trigger reload
    else:
        increment da_config_defaults_change_noop_total
```

### 繼承圖資料結構

Scanner 維護一個 **inheritance graph**：

```go
type InheritanceGraph struct {
    // _defaults.yaml 路徑 → 受影響的 tenant ID 清單
    DefaultsToTenants map[string][]string
    // tenant ID → 其繼承鏈上的 _defaults.yaml 路徑（ordered, L0→L3）
    TenantDefaults    map[string][]string
}
```

`_defaults.yaml` 變動時，透過 `DefaultsToTenants` 快速查出需要重算 `merged_hash` 的 tenant 清單，
避免全量重算。

### Watch 機制：維持 Periodic Scan

- **不採用 inotify/fsnotify**：container mount 事件遺失 + kernel watch 上限
- 維持既有 periodic scan（可設定 interval，default 30s）
- 掃描只重算 `stat()` 變動的檔案 → 避免 O(n) hash 計算

### Debounce

- `git pull` 落地 50 檔案時，每個 `stat()` 變動不立即觸發 reload
- Debounce window: **300ms**（可設定，`--scan-debounce` flag）
- Window 內累積所有變動 → 一次性 batch recompute → 一次性 reload
- 避免 reload 風暴（50 個 tenant 各 reload 一次 → 變成只 reload 一次）

### Cardinality Guard

- `_defaults.yaml` **本身不產生 Prometheus metric series**
- 繼承欄位仍遵循既有 Cardinality Guard 規則（v2.5.0 ADR-005）
- `merged_hash` label 不暴露在 metrics（防 label 爆炸）

### 新增 Prometheus Metrics

| Metric | Type | Labels | Description |
|:-------|:-----|:-------|:------------|
| `da_config_scan_duration_seconds` | histogram | — | 單次 periodic scan 耗時 |
| `da_config_reload_trigger_total` | counter | `reason` | reload 原因：source / defaults / new / delete |
| `da_config_defaults_change_noop_total` | counter | — | merged_hash 不變時跳過 reload 的次數 — **v2.8.0 起語義收窄為 cosmetic-only**（見 §Amendment 2026-04-25） |
| `da_config_defaults_shadowed_total` | counter | — | **v2.8.0 (Issue #61)** — defaults 變動但被 tenant override 擋下的次數（從 `da_config_defaults_change_noop_total` 拆出） |
| `da_config_blast_radius_tenants_affected` | histogram | `reason / scope / effect` | **v2.8.0 (Issue #61)** — 每 tick 受影響 tenant 數的分佈 |

### Amendment 2026-04-25 (Issue #61): noop 語義拆分

原 §Reload 判斷邏輯把「comment-only edit」與「override-shadowed edit」都記為 `da_config_defaults_change_noop_total`，使 ops 無法區分「真的沒事」vs「繼承機制擋下變動」。v2.8.0 後拆為兩個 effect：

```
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        trigger reload
        emit blast_radius{effect="applied"}
    else:
        # 進一步拆分（Issue #61）
        compute changedKeys = diff(prior_parsed_defaults, new_parsed_defaults)
        if len(changedKeys) == 0:
            # 純 cosmetic：comment-only / reordering / whitespace
            increment da_config_defaults_change_noop_total
            emit blast_radius{effect="cosmetic"}
        elif tenantOverridesAll(tenant_src, changedKeys):
            # Shadowed：tenant 覆寫了所有變動的 key
            increment da_config_defaults_shadowed_total
            emit blast_radius{effect="shadowed"}
        else:
            # 邏輯上不可達（merged_hash 應已移動）
            # — 防禦性 fallback 至 cosmetic
            increment da_config_defaults_change_noop_total
```

實作要點：
- `m.parsedDefaults` 新增的 ConfigManager 欄位，與 `hierarchyHashes` 同 atomic-swap，存放每個 `_defaults.yaml` 的 normalized parsed dict（`map[string]any`），記憶體 ~1MB / 1000 tenants
- 在 `populateHierarchyState` cold-start 時 eager-parse 全部 defaults；`diffAndReload` 時只重新 parse 有 hash 變動的檔案，未變動的沿用前值
- 詳見 `components/threshold-exporter/app/config_defaults_diff.go` + Issue #61 RFC

## 考量的替代方案

### A: Single-Hash（僅 source_hash）

❌ `_defaults.yaml` 變動時無法判斷哪些 tenant 真正受影響，
只能全量 reload。1000+ tenant 環境下 reload 風暴不可接受。

### B: fsnotify / inotify

❌ 在 container mount（NFS/FUSE/projected volume）環境下事件遺失是已知問題。
kernel watch 限制（default 8192）在千租戶環境會被用盡。
v2.5.0 已驗證 periodic scan 在 2000 tenant 下 < 200ms（Phase .a baseline 確認）。

### C: Array Concat（而非 Replace）

❌ `group_by: [severity]`（L0）+ `group_by: [alertname]`（L1）
→ concat 結果 `[severity, alertname]` 語意不明確。
用戶預期「我覆蓋了 group_by」而非「我追加了」。
Replace 語意更直覺，且與 Helm values merge 行為一致。

## 影響

- **Directory Scanner Go 程式碼**：新增 inheritance graph + dual-hash + debounce
- **CLI**：新增 `describe-tenant` 可展開 effective config + 顯示繼承來源
- **Tenant API**：新增 `GET /api/v1/tenants/{id}/effective` endpoint
- **Schema**：`tenant-config.schema.json` 升級支援 `_defaults.yaml` 結構
- **Benchmark**：千租戶 + 多層繼承的 scan 效能需對照 Phase .a baseline

## 相關

- [ADR-017: conf.d/ 目錄分層 + 混合模式](017-conf-d-directory-hierarchy-mixed-mode.md)
- [Benchmark Report §12 «Incremental Hot-Reload + B-1 Scale Gate»](../benchmarks.md#12-incremental-hot-reload-b-1-scale-gate) — dual-hash 1000-tenant 實測 + SLO 判讀
- [architecture-and-design.md §設計概念](../architecture-and-design.md#設計概念總覽)
