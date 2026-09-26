---
section: Fixed
topic: da-tools-cli
issues: [1044, 1112, 1168, 1494]
created: 2026-09-26T17:00:00+00:00
---
- **da-tools 命令與出貨映像的執行期缺陷**：出貨映像補齊 `threshold-recommend`／`threshold-govern` 缺漏的模組與資料檔，並修掉 HEAD build 後 `generate-routes`／`byo-check` 在扁平佈局下 import 即失敗，另以守衛在映像佈局下逐支 import 出貨工具。`cardinality-forecast` 與 `discover-mappings --prometheus` 因 PromQL 未 URL-encode 而 crash 已修；`rule-pack-split` 對現行 rule-packs 恆 exit 1 並靜默丟 group，改為依每條 rule 的資料落點分流。傳統 Windows console（cp950 等）上 `--help` 不再 crash；`batch-diagnose --json` 不再偶發 rc 0 卻輸出空白。⚠️ 十支 `--prometheus` 工具 standalone 執行也會讀 `$PROMETHEUS_URL`，明確給的 `--prometheus=URL` 不再被環境變數蓋掉；`config-history` 的 `DA_LANG=en` 不再輸給 `LC_ALL=zh`；`runtime-audit --help` 改依 `DA_LANG` 切換；`migrate-to-operator --checklist-only` 的 Rule 群組數不再恆為 0（[#1494](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1494)、[#1044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1044)、[#1112](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1112)）。
