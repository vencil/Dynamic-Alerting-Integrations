---
section: Removed
topic: confd-family
issues: [3206]
created: 2026-10-23T01:00:00+00:00
---
- **⚠️ Breaking：`assemble_config_dir.py --validate` 移除（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：它判為 ERROR 的檔（頂層不是 mapping、解析錯誤）現在都是 da-guard 驗證會點名的路徑，在組裝前就被共用的 conf.d 拒絕擋下（結束碼 2、逐條點名、不寫任何檔），有沒有帶旗標都一樣；它剩下的 WARN（空檔、沒有讀取端讀的拼法）一併拿掉。帶 `--validate` 現在是用法錯誤（結束碼 2）；`make sharded-assemble` 已不帶它，自己的 CI 呼叫請拿掉這個旗標。
