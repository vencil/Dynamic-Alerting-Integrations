---
section: Changed
topic: confd-family
issues: [2906]
created: 2026-10-17T16:40:00+00:00
---
- **⚠️ Breaking：寫 conf.d 的工具改依 `da-guard parse-tree` 判定，寫入前再以 parse-tree 讀回確認（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：`patch-config`、`deprecate`、`init`、`offboard`、`threshold-govern`、`scaffold`、`onboard`、`migrate`、`migrate_conf_d.py`、`da_assembler.py`、`generate_tenant_fixture.py` 改什麼、改不改都依 parse-tree，PyYAML 只產生文字；寫入前讀回的鍵、結構、純量原文／型別／值必須正是預期，原檔讀不了或路徑沒有讀取端也不寫，一個檔都不動。沒有 da-guard 結束碼 2（`migrate_conf_d.py` 的 `--dry-run` 也需要；`make generate-fixtures`／`bench-e2e` 會先建）。PyYAML 會換掉寫法的值（`!!float 1`、未加引號的字串 `1e3`）讓整份檔拒寫。`onboard` 的 `_defaults` 建議改寫數字（加引號會讓 exporter 丟掉整個 `defaults:`）；`generate_tenant_fixture.py` 同 seed 輸出固定（domain `_defaults.yaml` 不再隨雜湊變），`--extra-defaults k=inf` 被拒；`deprecate` 不改寫沒選上的 `_defaults` 拼法；`patch-config` 不再因 `<<` 拒絕；`offboard` 只刪恰好只宣告該租戶的檔；`init` 以 parse-tree 判斷別的檔宣告了誰。
