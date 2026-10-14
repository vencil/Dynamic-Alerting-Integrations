---
section: Added
topic: da-tools
issues: [2901]
created: 2026-10-14T20:24:32+00:00
---
- **部署端 policy 檔新增選填鍵 `min_generator_version`（generate-routes；ADR-036 §6；[#2901](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2901)）**：`--policy` 指定的檔可宣告 `min_generator_version: "X.Y.Z"`。執行的 da-tools 比它舊、或讀不到自己的版本時，`generate-routes` 結束碼 2 並說明修法（調高 da-tools 版本）；值不是 `X.Y.Z` 形式時整份 policy 不可用（同為結束碼 2）。只從 `--policy` 讀，寫在 conf.d 裡的 policy 檔不生效。沒寫這個鍵時行為不變。
