---
section: Fixed
topic: dx
issues: [2335]
created: 2026-09-28T17:09:30+00:00
---
- **`generate_crd_schemas` 不再於 import 時改寫全域 `yaml.SafeLoader`（dx；[#2335](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2335)）**：讀上游 CRD 需要的 YAML 1.1 `=`（`!!value`）constructor 改掛在模組私有的 `_CRDLoader` 上，只用於讀 CRD 的那一處。先前只要同一行程先 import 過這支工具，`yaml.safe_load` 與 exporter-key／strict loader 就會開始接受 `=`，行為與測試結果因此依賴 import 順序。vendored CRD schema 產出不變。
