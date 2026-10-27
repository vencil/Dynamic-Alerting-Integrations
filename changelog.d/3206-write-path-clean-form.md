---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T14:12:04+00:00
---
- **`da-guard write` 的路徑必須是正規形式（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：輸入的 `path` 與 `from` 照收到的字串使用；`a//b.yaml`、`a/./b.yaml`、`a/../b.yaml`、`./a.yaml`、結尾 `/` 改為呼叫錯誤 `bad_path`（結束碼 2，什麼都不寫），訊息點名原字串與它清理後的路徑。原本 write 會悄悄清理後寫到清理後的位置，呼叫端若再拿原字串做事（例如更新 git index），指的就不是寫入的那個檔。
