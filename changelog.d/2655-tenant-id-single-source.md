---
section: Changed
topic: confd-family
issues: [2655]
created: 2026-10-03T07:04:11+00:00
---
- **tenant id 規則改由 schema 單一來源提供（[ADR-035](docs/adr/035-tenant-id-single-source.md) accepted；[#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655)）**：ADR-035 已由 owner 核可，tenant id 的最終規則定為 DNS-1123 label。規則只寫在 `tenant-config.schema.json` 的 `definitions.tenantId`（pattern 加一段說明文字）：Python 在 runtime 讀它，讀不到時 fail-closed；Go（`pkg/tenantid`）與 portal 讀由 `make tenant-id-json` 產生的 JSON 副本，pre-commit 與 CI 以 `tenant-id-json-check` 擋副本漂移。三方以同一份案例表互相釘住。本次**行為不變**：產生器、da-guard、tenant-api、portal 仍用原本的規則，改用新規則的 breaking 變更在後續 PR。
