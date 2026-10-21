---
section: Changed
topic: tenant-api
issues: [3206]
created: 2026-10-21T06:41:11+00:00
---
- **⚠️ breaking：tenant-api 寫入與自己讀的管理設定改用 `da-guard parse-tree` 的判定（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 7）**：寫入本文被判為讀不了時回 400（狀態碼不變，錯誤文字改為 `parse-tree refuses it (kind …, line N)`）；以前放行的只有 `---`、註解或 `~` 的尾端文件、`!!binary`、`!!set`、本地標記 `!foo`、非特定標記 `!` 改為拒收。整檔寫入要取代的原檔被判為讀不了時視同無法解析（須全租戶寫入權限）。`_tenant_orgs.yaml`、`--rbac` 的 RBAC 檔（不論是否在 conf.d 內）、群組、視圖、聯邦白名單與子集合、AccountID 帳本：這些寫法改為解析失敗，不當成空檔——RBAC 與 `_tenant_orgs.yaml` 啟動失敗、熱重載保留上一版；群組、視圖、白名單啟動時 WARN 並以空設定起；改寫群組或視圖回 500 `CONFIG_DECODE`；帳本匯入失敗即啟動失敗。升級前以新版 da-guard 的 `parse-tree` 單檔模式（`--config-dir <目錄> --path <檔名> --input <檔>`）驗這些檔。
