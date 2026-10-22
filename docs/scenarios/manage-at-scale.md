---
title: "場景：千租戶規模管理"
tags: [scenario, scale, blast-radius, search, management]
audience: [platform-engineer, operator, devops]
version: v2.9.0
lang: zh
---
# 場景：千租戶規模管理

> **Language / 語言：** **中文 (Current)** | [English](./manage-at-scale.en.md)

> **v2.9.0** | 相關文件：[multi-domain-conf-layout](multi-domain-conf-layout.md)、[ADR-016](../adr/016-conf-d-directory-hierarchy-mixed-mode.md)、[ADR-017](../adr/017-defaults-yaml-inheritance-dual-hash.md)、[tenant-lifecycle](tenant-lifecycle.md)

## 概述

當平台規模從數十租戶成長到數百甚至上千租戶時，原本在小規模下足夠的操作方式會遇到效率瓶頸。本文件描述在 Dynamic Alerting 平台中，如何利用既有工具鏈有效管理大規模租戶環境：

- **影響範圍預估**：變更 `_defaults.yaml` 前，用 `config-diff` 了解影響範圍
- **批次查詢與篩選**：快速定位特定域名、區域或環境的租戶
- **繼承鏈追蹤**：確認每個租戶的有效配置來源
- **安全變更流程**：PR → CI 貼出 config-diff 報告 → Review → Merge

## 前置條件

- 已完成 `conf.d/` 階層式結構遷移（參考 [multi-domain-conf-layout](multi-domain-conf-layout.md)）或至少部分域名使用階層式結構（混合模式）
- 已安裝工具：`da-tools config-diff`（`scripts/tools/ops/config_diff.py`）、`scripts/tools/dx/describe_tenant.py`、`scripts/tools/dx/migrate_conf_d.py`。兩支都讀 `da-guard`（`$DA_GUARD_BINARY` 或 PATH 上的 `da-guard`，da-tools 映像已內建；repo 內 `make da-guard-build` 建到 `.build/da-guard`）：`describe_tenant.py` 輸出的每個值（有效設定、繼承鏈、`merged_hash`）都讀自 `da-guard effective`，沒有 da-guard 時結束碼 2、不輸出（[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）；`config-diff` 比較兩棵樹經 `da-guard served-values` 與路由產生器渲染後的結果（[ADR-037](../adr/037-config-diff-compares-rendered-output.md)）
- PR 上的自動報告：`da-tools init` 產生的 CI 會在 PR 上跑 config-diff 並貼出報告（見 [GitOps CI 整合](gitops-ci-integration.md)）

## 情景 1：變更域預設值前評估影響範圍

### 問題

你需要將 Finance 域所有租戶的 `MariaDBHighConnections` 閾值從 90 調高到 95。在有 200+ Finance 租戶的環境下，你想在修改前確認：

1. 有多少租戶會受影響？
2. 哪些租戶已自行覆蓋此閾值（不受影響）？
3. 變更是否會觸發路由或 receiver 變動？

### 步驟

#### A. 保留修改前的 conf.d

```bash
cp -r conf.d/ conf.d.before/
```

#### B. 修改域預設值

```yaml
# conf.d/finance/_defaults.yaml —— domain 層的平台檔
defaults:
  mysql_connections: 95   # 從 finance domain 的 90 調高
```

> 閾值以**指標 key**（`mysql_connections`）為鍵，不是以告警名為鍵：
> `MariaDBHighConnections` 消費的是 `tenant:alert_threshold:mysql_connections`，
> 所以一個 key 可以驅動多條告警。平台值是不加引號的數字（`map[string]float64`），
> 租戶覆寫則是加引號的字串。

#### C. 執行 config-diff

```bash
da-tools config-diff --old-dir conf.d.before/ --new-dir conf.d/
```

結束碼 0 是沒有變更，1 是有變更（或有沒比對到的東西），2 是沒有結果（工具或 PR 側失敗）。

輸出範例（三個 finance 租戶的樹，`tenant-fin-003` 自己寫了 `mysql_connections: "98"`；加 `--at 2026-10-22T00:00:00Z` 實際跑出）：

```
# Config Diff Report

Comparing: `conf.d.before` → `conf.d`

Rendered at: `2026-10-22T00:00:00Z` (schedules, silences and maintenance windows are read at this instant, on both sides)

## Platform Settings

> Top-level keys of `_`-prefixed files whose value changed (comments and formatting are not changes), and `_` files no reader reads whose bytes changed. The renders compare what a setting does; a setting they do not show is listed here to review directly.

**Rendered (see the sections below)** (1)

- `finance/_defaults.yaml` `defaults` (modified)
  - the renders that read it changed; which of those changes come from this key is not told apart

## Threshold Changes

| Metric | Before | After | Change | Affected Alerts | Tenants |
|--------|--------|-------|--------|-----------------|---------|
| mysql_connections | 90 | 95 | looser | MariaDBHighConnections, MariaDBSystemBottleneck | 2 tenants: `tenant-fin-001`, `tenant-fin-002` |

---
Summary: 2 tenant(s) changed, 2 metric change(s)
```

注意：已自行覆蓋 `mysql_connections` 的租戶（這裡的 `tenant-fin-003`）不在受影響清單中——覆寫的鍵是指標 key，不是告警名。路由或 receiver 有變時，報告另有對應區段。要機器可讀的結果用 `--format json`。

### D. 確認後提交 PR

CI 會在 PR 上貼出同一份 config-diff 報告，reviewer 可在 merge 前確認影響範圍。

## 情景 2：追蹤單一租戶的配置來源

### 問題

租戶 `tenant-fin-042` 的 `MariaDBHighConnections` 告警不斷觸發。你想確認這個閾值來自哪一層，才能在正確的位置修改。

### 步驟

```bash
python scripts/tools/dx/describe_tenant.py tenant-fin-042 --show-sources --conf-d conf.d/
```

輸出範例：

```json
{
  "tenant_id": "tenant-fin-042",
  "source_file": "finance/us-east/prod/tenant-fin-042.yaml",
  "source_hash": "2748782e0e18b18b",
  "merged_hash": "afe82e02e229272f",
  "defaults_chain": [
    "_defaults.yaml",
    "finance/_defaults.yaml"
  ],
  "effective_config": {
    "mysql_connections": 90,
    "container_memory": "92"
  }
}
```

`defaults_chain` 就是合併順序，越內層越後面。`mysql_connections: 90` 是不加引號的
數字 ⇒ 來自平台的 `_defaults.yaml`，而鏈上最內層設它的是 `finance/_defaults.yaml`，
也就是 **domain 層**。（`container_memory` 是加引號的字串，那個是租戶自己設的。）
若輸出另有 `platform_overlay`，其中列出的鍵來自根目錄平台檔 `tenants:` 區塊給這個租戶的值
（#2019），既不是 defaults chain 也不是租戶檔——這些鍵的來源以該清單為準，不看引號。
同理，`profile_overlay` 列出租戶以 `_profile` 選用的 profile 補上的鍵（#2117）。
若只想為這個租戶調整 `mysql_connections`，在 tenant 檔案中覆蓋即可：

```yaml
# conf.d/finance/us-east/prod/tenant-fin-042.yaml
tenants:
  tenant-fin-042:
    mysql_connections: "120"   # 只對此租戶提高；加引號——租戶值是
                               # ScheduledValue（字串｜物件）
```

## 情景 3：比較兩個租戶的配置差異

### 問題

`tenant-fin-001`（US-East）和 `tenant-fin-080`（EU-West）的告警行為不同。你想了解兩者的有效配置差異。

### 步驟

```bash
python scripts/tools/dx/describe_tenant.py tenant-fin-001 --diff tenant-fin-080 --conf-d conf.d/
```

輸出範例：

```json
{
  "tenant_a": "tenant-fin-001",
  "tenant_b": "tenant-fin-080",
  "only_in_tenant-fin-001": {
    "container_cpu": "75"
  },
  "only_in_tenant-fin-080": {
    "pg_replication_lag": "60"
  },
  "different": {
    "mysql_connections": {"a": "110", "b": "120"}
  }
}
```

差異來自 region 層預設值（US-East vs EU-West）。

## 情景 4：CI 自動化 — PR 上的 config-diff 報告

### 觸發條件

`da-tools init` 產生的 workflow（GitHub Actions／GitLab）在 PR 修改 `conf.d/` 時執行 config-diff，GitHub 那一份把報告貼成 PR comment，GitLab 那一份印在 job log 並以 artifact 上傳。步驟細節、結束碼的處理與 GitLab 的差異見 [GitOps CI 整合](gitops-ci-integration.md)。

### 流程

```
PR 提交 → CI 執行 config-diff 那一步
  ├── 1. 取出 base branch 的 conf.d/ 到 conf.d.base/
  ├── 2. da-tools config-diff --old-dir conf.d.base/ --new-dir conf.d/
  │      （兩側各以 da-guard served-values 與路由產生器渲染，再比較）
  └── 3. 結束碼 0 或 1：貼出報告；2：該步驟失敗
```

報告的內容與情景 1 的輸出範例相同：平台設定的變更、閾值、路由、receiver 等區段，每一筆變更帶它影響的租戶清單。比較規則見 [ADR-037](../adr/037-config-diff-compares-rendered-output.md)。

## 情景 5：大規模遷移後驗證

### 問題

你剛將 200 個 Finance 租戶從平面結構遷移到階層式結構，需要驗證遷移前後每個租戶的有效配置沒有變化。

### 步驟

```bash
# 1. 保留遷移前的 conf.d
cp -r conf.d/ conf.d.pre-migration/

# 2. 執行遷移
python scripts/tools/dx/migrate_conf_d.py --apply \
  --conf-d conf.d/ \
  --infer-from metadata

# 3. 比對：應該沒有變更
da-tools config-diff --old-dir conf.d.pre-migration/ --new-dir conf.d/
```

預期結果：結束碼 0，報告為 `No changes detected.`。config-diff 比較的是渲染結果，不是檔案，所以只搬移或改名檔案（包括宣告 `_custom_alerts` 的檔）不算變更。如果有變更，表示遷移過程中有配置語義發生變化，需要逐一排查。

## 工具速查

| 工具 | 用途 | 典型用法 |
|------|------|---------|
| `describe_tenant.py <id>` | 查看單一租戶有效配置 | `python scripts/tools/dx/describe_tenant.py tenant-a --show-sources` |
| `describe_tenant.py --all` | 產出所有租戶有效配置 JSON | `python scripts/tools/dx/describe_tenant.py --all --output snap.json` |
| `describe_tenant.py --diff` | 比較兩個租戶配置差異 | `python scripts/tools/dx/describe_tenant.py tid-1 --diff tid-2` |
| `da-tools config-diff` | 比較兩棵 conf.d 的渲染結果，列出影響的租戶 | `da-tools config-diff --old-dir conf.d.before/ --new-dir conf.d/` |
| `migrate_conf_d.py` | 平面→階層遷移 | `python scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run` |
| `da-tools validate-config` | 配置正確性驗證 | `da-tools validate-config --config-dir conf.d/` |

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [場景：多域名階層式配置](multi-domain-conf-layout.md) | ⭐⭐⭐ |
| [ADR-016：階層式 conf.d 設計決策](../adr/016-conf-d-directory-hierarchy-mixed-mode.md) | ⭐⭐⭐ |
| [ADR-017：繼承機制與雙重雜湊](../adr/017-defaults-yaml-inheritance-dual-hash.md) | ⭐⭐⭐ |
| [場景：租戶完整生命週期管理](tenant-lifecycle.md) | ⭐⭐ |
| [`da-tools` CLI 參考](../cli-reference.md) | ⭐⭐ |
