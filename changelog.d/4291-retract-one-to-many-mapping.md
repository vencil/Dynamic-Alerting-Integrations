---
section: Removed
topic: tools
issues: [4291]
created: 2026-11-28T16:08:06+00:00
---
- **⚠️ Breaking：撤回一個實例對多個租戶（1:N）的 recording rule 映射（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)；[#4291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4291)）**：產生的 `tenant_mapped:*` 序列沒有任何規則包讀，也沒掛進平台附的 Prometheus，告警仍落在 namespace 那個租戶。移除 `da-tools discover-mappings`（`discover_instance_mappings.py`）、`generate_tenant_mapping_rules.py`、範例 `_instance_mapping.yaml`，以及 `scaffold --topology=1:N`、`--mapping-instance`、`--mapping-filter`（現在一律以結束碼 2 拒收）；portal 的 CLI playground 與 config-lint 的對應項目一併拿掉。遷移：共用實例改為每個 schema 各跑一個 exporter；拿不到各 schema 帳號時，在抓取時自己寫 `metric_relabel_configs` 改寫 `tenant`（寫法見 ADR-006）。設定目錄裡留著的 `_instance_mapping.yaml` 本來就沒有 exporter 讀，可以直接刪掉。
