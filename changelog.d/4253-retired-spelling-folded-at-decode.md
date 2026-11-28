---
section: Changed
topic: exporter
issues: [4253]
created: 2026-11-28T08:00:00+00:00
---
- **⚠️ breaking：同一層同時寫了閾值的新舊兩種拼寫（#1231，如 `mysql_threads_running` 與 `mysql_cpu`）時，送哪一行改由同一條規則決定（exporter、da-guard、tenant-api；[#4253](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4253)）**：每一層解碼時就把舊拼寫收進新拼寫；這一層不採用的那行（被拒收的值、null、非有限數字）等於沒寫，兩行都採用時新拼寫勝出。先前根 `_defaults.yaml` 寫壞的新拼寫會不會蓋掉舊拼寫的值，取決於壞值的長相（`"off"`、`0x10` 會蓋掉，`abc`、`.nan` 不會）；租戶寫壞的新拼寫旁邊有舊拼寫時改送上層的值。現在兩者都送那一層自己的另一行。新拼寫那行過期時改送另一行。`/effective`、`da-guard` 的輸出與到期事件一律用新拼寫，寫舊拼寫的租戶 `merged_hash` 會變一次；改名提醒與 `da_config_deprecated_keys` 照舊。tenant-api 拒收「這次寫入」新造成的兩種拼寫並存，檔案原本就有的只提醒。
