---
section: Changed
topic: exporter
issues: [3475, 3556]
created: 2026-11-01T01:30:00+00:00
---
- **⚠️ breaking：維度鍵改由根 `_defaults.yaml` 的 `dimensions:` 宣告哪些 label 會送出（ADR-038 決定 11）**：`<base>{label="v"}` 只有在它送出的每個 label 都為該 base 宣告過時才送出，不論哪一層；regex 選擇器送出 `<label>_re`，要宣告的是那個名稱。沒宣告的不送出，判決報 `unread … (no_row)`、tenant-api 寫入擋下；例外：值寫 `disable` 的報 `disable_noop`（警告），Policy-as-Code 規則指名的讀成 `read`。rule pack 以 `max by(…)` 只留分組裡的 label，其他 label 的值會併進同組門檻。出貨的 `dimensions:` 來自 `scaffold_tenant.RULE_PACKS` 每個 key 的 `dimensions`：`check_threshold_registry.py --regen` 由它重生門檻登錄表、helm `thresholdConfig.dimensions` 與開發用、try-local 的 `_defaults.yaml`，`da-tools init`、`scaffold_tenant` 也直接讀它；目前是 `container_cpu`、`container_memory` 的 `version`；手寫的 version 試行清單移除。base 不能是 `<key>_critical`（#3556，da-guard 報 error）；既有 `<key>_critical{…}` 改寫成 `<key>{…}: "N:critical"`。**遷移**：根 `_defaults.yaml` 不是 Helm 渲染的部署（GitOps、`existingConfigMap`、kustomize）升級前要加上 `dimensions: {container_cpu: [version], container_memory: [version]}`，否則版本門檻不再送出、tenant-api 也擋寫入。自訂規則讀某個 label 時再為該 base 加上它；沒有規則讀的維度鍵拿掉即可。
