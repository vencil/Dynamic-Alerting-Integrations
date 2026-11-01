---
section: Fixed
topic: confd-family
issues: [3478]
created: 2026-11-01T10:31:21+00:00
---
- **threshold-reachability 不再擋根 `defaults:` 的扁平維度鍵（`DIMENSIONAL-IN-DEFAULTS`；[#3478](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3478)）**：自 #2031 起 exporter 會把根 `defaults:` 的完整維度鍵當作每個沒寫它的租戶的預設送出（label 須在 `dimensions:` 宣告），檢查卻仍以「維度門檻只限租戶」擋下。扁平寫法改為放行，送不送由共用判決決定；值為 mapping 或巢狀路徑的寫法照舊報錯。文件範例檢查的維度那一半一併移除，migration-guide 同步。
