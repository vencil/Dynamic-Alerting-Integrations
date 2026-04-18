---
title: "場景：千租戶規模管理"
tags: [scenario, scale, blast-radius, search, management]
audience: [platform-engineer, operator, devops]
version: v2.7.0
lang: zh
---
# 場景：千租戶規模管理

> **v2.7.0** | 相關文件：[multi-domain-conf-layout](multi-domain-conf-layout.md)、[ADR-017](../adr/017-conf-d-directory-hierarchy-mixed-mode.md)、[ADR-018](../adr/018-defaults-yaml-inheritance-dual-hash.md)、[tenant-lifecycle](tenant-lifecycle.md)

## 概述

當平台規模從數十租戶成長到數百甚至上千租戶時，原本在小規模下足夠的操作方式會遇到效率瓶頸。本文件描述在 Dynamic Alerting 平台中，如何利用 v2.7.0 引入的工具鏈有效管理大規模租戶環境：

- **Blast Radius 預估**：變更 `_defaults.yaml` 前了解影響範圍
- **批次查詢與篩選**：快速定位特定域名、區域或環境的租戶
- **繼承鏈追蹤**：確認每個租戶的有效配置來源
- **安全變更流程**：PR → Blast Radius CI Bot → Review → Merge

## 前置條件

- 已完成 `conf.d/` 階層式結構遷移（參考 [multi-domain-conf-layout](multi-domain-conf-layout.md)）或至少部分域名使用階層式結構（混合模式）
- 已安裝工具：`describe-tenant`、`blast_radius.py`、`migrate-conf-d`
- GitHub Actions 已啟用 `blast-radius.yml` workflow

## 情景 1：變更域預設值前評估 Blast Radius

### 問題

你需要將 Finance 域所有租戶的 `MariaDBHighConnections` 閾值從 90 調高到 95。在有 200+ Finance 租戶的環境下，你想在修改前確認：

1. 有多少租戶會受影響？
2. 哪些租戶已自行覆蓋此閾值（不受影響）？
3. 變更是否會觸發路由或 receiver 變動？

### 步驟

#### A. 產出當前有效配置快照

```bash
da-tools describe-tenant --all \
  --conf-d conf.d/ \
  --output /tmp/before.json
```

#### B. 修改域預設值

```yaml
# conf.d/finance/_defaults.yaml
tenants:
  "_defaults":
    alerts:
      threshold:
        MariaDBHighConnections: 95    # 從 90 調高到 95
```

#### C. 產出修改後有效配置快照

```bash
da-tools describe-tenant --all \
  --conf-d conf.d/ \
  --output /tmp/after.json
```

#### D. 執行 Blast Radius 分析

```bash
python3 scripts/tools/ops/blast_radius.py \
  --base /tmp/before.json \
  --pr /tmp/after.json \
  --format markdown \
  --changed-files "finance/_defaults.yaml"
```

輸出範例：

```
### Blast Radius: this PR modifies `finance/_defaults.yaml`

| Metric | Count |
|--------|-------|
| Total tenants scanned | 500 |
| Affected tenants | 187 |
| Tier A (threshold/routing) | 187 |

<details>
<summary>Substantive changes: 187 tenants</summary>

- **tenant-fin-001**
  - `alerts.threshold.MariaDBHighConnections`: 90 → 95
- **tenant-fin-002**
  - `alerts.threshold.MariaDBHighConnections`: 90 → 95
...
</details>
```

注意：已自行覆蓋 `MariaDBHighConnections` 的租戶（例如設為 98）不會出現在影響清單中。

### E. 確認後提交 PR

Blast Radius CI Bot 會在 PR 上自動發布報告，reviewer 可在 merge 前確認影響範圍。

## 情景 2：追蹤單一租戶的配置來源

### 問題

租戶 `tenant-fin-042` 的 `DiskUsageHigh` 告警不斷觸發。你想確認這個閾值來自哪一層，才能在正確的位置修改。

### 步驟

```bash
da-tools describe-tenant tenant-fin-042 --show-sources --conf-d conf.d/
```

輸出範例：

```
tenant-fin-042 (finance/us-east/prod/tenant-fin-042.yaml)
═════════════════════════════════════════════════════════
Configuration sources (order of merge):
  1. conf.d/_defaults.yaml (global)
  2. conf.d/finance/_defaults.yaml (domain: finance)
  3. conf.d/finance/us-east/_defaults.yaml (region: us-east)
  4. conf.d/finance/us-east/prod/tenant-fin-042.yaml (tenant-specific)

Effective configuration:
  alerts.threshold.DiskUsageHigh: 85 (from: domain)
  alerts.threshold.MariaDBHighConnections: 90 (from: domain)
  receivers[0].type: slack (from: global)
  timezone: America/New_York (from: region)
```

從輸出可知 `DiskUsageHigh: 85` 來自 **domain 層**（`finance/_defaults.yaml`）。如果只想為這個租戶調整，在 tenant 檔案中覆蓋即可：

```yaml
# conf.d/finance/us-east/prod/tenant-fin-042.yaml
tenants:
  tenant-fin-042:
    alerts:
      threshold:
        DiskUsageHigh: 92    # 只對此租戶提高閾值
```

## 情景 3：比較兩個租戶的配置差異

### 問題

`tenant-fin-001`（US-East）和 `tenant-fin-080`（EU-West）的告警行為不同。你想了解兩者的有效配置差異。

### 步驟

```bash
da-tools describe-tenant tenant-fin-001 --diff tenant-fin-080 --conf-d conf.d/
```

輸出範例：

```json
{
  "tenant_a": "tenant-fin-001",
  "tenant_b": "tenant-fin-080",
  "only_in_tenant-fin-001": {
    "timezone": "America/New_York"
  },
  "only_in_tenant-fin-080": {
    "_signature": {"mode": "gdpr-compatible"},
    "timezone": "Europe/Dublin"
  },
  "different": {
    "_encryption.enabled": {"a": false, "b": true}
  }
}
```

差異來自 region 層預設值（US-East vs EU-West）。

## 情景 4：CI 自動化 — Blast Radius Bot 工作流程

### 觸發條件

GitHub Actions workflow `blast-radius.yml` 在 PR 修改 `conf.d/**` 路徑時自動觸發。

### 流程

```
PR 提交 → CI 觸發 blast-radius.yml
  ├── 1. checkout base + PR
  ├── 2. 各自執行 describe-tenant --all
  ├── 3. blast_radius.py 比對 + 分類
  ├── 4. 發布 PR comment（含 Tier A/B/C 摘要）
  └── 5. 上傳 JSON report artifact（供審計用）
```

### PR Comment 範例

```
### Blast Radius: this PR modifies `finance/_defaults.yaml`

| Metric | Count |
|--------|-------|
| Total tenants scanned | 500 |
| Affected tenants | 347 |
| Tier A (threshold/routing) | 12 |
| Tier B (other alerting) | 0 |
| Tier C (format-only) | 335 |

<details>
<summary>Substantive changes: 12 tenants</summary>
- **tenant-fin-001**: `alerts.threshold.MariaDBHighConnections`: 90 → 95
- **tenant-fin-002**: `alerts.threshold.MariaDBHighConnections`: 90 → 95
...
</details>

Format-only changes: 335 tenants (no threshold/routing/alerting impact)
```

### Tier 分類邏輯

| Tier | 定義 | PR Comment 行為 |
|------|------|----------------|
| **A** | 閾值數值變動、routing receiver 變動 | 高亮，展開細節 |
| **B** | 其他 alerting 欄位變動（severity、rules 等） | 列表 |
| **C** | 純格式 / metadata / timezone 等非告警欄位 | 僅計數，不展開 |

## 情景 5：大規模遷移後驗證

### 問題

你剛將 200 個 Finance 租戶從平面結構遷移到階層式結構，需要驗證遷移前後每個租戶的有效配置沒有變化。

### 步驟

```bash
# 1. 遷移前快照
da-tools describe-tenant --all --conf-d conf.d/ --output /tmp/pre-migration.json

# 2. 執行遷移
da-tools migrate-conf-d --apply \
  --conf-d conf.d/ \
  --infer-from metadata

# 3. 遷移後快照
da-tools describe-tenant --all --conf-d conf.d/ --output /tmp/post-migration.json

# 4. 比對：應該 0 個受影響租戶
python3 scripts/tools/ops/blast_radius.py \
  --base /tmp/pre-migration.json \
  --pr /tmp/post-migration.json \
  --format json
```

預期結果：`"affected_tenants": 0`。如果有非零結果，表示遷移過程中有配置語義發生變化，需要逐一排查。

## 工具速查

| 工具 | 用途 | 典型用法 |
|------|------|---------|
| `describe-tenant <id>` | 查看單一租戶有效配置 | `da-tools describe-tenant tenant-a --show-sources` |
| `describe-tenant --all` | 產出所有租戶有效配置 JSON | `da-tools describe-tenant --all --output snap.json` |
| `describe-tenant --diff` | 比較兩個租戶配置差異 | `da-tools describe-tenant tid-1 --diff tid-2` |
| `blast_radius.py` | 比對兩份快照，分類影響 | `blast_radius.py --base a.json --pr b.json` |
| `migrate-conf-d` | 平面→階層遷移 | `da-tools migrate-conf-d --dry-run` |
| `validate-conf-d` | 配置正確性驗證 | `da-tools validate-conf-d --check-merge-conflicts` |

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [場景：多域名階層式配置](multi-domain-conf-layout.md) | ⭐⭐⭐ |
| [ADR-017：階層式 conf.d 設計決策](../adr/017-conf-d-directory-hierarchy-mixed-mode.md) | ⭐⭐⭐ |
| [ADR-018：繼承機制與雙重雜湊](../adr/018-defaults-yaml-inheritance-dual-hash.md) | ⭐⭐⭐ |
| [場景：租戶完整生命週期管理](tenant-lifecycle.md) | ⭐⭐ |
| [`da-tools` CLI 參考](../cli-reference.md) | ⭐⭐ |
