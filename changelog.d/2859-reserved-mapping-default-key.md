---
section: Fixed
topic: threshold-exporter
issues: [2859]
created: 2026-10-11T03:10:00+00:00
---
- **含 `default` 鍵的 `_metadata`／`_routing` mapping 不再被吞掉其他欄位（threshold-exporter；[#2859](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2859)）**：排程值解碼看不到值掛在哪個鍵下，只要 mapping 裡有 `default` 就當成 `{default, overrides, expires, reason}` 解，其餘鍵整份丟掉；例如 `_metadata: {default: x, environment: production, owner: team-a}` 讀不出 environment 與 owner，同一份 mapping 拿掉 `default` 卻讀得到。現在 schema 規定只能是 mapping 的保留鍵（`_metadata`、`_routing`、`_routing_enforced`、`_routing_defaults`）一律整份保留，與沒有 `default` 的 mapping 讀法相同。`_profile` 的 `{default: <名稱>}` 寫法照舊選取該 profile。
