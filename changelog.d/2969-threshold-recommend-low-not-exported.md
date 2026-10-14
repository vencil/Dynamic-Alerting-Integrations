---
section: Changed
topic: threshold-governance
issues: [2969]
created: 2026-10-14T13:00:01+00:00
---
- **`threshold-recommend`：信心等級 LOW 的建議不再寫成 `--export-patch` 的值行，也不再算進 `recommended_changes`（[#2969](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2969)）**：先前只要 \|delta\|≥5% 就產生值行，樣本不足的建議照抄就會被套用；現在與 `threshold-govern` 用同一道信心門檻（HIGH／MEDIUM），LOW 改以整行註解 `# [租戶] (low confidence) key: recommended X, was Y, -Z% — <reason>` 列出。⚠️ 行為變更：`--json` 的 `summary.recommended_changes` 只算可套用的建議，拿它當門檻的腳本數字會變小；LOW 另計在新欄位 `low_confidence_changes`（summary 與每個租戶都有），文字報表標題寫成 `0/1 changes recommended, 1 low-confidence`。整份 patch 只有 LOW 時，固定標頭後的第一行改為說明下方列了幾筆 low-confidence 建議、沒有可套用的值行。每天只有幾小時資料的間歇序列因此不會有值行，需要時請從註解手動抄寫。
