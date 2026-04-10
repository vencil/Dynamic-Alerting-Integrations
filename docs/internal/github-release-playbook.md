---
title: "GitHub Release — 操作手冊 (Playbook)"
tags: [documentation]
audience: [all]
version: v2.6.0
verified-at-version: v2.6.0
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

## Distribution Artifacts

每次 Release 會產出以下 artifacts（OCI registry + Docker images）：

| Artifact | 來源 | 對應 tag line |
|----------|------|---------------|
| `ghcr.io/vencil/threshold-exporter` | `cmd/threshold-exporter/` | `exporter/v*` |
| `ghcr.io/vencil/da-tools` | `scripts/tools/` | `tools/v*` |
| `ghcr.io/vencil/da-portal` | `portal/` | `portal/v*` |
| `ghcr.io/vencil/dynamic-alerting` (platform OCI) | Helm chart + Rule Packs | `v*` |
| `ghcr.io/vencil/tenant-api` | `cmd/tenant-api/` | `tenant-api/v*` |

推完 tag 後用 `gh api /users/vencil/packages?package_type=container` 驗證 package 是否更新，或直接在瀏覽器 <https://github.com/vencil?tab=packages> 確認。

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

### Step 2.5: ⛔ Pre-tag 品質閘門（硬性要求）

**所有檢查必須通過才能打 tag。** 跳過此步驟是 v2.4.0 re-tag 三輪的根本原因。

```bash
make version-check              # 版號一致性 — 必須 ✅
make lint-docs                  # 文件 lint — 必須 0 failed
pre-commit run --all-files      # auto hooks — 必須全過
make pre-tag                    # 一鍵整合（包含以上全部）
```

任何一項失敗 → 修正 → 重新驗證 → 才能進入 Step 3。

### Step 3: 建立 Tag

五條版號線各有對應 tag：

| 版號線 | Tag 格式 | 建立方式 | CI 觸發（release.yaml） |
|--------|---------|---------|---------|
| Platform (docs) | `v1.9.0` | `git tag v1.9.0` | **不觸發 build**（僅作 GitHub Release 錨點） |
| Exporter (Go) | `exporter/v1.8.0` | `make release-tag-exporter`（從 Chart.yaml 推導） | `release-exporter` job → Docker image + Helm chart |
| da-tools (Python) | `tools/v1.9.0` | `git tag tools/v1.9.0` | `release-da-tools` job → Docker image |
| da-portal (Static) | `portal/v2.0.0` | `make release-tag-portal` | `release-portal` job → Docker image |
| tenant-api (Go) | `tenant-api/v2.4.0` | `git tag tenant-api/v2.4.0` | `release-tenant-api` job → Docker image + Helm chart |

**Workflow 整併：** `release.yaml` 是唯一的 release workflow（`release-exporter.yaml` 和 `release-tools.yaml` 已刪除）。`v*` tag 不在 trigger 列表中，不會觸發任何 CI job。

**五線版號策略：** 五條獨立版號線（`v*` platform、`exporter/v*`、`tools/v*`、`portal/v*`、`tenant-api/v*`）各有各的生命週期。不是所有 component 每次都升版；僅推有 code change 的版號線。

```bash
# 情況 A：五線全升（所有 component 有變更）
git tag v<PLATFORM>
make release-tag-exporter   # 自動建 exporter/v<CHART_VER> tag
git tag tools/v<TOOLS>
git tag portal/v<PORTAL>
git tag tenant-api/v<TENANT_API>
git push origin v<PLATFORM> exporter/v<CHART_VER> tools/v<TOOLS> portal/v<PORTAL> tenant-api/v<TENANT_API>

# 情況 B：僅 platform + da-tools（其他 component 未變）
git tag v<PLATFORM>
git tag tools/v<TOOLS>
git push origin v<PLATFORM> tools/v<TOOLS>
# ⚠️ 不推未變更 component 的 tag — 版號不變時不推
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

## tenant-api 獨立 Release

tenant-api 有獨立版號線（`tenant-api/v*`），與 platform 脫鉤。

**檢查清單（每次 platform release 後）：**

```bash
# 1. 檢查 tenant-api 自上次 tag 以來是否有 code change
git diff $(git tag -l 'tenant-api/v*' --sort=-v:refname | head -1)..HEAD -- components/tenant-api/ helm/tenant-api/

# 2. 若有變更 → 推 tag
git tag "tenant-api/v<VERSION>"
git push origin "tenant-api/v<VERSION>"

# 3. 驗證 CI（tenant-api/v* 觸發 release.yaml 的 release-tenant-api job）
```

## 已知陷阱

> **環境層陷阱**（Docker exec、PowerShell 編碼、MCP timeout 等）集中在 [Windows-MCP Playbook § 已知陷阱速查](windows-mcp-playbook.md#已知陷阱速查)。本表僅列 Release 流程專屬的陷阱。

### PAT 權限矩陣

| Permission | Level | 用途 | 缺少時的症狀 |
|-----------|-------|------|------------|
| Contents | Read and write | git push, tag, **建立 Release** | push 被 reject / Release API 回 403 |
| Metadata | Read | 基礎 API 存取 | API 呼叫全部失敗 |
| Workflows | Read and write | push `.github/workflows/` | push 含 workflow 的 commit 被 reject |
| Packages | Read | 查詢 GHCR packages | GET packages 回 403（但 CI push 不受影響） |

### Release 流程陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | Cowork VM 無法存取 `api.github.com` | GitHub API 操作改走 Windows MCP。詳見 [Windows-MCP Playbook #31](windows-mcp-playbook.md) |
| 2 | `gh` CLI 無法安裝（github.com 403） | 用 Windows MCP PowerShell 直接呼叫 REST API |
| 3 | CI 未觸發 | 確認 tag 格式：`exporter/v*`（exporter）、`tools/v*`（da-tools）。`v*` 不觸發 build |
| 4 | Chart.yaml 版號不匹配 exporter tag | CI 有 version gate；先 `make version-check` |
| 5 | Token 洩漏到 repo | **嚴格禁止** — 只存 `~/.git-credentials`，session 結束消失 |
| 6 | API URL 用舊 repo 名 `vibe-k8s-lab` | git push 有重導，但 **REST API 必須用 `Dynamic-Alerting-Integrations`**。詳見 [Windows-MCP Playbook #24](windows-mcp-playbook.md) |
| 7 | PAT 無 `packages:read` → 查不到 packages | 不代表 push 失敗（CI 用 `GITHUB_TOKEN`）；瀏覽器驗證 |
| 8 | `bump_docs.py` 更新 da-tools 版號但沒推 tag | 每次 release 後用 `git diff` 檢查 da-tools code change（見上方檢查清單） |
| 9 | ~~`v*` tag 觸發 exporter build~~ | 🗄️ 已歸檔（v* 不再觸發 CI）。詳見 [archive/lessons-learned.md](archive/lessons-learned.md) |
| 10 | `replace_all` 批次改版號誤改跨元件版號 | 改完後 `bump_docs.py --check` 驗證；手動確認 exporter 版號未被誤改 |
| 11 | Release `already_exists`（tag 已被 CI 或先前操作建立） | 先 GET `/releases/tags/<tag>` 取 `id`，再 PATCH `/releases/<id>` 更新 name + body |
| 12 | 合併版號時遺漏語義更新 | 全局 sed 改版號後，需手動校正：CHANGELOG（合併 section）、da-tools 版號表（Git Tag + 說明）、architecture 底部版本戳（日期 + 功能摘要 + CLI 命令數） |
| 13 | 刪除遠端 tag 會連帶刪除關聯 Release | `git push origin :refs/tags/v*` 刪除 tag 後，GitHub 自動刪除該 tag 的 Release；重推 tag 後須重新 `POST /releases` 建立 |
| 14 | Re-tag 完整 SOP（同版號新 commit） | ① push main → ② 逐一刪遠端 tag → ③ 刪本地 tag → ④ 建新 tag on HEAD → ⑤ **逐一** push tag → ⑥ 重建 Release（因 #13 刪 tag 會刪 Release）→ ⑦ 重部署 GitHub Pages |
| 15 | ~~`mkdocs gh-deploy` 連續失敗~~ | 🗄️ 已歸檔（workaround 已轉移至 Windows-MCP #29-30）。詳見 [archive/lessons-learned.md](archive/lessons-learned.md) |
| 16 | `bump_docs.py` 漏網規則 | 每次 release 前先跑 `bump_docs.py --what-if` 審計所有規則。新增 component 時須同步加入版號線（`--tenant-api` 等） |
| 17 | Rule Pack 計數 14 vs 15 混淆 | `rule-packs/` yaml 檔案 = 14（optional Projected Volume），`platform-data.json` = 15（含 platform ConfigMap）。**總數以 `platform-data.json` 為準（15）** |
| 18 | Cowork VM mount 製造 phantom lock | 詳見 [Windows-MCP Playbook #27](windows-mcp-playbook.md)。Release 場景的 workaround：fresh `git clone --depth 1` 到暫存目錄做 tag 操作 |
| 19 | 新 component 上線遺漏版號工具 | 新增 component 時，除了 `release.yaml` 加 job，還須：① `bump_docs.py` 加版號線 ② `validate_docs_versions.py` 加規則 ③ Dockerfile base image 驗證 |

### Follow-up PR / Stacked PR 陷阱

當需要連續開多個小 PR 修 CI（例如從一個大 PR 的 triage 結果拆出 N 個 root-cause 修復），很容易踩到 GitHub 本身的「stacked PR」邊角案例。以下陷阱在 v2.6.0 CI 穩定化期間實測確認。

| # | 陷阱 | 根因 | 解法 |
|---|------|------|------|
| 20 | `gh pr merge --squash --delete-branch` 會**自動關閉** stacked 在其上的 child PR | GitHub 在 base branch 被刪時把依賴它的 PR 置為 `CLOSED`（不是 retarget），且該狀態的 PR **無法用 `gh pr reopen`** 重開（GraphQL 回 `Could not open the pull request`） | ① merge parent 時**不要**用 `--delete-branch`，先手動 `gh pr edit <child> --base main` 把 child 改 target 到 main，再 delete parent branch；或 ② 預期 child 會被關，直接用相同 head branch `gh pr create` 開新 PR（branch 還在 origin，commits 不會丟） |
| 21 | Stacked PR 的 `docs-ci.yaml`（以及任何 `pull_request.branches: [main]` filter 的 workflow）**完全不觸發** | PR base 不是 main → `branches:` filter 把事件全部濾掉。只有無 filter 的 workflow（如 `commitlint.yaml`）會跑 | 驗證 stacked PR 的 docs-ci 唯一路徑 = 先 merge parent。規避：stacked PR 不要期待在 merge 前跑到完整 docs-ci；用 parent PR 上的相同 commit tree 當 proof（amend 只改 message、不改 tree 時，先前的 CI 結果仍然有效） |
| 22 | `commitlint.yaml` 的 `scope-enum` allowlist 不含 `lint` | `.commitlintrc.yaml` 只允許 `exporter, tools, docs, rule-packs, ci, k8s, helm`。寫 `fix(lint): ...` 會被 commitlint 擋下並失敗 | 修 `scripts/tools/lint/` 底下的檔案用 `fix(tools): ...`（檔案在 `tools/lint/` 子樹，scope 取父目錄）；PR title + branch 上的 commit message **兩處都要改**——`commitlint.yaml` 有兩個 step，第一個驗 PR title，第二個用 `commitlint --from base.sha --to head.sha` 逐 commit 驗 |
| 23 | 改 PR title 後 commitlint 不重跑 | `commitlint.yaml` 的 `pull_request.types` 只列 `[opened, synchronize, reopened]`，**沒有 `edited`** | `gh pr edit --title` 後需要額外觸發 `synchronize` 事件：① `git commit --amend` 改 commit message 符合 scope 規則 → ② `git push --force-with-lease --no-verify` → synchronize 事件會同時觸發 commitlint 重跑和逐 commit 檢查 |
| 24 | Rebase stacked branch 到 main 時 parent 的 commit「消失」 | Parent 已 squash-merge 進 main，squash commit 和原 branch 的 commit SHA 不同。Git rebase 偵測 tree 相同 → skip (`skipped previously applied commit`) | 這是**預期行為**，不是錯誤。`git log --oneline` 應該只剩 child 自己的 commit + main HEAD（squash commit）。`--reapply-cherry-picks` 反而會造成 duplicate |

**操作範例**（來自 v2.6.0 期間的 PR #3 → PR #4 → PR #5 事件）：

```bash
# 錯誤路徑（實際發生）：
gh pr merge 3 --squash --delete-branch --admin      # ← --delete-branch 砍了 stacked PR #4 的 base
gh pr reopen 4                                       # ← GraphQL: Could not open the pull request
# 不得不開替代 PR #5

# 正確路徑（未來照做）：
gh pr edit 4 --base main                             # 先把 child retarget 到 main
gh pr merge 3 --squash --admin                       # 不帶 --delete-branch
git push origin --delete ci/fix-docs-workflow-cli-drift  # 事後手動刪 parent branch
# PR #4 會進入 synchronize 事件並跑完整 docs-ci（base=main 通過 branches filter）
```

> **PowerShell 環境陷阱**（JSON body 編碼、CJK 亂碼、PSObject 序列化、長 body timeout、`Invoke-RestMethod` timeout、BOM 問題）統一見 [Windows-MCP Playbook § PowerShell REST API](windows-mcp-playbook.md#powershell-rest-apigithub-等) 及 [§ 長 Body 的建議做法](windows-mcp-playbook.md#長-body-的建議做法)。

## 上版前品質驗證清單（Pre-release Checklist）

每次大版號 release 前，依序執行以下檢查。此清單同時作為 AI Agent 的標準操作程序。

### Phase 1: 資產完整性

1. 🛡️ **doc-map / tool-map / test-map 同步** `[已自動化於 hook: doc-map-check, tool-map-check]`
   ```bash
   python3 scripts/tools/dx/generate_doc_map.py --check --include-adr
   python3 scripts/tools/dx/generate_tool_map.py --check
   # test-map: 確認 tests/ 下每個 test_*.py 都有對應的 source module
   ```
   目標：無孤兒文件（doc-map 有但實際不存在）、無遺漏文件（存在但 map 未收錄）

2. **test-coverage-matrix.md 現況更新**（手動）
   確認「進階場景與測試覆蓋」中的場景列表、測試數量、工具引用是否反映最新版本。

3. **Backlog + Roadmap 衛生**（手動）
   - `dx-tooling-backlog.md`：只保留未完成項目。已交付功能從 backlog 徹底刪除（CHANGELOG 是唯一的交付紀錄）
   - `roadmap-future.md`：只保留「計畫中」和「探索方向」。已完成項目不留存——版本演進表和 CHANGELOG 負責展示歷史
   - `architecture-and-design.md` §5 摘要表：同步更新，確保與 roadmap 一致
   - 可新增本版開發過程發現的未來方向

### Phase 2: 文件品質維護

> **設計原則**：每份文件有明確職責，不重複。README 做「為什麼 + 怎麼開始」，Roadmap 做「接下來」，CHANGELOG 做「做過什麼」，Playbook 做「怎麼不踩坑」。文件膨脹的根因是職責模糊——同一件事在多處描述。

4. **文件簡潔性檢查**（部分自動化：`check_doc_reading_time` manual-stage hook）
   - README（root）：維持 ~190 行以內，「5s→30s→5min」漸進式揭露結構完整
   - `docs/index.md`：維持 ~140 行以內，專注 MkDocs 導航入口角色
   - `architecture-and-design.md`：Hub 文件 ≤ 250 行，細節在 spoke 文件
   - 新增內容時先問：「這屬於哪份文件的職責？」而非「放哪裡最方便？」

5. 🛡️ **敘述風格一致性** `[部分自動化於 hook: repo-name-check, bilingual-structure-check]`
   - 禁止代號/暗語進入文件（如「場景 A/B」）——用描述性名稱（如「中央評估/邊緣評估」）
   - 禁止推銷語言進入 repo（CLAUDE.md 規範 #6）
   - 數字引用（場景數、工具數、Rule Pack 數）與 `platform-data.json` / `tool-registry.yaml` 對齊

6. **Playbook 經驗回寫**
   回顧踩坑經驗，只記錄**跨版本可復用**的教訓。判斷標準：「下次開發時如果沒看到這條會再踩一次嗎？」是才寫。不為寫而寫。

### Phase 3: 架構文件一致性

7. **architecture-and-design.md + ADR 審核**
   - 確認 arch doc 與 ADR 不過度重疊（arch doc 放設計概覽，ADR 放決策紀錄與取捨）
   - 確認 arch doc 所有 §N 編號連續且 Mermaid 圖同步
   - 若 arch doc 超過 1,200 行，評估拆分或精煉

8. 🛡️ **README 連結 + 導航完整性** `[已自動化於 hook: doc-links-check]`
   - `check_doc_links.py` 零 broken links
   - README 的「為什麼要用」和「怎麼開始」路徑清晰
   - 場景數、工具數、Rule Pack 數等數字與實際一致

9. 🛡️ **CLAUDE.md 準確性** `[已自動化於 hook: version-consistency]`
   - 工具數量、場景數、CLI 命令數與 CHANGELOG 一致
   - Playbook 引用路徑正確
   - 新增概念有被收錄

### Phase 4: 版號 + 品質閘門

10. 🛡️ **版號治理**（⛔ 硬性要求）`[已自動化於 hook: version-consistency + check_frontmatter_versions]`
    ```bash
    make version-check              # 全 repo 版號一致性 — 必須 ✅
    make bump-docs                  # 若需更新 — 必須完全覆蓋
    check_frontmatter_versions.py --fix   # frontmatter 批次更新 — 必須 0 failed
    ```
    **任何版號不一致進入下一步都是致命風險。必須全數修正才能推送。**

11. **品質閘門**
    ```bash
    pre-commit run --all-files                           # 30 auto hooks
    pre-commit run --hook-stage manual --all-files        # 10 manual hooks
    python -m pytest tests/ --ignore=tests/test_property.py --ignore=tests/test_benchmark.py -q
    ```

### Phase 5: 收尾

12. **Rebase 為單一 commit**
    將本版所有 WIP commit 合併為一個語義完整的 release commit。CHANGELOG 以全局角度更新，不囉嗦不遺漏。

13. **CHANGELOG 真實性檢查**
    逐條確認 CHANGELOG 提到的功能確實存在、數字準確、檔案路徑可訪問。

14. **等待 Owner 確認**
    停下來等主人 review + 提供臨時 GitHub token。Token 不記錄到任何 repo 檔案。

15. **推送前：驗證 base image + Chart.yaml**
    ```bash
    # Dockerfile base image 必須在 Docker Hub 存在（CI build 階段才會 fail，太遲了）
    docker manifest inspect <每個 Dockerfile 的 FROM tag> > /dev/null
    # Chart.yaml version 必須與即將推的 exporter/v* tag 一致
    grep "^version:" components/threshold-exporter/Chart.yaml
    ```

16. **推送 + 等 CI 全綠 + Release**
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
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 