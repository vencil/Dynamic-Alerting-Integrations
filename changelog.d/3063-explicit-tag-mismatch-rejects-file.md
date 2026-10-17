---
section: Changed
topic: exporter
issues: [3063]
created: 2026-10-17T06:26:37+00:00
---
- **⚠️ breaking：泛型解碼（解成 `any`）失敗的租戶檔整份拒收（exporter、tenant-api、da-guard；[#3063](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3063)）**：exporter 過去只做型別化解碼，不解析純量的顯式 tag、不讀沒有欄位的頂層 key，於是這類檔 exporter 照常服務，`/effective`、merged_hash、路由產生器卻拒收，tenant-api `GET /api/v1/tenants/{id}` 回 500、列表判為健康。判準是泛型解碼失敗，不是 tag 清單：最常見的是 tag 與內容不符（`mysql_connections: !!int abc`），也包括未知頂層 key（如 `x-anchors:`）內的重複 key、壞 tag（`!!float abc`）、`<<: 5`、自我引用 alias。現在整份跳過（`WARN: skip unparseable file`、`da_config_parse_failure_total` 上升，熱重載與重啟皆同），同檔所有租戶從 `/metrics` 消失；tenant-api 回 200 `config_error: invalid_config`，`/effective` 回 404。tag 與內容相符（`!!int 5`、`!!str 5`）不受影響。修法：改正該處；以 API 修需整檔 `PUT` 且對所有租戶有寫入權限（#2405），否則改 git。已知差異：自我引用 alias 的檔路由產生器（PyYAML）讀得了、exporter 拒收；整檔 `PUT` 本就擋下，只有直接改 git 會遇到，有上述訊號與 da-guard exit 3。`_` 平台檔不套用此判準（[#3131](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3131)）。
