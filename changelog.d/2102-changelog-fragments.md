---
section: Changed
topic: dx
issues: [2102]
created: 2026-09-26T15:40:04+00:00
---
- **還沒發布的變更改寫在 `changelog.d/` 片段檔，`CHANGELOG.md` 的 `[Unreleased]` 凍結（dx、lint；[#2102](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2102)）**：每個變更一個檔案（YAML front matter 帶 `section` / `topic` / `issues` / `created`，本文恰好一個條目），並行的 PR 不再因為都接在 CHANGELOG 同一段末尾而互相衝突。`generate_changelog.py --fragments` 做 lint（pre-commit `changelog-fragments`），`--assemble` 依 section → topic → created 組裝成發版原料；`--lint` 對 `[Unreleased]` 新增的條目報錯並指向片段目錄。格式與規則見 `changelog.d/README.md`。
