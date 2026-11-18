---
section: Changed
topic: tools
issues: [4027]
created: 2026-11-18T15:05:09+00:00
---
- **⚠️ BREAKING：analyze-gaps 的 `--config-dir` 讀整個 UTC 日，同一棵樹不論幾點跑結果都一樣；`--json` 的 `current_value` 依來源改為 `served`／`written`（tools；[#4027](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4027)）**：先前只讀執行那一刻的值，排程停用的時段裡跑，custom_ 鍵會從報告消失；某時段 `/metrics` 收集不了時，只在那段時間 rc 2。現在列入哪些鍵照 da-guard 的答案（一天中任一時段有發出，或被 `expires:` 限時），`served` 逐段列出整天的值（停用的段 `value: null` 帶 `reason`），文字報告標 `[scheduled: …]`；`expires:` 限時的鍵另帶 `expires`、`expired`，未到期時標 `[until …]`；由新的 `--at`（預設現在，只用於 `--config-dir`）判斷是否過期。一天中有收集不了的時段時，不論幾點都 rc 2、點名時段。`--tenant-config` 的值改名 `written`。
