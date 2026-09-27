---
section: Removed
topic: dx
issues: [1735]
created: 2026-09-27T16:40:00+00:00
---
- **退役雙語翻譯量檢查（lint；[#1735](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1735)）**：刪除 `scripts/tools/lint/check_translation.py`、manual-stage hook `translation-check` 與 `validate_all` 的 translation 列。兩個入口都沒有傳 `--ci`，這支工具在任何入口都不會失敗；它的 heading 計數不認 code fence，會把區塊內的 `#` 註解算成標題，而 heading 對齊本來就由 auto-stage 的 `check_bilingual_structure.py --ci` 負責。`validate_all` 已沒有無法失敗的列。
