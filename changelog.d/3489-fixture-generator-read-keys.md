---
section: Changed
topic: dx
issues: [3489]
created: 2026-11-01T16:05:00+00:00
---
- **`generate_tenant_fixture` 產生的每個租戶鍵都有讀取端（dx；[#3489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3489)）**：`da-guard write` 現在拒寫沒人讀的租戶鍵，產生器因此一律寫出根 `_defaults.yaml`，在 `optional_overrides:` 宣告每個會產生的閾值鍵、在 `dimensions:` 宣告維度鍵用到的每個標籤（regex 用 `<label>_re`）——宣告不是預設值，不加 `--with-defaults` 也不會多出預設的 row。`--with-defaults` 仍只代表平台的預設值（根 `defaults:` 與各 domain 的 `_defaults.yaml`）；`--extra-defaults` 不論有沒有 `--with-defaults` 都寫進根 `defaults:`（以前沒有它時會被丟掉）。synthetic-v2 用來撐大檔案的 `_extra_threshold_NN` 改成有宣告的 `extra_threshold_NN`，exporter 會為它們送出 row，bench 的 series 數因此比之前多。
