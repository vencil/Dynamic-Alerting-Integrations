---
section: Changed
topic: da-tools-cli
issues: [1112, 1514, 1582, 1609, 1619, 1787, 1928, 1950, 19639]
created: 2026-09-26T17:00:00+00:00
---
- **⚠️ da-tools 寫入類命令與 `--json` 輸出的行為變更**：`patch-config` 改依 `tenants:` 宣告定位 key（`default` 清空租戶區塊時保留它）；apply 後逐 pod 驗收，動到其他租戶 series、新 parse failure、逾時或失聯即回滾；新增結束碼 `1`、`3`–`7` 與 `--exporter-*`、`--reload-timeout` 旗標，執行者需 `list pods`＋`get pods/proxy`；`--json` 不帶 `--diff` 會真的寫入（舊版回 `2`）；`--diff` 不再顯示現值（`before` 恆為 `null`）。`operator-generate`／`migrate-to-operator` 不給 `--output-dir` 時改印 stdout。`--json` 的 stdout 恰為一份 JSON，`operator-generate --json` 改為 `{crds, kustomization, summary}`。`deprecate_rule` 下架改為刪 key、不再把 `defaults:` 寫成 `disable`；rc `1` 擴及 key 級殘留、無載體或有載體未寫入（預覽同樣回 `1`），指到子樹的呼叫要加 `--plane subtree`。scaffold 與 portal YAML Validator 對飽和類 `*_critical` 鍵加純顯示的教育提示（[#1950](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1950)、[#1928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1928)、[#1582](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1582)）。
