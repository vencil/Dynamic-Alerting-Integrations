---
section: Changed
topic: ci
issues: [3935]
created: 2026-11-14T18:21:13+00:00
---
- **`Self-Review-Pass-2` 閘門豁免純文件 PR（ci；[#3935](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3935)）**：PR 裡沒有任何 commit 帶 trailer、但 merge-base diff 的每個改動路徑都是文件時，`Validate Self-Review-Pass-2 trailer` 改為 SKIP。文件只認副檔名：任何位置以 `.md` 結尾（大小寫敏感）的檔案才算，`docs/`、`changelog.d/` 底下的 schema、設定或腳本不算；任何深度的 `CLAUDE.md`／`AGENTS.md`／`SKILL.md`，以及 `.agents/`、`.claude/` 底下的檔案是規則、不算文件，照樣要 trailer。規則只定義在 `scripts/tools/dx/pr_preflight.py` 的 `is_doc_only_path()`，PR 模板改為指向它，拿掉原本的「doc-only／chore-only 可略」。diff 為空（例如成對的 revert）不豁免，git 出錯一律判失敗。
