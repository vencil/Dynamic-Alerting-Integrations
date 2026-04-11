---
title: "文件模板規範"
tags: [internal, dx]
audience: [all]
version: v2.6.0
lang: zh
---

# 文件模板規範

> **受眾**：所有貢獻者 (All Contributors)
> **版本**：v2.3.0
> **相關文件**：[文件導航地圖](doc-map.md) · [開發規範](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)

本文件定義 Dynamic Alerting 平台所有 Markdown 文檔的標準結構與撰寫規範。遵循本模板能確保文件風格一致、可維護性高、易於自動化檢查。

---

## 1. Frontmatter（必須）

所有文件必須以 YAML frontmatter 開頭，包含以下必須欄位：

| 欄位 | 說明 | 範例 | 狀態 |
|------|------|------|------|
| `title` | 文件標題（英文或中文） | `"da-tools CLI Reference"` | 必須 |
| `lang` | 文件語言 | `zh` （繁體中文）或 `en` （英文）| 必須 |
| `tags` | 文件分類標籤（陣列） | `[cli, reference, da-tools]` | 必須 |
| `audience` | 目標受眾（陣列） | `[platform-engineer, sre, tenant]` | 必須 |
| `version` | 文件版本（與 release 版本同步） | `v2.3.0` | 強烈建議 |

**Frontmatter 範例**：

```yaml
---
title: "da-tools CLI Reference"
tags: [cli, reference, da-tools, tools]
audience: [platform-engineer, sre, devops, tenant]
version: v2.6.0
lang: zh
---
```

**受眾標籤定義**：

| 標籤 | 說明 |
|------|------|
| `platform-engineer` | 平台工程師、SRE、DevOps |
| `tenant` | 租戶業務系統負責人 |
| `domain-expert` | 領域專家（DBA、Infrastructure lead） |
| `all` | 適用所有人 |

---

## 2. 文件結構順序

按以下順序組織內容：

### 2.1 第一行：H1 標題

```markdown
# 文件標題
```

標題應清晰反映文件內容。

### 2.2 語言切換行（雙語文件可選）

如果支援多語言，在 H1 之後加上語言指示：

```markdown
> **Language / 語言：** | **中文（當前）** | [English](./filename-en.md)
```

或簡化版本：

```markdown
> **Language / 語言：** | **中文（當前）**
```

### 2.3 上下文區塊（適用對象、版本、相關文件）

在標題下方提供快速上下文：

```markdown
> **受眾**：Platform Engineers、SREs
> **版本**：v2.3.0
>
> **相關文件**：[Architecture](architecture-and-design.md) · [Troubleshooting](./troubleshooting.md)
```

或以表格形式（較為簡潔）：

```markdown
> **v2.6.0** | 適用對象：Platform Engineer、SRE
>
> 相關文件：[Architecture](./architecture-and-design.md) · [Troubleshooting](./troubleshooting.md)
```

### 2.4 簡介段落

一或兩句話說明文件用途，幫助讀者決定是否繼續閱讀。

**範例**：

```markdown
本文檔介紹 Dynamic Alerting 平台提供的運維工具與 Prometheus API 集成。
涵蓋常見的診斷、驗證和配置管理任務。
```

### 2.5 目錄（可選，長文件建議）

對於超過 500 行的文件，在簡介後加入「目錄」section：

```markdown
## 目錄

1. [快速開始](#快速開始)
2. [核心概念](#核心概念)
3. [API 參考](#api-參考)
```

### 2.6 主要內容

使用 H2（`##`）作為主要 section，H3（`###`）作為子 section。避免超過四層標題深度（H4 以下）。

**最佳實踐**：

- 每個 section 開頭加上一句話說明該 section 的目的
- 使用表格、代碼塊、清單等視覺化信息
- 提供實際範例（代碼、配置、命令行輸出）

### 2.7 常見問題（FAQ，如適用）

如果文件涉及複雜概念或常見誤解，加入 FAQ section：

```markdown
## 常見問題

### Q1：為什麼...？
A：...

### Q2：如何...？
A：...
```

### 2.8 相關資源（強制）

**所有文件必須以「相關資源」section 結尾。**

此 section 使用表格格式，列舉相關的文檔、工具、API 等資源。

**格式（繁體中文）**：

```markdown
## 相關資源

| 資源 | 說明 |
|------|------|
| [Title 1](link1) | Brief description or relevance |
| [Title 2](link2) | Brief description or relevance |
| [External Resource](https://example.com) | ⭐⭐⭐ (if highly relevant) |
```

**格式（English）**：

```markdown
## Related Resources

| Resource | Description |
|----------|-------------|
| [Title 1](link1) | Brief description |
| [Title 2](link2) | Brief description |
```

**表格欄位**：

| 欄位 | 說明 |
|------|------|
| 資源 (Resource) | 超連結 + 簡短標題（例 `[Playbook](./testing-playbook.md)`） |
| 說明 (Description) | 該資源與本文件的關係（一句話）或相關程度（星號 ⭐⭐⭐） |

---

## 3. 內容撰寫指南

### 3.1 語言與風格

- **語言**：繁體中文（Traditional Chinese）為主
- **英文**：技術術語、產品名稱保持英文（例：Prometheus、ConfigMap、PromQL）
- **觀點**：工程語言，避免營銷宣傳詞彙
- **個人代名詞**：避免「我們」（We），改用被動語態或直接描述事實

**不推薦**：「我們提供了一個強大的告警平台...」
**推薦**：「Dynamic Alerting 平台支援多租戶配置驅動告警...」

### 3.2 代碼與配置示例

- 所有代碼、YAML、PromQL 應包含在代碼塊中，並註明語言：

```yaml
# 配置示例
tenants:
  db-a:
    mysql_connections: "800"
```

```bash
# 命令行示例
da-tools diagnose --tenant db-a --config-dir conf.d/
```

```promql
# PromQL 示例
rate(requests_total[5m]) > 100
```

### 3.3 表格

使用 Markdown 表格組織信息。表頭必須使用分隔線：

```markdown
| 欄位 | 說明 | 範例 |
|------|------|------|
| Row 1 Col 1 | Row 1 Col 2 | Row 1 Col 3 |
```

### 3.4 列表

使用無序列表（`-` 或 `*`）或有序列表（`1.` `2.`）。避免過度嵌套（超過 3 層）。

### 3.5 強調與引用

- **粗體**：用於關鍵術語（例 `**threshold-exporter**`）
- **斜體**：用於強調概念（例 `*Config-driven architecture*`）
- **代碼引用**：用於代碼名稱、命令、變數（例 `` `user_threshold` ``）
- **引用塊**：用於重要提示或警告

```markdown
> **警告**：此操作無法復原。

> **提示**：使用 `--dry-run` 先預覽效果。
```

### 3.6 交叉引用

使用相對連結指向同目錄或上層目錄的文件：

```markdown
[Architecture Guide](./architecture-and-design.md)
[CLI Reference](../cli-reference.md)
[Playbook](./internal/testing-playbook.md)
```

不使用絕對 URL（除非指向外部資源，例 GitHub、Prometheus 官方文檔）。

---

## 4. 版本管理

### 4.1 版本號同步

文件 frontmatter 的 `version` 欄位應與當前 release 版本同步。

**更新觸發**：

- 平台版本升級時，執行 `make bump-docs` 自動更新所有文件的 `version: v*.*.*`
- 文件內容有重大改動時，在 commit message 中註明（例 `Update docs: add section on X`）

### 4.2 文件生命週期

新增文件時：

1. 從本模板複製 frontmatter
2. 設定 `version: v2.3.0`（當前版本）
3. 設定 `lang: zh` 或 `lang: en`
4. 選擇合適的 `audience` 標籤
5. 填寫內容
6. **確保有「相關資源」section**
7. 執行 `pre-commit run --all-files` 驗證

過期或廢止的文件應：

- 在首段加入棄用通知
- 設定重定向（在相關文件中手動補充連結提示）
- 逐步移除或封存至 `docs/archive/`

---

## 5. 自動化檢查

所有文件必須通過 `check_doc_template.py` lint 工具的檢查。

**檢查項目**：

1. ✓ Frontmatter 存在（文件開頭 `---`）
2. ✓ 必須欄位（`title`、`lang`）
3. ✓ 相關資源 section 存在（`## 相關資源` 或 `## Related Resources`）
4. ✓ 版本一致性（可選，當 `--check-version` 指定）

**執行方式**：

```bash
# 檢查所有文件
python3 scripts/tools/lint/check_doc_template.py

# 檢查特定目錄
python3 scripts/tools/lint/check_doc_template.py --docs-dir docs/getting-started/

# 自動修復（附加缺失的相關資源 section）
python3 scripts/tools/lint/check_doc_template.py --fix

# 檢查版本一致性
python3 scripts/tools/lint/check_doc_template.py --version v2.3.0 --check-version
```

---

## 6. 模板快速複製

新增文件時，使用以下範本：

```markdown
---
title: "Your Document Title Here"
tags: [tag1, tag2]
audience: [platform-engineer]
version: v2.6.0
lang: zh
---

# 文件標題

> **受眾**：描述目標受眾
> **版本**：v2.3.0
>
> **相關文件**：[Related Docs](./related.md)

簡介段落（一到兩句話）。

---

## 主要 Section 1

說明此 section 目的。

### 小 Section 1.1

內容...

---

## 主要 Section 2

內容...

---

## 相關資源

| 資源 | 說明 |
|------|------|
| [Documentation](./doc.md) | Link description |
| [Tool](./tool.md) | Tool description |
```

---

## 相關資源

| 資源 | 說明 |
|------|------|
| [文件導航地圖](./doc-map.md) | 所有 83 個文檔的完整列表與分類 |
| [開發規範](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) | 平台整體開發指引與架構速查 |
| [文件維護 Playbook](./testing-playbook.md#文件敘述風格) | 文件更新與版本管理的實務指南 |
| [Lint 工具使用](../cli-reference.md) | 文件 Lint 工具詳細用法 |
| [GitHub Release Playbook](./github-release-playbook.md) | 文件版本更新與 Release 流程 |
