---
title: "ADR-026: Node/Cluster 維護告警抑制 — 不需要子系統"
tags: [adr, alerting, maintenance, k8s]
audience: [platform-engineers, sre]
version: v2.9.0
lang: zh
id: ADR-026
tracking_kind: adr
status: proposed
domain: k8s
created_at: 2026-06-18
updated_at: 2026-11-22
---

# ADR-026: Node/Cluster 維護告警抑制 — 不需要子系統

## 狀態

🟡 **Proposed**（2026-06-18）。owner 核可後昇格 Accepted。

2026-11-22 修訂：改寫為現行行為。維護期間擋不擋，改由每條告警的 `maintenance: mute|page` 標籤決定；週期維護窗由 threshold-exporter 自己判斷，不再建 Alertmanager silence；每個租戶各自的 exporter 缺席偵測已上線。決策不變。

## 決策

**不建「維護抑制子系統」。** Kubernetes node 維修、多叢集 rolling upgrade 時要靜音的告警，由既有機制處理：

1. **exporter 以 HA 部署為主。** 乾淨 drain 時唯一會響、又擋不掉的告警，是「單實例 exporter 所在的節點被 drain → 租戶宣告的這一種 exporter 全部沒有回應」。exporter 跑兩個以上副本並分散到不同節點（anti-affinity），這條告警就不會在 drain 時響。這是根因解：不替單點故障掩蓋告警。
2. **HA 叢集的 drain 噪音交給告警分級，不是維護抑制。** 單一 instance 掛掉是 warning、可在維護期間擋掉；叢集選不出 primary、租戶宣告的某一種 exporter 全部沒有回應這類「叢集存活」訊號是 critical，維護期間照樣叫。
3. **重用既有機制，不加常駐元件。** 每條租戶告警都標明維護期間擋不擋（`maintenance: mute` 或 `page`），租戶以 `_state_maintenance` 開啟維護；臨時靜音用 Alertmanager silence，sentinel／inhibit 照 [ADR-003](003-sentinel-alert-pattern.md)。不引入 controller／operator，延續 [ADR-008](008-operator-native-integration-path.md)。

### 範例：同一個維護旗標，兩條告警的結果不同

租戶設定開啟維護（也可寫成帶 `target`、`expires`、`reason` 的結構化格式）：

```yaml
tenants:
  db-a:
    _state_maintenance: "enable"
```

threshold-exporter 因此對這個租戶輸出 `user_state_filter{filter="maintenance"} 1`。標 `mute` 的告警，規則尾端帶著這個旗標的排除條件，維護期間不觸發，TSDB 也沒有紀錄：

```yaml {.quote src="rule-packs/rule-pack-kubernetes.yaml"}
      - alert: NodeNotReady
        expr: |
          rule_pack_kubernetes:node_not_ready:core
          unless on(tenant)
          (user_state_filter{filter="maintenance"} == 1)
        for: 3m
        labels:
          severity: warning
          maintenance: mute
```

租戶宣告的某一種 exporter 全部沒有回應（`TenantExporterAbsent`）則標 `page`，規則不帶排除條件，維護期間照常觸發：

```yaml {.quote src="rule-packs/rule-pack-liveness.yaml"}
        for: 3m
        labels:
          severity: critical
          maintenance: page
```

這組標籤是「維護期間擋不擋」唯一寫下決定的地方；`mute` 告警的排除條件由產生器依標籤寫入，逐條清單見[告警參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/ALERT-REFERENCE.md)的「維護期間」欄。週期維護窗（租戶設定的 `recurring:`，`weekdays` 加 UTC 的 `window`）走同一條路：exporter 在窗內輸出同一個旗標。細節見[設定驅動設計](../design/config-driven.md)的 Maintenance Mode 段。

## 背景

起點是「想在 node 維修時做 node-level 靜音，又不波及其他叢集」。但靜音是手段，目的是：**讓預期內的擾動對人隱形，同時讓預期外的真實事故照樣可見。** 用這把尺量，「整個叢集一鍵靜音」就不成立：它蓋掉預期內擾動的同時，也蓋掉維護窗內的真實事故。

所以先問：乾淨的 `kubectl drain` 期間，到底有哪些告警會響、能不能用既有機制擋掉？

| 告警 | 乾淨 drain 會響？ | 維護期間 |
|---|---|---|
| `NodeNotReady`（`for: 3m`） | cordon 不等於 NotReady → 不會 | `mute` |
| `ContainerCrashLoop` | graceful evict 不是 crash → 不會 | `mute` |
| 閾值類（例如連線數過高） | 看負載 | 依各條標籤 |
| 租戶自訂告警 | 看宣告 | `mute`（編譯器一律寫入排除條件） |
| `TenantExporterAbsent`（critical） | 單實例 exporter 的節點被 drain → **會** | `page` |

只剩一類：`TenantExporterAbsent` × 單實例 exporter。這條告警對租戶在 `_expected_exporters` 宣告的每一種 exporter 各判一次（宣告寫成 `種類@叢集` 時逐叢集判），某一種全部沒有回應就響。它刻意標成 `page`，因為「這一種 exporter 一台都沒在回應」在維護窗內也可能是真的停機。HA exporter 下，drain 一個節點還有別的副本在回應，這條告警不會響。

另有一類平台自我監控告警會在升級時響，受眾是平台 SRE，多半是「升級期間本來就該看到」的。處置是寫進 runbook，不是抑制。

客戶現場的資料支持這個方向：客戶的 mariadb／mongodb 都是 HA 叢集，上表最後一列在他們的拓撲上不會響。他們 drain 時真正的噪音，是「單一 instance 的 `<db>_up == 0` 被定成 critical」，正常 failover 切一台 replica 就誤叫醒人。這是告警分級錯置，已改為 instance 級 warning、叢集存活 critical（[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)）。

## 後果與已知限制

- **單實例 exporter 的租戶，drain 時會被叫醒。** 這是刻意的：平台分不出「維護中缺席」與「真的停機」。平台可以提供預設 HA 的 exporter chart 來降低門檻，但目前沒有，要由部署方自己把 exporter 跑成多副本。
- **exporter 缺席偵測是 opt-in。** 只有在 `_expected_exporters` 宣告了種類的租戶，才會被 `TenantExporterAbsent` 監看；沒宣告的租戶，exporter 消失不會有這條告警。
- **維護以租戶為單位，不分叢集。** 排除條件以 `on(tenant)` 配對，一個租戶跑在好幾個叢集時（[ADR-006](006-tenant-mapping-topologies.md)），開啟維護會擋掉它在所有叢集的 `mute` 告警。
- **擋掉的告警沒有紀錄。** `mute` 告警在維護期間不觸發，TSDB 沒有 `ALERTS` 紀錄；要事後看「維護期間發生過什麼」，得看原始指標。
- **方向要先用現場資料驗。** 這個決策是在實作前拿到客戶拓撲之後才定下的；新的抑制需求，也先確認「這是不是客戶真的會遇到的噪音」，再投入設計。

## 暫緩的設計：讓 exporter 缺席告警在維護期間可擋

**觸發條件：第一個 exporter 只能跑單實例的客戶。** 在那之前不做，追蹤在 [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)（以 not planned 關閉，觸發時重開）。

要做的是把 `TenantExporterAbsent` 改成維護期間可擋，要點：

- **兩條路開啟維護。** 計畫性維護走設定（`_state_maintenance`，經 GitOps 審核，本來就提前規劃）；緊急延長走 Alertmanager silence API（秒級，不依賴 exporter 還活著）。
- **緊急延長要有 CLI。** 目前沒有這個入口，需要新做。
- **盲區護欄。** 擋掉存活告警等於維護窗內對真實停機失明，所以維護旗標要有強制的到期上限，事後對帳。

暫緩的理由不是場景罕見（維護窗靜音是標準需求），而是三點不對稱：

1. **目前沒有需求。** 現有客戶都是 HA，這條告警在 drain 時不會響。
2. **出錯的代價高。** 這個功能本質是「維護期間靜音一條 critical 存活告警」，設定一錯就能藏住真實停機。
3. **之後補很便宜。** 標籤從 `page` 改成 `mute`，產生器就會寫入排除條件；要另外做的只有上面的 CLI 與護欄。

## 考慮過的替代方案

- **整個叢集一鍵靜音**：會蓋掉維護窗內的預期外事故。
- **跟著 cordon 自動抑制的資料面子系統**：要抑制的只剩一類告警，不值得一套常駐元件，也違反 [ADR-008](008-operator-native-integration-path.md)「不建 controller」。
- **動態產生每個租戶的規則、自建 operator**：[ADR-008](008-operator-native-integration-path.md) 已取消。
- **為單實例建正式的服務等級（Service Tier）系統**：過度；單實例能期待什麼，一行說明即可。

## 還沒做、各自的觸發條件

- **讓 exporter 缺席告警在維護期間可擋**：見上一段。
- **只靜音某一個叢集**（A 叢集維護、B／C／D 照常）：觸發條件是有租戶跨叢集部署、且需要逐叢集維護。
- **跟著 taint／cordon 自動宣告維護**：手動開旗標在大規模下會累人，但要先有「節點 ↔ 租戶」的對應。觸發條件是手動旗標真的成為負擔。
- **全 CRD 原生介面／operator**：維持 [ADR-008](008-operator-native-integration-path.md) 的 deferred。觸發條件是客戶 RFP 要 kubectl 原生介面。

## 相關

- [ADR-003](003-sentinel-alert-pattern.md) — sentinel 與 inhibit
- [ADR-006](006-tenant-mapping-topologies.md) — 一個租戶跑在多個叢集
- [ADR-008](008-operator-native-integration-path.md) — 取消 operator；本 ADR 延續「不建 controller」
- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) — 自訂告警編譯器
- [ADR-025](025-alerting-plane-self-liveness.md) — 告警平面自我存活
- [生命週期治理矩陣](../internal/monitoring-lifecycle-governance-matrix.md)
- [#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875) — HA 叢集的告警分級
- [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870) — 暫緩的維護抑制設計
