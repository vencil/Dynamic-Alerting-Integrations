---
title: "開發規範 (Development Rules)"
tags: [documentation, governance]
audience: [all]
version: v2.7.0
verified-at-version: v2.6.0
lang: zh
---
# 開發規範 (Development Rules)

> 本專案的 12 條開發規範 + 互動工具變更 SOP。從 `CLAUDE.md` 搬出，避免 tier 1 context 太肥。
> 違反任何一條都會觸發 pre-commit hook / SAST 攔截，或在 review 階段被退回。
>
> **相關文件：** [governance-security.md](../governance-security.md)（SAST 規則細節、Schema 驗證）· [doc-map.md](doc-map.md)（Change Impact Matrix）· [testing-playbook.md](testing-playbook.md)（SAST 合規）

## 為什麼要有這份文件

`CLAUDE.md` 是 tier 1 context，每次 session 都會載入。12 條規範中大部分 Agent 不需要每次都讀完整規則——只需要知道「有這條規則存在，詳細見這裡」。本文件是規範的 Single Source of Truth，CLAUDE.md 只保留 Top 3 最常被違反的條目 + 一個 pointer。

## 12 條開發規範

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

**檢查方式**：pre-commit hook `lint_hardcode_tenant` 會掃描。

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

**檢查方式**：見 [doc-map.md § Change Impact Matrix](doc-map.md)，列出每種變更類型要連動哪些文件。

### 5. SAST：7 條自動掃描規則

**規則**：pre-commit stage 會跑 7 條 SAST 規則：
1. encoding 檢查（強制 UTF-8 without BOM）
2. shell 安全（禁用 `shell=True` + unvalidated input）
3. chmod 檢查（禁止 0o777）
4. `yaml.safe_load` 強制（禁用 `yaml.load`）
5. credentials 掃描（禁止 hardcode token / password）
6. dangerous functions（禁用 `eval`、`exec`、`pickle.loads` 對外部輸入）
7. stderr routing（CLI 錯誤訊息必須走 stderr 而非 stdout）

**為什麼**：這 7 條是歷史踩坑的累積，全都至少炸過一次。

**細節**：見 [governance-security.md](../governance-security.md)。

### 6. 推銷語言不進 repo

**規則**：README、文件、commit message 禁止使用推銷性語言（「業界領先」、「革命性」、「唯一」等）。保持客觀工程語言。

**為什麼**：這是 OSS 專案，文件必須經得起技術 review。推銷語言會被 reviewer 視為不專業，且無法證明。

**檢查方式**：pre-commit hook `check_marketing_language`（manual stage）。

### 7. 版號治理：五線 tag

**規則**：版號管理流程：
1. `make version-check` — 檢查五線版號是否一致
2. `make bump-docs` — 自動更新文件內的版號字串
3. 推 tag — 五條線各自：
   - `v*` — platform（Helm chart + Rule Packs）
   - `exporter/v*` — threshold-exporter
   - `tools/v*` — da-tools Python CLI
   - `portal/v*` — Self-Hosted Portal
   - `tenant-api/v*` — Tenant Manager API

**為什麼**：五個 component 獨立發版，避免「小修一個 tool 要 bump 整個 platform」。

**細節**：見 [github-release-playbook.md](github-release-playbook.md)。

### 8. Sentinel Alert 模式

**規則**：新增的 flag metric（例如 `_silent_mode`、`_state_maintenance`）必須走 sentinel alert + Alertmanager inhibit 模式，不要在 PromQL 裡用 `unless` / `and` 做條件 dedup。

**為什麼**：Sentinel 模式讓 TSDB 永遠保留完整指標（便於 audit + replay），inhibit 只影響通知層。如果用 PromQL 做 dedup，歷史數據會「消失」，debug 困難。

**範例**：見 ADR-003 (`docs/adr/003-sentinel-alert-pattern.md`)。

### 9. i18n 三層架構

**規則**：i18n 必須三層各自獨立處理，不能混：
- **JSX 工具**（`docs/interactive/tools/*.jsx`）— 用 `window.__t(zh, en)` helper
- **Rule Pack annotation** — 用 `*_zh` 後綴欄位（例如 `summary` + `summary_zh`）
- **Python CLI help**（`scripts/tools/**`）— 用 `detect_cli_lang()` 切換 argparse help 字串

**為什麼**：三層的載入時機、SSR / CSR 狀態、locale 來源都不同，共用會耦合炸鍋。

**檢查方式**：pre-commit hook `check_bilingual_annotations`。

### 9b. SSOT 語言遷移（v2.7.0+）

**規則**：v2.7.0 起開始從「中文為主 SSOT」遷移至「英文為主 SSOT」。遷移期間 lint hooks 同時支援兩種檔案對命名：

- **Legacy**：`foo.md`（ZH）+ `foo.en.md`（EN）— 中文為主
- **New**：`foo.md`（EN）+ `foo.zh.md`（ZH）— 英文為主

**遷移工具**：`python3 scripts/tools/dx/migrate_ssot_language.py --dry-run`

**全量遷移時程**：v2.8.0（需 mkdocs.yml 原子性修改，不可漸進式遷移）

**評估文件**：[`ssot-language-evaluation.md`](ssot-language-evaluation.md) + [`ssot-migration-pilot-report.md`](ssot-migration-pilot-report.md)

### 10. 雙語政策：internal docs 不需英文版

**規則**：`docs/internal/`、工具性檔案（CHANGELOG、tags、includes、plan docs）**一律不需英文版**。僅外部面向文件（`docs/*.md` 頂層、`docs/scenarios/`、`docs/design/`、README）需維持 ZH/EN 雙語對。

**為什麼**：internal docs 的讀者是開發者，且更新頻繁。雙語維護成本極高，ROI 低。

**實作**：pre-commit hook 已設 `BILINGUAL_EXEMPT_PATHS` 自動豁免 `docs/internal/**`。

**Agent 行為**：不需詢問「要不要補 internal docs 英文版」——答案一律是不用。

### 11. 檔案衛生：禁用 `sed -i` 在掛載路徑

**規則**：**禁止** 對掛載路徑（`/sessions/*/mnt/**`）的檔案使用 `sed -i`。

**為什麼**：FUSE 掛載下，`sed -i` 對 **缺少 EOF 換行的檔案** 會截斷最後一行（已踩過多次）。

**應該用**：
- **首選**：Read + Edit 工具（自動處理 EOF）
- **批次 pipe**：`git show HEAD:file | sed '...' | tr -d '\0' > file`
- **絕對不要**：`sed -i '...' file`

**自動修復**：`file-hygiene` pre-commit hook 會偵測並修復 null bytes + 缺失 EOF 換行，但最好一開始就不要製造問題。

> **⚠️ v2.7.1 LL — Symlink blob + EOF newline**：bulk「補 EOF newline」cleanup（Windows-side commit 尤甚）會把 symlink 的 blob 從 `../target.md` 變成 `../target.md\n`，Linux CI 的 `readlink()` 就解不了。`file-hygiene` hook 的 `os.path.islink()` 在 Linux/FUSE 側會擋下，但 Windows clone 若 `core.symlinks=false`，symlink 被物化成純文字檔、runtime guard 失效。防線：`.pre-commit-config.yaml` 把所有 symlink proxy md 加進 `exclude` 正則。完整事件與 repo symlink 清單見 [windows-mcp-playbook §v2.7.1 LL：`end-of-file-fixer` 會把 symlink blob 弄壞](windows-mcp-playbook.md#v271-llend-of-file-fixer-會把-symlink-blob-弄壞)。

### 12. Branch + PR 流程：禁止直推 main

**規則**：任何程式碼或文件變更**不得**直接 commit 到 `main`。必須：
1. 從 `main` 開 feature branch（命名慣例：`feat/xxx`、`fix/xxx`、`chore/xxx`、`docs/xxx`）
2. 推到 remote → 開 PR
3. 取得 owner 明確同意後才 merge

**為什麼**：歷史教訓 — 多次 session 未經實質審核就直推 main，事後才發現問題。Branch + PR 強制建立 review 節點，避免未經檢視的變更進入主幹。

**Harness**：`scripts/ops/protect_main_push.sh` 作為 pre-push hook，在 push 到 `main`（或 `master`）時攔截並報錯。安裝方式：
```bash
# 自動安裝（pre-commit install --hook-type pre-push 已包含）
pre-commit install --hook-type pre-push

# 或手動安裝
cp scripts/ops/protect_main_push.sh .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

**PR 收尾 SOP（Branch Closing Checklist）**：

merge 前執行 `make pr-preflight`（或 `make pr-preflight-quick` 跳過 local hooks），自動檢查六項：

```
┌─ 1. Branch 身份    確認在 feature branch，非 main
├─ 2. Behind main    落後幾個 commit → 建議先 merge main
├─ 3. Conflict       dry-run merge 偵測衝突
├─ 4. Local hooks    pre-commit run --all-files
├─ 5. CI 狀態        gh pr checks（含 A/B 分類：pre-existing vs this-PR）
└─ 6. PR mergeable   GitHub mergeable + review 狀態
```

各 status 的處理方式：

| Status | 意義 | 處理 |
|--------|------|------|
| ✅ PASS | 檢查通過 | 無需動作 |
| ⚠️ WARN | 可合併但有風險 | behind main → merge main；CI pending → 等 |
| ❌ FAIL | 必須修復 | conflict → merge main 解衝突；CI fail → 看 A/B 分類決定是否需修 |
| ⏭️ SKIP | 檢查被跳過 | gh 不可用 → 在 Windows 跑；hooks 跳過 → 改跑完整版 |

**執行入口**（三條等價路徑）：
- Cowork VM / Dev Container：`make pr-preflight` 或 `make pr-preflight-quick`
- Windows 逃生門 (bat)：`win_git_escape.bat pr-preflight [PR#]`
- Windows 逃生門 (ps1)：`win_git_escape.ps1 pr-preflight [PR#]`

**例外**：若確實需要直推 main（例如 hotfix），必須在 commit message 或 push 命令中明確標記理由，並事後補 PR review。

## 互動工具變更 SOP

專案有 **39 個 JSX 互動工具**，Source of Truth 檔案：

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

### S1. 中性色禁 slate，用 `--da-neutral-*` 或 `gray-*`

**規則**：`docs/interactive/tools/` 下的 JSX 禁止使用 Tailwind `slate-*` 類別。中性色統一走 `--da-neutral-*` token 或對應的 `gray-*` shade。

**為什麼**：`design-tokens.css` 的 `--da-neutral-*` 色值是 Tailwind `gray` scale（暖中性灰）。`slate` 是冷藍灰，兩者色調不同。混用會導致同頁面兩種中性灰色調。Day 3 deployment-wizard 遷移時確立（commit `8634ea2`）。

**Waiver**：IDE / code preview 情境可保留 `bg-slate-900 text-slate-100`（深底等寬字型視覺），需在 JSX 註解中標明。

**收束驗收**：`grep -rE '(bg|text|border)-slate-[0-9]+' docs/interactive/tools/` 僅剩 waiver。

### S2. Playwright spec 含 `assertNoAbsoluteRootHrefs` 守門

**規則**：每個新 Playwright spec 須呼叫 `assertNoAbsoluteRootHrefs(page)`（`tests/e2e/fixtures/portal-tool-smoke.ts` 提供），防止 REG-004 類型的硬編碼絕對根路徑（`href="/xxx"`）再犯。

**為什麼**：portal 透過 `jsx-loader.html?component=<key>` 載入工具，絕對根路徑全部 404（REG-004 root cause）。長期解是 `jsx-loader.navigate(key)` helper（規畫 v2.8.0 Portal Navigation Refactor），短期靠 test-layer guard 防退化。

**實作**：`assertNoAbsoluteRootHrefs` 掃描所有 `<a href>` 是否為 portal-safe 路徑（相對 / external / fragment）。Day 3 首次落地（commit `ca48275`），`deployment-wizard.spec.ts` 和 `wizard.spec.ts` 已採用。

### S3. Scrollable container 必附 `tabIndex={0}` + accessible name（v2.8.0 Phase .a 新增）

**規則**：`docs/interactive/tools/` 下任何產生 scrollable overflow 的容器（`overflow-auto` / `overflowY: 'auto'` / `overflow-y-auto` / `overflow-scroll` 並搭配 `max-h` / `maxHeight` 或 flex 限高）**必須**同時滿足：

1. `tabIndex={0}` — 讓鍵盤使用者能 Tab 進容器 → 方向鍵捲動
2. `aria-label={t('繁體中文標籤', 'English label')}` 或相等的 `aria-labelledby` — 讓 screen reader 宣告容器用途
3. （建議）`role="region"` — 若內容為邏輯上的獨立區塊

**為什麼**：axe-core `scrollable-region-focusable` rule 在任何 scrollable 且**無 focusable children** 的元素上觸發，不論是否有 `role="region"`。v2.8.0 Phase .a Day 1 scope check 發現 `notification-previewer.jsx` 雖 Day 5-7 期間移除了 `role="region"`，但 `styles.previewBox` 的 `overflowY: 'auto'` + `maxHeight: '400px'` 仍觸發 axe，印證這是容器本身屬性問題而非 role 問題。

**反例**（v2.7.0 實際踩過）：

```jsx
// ❌ TECH-DEBT-006：threshold-heatmap scrollable heatmap grid 沒有 tabIndex
<div className="... overflow-auto" role="region" aria-label="Heatmap">...</div>

// ❌ TECH-DEBT-009：notification-previewer previewBox 拿掉 role="region" 以為修了，
//     其實 overflowY:'auto' + maxHeight:'400px' 還是觸發 axe
<div style={{ overflowY: 'auto', maxHeight: '400px' }} aria-live="polite">...</div>
```

**正例**（v2.8.0 PR#2 修後）：

```jsx
// ✅ TECH-DEBT-006 修法
<div className="... overflow-auto"
     role="region"
     aria-label={t('閾值熱力圖', 'Threshold heatmap grid')}
     tabIndex={0}>...</div>

// ✅ TECH-DEBT-009 修法（雙 previewBox 同模式）
<div style={{ overflowY: 'auto', maxHeight: '400px' }}
     aria-live="polite"
     tabIndex={0}
     aria-label={t('通知標題預覽', 'Notification title preview')}>...</div>
```

**收束驗收**：`tests/e2e/_axe-audit-day1to3.spec.ts` + `_axe-audit-day4.spec.ts` `scrollable-region-focusable` rule 全綠。提案同期啟用 `eslint-plugin-jsx-a11y/scrollable-region-focusable` 於 ESLint config，防止 regression。

### S4. Form control 必附 accessible name（v2.8.0 Phase .a 新增）

**規則**：所有 `<input>` / `<select>` / `<textarea>`（除了 `type="hidden"`）**必須**至少有以下其一：

1. 關聯 `<label>`：`<label htmlFor="foo">Name</label><input id="foo" />`
2. `aria-label={t('zh', 'en')}` — 雙語 token
3. `aria-labelledby="<existing-id>"`

**為什麼**：axe-core `label` / `select-name` rule 為 CRITICAL 等級。Placeholder **不是** accessible name（screen reader 只讀 "edit text"），視覺 label 但未 `htmlFor` 關聯也不成。v2.7.0 Day 5 retrospective 單次 runtime axe 掃到 TECH-DEBT-008 / -011 / -012 三個 CRITICAL violation，全源自此規則被忽略。

**實作檢查**：互動工具 PR 送審前，手動 `grep -n '<\(input\|select\|textarea\)' <tool>.jsx` 配 `grep -n 'aria-label\|htmlFor' <tool>.jsx`，確認每個 form element 都有對應的 accessible name。

### S5. 單一 semantic token 不可 serve 亮度相差 > 40% 的兩種背景（v2.8.0 Phase .a PR#1c 新增）

**規則**：在 `docs/assets/design-tokens.css` 定義文字色 / foreground token 時，如果 consumer 包含**語意背景亮度差異 > 40%** 的場景（例：hero dark bg `#0f172a` vs tile 白 / 淺灰），**必須 split 為兩個 token**。**禁止**把「hero muted」與「tile muted」用同一 token 服務。

**為什麼**：WCAG 2.1 AA 要求文字對背景達 4.5:1 對比。單一色值在 dark bg 上高對比（白灰系列）→ 同一色值在 light bg 上必然低對比，反之亦然。Phase .a0 PR#1 Day 5 retrospective 踩過這坑（TECH-DEBT-007：`--da-color-hero-muted` 用 `gray-400` 一口氣套在 hero dark bg + card light bg + SVG white bg 上，axe-core 在 multi-tenant-comparison 報出 40 nodes color-contrast 違規）。v2.8.0 Phase .a PR#1c 用 token-split 徹底解決——保留 `--da-color-hero-muted` 給 hero dark bg，新增 `--da-color-tile-muted` 給 tile / card / SVG 永遠亮底的 consumers。

**反例**（TECH-DEBT-007 原始犯案）：

```css
/* ❌ 一個 token 服務兩種背景 */
:root {
  --da-color-hero-muted: #94a3b8;  /* slate-400 */
}
```

```jsx
// ❌ hero dark bg 用（7.2:1 pass）
<p style={{ color: 'var(--da-color-hero-muted)', background: '#0f172a' }}>subtitle</p>

// ❌ tile light bg 也用（3.12:1 fail）— 同一 token，不同語意
<td style={{ color: 'var(--da-color-hero-muted)', background: '#fef3c7' }}>label</td>
```

**正例**（PR#1c 修後 token-split）：

```css
/* ✅ 按 bg 語意 split */
:root {
  --da-color-hero-muted: #94a3b8;  /* Hero dark bg ONLY: 7.2:1 on #0f172a */
  --da-color-tile-muted: #6b7280;  /* Light bg contexts: 4.83:1 on white */
}
```

```jsx
// ✅ Hero dark bg
<p style={{ color: 'var(--da-color-hero-muted)', background: '#0f172a' }}>subtitle</p>

// ✅ Tile always-light bg
<td style={{ color: 'var(--da-color-tile-muted)', background: '#fef3c7' }}>label</td>
```

**命名慣例**：`--da-color-<surface>-<intent>`，`<surface>` 明示語意背景族群（`hero` / `tile` / `card` / `chip` / `toast` 等），`<intent>` 為 foreground 角色（`muted` / `accent` / `strong` / `danger` 等）。避免 `--da-color-muted`（無 surface scope）這類涵蓋過廣的命名。

**雙主題翻色 caveat**：如果 surface 本身會在 `[data-theme="dark"]` 翻色（例：MetricCard light `#f8fafc` → dark `#334155`），**token-split 仍不夠**，需再 split 為 light / dark mode 各自的值（在 `[data-theme="dark"]` 區塊 override）；或改走 theme-aware JSX conditional（useTheme hook）。此情境見 TECH-DEBT-016（PR#1c 分析衍生，L133 MetricCard subStyle 雙背景問題，另案追蹤）。

**收束驗收**：(1) design-tokens.css 每個 foreground token 須有 JSDoc-style 註解標明「allowed bg contexts + contrast ratio」；(2) axe-core `color-contrast` rule 在 light + dark dual-mode 皆 0 violations；(3) design-system-guide.md §TL;DR 的「Token 速查」應列出 surface scope 分類。

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
