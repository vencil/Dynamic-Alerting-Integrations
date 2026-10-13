---
section: Changed
topic: tenant-api
issues: [2491]
created: 2026-10-13T23:18:31+00:00
---
- **tenant-api：寫入模式與 RBAC 路徑的空值改為拒絕啟動（tenant-api；[#2491](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2491)）**：`TA_WRITE_MODE` 設成空字串（常見於模板沒渲染到值）原本會靜默變成 `direct`，現在拒絕啟動；不設仍是 `direct`。RBAC 路徑同理：`TA_RBAC_PATH` 設成空字串或 `--rbac=` 不再進入 open-read，而是拒絕啟動；要 open-read 就完全不設 RBAC 路徑。官方 helm chart 以命令列參數帶入非空值，不受影響。⚠️ breaking：用空值取得 `direct` 或 open-read 的部署，升級後要改成不設該變數。
