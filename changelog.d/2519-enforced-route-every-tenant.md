---
section: Changed
topic: alertmanager-routing
issues: [2519]
created: 2026-10-03T16:02:46+00:00
---
- **行為變更：`_routing_enforced` 的 receiver 含 `{{tenant}}` 時，`platform-enforced-<租戶>` 改為對每個租戶產生（`generate-routes`、validate-config、explain-route、da-guard；[#2519](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2519)）**：先前只有具備路由設定的租戶拿得到。現在範圍與 severity dedup inhibit rule 相同，只寫閾值、只設 `_silent_mode`、`_routing: disable` 或 `_routing` 被拒收的租戶都有，退不出這條通道；id 不合法照舊整棵樹拒收。這類沒有 tenant route 的租戶另補一條尾端 route 指向 root receiver，root 照收（`_routing` 主 receiver 被略過——type 不支援、網域政策擋下——的租戶也是，root 恢復收到），NOC 等 enforced 通道則多收到它們的告警；手動合併 fragment 時其 `route.routes` 須放在 root 子 route 最後；route 數隨租戶數線性增加。Silent mode 由 base config 的 TenantSilent inhibit 壓制（內建 base 不含，需以 `--base-config` 帶入）。單一平台 route（不含 `{{tenant}}`）不變。
