---
section: Changed
topic: docs
issues: [1375]
created: 2026-09-28T05:37:17+00:00
---
- **文件站的標題錨點改用 GitHub 的規則（[#1375](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1375)）**：站上的標題 id 原本會吃掉中文、把連續空白收成一個，所以照 GitHub 規則寫的 `#中文錨點` 連結在站上找不到。現在兩邊共用同一支 slug 函式，已知的錨點問題從 207 筆降到 3 筆。站上舊的錨點網址仍然可用：找不到目標時，頁面會改找舊規則對應的標題並捲動過去。另修正 13 行以 `#數字` 開頭、在站上被誤渲染成大標題的句子。
