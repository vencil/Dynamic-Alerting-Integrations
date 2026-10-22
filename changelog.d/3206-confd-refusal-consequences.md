---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-22T23:00:00+00:00
---
- **conf.d 拒絕的連帶變更（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：`deprecate` 不再替有問題的樹修復——即使載體只因本輪要刪的 key（例如 `defaults:` 的 `old_metric: disable`）才被 exporter 丟掉，也先拒絕、點名該檔，請先手動修好；`--plane subtree` 不走這個拒絕（已知缺口）。`backtest` 拒絕時 `--json` 的 `reason` 為 `conf_d_not_servable`，`-o`／`--markdown-output` 都不寫。`assemble_config_dir --validate` 以前判為 ERROR 的檔現在不論有沒有帶旗標都在組裝前被拒絕，旗標只剩 WARN。`compile_custom_alerts` 不再編出「其餘的」pack。
