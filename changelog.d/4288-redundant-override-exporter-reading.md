---
section: Fixed
topic: da-guard
issues: [4288]
created: 2026-11-24T23:32:38+00:00
---
- **`redundant_override` 改以 exporter 讀到的值比對（da-guard；[#4288](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4288)）**：以前比的是 YAML 的值與型別，根目錄 `_defaults.yaml` 寫數字 `80`、租戶檔依 schema 寫字串 `"80"` 這種一般寫法抓不到重複覆寫。現在頂層的鍵以 exporter 的讀法比：`80`、`"80"`、`"80.0"`、`"80:warning"` 視為同值，`"80:critical"` 與 exporter 讀不了的值（即使兩邊寫的是同一串）不算；巢狀的葉節點仍分型別。⚠️ 開著 `--warn-as-error` 的 CI 可能因此多出 warning 而轉紅，刪掉被點名的覆寫即可：刪掉後送出的閾值、severity 與 series 不變（寫的是舊拼法時，`da_config_deprecated_keys` 會跟著少一筆）。
