---
name: vibe-paths-docs
description: "Vibe 路徑觸發指引：動到 docs/**/*.md 之前先讀的章節與一句約束。"
paths:
  - "docs/**/*.md"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-docs

先讀：
- `docs/internal/dev-rules.md` §10. 雙語政策
- `docs/internal/dev-rules.md` §4. Doc-as-Code

約束：`python3 scripts/tools/lint/check_doc_links.py --ci` 驗連結與錨點；`docs/internal/**` 不需 `.en.md`，其餘外部面向文件雙語同步；push 時 mkdocs strict 對被推的 commit 建站。
