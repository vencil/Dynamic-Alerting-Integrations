# GitHub Release — 操作手冊 (Playbook)

> AI Agent 透過 Cowork VM + Windows MCP 執行 git push、建立 GitHub Release 的流程與限制。
> **相關文件：** [Testing Playbook](testing-playbook.md) | [Windows-MCP Playbook](windows-mcp-playbook.md)

## ⚠️ 安全規則

**絕對禁止將 GitHub token 寫入任何 repo 檔案。** 包含但不限於：
- 本 playbook、CLAUDE.md、任何 `.md` / `.yaml` / `.sh` / `.py` 檔案
- Git commit message、PR body、Release body
- 腳本內 hardcoded 字串

Token 只能存在 VM 的 `~/.git-credentials` 和 `~/.bashrc`（session 結束即消失）。

## 環境限制

| 操作 | Cowork VM | Windows MCP |
|------|-----------|-------------|
| `git push` / `git tag` | ✅ HTTPS 直連 github.com | ✅ 但不必要 |
| GitHub API (create release) | ❌ sandbox proxy 擋 `api.github.com` | ✅ PowerShell `Invoke-RestMethod` |
| `gh` CLI 安裝 | ❌ `github.com` 下載被 403 | ✅ 但非必要 |
| `ghapi` Python library | ✅ 可安裝，但 API 呼叫被擋 | N/A |

**結論：** git 操作在 Cowork VM 做，GitHub API 操作透過 Windows MCP 做。

## 認證設定

使用者需提供 GitHub Fine-grained PAT，需要的 permissions：

| Permission | Level | 用途 |
|-----------|-------|------|
| Contents | Read and write | git push, tag |
| Metadata | Read | 基礎 API 存取 |

> **注意：** GitHub Release 由 CI workflow（`release.yaml`）的 `GITHUB_TOKEN` 建立，或由 Agent 透過 Windows MCP + 使用者 PAT 建立。若 PAT 未包含 Contents write，CI 的 `packages: write` 也足以推送 image/chart。

設定流程（在 Cowork VM 內）：

```bash
# 使用者提供 token 後，Agent 執行：
git config --global credential.helper store
echo "https://<USERNAME>:<TOKEN>@github.com" > ~/.git-credentials
export GITHUB_TOKEN=<TOKEN>
```

驗證：
```bash
git push --dry-run origin main   # 應回 "Everything up-to-date"
git ls-remote --heads origin     # 應列出 remote branches
```

## Release 標準流程

### Step 1: 版號驗證

```bash
make version-check        # 確認全 repo 版號一致
```

### Step 2: Commit & Push

```bash
git add <files>
git commit -m "..."
git push origin main
```

### Step 3: 建立 Tag

```bash
make release-tag          # 從 Chart.yaml 推導 tag（禁止手動 git tag）
git push origin v<VERSION>
```

推送 `v*` tag 後，GitHub Actions `release.yaml` 自動：
- Build + push `ghcr.io/vencil/threshold-exporter:<VERSION>` Docker image
- Package + push `oci://ghcr.io/vencil/charts/threshold-exporter:<VERSION>` Helm chart

推送 `tools/v*` tag 後，自動 build + push `ghcr.io/vencil/da-tools:<VERSION>`。

### Step 4: 建立 GitHub Release（透過 Windows MCP）

因 Cowork VM 無法存取 `api.github.com`，透過 Windows MCP PowerShell 建立：

```powershell
$token = "<TOKEN>"   # 從 VM 環境變數取得，不 hardcode
$headers = @{
    "Authorization" = "Bearer $token"
    "Accept" = "application/vnd.github+json"
}
$body = @{
    tag_name = "v1.8.0"
    name = "v1.8.0 — Release Title"
    body = "Release notes in markdown..."
    draft = $false
    prerelease = $false
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "https://api.github.com/repos/vencil/vibe-k8s-lab/releases" `
    -Method Post -Headers $headers -Body $body -ContentType "application/json"
```

### Step 5: 驗證

```bash
# CI workflow 狀態
# → 在 Windows MCP 查詢：
# Invoke-RestMethod "https://api.github.com/repos/vencil/vibe-k8s-lab/actions/runs?per_page=3"

# Release 確認
# → 在 Windows MCP 查詢：
# Invoke-RestMethod "https://api.github.com/repos/vencil/vibe-k8s-lab/releases/latest"
```

## da-tools 獨立 Release

da-tools 有獨立版號線（`tools/v*`），與 platform 脫鉤：

```bash
# 更新 components/da-tools/VERSION
git tag "tools/v<VERSION>"
git push origin "tools/v<VERSION>"
```

## 已知陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | Cowork VM 無法存取 `api.github.com` | GitHub API 操作改走 Windows MCP |
| 2 | `gh` CLI 無法安裝（github.com 403） | 用 Windows MCP PowerShell 直接呼叫 REST API |
| 3 | PAT 權限不足 push | Fine-grained PAT 需要 Contents: Read and write |
| 4 | CI 未觸發 | 確認 tag 格式為 `v*`（非 `V*` 或其他） |
| 5 | Chart.yaml 版號不匹配 tag | CI 有 version gate；先 `make version-check` |
| 6 | Token 洩漏到 repo | **嚴格禁止** — 只存 `~/.git-credentials`，session 結束消失 |
