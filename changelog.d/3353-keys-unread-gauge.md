---
section: Added
topic: exporter
issues: [3353]
created: 2026-11-12T15:57:48+00:00
---
- **exporter 讀不到的租戶 key 改為可告警：新 gauge `da_config_keys_unread{reason}` 與出貨告警 `ConfigKeysUnread`（threshold-exporter / rule-pack-platform；[#3353](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3353)）**：每次載入與 reload 設定，以（租戶, key）計 `unconsumed`、`profile_not_filled`、`unregistered`、`disabled_unread` 四種原因；根 `_defaults.yaml` 的 Policy-as-Code 規則點名的閾值 key 不計（與共用判決同一個函式）。direct 模式在 tenant-api 之外改樹時沒有 CI，現在由告警補上閾值 key 這一半；保留鍵（例如指向已刪 filter 的 `_state_<filter>`）仍量不到，gauge 為 0 不代表判決乾淨。⚠️ 既有樹若已有這類 key，升級後 `ConfigKeysUnread`（warning，`disabled_unread` 只計數不告警）會在 15 分鐘後響；用 `da-tools explain-keys` 找出來源檔。
