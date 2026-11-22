---
section: Changed
topic: tools
issues: [4111]
created: 2026-11-21T15:52:31+00:00
---
- **Python 讀 YAML 只有一種讀法：照規格，重複鍵即拒收（tools；[#4111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4111)）**：⚠️ breaking。工具、workflow 與 shell 內嵌的 PyYAML 讀取一律改經 `_lib_yaml_strict`，同一個 mapping 重複的鍵即拒收（先前靜默取後者），閘門擋下其他讀法，沒有例外清單。`da-tools init` 讀你的根目錄 `.gitlab-ci.yml`：v2.9.0 照 GitLab 取後者來判斷「已接好」或「需要加 include」，判的可能不是你在檔案裡看到、會去改的那一段；現在點名重複的鍵與行號、不給接線片段，請修正後重跑（`--dry-run` 同）。`.da-init.yaml` 有重複鍵時同樣點名，不據它刪任何檔。`verify-diff --rules` 改走宣告輸入的唯一解碼。vector 的 projection-gate（image 0.1.3、chart 0.12.3）遇到重複鍵的 registry 改判 registry_unreadable 並降級，先前會照後者放行跨租戶的 projection。
