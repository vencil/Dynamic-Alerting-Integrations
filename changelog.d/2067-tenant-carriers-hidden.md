---
section: Fixed
topic: confd-family
issues: [2067]
created: 2026-09-28T12:48:24+00:00
---
- **`operator-generate` 與 `migrate-to-operator` 不再對 conf.d 的隱藏檔發出「無效租戶名稱」（ops；[#2067](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2067)）**：兩支共用的租戶掃描（`_lib_confd.tenant_carriers`）先前會把 `.ghost.yaml` 這類 `.` 開頭的檔名交給名稱檢查，於是 stderr 印出 `Skipping invalid tenant name '.ghost'`，`migrate-to-operator` 的 JSON `issues` 也多一筆，但 exporter 根本不讀這個檔；不做名稱檢查的呼叫端還會把 `.ghost` 當成租戶。現在與 exporter 一致，隱藏檔直接略過、不出任何訊息。產出的 CRD 不變。另新增 `tests/shared/test_confd_hidden_axis_across_tools.py`：每支會列舉 conf.d 的工具都讀同一棵含隱藏檔與隱藏目錄的樹，不能出現隱藏路徑裡的租戶，對照租戶則必須出現。
