---
title: "Windows Clone Handoff — 2026-04-12"
tags: [resume, handoff, internal]
audience: [maintainers]
version: v2.6.0
lang: zh
---

# Windows Clone Handoff Walkthrough

> 本份是 `_resume-2026-04-12.md` 的執行手冊。所有需要 **真實 git / gh 操作**
> 的步驟都在這裡，照順序跑就行。預設環境：Windows + Git Bash（如果你只
> 有 PowerShell，相容語法會另外標註）。
>
> 預估總時間：專心跑不被打斷約 30–45 分鐘（含跑 pre-commit 和 CI 等待）。

## 前置檢查（5 分鐘）

### 1. 確認 Developer Mode 已開啟（避免 symlink 物化）

```powershell
# 在 PowerShell（不需要 admin）查
Get-ItemProperty `
  "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" `
  -Name AllowDevelopmentWithoutDevLicense 2>$null
```

- 回 `AllowDevelopmentWithoutDevLicense : 1` → 已開 ✅
- 回空或 `0` → 去 **設定 → 隱私權與安全性 → 開發人員專用 → 開發人員模式** 打開，然後**整個重新 clone** repo（已經 clone 的 working tree 不會追溯）

> ⚠️ 如果 `rule-packs/` 下的 `.yaml` 看起來只有 ~13 bytes，代表 symlink 物化了，
> 必須重 clone 才能修。這不是資料遺失——實體檔都在 repo 裡，但 working tree 要
> 重建。

### 2. 確認 `gh` 已登入且有足夠權限

```bash
gh auth status
```

- 期待輸出包含 `Logged in to github.com as <你的帳號>` 以及 `Token scopes: ...`
- 需要的 scope：`repo`（足夠 push + PR）、`workflow`（push 到 `.github/workflows/`）、`admin:repo_hook` 或 repo admin（Priority D 鎖 required checks 用）

如果 scope 不夠：

```bash
gh auth refresh -h github.com -s repo,workflow,admin:repo_hook
```

### 3. 確認 pre-commit 已經在 Windows 側裝好

```bash
pre-commit --version   # 應該 >= 3.0
```

如果沒裝：

```bash
pip install --user pre-commit
# 或者
pipx install pre-commit
```

### 4. 找到你的 Windows clone

本專案預設路徑是 `C:\tmp\vibe-clone-pr14`（見 `_resume-2026-04-11-v2.md`）。
如果你已經切到別的路徑，替換下面所有 `$CLONE` 為你實際的路徑即可。

```bash
# Git Bash
export CLONE="/c/tmp/vibe-clone-pr14"
cd "$CLONE"
git status          # 應該顯示 clean 或只有未追蹤檔
git log -1 --oneline
```

PowerShell 版本：

```powershell
$CLONE = "C:\tmp\vibe-clone-pr14"
Set-Location $CLONE
git status
```

## 步驟 A — 同步 main 並開新分支（2 分鐘）

```bash
cd "$CLONE"

# 先把 main 拉新
git fetch origin main
git checkout main
git pull --ff-only origin main

# 確認乾淨沒 local 改動
git status
# working tree clean 才繼續

# 開新分支
git checkout -b chore/doc-coverage-hygiene-sweep
```

## 步驟 B — 從 Cowork FUSE 取修改檔案（10 分鐘）

**最穩的做法**：把修改過的檔案**列出來**，逐檔從 Cowork mount 路徑複製到 Windows clone。
不要用 `rsync` 整個目錄——會把產生檔（`site/`、`.pytest_cache/` 等）也拉過來，容易翻車。

### B-1. 修改清單（14 類，共 60+ 檔）

工具層（4 檔）：

```
scripts/tools/dx/doc_coverage.py
scripts/tools/dx/generate_tool_map.py
scripts/tools/dx/generate_doc_map.py
scripts/tools/dx/add_frontmatter.py
```

Internal 文件（6 檔）：

```
CLAUDE.md
docs/internal/doc-map.md
docs/internal/doc-map.en.md
docs/internal/tool-map.md
docs/internal/tool-map.en.md
docs/internal/test-map.md
docs/internal/windows-mcp-playbook.md
docs/internal/archive/lessons-learned.md
docs/internal/commit-convention.md
docs/internal/dx-tooling-backlog.md
docs/internal/v2.5.0-v2.6.0-planning.md
```

ADR 24 檔：

```
docs/adr/README.md
docs/adr/README.en.md
docs/adr/001-severity-dedup-via-inhibit.md
docs/adr/001-severity-dedup-via-inhibit.en.md
docs/adr/002-oci-registry-over-chartmuseum.md
docs/adr/002-oci-registry-over-chartmuseum.en.md
docs/adr/003-sentinel-alert-pattern.md
docs/adr/003-sentinel-alert-pattern.en.md
docs/adr/004-federation-central-exporter-first.md
docs/adr/004-federation-central-exporter-first.en.md
docs/adr/005-projected-volume-for-rule-packs.md
docs/adr/005-projected-volume-for-rule-packs.en.md
docs/adr/006-tenant-mapping-topologies.md
docs/adr/006-tenant-mapping-topologies.en.md
docs/adr/007-cross-domain-routing-profiles.md
docs/adr/007-cross-domain-routing-profiles.en.md
docs/adr/008-operator-native-integration-path.md
docs/adr/008-operator-native-integration-path.en.md
docs/adr/009-tenant-manager-crud-api.md
docs/adr/009-tenant-manager-crud-api.en.md
docs/adr/010-multi-tenant-grouping.md
docs/adr/010-multi-tenant-grouping.en.md
docs/adr/011-pr-based-write-back.md
docs/adr/011-pr-based-write-back.en.md
```

EOF hygiene 26 檔（fix_file_hygiene.py 修過的，**不含上面已列過的**）：

```
.github/workflows/docs-ci.yaml
docs/assets/template-data.json
docs/interactive/tools/*.jsx   # 兩個檔，git diff 查實際哪兩個
rule-packs/*.yaml              # 8 檔
README.md
README.en.md
CHANGELOG.md
# 其他 internal docs 數份
```

新增檔（2 檔）：

```
docs/internal/_resume-2026-04-12.md           # 本次 session 的主 resume
docs/internal/_resume-2026-04-12-handoff.md   # 本文件
```

刪除檔（3 檔，見步驟 D 的 commit 5）：

```
docs/internal/_resume-2026-04-11.md
docs/internal/_resume-2026-04-11-v2.md
# （可選）docs/internal/_resume-2026-04-12*.md 也一併 archive
```

### B-2. 複製的兩種做法

#### 做法一：用 Cowork 的 computer:// 連結逐檔複製（最保險）

在 Cowork chat 裡問我「請把 `<檔名>` 內容給我」，我用 Read tool 讀出來貼到
chat，你再手動複製到 Windows clone。適合檔數 ≤ 10 的情境。

#### 做法二：用 Windows 直接讀 Cowork mount（如果有 FUSE 或 SMB 暴露）

大多數 Cowork setup 不會把 VM 的 FUSE mount 暴露到 Windows 側，所以這個做法
通常不可行。如果你的環境可以，指令大概是：

```bash
# 假設 Cowork mount 在 \\wsl$\cowork\sessions\...
export FUSE="/mnt/c/Users/<you>/cowork-mount/vibe-k8s-lab"  # 你的實際路徑
# 工具層
cp "$FUSE/scripts/tools/dx/doc_coverage.py"     "$CLONE/scripts/tools/dx/"
cp "$FUSE/scripts/tools/dx/generate_tool_map.py" "$CLONE/scripts/tools/dx/"
cp "$FUSE/scripts/tools/dx/generate_doc_map.py"  "$CLONE/scripts/tools/dx/"
cp "$FUSE/scripts/tools/dx/add_frontmatter.py"   "$CLONE/scripts/tools/dx/"
# ...以此類推
```

#### 做法三（推薦）：用 `git diff` 從 Cowork 產出 patch 再 `git apply`

在 Cowork chat 裡叫我執行 `git diff main -- <檔案>`，因為 FUSE 側 git 壞了，
這個不行。退路：叫我把所有修改過的檔案內容逐個 Read 出來，我把它們 bundle
成一份 patch 給你（**告訴我想用做法三，我就產 patch**）。

> **最推薦的實際做法**：複製每一類檔案時，先 `git status` 看清單，把本文件
> 上方 B-1 的清單逐條對照。**不要一次全貼——每一 commit 只貼該 commit 涵蓋
> 的檔案，然後立刻 `git add` + `git commit`**。這樣 commit 順序維持乾淨，
> 也不會在單一 commit 混入不相干的改動。

## 步驟 C — 本地 pre-commit 乾跑一次（3 分鐘）

在 commit 前先跑一次，把意外的 drift / lint 問題先處理掉：

```bash
cd "$CLONE"
pre-commit run --all-files
```

**預期全綠**（本 session 的修改應該讓三個關鍵 check 都通過）：

- `doc-map-check` ✅
- `tool-map-check` ✅
- `doc-coverage` ✅（如果 hook 叫這個名字）
- `fix-file-hygiene` ✅（EOF newline / null byte）
- `bilingual-structure-check` ✅
- `frontmatter-check` ✅

**如果某個 hook 失敗**，照這個分流表處理：

| 失敗 hook | 最可能原因 | 解法 |
|----------|-----------|------|
| `doc-map-check` | `docs/internal/doc-map.md` 沒帶過來 | 重跑 `python scripts/tools/dx/generate_doc_map.py --generate --lang all --include-adr` |
| `tool-map-check` | 同上，tool-map.md | `python scripts/tools/dx/generate_tool_map.py --generate --lang all` |
| `fix-file-hygiene` | 有檔案沒抓到 hygiene 修復 | hook 會自動修，`git add` 修復結果後再 commit |
| `bilingual-structure-check` | 某個 `.md` 缺 `.en.md` 對偶 | 如果該檔是 internal，加到 `BILINGUAL_EXCLUDE_PATH_PREFIXES`；否則補 `.en.md` |
| `add-frontmatter --check` 式的 hook | 你只帶了 add_frontmatter.py 但沒帶 doc_coverage.py 的 EXCLUDE_* | 確保 doc_coverage.py 有到 |
| SAST / ruff | `add_frontmatter.py` 裡新加的 `_is_excluded` / `EXCLUDE_*` 被 lint 嫌 | 照 lint 提示調整（通常是 import 順序 / 空行） |

## 步驟 D — 分階段 commit（10 分鐘）

### Commit 1 — 工具鏈（含 add_frontmatter 修復）

```bash
git add \
  scripts/tools/dx/doc_coverage.py \
  scripts/tools/dx/generate_tool_map.py \
  scripts/tools/dx/generate_doc_map.py \
  scripts/tools/dx/add_frontmatter.py

git commit -m "$(cat <<'EOF'
chore(dx): align doc tooling on shared exclusion list, fix add_frontmatter --check

- doc_coverage: add EXCLUDE_* and BILINGUAL_EXCLUDE_* for symlinks,
  includes, internal docs, rule-packs, and _resume-*.md drafts
- doc_coverage: report bilingual_excluded_files in statistics
- generate_tool_map: emit YAML frontmatter in both zh/en output
- generate_doc_map: emit frontmatter and skip _resume-*.md drafts
- add_frontmatter: --check no longer writes files (dry_run path);
  skip os.path.islink() proxies; share EXCLUDE_* with doc_coverage;
  dedup duplicate walk via os.path.realpath + set
EOF
)"
```

### Commit 2 — Frontmatter + Playbook

```bash
git add \
  CLAUDE.md \
  docs/adr/README.md docs/adr/README.en.md \
  docs/adr/001-*.md docs/adr/001-*.en.md \
  docs/adr/002-*.md docs/adr/002-*.en.md \
  docs/adr/003-*.md docs/adr/003-*.en.md \
  docs/adr/004-*.md docs/adr/004-*.en.md \
  docs/adr/005-*.md docs/adr/005-*.en.md \
  docs/adr/006-*.md docs/adr/006-*.en.md \
  docs/adr/007-*.md docs/adr/007-*.en.md \
  docs/adr/008-*.md docs/adr/008-*.en.md \
  docs/adr/009-*.md docs/adr/009-*.en.md \
  docs/adr/010-*.md docs/adr/010-*.en.md \
  docs/adr/011-*.md docs/adr/011-*.en.md \
  docs/internal/test-map.md \
  docs/internal/archive/lessons-learned.md \
  docs/internal/commit-convention.md \
  docs/internal/dx-tooling-backlog.md \
  docs/internal/v2.5.0-v2.6.0-planning.md \
  docs/internal/windows-mcp-playbook.md

git commit -m "$(cat <<'EOF'
docs: add missing frontmatter to ADRs, document Windows clone symlink setup

- Complete YAML frontmatter on CLAUDE.md, all 11 ADRs (zh+en),
  and internal docs (test-map, lessons-learned, commit-convention,
  dx-tooling-backlog, v2.5.0-v2.6.0-planning)
- windows-mcp-playbook: new section "Windows Clone 初次設定 — Symlink
  支援" with Developer Mode setup, core.symlinks config, and
  materialization detection script; added gotcha #39
EOF
)"
```

### Commit 3 — 重新產生的 maps

```bash
git add \
  docs/internal/doc-map.md \
  docs/internal/doc-map.en.md \
  docs/internal/tool-map.md \
  docs/internal/tool-map.en.md

git commit -m "$(cat <<'EOF'
docs(internal): regenerate doc-map and tool-map with frontmatter

Auto-generated by scripts/tools/dx/generate_{doc,tool}_map.py
after the frontmatter emission patch. 117 doc-map entries (with
--include-adr), 98 tool-map entries.
EOF
)"
```

### Commit 4 — EOF hygiene sweep

```bash
# 先讓 fix_file_hygiene 在 Windows 側再跑一次把遺漏的檔案補齊
python scripts/tools/lint/fix_file_hygiene.py

# 然後 add 所有被修改的檔案
git add -u           # 只更新已追蹤檔案，不會誤加新檔
git status            # 檢查沒有誤加 symlink 代理
git commit -m "$(cat <<'EOF'
chore: fix EOF newline and strip null bytes across 26 files

Covers CI workflow, ADR bodies with pre-existing null-byte padding,
rule-pack YAML files, template-data.json, interactive JSX tools,
and README/CHANGELOG pairs. Excludes the 3 FUSE symlink proxies
(docs/CHANGELOG.md, docs/README-root.md/.en.md) which intentionally
contain target strings without trailing newlines.
EOF
)"
```

**⚠️ 重要檢查**：commit 之前用 `git diff --cached` 看 `docs/CHANGELOG.md` 有沒有被
動。**不應該有**。如果有，立即 `git restore --staged docs/CHANGELOG.md` 然後
`git checkout -- docs/CHANGELOG.md`。

### Commit 5 — 清理舊 resume notes + 加新 resume

這一 commit 有兩個選擇：

**選項 A（推薦）**：把新的 `_resume-2026-04-12*.md` 也一併刪掉，讓 repo 沒有任何
resume note。resume 的存在只是「session 斷點記錄」，應該在 PR merge 後就不需要。

```bash
git rm \
  docs/internal/_resume-2026-04-11.md \
  docs/internal/_resume-2026-04-11-v2.md

# 新的兩份如果也想刪（選項 A）：
git rm \
  docs/internal/_resume-2026-04-12.md \
  docs/internal/_resume-2026-04-12-handoff.md

git commit -m "$(cat <<'EOF'
chore(internal): remove stale session resume notes

Session resume notes are transient scratchpads used for context
handoff between Cowork sessions. They should not persist in the
repo once the associated PR is merged.
EOF
)"
```

**選項 B**：刪舊 resume、新 resume 當作本次工作的歷史記錄保留。

```bash
git rm \
  docs/internal/_resume-2026-04-11.md \
  docs/internal/_resume-2026-04-11-v2.md

# 新的 _resume-2026-04-12.md 和 handoff 放著

git commit -m "chore(internal): remove stale session resume notes from 2026-04-11"
```

## 步驟 E — Push + 開 PR（3 分鐘）

```bash
cd "$CLONE"

# 最後一次 sanity
git log --oneline main..HEAD     # 應該看到 5 個新 commit
git status                         # clean

# Push
git push -u origin chore/doc-coverage-hygiene-sweep

# 開 PR
gh pr create \
  --base main \
  --title "chore(docs): coverage hygiene sweep + add_frontmatter --check fix" \
  --body "$(cat <<'EOF'
## Summary

Polish pass after PRs #14–#17. Brings documentation coverage from
71.8% / 67.8% back to **100.0 / 91.7 / 97.7** (all above 80% threshold)
and eliminates a footgun in `add_frontmatter.py --check`.

### Changes by area

**Tooling** (`scripts/tools/dx/`)
- `doc_coverage.py`: new `EXCLUDE_*` and `BILINGUAL_EXCLUDE_*` lists
  that mirror `mkdocs.yml` exclude_docs; new `bilingual_excluded_files`
  metric in JSON output
- `generate_tool_map.py`, `generate_doc_map.py`: emit YAML frontmatter
  in their output so drift checks stay green after the frontmatter
  rollout
- `generate_doc_map.py`: skip `docs/internal/_resume-*.md` drafts
- `add_frontmatter.py`: **fix `--check` writing files through FUSE
  symlink proxies** (`dry_run=(args.dry_run or args.check)`), skip
  `os.path.islink`, share `EXCLUDE_*` with `doc_coverage`, dedup
  duplicate walk via `os.path.realpath` + `set`

**Content**
- Full frontmatter on `CLAUDE.md`, all 11 ADRs (zh + en pairs),
  and 5 internal docs
- `docs/internal/windows-mcp-playbook.md`: new "Windows Clone 初次
  設定 — Symlink 支援" section and gotcha #39
- Regenerated `doc-map` / `tool-map` (with frontmatter) in both languages
- EOF newline / null-byte hygiene sweep across 26 files
- Removed 2 stale session resume notes

### Not included (separate work)

- **Unit tests for `add_frontmatter.py`**: needs a test harness
  under `tests/dx/` that doesn't exist yet; tracked as follow-up
- **Lock MkDocs + drift checks as required CI status checks**:
  blocked on repo admin permissions; see post-merge action below

### Post-merge action (repo admin)

After this PR merges, run:

\`\`\`bash
gh api -X PATCH "repos/<org>/<repo>/branches/main/protection/required_status_checks" \
  -f strict=true \
  -f 'contexts[]=docs-build' \
  -f 'contexts[]=docs-drift-check' \
  -f 'contexts[]=doc-coverage'
\`\`\`

(Replace job names with the actual values shown in the PR status checks UI.)

## Test plan

- [ ] `pre-commit run --all-files` green on the branch
- [ ] CI: `docs-build` green
- [ ] CI: `docs-drift-check` green (doc-map / tool-map parity)
- [ ] CI: `doc-coverage` exit 0 (fm ≥ 80, bi ≥ 80, link ≥ 80)
- [ ] Manual: `python scripts/tools/dx/add_frontmatter.py --check` does not
      modify `docs/CHANGELOG.md` (size remains 15 bytes on FUSE; reflects
      symlink target \`../CHANGELOG.md\`)

EOF
)"
```

## 步驟 F — 等 CI + Review（視情況）

```bash
# 監看 CI 狀態
gh pr checks --watch

# 看 PR 詳情
gh pr view --web
```

常見 CI 失敗分流：

| 失敗 | 原因 | 處置 |
|------|------|------|
| `docs-build` | MkDocs 找不到某個 nav 檔 | `mkdocs build --strict` 本地重現 |
| `docs-drift-check` | maps 不同步 | 本地重跑 generator 再 push |
| `doc-coverage` | 某個 metric 掉到 < 80 | 看 JSON 報告找出具體檔案 |
| `bilingual-structure-check` | 少 `.en.md` | 補對偶或加到 exclude |

## 步驟 G — PR Merge 後：Priority D（5 分鐘，需要 repo admin）

### G-1. 找出實際的 CI job name

進 merged PR 的 **Checks** 頁面（`https://github.com/<org>/<repo>/pull/<N>/checks`），
把每個 job 的顯示名稱抄下來。典型的命名會像：

```
docs-ci / docs-build
docs-ci / docs-drift-check
docs-ci / doc-coverage
```

前綴 `docs-ci` 是 workflow 名字，後面是 job id。`gh api` 裡的 context 字串是
**「workflow / job-name」組合**（或者只用 job-name，看 repo 的 branch protection
設定方式）。**先用 `gh api` 查一次現狀**：

```bash
REPO="<org>/<repo>"   # 替換成實際的 repo

gh api "repos/$REPO/branches/main/protection/required_status_checks" \
  --jq '{strict: .strict, contexts: .contexts}'
```

### G-2. 加入新的 required checks

**保留現有** contexts，只用 `gh api -X PATCH` 加入新的：

```bash
# 先把現有的 contexts 存下來
gh api "repos/$REPO/branches/main/protection/required_status_checks" \
  --jq '.contexts[]' > /tmp/current-contexts.txt

cat /tmp/current-contexts.txt

# 然後組一個新的 contexts 陣列（現有 + 新增）
# 手動編輯 /tmp/new-contexts.txt，每行一個 context

# 一次 PATCH 上去
gh api -X PATCH "repos/$REPO/branches/main/protection/required_status_checks" \
  -f strict=true \
  -F 'contexts[]=docs-build' \
  -F 'contexts[]=docs-drift-check' \
  -F 'contexts[]=doc-coverage'
```

**注意**：

- `-f` 送字串、`-F` 送可能被 shell 解析成數字/布林的值；`contexts[]` 用哪個都行
- `strict=true` 表示 PR 必須把 branch rebase 到最新 main 才能 merge
- 如果 `required_status_checks` 整個物件不存在（repo 沒啟用 branch protection），
  要先用 `PUT /protection` 建立整個 protection 設定：

```bash
gh api -X PUT "repos/$REPO/branches/main/protection" \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["docs-build", "docs-drift-check", "doc-coverage"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
```

### G-3. 驗證鎖成功

```bash
gh api "repos/$REPO/branches/main/protection/required_status_checks" \
  --jq '.contexts'
```

應該看到新加入的 3 個 context 在輸出裡。開一個假 PR 驗證 UI 上看得到這些
check 被列為 required：

```bash
git checkout -b test/required-checks-lock main
echo "" >> CHANGELOG.md
git commit -am "test: verify required checks lock"
git push -u origin test/required-checks-lock
gh pr create --fill --draft
gh pr view --web
# 在 Checks 區塊確認新的 required checks 出現
# 驗證完刪 PR + branch
gh pr close --delete-branch
```

## 步驟 H — Session 結束清理（2 分鐘）

回到 Cowork side：

```bash
make session-cleanup
```

把 FUSE 側殘留的 port-forward / lock 清掉。然後就可以關 Cowork session 了。

## 失敗回滾劇本

### 情境 1：步驟 B 貼錯檔，commit 已經下去

```bash
# 本地還沒 push
git reset --soft HEAD~1   # 把 commit 拆回 staging
git reset HEAD <錯的檔案>   # unstage 錯的檔
git checkout -- <錯的檔案> # 還原
# 然後重新 commit
```

### 情境 2：push 後發現 `docs/CHANGELOG.md` 被 commit 成一般檔

**立即 force push 回滾**：

```bash
git log --oneline -5   # 找到壞掉的那個 commit
git reset --hard <前一個好的 commit>
git push --force-with-lease origin chore/doc-coverage-hygiene-sweep
```

`--force-with-lease` 會在遠端有別人推新 commit 時拒絕 force push，比 `--force` 安全。

### 情境 3：branch protection 設錯把自己鎖出去

`enforce_admins: false` 是設計用來讓 admin 能 bypass protection。先確認設的是
`false`。如果不小心設 `true`：

```bash
gh api -X PATCH "repos/$REPO/branches/main/protection" \
  -f enforce_admins=false
```

## 結束檢查清單

- [ ] `git log --oneline main..HEAD` 顯示 5 個 commit
- [ ] `gh pr checks` 全綠
- [ ] PR merged to main
- [ ] `docs/internal/_resume-2026-04-11.md` 已刪
- [ ] `docs/internal/_resume-2026-04-11-v2.md` 已刪
- [ ] (選 A) `docs/internal/_resume-2026-04-12*.md` 已刪
- [ ] `gh api branches/main/protection/required_status_checks` 含新 contexts
- [ ] 假 PR 驗證 required checks UI 顯示正確後刪掉
- [ ] Cowork 側 `make session-cleanup` 跑過
