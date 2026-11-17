---
section: Added
topic: exporter
issues: [3831, 3832]
created: 2026-11-17T16:30:03+00:00
---
- **內建預設值表的執行期訊號（[#3831](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3831)）**：exporter 送 `da_config_builtin_defaults_info{digest}`（編譯進去的內建表摘要）與每個預設啟用鍵的 `da_config_builtin_default_state{key,state}`（`builtin`／`overridden`／`opted_out`／`withheld`，不帶租戶標籤，每次 commit 重設）；tenant-api 送 `tenant_api_builtin_defaults_info{digest}`。平台規則包新增記錄規則 `rule_pack_builtin_defaults_info{rulepack_digest}`（生成它的登錄表摘要，由 `check_threshold_registry.py --regen` 寫入）與告警 `BuiltinDefaultsDigestMismatch`（warning）：被 scrape 的 exporter 或 tenant-api 摘要與規則包不同、或沒送摘要（內建層之前的版本，`digest="none"`）持續 30 分鐘時觸發，label 寫出兩邊的摘要。⚠️ 這條告警只隨平台規則包出貨，threshold-exporter chart 不出貨規則包。
