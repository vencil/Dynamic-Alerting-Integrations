---
section: Changed
topic: exporter
issues: [3835, 3883]
created: 2026-11-14T14:13:09+00:00
---
- **共用鍵判決改成對每個租戶判全天，結論不再隨執行的時刻改變（da-guard key-verdict、explain-keys、validate-config；[#3835](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3835)、[#3883](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3883)）**：一個鍵在一天中任一時段有讀取端讀就算被讀，整天都沒人讀才是 `unread`；`--at` 只決定 `expires:` 是否已過。不再判閾值的值：`value_unparsed`／`value_non_finite` 的鍵算有讀，值交給主閘門的 `value_not_served`。`/metrics` 收集不了的時段以收集前的列判，`key-verdict` 不再因此結束碼 2，`da-guard write` 也照樣檢查新寫的未讀鍵。文件拿掉 `served`，升為 `da-guard.key-verdict/v3`、`da-tools.explain-keys/v2`；釘了 `served` 的 `--assert` 檔結束碼 2，請刪掉該欄位（兩者都尚未隨版本發布，只影響追 main 的使用者）。`no_alert` 列改讀整天送出的鍵與等級。帶時窗的租戶越多判決越慢，tenant-api 的判決逾時算「判決未完成」，以可重試的錯誤不收該筆寫入。
