---
section: Fixed
topic: confd-reader-consistency
issues: [1385]
created: 2026-09-28T09:20:09+08:00
---
- **`GET /api/v1/tenants/{id}` 與寫入驗證展開 `_profile`，與 `/metrics` 一致（tenant-api；[#1385](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1385)）**：過去 tenant-api 完全不讀 profile，選用 profile 的租戶 `resolved_thresholds` 與 `/metrics` 不同，寫入驗證還把有定義的 profile 誤報為 unknown profile 而擋下寫入。現照 exporter 的做法展開：profile 取自根目錄平台檔的 `profiles:`（`_profiles.yaml`、defaults carrier 等），多檔同名依檔名逐 key 後者優先；優先序為租戶檔 > 平台檔 `tenants:` 區塊 > profile（只補缺）> defaults。找不到定義的 `_profile` 仍擋寫入（與過去相同）；profile 補進來的 key 有問題只在 notices 以 `platform file <檔名>, profile "<名稱>"` 回報，不擋寫入。profile 讀自同一次有時限的根目錄讀取。
