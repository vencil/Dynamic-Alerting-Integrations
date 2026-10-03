---
section: Deprecated
topic: confd-family
issues: [2388]
created: 2026-10-03T07:15:19+00:00
---
- **子目錄 `_defaults.yaml` 的 defaults 寫保留鍵改由 da-guard 報 `subtree_default_reserved_key` 警告，下一個 minor 版起 exporter 不再套用（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)）**：子目錄 defaults 不支援 `_state_*`、`_silent_mode`、`_severity_dedup`、`_metadata` 等保留鍵；目前 exporter 只套用 `disable` 或數值、其他值丟掉（`_state_*` 關得掉、開不了；`_silent_mode: warning` 無效），先前 da-guard 不報。現在 da-guard 對 `--scope` 內每個受影響的租戶與 key 各報一條 warn（不論值，null 視為沒寫不報；`--warn-as-error` 時結束碼 1），訊息列出寫了該 key 的子目錄檔。`_routing`／`_routing_*` 仍由 routing 檢查報；檔案頂層的 `_custom_alerts` 是合法寫法、不報。本版 exporter 行為不變。⚠️ **下一個 minor 版起 exporter 不再套用子目錄 defaults 中的這些鍵（目前生效的 `disable` 也會失效），該 finding 改為 error**：請依訊息修正：多數改寫在租戶自己的 `tenants:` 條目；`_state_<filter>` 也可改根目錄的 `state_filters.<filter>.default_state`（影響整棵樹）；根目錄未宣告的 filter 與非受承認的鍵（如 `_silent_x`）則宣告或刪除。
