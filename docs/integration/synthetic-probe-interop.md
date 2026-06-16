---
title: "合成探測對接 (Synthetic-Probe Interop) — 用你現有的探測器驗證端到端投遞"
tags: [integration, alerting, synthetic-probe, alertmanager]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---
# 合成探測對接 (Synthetic-Probe Interop)

> **Language / 語言：** **中文 (Current)** | [English](./synthetic-probe-interop.en.md)

> **受眾**：已有 blackbox_exporter / synthetic monitoring 的 Platform Engineers、SREs
> **相關**：[ADR-025 告警平面自我存活性](../adr/025-alerting-plane-self-liveness.md)

## 這是什麼

平台預留一條**合成探測 sinkhole route**:任何帶 `component="synthetic-probe"` 標籤的告警,Alertmanager **保證**路由到專屬的 `synthetic-receiver` 且 `continue:false`。

這讓你用**自己現有的探測器**(blackbox_exporter、自寫 synthetic monitoring、CI smoke…)送一個假告警「穿過」平台的 Alertmanager,**端到端**驗證投遞鏈活著——而且**零風險**:這條測試告警**永遠不會**落到任何人類頻道 / 吵醒 on-call。

> **設計邊界(誠實說明)**:平台**不主動**發合成探測——這是給**你的**探測器用的**對接介面 (surface)**,不是平台自帶的探針(後者仍 deferred,見 ADR-025)。`synthetic-receiver` 預設 name-only(no-op black hole);你可指向自己的「探測 ack」端點,也可留空——契約保證的是**隔離**(測試告警絕不 page),不是投遞。

## 契約(平台側已保證)

| 項目 | 值 |
|---|---|
| 觸發標籤 | `component="synthetic-probe"` |
| 路由位置 | route index 2(Watchdog→Custom→**synthetic-probe**,皆在 NOC match-all 之前) |
| receiver | `synthetic-receiver` |
| `continue` | `false`(攔在此、不外溢) |
| `group_by` | `[alertname]`(不繼承 root `[alertname, tenant]`) |

這條路由由 `generate_alertmanager_routes.py` 每次 regen 自動注入並 pin 在最前段(撐過 `--apply` 的 route-REPLACE),手寫基底 `configmap-alertmanager.yaml` 同序、由路由 orchestration 測試守護。

## 怎麼用(你的探測器送一筆測試告警)

直接打 Alertmanager v2 API(把 `<alertmanager>` 換成你的位址;在叢集內常是 `alertmanager.monitoring:9093`):

```bash
curl -sS -XPOST http://<alertmanager>:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{
        "labels": {
          "alertname": "SyntheticProbe",
          "component": "synthetic-probe",
          "severity": "none"
        },
        "annotations": {"summary": "synthetic probe — verifying end-to-end alert delivery"},
        "startsAt": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"
      }]'
```

或在 blackbox_exporter / 你的 alerting rule 裡,讓那條 synthetic 告警帶上 `component: synthetic-probe` 標籤即可——其餘平台處理。

## 驗證它安全落地(沒外溢)

```bash
# 應出現在 synthetic-receiver、不在任何人類頻道:
amtool alert query --alertmanager.url=http://<alertmanager>:9093 alertname=SyntheticProbe
# 或在 AM UI 看該告警的 receiver = synthetic-receiver
```

看到這筆告警**只**命中 `synthetic-receiver`(`continue:false` 攔住、never 落到 default / NOC),就證明了平台的**壞租戶隔離 / blast-radius containment**——這條測試訊號既驗了投遞鏈、又絕不誤觸真人。
