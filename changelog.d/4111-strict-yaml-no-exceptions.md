---
section: Changed
topic: tools
issues: [4111]
created: 2026-11-21T16:00:00+00:00
---
- **YAML 只有一種讀法：重複鍵在客戶檔案也是錯，init 點名請你修（tools；[#4111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4111)）**：⚠️ breaking。`da-tools init` 讀你的根目錄 `.gitlab-ci.yml` 不再照 GitLab 的讀法（重複鍵取後者）讀；遇到同一個 mapping 裡重複的鍵，會點名鍵與行號、說明 GitLab 會靜默只取最後一個，不給接線片段、也不再判成「需要附加 include」或「無法解析」，請先修正再重跑（`--dry-run` 同樣提示）。`.da-init.yaml` 有重複鍵時同樣點名，不據它刪任何檔。repo 內的閘門改為：所有被追蹤的 Python 檔（測試除外）在 `_lib_yaml_strict` 以外引用 PyYAML／ruamel.yaml 的讀取入口或 `construct_object` 即失敗，沒有例外清單。
