---
title: "SSOT 語言遷移 Phase 1 Pilot Report"
short: SSOT Phase 1 Pilot
audience: [maintainers]
tags: [internal, planning, i18n, language-strategy]
status: completed
lang: zh
version: v2.6.0
---

# SSOT 語言遷移 Phase 1 Pilot Report

> v2.7.0 Phase .d D-2 產出

## 1. Pilot 範圍與結果

### 1.1 工具準備

| 工具 | 路徑 | 狀態 |
|------|------|------|
| 遷移腳本 | `scripts/tools/dx/migrate_ssot_language.py` | ✅ 完成 |
| 結構 lint | `check_bilingual_structure.py` 雙模式 | ✅ 完成 |
| 內容 lint | `check_bilingual_content.py` 雙模式 | ✅ 完成 |
| pre-commit trigger | `.pre-commit-config.yaml` `.zh.md` 觸發 | ✅ 完成 |

### 1.2 Dry-Run 結果

**Pilot 目標**：`docs/getting-started/` — 4 對文件

```
Migration plan: 4 file pairs (8 renames + 16 updates)

  [decision-matrix]
    → docs/getting-started/decision-matrix.md → decision-matrix.zh.md
    → docs/getting-started/decision-matrix.en.md → decision-matrix.md
    + frontmatter lang 更新 + nav link 更新

  [for-domain-experts]     同上模式
  [for-platform-engineers] 同上模式
  [for-tenants]            同上模式
```

**全量掃描**：66 對文件（132 renames + 264 updates）

### 1.3 Lint 驗證

| 檢查 | 結果 | 備註 |
|------|------|------|
| `check_bilingual_structure.py` | ✅ 0 errors | 雙模式正確偵測 legacy + new 模式 |
| `check_bilingual_content.py` | ✅ 0 warnings | `_is_english_doc()` / `_is_chinese_doc()` auto-detect |
| 45 個 bilingual lint 測試 | ✅ 全通過 | tests/lint/test_check_bilingual_*.py |

---

## 2. MkDocs 影響評估

### 2.1 現況架構

```yaml
# mkdocs.yml 關鍵設定
plugins:
  - i18n:
      docs_structure: suffix      # ← 用檔名 suffix 區分語言
      languages:
        - locale: zh
          default: true           # ← 中文是 default
        - locale: en
          build: true
```

意味著：
- `foo.md` = 中文（default locale）
- `foo.en.md` = 英文（suffix locale）

### 2.2 遷移後目標架構

```yaml
plugins:
  - i18n:
      docs_structure: suffix
      languages:
        - locale: en
          default: true           # ← 英文變成 default
        - locale: zh
          build: true
```

意味著：
- `foo.md` = 英文（default locale）
- `foo.zh.md` = 中文（suffix locale）

### 2.3 需修改的 mkdocs.yml 項目

| 項目 | 現值 | 目標值 | 影響 |
|------|------|--------|------|
| `theme.language` | `zh` | `en` | Material 主題預設語言 |
| `i18n.languages[0]` | `locale: zh, default: true` | `locale: en, default: true` | i18n plugin default |
| `i18n.languages[1]` | `locale: en` | `locale: zh` | i18n plugin secondary |
| `nav_translations` | zh→en mapping | en→zh mapping | 導航列翻譯 |
| `extra.alternate[0]` | `lang: zh` (default) | `lang: en` (default) | 語言切換按鈕 |
| `search.lang` | `[zh, en]` | `[en, zh]` | 搜尋語言優先序 |
| `exclude_docs` | `README-root.en.md` | `README-root.zh.md` | Symlink 排除 |

### 2.4 MkDocs 遷移的原子性要求

⚠️ **檔案 rename 和 mkdocs.yml 修改必須在同一個 commit 中完成**。

原因：i18n plugin 的 `default: true` 決定了 `foo.md` 是哪個語言。如果只 rename 檔案但不改 mkdocs.yml，plugin 會把原本的中文 `foo.md` 當成英文（因為 rename 後 foo.md 的內容已經是英文），但 plugin 的 locale 仍期待中文。反之亦然。

**結論**：不可能做漸進式遷移（directory-by-directory），必須全量一次遷移。

### 2.5 CI 影響

MkDocs Build Verification CI 會在 PR 上跑 `mkdocs build`。全量遷移 PR 會是一個大 diff（66 對 × 2 = 132 個檔案 rename + mkdocs.yml），但 CI 可以一次驗證。

### 2.6 nav 項目

`mkdocs.yml` 的 `nav:` section 引用的都是 `foo.md`（不帶 suffix），遷移後這些引用不需要改（因為 English 內容就在 `foo.md` 裡），但 `nav_translations` 需要反轉。

---

## 3. 建議遷移方案

### 3.1 全量遷移 PR（建議在 Phase .e 或 v2.8.0）

```bash
# Step 1: 全量 rename
python3 scripts/tools/dx/migrate_ssot_language.py --execute --git

# Step 2: 更新 mkdocs.yml（手動，見 §2.3 清單）

# Step 3: 更新 nav_translations（反轉 zh↔en mapping）

# Step 4: 驗證
mkdocs build --strict
pre-commit run bilingual-structure-check --all-files
pre-commit run bilingual-content-check --all-files

# Step 5: Commit + PR
```

### 3.2 風險緩解

| 風險 | 緩解措施 |
|------|---------|
| MkDocs build 失敗 | 先在 local build 驗證再推 PR |
| 外部連結到 `.en.md` 404 | README 公告 + 考慮 redirect |
| Git 歷史追蹤困難 | `git mv` 保留 rename 記錄 |
| PR 太大 reviewer 疲勞 | 純 rename + 機械性修改，review 聚焦 mkdocs.yml |

### 3.3 不在 v2.7.0 做全量遷移的原因

1. 全量遷移是 132 個檔案的 rename — 與其他 v2.7.0 功能 PR 衝突風險高
2. MkDocs 原子性要求意味著不能漸進式遷移
3. Phase .d 的價值在於 **準備工具和驗證可行性**，actual switch 可以延後

---

## 4. Phase 1 交付物清單

- [x] `migrate_ssot_language.py` — 遷移腳本（--dry-run / --execute / --check）
- [x] `check_bilingual_structure.py` — 支援 .zh.md 雙模式
- [x] `check_bilingual_content.py` — 支援 .zh.md 雙模式
- [x] `.pre-commit-config.yaml` — trigger pattern 擴展
- [x] 本報告 — Pilot 結果 + MkDocs 評估 + 遷移方案
- [x] CLAUDE.md 語言指引更新
- [x] `dev-rules.md` 語言指引更新
