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

因 Cowork VM 無法存取 `api.github.com`，透過 Windows MCP PowerShell 建立。

**⚠️ Repo 名稱：** GitHub 已從 `vibe-k8s-lab` 重導至 `Dynamic-Alerting-Integrations`。git push 有自動重導，但 **API URL 必須用新名稱**，否則回空結果。

**⚠️ PowerShell JSON：** 用單行字串賦值，**不要用** `@{} | ConvertTo-Json`（`&` 字元問題）或 heredoc（quote mangling）。

```powershell
$token = "<TOKEN>"
$headers = @{ "Authorization" = "Bearer $token"; "Accept" = "application/vnd.github+json"; "X-GitHub-Api-Version" = "2022-11-28"; "Content-Type" = "application/json" }
$b = '{"tag_name":"v1.8.0","name":"v1.8.0 — Release Title","body":"Release notes...","draft":false,"prerelease":false}'
Invoke-RestMethod -Uri "https://api.github.com/repos/vencil/Dynamic-Alerting-Integrations/releases" -Method Post -Headers $headers -Body $b
```

### Step 5: 驗證

```powershell
# CI workflow 狀態（Windows MCP）
Invoke-RestMethod -Uri "https://api.github.com/repos/vencil/Dynamic-Alerting-Integrations/actions/runs?per_page=3" -Headers $headers

# Release 確認
Invoke-RestMethod -Uri "https://api.github.com/repos/vencil/Dynamic-Alerting-Integrations/releases/latest" -Headers $headers

# ⚠️ Packages 查詢需要 PAT 有 packages:read scope
# 若 403 "Resource not accessible"，package 仍可能已成功推送（CI 用 GITHUB_TOKEN 有 packages:write）
# → 直接在瀏覽器 https://github.com/vencil?tab=packages 驗證
```

## da-tools 獨立 Release

da-tools 有獨立版號線（`tools/v*`），與 platform 脫鉤。

**⚠️ `bump_docs.py` 陷阱：** `bump_docs.py` 會把所有文件中的 `da-tools:<OLD>` 替換為新版號，但**不會自動建立 `tools/v*` tag**。若 da-tools 有 code change（`entrypoint.py`、新命令映射等），必須手動推 tag，否則文件引用指向不存在的 image。

**檢查清單（每次 platform release 後）：**

```bash
# 1. 檢查 da-tools 自上次 tools/v* tag 以來是否有 code change
git diff $(git tag -l 'tools/v*' --sort=-v:refname | head -1)..HEAD -- components/da-tools/app/

# 2. 若有變更 → 推 tag
git tag "tools/v<VERSION>"
git push origin "tools/v<VERSION>"

# 3. 驗證 CI（tools/v* 會同時觸發 Release + Publish da-tools 兩個 workflow，
#    其中 release-exporter 正確 skip，build-and-push-image 應 success）
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
| 7 | API URL 用舊 repo 名 `vibe-k8s-lab` | git push 有重導，但 **REST API 必須用 `Dynamic-Alerting-Integrations`** |
| 8 | PowerShell `@{} \| ConvertTo-Json` 出錯 | 用單行 `$b = '{"key":"value"}'` 字串賦值 |
| 9 | PAT 無 `packages:read` → 查不到 packages | 不代表 push 失敗（CI 用 `GITHUB_TOKEN`）；瀏覽器驗證 |
| 10 | `bump_docs.py` 更新 da-tools 版號但沒推 tag | 每次 release 後用 `git diff` 檢查 da-tools code change（見上方檢查清單） |
