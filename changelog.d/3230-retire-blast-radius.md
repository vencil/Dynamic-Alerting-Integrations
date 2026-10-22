---
section: Removed
topic: da-tools
issues: [3230, 3042, 1516]
created: 2026-10-22T00:35:05+00:00
---
- **⚠️ BREAKING：退役 `blast_radius`，「這個 conf.d 變更影響誰」只由 config-diff 回答（tools；[#3230](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3230)）**：刪除 `scripts/tools/ops/blast_radius.py`、`.github/workflows/blast-radius.yml` 與 `custom_alerts/loader.py`。它與 config-diff 回答同一個問題，卻讀 `describe_tenant` 輸出裡 exporter 不送出的 `_custom_alerts` 聯集，也看不到 `_routing_defaults` 這類頂層鍵的變更。遷移：改用 `da-tools config-diff --old-dir <base> --new-dir <pr>`。`describe_tenant` 的 `effective_config` 改為 da-guard effective 的值原樣，`_custom_alerts` 不再是聯集、`_custom_alerts_resolution` 移除；default、`--show-sources`、`--all` 在 `effective_config` 旁新增 `custom_alerts`（每條的 `index`、`name`、`recipe`、`status`，與 `reason`／`recipe_id`；宣告了但 exporter 一條都不送時為 `[]`）。
