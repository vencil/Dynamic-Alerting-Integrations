---
title: "開發規範 (Development Rules)"
tags: [documentation, governance]
audience: [all]
version: v2.6.0
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
