---
section: Added
topic: alertmanager-routing
issues: [2766]
created: 2026-10-09T17:16:09+00:00
---
- **`generate-routes --findings-json <PATH>`：結構化 finding（alertmanager-routing；[#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)）**：產生器印出的每一行 warning 與拒收訊息，另外以 JSON（`schema: da-tools.findings/v1`）寫到 PATH，每筆帶 `kind`（與 da-guard 同義者同名）、`severity`（SARIF 等級）、`blocks`（`always`／`strict`／`validate`／`never`）、`tenant`／`policy`／`file`／`field` 與原文 `message`，頂層附 `exit_code` 與 da-tools 版本。所有模式、每一種結束（含拒收與呼叫端錯誤）都會寫，先寫暫存檔再改名；stdout、stderr 與結束碼一個位元組都不變。domain policy、路由樹、四種拒收與略過／替換的條目已分類，其餘行暫為 `unclassified`。這是讓 tenant-api 改依 finding 判定寫入、不再比對錯誤字串的第一步。
