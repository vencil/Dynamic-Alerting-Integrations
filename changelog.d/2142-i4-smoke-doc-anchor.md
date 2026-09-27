---
section: Fixed
topic: ci-pipeline
issues: [2142]
created: 2026-09-27T21:59:37+08:00
---
- **I-4 runbook smoke test 手抄的指令與 troubleshooting-checklist 綁在一起（ci；[#2142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2142)）**：smoke test 跑的是手抄進腳本的副本，不讀文件，所以文件裡的指令改了，它照樣綠。新增 `tests/lint/test_i4_runbook_doc_anchor.py`：腳本裡每一個 `assert_*` 呼叫的 payload，都必須逐字出現在它所標示章節的 fenced block 內，中英兩版都要；刻意改寫的呼叫與 helper 以外的檢查，要在腳本裡用 `# i4-doc-anchor:` 標記並寫出理由。對齊時修正了兩處章節標錯（一格是重複的，已刪除），也把 job 註解與腳本檔頭改成實際範圍。仍不涵蓋腳本沒抄的指令。
