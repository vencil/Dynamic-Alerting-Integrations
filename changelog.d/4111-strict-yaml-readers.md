---
section: Changed
topic: tools
issues: [4111]
created: 2026-11-21T15:52:31+00:00
---
- **Python 讀 YAML 一律經嚴格讀法，重複鍵即拒收（tools；[#4111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4111)）**：⚠️ breaking。43 個以 PyYAML 預設讀法讀的地方（遇到重複鍵靜默取後者）改經 `_lib_yaml_strict`，讀取點閘門另守「直接呼叫 PyYAML」只准出現在照消費者讀法的地方（目前只有 init 讀客戶的 `.gitlab-ci.yml`）。`init` 的 `.da-init.yaml` 重複鍵時判為讀不了、不刪檔；`verify-diff --rules` 改走宣告輸入的唯一解碼。vector 的 projection-gate（image 0.1.3、chart 0.12.3）遇到重複鍵的 registry 改判 registry_unreadable 並降級，先前會照後者放行跨租戶的 projection。`da-assembler --render-cr` 不再提示舊版留下的檔名。
