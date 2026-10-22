---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-22T23:00:00+00:00
---
- **⚠️ Breaking：conf.d 拒絕的連帶變更（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：`deprecate` 不再替有問題的樹修復——即使載體只因本輪要刪的 key（例如 `defaults:` 的 `old_metric: disable`）才被 exporter 丟掉，也先拒絕、點名該檔，請先手動修好。`deprecate --plane subtree` 現在必須帶 `--root-dir <conf.d root>`：索引取自整棵樹，子樹外有 da-guard 驗證會點名的路徑時同樣拒絕（以前它把子樹當 root 判）。`backtest` 拒絕時照樣寫出 `-o`／`--markdown-output`，報告的 `reason` 為 `conf_d_not_servable`、`refused_paths` 列出路徑。`compile_custom_alerts` 不再編出「其餘的」pack。
