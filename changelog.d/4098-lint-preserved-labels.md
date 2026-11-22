---
section: Changed
topic: tools
issues: [4098, 4137]
created: 2026-11-21T05:49:53+00:00
---
- **`lint` 的租戶隔離檢查改看 alert 的結果是否保留 `tenant`，依 Prometheus 語法樹判定（tools；[#4098](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4098)）**：⚠️ breaking。政策鍵 `denied_patterns` 移除（還寫著它時結束碼 `2`），改為 `preserved_labels: [tenant]`。舊的文字比對讓 `without(tenant, b)`、`=~'.*'` 繞過，也從不擋 `sum by (a)`、不分組的 `sum(…)`、`ignoring(tenant)`；這些都讓告警的 `{{ $labels.tenant }}` 變成空的。現在丟掉 tenant 與無法證明保留（`label_replace` 寫入 tenant、不在本次 lint 的 recording rule 等）都是 ERROR。alert 把 `tenant` 寫成字面值時不判定。原始指標依平台 scrape 設定判定（[#4137](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4137)）：直接讀 KSM、Prometheus、Alertmanager 的指標，或非 tenant-exporters job 的 `up`／`go_*` 等，都是丟掉。`=~".*"` 選到的 series 與拿掉它相同，不再列管。
