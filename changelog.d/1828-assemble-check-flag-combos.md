---
section: Changed
topic: confd-family
issues: [1828, 1829]
created: 2026-10-10T11:30:00+08:00
---
- **`assemble_config_dir.py` 拒絕它做不到的旗標組合，結束碼 2（[#1828](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1828)、[#1829](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1829)）**：行為變更。原本 `--check --validate` 印 ✅ 並回 0，但 `--validate` 一行都沒跑；`--check --manifest` 回 0 卻沒寫 manifest；只給 `--manifest` 會以空來源覆寫既有的 manifest。現在這三種組合都在讀寫任何東西之前被拒絕，回 2 並說明怎麼改；組裝卻沒給 `--output` 同樣在讀來源之前就回 2。`--json` 下 stdout 仍是一份 `{"status": "caller_error", "reason": …}`。`--manifest` 的說明不再寫 load：它只能存，不能讀回。要驗證就拿掉 `--check`、改給 `--output`。
