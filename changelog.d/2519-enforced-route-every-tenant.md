---
section: Changed
topic: alertmanager-routing
issues: [2519]
created: 2026-10-03T16:02:46+00:00
---
- **行為變更：`_routing_enforced` 的 receiver 含 `{{tenant}}` 時，`platform-enforced-<租戶>` 改為對每個租戶產生（`generate-routes`、validate-config、explain-route、da-guard；[#2519](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2519)）**：先前只有具備路由設定的租戶拿得到，只寫閾值、只設 `_silent_mode` 或 `_routing: disable` 的租戶被漏掉。現在範圍與 severity dedup inhibit rule 相同，租戶寫 `_routing: disable` 或被拒收的 `_routing` 都退不出這條通道；id 不合法的租戶照舊整棵樹拒收。NOC 等 enforced 通道會開始收到原本不在範圍內的租戶的告警，route 與 receiver 數隨租戶數線性增加。Silent mode 照常生效；單一平台 route（不含 `{{tenant}}`）不變。
