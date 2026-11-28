---
section: Fixed
topic: docs
issues: [4251]
created: 2026-11-28T06:58:18+00:00
---
- **網站上 rule-pack 頁面的版號納入檢查與 release 同步；建站失敗不再留下暫存複本（docs；[#4251](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4251)）**：`rule-packs/README.md`、`ALERT-REFERENCE.md`、`ALERT-REFERENCE.en.md` 由 mkdocs hook 在建站時發布到網站，但版號檢查只掃 `docs/`，三頁的 frontmatter 一直停在 v1.12.0。現在 `check_frontmatter_versions` 與 `bump_docs` 都從 hook 的頁面清單讀這三頁、在 `rule-packs/` 原檔上判定與改寫，兩支生成器也改從平台版號的唯一來源寫入；三頁改為 v2.9.0。hook 在建站失敗時也會刪掉 `docs/rule-packs/` 暫存複本，每次建站前先清掉上一次留下的，版號檢查也不再讀這份複本。
