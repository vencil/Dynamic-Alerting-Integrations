---
section: Changed
topic: exporter
issues: [3475]
created: 2026-11-01T01:30:00+00:00
---
- **⚠️ breaking：維度鍵改由根 `_defaults.yaml` 的 `dimensions:` 宣告哪些 label 會送出（ADR-038 決定 11）**：`<base>{label="v"}` 只有在它送出的每個 label 都為該 base 宣告過時才送出，不論寫在哪一層；regex 選擇器送出 `<label>_re`，要宣告的是那個名稱。沒宣告的不送出，共用判決報 `unread … (no_row)`、tenant-api 寫入擋下；例外是值寫 `disable` 的報 `disable_noop`（警告），Policy-as-Code 規則指名的讀成 `read`，兩者同樣沒有列。原因是出貨的 rule pack 以 `max by(…)` 合成門檻，只保留分組裡的 label（多數只有 `tenant`）；不在分組裡的 label 無法成為獨立維度，它的值會併進同一組的門檻。出貨的 `dimensions:` 來自 `scaffold_tenant.RULE_PACKS` 每個 key 的 `dimensions`：`python3 scripts/tools/lint/check_threshold_registry.py --regen` 由它重生門檻登錄表、helm `thresholdConfig.dimensions`、開發用與 try-local 的 `_defaults.yaml`，`da-tools init` 與 `scaffold_tenant` 產生 `_defaults.yaml` 時也直接讀它；目前是 `container_cpu`、`container_memory` 的 `version`；原本手寫的 version 試行清單移除。**遷移**：根 `_defaults.yaml` 不是 Helm 渲染的部署（GitOps、`existingConfigMap`、kustomize）升級前要加上 `dimensions: {container_cpu: [version], container_memory: [version]}`，否則版本門檻不再送出、tenant-api 也擋寫入。自訂規則依某個 label 讀時，再為該 base 加上那個 label；沒有規則讀的維度鍵拿掉即可。
