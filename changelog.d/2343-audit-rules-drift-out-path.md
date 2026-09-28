---
section: Fixed
topic: dx
issues: [2343]
created: 2026-09-28T16:44:44+00:00
---
- **`audit_rules_drift --out` 指到 repo 外或給相對路徑時不再寫完檔才 rc 1（dx；[#2343](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2343)）**：先前報表已寫出，接著組 `wrote drift report:` 訊息時丟 `ValueError`、帶 traceback 以 rc 1 結束，看起來像沒寫成。現在路徑先 resolve：落在 repo 內就印相對路徑，否則印絕對路徑，rc 0。
