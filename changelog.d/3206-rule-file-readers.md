---
section: Changed
topic: da-tools
issues: [3206]
created: 2026-10-29T03:00:00+00:00
---
- **讀 Prometheus 規則檔的工具一律改經 `da-read prometheus-rules`（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：rule pack、ConfigMap 內的規則檔（外層經 `da-crdecode`）與 PrometheusRule 的 `spec` 都以 `rulefmt.Parse` 讀，需要 `da-read` 與 `da-crdecode`，沒有就 rc 2，不退回 PyYAML。⚠️ 值改為 Prometheus 的讀法：`for`／`interval` 印成正規寫法（`60s` → `1m`），label 值照原文是字串（`010` 不再是 8、`true` 不再是布林、未加引號的 `null` 是空字串）。⚠️ Prometheus 拒收的檔（不認得的鍵如 vmalert 的 `type`、`interval: 60`、PromQL 錯誤、第二份有內容的文件）：lint 點名檔案與原因、照各自的 exit code；產生器（ConfigMap、CRD、規則表、`platform-data`、`operator_generate`）直接失敗，不再略過或記 0。`migrate_rule` 的 label 值一律加雙引號。重產的規則 ConfigMap 與 operator CRD 以 Prometheus 的讀法輸出（labels／annotations 依字母序、duration 用正規寫法），一次性 diff，內容不變。
