---
section: Changed
topic: da-tools
issues: [3755, 3695]
created: 2026-11-12T01:00:00+00:00
---
- **⚠️ Breaking：其餘目錄輸入旗標全部改由宣告判定一次，拒收點名旗標；manifest 的 `dir` kind 刪除（da-tools；[#3755](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3755)，[#3695](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3695) 第 2 批）**：其餘 13 支工具的 conf.d 旗標與 lint、rule-pack-split、silencer-drift-check 的 rule-pack 目錄旗標改以 `_lib_input.add_input` 宣告：路徑、列不出的根目錄、空目錄在讀任何東西之前判定一次，拒收一律結束碼 2、一行點名旗標與路徑，`--json` 時 stdout 為空。空目錄是否接受沿用遷移前量到的行為：原本就以 2 結束的工具改點名旗標（先前轉印 da-guard 的 `no .yaml files found`），原本照常跑完的不變。validate-config：列不出的 `--config-dir` 根目錄由 1 改 2；空的 `--config-dir` 由「2 加一份部分報告」改為一行點名、不輸出報告；`--rule-packs` 不再自己判定，交給 lint，lint 的 2 成為 `custom_rules` 的 `caller_error` 列（整體 2、報告保留）——空或列不出的 `--rule-packs` 由 1 改 2。rule-pack-split 在拒收空的 `--rule-packs-dir` 之前不再先建出輸出目錄。deprecate 在 da-guard 沒回答時不再印「未發現任何引用」。gitops-check 的中文拒收訊息改為共用的 `ERROR:` 一行。
