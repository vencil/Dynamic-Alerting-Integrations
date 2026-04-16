---
title: "ADR-017: conf.d/ 目錄分層 + 混合模式 + 遷移策略"
tags: [adr, conf.d, directory-scanner, hierarchy, migration, phase-b, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.6.0
lang: zh
---

# ADR-017: conf.d/ 目錄分層 + 混合模式 + 遷移策略

> Phase .b B-1（v2.7.0 Scale Foundation I）。

## 狀態

🟡 **Proposed**（v2.7.0 Phase .b, 2026-04-17）

## 背景

v2.6.x 的 Directory Scanner 只認識 **flat** 結構：所有 tenant YAML 放在同一個 `conf.d/` 資料夾。
當 tenant 數量來到 200+ 以上，flat 結構產生以下痛點：

1. **人類可讀性差**：200 個 YAML 檔案排在一起，查找特定 domain/region 的 tenant 需要依賴 grep
2. **PR 審查困難**：修改 defaults 影響多少 tenant 無法從目錄結構直觀看出
3. **CI blast radius 不明**：`_defaults.yaml` 變動時無法快速判斷影響範圍
4. **metadata 重複**：每個 tenant 都要手動填寫 `_metadata.domain/region/environment`，與目錄結構語意重複

Phase .a A-4 的 `generate_tenant_fixture.py` 已支援 `--hierarchical` 模式（`domain/region/env` 三層），
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
Phase .a A-4 benchmark 已證明分層結構在效能上沒有退化。

### C: 使用外部索引（DB/JSON）代替目錄結構

❌ 偏離 "config-as-code" 原則，增加部署複雜度。
Directory Scanner 的設計哲學是「檔案系統即 source of truth」。

## 影響

- **Directory Scanner**：升級為遞迴掃描 + 混合模式識別
- **generate_tenant_fixture.py**：已支援 `--hierarchical`（Phase .a A-4）
- **Prometheus metrics**：目錄深度不影響 metric label（tenant-id 仍為唯一 label key）
- **CI/CD**：`migrate-conf-d --dry-run` 可納入 PR check
- **文件**：需新增 `docs/scenarios/multi-domain-conf-layout.md`

## 相關

- [ADR-018: _defaults.yaml 繼承語意 + dual-hash hot-reload](018-defaults-yaml-inheritance-dual-hash.md)
- [benchmark-v2.7.0-baseline.md](../internal/benchmark-v2.7.0-baseline.md) — flat vs hierarchical 效能對照
- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.md)
