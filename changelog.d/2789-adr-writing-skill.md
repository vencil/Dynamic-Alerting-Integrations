---
section: Added
topic: agent-guidance
issues: [2789]
created: 2026-10-09T16:20:51+00:00
---
- **新增 `vibe-adr-writing` skill，寫或改 ADR 時套用（dx；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：規定 ADR 讀者要帶走的五件事（決策、脈絡、選項與取捨、後果與限制、可對照的範例），列出要拿掉的內部代號、PR／票號歷史敘事與未解釋的名詞，並要求描述行為的句子在當下實測、量不到就刪，附自檢清單。編輯 `docs/adr/**` 時 paths-map 會提示先讀它。已照它改寫 ADR-001～013、016、017、019～021、023～028、030～036、038：只改表達、不改決策，拿掉內部代號與歷史敘事，描述行為的句子逐句對照現行行為；程式碼、chart 註解與告警描述裡指向舊段名的引用一併改到新段名。ADR-016 的「由目錄路徑推斷 `_metadata` 預設值」從未實作，已撤回（見另一條）。改寫時查到的未實作項目照實寫成限制，不再描述成現行行為：ADR-006 的 1:N 映射規則沒有掛進平台附的 Prometheus、平台規則包也不讀映射後的序列；ADR-013 的 `token_density` 比值沒有實作。
