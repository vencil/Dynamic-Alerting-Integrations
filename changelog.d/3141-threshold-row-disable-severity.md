---
section: Changed
topic: exporter
issues: [3141]
created: 2026-10-20T16:58:30+00:00
---
- **閾值列的 `disable:<severity>` 不是停用（exporter、da-guard、Python 工具；[#3141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3141)）**：⚠️ 行為變更（只在訊息與 Python 鏡像）。閾值列的停用判斷改成先以共用文法切開、值的部分是停用詞且沒有寫 severity；`disable:critical`、`off:warning` 照舊是讀不懂的值（base key 送預設值，結束碼維持 1），exporter 的 WARN 與 da-guard 的 `value_not_served` 訊息指名兩種正確寫法：整個 key 停用寫 `disable`，只停 critical 寫 `<key>_critical: disable`。custom alert 的 `disable:<severity>` 仍是停用（[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）。Python 的 `is_disabled` 改用 Go 的空白字元集合（`disable\x1c` 不再算停用）並與 `custom_alert_disabled` 共用切法。
