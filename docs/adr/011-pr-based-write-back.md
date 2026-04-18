---
title: "ADR-011: PR-based Write-back 模式"
tags: [adr, architecture, gitops, pr, write-back]
audience: [platform-engineers, developers]
version: v2.7.0
lang: zh
---

# ADR-011: PR-based Write-back 模式

> **Language / 語言：** **中文 (Current)** | [English](./011-pr-based-write-back.en.md)

## 狀態

✅ **Accepted** (v2.6.0) — 新增 `_write_mode: pr` 選項，UI 操作產生 GitHub PR 而非直接 commit

## 背景

### 問題陳述

ADR-009 建立的 commit-on-write 模式（UI → tenant-api → git commit）在快速迭代環境中運作良好，但在以下高安全場景會遇到合規摩擦：

1. **四眼原則（Four-eyes principle）**：金融、醫療等受監管行業要求配置變更經過至少一人審核後才能生效
2. **變更可逆性**：直接 commit 在多人並行操作時，revert 需手動追蹤 commit hash
3. **CI 整合**：部分團隊希望 config 變更觸發 CI pipeline（lint、dry-run apply、SLA 影響評估）後才合併
4. **審計粒度**：PR 提供比 git log 更豐富的審計元資料（reviewer、approval time、discussion thread）

### 決策驅動力

- 維持 GitOps 精神：Git repo 仍是 source of truth
- 向下相容：現有 `direct` 模式不受影響，`pr` 為 opt-in
- Eventual consistency 可接受：PR 模式下「已提交但未合併」的配置 UI 需明確標示
- 複用 GitHub API：不引入額外 approval 基礎設施

## 決策

### 雙模式架構：`_write_mode: direct | pr`

在 `_rbac.yaml` 同層級新增全域配置 `_write_mode`（或環境變數 `TA_WRITE_MODE`）：

```yaml
# conf.d/_write_mode.yaml 或 tenant-api flag
_write_mode: pr   # "direct"（預設，ADR-009 行為）| "pr"（PR-based）
```

**路由邏輯**（writer.go 層）：

```
WriteRequest → _write_mode?
  ├─ "direct" → 現有 commit-on-write（ADR-009）
  └─ "pr"     → create-branch → commit → create-PR → 回傳 pr_url
```

### PR 生命週期狀態模型

```
┌──────────┐    create    ┌─────────────┐    merge     ┌──────────┐
│ (UI 操作) │ ──────────→ │ pending_review│ ──────────→ │  merged   │
└──────────┘              └─────────────┘              └──────────┘
                               │
                               │ close/conflict
                               ▼
                          ┌──────────┐
                          │  closed   │
                          └──────────┘
```

| 狀態 | 語義 | UI 表現 |
|------|------|---------|
| `pending_review` | PR 已建立，等待 reviewer 審核 | 黃色 banner + PR 連結 |
| `merged` | PR 已合併，配置已生效 | 綠色通知，banner 消失 |
| `closed` | PR 被關閉或有衝突 | 紅色警告 + 重新提交按鈕 |

### PR 建立策略

**Branch 命名**：`tenant-api/{tenantID}/{timestamp}`（如 `tenant-api/db-a-prod/20260406-143022`）

**Commit 內容**：與 direct 模式相同（單一 tenant YAML 修改），author 為操作者 email

**PR 元資料**：
```json
{
  "title": "[tenant-api] Update db-a-prod configuration",
  "body": "Operator: alice@example.com\nChanges: _silent_mode → enabled\nSource: tenant-manager UI",
  "head": "tenant-api/db-a-prod/20260406-143022",
  "base": "main",
  "labels": ["tenant-api", "auto-generated"]
}
```

### API Response 格式

**單一 tenant 寫入**（PR 模式）：

```json
{
  "status": "pending_review",
  "pr_url": "https://github.com/org/repo/pull/42",
  "pr_number": 42,
  "message": "PR created. Configuration will take effect after merge."
}
```

**批量操作**（PR 模式）：

```json
{
  "status": "pending_review",
  "pr_url": "https://github.com/org/repo/pull/43",
  "pr_number": 43,
  "results": [
    {"tenant_id": "db-a-prod", "status": "included"},
    {"tenant_id": "db-b-staging", "status": "included"}
  ],
  "message": "Batch PR created with 2 tenant changes."
}
```

批量操作合併為 **單一 PR**（一個 PR 包含多個 tenant 的修改），避免 reviewer 被大量 PR 淹沒。

### Token 權限與 Secret 管理

**GitHub 模式**（`--write-mode pr` 或 `pr-github`）：

| 項目 | 規格 |
|------|------|
| **Token 類型** | GitHub Fine-grained Personal Access Token（推薦）或 GitHub App Installation Token |
| **最小權限** | `contents: write` + `pull_requests: write`（僅目標 repo） |
| **儲存方式** | K8s Secret → 環境變數 `TA_GITHUB_TOKEN`；禁止寫入 ConfigMap 或 YAML |
| **輪替策略** | 90 天過期 + Helm pre-upgrade hook 檢查有效期 |

**GitLab 模式**（`--write-mode pr-gitlab`，v2.6.0 Phase E 新增）：

| 項目 | 規格 |
|------|------|
| **Token 類型** | GitLab Project Access Token（推薦）、Group Access Token 或 Personal Access Token |
| **最小權限** | `api` scope（涵蓋 MR 建立與分支操作） |
| **儲存方式** | K8s Secret → 環境變數 `TA_GITLAB_TOKEN`；禁止寫入 ConfigMap 或 YAML |
| **輪替策略** | 365 天過期（GitLab 預設）+ Helm pre-upgrade hook 檢查有效期 |

### 平行 PR 衝突處理

**問題**：Tenant A 改路由（PR 1）+ Tenant B 改閾值（PR 2），若修改同一檔案會產生 Git conflict。

**緩解策略（雙層）**：

1. **檔案層面隔離**（已具備）：每個 tenant 一個 YAML 檔案（`conf.d/{tenantID}.yaml`），不同 tenant 的 PR 天然隔離
2. **同 tenant 並行控制**：同一 tenant 若已有 pending PR，新的寫入操作回傳 409 + 現有 PR 連結：
   ```json
   {
     "error": "pending_pr_exists",
     "existing_pr_url": "https://github.com/org/repo/pull/42",
     "message": "A pending PR for db-a-prod already exists. Merge or close it first."
   }
   ```
3. **`_groups.yaml` 特殊處理**：群組操作修改共用檔案，採用 advisory lock + auto-rebase：
   - 建立 PR 前先 fetch + rebase 到最新 main
   - 若 rebase 失敗，回傳 409 + 提示手動解決

### Eventual Consistency 語義

PR 模式下，tenant-manager UI 需區分兩種配置狀態：

| 狀態 | 資料來源 | 顯示方式 |
|------|---------|---------|
| **生效中** | `conf.d/*.yaml`（main branch HEAD） | 正常顯示 |
| **待審核** | tenant-api 內存的 pending PR 清單 | 黃色 overlay + "Pending PR" 標籤 |

tenant-api 維護一個 in-memory PR tracker（定期同步 GitHub API），提供：
- `GET /api/v1/prs` — 列出所有 pending PR
- `GET /api/v1/prs?tenant={id}` — 查詢特定 tenant 的 pending PR

### 實作分層

| 層 | 檔案 | 變更 |
|---|------|------|
| **Config** | `cmd/server/main.go` | `-write-mode` flag（`direct` / `pr` / `pr-github` / `pr-gitlab`）+ 環境變數 |
| **Platform Interface** | `internal/platform/platform.go`（v2.6.0 Phase E 新增） | 平台無關的 `Client` + `Tracker` interface |
| **Writer** | `internal/gitops/writer.go` | `WritePR()` 方法：branch → commit → push |
| **GitHub Client** | `internal/github/client.go` | 封裝 GitHub REST API，實作 `platform.Client` |
| **GitHub Tracker** | `internal/github/tracker.go` | In-memory pending PR 快取 + 定期 sync，實作 `platform.Tracker` |
| **GitLab Client** | `internal/gitlab/client.go`（v2.6.0 Phase E 新增） | 封裝 GitLab REST API v4，實作 `platform.Client` |
| **GitLab Tracker** | `internal/gitlab/tracker.go`（v2.6.0 Phase E 新增） | In-memory pending MR 快取 + 定期 sync，實作 `platform.Tracker` |
| **Handler** | `internal/handler/tenant_put.go` | 判斷 write mode → 透過 `platform.Client` 呼叫 `Write()` 或 `WritePR()` |
| **Handler** | `internal/handler/tenant_batch.go` | 批量操作 PR/MR 模式：合併為單一 PR/MR |
| **Handler** | `internal/handler/pr.go` | `GET /api/v1/prs` endpoint（透過 `platform.Tracker`） |
| **UI** | `tenant-manager.jsx` | Pending PRs/MRs banner + status overlay |

## 理由

### 為何不用 Git hook + auto-merge？

GitHub PR 機制提供原生的 code review、approval、CI check 整合。自建 approval 工作流會重複造輪子且缺乏生態整合。

### 為何批量操作合併為單一 PR？

- Reviewer 體驗：一次審核所有相關變更
- 原子性：batch 內的 tenant 變更要嘛全部生效、要嘛全部不生效
- 減少 PR 數量：避免 20 tenant batch 產生 20 個 PR

### 為何同 tenant 只允許一個 pending PR？

- 避免合併順序歧義（PR 1 設定 silent，PR 2 取消 silent，合併順序決定最終狀態）
- 簡化 UI 呈現（每個 tenant 最多一個 pending 標記）
- 若需多次修改，可更新（force-push）現有 PR branch

### 為何不拆分 `_groups.yaml` 為多檔案？

評估後認為成本大於效益：
- 群組操作頻率遠低於 tenant 操作，衝突概率低
- 拆分需要 loader、API、schema 全面改動
- advisory lock + auto-rebase 已足夠處理偶發衝突

## 後果

### 正面

- 符合金融/醫療等高安全環境的合規要求
- PR 提供原生的變更追蹤、discussion、CI 整合
- 向下相容：`direct` 模式不受任何影響

### 負面

- **延遲**：配置變更從「即時」變為「等待 merge」（PR 模式）
- **複雜度**：新增 GitHub API 依賴、token 管理、PR tracker
- **Eventual consistency**：UI 需處理「已提交但未生效」的中間狀態

### 風險緩解

| 風險 | 緩解 |
|------|------|
| GitHub/GitLab API 不可用 | 回傳 503 + 降級提示「暫時改用 direct mode 或稍後重試」 |
| Token 過期 | 啟動時檢查 token 有效性 + `/healthz` 回報 token 狀態 |
| PR/MR 永遠不被合併 | 可設 `pr_ttl` 過期自動關閉（預設不啟用） |

## 替代方案

| 方案 | 評估 | 棄用原因 |
|------|------|---------|
| **GitLab MR**（而非 GitHub PR） | ✅ **已實作** | v2.6.0 Phase E 實作 `platform.Client` 抽象層 + `internal/gitlab/` 套件。`--write-mode pr-gitlab` 啟用 |
| **自建 approval queue** | 可行 | 重複造輪子，缺乏 CI/CD 整合，維護成本高 |
| **Git branch per-write + 手動 merge** | 可行 | UX 差，操作者需離開 UI 到 Git 手動操作 |
| **Write-Ahead Log (WAL)** | 過度工程 | tenant config 不需要 ACID 等級的持久保證 |

## 相關決策

- **ADR-009**: Tenant Manager CRUD API — PR 模式建立在 commit-on-write 基礎上
- **ADR-010**: Multi-Tenant Grouping — 群組批量操作的 PR 合併策略
- **ADR-008**: Operator Native Integration — Operator 模式下 PR write-back 的 CRD 對應

## 參考

- [GitHub REST API: Pulls](https://docs.github.com/en/rest/pulls)
- [GitHub Fine-grained PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [GitLab REST API: Merge Requests](https://docs.gitlab.com/ee/api/merge_requests.html)
- [GitLab Project Access Tokens](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html)
- [Four-eyes principle (Wikipedia)](https://en.wikipedia.org/wiki/Two-man_rule)
