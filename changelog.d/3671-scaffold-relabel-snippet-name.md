---
section: Changed
topic: tools
issues: [3671]
created: 2026-11-11T00:20:00+00:00
---
- **⚠️ BREAKING：`scaffold --namespaces`（N:1）的 relabel 片段改名為 `relabel_configs-<tenant>.txt`（tools；[#3671](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3671)）**：這個 Prometheus 片段寫在 conf.d 檔旁邊，舊檔名 `relabel_configs-<tenant>.yaml` 會被 exporter 當成租戶檔讀，頂層的 `relabel_configs:` 沒有讀取端讀，共用判決因此回報 `tenant_file_top_level` 並擋下整棵樹。內容不變，引用舊檔名的腳本要改用新檔名。scaffold 寫在 conf.d 批次以外的檔，現在都先以 da-guard 的名稱判定確認不是設定檔；若是設定檔就以結束碼 2 拒寫，什麼都不寫。
