---
section: Changed
topic: tools
issues: [4111]
created: 2026-11-21T15:52:31+00:00
---
- **Python 讀 YAML 一律經嚴格讀法，重複鍵即拒收（tools；[#4111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4111)）**：⚠️ breaking。43 個以 PyYAML 預設讀法讀的地方（遇到重複鍵靜默取後者）改經 `_lib_yaml_strict`，直接呼叫 PyYAML 一律由閘門擋下，沒有例外（見 `4111-strict-yaml-no-exceptions` 片段）。`init` 的 `.da-init.yaml` 重複鍵時點名該鍵、清冊視為讀不到、不刪檔；`verify-diff --rules` 改走宣告輸入的唯一解碼。vector 的 projection-gate（image 0.1.3、chart 0.12.3）遇到重複鍵的 registry 改判 registry_unreadable 並降級，先前會照後者放行跨租戶的 projection。
