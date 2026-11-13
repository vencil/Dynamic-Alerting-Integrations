---
section: Changed
topic: exporter
issues: [3457, 3827]
created: 2026-11-13T17:00:00+00:00
---
- **⚠️ Breaking：閾值的數字只剩一份文法，exporter 一起收窄（exporter／portal／schema／tools；[#3457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3457)、[#3827](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3827)）**：文法寫在 recipe contract 的 `patterns.thresholdNumber`——十進位，可帶正負號、小數點與指數，數字合計最多 20 位（前導零也算）——exporter、da-guard、tenant-api、portal RecipeBuilder、Python 工具與 schema 的 `thresholdScalar` 都讀這一份，再要求是有限數。⚠️ `1_000`、`0x1p4` 這類底線與十六進位寫法、超過 20 位的數字（含過去被靜默讀成另一個數的 800 位以上尾數），exporter 改為不送（base key 改送平台預設值），da-guard 報 `value_not_served`（結束碼 0 → 1），tenant-api 新寫入回 400／422；請改寫成十進位。RecipeBuilder 不再把 `0x1F`、`0o17`、`0b101`、`Infinity` 判成可送出。schema 的 pattern 改由產生器產生，會擋 `abc`、`1_000`。`threshold_recommend`、`config_diff` 不再把這類值當數字（改列人工檢視／`modified`）；`generate_tenant_fixture --extra-defaults`、`inject_default_key` 拒收（結束碼 2／1）。寫入端只寫不帶指數的十進位：`da_assembler`、`migrate_rule`、`--extra-defaults` 的數字寫出後超過 20 位（如 `1e20`）由照寫改為結束碼 2、不寫檔。
