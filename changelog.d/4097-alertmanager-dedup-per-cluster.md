---
section: Fixed
topic: alertmanager-routing
issues: [4097]
created: 2026-11-21T23:50:00+00:00
---
- **severity dedup 逐叢集配對：一個叢集的 critical 不再壓掉另一個叢集的 warning（alertmanager-routing；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)，ADR-006）**：同一個租戶跨多個叢集時，去重規則原本只比 `tenant` 與 `metric_group`，A 叢集的 critical 會讓 B 叢集的 warning 不通知，而那是 B 唯一的通知。`generate_alertmanager_routes.py` 現在產兩條：兩則告警都帶 `da_cluster` 時，叢集也要相同才壓；都不帶時（Prometheus 還沒設 `external_labels.da_cluster`）照舊只比租戶與 `metric_group`；一則帶、一則不帶不配對。`tenant!~` 排除清單兩條都有。Silent Mode 與 custom recipe 的靜音維持整個租戶，不分叢集。**升級動作**：重新產生 Alertmanager 設定；`--base-config` 或 `--apply` 的來源若留著先前那一條去重規則會被拒收，刪掉即可；來源若本身是先前產生的輸出（有 `tenant-*` receiver，或第一次 `--apply` 採用既有的已產生 `alertmanager.yml`），改給原始來源設定，不要從它裡面刪規則。try-local 的手寫規則已同步。**限制**：兩個叢集都沒設 `da_cluster` 時仍會互相壓制，請照 ADR-006 設上。**通知預設不按叢集拆**（`group_by` 沒改）：兩個叢集同時觸發是一則通知；要每個叢集各一則，把 `da_cluster` 加進 `group_by`（BYO Alertmanager 整合指南）。文件的通知模板範例改為逐條列出告警與它的叢集。
