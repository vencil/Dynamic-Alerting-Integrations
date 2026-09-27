---
section: Fixed
topic: cli-docs-accuracy
issues: [1818, 1513]
created: 2026-09-27T07:50:00+00:00
---
- **Shadow Monitoring 切換劇本的命令與預期輸出改成工具真的行為（docs；[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)、[#1513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1513)）**：`migrate` 改用位置參數與 `--output-dir`，產出清單改成實際的六個檔（原文的 `custom_rules.yaml` 不存在），並說明沒有「只轉某些租戶」的選項；`cutover --dry-run` 與實際執行的預期輸出改為實跑結果；`cutover --rollback` 與自動回退改寫為「尚未實作」，指向 SOP §7.2 的手動回退；`batch-diagnose --check-shadow-removal` 改為 `--tenants` 健康報告加 `kubectl get` 確認舊物件已刪除；`--force` 範例補上必填的 `--readiness-json`；`cutover-readiness.json` 的欄位名改成工具實際寫出的欄位。
