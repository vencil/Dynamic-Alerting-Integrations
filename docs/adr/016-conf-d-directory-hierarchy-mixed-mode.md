---
title: "ADR-016: conf.d/ 目錄分層 + 混合模式 + 遷移策略"
tags: [adr, conf.d, directory-scanner, hierarchy, migration, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-016
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-04-18
updated_at: 2026-05-13
---
# ADR-016: conf.d/ 目錄分層 + 混合模式 + 遷移策略

> **Language / 語言：** **中文 (Current)** | [English](./016-conf-d-directory-hierarchy-mixed-mode.en.md)

> v2.7.0 Scale Foundation 第一塊。與 [ADR-017](017-defaults-yaml-inheritance-dual-hash.md)（繼承語意）為一組。

## 狀態

✅ **Accepted**（v2.7.0, 2026-04-19）— Directory Scanner 混合模式 + `migrate-conf-d` CLI 已隨 v2.7.0 出貨。

## 背景

v2.6.x 的 Directory Scanner 只認識 **flat** 結構：所有 tenant YAML 放在同一個 `conf.d/` 資料夾。
當 tenant 數量來到 200+ 以上，flat 結構產生以下痛點：

1. **人類可讀性差**：200 個 YAML 檔案排在一起，查找特定 domain/region 的 tenant 需要依賴 grep
2. **PR 審查困難**：修改 defaults 影響多少 tenant 無法從目錄結構直觀看出
3. **CI blast radius 不明**：`_defaults.yaml` 變動時無法快速判斷影響範圍
4. **metadata 重複**：每個 tenant 都要手動填寫 `_metadata.domain/region/environment`，與目錄結構語意重複

v2.7.0 規劃期 `generate_tenant_fixture.py` 已支援 `--hierarchical` 模式（`domain/region/env` 三層），
驗證了千租戶分層結構的可行性。本 ADR 正式定義 Directory Scanner 如何支援此結構。

## 決策

### 採用混合模式（Mixed Mode）

Directory Scanner 同時支援 flat 和分層結構，**不強制遷移**。

```
conf.d/
├── legacy-tenant-a.yaml          ← flat（向下相容）
├── legacy-tenant-b.yaml
├── _defaults.yaml                ← 全局 defaults（可選）
├── finance/                      ← domain 層
│   ├── _defaults.yaml            ← domain-level defaults
│   ├── us-east/                  ← region 層
│   │   ├── prod/                 ← environment 層
│   │   │   ├── _defaults.yaml   ← env-level defaults
│   │   │   ├── fin-db-001.yaml
│   │   │   └── fin-db-002.yaml
│   │   └── staging/
│   │       └── fin-db-003.yaml
│   └── eu-central/
│       └── prod/
│           └── fin-db-004.yaml
└── logistics/
    └── ap-northeast/
        └── prod/
            └── log-db-001.yaml
```

### 目錄層次：domain → region → env（建議，非強制）

- 層次深度 **0-3 層皆合法**（flat = 0 層）
- 建議命名：`{domain}/{region}/{env}/` — 與 `_metadata` 欄位對齊
- Scanner 不校驗目錄名 vs `_metadata` 對應（僅產生 warning 級 log）
- 超過 3 層的子目錄也會被掃描（未來擴展空間），但 `_defaults.yaml` 繼承只認 domain/region/env 三層

### 目錄路徑產生 metadata 預設值

- 若 tenant YAML 缺少 `_metadata.domain`，Scanner 從父目錄路徑推斷（第 1 層 = domain, 第 2 層 = region, 第 3 層 = env）
- `_metadata` 欄位明確設定時 **優先於路徑推斷**（explicit override）
- 路徑推斷值 ≠ `_metadata` 值時產生 **warning log**（不阻擋啟動）

### 遷移策略

1. **零中斷升級**：v2.7.0 Scanner 直接相容 v2.6.x flat 結構，不需任何改動
2. **`migrate-conf-d` 工具為可選**：提供 `--dry-run` 和 `--apply` 模式
3. **使用 `git mv` 保留歷史**：遷移工具生成 git mv 指令，不直接 mv
4. **`--infer-from metadata`**：根據 `_metadata.domain/region/environment` 推斷目標目錄
5. **不處理 `_metadata` 缺失的檔案**：skip 並提示人類決定

### 掃描行為

- Scanner 啟動時遞迴掃描 `conf.d/` 及所有子目錄
- `_defaults.yaml` 不視為 tenant 設定（不產生 metric）
- 以 `.yaml` / `.yml` 結尾且不以 `_` 開頭的檔案視為 tenant config
- 以 `_` 開頭的檔案為系統檔（`_defaults.yaml`, `_metadata.yaml` 等）

## 考量的替代方案

### A: 強制遷移至分層結構

❌ 破壞向下相容，強迫所有既有用戶在升級 v2.7.0 時一次性重整 conf.d/。
對於只有 10-20 tenant 的小型部署是不必要的負擔。

### B: 僅支援 flat（現狀）

❌ 無法解決 200+ tenant 的可讀性和 blast radius 問題。
v2.7.0 規劃期 benchmark 已證明分層結構在效能上沒有退化。

### C: 使用外部索引（DB/JSON）代替目錄結構

❌ 偏離 "config-as-code" 原則，增加部署複雜度。
Directory Scanner 的設計哲學是「檔案系統即 source of truth」。

## 影響

- **Directory Scanner**：升級為遞迴掃描 + 混合模式識別
- **generate_tenant_fixture.py**：支援 `--hierarchical` 千租戶 fixture 產生
- **Prometheus metrics**：目錄深度不影響 metric label（tenant-id 仍為唯一 label key）
- **CI/CD**：`migrate-conf-d --dry-run` 可納入 PR check
- **文件**：新增 `docs/scenarios/multi-domain-conf-layout.md`

### ⛔ 支援面邊界：階層布局只有 threshold-exporter 完整實作（#1339，2026-08-04 補記）

本 ADR 的「遞迴掃描」只落在 **threshold-exporter**（`pkg/config/hierarchy.go` 的
`filepath.WalkDir`）。**Python 工具鏈當年沒有跟上**：以 AST 量測，**11 支工具平面
列舉租戶設定目錄、6 支遞迴**——同一個階層目錄，`describe_tenant.py` 看得到租戶，
`validate_config.py` 卻回報 `Result: PASS` / exit 0 而掃到 **0 個租戶**。那不是擋下來，
是**對一個從未讀過的目錄發綠燈**。

現況（#1339 修正後）：

| 面 | 階層 `conf.d/` |
|:--|:--|
| threshold-exporter `/effective`（繼承解析） | ✅ 完整遞迴繼承 |
| threshold-exporter `/metrics`（Prometheus 輸出） | ⛔ **仍是平面**，見下方 #1521 |
| `validate_config.py` | ✅ 已改為遞迴 |
| 路由生成器 / 其餘平面工具 | ⚠️ **仍是平面**，但會列出被跳過的檔案並指回本節 |

⇒ **路由面尚未支援階層布局**：`_routing_defaults` 與租戶本體的 `_routing` 在子目錄裡
不會被任何元件消費。要用路由就把租戶檔放在 `conf.d/` 頂層。

這個「平面但出聲」的契約由 `tests/shared/test_confd_enumeration_contract.py` 強制：
新工具若平面讀取又不出聲會被擋下來，**選擇必須是刻意的**。共用列舉層在
`scripts/tools/_lib_confd.py`。

### ⛔ 已知缺陷：巢狀租戶 `/effective` 解析得到，`/metrics` 卻沒有 series（#1521，2026-08-22 補記）

上面「影響」清單裡的 **「Prometheus metrics：目錄深度不影響 metric label」目前是假的**。
threshold-exporter **自己內部**就有兩個對 `conf.d/` 的掃描器，對「哪些檔存在」的答案不同：

| 掃描器 | 列舉方式 | 出海口 |
|:--|:--|:--|
| `config_hierarchy.go` `scanDirHierarchical` | `filepath.WalkDir` — **遞迴，每一層** | `hierarchy.tenantSources` → `Resolve()` → `/effective` |
| `flat_scanner.go` `scanDirFileHashes` | `os.ReadDir` + `if IsDir() { continue }` — **只有頂層** | `m.config` → `ThresholdCollector` → `/metrics` |

⇒ 放在子目錄的租戶檔（`conf.d/db/hier-tenant.yaml`）**`/effective` 查得到、`/metrics` 一條 series 都沒有**，
所以它的告警**永遠不會 fire**。#1526 之前是**零信號**：沒有 ERROR、沒有 WARN、沒有 parse_failure，
連 "Config loaded" 統計行都看不出來。

現況（#1526 止血後）：新增 gauge `da_config_hierarchy_divergent_tenants` + 一行 ERROR 會**點名**受害租戶——
但**沒有修好**，那些租戶仍然不產生指標。⚠️ 純平面模式下 gauge 可能維持 `0` 直到重啟，
**別把 0 讀成「已完整檢查」**（兩個成因與釘住它的回歸測試見
[threshold-exporter README](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md)）。

⇒ 在 #1521 關閉前：**要指標，租戶檔就得放在 `conf.d/` 頂層**。
關閉 #1521 的那支 PR 同時負責移除本節與還原上面的表格列。

## 相關

- [ADR-017: _defaults.yaml 繼承語意 + dual-hash hot-reload](017-defaults-yaml-inheritance-dual-hash.md)
- [Benchmark Playbook §Synthetic Fixture Generation](../internal/benchmark-playbook.md#synthetic-fixture-generation-速率對照) — flat vs hierarchical 效能對照
- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.md)
