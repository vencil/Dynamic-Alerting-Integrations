---
title: SSOT 切換影響評估
short: SSOT 語言切換影響分析
audience: [maintainers]
tags: [internal, planning, i18n, language-strategy]
status: draft
lang: zh
version: v2.5.0
---

# SSOT 切換影響評估：中文↔英文主次互換

## 目錄
- [1 概述](#1-概述)
- [2 現況盤點](#2-現況盤點)
- [3 影響分析](#3-影響分析)
- [4 Migration Path](#4-migration-path)
- [5 風險評估](#5-風險評估)
- [6 建議時程](#6-建議時程)
- [7 決策建議](#7-決策建議)

---

## 1 概述

本文評估將 Dynamic Alerting 平台的 SSOT (Single Source of Truth) 語言從 **中文優先** 切換為 **英文優先** 的影響面。

### 背景

- **現況**：Chinese 為主 + English 為輔 (.en.md 對)
- **目標**：English 為主 + Chinese 為輔 (反轉為 .zh.md 對)
- **實施版本**：v2.6.0+ (v2.5.0 僅做準備與評估)

### 決策時機

SSOT 切換涉及文檔結構、CI lint 邏輯、程式碼註解、CLI 預設等多層面，**不適合 1-2 版本內完成**，建議分階段推進：
- **v2.5.0**：評估、準備、試點小型組件（如 CLAUDE.md）
- **v2.6.0–v2.7.0**：分階段切換（docs → config → code）
- **v2.8.0**：完整驗收

---

## 2 現況盤點

### 2.1 文檔 (.md/.en.md 對)

| 類別 | 數量 | 範例 |
|------|------|------|
| **paired** (.md + .en.md) | ~61 | docs/adr/001-*.md/en.md, docs/scenarios/*.md/en.md |
| **zh-only** (.md 無對應 .en.md) | ~2 | docs/internal/*.md (內部工具文檔) |
| **root docs** (.md 但無 .en.md) | ~1 | docs/tags.md |
| **總計** | **123** | |

### 2.2 互動工具 (tool-registry.yaml)

```yaml
# 現況結構
tools:
- key: wizard
  title:
    en: Getting Started Wizard
    zh: 入門精靈
  desc:
    en: Find your learning path
    zh: 找到你的學習路徑
```

- **工具總數**：32 個
- **所有工具均已雙語**：title.en + title.zh, desc.en + desc.zh
- **分布**：platform, domain, tenant 三層用戶均涵蓋
- **參考**：`docs/assets/tool-registry.yaml`

### 2.3 Rule Packs 註解 (platform-data.json)

```json
{
  "rulePacks": {
    "mariadb": {
      "label": "MariaDB/MySQL",
      "dependencies": {
        "reason": {
          "en": "Container resource alerts complement DB monitoring",
          "zh": "容器資源告警補充 DB 監控"
        }
      }
    }
  }
}
```

- **Rule Pack 總數**：15 個
- **雙語支援位點**：`dependencies.reason` (en + zh)
- **其他欄位**：label, category, display 等單語 (通常為英文)
- **參考**：`docs/assets/platform-data.json`

### 2.4 Lint Hooks (bilingual validation)

**現有 bilingual-aware hooks**：

| Hook | 檔案 | 功能 | 需改 |
|------|------|------|------|
| bilingual-structure-check | check_bilingual_structure.py | 確保 .md ↔ .en.md 結構同步 | ⚠️ 邏輯反轉 |
| bilingual-annotations-check | check_bilingual_annotations.py | Rule Pack `*_zh` suffix 完整性 | ⚠️ suffix 改為 `*` |
| bilingual-content-check | check_bilingual_content.py | .en.md 不含過多 CJK；zh-only 檢測 | ⚠️ 主次反轉 |
| i18n-coverage-check | check_i18n_coverage.py | JSX/Python 雙語覆蓋檢查 | ✓ 邏輯不變 |
| jsx-i18n-check | check_jsx_i18n.py | JSX `window.__t(zh, en)` 參數順序 | ⚠️ 參數反轉 |
| portal-i18n-check | check_portal_i18n.py | Portal JSX i18n 覆蓋 | ✓ 邏輯不變 |
| translation-check | check_translation.py (manual) | 翻譯品質檢查 | ⚠️ 邏輯微調 |

### 2.5 Go Code 中文註解

**目前中文出現位點**：
- Alertmanager 樣板：`.CommonAnnotations.summary_zh`, `.description_zh`, `.platform_summary_zh`
- 預留位點（尚未實裝）：Rule Pack 欄位中的 `*_zh` suffix

**掃描結果**：Go 程式碼本身無 hardcoded 中文，中文僅存於 YAML 樣板與 Rule Pack 定義

### 2.6 Python CLI 語言偵測

```python
def detect_cli_lang() -> str:
    """檢測 CLI 語言：DA_LANG → LC_ALL → LANG
    返回 'zh' | 'en'（預設 'en'）
    """
    for var in ("DA_LANG", "LC_ALL", "LANG"):
        val: str = os.environ.get(var, "")
        if val.startswith("zh"):
            return "zh"
        if val.startswith("en"):
            return "en"
    return "en"  # ← 現預設為英文
```

**使用位點**：`da-tools` 各命令的 help text、error msg、報表標題

### 2.7 Alertmanager 樣板 (fallback 順序)

```go
{{ $summary := or .CommonAnnotations.summary_zh .CommonAnnotations.summary }}
{{ $description := or .CommonAnnotations.description_zh .CommonAnnotations.description }}
{{ $platformSummary := or .CommonAnnotations.platform_summary_zh .CommonAnnotations.platform_summary }}
```

**現況**：中文優先 → 英文 fallback

---

## 3 影響分析

### 3.1 Markdown 文檔

#### 現況運作

```
docs/adr/001-severity-dedup.md          ← 中文（主、作者手編）
docs/adr/001-severity-dedup.en.md       ← 英文（輔、自動生成或手編）
```

#### 切換後

```
docs/adr/001-severity-dedup.md          ← 英文（主）
docs/adr/001-severity-dedup.zh.md       ← 中文（輔）
```

#### 變更項目

| 項目 | 操作 | 影響範圍 | 複雜度 |
|------|------|---------|--------|
| **檔名** | mv .en.md → .md, .md → .zh.md | 61 doc pairs | 高 |
| **YAML frontmatter** | `lang: zh` → `lang: en` | 123 files | 低 |
| **內部連結** | docs/foo.md → docs/foo.zh.md (zh context) | 可忽略 (markdown link resolver 已支援) | 低 |
| **_toc.yaml** | 若存在，需更新 lang 標籤 | 0-1 files | 低 |
| **README 中的語言聲明** | 更新描述 | 3+ files | 低 |

#### 執行方案

```bash
# Phase 1: 驗收準備（v2.5.0 late）
# 在試點目錄試跑一遍，如 docs/internal/
for f in docs/internal/*.md; do
  if [ -f "${f%.md}.en.md" ]; then
    echo "Would rename: $f → ${f%.md}.zh.md and ${f%.md}.en.md → $f"
  fi
done

# Phase 2: 批量執行（v2.6.0 early）
# 確保 git 歷史保留，使用 git mv
git mv docs/adr/001.en.md docs/adr/001.md
git mv docs/adr/001.md docs/adr/001.zh.md
```

#### 風險
- **Broken cross-doc links**：若手動連結使用絕對路徑且未更新（low risk，markdown 通常用相對路徑）
- **CI 斷檔**：rename 過程中 hook 一時無法觸發（需暫時 SKIP 部分 hooks）
- **文檔生成工具**：若有 mkdocs/Hugo 等配置硬編 `.en.md`，需更新 glob pattern

---

### 3.2 tool-registry.yaml（互動工具）

#### 現況

```yaml
tools:
- key: wizard
  title:
    en: Getting Started Wizard
    zh: 入門精靈
```

#### 切換後

**方案 A**（推薦）：交換順序，保持結構

```yaml
- key: wizard
  title:
    en: Getting Started Wizard  # 標準 YAML 對象順序 (無硬性要求)
    zh: 入門精靈
```

**方案 B**（激進）：主次反轉

```yaml
- key: wizard
  title_en: Getting Started Wizard   # 不推薦，易破壞下游消費者
  title_zh: 入門精靈
```

#### 建議

**採用方案 A**（交換順序 in-place）：
- ✓ 不改結構，YAML parser 相容
- ✓ 下游（JSX loader, Portal）無感
- ✓ lint hook `check_jsx_i18n` 邏輯不變
- ⚠️ 語義上「第一個」是英文，但 YAML 無序特性，需文檔說明

#### 變更項目

| 項目 | 操作 | 影響 |
|------|------|------|
| **doc 順序** | 將 `en:` 移到 `zh:` 之前 (或保持) | 可選 |
| **JSX loader** | 檢查 `window.__t(zh, en)` → `window.__t(en, zh)` | ⚠️ 見 3.7 |
| **JSX consumer** | 各工具 JSX 檢查 i18n call 順序 | ⚠️ 32 tools |

---

### 3.3 platform-data.json（Rule Pack 註解）

#### 現況

```json
{
  "rulePacks": {
    "mariadb": {
      "dependencies": {
        "reason": {
          "en": "Container alerts...",
          "zh": "容器告警..."
        }
      }
    }
  }
}
```

#### 切換後

```json
{
  "rulePacks": {
    "mariadb": {
      "dependencies": {
        "reason": {
          "en": "Container alerts...",
          "zh": "容器告警..."
        }
      }
    }
  }
}
```

**無實質變更**（JSON 對象無序），但語義上英文為主。

#### 影響

- `generate_platform_data.py`：確保生成時 en 優先於 zh（已有邏輯，無需改）
- Portal 消費邏輯：預期優先顯示 en（需確認）
- Lint：`check_bilingual_annotations.py` 需改邏輯（見 3.4）

---

### 3.4 Lint Hooks 詳細改動

#### 3.4.1 check_bilingual_structure.py（最關鍵）

**現況**：掃描所有 `.en.md`，確保每個都有對應的 `.md` 且結構一致

```python
for f in docs.rglob("*.en.md"):
    pair = f.with_suffix("")  # foo.en.md → foo.md
    if not pair.exists():
        ERROR(f"{f} has no zh pair {pair}")
```

**切換後**：掃描所有 `.md`，確保每個非內部的都有對應的 `.zh.md`

```python
for f in docs.rglob("*.md"):
    if "includes" in f.parts or ".en.md" in str(f):
        continue
    pair = f.with_name(f.stem + ".zh.md")
    if not pair.exists() and is_public_doc(f):  # 新增 is_public_doc
        ERROR(f"{f} has no zh pair {pair}")
```

**改動複雜度**：高（邏輯反轉，需測試 60+ 對）

#### 3.4.2 check_bilingual_annotations.py（Rule Pack）

**現況**：檢查 rule-packs/*.yaml 中是否都有 `*_zh` suffix 欄位

```python
for rule_pack in rule_packs:
    if rule_pack.get("summary_zh") is None:
        WARNING(f"Missing summary_zh in {rule_pack_name}")
```

**切換後**：檢查 `*` 欄位存在，`*_zh` 為可選

```python
for rule_pack in rule_packs:
    if rule_pack.get("summary") is None:
        ERROR(f"Missing summary (en) in {rule_pack_name}")
    # *_zh 變為 optional
```

**改動複雜度**：中等（邏輯反轉，驗證 15 rule pack）

#### 3.4.3 check_bilingual_content.py（CJK 檢測）

**現況**：
- `.en.md` 中 >20% CJK → warning（不應有太多中文）
- `.md` 中 <5% CJK → info（可能未翻譯）

**切換後**：
- `.md` 中 >20% CJK → warning（英文主檔不應有太多中文）
- `.zh.md` 中 <5% CJK → info（可能未翻譯）

```python
# 新邏輯
for f in docs.rglob("*.md"):
    if ".en.md" in f.name or ".zh.md" in f.name:
        continue  # 跳過輔文檔，只檢查主文檔
    ratio = count_cjk_ratio(f.read_text())
    if ratio > 0.20:
        WARNING(f"{f}: {ratio:.1%} CJK in primary (EN) doc")

for f in docs.rglob("*.zh.md"):
    ratio = count_cjk_ratio(f.read_text())
    if ratio < 0.05:
        INFO(f"{f}: only {ratio:.1%} CJK (might be untranslated)")
```

**改動複雜度**：中等

#### 3.4.4 check_jsx_i18n.py（JSX i18n 參數順序）

**現況**：JSX 中應使用 `window.__t(zh, en)` 順序

```javascript
window.__t("中文", "English")  // ✓ 當前慣例
```

**切換後**：應改為 `window.__t(en, zh)` 順序

```javascript
window.__t("English", "中文")  // ✓ 新慣例
```

**改動**：
1. Lint hook 期望值反轉
2. JSX 所有 32 工具的 `window.__t()` 呼叫需反轉參數

**複雜度**：高（32 個檔案 × 多個呼叫點）

#### 3.4.5 check_i18n_coverage.py（不變）

邏輯基於「是否有中英對照」，不因 SSOT 改變。

**改動**：無（或少量文檔說明)

#### 3.4.6 check_portal_i18n.py（不變）

檢查 Portal JSX 是否有 i18n hook，邏輯不因 SSOT 改變。

**改動**：無

---

### 3.5 Python CLI 語言偵測 (detect_cli_lang)

**現況**：預設 `'en'`

```python
def detect_cli_lang() -> str:
    for var in ("DA_LANG", "LC_ALL", "LANG"):
        val: str = os.environ.get(var, "")
        if val.startswith("zh"):
            return "zh"
        if val.startswith("en"):
            return "en"
    return "en"  # ← 預設
```

**切換後**：邏輯無需改，預設仍為 `'en'`（因為 SSOT 本身就是英文）

**改動**：無（或重新註釋說明預設)

#### 使用位點

- da-tools 命令 help/error：`i18n_text(zh_msg, en_msg)` → 優先英文
- 報表標題、欄位名：預設英文

---

### 3.6 Go Code 與 Alertmanager 樣板

#### 現況

```go
{{ $summary := or .CommonAnnotations.summary_zh .CommonAnnotations.summary }}
```

意義：優先中文 summary，fallback 英文。

#### 切換後

```go
{{ $summary := or .CommonAnnotations.summary .CommonAnnotations.summary_zh }}
```

意義：優先英文 summary，fallback 中文。

#### 變更項目

| 檔案 | 變更 | 影響 | 複雜度 |
|------|------|------|--------|
| configmap-alertmanager.yaml | or 順序反轉 (× 3 annotations) | 告警通知樣板 | 低 |
| Alertmanager 註釋 | 更新 doc comment | 文檔 | 低 |
| 無 Go 程式碼改 | - | - | - |

**影響運維**：告警通知會優先使用英文，中文為後備（適應 SSOT）

---

### 3.7 JSX i18n 參數順序

#### 現況

所有 JSX 工具使用 `window.__t(zh, en)` 模式：

```javascript
// docs/interactive/tools/playground.jsx (example)
const title = window.__t("YAML 驗證器", "Tenant YAML Validator");
```

#### 切換後

應改為 `window.__t(en, zh)`：

```javascript
const title = window.__t("Tenant YAML Validator", "YAML 驗證器");
```

#### 變更量

- **檔案數**：32 個 JSX 工具 + 1 wizard.jsx + 1 index.html loader = **34 個**
- **呼叫點**：估計 100–150 個 `window.__t()` 呼叫
- **複雜度**：**高**（需逐檔手編或用 regex + 驗證）

#### 執行方案

```bash
# 方案 1：regex 替換（有風險）
find docs -name "*.jsx" -type f -exec sed -i \
  "s/window\.__t(\([^,]*\), \([^)]*\))/window.__t(\2, \1)/g" {} \;

# 方案 2：逐檔手編（安全）
for tool in docs/interactive/tools/*.jsx; do
  # 手動檢視 window.__t 呼叫，交換參數
done

# 推薦：方案 2 + semi-automated review
```

---

## 4 Migration Path

### Phase 1：v2.5.0 (準備 + 試點)

#### 1a. 文檔準備（2–3 天）
- [ ] 更新 CLAUDE.md § 開發規範，說明「v2.6.0 會切換至英文優先」
- [ ] 更新 README.md 語言聲明
- [ ] 在 `docs/internal/` 試點 2–3 個文檔進行 rename test
  - 選擇低風險目標：如 `windows-mcp-playbook.md` (無其他文檔引用)
  - 執行 rename：.md → .zh.md, .en.md → .md
  - 驗證 lint 通過
  - **回滾**到原狀，記錄踩坑

#### 1b. Lint 工具適配（3–5 天）
- [ ] 新增 feature flag：`--target-lang en` (可選)
- [ ] 在 `check_bilingual_structure.py` 中添加試驗模式
  ```python
  if args.target_lang == "en":
      # 執行新邏輯
  ```
- [ ] 準備 `check_bilingual_annotations.py` 的新邏輯（暫不啟用）
- [ ] 記錄修改清單與驗證步驟

#### 1c. JSX 與 registry 評估（2 天）
- [ ] 掃描所有 32 個工具的 `window.__t()` 呼叫
- [ ] 生成 refactor 清單（工具名、行數、參數）
- [ ] 估算工作量 & 風險（如：是否有嵌套 __t 呼叫）

#### 1d. CI/CD 準備（1 天）
- [ ] 確認預 commit hook 執行環境
- [ ] 準備 SKIP 清單（v2.6.0 rename 期間跳過部分 hook）
- [ ] 整理回滾腳本

### Phase 2：v2.6.0 (大規模遷移)

#### 2a. 文檔遷移（1 周）
- [ ] 停止接受新 doc PR（freeze）
- [ ] 執行批量 rename（使用 git mv 保留歷史）
  ```bash
  # 以 ADR 為例
  for f in docs/adr/*.en.md; do
    base="${f%.en.md}"
    git mv "$f" "${base}.md"          # .en.md → .md
    git mv "${base}.md" "${base}.zh.md"  # .md → .zh.md
  done
  ```
- [ ] 驗證所有 123 個檔案名正確
- [ ] 執行完整 lint（`check_bilingual_structure.py` 已切換新邏輯）
- [ ] 修復任何 broken links（預期少量）
- [ ] 重新開放 doc PR

#### 2b. Lint Hook 啟用（2–3 天）
- [ ] 移除試驗模式，正式啟用新邏輯
  - `check_bilingual_structure.py` 反轉邏輯
  - `check_bilingual_annotations.py` 反轉邏輯
  - `check_bilingual_content.py` 反轉邏輯
- [ ] 執行 pre-commit full run，修復任何遺留問題
- [ ] CI 通過

#### 2c. JSX 工具適配（1–2 周）
- [ ] 逐工具更新 `window.__t()` 呼叫順序（32 + 2 = 34 個檔案）
  - 分批進行（per PR 3–5 工具）
  - 每批都需 JSX 視覺驗證（若可行）
  - `lint_jsx_babel.py` 應已驗證語法
- [ ] `check_jsx_i18n.py` 驗證新順序
- [ ] Portal 若有快取，清除 dist

#### 2d. Go Code 與樣板（1 天）
- [ ] 更新 configmap-alertmanager.yaml 的 or 順序
- [ ] 更新任何 Go 程式碼註釋（預期無程式碼改，只有註釋/文檔）
- [ ] `da-tools` 任何中文 help text 的順序確認（通常自動）

#### 2e. 平台測試（3–5 天）
- [ ] 端對端場景驗證
  ```bash
  make demo-showcase    # 5-tenant 展演
  make validate-config  # 配置驗證
  ```
- [ ] 告警通知測試：確保中英文 fallback 生效
- [ ] Portal 展示測試：工具列表、流程引導
- [ ] CLI 輸出測試：help, error, report

#### 2f. 文檔同步（2 天）
- [ ] 更新 CLAUDE.md
  - § 開發規範：移除 v2.6.0 遷移說明，改為「已切換至英文優先」
  - 更新版本數字（如 doc pair 統計)
- [ ] 更新 README.md
- [ ] 更新 CHANGELOG.md：記錄 BREAKING CHANGE
  ```markdown
  ## v2.6.0

  - **BREAKING: 語言策略切換** — 文檔現以英文為主 SSOT，中文為輔
    - doc pair 現為 .md (en) + .zh.md (zh)，反向於 v2.5.0
    - lint hook 邏輯已適配
    - JSX i18n 參數順序反轉為 __t(en, zh)
    - 告警樣板優先英文 annotation，fallback 中文
  ```
- [ ] 更新 `doc-map.md` 若有列出 lang 標籤統計

### Phase 3：v2.7.0–v2.8.0 (驗收 + 收尾)

#### 3a. 收集反饋（1–2 周）
- [ ] 監控用戶反饋、CI 失敗
- [ ] 記錄任何遺漏的 lint 位點

#### 3b. 微調與冷靜期
- [ ] 修復任何邊界情況
- [ ] 確認無遺留 hardcoded 中文在程式碼中

#### 3c. 完整文檔審視
- [ ] Playbook 中是否有涉及 SSOT 的敘述（如 testing-playbook.md）
- [ ] 若有，更新說明

---

## 5 風險評估

### 5.1 Breaking Changes

| 項目 | 影響範圍 | 嚴重度 | 緩解措施 |
|------|---------|--------|---------|
| **文檔 URL 變更** | 外部連結到 .en.md 檔案會 404 | 高 | 在 README 公告；提供重定向 |
| **JSX 參數順序** | 工具內 i18n 邏輯反轉 | 中 | 全面測試 (34 files) |
| **告警樣板順序** | 舊告警配置若硬編 `summary_zh` 會優先顯示 | 低 | Alertmanager 仍支援 fallback |
| **CLI 預設** | 已是英文，無改變 | 無 | - |

### 5.2 CI 風險

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| **Lint hook 邏輯錯誤** | 中 | 某些合法文檔被拒 | 充分測試，feature flag |
| **Rename 過程 broken links** | 低 | 文檔 build 失敗 | 預先掃描，手動驗證 |
| **JSX 語法錯誤** (regex 替換) | 中 | 工具無法加載 | 逐檔手編 + Babel 驗證 |
| **Git 歷史糾纏** | 低 | 部分開發者 git pull 衝突 | 提前公告，清楚文檔 |

### 5.3 文檔品質風險

- **翻譯遺漏**：若 .zh.md 未及時翻譯，用戶看中文 fallback
  - 緩解：`check_bilingual_content.py` 檢測 <5% CJK
- **結構不一致**：.md vs .zh.md 結構不同
  - 緩解：`check_bilingual_structure.py` 自動驗證

### 5.4 團隊適應

- **開發者學習曲線**：需理解新文檔對結構
  - 緩解：CLAUDE.md, CONTRIBUTING.md 清楚說明
- **工作流變化**：編輯英文為主，中文為次
  - 緩解：模板與範例更新

---

## 6 建議時程

### 最少可行集合 (MVP)

| 階段 | 工作項 | 工作天 | 版本 |
|------|--------|--------|------|
| **評估** | CLAUDE.md 評估文件、試點 rename | 3 | v2.5.0 |
| **準備** | Lint hook 適配、feature flag | 5 | v2.5.0 |
| **遷移** | doc rename、lint 啟用、JSX 更新 | 10 | v2.6.0 |
| **驗證** | 端對端測試、文檔同步 | 5 | v2.6.0 |
| **收尾** | 反饋迴圈、微調 | 3 | v2.7.0 |
| **總計** | | **26 工作天** | |

### 建議版本對應

- **v2.5.0** (now → +4 weeks)：評估文檔、試點、準備
  - 發佈標記：no breaking change
  - 文檔聲明：「v2.6.0 將切換語言優先」

- **v2.6.0** (+4–8 weeks)：大規模遷移
  - 發佈標記：**BREAKING CHANGE** — Language SSOT switch
  - Migration guide 供用戶參考

- **v2.7.0–v2.8.0** (+8–12 weeks)：驗收、微調

---

## 7 決策建議

### 7.1 Pros（切換到英文優先）

✅ **國際化友好**
- 開源社區預設英文；新貢獻者（非華語）體驗更佳
- GitHub Issues/PRs 主要用英文溝通

✅ **維護工作減少**
- 不再需要 maintainer 手動維持中英同步（可 crowd-source 中文翻譯）
- 英文 SSOT 降低決策負擔

✅ **工程規範化**
- Go/Python 主流註解語言是英文
- 與業界慣例對齊

✅ **CI 邏輯簡化**
- 不再需要複雜的 CJK 檢測邏輯（轉型為簡單的「確保二級語言存在」）

### 7.2 Cons（切換的代價）

⚠️ **龐大遷移工作**
- 123 個文檔、32 個工具、60+ lint 位點、多個 CI hook
- 預計 2 個版本、26 工作天

⚠️ **風險集中**
- 一旦切換，所有文檔 URL 變更（外部連結 404）
- 現有用戶書籤失效

⚠️ **中文用戶體驗轉變**
- 默認看英文（需環境變數切換到中文）
- 第三方 fork/復刻 需重新適應

⚠️ **翻譯品質**
- 如果中文翻譯跟不上英文更新，中文 fallback 會陳舊
- 需人力維護

---

### 7.3 替代方案

#### **方案 A：當前狀態維持（Chinese SSOT）**
- ✓ 零遷移成本
- ✗ 違背開源國際化趨勢
- ✗ 新貢獻者體驗欠佳

#### **方案 B：雙語並行（無 SSOT）**
- ✓ 避免切換成本
- ✗ 無單一真理來源；維護複雜度翻倍
- ✗ 分不清哪個是權威版本

#### **方案 C：切換至英文 SSOT（本文方案）** ✅ **推薦**
- ✓ 長期維護友好
- ✓ 符合國際化戰略
- ✗ 短期遷移代價

---

### 7.4 建議決策

**採用方案 C**，理由：

1. **戰略契合**：Dynamic Alerting 規劃為 open-source，英文 SSOT 是必經之路
2. **時機合適**：v2.6.0 前、用戶基尚小、改動容易收拾
3. **可控風險**：分三個版本推進，每步都可驗證與回滾
4. **長期效益**：一次切換，永久收益

**關鍵決策點**：
- ✅ **同意**：在 v2.5.0 進行評估與試點
- ✅ **同意**：v2.6.0 執行遷移（with BREAKING CHANGE 通知）
- ✅ **同意**：在官方公告中清楚解釋理由與遷移路徑

---

## 相關文件

- [`CLAUDE.md`](../../CLAUDE.md) — 開發上下文指引
- [`docs/internal/doc-map.md`](../internal/doc-map.md) — 文檔對照表
- [`docs/internal/tool-map.md`](../internal/tool-map.md) — 工具清單
- [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) — Lint hook 配置
- [`docs/assets/tool-registry.yaml`](../assets/tool-registry.yaml) — 工具 metadata
- [`docs/assets/platform-data.json`](../assets/platform-data.json) — Rule Pack 數據
