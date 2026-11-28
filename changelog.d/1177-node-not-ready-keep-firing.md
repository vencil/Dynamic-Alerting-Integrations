---
section: Changed
topic: rule-packs
issues: [1177]
created: 2026-11-28T10:41:00+00:00
---
- **`NodeNotReady` 在節點恢復後多響 10 分鐘（`keep_firing_for: 10m`；[#1177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1177)）**：節點反覆 NotReady 超過 3 分鐘又恢復時（例如壞 4 分鐘、好 1 分鐘），原本每個週期解除再觸發一次，一小時 12 則通知；現在整段是一次告警、一則通知。首次觸發仍是 NotReady 滿 3 分鐘。**行為改變**：(1) 告警在節點恢復、或租戶的 Pod 離開該節點之後約 10 分鐘才解除，不再是下一輪評估就解除；(2) 租戶在告警已經響著時才開啟維護模式，這條告警要再過 10 分鐘才安靜（先開維護、節點才 NotReady 則完全不響）——平台對這個行為的統一規則追蹤在 [#4348](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4348)。以解除通知驅動自動化的接收端會晚 10 分鐘收到。
