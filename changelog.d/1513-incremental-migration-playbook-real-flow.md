---
section: Fixed
topic: cli-docs-accuracy
issues: [1513, 1818]
created: 2026-09-27T09:10:00+00:00
---
- **漸進式遷移 Playbook 的階段 1–4 改成工具與元件真的行為（docs；[#1513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1513)、[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)）**：Helm 改用 OCI chart 與 `thresholdConfig`，並註明 redis 預設值要自己補；補上注入 `tenant` 標籤的 relabel 步驟；指標名與 recording rule 名改成真的；雙軌改成 `generate-routes` 產生的 route 手動加 `continue: true` 與 catch-all（`--apply`／`--output-configmap` 會取代整個 `route.routes`）；切換改成手動刪舊規則並用 `promtool`／`amtool` 預演，註明 `da-tools cutover` 屬於 migrate／shadow 流程；Alertmanager 查詢改用 v2 API（v1 自 v0.27.0 起回 410）；`batch-diagnose`／`diagnose` 註明只查 MariaDB Pod；`grep -v` 刪規則的做法拿掉；`offboard`、`tenant-verify`、`alert-quality --period` 改成工具接受的寫法。
