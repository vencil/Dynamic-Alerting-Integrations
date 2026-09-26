---
section: Fixed
topic: ci-pipeline
issues: [2102]
created: 2026-09-26T17:25:25+00:00
---
- **coverage 合併 job 不再因三片全被取消而變紅（ci；[#2102](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2102)）**：PR 有新 push 時舊 run 被取消，三片 coverage 都沒上傳資料的情況下，合併步驟原本因未比對到的 glob 直接失敗；現在與缺一、兩片時相同，只發 warning、不產生合併報告。
