---
section: Fixed
topic: tenant-api
issues: [3206]
created: 2026-10-21T08:32:22+00:00
---
- **tenant-api 拒收寫入時附的產生器 finding，依檔名判斷所屬租戶時改用共用的檔名規則（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：只指名檔案的 finding，過去只認小寫的 `.yaml`／`.yml`，所以 `Secret.YAML` 這類檔名不被當成租戶 `Secret` 的檔，看不到該租戶的呼叫者也會收到它的 finding；現在副檔名大小寫不拘，與 exporter、租戶列表的判法相同，這類 finding 對看不到該租戶的呼叫者隱藏。
