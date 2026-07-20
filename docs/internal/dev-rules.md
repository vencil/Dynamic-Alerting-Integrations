---
title: "開發規範 (Development Rules)"
tags: [documentation, governance]
audience: [all]
version: v2.9.0
verified-at-version: v2.9.0
lang: zh
---
# 開發規範 (Development Rules)

> 本專案的 13 條開發規範 + 互動工具變更 SOP。從 `CLAUDE.md` 搬出，避免 tier 1 context 太肥。
> 違反任何一條都會觸發 pre-commit hook / SAST 攔截，或在 review 階段被退回。
>
> **相關文件：** [governance-security.md](../governance-security.md)（SAST 規則細節、Schema 驗證）· [doc-map.md](doc-map.md)（Change Impact Matrix）· [testing-playbook.md](testing-playbook.md)（SAST 合規）

## 為什麼要有這份文件

`CLAUDE.md` 是 tier 1 context，每次 session 都會載入。13 條規範中大部分 Agent 不需要每次都讀完整規則——只需要知道「有這條規則存在，詳細見這裡」。本文件是規範的 Single Source of Truth，CLAUDE.md 只保留 Top 3 最常被違反的條目 + 一個 pointer。

## 13 條開發規範

### 1. ConfigMap 禁止 heredoc 寫入

**規則**：禁止用 `cat <<EOF | kubectl apply -f -` 或類似 heredoc 模式寫 ConfigMap。

**為什麼**：heredoc 會在 escape 層級踩坑（`$` 變數展開、雙引號、換行處理），且無法進行 diff / dry-run。

**應該用**：
- `kubectl patch configmap` — 小範圍修改
- `helm upgrade --set ...` — Helm chart 管理的 ConfigMap
- `scripts/tools/ops/patch_config.py` — 結構化批次修改

### 2. Tenant-Agnostic：Go/PromQL 禁止 Hardcode Tenant ID

**規則**：Go 程式碼、PromQL 表達式、Rule Pack YAML 一律不得出現具體 tenant id（例如 `db-a`、`db-b`）。

**為什麼**：平台設計是多租戶 config-driven，tenant id 應由 config 傳入而不是 hardcode。硬編會讓新增租戶時必須改 code，違反平台定位。

**檢查方式**：✅ **code-driven（v2.8.0, S#83 PR #173）** — `scripts/tools/lint/check_hardcode_tenant.py` 偵測 PromQL label selector `{tenant="<literal>"}` / `{tenant_id="<literal>"}` 樣式於 production 路徑（`components/`、`cmd/`、`internal/`、`pkg/`、`scripts/`、`rule-packs/`、`helm/templates/`）；test/fixture/example 自動排除。Per-line escape：`<!-- hardcode-tenant: ignore -->`（3-line lookback）。**範圍限縮**：只抓 PromQL label selector 的 `=` exact-match；regex `=~` / 否定 `!=` 不在 scope；docstring 例子 / Python f-string template / 註解列自動忽略。其他 hardcoded tenant id 形式（如 Go 變數預設值、CLI 範例字串）暫由 reviewer 把關。

### 3. 三態：Custom / Default（省略）/ Disable

**規則**：任何可配置欄位都必須支援三態：
- **Custom Value** — 填寫具體值
- **Default** — 省略欄位（取平台預設）
- **Disable** — 填寫字串 `"disable"` 明確關閉

**為什麼**：沒有 Disable 狀態時，使用者無法區分「沒設」和「主動關閉」，導致維護歧義。

**關聯**：詳見 `docs/design/config-driven.md` §2.1 三態邏輯。

### 4. Doc-as-Code：CHANGELOG / CLAUDE.md / README 同步更新

**規則**：任何影響 API、schema、CLI、配置格式、文件結構的變更，必須同步更新：
- `CHANGELOG.md` — Unreleased 區
- `CLAUDE.md` — 若影響 Agent routing 或計數
- `README.md` / `README.en.md` — 若影響使用者第一眼看到的資訊
- `docs/architecture-and-design.md` 的 **Mermaid / C4 架構圖** — 若變更動到 sequence / data-flow / component boundary（視為 schema 等級需同步項；對應 adversarial self-review 第 6 lens，TRK-303）

**檢查方式**：見 [doc-map.md § Change Impact Matrix](doc-map.md)，列出每種變更類型要連動哪些文件。

**mkdocs strict semantic gate（自動）**：mkdocs strict build 用 site-root path 語意（`docs/` 是 root），與 pre-commit `check_doc_links.py` 的 filesystem 語意有 gap — 例如 `../../CHANGELOG.md` 在 filesystem 對但 mkdocs 視為跳出 site 而 fail。**v2.8.0 自動化** by `mkdocs-strict-pre-push` hook（`scripts/ops/pre_push_mkdocs_strict.sh`，issue #412）：當推送含 `docs/**/*.md` / `mkdocs.yml` / `README.md` 變動時自動跑 strict check。Tier 1：native mkdocs 安裝即自動 block on fail；Tier 2：mkdocs 未安裝則 WARN + CI backstop。Bypass：`MKDOCS_STRICT_BYPASS=1 git push`（emergency only）。本地驗證仍可手動 `bash scripts/tools/lint/mkdocs_strict_check.sh`。

### 5. SAST：7 條安全 review 準則

**規則**：以下 7 條安全準則是歷史踩坑累積，全都至少炸過一次：

1. encoding 檢查（強制 UTF-8 without BOM）
2. shell 安全（禁用 `shell=True` + unvalidated input）
3. chmod 檢查（禁止 0o777）
4. `yaml.safe_load` 強制（禁用 `yaml.load`）
5. credentials 掃描（禁止 hardcode token / password）
6. dangerous functions（禁用 `eval`、`exec`、`pickle.loads` 對外部輸入）
7. stderr routing（CLI 錯誤訊息必須走 stderr 而非 stdout）

**為什麼**：這 7 條是歷史踩坑的累積，全都至少炸過一次。

**檢查方式**：✅ **code-driven (warn-only soak, v2.9.0 #455)**

- **Gate**: `bandit -ll -ii` (MEDIUM severity × MEDIUM confidence) over `scripts/tools/**` + `components/da-tools/**` via `.github/workflows/security-audit.yaml`; config in `.bandit`.
- **Soak**: workflow `continue-on-error: true` for 2 weeks post-merge → flip to hard-fail once triage stabilizes.
- **Suppression**: inline `# nosec B<ID>  # rationale` (dual-hash; em-dash / single-hash gets parsed as test IDs and emits warnings).
- **Coverage vs the 7 items**: bandit natively gates items 2/4/5/6 (shell B602/B603/B605, yaml_load B506, hardcoded password B105/B106 partial, eval/exec/pickle B102/B301/B307); items 1/3/7 (encoding, chmod 0o777, stderr routing) have no native bandit rule and remain reviewer convention.
- **Local run**: `bandit -c .bandit -r scripts/tools components/da-tools -ll -ii`.

**細節**：見 [governance-security.md](../governance-security.md)。

### 6. 推銷語言不進 repo

**規則**：README、文件、commit message 禁止使用推銷性語言（「業界領先」、「革命性」、「唯一」等）。保持客觀工程語言。

**為什麼**：這是 OSS 專案，文件必須經得起技術 review。推銷語言會被 reviewer 視為不專業，且無法證明。

**檢查方式**：⚠️ **reviewer convention（v2.8.0, PR #169）** — 此規則目前**未由 pre-commit hook 自動掃描**，靠 reviewer 在 PR review 時審視 README / 文件 / commit message 是否含推銷語言。Real lint candidate（簡單 keyword scan，~50 LOC）已排入 backlog；ship 後本句改為實際 hook 引用。

### 7. 版號治理：六線 tag

**規則**：版號管理流程：
1. `make version-check` — 檢查六線版號是否一致
2. `make bump-docs` — 自動更新文件內的版號字串
3. 推 tag — 六條線各自：
   - `v*` — platform（Helm chart + Rule Packs）
   - `exporter/v*` — threshold-exporter
   - `tools/v*` — da-tools Python CLI
   - `portal/v*` — Self-Hosted Portal
   - `recipe-preview/v*` — recipe-preview would-fire 預覽服務（#657 同步升）
   - `tenant-api/v*` — Tenant Manager API

**為什麼**：六個 component 獨立發版，避免「小修一個元件要 bump 整個 platform」。

**⛔ tag 版號鐵則（v2.9.0 燒過）**：component tag 的版號 = **該 component `Chart.yaml` 的 `version`**（不是 `appVersion`、不是平台線版號）。`release.yaml` 每個 component job 起手有 `Verify Chart.yaml version matches tag` 硬 gate，chart `version` ≠ tag 直接 fail。**exporter / portal / recipe-preview** chart 與 release 線同步升（feature PR 不 bump），故 tag = 平台同版；**tenant-api** chart 是 per-change（每 PR bump，見規則內「版號不變不推」），版號走在平台線前，故**以自己的 chart 版號發版**（如 chart 2.9.7 → `tenant-api/v2.9.7`），**不跟平台版**。硬壓 chart 回平台版 = 降級，禁止。

**細節**：見 [github-release-playbook.md](github-release-playbook.md)（§Step 3 + §Release-gate 陷阱 R2）。

### 8. Sentinel Alert 模式

**規則**：新增的 flag metric（例如 `_silent_mode`、`_state_maintenance`）必須走 sentinel alert + Alertmanager inhibit 模式，不要在 PromQL 裡用 `unless` / `and` 做條件 dedup。

**為什麼**：Sentinel 模式讓 TSDB 永遠保留完整指標（便於 audit + replay），inhibit 只影響通知層。如果用 PromQL 做 dedup，歷史數據會「消失」，debug 困難。

**範例**：見 ADR-003 (`docs/adr/003-sentinel-alert-pattern.md`)。

### 9. i18n 三層架構

**規則**：i18n 必須三層各自獨立處理，不能混：
- **JSX 工具**（`tools/portal/src/interactive/tools/*.jsx`）— 用 `window.__t(zh, en)` helper
- **Rule Pack annotation** — 用 `*_zh` 後綴欄位（例如 `summary` + `summary_zh`）
- **Python CLI help**（`scripts/tools/**`）— 用 `detect_cli_lang()` 切換 argparse help 字串

**為什麼**：三層的載入時機、SSR / CSR 狀態、locale 來源都不同，共用會耦合炸鍋。

**檢查方式**：pre-commit hook `check_bilingual_annotations`。Python CLI help 層另有行為型 gate [`tests/shared/test_bilingual_help_contract.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/shared/test_bilingual_help_contract.py)（實跑 `DA_LANG=zh/en --help` 斷言雙語切換；English-only / 中文-only 工具走顯式 allowlist + 只准縮 ratchet），與 coverage 軟性報表 `check_i18n_coverage.py` 互補。

### 9b. SSOT 語言策略（v2.8.0 S#101 policy lock）

**規則**：**中文為主 SSOT，英文為輔**。檔案對命名為 `foo.md`（ZH）+ `foo.en.md`（EN）。**不執行 ZH→EN 遷移**。

**為什麼鎖 ZH primary**：v2.5.0 評估文 §7 原推薦切換 EN SSOT，premise 是「open-source community 慣例 EN」；但實際客戶與 contributor 社群均為中文母語，premise 未驗證 → 切換 = 解決不存在的問題。詳細 audit 見 [`testing-playbook.md`](testing-playbook.md) §LL §12a Q4（premise validation）。

**Phase 1 pilot 工具狀態（v2.7.0 完成的，現 dormant）**：

- `migrate_ssot_language.py` — 單向 ZH→EN 遷移腳本，保留作 future-option（trigger 觸發後可用）
- `check_bilingual_structure.py` / `check_bilingual_content.py` — 雙模式 lint，自動偵測檔案命名；保留以支援未來若選擇遷移

**Trigger conditions for re-evaluation**（觸發後 reopen closed issue [#145](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/145#issuecomment-4587136920)，內有完整評估報告全文）：

1. 收到 ≥3 個非中文母語 contributor PR/issue
2. 客戶 RFP 顯式要求英文 SSOT
3. Maintainer 主動 pivot 為 international-positioning project

未觸發前：新文件沿用「中主英副」；不討論未出現的英文主體客戶。

**評估文件**（status: superseded by S#101）：`ssot-language-evaluation.md` + `ssot-migration-pilot-report.md` 全文已歸檔於 closed issue [#145](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/145#issuecomment-4587136920)（依「決策報告住 issue」原則移出 repo）

### 10. 雙語政策：internal docs 不需英文版

**規則**：`docs/internal/`、工具性檔案（CHANGELOG、tags、includes、plan docs）**一律不需英文版**。僅外部面向文件（`docs/*.md` 頂層、`docs/scenarios/`、`docs/design/`、README）需維持 ZH/EN 雙語對。

**為什麼**：internal docs 的讀者是開發者，且更新頻繁。雙語維護成本極高，ROI 低。

**實作**：pre-commit hook 已設 `BILINGUAL_EXEMPT_PATHS` 自動豁免 `docs/internal/**`。

**Agent 行為**：不需詢問「要不要補 internal docs 英文版」——答案一律是不用。

### 11. 檔案衛生：禁用 `sed -i` 在掛載路徑

**規則**：禁止對掛載路徑（`/sessions/*/mnt/**`、`/workspaces/vibe-k8s-lab/**`、`C:\Users\<user>\vibe-k8s-lab\**`）用 `sed -i`——FUSE 下截斷缺 EOF newline 的檔案 + 可能注入 NUL bytes。**改檔決策樹**：單檔小改→Read+Edit；整檔重寫→Read+Write；批次跨檔→Python + `scripts/tools/dx/_atomic_write.py`；真的要 pipe →`git show HEAD:<f> | sed '...' | tr -d '\0' > <f>`（非 in-place，HEAD 讀避 FUSE stale）。

✅ **四層防護**：(1) Prevent (harness) — `preflight_bash.py` PreToolUse hook 攔 `sed -i`+掛載路徑（audit-2026-04 §H1）；(2) Detect — `detect_sed_damage.py` (commit-time NUL+截斷)；(3) Repair — `fix_file_hygiene.py` (auto-補 EOF + 移 NUL)；(4) Shell — `vibe-sed-guard.sh` (docker exec / 人類 dev)。**Symlink 例外**：symlink proxy md 在 `.pre-commit-config.yaml` `exclude` 排除（詳 [windows-mcp-playbook §v2.7.1 LL](windows-mcp-playbook.md#v271-llend-of-file-fixer-會把-symlink-blob-弄壞)）。

### 12. Branch + PR 流程：禁止直推 main

**規則**：任何變更走 feature branch → PR → owner 同意後 merge；命名 `feat/` / `fix/` / `chore/` / `docs/`。歷史教訓：多次未審核直推 main 後才發現問題。

✅ **Codified**：
- `scripts/ops/protect_main_push.sh` pre-push hook（`pre-commit install --hook-type pre-push` 自動裝）攔截 main push
- `make pr-preflight`（merge 前必跑）寫 `.git/.preflight-ok.<SHA>` marker；`scripts/ops/require_preflight_pass.sh` 走 `gh pr view` 狀態判斷（OPEN PR 才擋，WIP 直接放行）
- 七項檢查：branch 身份 / behind main / conflict / local hooks / scope drift / CI 狀態 / PR mergeable

**執行入口**（三條等價）：`make pr-preflight` ｜ `win_git_escape.bat pr-preflight [PR#]` ｜ `win_git_escape.ps1 pr-preflight [PR#]`。
Status 處理 / hotfix 例外 / A vs B CI 分類細節見 [`github-release-playbook.md`](github-release-playbook.md)。

**快速路徑（ROI r6 D 波 codified）**：剛 commit 完、pre-commit hooks 已在 commit 時證綠 → 用 `make pr-preflight-quick`（`--skip-hooks`）。對 pre-push gate **完全等價**——`--skip-hooks` 的 Local hooks 檢查記為 SKIP 非 FAIL，一樣寫 `.git/.preflight-ok.<SHA>` marker——省掉 hooks 的第二次全跑（commit→preflight→CI 三重執行去掉一重）。commit 後又改過 working tree、或 hooks 綠的是別的 SHA → 回頭跑完整 `make pr-preflight`。
⚠️ **Scope 差異與適用邊界**：commit 時的 hooks 只掃 **staged 檔**，完整版 `pr-preflight` 的 Local hooks 跑 **`--all-files`**——「commit 剛證綠」≠「all-files 綠」。file-scoped hooks（如 `bump_docs --check`，staged-vs-all 是燒過的坑）對本次沒動到的檔的 pre-existing drift，只有 all-files 掃得到；quick 路徑下這類 drift 由 CI 的 all-files 兜底（push 後才知道）。連續多 commit 迭代的 branch 建議週期性（至少 PR 收尾前一次）跑完整 `make pr-preflight` 補 all-files 掃描。

### 13. da-tools 子命令 exit-code / `--json` / `--ci` 約定（#452）

**規則**：新增或修改 da-tools 子命令時，exit code 一律遵守 SSOT [`scripts/tools/_lib_exitcodes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/_lib_exitcodes.py) 的 `0/1/2`——`EXIT_OK`（乾淨）/ `EXIT_VIOLATION`（user-actionable 發現：違規、drift、`--ci` fail-on-finding）/ `EXIT_CALLER_ERROR`（bad args、檔案/路徑不存在、連線失敗、malformed 輸入、缺前置、crash）。**import 具名常數，不寫 magic number**。對齊 Go binary（da-guard / da-parser / da-batchpr）同款 0/1/2 註解。

- **`--json`（stdout 契約，#1112 收嚴）**：machine-readable 子命令須提供 `--json`（或既有的 `--json-output` 拼法），且 `da-tools <cmd> --json | jq` idiom 須在 cli-reference 文件化。**契約**：`--json` 模式下 **stdout 恰好一份 JSON 文件**，於**每一條終端路徑**——含 skip / 空輸入 / dry-run / 早退——**所有人類可讀訊息（進度、摘要、警告、狀態）一律走 stderr**。早退路徑亦須吐 JSON：慣例是**沿用該工具既有 schema 的鍵但歸零/清空，追加 `status`（discriminator）與 `reason`**；正常路徑 schema 不動（consumer 用 `.status // "ok"` 分辨）。矛盾的 flag 組合（如 `patch-config --json` 無 `--diff`）→ `EXIT_CALLER_ERROR`，不得靜默忽略旗標。
- **`--ci`**：Python 工具用 `--ci` 控 fail-on-finding；**Go binary 不引入 `--ci`**（無跨 Python/Go 統一 wrapper 消費者，CI 對 Go 工具用其原生 flag）。
- **認可例外**：`diag_pr_ci.py`（0/1/2/3，exit 3 = network-blocked，runbook 載明）、`tenant-verify`（倒置契約 2=驗證失敗，[cli-reference](../cli-reference.md) + rollback runbook 載明）、`init_project.py`（`--ci` 同名異義：`choices=['github','gitlab','both']` 選 CI config 產出對象，非 fail-on-finding）——改動須連帶遷移文件 + CHANGELOG breaking note。
- ✅ **Codified**：exit code → `tests/shared/test_tool_exit_codes.py`（驗 `--help`=0 + bad-flag=2 + SSOT 常數）；**`--json` stdout 契約 → [`tests/shared/test_json_stdout_contract.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/shared/test_json_stdout_contract.py)**（83 個 `(tool, mode)` recipe 涵蓋全 37 支 `--json`/`--json-output` 工具，斷言 `json.loads(全 stdout)`；meta-test 硬斷言 scope，新工具無法靜默逃脫）；**`--ci` fail-on-finding 契約 → `tests/shared/test_ci_flag_contract.py`**（每支一份保證產 finding 的 fixture、同 argv ± `--ci` 跑兩次斷言 exit-code 翻轉——no-op 即 fail-open 直接紅；同名異義 `init_project` 走 meta-asserted allowlist）。exit-code 章節見 [`testing-playbook.md`](testing-playbook.md)。**新增 `--json` 子命令或新增次要模式（`--dry-run` / `--skip-*` 等早退路徑）時，須在該 gate 補 recipe**——最陰險的違規都藏在次要模式。

## §E Engagement 去識別化（public repo 前提）

repo 與 issues 皆 **PUBLIC**、公開寫入**不可逆**（索引/fork/存檔，事後塗改不可靠）。**合取規則**：`{案量, 產品組合, 被退役的來源平台, air-gap 姿態, 時程}` **任兩項不得同時出現在同一公開處**（單項皆通用，合取在小市場可能 k=1）；**踩線的不是詞彙，是「斷言存在一個進行中的特定案子」**。私有素材放 repo 樹**外**、不版控（保留＝決策+30d／1Q 兜底）。完整政策＋為何刻意不做關鍵字 denylist見 [engagement-deid-policy.md](engagement-deid-policy.md)。✅ **Codified**：`check_engagement_disclosure.py`（窄 backstop；主控制是發布前人工語意檢查），行級 opt-out `<!-- deid-ok: 理由 -->`。

## 互動工具變更 SOP

專案有 **45 個 JSX 互動工具**（v2.8.0 Phase .c 期間自 39 增至 43：master-onboarding / alert-builder / routing-trace / simulate-preview），Source of Truth 檔案：

| 檔案 | 用途 |
|------|------|
| `docs/assets/tool-registry.yaml` | 工具 metadata（id, title, 分類, 路徑） |
| `docs/assets/platform-data.json` | Rule Pack 數據（count 為 15，以此為準） |
| `docs/assets/flows.json` | Guided Flow 編排（工具之間的引導順序） |
| `docs/assets/jsx-loader.html` | JSX 載入器 + `CUSTOM_FLOW_MAP` |
| `docs/interactive/index.html` | 互動工具 Hub 頁 |

### 變更流程

**新增 / 修改互動工具：**
```
1. 更新 tool-registry.yaml
2. make sync-tools     # 同步 metadata 到 Hub
3. make lint-docs      # 全套 lint
```

**新增 / 修改 Rule Pack：**
```
1. make platform-data  # 重新產生 platform-data.json
2. 新增 *_zh 雙語 annotation
3. python scripts/tools/lint/check_bilingual_annotations.py --check
```

**新增 / 修改 Guided Flow：**
```
1. 編輯 flows.json（tool key 須存在於 registry）
2. 新工具需同步 jsx-loader.html 的 CUSTOM_FLOW_MAP
3. make lint-docs
```

## Phase .a0 Style Rules（v2.7.0 新增）

Phase .a0 token 遷移期間確立的慣例，適用所有 JSX 互動工具。

### S1. 中性色禁 raw `slate-*` class，用 `--da-color-*` token 或 `gray-*`

**規則**：`tools/portal/src/interactive/tools/` 下的 JSX 禁止使用 Tailwind `slate-*` 色票類別（`bg-slate-*` / `text-slate-*` / `border-slate-*` 等）。中性色統一走 `--da-color-*` design token（`--da-color-fg` / `--da-color-muted` / `--da-color-surface` / `--da-color-surface-border` / `--da-color-tile-muted`），無對映時退 `gray-*` shade。

**為什麼**：raw `slate-*`（及任何寫死的 Tailwind 色票 class）**不會隨主題翻色**——`text-slate-600` 在 dark mode 仍是 slate-600，破壞 portal 明/暗主題。`--da-color-*` token 才會在 `[data-theme="dark"]` 下 flip（如 `--da-color-muted` 光 `#475569` → 暗 `#94a3b8`）。重點在「用會翻色的 token 取代寫死的 class」，而非避開 slate 色調本身——`--da-color-muted` 本身即 Slate 600。（本節取代原引用**不存在**的 `--da-neutral-*` 家族與已失準的「暖/冷灰」rationale；Day 3 deployment-wizard 遷移 commit `8634ea2` 為原始 context。）

**Waiver**：IDE / code preview 情境可保留 `bg-slate-900 text-slate-100`（深底等寬字型視覺），需在 JSX 註解中標明。

**強制**：新增的 `slate-*` class 由 pre-commit `design-token-usage`（diff-only）攔截（`scripts/tools/lint/check_design_token_usage.py`）。存量（500+ 處、多為早期工具）為 grandfathered baseline，隨 [S1-migration / #3 MetricCard 跨工具收斂] 逐工具遷移；因存量未清，不再宣稱「收束驗收 grep 僅剩 waiver」。

### S2. Playwright spec 含 `assertNoAbsoluteRootHrefs` 守門

**規則**：每個新 Playwright spec 須呼叫 `assertNoAbsoluteRootHrefs(page)`（`tests/e2e/fixtures/portal-tool-smoke.ts` 提供），防止 TRK-104 類型的硬編碼絕對根路徑（`href="/xxx"`）再犯。

**為什麼**：portal 透過 `jsx-loader.html?component=<key>` 載入工具，絕對根路徑全部 404（TRK-104 root cause）。長期解是 `jsx-loader.navigate(key)` helper（規畫 v2.8.0 Portal Navigation Refactor），短期靠 test-layer guard 防退化。

**實作**：`assertNoAbsoluteRootHrefs` 掃描所有 `<a href>` 是否為 portal-safe 路徑（相對 / external / fragment）。Day 3 首次落地（commit `ca48275`），`deployment-wizard.spec.ts` 和 `wizard.spec.ts` 已採用。

### S3. Scrollable container 必附 `tabIndex={0}` + accessible name（v2.8.0 Phase .a 新增）

**規則**：`tools/portal/src/interactive/tools/` 下任何產生 scrollable overflow 的容器（`overflow-auto` / `overflowY: 'auto'` / `overflow-y-auto` / `overflow-scroll` 並搭配 `max-h` / `maxHeight` 或 flex 限高）**必須**同時滿足：

1. `tabIndex={0}` — 讓鍵盤使用者能 Tab 進容器 → 方向鍵捲動
2. `aria-label={t('繁體中文標籤', 'English label')}` 或相等的 `aria-labelledby` — 讓 screen reader 宣告容器用途
3. （建議）`role="region"` — 若內容為邏輯上的獨立區塊

**為什麼**：axe-core `scrollable-region-focusable` rule 在任何 scrollable 且**無 focusable children** 的元素上觸發，不論是否有 `role="region"`。v2.8.0 Phase .a Day 1 scope check 發現 `notification-previewer.jsx` 雖 Day 5-7 期間移除了 `role="region"`，但 `styles.previewBox` 的 `overflowY: 'auto'` + `maxHeight: '400px'` 仍觸發 axe，印證這是容器本身屬性問題而非 role 問題。

**反例：容器已有 focusable children 時勿再套 `tabIndex={0}`**（v2.8.0 Phase .a 追加補充）：若捲動容器內已經放了會吃 Tab 焦點的元素（`<button>`、`<a href>`、`<input>`、`<select>`、`[tabindex="0"]` 等），**容器自己應走 `tabIndex={-1}`**（或乾脆省略），避免 Tab 序列產生容器→內部元素的雙 stop。axe 在這種情況本來就不會觸發 `scrollable-region-focusable`（容器「有 focusable descendants」）；本規則適用範圍就是「容器是捲動區但內部沒 tabbable 元素」的純資訊顯示區塊（heatmap cells / preview panels）。

**反 / 正例**（v2.7.0 實際踩過，TRK-206 / -209）：

```jsx
// ❌ overflow-auto / overflowY:'auto' 容器無 tabIndex／accessible name
<div className="... overflow-auto" role="region" aria-label="Heatmap">...</div>
<div style={{ overflowY: 'auto', maxHeight: '400px' }} aria-live="polite">...</div>

// ✅ 補 tabIndex={0} + aria-label (雙語 token)
<div className="... overflow-auto" role="region" tabIndex={0}
     aria-label={t('閾值熱力圖', 'Threshold heatmap grid')}>...</div>
<div style={{ overflowY: 'auto', maxHeight: '400px' }} aria-live="polite" tabIndex={0}
     aria-label={t('通知標題預覽', 'Notification title preview')}>...</div>
```

**收束驗收**：`tests/e2e/_axe-audit-day1to3.spec.ts` + `_axe-audit-day4.spec.ts` `scrollable-region-focusable` rule 全綠。提案同期啟用 `eslint-plugin-jsx-a11y/scrollable-region-focusable` 於 ESLint config，防止 regression。

### S4. Form control 必附 accessible name（v2.8.0 Phase .a 新增）

**規則**：所有 `<input>` / `<select>` / `<textarea>`（除了 `type="hidden"`）**必須**至少有以下其一：

1. 關聯 `<label>`：`<label htmlFor="foo">Name</label><input id="foo" />`
2. `aria-label={t('zh', 'en')}` — 雙語 token
3. `aria-labelledby="<existing-id>"`

**為什麼**：axe-core `label` / `select-name` rule 為 CRITICAL 等級。Placeholder **不是** accessible name（screen reader 只讀 "edit text"），視覺 label 但未 `htmlFor` 關聯也不成。v2.7.0 Day 5 retrospective 單次 runtime axe 掃到 TRK-208 / -211 / -212 三個 CRITICAL violation，全源自此規則被忽略。

**實作檢查**：互動工具 PR 送審前，手動 `grep -n '<\(input\|select\|textarea\)' <tool>.jsx` 配 `grep -n 'aria-label\|htmlFor' <tool>.jsx`，確認每個 form element 都有對應的 accessible name。

### S5. 單一 semantic token 不可 serve 亮度相差 > 40% 的兩種背景（v2.8.0 Phase .a PR#1c 新增）

**規則**：在 `docs/assets/design-tokens.css` 定義文字色 / foreground token 時，如果 consumer 包含**語意背景亮度差異 > 40%** 的場景（例：hero dark bg `#0f172a` vs tile 白 / 淺灰），**必須 split 為兩個 token**。**禁止**把「hero muted」與「tile muted」用同一 token 服務。

**為什麼**：WCAG 2.1 AA 要求文字對背景達 4.5:1 對比。單一色值在 dark bg 上高對比（白灰系列）→ 同一色值在 light bg 上必然低對比，反之亦然。Phase .a0 PR#1 Day 5 retrospective 踩過這坑（TRK-207：`--da-color-hero-muted` 用 `gray-400` 一口氣套在 hero dark bg + card light bg + SVG white bg 上，axe-core 在 multi-tenant-comparison 報出 40 nodes color-contrast 違規）。v2.8.0 Phase .a PR#1c 用 token-split 徹底解決——保留 `--da-color-hero-muted` 給 hero dark bg，新增 `--da-color-tile-muted` 給 tile / card / SVG 永遠亮底的 consumers。

**反 / 正例**（TRK-207：一 token 服務兩種背景 → PR#1c token-split）：

```css
/* ❌ 一個 token 服務兩種背景 */
:root { --da-color-hero-muted: #94a3b8; }  /* slate-400 */

/* ✅ 按 bg 語意 split */
:root {
  --da-color-hero-muted: #94a3b8;  /* Hero dark bg ONLY: 7.2:1 on #0f172a */
  --da-color-tile-muted: #6b7280;  /* Light bg contexts: 4.83:1 on white */
}
```

```jsx
// ❌ 同一 token 給 dark/light 兩背景（後者 3.12:1 fail）
<p style={{ color: 'var(--da-color-hero-muted)', background: '#0f172a' }}>subtitle</p>
<td style={{ color: 'var(--da-color-hero-muted)', background: '#fef3c7' }}>label</td>

// ✅ 分 token，各自符合 4.5:1
<p style={{ color: 'var(--da-color-hero-muted)', background: '#0f172a' }}>subtitle</p>
<td style={{ color: 'var(--da-color-tile-muted)', background: '#fef3c7' }}>label</td>
```

**命名慣例**：`--da-color-<surface>-<intent>`，`<surface>` 明示語意背景族群（`hero` / `tile` / `card` / `chip` / `toast` 等），`<intent>` 為 foreground 角色（`muted` / `accent` / `strong` / `danger` 等）。避免 `--da-color-muted`（無 surface scope）這類涵蓋過廣的命名。

**雙主題翻色 caveat**：如果 surface 本身會在 `[data-theme="dark"]` 翻色（例：MetricCard light `#f8fafc` → dark `#334155`），**token-split 仍不夠**，需再 split 為 light / dark mode 各自的值（在 `[data-theme="dark"]` 區塊 override）；或改走 theme-aware JSX conditional（useTheme hook）。此情境見 TRK-216（PR#1c 分析衍生，L133 MetricCard subStyle 雙背景問題，另案追蹤）。

**收束驗收**：(1) design-tokens.css 每個 foreground token 須有 JSDoc-style 註解標明「allowed bg contexts + contrast ratio」；(2) axe-core `color-contrast` rule 在 light + dark dual-mode 皆 0 violations；(3) design-system-guide.md §TL;DR 的「Token 速查」應列出 surface scope 分類。

### S6. JSX 工具一律 ESM import，禁止 module-scope `window.__X` 無 fallback 讀取（TRK-233/234 新增）

**規則**：`tools/portal/src/interactive/tools/**` 下的 `.jsx` / `.js`，**禁止** module-scope 寫 `const X = window.__X;` / `const X = globalThis.__X;`（無 fallback）。React hooks 同理——禁 `const { useState } = React;`，必須 `import { useState } from 'react';`。

**為什麼**：portal 走 ESM dist-bundle（TRK-230 Option C）後，esbuild `splitting: true` 切出的 chunk 之間 evaluation 順序非 deterministic——consumer chunk 可能在 `window.__X` 設定的 chunk 之前 evaluate → 讀到 `undefined` → render 失敗。TRK-233 的 PR-E rebuild 觸發過一次，20 個 spec 在 main 上靜默壞掉直到 audit 才發現。

**反 / 正例**：

```jsx
// ❌ Module-scope no-fallback global read
const RULE_PACK_DATA = window.__RULE_PACK_DATA;
const styles = window.__styles;
const { useState } = React;

// ✅ ESM imports
import { RULE_PACK_DATA } from './_common/data/rule-packs.js';
import { styles } from '../styles.js';
import { useState } from 'react';

// ✅ 例外：fallback 形式仍允許（undefined 時走 fallback function）
const t = window.__t || ((zh, en) => en);
```

**實作檢查**：pre-commit hook（TRK-236 Plan C）grep `^const \w+\s*=\s*window\.__\w+\s*;` 阻擋此 pattern。配套：`tools/portal/build.mjs` 禁用 `define: { React: ... }` 之類把 bare identifier rewrite 成 global 讀取的技巧——任何 esbuild splitting 都會打破假設。

## §T 工具生命週期（v2.8.0 Phase .a A-5b 新增）

互動工具（`docs/assets/tool-registry.yaml` 註冊的 JSX）在生命週期中會經歷以下四種狀態。
狀態轉換由 **Tier 評分自動推進**（降階）+ **Registry 手動 opt-in**（下架），避免誤判引發資料毀損。

| 狀態 | 判定來源 | scan_component_health 行為 | 典型動作 |
|------|----------|----------------------------|----------|
| **active** | `scan_component_health` Tier 1/2/3 | 完整計分 + 納入所有 aggregates（tier / token / i18n / playwright） | 日常開發、tier 升降視分數 |
| **deprecation_candidate** | `Tier 3 (deprecation_candidate)` override（LOC<100 + stale，或 writer=0 + audience=narrow） | 仍納入 aggregates，但標記為候選 | 重構 / 合併到其他工具 / opt-in archived |
| **archive_candidate** | `archive_candidates` 自動建議（Tier3 deprecation_candidate + LOC<50 + 未動 >180d + writer=0 + no-spec + first_commit>365d） | 未變更（active），僅在 summary 額外列出建議 | 維護者評估後決定是否 opt-in `status: archived` |
| **archived** | Registry 手動 `status: archived`（opt-in） | `tier="Archived"`, `status="ARCHIVED"`；**從所有 aggregates 排除**（tier / token / playwright / i18n / hex / px）；保留 LOC/i18n 作為 visibility | 觀察期 + 後續歸檔或刪除 |

### 為何 archived 是 opt-in（而非自動下架）

**Q2 warning-only 政策延伸**：scan_component_health 不能片面決定一個工具「該下架」，因為判定訊號（LOC / 活躍度 / spec 覆蓋）都是 proxy，不是真實用戶行為。自動化建議會被 `archive_candidates` 標示，但實際下架需維護者寫入 registry，留下明確 audit trail。

### Registry schema（tool-registry.yaml）

```yaml
tools:
  - key: legacy-thing
    file: interactive/tools/LegacyThing.jsx
    # ...原有欄位...
    status: archived              # 新增：opt-in 下架
    archived_reason: "superseded by new-thing (v2.7.0)"   # 強烈建議填寫
```

### 排除後仍保留 LOC/i18n 的原因

避免 archived 工具在 registry 中「完全消失」──維護者仍可透過 `component-health-snapshot.json` 的 `archived_tools` 清單一眼掌握下架範圍，需要時再決定徹底刪除還是保留為歷史參考。

### 相關自動化

- `scan_component_health.py`：實作於 `scripts/tools/dx/scan_component_health.py`，含 `_is_archive_candidate()` helper
- `tests/dx/test_scan_component_health.py`：12 個測試覆蓋 tier / archived / candidate 三條路徑
- 與 Q2 policy 對齊：警告型（不 fail），可在 CI 印出 `archived_tools` + `archive_candidates` 供 PR review

## §P 流程紀律

§S 管程式碼風格、§T 管工具生命週期，§P 管**寫進 commit 的人類流程紀律**。規則的「why」敘述放這裡，攔截則由 hook 做。

### P1. Commit trailer 必含 `Resolves <ID>`（追蹤項目修復時）

修復已登錄的追蹤項目（`TRK-NNN` 統一 namespace；或 `windows-mcp-playbook.md` 的 `Trap #N`）時，commit message 必須含 trailer：`Resolves TRK-205` / `Fixes Trap #12` / `Closes TRK-103`（動詞大小寫不敏感）。

**原因**：沒有 trailer 時 backlog frontmatter 的 `status:` 與 git log 失聯，下次 session 會把已修項目當新項目再 audit 一次。

**Namespace + frontmatter（v2.8.1）**：原 `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NNN` 統一為 `TRK-NNN`（[ADR-019](../adr/019-planning-ssot.md) Option C；對映見 [`planning-id-mapping.md`](planning-id-mapping.md)）；過渡期舊 ID 仍 work，CI 自動翻譯但 warn。新 planning entry 必填 frontmatter `id: TRK-NNN` / `tracking_kind` / `status`，done 後補 `pr_ref:`（完整 schema + 三 namespace 平行政策見 [ADR-019 §Frontmatter Contract / §Namespace Policy](../adr/019-planning-ssot.md#三層設計)）；chunk 2a `generate_planning_index.py` 待落地後會掃 frontmatter 產 derived view。追蹤項目 status sync 由 `check_planning_status_sync.py`（[issue #379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2b，ADR-019 Layer 3）強制。純文件 / 純 refactor / 跨多項目批次清理可不寫 trailer，改在 body 用 prose 列 IDs。

### P2. PR Scope Drift（由 `check_pr_scope_drift` hook 強制，v2.8.0 Phase .a 新增）

**規則**：本條無文字敘述——由 `scripts/tools/lint/check_pr_scope_drift.py` 於 `make pr-preflight` 強制執行。偵測項：

1. **Tool count drift**：`bump_docs.py --check` 不通過（CLAUDE.md「N 個 Python 工具」與實際 `scripts/tools/**/*.py` count 不一致）
2. **Working-tree clean**：準備 merge 的 PR branch 存在未 commit 的修改（`git diff --quiet` + `git diff --cached --quiet` 都必須通過）

設計原則：規則本體即為 hook 程式碼，避免「文字規範 → 記性 → 執行」三段 rot。新增 drift 項目時改 code，不改本節。

### P3. 高成本 / 高 blast-radius workflow_dispatch 需明確 user 授權（v2.8.0 Phase .b Track A A10 新增）

**規則**：對「執行成本高 + 影響可被外部觀察 + 一旦啟動無法廉價中斷」的 production GitHub Actions workflows，**agent 必須在每次觸發前取得 user 明確授權**，不得僅憑「PR 已 merged」「CI 已綠」推斷可以自跑。

**目前的 perimeter（隨情境演化，更新時直接改 code-driven 偵測，不改本條）**：
- `bench-e2e-record.yaml`（5000-tenant run = 60 分鐘 budget × 多個 GHA runner = 客觀貴）
- `bench-record.yaml`（nightly trend；非 production-blocking 但同 cost class）
- 任何未來進入 `.github/workflows/*-record.yaml` 命名 pattern 的 workflow

**Why**：v2.8.0 Phase 2 e2e harness saga（cycle-1 ~ cycle-6）累積燒掉 ~5 hours wall-clock + 對應 GHA minutes。S#45 archive permission lesson — agent 在 PR #105 merge 後直接 trigger 1000-tenant + 5000-tenant 兩支 workflow，runtime 擋住 5000 等候明確 user "go"。CI passing ≠ user consent；user 同意一個 trigger ≠ 同意後續 trigger。

**How to apply**：SOP — PR merge → 等 user "merged" 確認（不主動執行下一步）→ 等 user 對下一動作的明確指令（"go trigger 5000" 等）→ 才 `gh workflow run`。

不適用於：cheap workflows（lint / commitlint / unit-test re-run）、user-explicit `/loop` 或 schedule 已 codified 的 cron。本條只擋成本高且需要 ad-hoc 評估的人為觸發。

**自動化攔截**：runtime 已示範（cycle-6 阻擋 5000-tenant trigger）；本條為文字版 codification — 未來若加 hook 可走 `pre-bash-tool` 攔 `gh workflow run` + 檢查 workflow basename。

### P4. 數據 claim 須附量測；新機制須附驗證法（epic #570 retrospective）

**規則**：PR 宣稱的數字（token / 行數 / coverage / 節省）須附**可重現量測指令**（`wc` / `git diff --stat` / recall subagent）；新機制的 PR body 須含「**怎麼證明它有效**」（harness run / CI job / recall test）。無量測佐證的數字 claim = 杜撰（#570 燒過：110 行 / 138 peak / ~1000 token 省全是估值，實測 CLAUDE.md token 反而 +19%；line count 等 proxy 會誤導）。

## §A 產出物治理（Planning Artifact Policy，v2.8.0 Phase .a 新增）

§S 管程式碼風格、§T 管工具生命週期、§P 管 commit 紀律；§A 管**產出物（plan / decomposition / scope-discovery 等中繼文件）的歸屬與生命週期**。原始脈絡：v2.8.0 Session #06c maintainer FYI「中繼文件不應該長期在 repo」。

### A1. 三層文件分類

| Layer | 類型 | 範例 | 歸屬 | .gitignore 處置 |
|---|---|---|---|---|
| **L1 Persistent** | 定版後長期 SSOT | `CHANGELOG.md` / `dev-rules.md` / `architecture-and-design.md` / `docs/design/` | `docs/` / repo root | tracked |
| **L2 Pattern-gated ephemeral** | 遵循命名慣例的中繼文件 | `v*-planning.md` / `v*-tech-debt-decomposition.md` / `v*-day*-*.md` / `known-regressions.md` | `docs/internal/` | `.gitignore` pattern catch（見 A3） |
| **L3 Free-form scratch** | Session 內臨時檔（log / script / message draft） | `_pr33v.txt` / `_merge_msg.txt` / `_ci_*.log` | working dir 根目錄 | `_*.{txt,md,json,out,err}` prefix catch-all |

**L1 ← L2 抄寫義務**：L2 內定版的結論（DEC / 風險登錄 / 分解表欄位）**必須抄寫進 L1**（CHANGELOG / dev-rules / playbook / `docs/design/`），並由本版 planning §12.1 Session Ledger 紀錄抄寫路徑。L2 文件**單機 gitignored，會丟**——只有抄寫進 L1 的內容才有 git 層備份。

### A2. 為什麼 L2 不落 repo

1. **Git 歷史污染**：planning doc 在一個版本週期內被改 10-30 次，commit 汙染 `git log --oneline` / bisect / blame / release-notes 自動抽取。
2. **Stale 污染下游**：被 doc-map / MkDocs / search index 收錄後讀者踩到「v2.6.x 時代的 plan」誤導。
3. **決策雙軌化**：CHANGELOG 的 user-facing 敘述 vs planning 的 internal rationale 同步崩壞，SSOT 破壞。
4. **隱私／戰略外洩**：決策矩陣、Claude × Gemini cross-review 對話脈絡不宜暴露 public repo。

### A3. 現行 pattern 清冊（`.gitignore:63-71`）

| 行 | Pattern | 新增時機 | Class | Retention 觸發 |
|---|---|---|---|---|
| 64 | `docs/internal/v*-planning.md` | v2.7.0 | recurring（每 minor 一次） | 跨 2 minor 未匹配 → 檢討 |
| 65 | `docs/internal/v*-planning-archive.md` | v2.8.0 Phase .a Session #18 | recurring（planning-archive 搭配 planning.md） | 跨 2 minor 未匹配 → 檢討 |
| 66 | `docs/internal/v*-tech-debt-decomposition.md` | v2.8.0 Session #06c | recurring（技術債密集版） | v3.0.0 前若僅 v2.8.0 唯一匹配 → 考慮轉 single-file |
| 67 | `docs/internal/v*-day*-*.md` | v2.6.x Cowork day notes | recurring（密集開發版） | 跨 2 minor 未匹配 → 檢討 |
| 68 | `docs/internal/*-plan-draft.md` | v2.5.x | recurring（草案期暫存） | 跨 2 minor 未匹配 → 檢討 |
| 69 | `docs/internal/known-regressions.md` | v2.7.0 | single-instance | 該檔永久消失 → 移除 pattern |
| 70 | `docs/internal/component-health-snapshot.json` | v2.7.0 | single-instance | 同上 |
| 71 | `docs/internal/design-reviews/` | v2.7.0 | directory-level | dir 永久空 → 移除 pattern |

新增 pattern 時優先用 **recurring-class**（`v*-<class>.md`）而非 single-instance（具體檔名）：未來同類再生時零新增動作；即使日後 dead，pattern 行成本 1 行。

### A4. 新 artifact 決策樹

```
新 intermediate doc 要進 docs/internal/
├── 只活一個 session？
│   └── → L3，放 working dir 根目錄並用 `_` 前綴
│
├── 活整個版本週期？
│   ├── 命名可歸入既有 pattern（v*-planning / v*-day*-*）→ 直接沿用
│   ├── 新 class 但預期未來版也會出現 → 加 recurring-class pattern `v*-<class>.md`
│   └── 新 class 確定 one-shot → 加 single-instance pattern + §A3 註記「one-shot」
│
└── 需要被 reviewer 看到（PR comment / 外部顧問）？
    └── → 不屬 L2/L3，重新設計：定版結論進 L1；草案期用 PR description / GitHub comment
```

### A5. Retention Rule（dead pattern 清除）

**觸發**：某 `.gitignore` pattern 跨 **2 個 minor 版**從未匹配實體檔案。
**判定**：每次 `v*-final` tag 後在 §A3 加「pattern 使用審計」row，記錄本版各 pattern 是否被使用。
**三選一處置**：

1. **移除 pattern**（class 確認死亡 → 刪 `.gitignore` 行 + §A3 對應 row strike-through 保留歷史）
2. **轉 single-instance**（僅唯一一次匹配 → pattern 改寫為具體檔名避免 glob 擴張）
3. **升 recurring**（反覆跨版出現 → 保留 pattern + 標 stable）

### A6. Session Ledger 退場（v2.9.0+）

v2.8.0 期間發現 planning.md `§12.1 Session Ledger（Working Log）`型 append-only 表會持續膨脹（單一 session row 動輒 2-4 KB），在重複 read 時造成 context 壓力。**v2.9.0+ planning doc 不再保留 Session ledger 表**，改採：

- **完成 PR / commit**：抄入 `CHANGELOG.md` 即足夠（git log 為事實 SSOT）
- **跨 session 進度追蹤**：用 §12.2 型 Live Tracker（mutate-only，不 append）
- **環境 trap / Lesson Learned**：直接落對應 playbook（不在 planning 中轉手）
- **session 內臨時筆記**：L3 `_*.md` working scratch

驗證：`scripts/tools/lint/validate_planning_session_row.py` 可掃描 §12.1 表並 flag 超過 char limit 的 session row（manual hook，因 L2 文件不入 staging）。

### A7. Dissent / 反向觀點

L2 不落 repo 與 retention 門檻有可辯駁餘地（社群化專案 transparent governance 收益 / 單機 gitignored 風險 / 「跨 2 minor」門檻主觀性等）。完整反向論點與 v2.9.0 重新評估觸發條件見 `v2.8.0-planning-archive.md §12.6 Dissent`（archive 為 maintainer-local、gitignored；anchor slug `126-planning-artifact-policy-dissent-archived-2026-04-19`）。

## 安全紀律（Secret Hygiene，v2.8.1 #445 新增）

Secret leak 防線是 L0/L1/L2/L3 四層：L0 GitHub native push-protection（repo 設定，#470）／ L1 pre-commit hook `secrets-scan-staged`（本地，可被 `--no-verify` 繞）／ L2 server-side `secret-scan.yml`（GitHub 基礎設施上跑，**不可繞**）／ L3 release-time image digest verification（`scripts/ops/verify_release_digest.sh`）。完整守備邊界表、provider rotate cheat-sheet、incident 5-step response 全在 [`secret-leak-remediation-sop.md`](secret-leak-remediation-sop.md)。另起 container/k8s IaC SAST 四層（epic #448）：Layer 1（TRK-311）已上線——hadolint 引擎 + Vibe wrapper（`scripts/tools/lint/check_iac_vibe_rules.py`，hook `iac-sast-check`）掃全部 Dockerfile（HEALTHCHECK-or-rationale／禁過寬 COPY／`.dockerignore` baseline，error=BLOCK／warning=baseline），非阻擋 High findings 列管於 [`iac-lint-baseline.md`](iac-lint-baseline.md)。Layer 2（TRK-312）已上線——kube-linter 引擎 + wrapper `check_iac_helm.py`（hook `iac-helm-sast-check` manual stage；CI 獨立 job「Container SAST L2 (Helm)」）掃 9 個 Helm chart（Mode A 源碼掃 ALLOW_EMPTY/INSECURE + Mode B render-then-lint + `capabilities.add`），Critical 真擋無 escape、High 例外採**中央 EXEMPTIONS 註冊表**（非 in-chart 註解——`helm template` 剝註解，且集中式給 SecOps 單一稽核面）；trivy-config 因與 kube-linter 重疊而不採用。Layer 3（TRK-313）已上線——純 Vibe wrapper `check_helm_values_secrets.py`（hook `helm-values-secrets-check`，class (b) diff-only）抓 Helm values/secret templates 的硬編字面 secret（key 名像 secret 但值非空），白名單放行 `${VAR}`/`{{ .Values }}`/placeholder/ref，與 #445 trufflehog（高熵）互補不雙重 fire（scope 於 TRK-314 擴含 raw `k8s/`）。Layer 4（TRK-314）已上線——kube-linter 引擎 + wrapper `check_k8s_manifests.py`（hook `k8s-manifests-sast-check` manual stage；CI 併入「Container SAST (Helm L2 + raw k8s L4)」job）直掃 `k8s/` 42 個 raw manifest（不需 render，重用 L2 severity + 自有 `(path, check)` 中央 EXEMPTIONS），全 4 層共用的 Severity→Action SSOT 表 + branch-protection required-check checklist 同於 TRK-314 收斂於 [`iac-lint-baseline.md`](iac-lint-baseline.md)。4 層採 **hybrid policy（lint adoption，TRK-315 收斂）**：既有 open-source engine（hadolint／kube-linter）優先 + Vibe wrapper 疊上專案政策（severity／中央 exemption／scope），取代過去 DIY-only `check_*.py` 的 reactive whack-a-mole；**僅 greenfield 套用，不回頭遷移既有 ~50 支** DIY lint。統一 **Severity→Action**：Critical → BLOCK（required status check 擋 merge，無 escape）／ High → WARN + 中央 EXEMPTIONS 註冊表或 baseline 列管 ／ 其餘 INFO；全 4 層 consolidated baseline + SSOT 表 + branch-protection checklist 見 [`iac-lint-baseline.md`](iac-lint-baseline.md)。

1. **不得 `git commit --no-verify` 繞過 secret scan** — L1 可繞，但 L2 server-side 不可繞、會在 push 後擋下同一個 finding。`--no-verify` 只是把 leak 帶進本地 clone 多撐幾分鐘，SOP 的 rotate-first 時鐘已經在走。誤判（test fixture／known-fake string）的合法 escape 是 `.trufflehogignore`（repo root，path regex，L1 hook script 自行讀取）或 inline `# trufflehog:ignore`（行尾，trufflehog-native），**不是** `--no-verify`。
2. **Secret 推上 public repo → 立即進 SOP** — `secret-leak-remediation-sop.md` 鐵律 ASSUME COMPROMISE / ROTATE FIRST：先去 provider console revoke/rotate，洗 git history 是次要善後。任何 contributor 都有 unilateral 授權執行 rotate，不必等 approve。
3. **罰則** — `--no-verify` 繞過 secret scan 屬 process violation，須記入 incident post-mortem（SOP Step 5，blameless 但留痕）。重複發生 → 檢討是否將 L1 升格為更難繞過的機制（如 server-side `pull_request` 必跑 + 必過）。

## 常被違反 Top 4（CLAUDE.md 會保留這四條）

根據歷史 LL 與 pre-commit 攔截記錄，以下四條最容易被違反：

1. **#12 直推 main** — AI agent session 最容易犯的錯誤：改完直接 commit + push main，沒開 branch/PR。已有 pre-push hook 攔截
2. **#11 `sed -i` 在掛載路徑** — 尤其是跨 Windows → VM → Docker 層時，自動補 EOF 的行為不一致
3. **#4 CHANGELOG / CLAUDE.md 同步** — 小修改很容易忘記連動，被 pre-commit `md-yaml-drift-check` 擋下來
4. **#2 Hardcode Tenant ID** — 寫單元測試時最容易偷懶把 `db-a` 寫死在 fixture

## 版本歷史

| 版本 | 變更 |
|------|------|
| v2.6.0 | 從 `CLAUDE.md` 搬出，作為 11 條規範的 SSOT |
| v2.8.0 | 新增 §T 工具生命週期（A-5b scan_component_health archived opt-in）|
| v2.8.0 Phase .a | 新增 §P1 Commit trailer 紀律 + 對應 pre-push 攔截 hook（Trap #12 三層防禦的「規範層 + 攔截層」；該 hook 後因 `known-regressions.md` 撤除而移除）|
| v2.8.0 Phase .a | 新增 §A 產出物治理（L1/L2/L3 taxonomy + retention rule + §A6 v2.9.0+ Session Ledger 退場）：由 `v2.8.0-planning.md §12.6` 搬入 SSOT；compact-pressure 分析催生 §A6 退場政策 |
| v2.8.1 | 新增 §安全紀律（Secret Hygiene，#445 AC iv）：`--no-verify` 嚴禁政策 + L0/L1/L2/L3 四層防線指引。size cap 500→520（§安全紀律 為實質新內容，`--no-verify` ban 是無法 code-enforce 的純文字規則） |
