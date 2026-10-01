---
section: Changed
topic: da-tools
issues: [2542]
created: 2026-09-30T23:58:57+00:00
---
- **`da-tools init` 產物裡兩段給客戶的操作說明改由照做的結果守住（測試；[#2542](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2542)）**：`.pre-commit-config.da.yaml` 開頭的合併指示，現在會被解析成「搬哪一項、搬進哪個清單」並實際合併進一份既有設定，原有 hook 必須還在；整份 append 則證實會讓原有 hook 消失。`kustomize/base/README.md` 叫客戶打的 `kustomize build` 那一行，會在照 README 建好 symlink 的產出樹上原樣執行，拿掉 `--load-restrictor` 後必須失敗。CI 的 kustomize 步驟一併執行這個新測試模組，缺 kustomize 時是失敗而不是略過。產物本身沒有改變。
