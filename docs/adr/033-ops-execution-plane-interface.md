---
title: "ADR-033: 與運維執行平面的協同介面 — MariaDB 計畫性作業"
tags: [adr, alerting, integration, mariadb, maintenance]
audience: [platform-engineers, sre]
version: v2.9.0
lang: zh
id: ADR-033
tracking_kind: adr
status: accepted
domain: integration
created_at: 2026-08-22
updated_at: 2026-11-24
---

# ADR-033: 與運維執行平面的協同介面 — MariaDB 計畫性作業

## 狀態

✅ **Accepted**（2026-08-29，owner 核可）。

2026-11-02 修訂：租戶 metadata 不再 join 進告警，改由告警的 annotation 查詢 `tenant_metadata_info`（[#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)）。

2026-11-24 修訂：改寫為現行行為；告警表改照 `maintenance` 標籤。

## 決策

同一個 MariaDB 領域裡有一個協同產品的任務平面，負責**執行**運維動作：切換 primary、blue-green 換版、邏輯還原、刪 pod。本平台（DAI）負責告警。兩者今天互不認識。本 ADR 決定兩者之間要建什麼、不建什麼：

1. **不建「機器呼叫的即時靜音入口」。** 讓任務平面在作業前呼叫一個受限的 API 靜音告警，在現行架構下行不通，理由見「決定的細節」第 1 節。計畫性作業的靜音走既有的兩條路。
2. **身分對照只做成給人看的指引（advisory）。** DAI 的 tenant id 與任務平面的座標（`kubectl context` + namespace + 實例名）之間沒有橋；要補，只能是給人看的參考，不得餵給任何自動動作。
3. **`runbook_url` 指向唯讀的診斷任務。** 這是部署約定，不需要寫程式。

本 ADR 不推翻 [ADR-026](026-node-maintenance-liveness-suppression.md)（不建維護抑制子系統）與 [ADR-008](008-operator-native-integration-path.md)（不建 controller）。

### 範例：一次計畫性作業會打中哪些告警

下表取自 `rule-pack-mariadb.yaml` 與 `rule-pack-liveness.yaml`；「維護期間」是每條告警的 `maintenance` 標籤（`mute` 表示租戶開啟 `_state_maintenance` 時不觸發，`page` 表示照常叫人），全部告警的這一欄見[告警參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/ALERT-REFERENCE.md)。

| 告警 | 嚴重度 | `for` | 維護期間 | 會被哪類作業打中 |
|---|---|---|---|---|
| `MariaDBDown` | warning | 15s | mute | 任何讓 mysqld 停的動作 |
| `MariaDBRecentRestart` | info | 0s | mute | 重啟 |
| `MariaDBSemiSyncDegraded` | warning | 2m | mute | 動 replica |
| `MariaDBReplicationLag` | warning | 30s | mute | 還原、重新同步 |
| `MariaDBReplicationLagCritical` | critical | 30s | mute | 還原、重新同步 |
| `MariaDBClusterDown` | critical | 30s | page | 全部實例停機 |
| `MariaDBNoPrimary` | critical | 2m | page | 切換 primary |
| `MariaDBSemiSyncReplicasGone` | critical | 1m | page | 殺掉唯一的 semi-sync replica |
| `MariaDBReplicaIOThreadDown` | critical | 1m | page | `STOP SLAVE`、重新指向 |
| `MariaDBReplicaSQLThreadDown` | critical | 1m | page | 同上 |
| `TenantExporterAbsent` | critical | 3m | page | 租戶宣告的某一種 exporter 全部缺席超過 3 分鐘 |

所以「殺掉一個 replica pod 不會叫人」只在沒有啟用 semi-sync 時成立：單一 semi-sync replica 的拓撲下，殺掉它 1 分鐘後就是一條維護期間也照常叫人的 critical。replica 執行緒停掉的兩條 critical 不給維護期間靜音是刻意的：規則旁的註解寫明，停掉的複寫執行緒會侵蝕非同步的持久性，計畫性的 `STOP SLAVE` 要走 Alertmanager silence。

## 決定的細節

### 1. 不建機器呼叫的即時靜音入口

曾經提議的形狀：只准靜音白名單內的 alertname、`MariaDBClusterDown`／`MariaDBNoPrimary` 永不可靜音、強制最長時效與理由、回傳 silence id 供主動解除。以下四點各自就足以否決它：

- **網路不通。** `k8s/03-monitoring/network-policies.yaml` 的 `allow-alertmanager-ingress` 只放行 monitoring namespace 裡 `app: prometheus` 的 pod 連 9093；任務平面的 pod 不在其中。
- **核心保證做不到。** 「`MariaDBClusterDown`／`MariaDBNoPrimary` 永不可靜音」是這個提案唯一的安全論證。但 `configmap-alertmanager.yaml` 的 `TenantSilentCritical` inhibit 規則，target matcher 是 `severity="critical"`、`alert_source=""`、`tenant=~".+"`，完全不分 alertname：租戶只要進入 silent-critical，這兩條照樣被壓。新入口拒絕靜音它們，只是把呼叫端推向那條更粗的路。
- **更粗的等價機制早就存在。** `da-tools patch-config <tenant> _silent_mode critical` 以 `kubectl patch` 直接改 ConfigMap、不經 GitOps，hot-reload 後生效，就是一個即時、機器可呼叫、整個租戶的 critical 靜音。缺的不是工具，是「這把工具可以這樣用」的文件。
- **它依賴一張會漂移的身分對照，而漂移的後果是靜音錯的租戶。** 以租戶為單位的靜音要 DAI 的 tenant id；呼叫端結構上只知道 namespace 與 MariaDB CR 名，換算只能靠決定 2 那張宣告式對照表。表一漂移，就是把別人家的 critical 藏起來，而上述任何護欄都不檢查 tenant 對不對。

計畫性作業的靜音因此走既有的兩條路：宣告式用租戶設定的 `_state_maintenance`（可帶 `expires` 自動到期），只擋標 `mute` 的告警；即時用 `patch-config <tenant> _silent_mode critical`（粗，但存在）。

**重新評估的觸發條件**：前兩點任一被獨立的理由改掉時，例如 Alertmanager 前面多了一個帶認證的 API gateway，或 silent-mode 的 inhibit 改成分 alertname。即使如此，重開前也必須先解掉下一節的座標重用問題。

### 2. 身分對照只做成 advisory

DAI 的租戶是 conf.d 裡刻意不透明的 id（平台程式碼不寫死租戶 id）。任務平面的座標是 `kubectl context` + namespace + 實例名。兩邊沒有橋，任何「從告警走到可執行動作」的想法都卡在第一步。

- **放在 `_metadata` 不輸出成 label 的欄位類別**：`types.go` 記著 `environment`／`region`／`domain` 這類欄位不輸出成 Prometheus label（基數考量），只給 tenant-api 與 `generate_tenant_metadata.py` 讀。
- **不得加進 `tenant_metadata_info` 的 label。** 這個資訊指標每個租戶恰好一筆、每條 rule pack 告警的 metadata annotation 都查詢它；`collector.go` 寫明加寬它的 label 集是禁止的。要新增，比照 `tenant_expected_exporter` 開一個新 metric。
- **只能是 advisory。** 宣告式的表加上不建 controller，做不出「對不上就降級」的判定：最壞的情形是座標被重用（實例改名後舊的 namespace + 名稱仍存在、仍查得到、只是指向別的東西），任何「查得到就算對」的檢查都會回綠，要區分只能靠 DAI 沒有的身分憑證（例如 CR 的 uid）。所以這份對照是給人看的指引，不是給機器執行的座標：對照錯了的後果是「有人跑了一個唯讀診斷任務、發現對象不對」，而不是「靜音了錯的租戶」。

### 3. `runbook_url` 指向唯讀診斷任務

`runbook_url` 來自租戶的 `_metadata`，是自由填寫的 URL，告警的 annotation 經 `tenant_metadata_info` 帶出它。任務平面的任務目錄刻意把唯讀診斷（`status`、`sanity-check`、`connection-usage`、`pods/list`、`blue-green/validate`）與破壞性任務（`switch-primary`、`logical-restore`、`pods/delete`）分開，這正好是告警可以安全建議的界線：告警只指向唯讀那一半。

不需要改程式：欄位已在、值是自由字串。代價是零強制，沒有機制擋住有人指到破壞性任務；DAI 看不到對方的任務目錄，加 lint 也驗不了，所以維持約定。

## 後果與已知限制

- **兩個產品仍然互不認識**：這是刻意的，本 ADR 只建了一條約定（`runbook_url`）與一份可選的對照。
- **告警點名了 Silence API，卻沒給走法。** `MariaDBClusterDown` 與 `MongoDBClusterDown` 的 description 要計畫性停機的人走 Alertmanager silence，但沒說怎麼到。拿著 kubectl 的值班人其實到得了（`kubectl exec` 進 Alertmanager 用 `amtool`，或 `port-forward`），只是這些走法分散在 troubleshooting 與 CLI 參考裡，沒有一條從告警本身連得過去。這正是決定 3 的 `runbook_url` 的用途，目前沒有 rule pack 用它承載這件事。
- **「一種計畫性作業會打中哪些告警」沒有單一出處。** 哪些告警在維護期間可擋，已有[告警參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/ALERT-REFERENCE.md)的「維護期間」欄；但「切換 primary 會打中哪些」仍要自己對照，上面那張表就是這樣拼出來的。
- **沒有量到的事**：計畫性切換期間 `Slave_IO_Running=No` 會不會持續超過 `for: 1m`、切換瞬間 master server id 會不會歸零而讓規則自己的條件壓下告警，要真的 MariaDB 叢集才量得到；兩個產品是否部署在同一叢集或同一 service mesh 也未知。前者決定下方第一項延後工作值不值得做，後者若是不同叢集，還要多一層對外暴露與認證。

## 考慮過的替代方案

| 方案 | 為什麼不採用 |
|---|---|
| 機器呼叫的即時靜音入口 | 決定 1 |
| 讓任務平面經 `PUT /api/v1/tenants/{id}` 寫 conf.d | 技術上安全（有 `X-DA-Base-Hash` 樂觀併發控制，預設模式是直接寫入、沒有核可延遲，[ADR-009](009-tenant-manager-crud-api.md)），但把一次性的運維事件寫進設定的唯一來源，而且同樣受身分對照問題所限 |
| DAI 主動偵測運維作業（輪詢 K8s 或 watch CR） | 違反 [ADR-008](008-operator-native-integration-path.md)；偵測不到就靜默失效，是 fail-open |
| 把 namespace／實例名加進 `tenant_metadata_info` 的 label | `collector.go` 明文禁止加寬它的 label 集 |
| 重開維護抑制子系統（[#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)） | [ADR-026](026-node-maintenance-liveness-suppression.md) 的現場資料結論沒有被推翻 |

## 延後的工作

- **`MariaDBSemiSyncReplicasGone` 要不要在維護期間可擋**：它是唯一一條「單一 replica 級事件 → 維護期間照常叫人的 critical」。觸發條件：第一個啟用 semi-sync 的客戶回報維護窗被叫醒。
- **MongoDB 的對應面**：任務平面的 MongoDB 任務比 MariaDB 多得多。觸發條件：決定 2、3 在 MariaDB 上實際用起來之後。
- **反向流（DAI 告警觸發運維任務）**：刻意不碰。單向是宣告事實，反向是授權執行破壞性動作，信任模型完全不同，需要獨立的 ADR。

## 相關

- [ADR-003](003-sentinel-alert-pattern.md) —— sentinel 與 inhibit（決定 1 第二點的機制）
- [ADR-008](008-operator-native-integration-path.md) —— 不建 controller
- [ADR-009](009-tenant-manager-crud-api.md) —— tenant-api 的寫入模式
- [ADR-023](023-write-plane-single-writer-invariant.md) —— 寫入平面單一寫者
- [ADR-026](026-node-maintenance-liveness-suppression.md) —— 不建維護抑制子系統
