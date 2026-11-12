---
section: Changed
topic: tools
issues: [3671]
created: 2026-11-07T13:11:15+00:00
---
- **⚠️ BREAKING：da-tools migrate 產出一棵 conf.d：`.conf.d/<tenant>.yaml` 與 `.conf.d/_defaults.yaml`，標準模式需要 `--tenant`（tools；[#3671](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3671)）**：先前 `tenant-config.yaml` 把閾值平鋪在租戶檔頂層，沒有讀取端讀，要使用者自己縮排貼進 `tenants:`；`defaults-snippet.yaml` 也不是 conf.d 認得的檔名。現在閾值寫在 `-o/.conf.d/<tenant>.yaml` 的 `tenants.<tenant>` 底下，defaults 寫成 `-o/.conf.d/_defaults.yaml`（與 scaffold 同一組檔名）；目錄以 `.` 開頭，`-o` 落在服務中的 conf.d 裡時，讀取端會略過它，草稿租戶不會被服務；這棵 conf.d 只有這兩個檔，寫入時就由 `da-guard write` 以共用判決判過，規則檔與報告留在 `-o`，不會與租戶檔同名。`.conf.d/` 已有其他檔（例如另一次 `--tenant` 留下的租戶檔）時拒寫。`--tenant` 給租戶 id（ADR-035），標準模式缺了或不合法就 rc 2、什麼都不寫；`--dry-run`、`--triage` 不需要。`.conf.d/_defaults.yaml` 的 `defaults:` 區塊仍要合併進 conf.d 根的 `_defaults.yaml`，不要整份覆蓋。
