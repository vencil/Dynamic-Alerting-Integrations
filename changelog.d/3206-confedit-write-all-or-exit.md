---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-24T02:25:32+00:00
---
- **寫 conf.d 的工具共用一個 `da-guard write` 出口（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：`generate_tenant_fixture.py`、`onboard_platform.py`、`migrate_rule.py` 一批寫入失敗時的處理收成 `_lib_confedit.write_all_or_exit`：被拒（逐檔點名原因）、da-guard 的呼叫錯誤或版本較舊、找不到可用的 da-guard、寫到一半停下（點名已寫入與可能已寫入、不會還原的檔）一律結束碼 2。⚠️ 可見變化：這些訊息每一行改以 `<工具名>: ERROR: ` 開頭（例如 `generate_tenant_fixture: ERROR: …`），`generate_tenant_fixture` 的訊息不再附「(the directory given to -o/--output)」。
