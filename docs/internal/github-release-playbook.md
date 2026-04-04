---
title: "GitHub Release — 操作手冊 (Playbook)"
tags: [documentation]
audience: [all]
version: v2.3.0
lang: zh
---
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
| Workflows | Read and write | push `.github/workflows/` 檔案 |

> **注意：** 沒有 `workflows` scope 的 PAT 可以 push 一般程式碼，但 push 含 `.github/workflows/` 變更的 commit 會被 reject：`refusing to allow a Personal Access Token to create or update workflow ... without workflow scope`。
>
> GitHub Release 由 CI workflow（`release.yaml`）的 `GITHUB_TOKEN` 建立，或由 Agent 透過 Windows MCP + 使用者 PAT 建立。若 PAT 未包含 Contents write，CI 的 `packages: write` 也足以推送 image/chart。

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

四條版號線各有對應 tag：

| 版號線 | Tag 格式 | 建立方式 | CI 觸發（release.yaml） |
|--------|---------|---------|---------|
| Platform (docs) | `v1.9.0` | `git tag v1.9.0` | **不觸發 build**（僅作 GitHub Release 錨點） |
| Exporter (Go) | `exporter/v1.8.0` | `make release-tag-exporter`（從 Chart.yaml 推導） | `release-exporter` job → Docker image + Helm chart |
| da-tools (Python) | `tools/v1.9.0` | `git tag tools/v1.9.0` | `release-da-tools` job → Docker image |
| da-portal (Static) | `portal/v2.0.0` | `make release-tag-portal` | `release-portal` job → Docker image |

**Workflow 整併：** `release.yaml` 是唯一的 release workflow（`release-exporter.yaml` 和 `release-tools.yaml` 已刪除）。`v*` tag 不在 trigger 列表中，不會觸發任何 CI job。

```bash
# 情況 A：四線全升（exporter + portal 有變更）
git tag v<PLATFORM>
make release-tag-exporter   # 自動建 exporter/v<CHART_VER> tag
git tag tools/v<TOOLS>
git tag portal/v<PORTAL>
git push origin v<PLATFORM> exporter/v<CHART_VER> tools/v<TOOLS> portal/v<PORTAL>

# 情況 B：僅 platform + da-tools（exporter / portal 未變）
git tag v<PLATFORM>
git tag tools/v<TOOLS>
git push origin v<PLATFORM> tools/v<TOOLS>
# ⚠️ 不推 exporter / portal tag — 版號不變時不推
```

### Step 4: 建立 GitHub Release（透過 Windows MCP）

因 Cowork VM 無法存取 `api.github.com`，透過 Windows MCP PowerShell 建立。

**⚠️ Repo 名稱：** GitHub 已從 `vibe-k8s-lab` 重導至 `Dynamic-Alerting-Integrations`。git push 有自動重導，但 **API URL 必須用新名稱**，否則回空結果。

**PowerShell JSON 兩種可靠做法：**

```powershell
$token = "<TOKEN>"
$headers = @{ "Authorization" = "token $token"; "Accept" = "application/vnd.github+json" }

# 方法 A：單行字串 — 適合短 body、純 ASCII
$b = '{"tag_name":"v1.8.0","name":"Release Title","body":"short notes","draft":false,"prerelease":false}'
Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $b

# 方法 B：ConvertTo-Json + UTF8 Bytes — 適合長 body、CJK 字元
$payload = @{
    tag_name = "v1.9.0"
    name = "v1.9.0 — 遷移全鏈自動化"
    body = $bodyText   # 可用 @"..."@ heredoc 賦值
    draft = $false
    prerelease = $false
} | ConvertTo-Json -Depth 3
Invoke-RestMethod -Uri $url -Method Post -Headers $headers `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
    -ContentType "application/json; charset=utf-8"
```

> **方法 B 關鍵：** 必須用 `[System.Text.Encoding]::UTF8.GetBytes()` 轉換 body，並顯式指定 `charset=utf-8`，否則 CJK 字元會被 PowerShell 以系統 codepage 編碼導致亂碼。`&` 字元在 `ConvertTo-Json` 中會被正確 escape。

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

# 3. 驗證 CI（tools/v* 觸發 release.yaml 的 release-da-tools job）
```

## 已知陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | Cowork VM 無法存取 `api.github.com` | GitHub API 操作改走 Windows MCP |
| 2 | `gh` CLI 無法安裝（github.com 403） | 用 Windows MCP PowerShell 直接呼叫 REST API |
| 3 | PAT 權限不足 push | Fine-grained PAT 需要 Contents: Read and write + Workflows: Read and write |
| 4 | CI 未觸發 | 確認 tag 格式：`exporter/v*`（exporter）、`tools/v*`（da-tools）。`v*` 不觸發 build |
| 5 | Chart.yaml 版號不匹配 exporter tag | CI 有 version gate；先 `make version-check` |
| 6 | Token 洩漏到 repo | **嚴格禁止** — 只存 `~/.git-credentials`，session 結束消失 |
| 7 | API URL 用舊 repo 名 `vibe-k8s-lab` | git push 有重導，但 **REST API 必須用 `Dynamic-Alerting-Integrations`** |
| 8 | PowerShell JSON body 編碼問題 | 短 body 用單行字串；長 body / CJK 用 `ConvertTo-Json` + `UTF8.GetBytes()` |
| 9 | PAT 無 `packages:read` → 查不到 packages | 不代表 push 失敗（CI 用 `GITHUB_TOKEN`）；瀏覽器驗證 |
| 10 | `bump_docs.py` 更新 da-tools 版號但沒推 tag | 每次 release 後用 `git diff` 檢查 da-tools code change（見上方檢查清單） |
| 11 | PAT 缺 `workflow` scope → push `.github/workflows/` 被 reject | PAT 需含 Workflows: Read and write |
| 12 | `v*` tag 觸發 exporter build 但版號不匹配 | 已修正：`v*` 不再觸發 CI；exporter 改用 `exporter/v*` tag |
| 13 | `replace_all` 批次改版號誤改跨元件版號 | 改完後 `bump_docs.py --check` 驗證；手動確認 exporter 版號未被誤改 |
| 14 | Release `already_exists`（tag 已被 CI 或先前操作建立） | 先 GET `/releases/tags/<tag>` 取 `id`，再 PATCH `/releases/<id>` 更新 name + body |
| 15 | Windows MCP Shell 長 body timeout | 用 Desktop Commander `write_file` 寫暫存檔 → PowerShell `Get-Content -Raw` 讀入（⚠️ 須 `[string]` cast，見 #17）→ 結束後刪暫存 |
| 16 | 合併版號時遺漏語義更新 | 全局 sed 改版號後，需手動校正：CHANGELOG（合併 section）、da-tools 版號表（Git Tag + 說明）、architecture 底部版本戳（日期 + 功能摘要 + CLI 命令數） |
| 17 | 刪除遠端 tag 會連帶刪除關聯 Release | `git push origin :refs/tags/v*` 刪除 tag 後，GitHub 自動刪除該 tag 的 Release；重推 tag 後須重新 `POST /releases` 建立 |
| 18 | `Get-Content -Raw` 回傳 PSObject 非純字串 | 放入 hashtable → `ConvertTo-Json` 會序列化 filesystem metadata（PSPath/PSDrive/PSProvider 數千行）；須 `[string]` cast 或改用 here-string `@"..."@`（詳見 [Windows MCP Playbook](windows-mcp-playbook.md#長-body-的建議做法)） |
| 19 | Repo rename 導致 Release API POST 靜默失敗 | 改名後 GET 自動 redirect，但 POST 回 307 且 `Invoke-RestMethod` 不跟隨 POST redirect（靜默回 401）。必須用**新 repo name** 或 **repo ID URL**（`/repositories/{id}/releases`）。詳見 [Windows MCP Playbook #24](windows-mcp-playbook.md) |
| 20 | Fine-grained PAT 預設無 Release 寫入權限 | 需在 GitHub Settings → Fine-grained PAT → Permissions 加上 **Contents: Read and Write** 才能建立 Release |
| 21 | Re-tag 完整 SOP（同版號新 commit） | ① push main → ② 逐一刪遠端 tag（`git push origin --delete <tag>`，v2.1.0 可能已被刪，逐一操作避免 `--delete` 批次錯誤中斷）→ ③ 刪本地 tag（`git tag -d`）→ ④ 建新 tag on HEAD → ⑤ **逐一** push tag（避免同時推送不觸發 CI）→ ⑥ 重建 Release（因 #17 刪 tag 會刪 Release）→ ⑦ 重部署 GitHub Pages |
| 22 | `Invoke-RestMethod` 在 Windows MCP 下頻繁 timeout | 原因：PowerShell 模組初始化 + TLS 協商累計超過 MCP 60s timeout。改用 `curl.exe`：JSON body 用 `[IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))` 寫無 BOM 暫存 → `curl.exe --data-binary @file`。注意 `Set-Content` 預設加 BOM 會導致 `Problems parsing JSON` |
| 23 | `mkdocs gh-deploy` 連續失敗（site/ 權限 + ghp_import bytes bug） | `site/` 一旦建出 Cowork VM 無法清除（`Operation not permitted`），須 Windows MCP `Remove-Item` 清理。`ghp_import` 在 Python 3.10 下有 `TypeError: write() argument must be str, not bytes`；Workaround：手動建 temp git repo → 複製 `site/*` → commit → push `gh-pages --force` |

## 上版前品質驗證清單（Pre-release Checklist）

每次大版號 release 前，依序執行以下檢查。此清單同時作為 AI Agent 的標準操作程序。

### Phase 1: 資產完整性

1. **doc-map / tool-map / test-map 同步**
   ```bash
   python3 scripts/tools/dx/generate_doc_map.py --check --include-adr
   python3 scripts/tools/dx/generate_tool_map.py --check
   # test-map: 確認 tests/ 下每個 test_*.py 都有對應的 source module
   ```
   目標：無孤兒文件（doc-map 有但實際不存在）、無遺漏文件（存在但 map 未收錄）

2. **advanced-scenarios.md 現況更新**
   確認「進階場景與測試覆蓋」中的場景列表、測試數量、工具引用是否反映最新版本。

3. **dx-tooling-backlog.md + architecture §5 Roadmap**
   已完成項目只能在 CHANGELOG 和功能文件中出現，不能殘留在 backlog / roadmap。可新增 Agent 發想的未來方向。

### Phase 2: 經驗回寫

4. **Playbook 經驗回寫**
   回顧踩坑經驗，只記錄**跨版本可復用**的教訓。判斷標準：「下次開發時如果沒看到這條會再踩一次嗎？」是才寫。不為寫而寫。

### Phase 3: 架構文件一致性

5. **architecture-and-design.md + ADR 審核**
   - 確認 arch doc 與 ADR 不過度重疊（arch doc 放設計概覽，ADR 放決策紀錄與取捨）
   - 確認 arch doc 所有 §N 編號連續且 Mermaid 圖同步
   - 若 arch doc 超過 1,200 行，評估拆分或精煉

6. **README 連結 + 價值傳達**
   - `check_doc_links.py` 零 broken links
   - README 對客戶的「為什麼要用」和「怎麼開始」路徑清晰
   - 場景數、工具數、Rule Pack 數等數字與實際一致

7. **CLAUDE.md 準確性**
   - 工具數量、場景數、CLI 命令數與 CHANGELOG 一致
   - Playbook 引用路徑正確
   - 新增概念（如 GitOps Native Mode）有被收錄

### Phase 4: 版號 + 品質閘門

8. **版號治理**
   ```bash
   make version-check              # 全 repo 版號一致性
   make bump-docs                  # 若需更新
   check_frontmatter_versions.py --fix   # frontmatter 批次更新
   ```

9. **品質閘門**
   ```bash
   pre-commit run --all-files                           # 13 auto hooks
   pre-commit run --hook-stage manual --all-files        # 18 manual hooks
   python -m pytest tests/ --ignore=tests/test_property.py --ignore=tests/test_benchmark.py -q
   ```

### Phase 5: 收尾

10. **Rebase 為單一 commit**
    將本版所有 WIP commit 合併為一個語義完整的 release commit。CHANGELOG 以全局角度更新，不囉嗦不遺漏。

11. **CHANGELOG 真實性檢查**
    逐條確認 CHANGELOG 提到的功能確實存在、數字準確、檔案路徑可訪問。

12. **等待 Owner 確認**
    停下來等主人 review + 提供臨時 GitHub token。Token 不記錄到任何 repo 檔案。

13. **推送前：驗證 base image + Chart.yaml**
    ```bash
    # Dockerfile base image 必須在 Docker Hub 存在（CI build 階段才會 fail，太遲了）
    docker manifest inspect <每個 Dockerfile 的 FROM tag> > /dev/null
    # Chart.yaml version 必須與即將推的 exporter/v* tag 一致
    grep "^version:" components/threshold-exporter/Chart.yaml
    ```

14. **推送 + 等 CI 全綠 + Release**
    - `git push origin main` + 推對應 tag
    - **等所有 Release workflow 完成並 success 後**才建 GitHub Release
    - 若 CI 失敗：修正 → amend → force-push → 刪遠端 tag → 重推 tag
    - GitHub Pages 部署確認
    - 建立 GitHub Release（英文敘述）

## 版號合併流程

多版本未對外釋出時可合併為單一版號。步驟：

1. **CHANGELOG**: 合併 section 為一個條目（feature 按邏輯分組、da-tools CLI 命令數累加、測試數取最終值）
2. **全局替換**: `sed -i 's/OLD/NEW/g'` 所有 `.md`、VERSION 檔案（排除 CHANGELOG，需手動合併）
3. **語義校正**（sed 無法自動處理）：
   - da-tools README 版號策略表：Platform Git Tag、da-tools 說明（累加新命令）
   - architecture-and-design 底部版本戳：日期、功能摘要、CLI 命令數區間
   - CHANGELOG 測試表：基線版對齊前一版（如 → v1.10.0，非 → v1.10.0）
4. **驗證**: `grep -rn "OLD_VERSION"` → 0 命中；`bump_docs.py --check` → ✅

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["GitHub Release — 操作手冊 (Playbook)"](github-release-playbook.md) | ⭐⭐ |
| ["測試注意事項 — 排錯手冊 (Testing Playbook)"](testing-playbook.md) | ⭐⭐ |
| ["Windows-MCP — Dev Container 操作手冊 (Playbook)"](windows-mcp-playbook.md) | ⭐⭐ |
