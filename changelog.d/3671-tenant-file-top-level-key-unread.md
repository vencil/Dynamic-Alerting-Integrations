---
section: Changed
topic: exporter
issues: [3671]
created: 2026-11-07T10:35:29+00:00
---
- **租戶檔頂層 `tenants` 以外的 key 改由共用判決報 unread（da-guard、validate-config、tenant-api；[#3671](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3671)）**：租戶檔（檔名不以 `_` 開頭）頂層的平鋪閾值、`defaults:`、`optional_overrides:` 等，exporter 一律不讀，以前只有 CI 的 tenant-config schema 閘門擋，直接改 git 的樹沒有東西判。現在 `da-guard key-verdict` 對每個這種 key 報一筆 `unread`（error，原因 `tenant_file_top_level`），掛在該檔、不列租戶；⚠️ **breaking**：含這種 key 的樹會讓 validate-config、`generate-routes --validate` 轉紅，`da-guard write` 拒收新寫進去的（檔裡原有的不擋同檔的其他修改）。允許的頂層 key 只有一份（exporter 的 `config.TenantFileTopLevelKey`，對齊 schema），tenant-api 判 git 管理也讀它。遷移：把平鋪的鍵移到 `tenants:\n  <id>:` 底下。
