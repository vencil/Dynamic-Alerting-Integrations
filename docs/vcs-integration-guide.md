---
title: "VCS 整合指南 — GitHub / GitLab / 自託管實例"
tags: [integration, vcs, github, gitlab, gitops]
audience: [platform-engineer]
version: v2.6.0
lang: zh
---
# VCS 整合指南 — GitHub / GitLab / 自託管實例

> **v2.6.0** | 相關文件：[ADR-011 PR-based Write-back](adr/011-pr-based-write-back.md) · [tenant-api README](../components/tenant-api/README.md) · [GitOps 部署指南](gitops-deployment.md)

## 概覽

tenant-api 的 PR write-back 模式讓 Portal UI 和 REST API 的寫入操作自動產生 Git PR/MR，而非直接 commit。這同時支援 GitHub（含 Enterprise Server）和 GitLab（含自託管實例），實現審計可追蹤的變更管理。

### 架構

```
Portal UI / REST API
    ↓ write request
tenant-api (write-back mode)
    ↓ create branch + commit + PR/MR
VCS (GitHub / GitLab)
    ↓ review + merge
GitOps controller (ArgoCD / Flux)
    ↓ deploy
threshold-exporter (hot-reload)
```

## Write-back 模式選擇

| 模式 | 環境變數 | 適用場景 |
|------|---------|----------|
| `direct` | `TA_WRITE_MODE=direct` | 開發環境、單人操作、不需審核 |
| `pr-github` | `TA_WRITE_MODE=pr-github` | GitHub.com 或 GitHub Enterprise Server |
| `pr-gitlab` | `TA_WRITE_MODE=pr-gitlab` | GitLab.com 或自託管 GitLab |

## GitHub 整合

### Token 權限

使用 **Fine-grained Personal Access Token**（推薦）或 Classic PAT：

| 權限 | 用途 |
|------|------|
| `contents:write` | 建立分支、commit 檔案 |
| `pull_requests:write` | 建立和更新 PR |

Classic PAT 需要 `repo` scope。

### 配置

```bash
export TA_WRITE_MODE=pr-github
export TA_GITHUB_TOKEN=ghp_xxxxxxxxxxxx
export TA_GITHUB_REPO=org/config-repo        # owner/repo 格式
export TA_GITHUB_BASE_BRANCH=main            # PR 目標分支（預設 main）
```

### GitHub Enterprise Server

額外設定 API URL：

```bash
export TA_GITHUB_API_URL=https://github.internal.example.com/api/v3
```

啟動時自動執行 `ValidateToken()` 驗證連線和權限。

## GitLab 整合

### Token 權限

使用 Project、Group 或 Personal Access Token，需要 `api` scope。

| Token 類型 | 建議場景 |
|-----------|---------|
| Project Access Token | 單一 repo，最小權限 |
| Group Access Token | 多個 repo 共用同一 group |
| Personal Access Token | 開發測試用 |

### 配置

```bash
export TA_WRITE_MODE=pr-gitlab
export TA_GITLAB_TOKEN=glpat-xxxxxxxxxxxx
export TA_GITLAB_PROJECT=infra/alerting-config  # 專案路徑或數字 ID
export TA_GITLAB_TARGET_BRANCH=main             # MR 目標分支（預設 main）
```

### 自託管 GitLab

額外設定 API URL：

```bash
export TA_GITLAB_API_URL=https://gitlab.internal.example.com
```

啟動時自動驗證 Token 有效性和專案存取權限。

## Helm 部署範例

```yaml
# values.yaml
tenantApi:
  env:
    TA_WRITE_MODE: "pr-github"
    TA_GITHUB_REPO: "org/config-repo"
    TA_GITHUB_BASE_BRANCH: "main"
  secretEnv:
    TA_GITHUB_TOKEN:
      secretName: tenant-api-vcs
      key: github-token
```

## 故障排查

| 症狀 | 可能原因 | 解法 |
|------|---------|------|
| 啟動時 `FATAL: token validation failed` | Token 權限不足或過期 | 確認 Token scope 正確、未過期 |
| PR 建立失敗 `403 Forbidden` | Token 缺少 repo 寫入權限 | GitHub: 需 `contents:write` + `pull_requests:write`；GitLab: 需 `api` scope |
| `FATAL: TA_GITHUB_TOKEN is required` | 未設定環境變數 | 確認 `TA_WRITE_MODE` 和對應 Token 環境變數都已設定 |
| PR 建立成功但 GitOps 未部署 | ArgoCD/Flux 未監聽目標分支 | 確認 GitOps controller 監聽的分支與 `*_BASE_BRANCH` 一致 |
| 自託管實例連線逾時 | `*_API_URL` 設定錯誤或網路不通 | 確認 URL 格式正確（含 `/api/v3` for GitHub Enterprise） |

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [ADR-011 PR-based Write-back](adr/011-pr-based-write-back.md) | ⭐⭐⭐ |
| [tenant-api README](../components/tenant-api/README.md) | ⭐⭐⭐ |
| [GitOps 部署指南](gitops-deployment.md) | ⭐⭐ |
