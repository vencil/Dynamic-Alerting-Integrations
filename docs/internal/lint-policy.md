---
title: "Lint Policy"
tags: [internal, lint, dx, governance]
audience: [contributors, maintainers]
version: v2.8.0
lang: zh
---

# Lint Policy — 50 個 lint 工具的治理規範

> 對 [`scripts/tools/lint/check_*.py`](../../scripts/tools/lint/) 50 個 lint 工具的分類、scope、bypass 機制、allowlist 治理。
> 觸發來自 PR #375 retrospective 對 (b)/(c) class lint anti-pattern 的識別。
>
> **EN mirror**：本文件仍在 outline 階段；待 [ADR-019](../adr/019-planning-ssot.md) Accepted 後與 lint-policy 一併 ship `lint-policy.en.md`。

## 1. 為什麼需要 lint policy

PR #375 cleanup 過程中暴露：

- 既有 lint 沒有統一分類，contributor 不清楚某 lint 是 hard-block 還是 soft-warn
- 部分 lint 有 ALLOWLIST 但缺乏「permanent vs transitional」區分，allowlist 隨時間膨脹成垃圾場
- 部分 lint 試圖匡列模糊語義（例如：「使用者可見字串」），結構性永遠假陽 + 假陰
- bypass 機制不存在；偶發合理違規只能 `--no-verify` 整個 pre-commit 跳過，無 audit trail

## 2. 三類 lint 定義

| Class | 性質 | 何時用列舉 | 例子 |
|---|---|---|---|
| **(a) Bounded enumeration** | 列舉是政策 SOT 的鏡像；新增條目就是政策變動 | commit scope（`.commitlintrc.yaml` 定義 17 個）/ Rule Pack 數 / valid frontmatter 欄位 / Go test build tag enum | `check_commit_scope_doc.py` / `check_changelog_no_tbd.py` / `check_hardcode_tenant.py` |
| **(b) Negative pattern + false-positive escape** | 規則本身是 negative（找壞東西的 regex / AST），allowlist 列舉「這個 pattern 命中但其實合法」的少數例外 | 偵測代號 / 路徑 / 命名違反 + 已知合法例外 | `check_codename_leak.py` / `check_repo_name.py` / `check_ad_hoc_git_scripts.py`（~12 個） |
| **(c) Fuzzy semantic enumeration** | 試圖列舉一個本質模糊的概念（語義空間不可窮舉） | 「使用者可見字串」/「推銷語言」/「過時敘述」 | `check_portal_i18n.py` / `check_i18n_coverage.py`（~2 個） |

### 判定邊界（Decision Tree）

```
Q1: 規則的「壞東西」集合，是被某個 SOT（檔案、政策、enum）明確定義的嗎？
    YES → (a)
    NO  → 繼續

Q2: 規則的「壞東西」集合是有限 grammar 的（例如 regex 能完整描述）？
    YES → (b)，allowlist 是 false-positive escape
    NO  → (c)，本質是 heuristic

Q3 (only for (c)): 漏抓 / 誤擋的成本是「reviewer 多看 30 秒」還是「客戶生產出事」？
    前者 → (c)，soft-warn 即可
    後者 → 不該用 lint，改 RFC 流程或 manual gate
```

## 3. 三類 lint 的 scan scope 政策

| Class | scan scope | pre-commit stage | CI behavior | 為什麼 |
|---|---|---|---|---|
| **(a)** | full repo | `[pre-commit]` auto-stage | hard-block（紅燈擋 merge） | 政策一致性必須 holistic 檢查；diff-only 會漏掉 cross-file SOT drift |
| **(b)** | **diff-only**（`git diff --unified=0 origin/<base>` 取 added 行）| `[pre-commit]` auto-stage | hard-block | 避免 collateral damage：engineer A 的合法用法不會在 B 改同檔時被擋（PR #375 Gemini 對抗 reviewer 點出） |
| **(c)** | diff-only | `[manual]` 不入 auto-stage | soft-warn（PR comment 提示，不擋 merge） | 模糊語義永遠有誤差；當 hard-block 會養成 `--no-verify` 習慣，當 soft-warn 反而被認真讀 |

### diff-only 實作標準

```python
def get_added_lines(file_path, base="origin/main"):
    """Return list of (line_no, content) for lines added in current diff vs base."""
    out = subprocess.run(
        ["git", "diff", "--unified=0", base, "--", file_path],
        capture_output=True, text=True, check=True,
    ).stdout
    # parse @@ -X,Y +A,B @@ hunks; return added (`+`) lines with line numbers
    ...
```

統一在 `scripts/tools/lint/_lint_helpers.py` 提供 `get_diff_added_lines(filepath, base=None)` helper，所有 (b)/(c) class lint 共用，base 預設讀 `GITHUB_BASE_REF` env，回退 `origin/main`。

### ⚠️ Implementation gotcha：GitHub Actions 淺拷貝陷阱

`actions/checkout@v4` 預設 `fetch-depth: 1`（淺 clone，只拉當前 commit）。在 CI 上跑 `git diff origin/main` 會直接 fatal error，因為 `.git` 內根本沒有 `origin/main` ref 的歷史。

**所有 (b)/(c) class lint 對應的 GitHub Actions workflow 必須**：

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0    # full history — required for diff-only lints
```

或顯式 fetch base branch：

```yaml
- run: |
    git fetch origin ${{ github.base_ref }}:base-ref
    # then lint can use `base-ref` instead of `origin/main`
```

`_lint_helpers.get_diff_added_lines()` 應該偵測 base ref 不存在時 fail-loud（給清楚錯訊指向本節），不要 silent-fall-through 到「全檔掃描」假裝 diff-aware。

### Diff-aware drift 預期行為

Lint 以 PR 目標分支當下 head 為 base；rebase 後 base 變動可能需 re-run CI。**這是預期行為**，不是 bug——它正是「合併後的最終狀態必須合法」的保證。

## 4. Bypass 機制（PR-level，僅限 (b) class）

### 為什麼要 bypass

(b) class lint 偶會擋下作者明確認定的合法用法。例如新加的 ADR 引用一個內部代號作 historical context（必要的）。沒 bypass 機制 → 作者只能 `--no-verify` 跳整個 pre-commit + CI 紅燈，反而失去 lint 的監控意義。

### Bypass spec

**位置**：PR description body（**不**是 commit message — squash merge 會丟失）

**格式**：

```markdown
bypass-lint: <lint-name>
reason: <≥30 words 解釋為何此例外合法>
issue: #<NN>  (optional — 若需後續追蹤)
```

**CI 邏輯**（`check_*_lint` GitHub Action）：

1. 透過 GitHub API 讀 `${{ github.event.pull_request.body }}`
2. parse `bypass-lint:` 行，取 lint-name
3. 若當前 lint name match → CI 黃燈警告（記入 PR check）但不擋 merge
4. PR template 自動 link 到本文件 §4

### Bypass 不適用 (a) / (c)

- (a) class：違反就是違反政策，沒有「合法例外」一說
- (c) class：本來就 soft-warn，不擋 merge，無需 bypass

## 5. Allowlist 治理（針對 (b) class）

### 兩種 allowlist 條目

| 類型 | 行為 | schema |
|---|---|---|
| **Permanent** | 永久合法的 false positive（技術術語、行業標準）| `(pattern, None)` — None 表示不過期 |
| **Transitional** | 暫時合法（過渡期、特定 ADR 期間）| `(pattern, expires_at)` — ISO 日期，過期後自動失效 |

### 範例（在 `check_codename_leak.py`）

```python
ALLOW_LIST = [
    # Permanent — 不會過期的技術縮寫
    ("SHA-256", None),
    ("RFC-", None),
    ("CVE-", None),
    ("ISO-", None),
    ("UTF-8", None),

    # Transitional — 有結束時點
    ("LEGACY-MIGRATION-2026", "2026-12-31"),
]
```

### 過期檢查工具

`scripts/tools/lint/check_allowlist_expiry.py`（manual stage，月度執行）：

- 掃所有 (b) class lint 的 ALLOW_LIST
- expires_at 早於當天 → 報 stale，提示 maintainer 決定 promote permanent 或 remove

### 新增 allowlist 條目的 review checklist

PR 加入新 allowlist entry 時須在 PR description 答：

- [ ] 為何此 pattern 是 false positive（具體 case）
- [ ] permanent 還 transitional？transitional 給 expires_at
- [ ] 是否有更精確的 negative pattern 可以從根本不誤抓（避免 allowlist 膨脹）

## 6. 50 個現存 lint 分類表（first cut）

> 完整表格將在 [ADR-019](../adr/019-planning-ssot.md) 工具實作完成後由 `generate_planning_index.py` 自動產出 `planning-index.md`；此處先列分類 summary。

### (a) class — ~36 個

`check_bilingual_*` (3) / `check_doc_*` (5) / `check_frontmatter_versions.py` / `check_glossary_*` (2) / `check_includes_sync.py` / `check_jsx_loader_compat.py` / `check_makefile_targets.py` / `check_metric_dictionary.py` / `check_path_metadata_consistency.py` / `check_playbook_freshness.py` / `check_property_pilot_*` (2) / `check_rule_pack_*` (3) / `check_structure.py` / `check_subprocess_timeout.py` / `check_tool_consistency.py` / `check_translation_*` (2) / `lint_*.py` (~5) 等。

特徵：列舉是 SOT 的鏡像，policy 變動才需要更新。

### (b) class — ~12 個

| Lint | Allowlist 內容 | diff-only 狀態 |
|---|---|---|
| `check_codename_leak.py` | 技術縮寫（SHA-256, RFC-, ISO-, UTF-8 等 12 條） | ✅ PR #382 |
| `check_repo_name.py` | `/workspaces/vibe-k8s-lab` 等 dev container 路徑 | ✅ PR #383 |
| `check_changelog_no_tbd.py` | HTML comment 內 / brackets 內的 TBD | ✅ PR #383 |
| `check_ad_hoc_git_scripts.py` | `scripts/ops/` 已 sanctioned scripts | ✅ PR #387 |
| `check_bat_ascii_purity.py` | 已知必要 non-ASCII | ✅ PR #387（pre-commit `files:` 自然 diff-aware）|
| `check_design_token_usage.py` | hardcoded color allowlist (legacy theme) | ✅ PR #387 |
| `check_dev_rules_enforcement.py` | dev-rules 內合法 placeholder | OK（diff-only acceptable） |
| `check_dist_source_consistency.py` | excluded fixtures | OK |
| `check_flaky_registry.py` | grandfathered tests | OK |
| `check_head_blob_hygiene.py` | 已知 large fixtures | OK |
| `check_jsx_i18n.py` | 已 i18n marker 標記過 | OK |
| `check_planning_status_sync.py` | （待 ADR-019 ship 後新增）| OK |

**Action item**：上述 6 個 (b) class lint 在 PR ship 後（V-2 phase）批次 refactor 為 diff-only。

### (c) class — 2 個

| Lint | 為什麼 (c) | 處置 |
|---|---|---|
| `check_portal_i18n.py` | UI keyword 集合（"Click", "Save", "Enter"...）試圖匡列「使用者可見字串」 — UI 詞彙無限多、業務字串有時也使用者可見、新業務帶新術語 | 改 `[manual]` stage + soft-warn |
| `check_i18n_coverage.py` | "all_strings" heuristic 計算 i18n 覆蓋率，無語意邊界 | 改 `[manual]` stage + soft-warn |

## 7. 新增 lint 的審核 checklist

PR 新增 lint 須在 PR description 答：

- [ ] 屬於哪一 class？（依 §2 decision tree）
- [ ] (a)：列舉的 SOT 是什麼？SOT 變動如何同步到 lint？
- [ ] (b)：grammar 邊界明確嗎？allowlist 預期成長速度？
- [ ] (c)：為什麼不用 (a)/(b)？是否真需要 lint，還是 PR review checklist 即可？
- [ ] scan scope 對應 §3？
- [ ] 是否需要 bypass 機制？
- [ ] 對 reviewer / contributor 的 friction 評估

## 8. 季度治理 cadence

每季度（與 release 對齊）：

1. **Allowlist 過期 sweep**：`check_allowlist_expiry.py --ci` 執行，過期條目處理
2. **(c) class 重評**：是否仍有價值？是否該升級成 (b) 或 (a)？是否該 retire？
3. **新 lint 提案 review**：累積的「該機器化但還沒做」候選統一拍板

## Future Work

- 自動化 SOT-driven allowlist 同步：例如 `check_commit_scope_doc.py` 從 `.commitlintrc.yaml` 自動 sync，無需手動更新 lint
- bypass tag 與 `check_planning_status_sync.py` 整合：bypass 動作自動產生 backlog entry 追蹤
- (c) class 替代方案 RFC：LLM-augmented review（用 Claude/Gemini 跑 PR review 補位 lint 無法處理的模糊語義）

## 關聯

- 本文件與 [ADR-019](../adr/019-planning-ssot.md) 同 PR ship；ADR-019 的 `check_planning_status_sync.py` 是新 (b) class lint
- 與 [dev-rules.md #4 Doc-as-Code](dev-rules.md) 配合：dev-rules 規範作者該做什麼，本文件規範自動化該擋什麼
- bypass 機制觸發來自 PR #375 對 lint hard-block + full-file scan 的 collateral damage 識別
