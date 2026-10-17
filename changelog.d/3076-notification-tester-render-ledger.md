---
section: Changed
topic: da-tools
issues: [3076]
created: 2026-10-17T07:40:13+00:00
---
- **`test-notification` 只測路由產生器實際產出的 receiver；產生器為主 receiver 沒產出的租戶逐一點名被丟掉的子項目（[#3076](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3076)）**：先前工具自己列舉 `_routing`，產生器在路由層略過的項目（override 缺 `alertname`／`metric_group`、`routes[i]` 有不支援的 key、主 receiver 被拒或缺少時的每個 `overrides`／`routes` 項目）仍報 `DRY_RUN` 或照送。現在受測清單取自產生器對每個項目的判定：這些項目不送出，標新狀態 `NOT_RENDERED` 並附產生器的原因，算失敗；沒寫 `receiver`、`receiver` 沒有 `type` 或不是物件的主 receiver／override／`routes` 項目（先前直接不列）標 `INVALID_CONFIG`。結束碼變化：上述情況在 `--ci` 下由 `0` 變 `1`；不帶 `--ci` 時仍為 `0`，但 `--json` 的 `status` 由 `pass` 變 `fail`、`summary.failed` 與 `total_receivers` 隨之改變。`generate-routes --validate` 結束碼不變，但每個被丟掉的子項目多一行 `parent_receiver_not_rendered` 的 WARN；`explain-route` 的生效／略過子路由改取同一份判定，主 receiver 缺少或被拒的租戶也逐一列出（`skipped_sub_routes`），結束碼不變。
