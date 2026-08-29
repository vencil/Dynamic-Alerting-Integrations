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
updated_at: 2026-08-29
---

# ADR-033: 與運維執行平面的協同介面 — MariaDB 計畫性作業

## 狀態

✅ **Accepted**（2026-08-22 起草，2026-08-29 由 owner 核可）。

> 依語言政策（自 ADR-019 起預設 ZH-only；ADR-024 / ADR-025 為保留 `.en.md` sibling 的例外），本 ADR 不另製 `.en.md`。

## TL;DR

- **背景**：同 MariaDB domain 有一個協同產品的任務平面，負責**執行**運維動作——切換 primary、blue-green 換版、邏輯還原、刪 pod。兩邊今天互相零認知（DAI 端 grep `db-runbooks|aqsh` 只命中本檔；對方 repo grep `Dynamic-Alerting|alertmanager` 零命中）。
- **本 ADR 最主要的產出是「不要建什麼」。** 草稿階段提過一個「機器面即時靜音入口」，外審把它打穿了：傳輸層不通、核心保證做不到、更粗暴的等價機制早就存在。**該提案改列為 reject。**
- **留下的三條**：
  1. ⛔ **不建**機器面靜音入口（見「被打穿的提案」整節）。
  2. **身分對照**是唯一真正缺、而且**擋住其他所有想法**的東西——但只能做成 **advisory**，不可餵給任何自動靜音。
  3. **`runbook_url` 指向唯讀診斷任務**——零程式碼，是部署約定。
- **順帶查出兩個 DAI 面的實際缺陷**（見最後一節）：rule pack 指定的補救途徑**機器面走不通、人面走得通，但告警本身沒給走法**（⚠️ 該條初版寫成「沒有人走得到」，2026-08-28 更正）；以及「計畫性作業會打中哪些告警」的資訊分散且不完整。
- **不動搖**：[ADR-026](026-node-maintenance-liveness-suppression.md)「不建維護抑制子系統」、[ADR-008](008-operator-native-integration-path.md)「不建 controller」。

## 先驗方向：三個假設，全部不成立

[ADR-026](026-node-maintenance-liveness-suppression.md) 留下的那一課是本節存在的理由：

> 多輪對抗式 review 把實作層磨得很細，卻沒先用真實現場數據驗證「這是不是客戶的痛」…… **先驗方向，再投入實作深度。**

### ❌ 假設 1：「計畫性作業會製造 DAI 擋不住的噪音」→ 部分成立，但不是原本想的那樣

[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)（completed）已把**單實例存活**降級。但把整個 MariaDB pack 加上 liveness pack 一起列出來之後，圖像不是「已經沒事了」：

| 告警 | severity | `for` | 吃 `_state_maintenance` opt-out | 會被哪類計畫性作業打中 |
|---|---|---|---|---|
| `MariaDBDown` | warning | 15s | ✅ | 任何讓 mysqld 停的動作 |
| `MariaDBRecentRestart` | info | 0s | ❌ | 重啟類（info 不 page） |
| `MariaDBSemiSyncDegraded` | warning | 2m | ✅ | 動 replica |
| `MariaDBReplicationLag` | warning | 30s | ✅ | 還原 / 重新同步 |
| `MariaDBReplicationLagCritical` | critical | 30s | ✅ | 還原 / 重新同步 |
| `MariaDBClusterDown` | critical | 30s | ❌ 刻意 | 全實例停機類 |
| `MariaDBNoPrimary` | critical | 2m | ❌ 刻意 | 切換 primary |
| **`MariaDBSemiSyncReplicasGone`** | **critical** | 1m | ❌ | **殺掉唯一的 semi-sync replica** |
| **`MariaDBReplicaIOThreadDown`** | **critical** | 1m | ❌ | **`STOP SLAVE` / 重新指向** |
| **`MariaDBReplicaSQLThreadDown`** | **critical** | 1m | ❌ | 同上 |
| `MariaDBExporterAbsent`（標 DEPRECATED 但仍在 pack 內） | critical | 30s | ❌ | pod 消失 |
| `TenantExporterAbsent`（`rule-pack-liveness.yaml`） | critical | 3m | ❌ | 該租戶**全部** exporter 缺席逾 3 分鐘 |

所以「殺掉一個 replica pod 不會 page」**只在沒有啟用 semi-sync 時成立**。`MariaDBSemiSyncReplicasGone` 的 expr 是「semi-sync master enabled 且 connected clients == 0」——單一 semi-sync replica 的拓撲下，殺掉它就是 1 分鐘後一個不可 opt-out 的 critical。

### ❌ 假設 2：「replica-thread critical 沒有 opt-out 是漏掉的」→ 撤回

該告警（`MariaDBReplicaIOThreadDown`，`rule-pack-mariadb.yaml:349`）上方有一整塊 **45 行**的 YAML 註解（`:304-348`）在交代設計取捨，其中直接處理 opt-out 的是 `:336-342` 這 7 行，明寫這是刻意的：

> `NO maintenance opt-out (matches ClusterDown/NoPrimary/ReplicasGone): a stopped replication thread = ... async durability erosion ... Planned STOP SLAVE → Alertmanager Silence API (same guidance as MariaDBClusterDown); a tenant's silent-critical sentinel still suppresses notification.`

⚠️ 注意最後那半句——它在草稿階段被引用了卻沒被讀懂，而它正是打穿下一個假設的關鍵。

⚠️ **2026-08-28 更正**：本句初版寫「12 行」。實測連續註解區塊為 `:304-348` 共 45 行（從告警行往上取連續 `#` 行；空行 `:303` 不計入）。更正方向對本節論證有利——註解比初版所稱的更長，「這是刻意的」這個結論只被加強。改成帶行號區間是為了讓後人能重新推導，而不是再信一個裸數字。

### ❌ 假設 3：「即時靜音這條路沒有 client，所以要補一個」→ **不成立**

草稿的整個提案建立在這句上。它錯在只查了一條路。

## 被打穿的提案：機器面即時靜音入口（reject）

草稿提議補一個受限的入口：只准靜音白名單內的 alertname、`MariaDBClusterDown` / `MariaDBNoPrimary` 永不可靜音、強制 max TTL、強制 reason、回傳 silence id 供主動解除。**四項獨立事實各自足以否決它。**

### R1 傳輸層本來就不通

`k8s/03-monitoring/network-policies.yaml` 的 `allow-alertmanager-ingress` 只放行 monitoring namespace 內兩種 pod 到 9093：`app: prometheus` 與 `component: maintenance-scheduler`。協同產品的任務 pod 兩個條件都不符。

⚠️ 而且這條 NetworkPolicy 上就掛著**同一種失敗曾經真的發生過**的紀錄（#1203：`maintenance-scheduler` 這一側原本沒開，「every silence call was dropped here … silent-and-self-concealing」）。同樣的形狀會再發生一次，而且一樣不會有人發現。

### R2 核心保證做不到

「`MariaDBClusterDown` / `MariaDBNoPrimary` 永不可靜音」是這個提案唯一的安全論證。但 `k8s/03-monitoring/configmap-alertmanager.yaml` 的 `TenantSilentCritical` inhibit rule 的 target matcher 是 `severity="critical"` + `alert_source=""` + `tenant=~".+"`，**完全不分 alertname**。租戶只要進 silent-critical，這兩條照樣被壓。新入口拒絕靜音它們，只是把呼叫端推向那條更粗的路而已。

### R3 更粗暴的等價機制早就存在，而且是機器面的

`da-tools patch-config <tenant> <key> <value>` 的 `metric_key` 是**位置參數、沒有任何 key 白名單**（`scripts/tools/ops/patch_config.py`：`tenant_config["tenants"][tenant][metric_key] = str(value)`），寫入方式是 `kubectl patch configmap threshold-config -n monitoring --type merge`，**不經 GitOps、不經 PR**，靠 hot-reload 生效。也就是說 `patch-config <tenant> _silent_mode critical` 今天就是一個即時、機器可呼叫、**整租戶**的 critical 靜音。

真正的狀況因此不是「沒有工具」，而是「**只有一把過粗的工具，而且沒有文件說它可以這樣用**」。

### R4 它對「決策 2」有未宣告的硬依賴，而那個依賴會反向放大傷害

Alertmanager silence 必須帶 `tenant` matcher（既有 builder 在 `scripts/tools/ops/maintenance_scheduler.py`）。呼叫端結構性知道的是 **namespace + MariaDB CR 名**，不是 DAI 的 tenant id；要換算只能靠決策 2 那張**會漂移**的宣告式對照表。

於是漂移的後果在這裡**不是**良性降級，而是**對錯的租戶開靜音**——把別人家的 critical 藏起來。草稿列的五條護欄沒有任何一條檢查 tenant 是否正確。

> **這是草稿漏掉的第三個「動工前必須先量」的問題，而且它比原本列的兩個更致命**：就算計畫性切換確實會 fire，只要呼叫端拿不到一個**可信且可被 DAI 端驗證**的 tenant id，這個入口就是淨負值。

### 順帶更正草稿的兩處錯誤

- 草稿寫「⛔ 對整個 tenant 開一張 silence ＝ 手刻 curl 的失敗模式本身」。但**產品自己出貨的 `maintenance_scheduler` 開的就是整租戶 silence**（matcher `{tenant, alert_source=""}`）。這條「護欄」與既有行為直接矛盾，草稿沒處理。
- 草稿在替代方案表寫「走 `PUT /api/v1/tenants/{id}` 要過 GitOps commit ＋ owner 核可」。**預設模式是 direct commit-on-write（[ADR-009](009-tenant-manager-crud-api.md)），沒有 PR、沒有核可**；要 PR 核可是 PR write-back 模式才有。這個 reject 理由本身是錯的。

## 決策

### 決策 1：⛔ 不建機器面靜音入口

理由見上節四條。**不是「以後再說」，是「以現行架構這條路不對」**——R1 與 R2 都不是實作難度問題，是既有設計的直接後果。

**Reopen trigger**：R1 與 R2 任一被獨立的理由改掉時（例如 Alertmanager 前面長出一個帶認證的 API gateway；或 silent-mode inhibit 改成分 alertname）。在那之前，計畫性作業的靜音走既有的兩條路：宣告式走 conf.d `_state_maintenance`（有 `expires` 自動到期），即時走 `patch-config _silent_mode`（粗，但存在且可用）。

### 決策 2：身分對照——唯一真正缺的東西，但只能做 advisory

DAI 的租戶是 conf.d 裡一個刻意不透明的 id（dev-rules #2 要求 tenant-agnostic）。運維執行平面的座標是 `kubectl context + namespace + 實例名`。**兩邊沒有橋，所以任何「從告警走到可執行動作」的想法都卡在第一步**——包含被 reject 的決策 1。

放在 `TenantMetadata` 既有的**非 label 欄位**類別（`types.go` 記著 v2.5.0 的決定：`environment` / `region` / `db_type` 等「NOT emitted as Prometheus labels (cardinality concern) — consumed by tenant-api and generate_tenant_metadata.py only」）。

⛔ **不得加進 `tenant_metadata_info` 的 label 集**。這不是我的推論，是 `collector.go` 自己寫的：`tenant_metadata_info` 是「load-bearing 1-series-per-tenant info metric consumed by ~15 group_left joins, so widening its label set is forbidden」。同一段也記了唯一一次 scoped reversal 是**開一個新 metric**（`tenant_expected_exporter{tenant, db_type}`），不是加寬既有的——要走那條路就得比照辦理。

**這份對照必須是 advisory-only。** 外審在這點上是對的而草稿是錯的：草稿說「對不上時應該降級成找不到可執行動作」，但**沒指定誰、用什麼、在哪一側做這個判定**，而在宣告式表 + 不建 controller 的前提下，這個判定做不出來——最惡劣的情形是座標被**重用**（實例改名後舊的 `(ns, name)` 仍然存在、仍然解析得到、只是指向別的東西），任何「查得到就算對」的檢查都會回綠，要區分只能靠 DAI 沒有的身分憑證（如 CR uid）。

**所以定義域限定為：給人看的指引，不是給機器執行的座標。** 對照錯了的後果是「有人照著跑了一個唯讀診斷任務、發現對象不對」，而不是「靜音了錯的租戶」。決策 1 被 reject 之後，這條線是自洽的；決策 1 若哪天 reopen，**必須先解掉座標重用問題**，不能沿用這張表。

### 決策 3：`runbook_url` 指向唯讀診斷任務（零程式碼，是約定）

`runbook_url` 來自 conf.d `_metadata`，是客戶自填的任意 URL，已 join 進每一條告警。協同產品的任務目錄有一個現成而且刻意維護的性質：**唯讀診斷任務與破壞性任務是分開的**（`status` / `sanity-check` / `connection-usage` / `pods/list` / `blue-green/validate` 對比 `switch-primary` / `logical-restore` / `pods/delete`）。這正好是一條告警可以安全建議的界線——**告警只該指向唯讀那一半**。

不需要 DAI 改任何程式碼：欄位已在、值是自由字串。**Trade-off**：零成本也零強制，沒有機制擋住有人指到破壞性任務。加 lint 驗一個自由字串欄位的語意是過度設計（DAI 也看不到對方的任務目錄，驗不了）。維持約定。

## 順帶查出的兩個 DAI 面缺陷

這兩個與協同無關，是查證過程的副產品，各自值得獨立處理：

1. **rule pack 指定的補救途徑：機器面走不通、人面走得通，而指引沒給走法。**

   ⚠️ **本條於 2026-08-28 更正。** 初版寫成「指引指向一條**沒有人**走得到的路」——那是錯的，理由見下方 ⛔ 段。初版同時把載體與告警對錯了，一併更正。

   **實際載體與告警**（`grep -n "Alertmanager Silence API" rule-packs/` 的全部命中）：

   | 告警 | 承載這段指引的欄位 | 值班人看得到 |
   |---|---|:---:|
   | `MariaDBClusterDown`（`rule-pack-mariadb.yaml:197`） | `description` / `description_zh`（`:207-208`） | ✅ |
   | `MongoDBClusterDown`（`rule-pack-mongodb.yaml:136`） | `description` / `description_zh`（`:146-147`） | ✅ |
   | `MariaDBReplicaIOThreadDown`（`:349`） | 其上方 YAML 註解（`:340`，塊範圍 `304-348`） | ❌ |
   | `MariaDBReplicaSQLThreadDown`（`:367`） | 兩種載體皆無此文字（其 description 指向 `Last_SQL_Error`） | — |

   ⇒ 初版把三者一律稱為「註解」。實際上走註解的只有 `MariaDBReplicaIOThreadDown`；兩個 `*ClusterDown` 走的是 **`description`**——值班人**會**看到的那一種。兩者不可互換。`SQLThreadDown` 沒有這段文字，`MongoDBClusterDown` 則是初版盤點漏掉的。

   **機器面確實走不通**：(a) `allow-alertmanager-ingress` 只放行 `app: prometheus` 與 `component: maintenance-scheduler` 兩種 pod 到 9093；(b) `maintenance_scheduler.py` 的 CLI 只有 `--config-dir` / `--alertmanager` / `--pushgateway` / `--dry-run` / `--json-output`，`extend_silence()` 是內部函式、唯一呼叫點在同檔排程迴圈，**沒有 ad-hoc 入口**；(c) 要建那個入口的 [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870) §4 已 closed as `not_planned`。**⛔ 但這三點就是 R1 的內容**，不是一個獨立於 R1 的缺陷。

   ⛔ **人面走得通，初版把 (a)(b)(c) 誤讀成「沒有人走得到」。** (a) 約束的是 **pod 網路 ingress**；(b)(c) 談的是**機器呼叫端**。拿著 kubectl 的值班人用本 repo 已記載的三條路都到得了，其中第一條完全不受 (a) 約束——pod 內 loopback 不經過 pod 網路：

   - `docs/troubleshooting.md:112-113` — `kubectl exec -n monitoring deploy/alertmanager -- amtool ... --alertmanager.url=http://localhost:9093`
   - `docs/cli-reference.md:1338` — `amtool silence query --alertmanager.url=http://<am>:9093`
   - `CLAUDE.md:122` — `port-forward` + `localhost:9093`

   **更正後仍然成立的缺陷（窄，但是真的）**：`description` 點名了一個 API，卻沒有給到達它的路；那三條路分散在三份不同文件，**沒有一條從告警本身連得過去**。⇒ 這正是決策 3 那個欄位（`runbook_url`）的用途，而目前沒有任何 rule pack 用它承載這件事。
2. **「計畫性作業會打中哪些告警、哪些可 opt-out」沒有單一出處。** 上面那張表是逐檔 grep 兩個 rule pack 拼出來的。維運者要自己拼，而拼漏一條（例如 `MariaDBSemiSyncReplicasGone`）的後果就是維護窗開了照樣被 page。

## 考慮過但 reject 的替代方案

| 替代方案 | 為什麼 reject |
|---|---|
| **機器面即時靜音入口** | 本 ADR 決策 1，四條理由見上 |
| **讓任務平面直接寫 conf.d（走 `PUT /api/v1/tenants/{id}`）** | 技術上安全（已有 `X-DA-Base-Hash` 樂觀併發控制），預設模式也沒有核可延遲。但它把一次性運維事件寫進設定 SSOT，且同樣受 R4 的身分問題所限 |
| **DAI 主動偵測運維作業正在跑（polling K8s / watch CR）** | 違反 [ADR-008](008-operator-native-integration-path.md)。且偵測不到就靜默失效，是 fail-open |
| **把 namespace / 實例名加進 `tenant_metadata_info` label** | `collector.go` 明文「widening its label set is forbidden」 |
| **重開 #870 的維護抑制子系統** | ADR-026 的現場數據結論沒有被推翻 |

## Future work（各自帶 trigger）

- **`MariaDBSemiSyncReplicasGone` 的 opt-out 語意**：它是唯一一條「單一 replica 級事件 → 不可 opt-out 的 critical」。是否該比照 `MariaDBDown` 給 opt-out，需要 semi-sync 拓撲的現場數據。trigger = 第一個啟用 semi-sync 的客戶回報維護窗誤 page。
- **MongoDB 對應面**：協同產品的 MongoDB 任務面比 MariaDB 大得多。trigger = 決策 2 / 3 在 MariaDB 上實際被用起來之後。
- **反向流（DAI 告警觸發運維任務）**：⛔ 本 ADR **刻意不碰**——單向是宣告事實，反向是授權執行破壞性動作，信任模型完全不同。trigger = 無，需要獨立 ADR。

## 本 ADR 沒有量到的事

- **計畫性切換到底會不會 fire。** `switch-primary` 期間 `Slave_IO_Running=No` 是否持續 ≥ `for: 1m`；切換瞬間 `mysql_slave_status_master_server_id` 會不會歸零而讓 expr 自己的 gate 把告警壓下去。需要真的 MariaDB 叢集，本文件**量不到**。這兩題不影響決策 1（已 reject）與決策 3，但決定「Future work 第一項」值不值得做。
- **兩個產品是否部署在同一叢集 / 同一 service mesh。** 若不同叢集，R1 之外還要多一層對外暴露與認證的問題。

## 關聯

- [ADR-003](003-sentinel-alert-pattern.md) — sentinel + inhibit；R2 的機制出處
- [ADR-008](008-operator-native-integration-path.md) — 不建 controller / operator
- [ADR-009](009-tenant-manager-crud-api.md) — direct commit-on-write 預設模式
- [ADR-023](023-write-plane-single-writer-invariant.md) — 寫入平面單一寫者
- [ADR-026](026-node-maintenance-liveness-suppression.md) — 不建維護抑制子系統；本 ADR 不推翻它
- [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870) — 維護抑制層（closed / not_planned）
- [#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875) — HA-aware 語意分級（completed）
