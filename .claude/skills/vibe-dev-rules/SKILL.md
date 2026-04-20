---
name: vibe-dev-rules
description: Vibe 專案 12 條開發規範（dev-rules.md）的快速參考 + 最常違反 Top 4 深入說明。Use before git commit / push, when refactoring multi-tenant logic, when editing mount-path files, when touching API / schema / CLI / counts that require doc sync, or when unsure whether an action follows Vibe conventions. Also use when user asks "can I do X" about project conventions, or when about to hardcode a tenant id, use sed -i, or push directly to main.
---

# vibe-dev-rules — Vibe 12 條開發規範速查

完整規範（含範例、歷史背景）見 [`docs/internal/dev-rules.md`](../../../docs/internal/dev-rules.md)。本 skill 只抽出**最常被違反 Top 4** + 互動工具變更 SOP 的觸發時機。

## 最常被違反 Top 4

### #12 Branch + PR —— ⛔ 禁止直推 main

一律 `git checkout -b feat/xxx` → push → `gh pr create` → owner 同意 → merge。

- 已有 pre-push hook 攔截：`scripts/ops/protect_main_push.sh`（直推 main 會被 block）
- v2.8.0 新增 pre-push gate：`scripts/ops/require_preflight_pass.sh`（沒跑 `make pr-preflight` 也 block）
- 緊急 bypass：`GIT_PREFLIGHT_BYPASS=1 git push ...`（只在 owner 親自核准後用）

### #11 檔案衛生 —— 禁止對掛載路徑用 `sed -i`

FUSE 掛載 + 缺少 EOF 換行的檔案被 `sed -i` 處理後會被截斷（丟失最後一行）。已有 shell wrapper 攔截（`vibe-sed-guard.sh`），Bash 工具裡違反會直接報錯。

**正確做法**：
- 用 Read + Edit 工具（主路徑）
- 真的需要 shell pipeline：`git show HEAD:file | sed '...' | tr -d '\0' > file`（不要 `-i`）
- 批次替換：`sed '...' < file > file.tmp && mv file.tmp file`

### #4 Doc-as-Code —— 影響 API / schema / CLI / 計數的變更須同步三個檔案

```
CHANGELOG.md + CLAUDE.md + README.md
```

完整連動規則見 [`docs/internal/doc-map.md` §Change Impact Matrix](../../../docs/internal/doc-map.md)。pre-commit hook `bump_docs.py --check` 會攔截計數漂移。

典型觸發：
- 新增 / 刪除 Python 工具（影響 tool-map 計數）
- 新增 / 刪除文件（影響 doc-map 計數）
- API schema 變更（影響 tenant-api / portal docs）
- CLI flag 增刪（影響 cli-reference.md）
- Rule Pack 數量變更（影響 platform-data）

### #2 Tenant-Agnostic —— Go / PromQL / fixture 禁止 hardcode tenant id

禁止寫 `db-a` / `foo` / `bar` / `prod-a` 這類具名 tenant。所有 tenant 都從 config / fixture 注入。違反會被測試 + lint hook 攔截。

**正確做法**：
- Go test：`factory.NewTenant(t)` 產生匿名 tenant
- PromQL：用 `{tenant="$tenant"}` label selector
- fixture：用 `conf.d/tenants/<uuid>.yaml` + scanner pickup

## 其他 8 條規範（摘要）

| # | 規則 | 一句話 |
|---|-----|--------|
| 1 | Config-driven | 所有 routing / alert 邏輯靠 config，不寫死在程式碼 |
| 3 | SHA-256 hot-reload | config 改動自動 reload，不需重啟 |
| 5 | Fail-loud validation | schema 錯誤立刻報錯，不 silent swallow |
| 6 | Severity dedup | 同 tenant 同 alertname 不同 severity 要去重 |
| 7 | Sentinel alert | 每 tenant 必有 heartbeat sentinel |
| 8 | Cardinality guard | 高基 label 需 explicit opt-in |
| 9 | Three-state semantics | active / resolved / unknown 明確分開 |
| 10 | Dual-perspective observability | 同時從 platform + tenant 看 |

完整說明見 [`docs/internal/dev-rules.md`](../../../docs/internal/dev-rules.md)。

## 互動工具變更 SOP

動 `docs/assets/tool-registry.yaml`（39 個 JSX 互動工具 SOT）時：

1. 改 YAML → run `make platform-data`（re-derive 所有派生產物）
2. 改 JSX 內容（`docs/tools/*.jsx`）
3. 同步 CHANGELOG + 版本號
4. pre-commit `tool_registry_drift_check.py` 會驗證一致性

完整 SOP 見 [`dev-rules.md` §互動工具變更 SOP](../../../docs/internal/dev-rules.md#互動工具變更-sop)。

## 使用法

1. 承諾前先對照 Top 4，避免最常見違反
2. 不確定時直接讀 `docs/internal/dev-rules.md` 對應章節
3. 被 pre-commit hook 擋下 → 讀 hook 訊息指向的規則，不要 `--no-verify`
