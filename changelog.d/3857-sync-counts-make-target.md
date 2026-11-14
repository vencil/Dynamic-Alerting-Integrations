---
section: Added
topic: dx
issues: [3857]
created: 2026-11-14T08:38:27+00:00
---
- **新增 `make sync-counts`，計數對帳與寫回改走 make 入口（dx；[#3857](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3857)）**：直接跑 `bump_docs.py --sync-counts` 需要 da-read，在沒有 `.build/`、也沒設 `DA_READ_BINARY` 的乾淨樹上會以 exit 2 失敗（`da-read was not found`）。新目標跟 `make version-check` 一樣，缺 da-read／da-crdecode 時先建再跑。`CLAUDE.md`、`commit-convention.md`、`hook-vs-skill-coverage.md`、`vibe-workflow`／`vibe-release` skill 與 paths-map 改成指向這兩個 make 目標：只檢查用 `make version-check`，要寫回計數用 `make sync-counts`。
