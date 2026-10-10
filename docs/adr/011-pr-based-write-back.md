---
title: "ADR-011: PR-based Write-back 模式"
tags: [adr, architecture, gitops, pr, write-back]
audience: [platform-engineers, developers]
version: v2.9.0
lang: zh
id: ADR-011
tracking_kind: adr
status: accepted
domain: tenant-api
created_at: 2026-04-07
updated_at: 2026-10-10
---
# ADR-011: PR-based Write-back 模式

> **Language / 語言：** **中文 (Current)** | [English](./011-pr-based-write-back.en.md)

**決策摘要**：tenant-api 除了直接 commit，另提供「開 PR」的寫回模式，部署時以 `--write-mode`（或環境變數 `TA_WRITE_MODE`）選擇，預設仍是直接 commit。PR 模式下，每次 UI 寫入都開一條新分支、commit、push，再在 GitHub 開 PR 或在 GitLab 開 MR；設定要等 PR 合併後才生效。同一個租戶同時只能有一個待審核的 PR。

## 狀態

✅ **Accepted**（v2.6.0）— 新增 PR 寫回模式（`--write-mode pr`），UI 操作產生 GitHub PR 而非直接 commit

## 名詞

- **直接寫回（direct）**：[ADR-009](009-tenant-manager-crud-api.md) 的 commit-on-write：API 修改 conf.d/ 的 YAML 後立刻 commit。
- **PR／MR**：GitHub 的 Pull Request 與 GitLab 的 Merge Request，同一件事的兩種叫法。本文說 PR 時兩者都算。
- **四眼原則（four-eyes principle）**：變更至少要經過另一個人審核才能生效。
- **最終一致（eventual consistency）**：寫入成功不代表立刻生效；PR 模式下，設定在 PR 合併後才真正生效，中間有一段「已送出、未生效」的狀態。

## 背景

### 問題

[ADR-009](009-tenant-manager-crud-api.md) 的直接寫回（UI → tenant-api → git commit）在快速迭代的環境運作良好，但在高安全要求的場景會遇到合規上的摩擦：

1. **四眼原則**：金融、醫療等受監管行業要求設定變更經過至少一人審核才能生效。
2. **變更可逆性**：多人並行操作時，直接 commit 要 revert 得手動追 commit hash。
3. **CI 整合**：有些團隊希望設定變更先觸發 CI（lint、dry-run apply、SLA 影響評估），再合併。
4. **稽核粒度**：PR 提供比 git log 更豐富的稽核資訊（reviewer、核准時間、討論串）。

### 決策驅動力

- 維持 GitOps 精神：Git repo 仍是設定的唯一來源。
- 向下相容：既有的直接寫回不受影響，PR 模式要明確開啟。
- 可以接受最終一致：PR 模式下「已送出、未合併」的設定，UI 要明確標示。
- 重用 GitHub 的 PR 機制，不另建審核基礎設施。

## 決策

### 兩種寫回模式

| `--write-mode` | 行為 |
|------|------|
| `direct`（預設） | 直接 commit（ADR-009 的行為） |
| `pr` 或 `pr-github` | 開 GitHub PR |
| `pr-gitlab` | 開 GitLab MR |

GitHub 與 GitLab 實作同一組平台無關的介面（建立 PR、追蹤待審核的 PR），handler 不必知道背後是哪一個平台。

### 範例：開啟 PR 模式

```bash
TA_WRITE_MODE=pr
TA_GITHUB_REPO=org/repo          # 也可用 --github-repo
TA_GITHUB_TOKEN=<token>          # 從 Kubernetes Secret 注入
# 選填：TA_GITHUB_BASE_BRANCH，預設 main
```

`pr-gitlab` 對應的是 `TA_GITLAB_PROJECT`、`TA_GITLAB_TOKEN` 與 `TA_GITLAB_TARGET_BRANCH`。缺少 repo 或 token 時，tenant-api 啟動即失敗。

寫入流程：

```
寫入請求 → 寫回模式？
  ├─ direct → 直接 commit（ADR-009）
  └─ pr     → 開分支 → commit → push → 建立 PR → 回傳 pr_url
```

### PR 的生命週期

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

| 狀態 | 意義 |
|------|------|
| `pending_review` | PR 已建立，等待審核 |
| `merged` | PR 已合併，設定生效 |
| `closed` | PR 被關閉或有衝突 |

### 範例：單一租戶寫入

`PUT /api/v1/tenants/db-a-prod`，操作者 `alice@example.com`。tenant-api 開出的分支是 `tenant-api/{租戶 ID}/{UTC 時間}`，例如 `tenant-api/db-a-prod/20260406-143022`；commit 內容與直接寫回相同（只改這個租戶的 YAML），author 是操作者的 email。建立的 PR：

```json
{
  "title": "[tenant-api] Update db-a-prod configuration",
  "body": "**Operator:** alice@example.com\n**Source:** tenant-manager UI\n**Tenant:** db-a-prod",
  "head": "tenant-api/db-a-prod/20260406-143022",
  "base": "main"
}
```

PR 建立後再加上 `tenant-api`、`auto-generated` 兩個 label。API 回應：

```json
{
  "status": "pending_review",
  "tenant_id": "db-a-prod",
  "pr_url": "https://github.com/org/repo/pull/42",
  "pr_number": 42,
  "message": "PR/MR created. Configuration will take effect after merge."
}
```

### 範例：批量寫入

批量操作合併成**一個 PR**（一個 PR 包含多個租戶的修改），避免 reviewer 被大量 PR 淹沒：

```json
{
  "status": "pending_review",
  "pr_url": "https://github.com/org/repo/pull/43",
  "pr_number": 43,
  "results": [
    {"tenant_id": "db-a-prod", "status": "included"},
    {"tenant_id": "db-b-staging", "status": "included"}
  ],
  "summary": "2 included in PR/MR, 0 failed",
  "message": "Batch PR/MR created with 2 tenant changes."
}
```

### Token 權限與 Secret 管理

**GitHub**（`--write-mode pr` 或 `pr-github`）：

| 項目 | 規格 |
|------|------|
| **Token 類型** | GitHub Fine-grained Personal Access Token（建議）或 GitHub App Installation Token |
| **最小權限** | `contents: write` + `pull_requests: write`（只限目標 repo） |
| **儲存方式** | Kubernetes Secret → 環境變數 `TA_GITHUB_TOKEN`；不要寫進 ConfigMap 或 YAML |

**GitLab**（`--write-mode pr-gitlab`）：

| 項目 | 規格 |
|------|------|
| **Token 類型** | GitLab Project Access Token（建議）、Group Access Token 或 Personal Access Token |
| **最小權限** | `api` scope（涵蓋建立 MR 與分支操作） |
| **儲存方式** | Kubernetes Secret → 環境變數 `TA_GITLAB_TOKEN`；不要寫進 ConfigMap 或 YAML |

### 同一租戶的並行 PR

**問題**：租戶 A 改路由（PR 1）、租戶 B 改閾值（PR 2），若兩者修改同一個檔案，就可能產生 Git 衝突。

**做法**：同一個租戶若已有待審核的 PR，新的寫入回 409，並附上現有 PR 的連結：

```json
{
  "code": "PENDING_PR_EXISTS",
  "error": "pending_pr_exists",
  "existing_pr_url": "https://github.com/org/repo/pull/42",
  "message": "A pending PR/MR for db-a-prod already exists or is being created. Merge or close it first.",
  "pr_number": 42,
  "request_id": "<請求 ID>"
}
```

### 最終一致的呈現

PR 模式下，tenant-manager UI 要區分兩種設定狀態：

| 狀態 | 資料來源 | 顯示方式 |
|------|---------|---------|
| **生效中** | `conf.d/*.yaml`（base 分支的 HEAD） | 正常顯示 |
| **待審核** | tenant-api 記憶體裡的待審核 PR 清單 | 黃色標記 + "Pending PR" 標籤 |

tenant-api 在記憶體裡維護一份待審核 PR 的清單，定期向 GitHub／GitLab API 同步，並提供：

- `GET /api/v1/prs` — 列出所有待審核的 PR
- `GET /api/v1/prs?tenant={id}` — 查詢特定租戶的待審核 PR

## 理由

### 為何不用 Git hook + 自動合併？

GitHub 的 PR 機制原生整合了 code review、核准與 CI 檢查。自建核准流程是重複造輪子，也接不上既有的生態。

### 為何批量操作合併成一個 PR？

- Reviewer 體驗：一次審完所有相關變更。
- 原子性：批量裡的租戶變更要嘛全部生效，要嘛全部不生效。
- 減少 PR 數量：避免 20 個租戶的批量產生 20 個 PR。

### 為何同一租戶只允許一個待審核 PR？

- 避免合併順序造成歧義（PR 1 開啟靜默、PR 2 取消靜默，最終狀態取決於合併順序）。
- 簡化 UI（每個租戶最多一個待審核標記）。
- 需要多次修改時，可以更新（force-push）現有 PR 的分支。

### 為何不把 `_groups.yaml` 拆成多個檔案？

評估後認為成本大於效益：

- 群組操作的頻率遠低於租戶操作，衝突機率低。
- 拆分需要全面修改載入程式、API 與 schema。

## 後果與已知限制

**得到的**

- 符合金融、醫療等高安全環境的合規要求。
- PR 提供原生的變更追蹤、討論與 CI 整合。
- 向下相容：直接寫回模式完全不受影響。

**要承擔的**

- **延遲**：PR 模式下，設定變更從「立即生效」變成「等合併」。
- **複雜度**：多了 GitHub／GitLab API 的依賴、token 管理與待審核 PR 的追蹤。
- **最終一致**：UI 要處理「已送出、未生效」的中間狀態。

## 考慮過的替代方案

| 方案 | 評估 | 不採用的原因 |
|------|------|---------|
| **自建核准佇列** | 可行 | 重複造輪子，缺乏 CI/CD 整合，維護成本高 |
| **每次寫入開分支 + 手動合併** | 可行 | 使用體驗差，操作者得離開 UI 到 Git 手動操作 |
| **預寫日誌（Write-Ahead Log，先把每筆變更寫進日誌、再套用）** | 過度設計 | 租戶設定不需要資料庫等級（ACID）的持久保證 |

## 實作位置

| 層 | 檔案 | 內容 |
|---|------|------|
| **設定** | `cmd/server/main.go` | `--write-mode` flag（`direct` / `pr` / `pr-github` / `pr-gitlab`）與對應的環境變數 |
| **平台介面** | `internal/platform/platform.go` | 平台無關的 `Client` 與 `Tracker` 介面 |
| **Writer** | `internal/gitops/writer_pr.go` | `WritePR()`：開分支 → commit → push |
| **GitHub** | `internal/github/client.go`、`internal/github/tracker.go` | 封裝 GitHub REST API；待審核 PR 的快取與定期同步 |
| **GitLab** | `internal/gitlab/client.go`、`internal/gitlab/tracker.go` | 封裝 GitLab REST API v4；待審核 MR 的快取與定期同步 |
| **Handler** | `internal/handler/tenant_put.go` | 依寫回模式選擇直接 commit 或開 PR |
| **Handler** | `internal/handler/tenant_batch.go` | 批量操作在 PR 模式下合併成一個 PR |
| **Handler** | `internal/handler/pr.go` | `GET /api/v1/prs` |
| **UI** | `tenant-manager.jsx` | 待審核 PR 的提示與標記 |

以上路徑除 UI 外都在 `components/tenant-api/` 底下。

## 相關

- [ADR-009: Tenant Manager CRUD API 架構](009-tenant-manager-crud-api.md) — PR 模式建立在直接寫回的基礎上
- [ADR-010: Multi-Tenant Grouping Architecture](010-multi-tenant-grouping.md) — 群組批量操作的 PR 合併策略
- [ADR-008: Operator-Native 整合路徑](008-operator-native-integration-path.md) — Operator 模式下 PR 寫回與 CRD（Kubernetes 自訂資源）的對應
- [GitHub REST API: Pulls](https://docs.github.com/en/rest/pulls)
- [GitHub Fine-grained PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [GitLab REST API: Merge Requests](https://docs.gitlab.com/ee/api/merge_requests.html)
- [GitLab Project Access Tokens](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html)
- [Four-eyes principle (Wikipedia)](https://en.wikipedia.org/wiki/Two-man_rule)
