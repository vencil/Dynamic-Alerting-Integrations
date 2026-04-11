---
title: "ADR-010: Multi-Tenant Grouping Architecture"
tags: [adr, architecture, groups, tenant-management]
audience: [platform-engineers, developers]
version: v2.6.0
lang: zh
---

# ADR-010: Multi-Tenant Grouping Architecture

## 狀態

✅ **Accepted** (v2.5.0) — 自定義群組以 `_groups.yaml` 儲存於 conf.d/，透過 tenant-api CRUD endpoints 管理

## 背景

### 問題陳述

v2.4.0 的 tenant-api 提供了單一 tenant CRUD 與批量操作能力，但隨著 tenant 數量增長（50+），domain expert 面臨以下困難：

1. **缺乏分組視角**：ListTenants 回傳扁平列表，無法按業務維度（region、domain、db_type）快速篩選
2. **批量操作需手動指定**：每次 batch 需逐一列出 tenant ID，無法「對某個群組整批操作」
3. **metadata 不夠豐富**：v2.4.0 `_metadata` 僅有 runbook_url、owner、tier，無法支撐多維度篩選
4. **無持久化群組概念**：UI 篩選條件在刷新後消失，無法建立可命名、可重用的群組定義

### 決策驅動力

- 群組是 UI/API 層概念，**不影響 Prometheus metric 產生**（threshold-exporter 不讀 `_groups.yaml`）
- 群組定義需版本控制（Git）並支援多人協作（conflict detection）
- 複用 ADR-009 的 gitops writer 模式，不引入新的持久層

## 決策

### Core Architecture: _groups.yaml + Extended _metadata Schema

**1. `_metadata` 擴展（Go types + YAML schema）**

```yaml
_metadata:
  runbook_url: "https://wiki.example.com/db-a"
  owner: "team-dba"
  tier: "tier-1"
  # v2.5.0 新增 ↓
  environment: "production"       # production | staging | development
  region: "ap-northeast-1"       # cloud region
  domain: "finance"              # business domain
  db_type: "mariadb"             # database type
  tags: ["critical-path", "pci"] # free-form tags
  groups: ["production-dba"]     # group memberships
```

新欄位特性：
- **全部 optional**：省略等同空值，向下相容
- **API/UI only**：不增加 `tenant_metadata_info` Prometheus label（避免 cardinality 爆炸）
- **雙端驗證**：Go `TenantMetadata` struct + Python `generate_tenant_metadata.py` 均能解析

**2. `_groups.yaml` — 自定義群組定義**

```yaml
# conf.d/_groups.yaml — managed via tenant-api or manual editing
groups:
  production-dba:
    label: "Production DBA"
    description: "All production database tenants managed by DBA team"
    filters:                      # metadata-based auto-match（未來擴充用）
      environment: "production"
      domain: "finance"
    members:                      # 靜態成員列表
      - db-a
      - db-b
```

設計決策：
| 面向 | 決策 | 理由 |
|------|------|------|
| 儲存位置 | `conf.d/_groups.yaml`（底線前綴） | threshold-exporter loader 自動 skip `_` 前綴檔案；與 `_defaults.yaml`、`_rbac.yaml` 一致 |
| 成員模式 | 靜態 `members[]` list | 可預測、可 review、可 diff；filter-based auto-membership 排入 v2.7.0+ 候選 |
| 寫入模式 | 複用 `gitops.Writer` 的 `sync.Mutex` + HEAD conflict detection | 不引入新鎖機制，確保與 tenant 寫入互斥 |
| ID 格式 | `[a-z0-9\-_]`，最長 128 字元 | 兼容 YAML key 與 URL path segment |

**3. tenant-api Group Endpoints**

| Method | Path | Permission | 說明 |
|--------|------|-----------|------|
| GET | `/api/v1/groups` | read | 列出所有群組 |
| GET | `/api/v1/groups/{id}` | read | 取得單一群組詳情 |
| PUT | `/api/v1/groups/{id}` | write | 建立或更新群組 |
| DELETE | `/api/v1/groups/{id}` | write | 刪除群組 |
| POST | `/api/v1/groups/{id}/batch` | read (route) + per-tenant write | 對群組成員批量操作 |

Group batch 的 RBAC 模型與 tenant batch 一致：route-level 只檢查 authenticated，每個成員 tenant 的 write 權限在 handler 內逐一驗證。

**4. UI 群組管理（tenant-manager.jsx）**

- Group 側欄：顯示群組列表 + 成員數 + 建立/刪除操作
- Auth-aware：呼叫 `/api/v1/me`，無 write 權限時灰掉寫入按鈕
- 群組篩選：點選群組自動過濾 tenant 列表
- 多維度篩選增強：domain、db_type dropdown 從 tenant metadata 動態產生

## 基本原理

### 為何不用 Label/Tag 自動群組？

靜態 `members[]` 列表的優勢：
- **可 review**：PR diff 清楚顯示「哪些 tenant 被加入/移除群組」
- **可預測**：不會因 metadata 變更而意外改變群組成員
- **簡單**：不需要實作 filter expression parser

Filter-based auto-membership 保留在 `filters` 欄位中，但目前不啟用自動匹配邏輯，排入 v2.7.0+ 候選。

### 為何新增 metadata 欄位而非使用 tags-only？

結構化欄位（environment、domain、db_type）比自由標籤更適合 UI 篩選：
- 下拉選單需要有限的選項集合
- PromQL join 需要 well-known label name
- Schema validation 可以對結構化欄位做值域檢查

`tags[]` 則作為自由標籤補充結構化欄位無法涵蓋的場景。

## 後果

### 正向

- Domain expert 可透過 UI 在 3 分鐘內建立群組並批量操作（Phase B review 目標）
- 多維度篩選讓 100+ tenant 的環境仍可快速找到目標
- `_groups.yaml` 納入 Git 版本控制，audit trail 完整

### 負向

- `conf.d/` 目錄增加一個非 tenant 設定檔（但已有 `_defaults.yaml`、`_rbac.yaml` 先例）
- Group 寫入與 tenant 寫入共用 `sync.Mutex`，高併發場景可能互相等待（但實際操作頻率低）

### 風險

| 風險 | 緩解 |
|------|------|
| `_groups.yaml` 多人同時編輯造成 conflict | 複用 writer 的 HEAD conflict detection，409 要求重試 |
| Group member 引用不存在的 tenant ID | 寫入時不驗證（soft reference），lint hook 排入 v2.7.0+ 候選 |
| Metadata 欄位增多導致 YAML 冗長 | 新欄位全部 optional，不設 metadata 的 tenant 不受影響 |

## 演進狀態

**v2.5.0 已交付**：
- 靜態 `members[]` 群組 CRUD + batch 操作
- 多維度篩選（environment / domain / db_type dropdown）
- Group sidebar + auth-aware UI
- Environment / domain 維度 RBAC（`_rbac.yaml` dimension filtering）
- Optimistic update + 409 conflict toast（v2.5.0-final 補齊）

**殘留**：
1. **Filter-based auto-membership**（v2.7.0+ 候選）：啟用 `filters` 欄位，依 metadata 自動匹配 tenant 進群組，減少手動維護成本
2. **Group member lint hook**（v2.7.0+ 候選）：寫入時驗證 member 引用的 tenant ID 存在，從 soft reference 升級為 validated reference
3. **Group nesting**（v2.7.0+ 候選）：群組可包含子群組，支援階層式組織結構

## 相關決策

- [ADR-009](009-tenant-manager-crud-api.md) — Tenant Manager CRUD API 架構（基礎）
- [ADR-007](007-cross-domain-routing-profiles.md) — 四層路由合併（`_routing` schema）

## 相關資源

- `components/tenant-api/internal/groups/groups.go` — Group manager implementation
- `components/tenant-api/internal/handler/group.go` — Group CRUD handlers
- `components/tenant-api/internal/handler/group_batch.go` — Group batch handler
- `docs/interactive/tools/tenant-manager.jsx` — UI implementation
- `scripts/tools/dx/generate_tenant_metadata.py` — Metadata generator with multi-dimension grouping
