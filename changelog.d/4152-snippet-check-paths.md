---
section: Removed
topic: docs
issues: [4152]
created: 2026-11-22T15:00:28+00:00
---
- **文件站移除從未生效的縮寫提示，snippet 缺檔改為建站失敗（docs；[#4152](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4152)）**：`mkdocs.yml` 的 `auto_append` 路徑寫錯，縮寫表從來沒有套用到任何頁面；而照原樣修好會在英文頁顯示中文提示，且只涵蓋 glossary 約 50 個詞條中的 4 個。因此刪除 `auto_append`、`docs/includes/abbreviations.md`、生成它的 `sync_glossary_abbr.py` 與 `glossary-check` hook。`pymdownx.snippets` 改設 `check_paths: true`：文件引入的 snippet 檔不存在時，strict 建站直接失敗，不再渲染成空白區塊。術語仍以 `docs/glossary.md` 為準。
