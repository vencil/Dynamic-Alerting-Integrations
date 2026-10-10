---
title: "ADR-010: Multi-Tenant Grouping Architecture"
tags: [adr, architecture, groups, tenant-management]
audience: [platform-engineers, developers]
version: v2.9.0
lang: zh
id: ADR-010
tracking_kind: adr
status: accepted
domain: tenant-api
created_at: 2026-04-06
updated_at: 2026-10-10
---
# ADR-010: Multi-Tenant Grouping Architecture

> **Language / 語言：** **中文 (Current)** | [English](./010-multi-tenant-grouping.en.md)

**決策摘要**：自訂群組定義在 conf.d/ 的 `_groups.yaml`，成員是一份明列的租戶 ID 清單，透過 tenant-api 的群組 API 管理，寫入走與租戶設定相同的 Git 寫回流程。同時在租戶的 `_metadata` 加上 environment、region、domain、db_type 等欄位，供 API 與 UI 篩選；這些欄位不加進 `tenant_metadata_info` 的標籤。

## 狀態

✅ **Accepted**（v2.5.0）— 自訂群組以 `_groups.yaml` 儲存於 conf.d/，透過 tenant-api CRUD endpoints 管理

## 名詞

- **conf.d/**：存放租戶設定 YAML 的目錄。檔名以 `_` 開頭的是平台層級的檔案（例如 `_defaults.yaml`、`_rbac.yaml`），不是某個租戶的設定。
- **`_metadata`**：租戶設定裡描述租戶本身的區塊（負責人、runbook 連結等），不是閾值。
- **`tenant_metadata_info`**：threshold-exporter 對每個租戶輸出的資訊指標，值固定為 1，標籤帶 `_metadata` 的部分欄位，讓 PromQL 可以把這些欄位 join 到告警上。
- **Cardinality（基數）**：一個指標底下不同標籤組合的數量。每多一個標籤、標籤值越多，Prometheus 要存的時間序列就越多。

## 背景

### 問題

tenant-api 已提供單一租戶的 CRUD 與批量操作，但租戶數量增加（50 個以上）後，領域專家遇到以下困難：

1. **沒有分組的視角**：列出租戶的 API 回傳扁平清單，無法依業務維度（region、domain、db_type）快速篩選。
2. **批量操作要手動指定**：每次批量操作都得逐一列出租戶 ID，無法「對某個群組整批操作」。
3. **metadata 不夠用**：當時的 `_metadata` 只有 runbook_url、owner、tier，撐不起多維度篩選。
4. **沒有可保存的群組**：UI 的篩選條件重新整理後就消失，無法建立可命名、可重用的群組定義。

### 決策驅動力

- 群組是 UI 與 API 層的概念，**不影響 Prometheus 指標的產生**。
- 群組定義要受版本控制（Git），並支援多人協作（偵測衝突）。
- 重用 [ADR-009](009-tenant-manager-crud-api.md) 的 Git 寫回模式，不引入新的持久層。

## 決策

### 1. 擴充 `_metadata`

```yaml
_metadata:
  runbook_url: "https://wiki.example.com/db-a"
  owner: "team-dba"
  tier: "tier-1"
  # 以下為新增欄位
  environment: "production"       # production | staging | development
  region: "ap-northeast-1"       # 雲端區域
  domain: "finance"              # 業務領域
  db_type: "mariadb"             # 資料庫類型
  tags: ["critical-path", "pci"] # 自由標籤
  groups: ["production-dba"]     # 所屬群組
```

新欄位的特性：

- **全部選填**：省略等同空值，向下相容。
- **不加進 `tenant_metadata_info`**：不成為這個指標的標籤，避免 cardinality 暴增。
- **`db_type` 另有一個指標**：宣告了 `db_type` 的租戶，exporter 另外輸出 `tenant_expected_exporter{tenant, db_type}`（值為 1），供存活檢查判斷這個租戶的資料庫 exporter 是否缺席；每個宣告的租戶只有一條。
- **兩端都能解析**：Go 的 `TenantMetadata` struct 與 Python 的 `generate_tenant_metadata.py` 都讀得懂這些欄位。

### 2. `_groups.yaml`：自訂群組定義

```yaml
# conf.d/_groups.yaml — 透過 tenant-api 或手動編輯維護
groups:
  production-dba:
    label: "Production DBA"
    description: "All production database tenants managed by DBA team"
    filters:                      # 依 metadata 自動匹配的條件；目前只儲存，不做自動匹配
      environment: "production"
      domain: "finance"
    members:                      # 明列的成員清單
      - db-a
      - db-b
```

| 面向 | 決策 | 理由 |
|------|------|------|
| 儲存位置 | `conf.d/_groups.yaml`（底線開頭） | 與 `_defaults.yaml`、`_rbac.yaml` 一致 |
| 成員模式 | 明列的 `members[]` 清單 | 可預測、可 review、可 diff |
| 寫入模式 | 重用 `gitops.Writer` 的 `sync.Mutex` 與 HEAD 衝突偵測 | 不引入新的鎖機制，確保與租戶寫入互斥 |
| ID 格式 | `[a-z0-9\-_]`，最長 128 字元 | 可以直接當 YAML key 與 URL 路徑片段 |

### 3. tenant-api 的群組 API

| Method | Path | 權限 | 說明 |
|--------|------|-----------|------|
| GET | `/api/v1/groups` | read | 列出所有群組 |
| GET | `/api/v1/groups/{id}` | read | 取得單一群組 |
| PUT | `/api/v1/groups/{id}` | write | 建立或更新群組 |
| DELETE | `/api/v1/groups/{id}` | write | 刪除群組 |
| POST | `/api/v1/groups/{id}/batch` | read（路由）+ 逐一檢查成員的 write | 對群組成員批量操作 |

### 4. UI 的群組管理（tenant-manager.jsx）

- 群組側欄：顯示群組清單、成員數，以及建立與刪除操作。
- 群組篩選：點選群組就過濾租戶清單。
- 多維度篩選：可依 environment、domain、db_type 等欄位過濾，其中 domain、db_type 的下拉選單由租戶的 metadata 動態產生。
- 依權限顯示：呼叫 `/api/v1/me` 取得權限，沒有寫入權限時，群組的建立、刪除等寫入操作直接隱藏（不是灰掉）。
- 寫入回 409 時，提示設定已被別人更新、請重新整理後再試。

### 範例：群組不影響 `/metrics`，新欄位不進 `tenant_metadata_info`

輸入：conf.d/ 裡有 `_defaults.yaml`（`defaults: {mysql_connections: 80}`）、租戶檔 `db-a.yaml`（`mysql_connections: "70"`，`_metadata` 為第 1 節的內容），以及第 2 節的 `_groups.yaml`。

threshold-exporter 的 `/metrics` 中帶 `tenant="db-a"` 的全部輸出（不含註解行）：

```
da_tenant_metrics_over_limit{tenant="db-a"} 0
tenant_expected_exporter{db_type="mariadb",tenant="db-a"} 1
tenant_metadata_info{owner="team-dba",runbook_url="https://wiki.example.com/db-a",tenant="db-a",tier="tier-1"} 1
user_severity_dedup{mode="enable",tenant="db-a"} 1
user_threshold{component="mysql",metric="connections",severity="warning",tenant="db-a"} 70
```

結果：`tenant_metadata_info` 只帶 owner、runbook_url、tier 三個欄位，environment、region、domain、tags、groups 都不出現在任何指標上；`db_type` 只出現在 `tenant_expected_exporter`。拿掉 `_groups.yaml` 重跑，除了載入耗時這類計時指標，輸出完全相同。

## 理由

### 為何不用標籤自動分組？

明列 `members[]` 的優點：

- **可 review**：PR diff 清楚看出哪些租戶被加入或移出群組。
- **可預測**：群組成員不會因為 metadata 改了而意外變動。
- **簡單**：不必實作篩選條件的運算式解析器。

依 metadata 自動匹配的條件保留在 `filters` 欄位，但目前不啟用自動匹配。

### 為何新增 metadata 欄位，而不是只用 tags？

結構化欄位（environment、domain、db_type）比自由標籤更適合 UI 篩選：

- 下拉選單需要有限的選項集合。
- Schema 驗證可以檢查結構化欄位的值域。

`tags[]` 則補足結構化欄位涵蓋不到的情境。

## 後果與已知限制

**得到的**

- `_groups.yaml` 納入 Git 版本控制，稽核軌跡完整。

**要承擔的**

- conf.d/ 多一個不是租戶設定的檔案（已有 `_defaults.yaml`、`_rbac.yaml` 的先例）。
- 群組寫入與租戶寫入共用同一把 `sync.Mutex`，並行量高時可能互相等待（實際操作頻率不高）。

**風險與緩解**

| 風險 | 緩解 |
|------|------|
| 多人同時編輯 `_groups.yaml` 造成衝突 | 重用 writer 的 HEAD 衝突偵測，回 409 要求重試 |
| 群組成員引用了不存在的租戶 ID | 寫入時不驗證（軟引用） |
| metadata 欄位變多讓 YAML 變冗長 | 新欄位全部選填，沒設 metadata 的租戶不受影響 |

**尚未提供的**

1. **依 metadata 自動匹配成員**：啟用 `filters` 欄位，依 metadata 自動把租戶納入群組，減少手動維護。
2. **寫入時驗證成員**：寫入時確認成員引用的租戶 ID 存在，從軟引用升級為經驗證的引用。
3. **巢狀群組**：群組可以包含子群組，支援階層式的組織結構。

## 實作位置

- `components/tenant-api/internal/groups/groups.go` — 群組的載入與查詢
- `components/tenant-api/internal/handler/group.go` — 群組 CRUD handler
- `components/tenant-api/internal/handler/group_batch.go` — 群組批量操作 handler
- `tools/portal/src/interactive/tools/tenant-manager.jsx` — UI
- `scripts/tools/dx/generate_tenant_metadata.py` — 產生租戶元資料（含多維度分組）

## 相關

- [ADR-009: Tenant Manager CRUD API 架構](009-tenant-manager-crud-api.md) — 群組 API 的基礎
- [ADR-007: 跨域路由設定檔與域策略](007-cross-domain-routing-profiles.md) — `_routing` 的 schema
- [ADR-011: PR-based Write-back 模式](011-pr-based-write-back.md) — 群組批量操作在 PR 模式下合併成一個 PR
