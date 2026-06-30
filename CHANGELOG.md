---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.9.0
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [Unreleased]

<!-- 下一版 in-flight 工作暫存區。每筆 entry 目標 3-6 行使用者重點 + 一行指回內部 artifact；session 過程 / FUSE trap / 完整 commit list 不入此處。release 收尾時做最終 condensation 並切正式 `## [vX.Y.Z]` heading。 -->

### Added

- **租戶 log projection 防洩漏 gate 核心（#908 主線 config-from-SSOT 起手，承 [#609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609) ADR-021）**：`tenantProjections`（`helm/vector` 的 `{tenantId, accountId}` 手抄 list）是租戶日誌隔離的 trust root，但現有 render-time `{{fail}}`（只查唯一性）+ `values.schema.json`（只查 ≥1000 int）+ discard detector **全抓不到**一類 **unique-but-wrong** 抄錯（例 `{tenant-alpha: 1001}` 而 registry 配的是 1000）→ 該租戶日誌靜默寫進別人分區＝跨租戶洩漏；且 repo 無 committed 部署 pipeline（操作者手動 `helm upgrade --set`），手抄連 PR review 都不保證。本 PR 落 gate 的**安全核心**：純函數驗 `∀ p∈tenantProjections: registry.allocations[p.tenantId] == p.accountId`（[`verify_tenant_projections.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/helm/vector/projection-gate/verify_tenant_projections.py)）+ Vector **init-container image**（[Dockerfile](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/helm/vector/projection-gate/Dockerfile)），**fail-closed + fail-available**（mismatch／registry 讀不到 → degrade 回退平台-only `0:0`、Vector 不倒；`enforce` 模式硬 fail pod）。15 測試（含 unique-but-wrong headline、registry-unreadable、config-dir 片段 place/omit）。**chart 接線（init-container 進 DaemonSet + config-dir 重構 + ValidatingAdmissionPolicy anti-silent-disarm）為 follow-up PR**。設計決策記錄（safety/toil 拆解、enablement SSOT、業界對抗）見 [#908 留言](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/908)。
- **federation-audit.mtail 編譯 gate 進 CI（#908 次線，承 [#609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609) Phase 1 (b) 收尾）**：[`federation-audit.mtail`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/helm/federation-gateway/files/federation-audit.mtail)（從 Envoy audit access log 導出 `tenant_federation_requests_total` 與 #609 的 `tenant_log_query_requests_total{account_id,project_id,status}` ／ `tenant_log_query_duration_ms`）由 audit-sidecar 在 **pod 啟動時**才編譯——語法／型別退化過去只會以 sidecar CrashLoop 在**部署時**炸、**非自我浮現**，且 mtail 在 CI 零存在。新增 [`test_federation_audit_mtail.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/shared/test_federation_audit_mtail.py) 把 `mtail --compile_only` 釘成 gate（比照 `vector validate`／promtool 的 codify 模式），`ci.yml` `python-tests` job 裝 pin + SHA-256 校驗的 **mtail 3.0.8**（與 `audit-sidecar/Dockerfile` **同版同 digest**、對齊 runtime 編譯器；未裝即靜默 skip，同 Vector／Helm install 哲學）。含**正控**：刻意壞掉的程式必須編譯失敗，證明 gate 非 no-op。
- **VM 後端 `for:`／range-fn cold-start 偏差治理 gate（VictoriaMetrics；補既有 parity smoke 明示 defer 的 `for:` 層）**：把「rule pack 在 vmalert 直接可跑」從**穩態**宣稱收成**全 fixture 機械驗證**。新增 [`test_vm_alert_parity.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/rulepacks/test_vm_alert_parity.py) 用 `vmalert-tool unittest`（真 MetricsQL 引擎）跑**全部** rule-pack 告警 fixture，比 promtool 多守 `for:`／range 函數層。**實測抓到一條真分歧**：MetricsQL 的 `rate`/`changes`/range 函數在 **series cold-start**（新 series／exporter·KSM 重啟／counter reset／大於窗長的 gap 後首窗）刻意不外推 → 與 Prom 分歧（雙向：`rate>閾值` 過度 fire、`changes==0` gate 晚 fire）；`TenantHAReplicasDegraded` 在新 series 後**約晚 10 分鐘**才發（穩態正常、full-down 仍由 #875 critical 蓋）。每個已接受偏差登記於 [`vm_deviation_catalog.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/rulepacks/vm_deviation_catalog.yaml)，**未登記的新偏差即 fail CI**（gate 起手 informational）。順**證偽**既有 `test_vm_backend_parity.py`「`for:` identical regardless of backend」scope 註解、並標記其 staleness defer-with-trigger（「首客戶自有後端」）**已觸發**（VM 遷移客戶）。見 [§3.1 MetricsQL 已知限制](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/docs/integration/victoriametrics-integration.md)。
- **orphan/dead-lint 偵測 `check_orphan_lint.py`（#717，#456 唯一乾淨殘餘）**：`generate_tool_map` 的 glob 保證新 `scripts/tools/lint/check_*.py` 被**記錄**進 tool-map（`tool-map-check` 硬閘），但不保證被**執行**——既沒接進 `.pre-commit-config.yaml` 的 `entry:`、也沒進 `validate_all.py` 的 `TOOLS` 的 lint 就是 **dead lint**（存在、被文件化、永不跑；#141 Track A 曾靠人眼抓到並 revive）。新增 pre-commit 級 meta-lint：對 lint 目錄建「被任一 runner 引用」的引用圖（pre-commit `entry:` ／ `validate_all` `TOOLS` ／ Makefile ／ CI workflow ／ `dx`·`ops` sibling script 呼叫；**刻意比票面兩個 runner 更廣**——否則 8 條經 Makefile/CI 合法執行的 check 會被誤報，逼出 fail-open 靜態 allowlist；引用圖則自我維護、日後從 CI 移除即重新浮現）。`scripts/tools/lint/` 本身**排除在 referencer 之外**（lint 檔常在 docstring 互相引述，那不是執行）。未被任一 runner 引用即報 orphan、`--ci` exit 1。**dogfood 當場抓到一條真 dead lint**：`check_lint_toolchain_fit.py`（#444 meta-lint，`lint-policy.md` 宣稱「自動強制」卻接在零 runner）→ 一併接進 pre-commit 補實其文件承諾。allowlist 機制保留給真·manual-only check（目前空集；helper 為 `_`-prefix 不入 `check_*.py` glob）。referencer 含 GitHub + GitLab CI（repo 有 GitLab lineage，防只接 GitLab 的 lint 被誤報）、且 tree-walk 排除 `.venv`/`venv`/`node_modules`/`.tox` 等本地依賴黑洞（防 corpus 爆炸 + 跨檔名碰撞假命中）。比對用**單詞邊界**而非裸子字串（`disable_check_foo.py`／`.pyc` 不再遮蔽 `check_foo.py`）。19 測試（orphan FAIL／wired PASS／allowlist／`lint/` 排除／GitLab-CI rescue／前綴遮蔽防護／venv 黑洞排除／scaffold dogfood／真樹 clean；dogfood 走 tmp scaffold 不污染共享 lint 目錄，避 `pytest -n auto` race）。
- **MariaDB semi-sync 寫入可用性告警 `MariaDBSemiSyncDegraded` + `MariaDBSemiSyncReplicasGone`（#892；#875 defer 再評估 + Gemini 外審）**：補上 `MariaDBNoPrimary`（查 `read_only==0`）結構性看不到的 semi-sync 盲區——客戶 primary-replica semi-sync 拓撲下，primary 仍可寫（`read_only=0`）但 (a) 因 replica timeout 靜默 fallback 到 async（`status=0`，失 semi-sync 持久性保證、資料遺失風險）或 (b) 連線的 semi-sync replica 歸零（`clients=0`，備援歸零，`wait_no_slave=ON` 時寫入 stall 到 timeout）→ NoPrimary 完全隱形（`read_only` 答「有沒有可寫 primary」、非「replication 健不健康」，兩軸正交）。新增 **warning `MariaDBSemiSyncDegraded`**（`status==0`，吃維護 opt-out、`for:2m`）+ **critical `MariaDBSemiSyncReplicasGone`**（`clients==0`，無 opt-out 比照 NoPrimary、`for:1m`）。**⛔ intent-gate `enabled==1` 為必須（防全域誤報 storm）**：MariaDB ≥10.3 semi-sync 是 **built-in**（非 plugin），非-semi-sync 實例**仍吐** `status=0/clients=0`（**對真 prom/mysqld-exporter v0.18.0 + mariadb:11.8 docker 實測證實**），裸 `status==0` 會對平台**全租戶狂噴**；`enabled==1`＝「此實例想要 semi-sync」（expected-vs-observed，同 #869 `tenant_expected_exporter` idiom）。per-instance gate（primary 才有 `master_enabled=1`）；兩條 `metric_group:liveness` 走 severity-dedup（NoPrimary/ReplicasGone critical 抑制 Degraded warning）。promtool 三情境（**storm-gate 非-semi-sync 靜默 mutation-proven**／兩規則觸發+健康靜默／維護 opt-out 非對稱／`for:` 去抖）。lab 單實例無法產生 event（需 2-node semi-sync）→ synthetic fixture + customer-env-validated-on-rollout（同 #888 in-cluster precondition 模式）。承 #875 六條 defer 再評估的**唯一條件塌掉者**（Gemini 抓 storm trap、我 docker 實測驗證 + reframe「clients=0 無期限阻擋」為「timeout-bounded→async」）。
- **would-fire 預覽面板解鎖 absence recipe（#657 P3 收尾，承 #891 後端）**：recipe-builder 的「會不會觸發」面板原本 Run 鈕卡在 `Number.isFinite(測試值)`——absence（無數據偵測，presence-based、本來就無測試值）按不下去。改為 recipe 是 absence 時：放寬 `canRun`（無需試算值）、**隱藏數字輸入改放「指標停止上報時觸發、無需另填試算值」說明**、送**空 scenario**（`{}`），並把 firing／inactive 限定詞與常駐 scope-note 由「此測試值下」改為「模擬指標停止上報／合成數據」（誠實度——對 absence 沒有「測試值」可言）。a11y：說明以 `aria-describedby` 連到 Run 鈕（AT 也聽得到「為何不需試算值」，比照 #881／#885 把理由給 AT 的承諾）；verdict 仍 glyph＋文字＋顏色三重編碼（ADR-012）。**3 獨立 subagent 審查**（皆 take/reframe）：a11y/UX + 結構化 WCAG 2.1 AA（confirm absence 路徑零 BLOCKER；pre-existing stroke-as-text／focus-outline a11y debt 另開 follow-up）+ UX design-critique（用詞對齊表單既有「無數據」、去 jargon「缺席」；「測試值」→「試算值」避免與必填「閾值」欄撞名）。Vitest +4 案、全 portal 274 測試綠，`make portal-build` 重建 dist（共用 chunk re-hash 連帶 tenant-manager dist，benign）。**註**：da-portal 為 pulled image，下次 portal release 才生效。承 [issue #657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) P3。
- **K8s 副本就緒度告警 `TenantHAReplicasDegraded`（#875 follow-up，Gemini 對抗審查）**：補上 value-based DB 存活規則（#875 `*Down`／`*ClusterDown`／`*NoPrimary`、#869 `TenantExporterAbsent`）結構性看不到的盲區——HA 叢集中**單一 pod「消失」**（series 缺席非 `==0`，存活副本把它遮住）導致 N→N-1 降級（仍服務、失去備援）卻**零告警**。**採 kube-prometheus 標準件 `KubeStatefulSetReplicasMismatch`／`KubeDeploymentReplicasMismatch` 的 core**（非自造）+ 平台租戶 wrapper：db-* HA 工作負載（desired ≥2）就緒副本不足 → **warning**；`tenant` 由 namespace `label_replace`、維護 opt-out + metadata LOJ（比照 node-health）、`for:15m`。採標準件補上自造版漏掉的兩個精修：**created-vs-ready**（SS 比 `ready < status_replicas` 已建立數、Deploy 比 `available < spec` surge-safe → 避免擴容誤報）與 **`changes(*_status_replicas_updated[10m])==0` rollout 排除**（避免滾動更新誤報；代價是真 stuck 降級延後 ~10m 偵測，warning 故可接受）。單實例（desired<2）不納（=total-down，#875 ClusterDown 已 page）。promtool 八情境（含 scale-up-no-FP、rollout-exclusion 兩條精修專測）；**對真 KSM v2.18.0 實測**（kind）：降級時 `status_replicas_ready` 確掉、stuck-vanish 時 Pending 替補仍計入 `status_replicas`（created-vs-ready 接得住）、rollout 時 `status_replicas_updated` 2→0（gate 確跳）。
- **MongoDB + MariaDB 存活告警改為 HA-aware 三層分級（#875 / [ADR-026](docs/adr/026-node-maintenance-liveness-suppression.md)）**：修正 HA 叢集正常 failover 切換一個節點就被當 **critical 誤 page** 的客戶痛點。兩引擎一致改為 instance／cluster／quorum 三層：`<DB>Down`（單節點 `up==0`）由 **critical 降為 warning** 並吃既有維護 opt-out（`user_state_filter{maintenance}`，計畫性 drain 靜音）；新增 **`<DB>ClusterDown`**（`max by(tenant)(up)==0` → 整體實例皆掛才 critical，拓撲無關、單實例與 HA 皆正確）與 **`<DB>NoPrimary`**（節點可連線但無 primary → critical，接住 `max(up)==0` 結構性漏掉的「失 primary／quorum 但仍有節點存活」寫入中斷）。MongoDB 用 replica-set `member_state`（無 PRIMARY）；MariaDB 為 **primary-replica semi-sync**，用 `mysql_global_variables_read_only==0`（無可寫 primary）——兩者的 cluster-state metric 都與該 pack 既有告警同源（mongo 的 replication-lag／maria 的 max_connections，同一 exporter collector，無需新開）。叢集存活類**不提供維護窗 opt-out**（計畫性單實例維護改用 Alertmanager Silence；租戶 silent-critical sentinel 仍會壓制）。降級副作用：`<DB>Down` 現為 warning，租戶 silent-warning sentinel 會一併靜音它（dedup-enabled 租戶整體中斷時看到單一 critical，ClusterDown 抑制 Down warning）。各引擎 promtool 七情境驗證（failover 不誤 page／維護靜音／跨租戶維護隔離／total-down／no-primary 觸發／for: 去抖）。**範圍**：PostgreSQL／Redis／Oracle／DB2／ClickHouse 同款分級**延後**——須先有具名 HA 租戶 + 該引擎 cluster-state metric（如 patroni／sentinel）落地（#875 ⛔ 驗證步驟），否則裸 `max(up)==0` 會把 primary/quorum-loss 誤降為 warning。
- **would-fire 預覽明示「答的範圍」（#657 收尾誠實度）**：面板新增常駐註記——預覽是「**這條 recipe 的閾值邏輯在你填的測試值會不會越線**」（合成數據），**不代表在你環境會發出通知**（未模擬真實走勢／`for:` 計時／Alertmanager 靜默路由）。避免「邏輯會觸發」被讀成「我的告警會響」。§7 誠實度延伸（型別支援之外、再補「verdict 語意範圍」這條軸）；設計 §5.1 + 元件 README 同步。
- **租戶 portal 表單當場試算「會不會觸發」（#657 收尾，would-fire 最後一哩）**：recipe-builder 表單新增 would-fire 面板——填一個測試值按「試算」當場回 firing／inactive，走平台**同一套** compiler + `promtool`（經同源 `/preview` 反代打 recipe-preview 服務，#657 PR 2）。verdict 一律由後端逐字顯示（dumb handoff，前端不自算 firing）；不支援的 recipe 型別誠實標示「即將推出」、不留白（設計 §7）；**手動**觸發、不自動試算（§6）。可及性比照 ADR-012／#655：firing／inactive／error 以**形狀符號＋文字＋顏色**三重區分（非僅靠顏色）。新增 da-portal nginx 同源 `/preview` 反代（`components/da-portal/nginx.conf` + helm configmap，CSP `connect-src 'self'` 不變）。6 個 Vitest 案。
- **recipe-preview would-fire 預覽服務（#657 PR 2，先上 try-local）**：新增獨立的小型 stdlib HTTP 服務 `components/recipe-preview/`——`POST /preview` 給一條 recipe + 情境值，當場回「會不會觸發」，走平台**同一套** compiler + `promtool`（`_recipe_preview`，不另寫 eval）。**PEP（policy enforcement point）**：服務不自判租戶授權，把呼叫者身分轉發去打 tenant-api 的 `GET /tenants/{id}/access`（#876）做決策，`403`／非 `200`／連不到一律 **fail-closed 拒絕**——RBAC 留在 tenant-api 單一權威。信任邊界比照 tenant-api（須擺認證代理後 + NetworkPolicy，**只轉發兩個身分標頭**防 confused-deputy）；try-local 用 dev-bypass。§6 護欄：並發上限、每租戶滑動視窗限流、`promtool` 3.12.0 SHA-pin（判定與版本綁）+ 啟動記錄版本。17 pytest（驗證／PEP fail-closed／身分標頭隔離／限流／契約透傳 + 經真實 core 的 e2e）。接線進 try-local docker-compose（`:8082`）；首版 `threshold` 型，時間相依型回 `supported:false`，正式環境部署延後（[設計 §9](docs/design/recipe-would-fire-preview.md)）。
- **tenant-api 授權探測端點 `GET /api/v1/tenants/{id}/access`（#657 預覽服務前置）**：新增輕量 RBAC 讀取探測——可讀該租戶回 `200 {allow,tenant,permission}`、否則 `403`、缺身分標頭回 `401`。專門打造給姊妹服務（recipe-preview 預覽服務 #657）**重用** tenant-api 既有的租戶隔離決策，而**不**在第二種語言重寫 `_rbac.yaml`/`HasPermission`（會有授權漂移＝跨租戶風險）、也**不**像探 `GET /{id}` 那樣過度取得整包租戶設定（最小權限）。實作零新授權邏輯：路由掛既有 `Middleware(PermRead, TenantIDFromPath)` 做完整決策（含開放模式正確、未來 RBAC 演進自動跟上）；端點只回布林值，空白／含路徑分隔字元的租戶 id 一律 `400`。承 [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) §4.1 租戶隔離（消費它的 recipe-preview 服務本身為後續 PR）。
- **合成探測 sinkhole route 對接介面（ADR-025 synthetic-probe interop）**：平台預留一條 Alertmanager sinkhole route——任何帶 `component="synthetic-probe"` 的告警**保證**路由到專屬 `synthetic-receiver` 且 `continue:false`，讓客戶用**自己現有的** blackbox / synthetic 探測器送測試告警「穿過」平台 Alertmanager、端到端驗證投遞鏈，且**零風險**（絕不外溢到人類頻道 / 吵醒 on-call）。route 經既有 `generate_alertmanager_routes.py` injector 注入並 pin 在 index 2（Watchdog→Custom→synthetic-probe，撐過 `--apply` route-REPLACE）、base `configmap-alertmanager.yaml` 同序、路由 orchestration + amtool 測試守護。設計邊界：平台**不自建**探針（這是 interop 對接面 only；自建探針仍 defer-with-trigger）。承 ADR-025；見 [synthetic-probe-interop.md](docs/integration/synthetic-probe-interop.md)。
- **後端相容性 — vmalert-tool == 真 vmsingle 等價 anchor（VictoriaMetrics；ADR-025「後端相容性」Part 1，[#947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/947) consolidation）**：把「平台 backend-agnostic」變成可驗證事實——對真實 VictoriaMetrics 跑代表性 rule-pack golden（`and on()` / `group_left` / `label_replace` / `max by(tenant)` idiom + 多層 recording 鏈），斷言與 Prometheus 評估**我們編譯出的 expr** 一致（label-set + fire/no-fire + 值 epsilon）。**per-PR 的 VM parity 改由 `test_vm_alert_parity.py`（gate A，全 fixture 過 `vmalert-tool unittest` = 生產 MetricsQL 引擎）單獨守**；本 real-vmsingle 檢查退役成 **on-demand 等價 anchor**、原每-PR docker-VM job 移除（其 3+1 案是 gate A 全集的子集，唯一真分歧軸 storage-staleness 兩者皆 defer，每 PR 重跑僅重複驗 gate A 已更廣涵蓋的 shared-math 層）。等價＝「pinned 引擎版本」的性質→只在版本 bump 重驗；新增 **pin-coupling guard**：gate A 與 anchor 共用單一版本 pin `tests/rulepacks/vm_engine_version`（`test_engine_version_matches_pin` 斷言 live vmsingle 版本 == 它）。`promql-compliance-tester` 仍不適用（DIY-exception 理由不變）。**staleness/時間語意仍 defer**（trigger「首客戶自有後端」**已觸發**，剩餘 slice = `vmalert -replay`，#947）。見 [backend-compat-baseline.md](docs/internal/backend-compat-baseline.md)。
- **租戶歸因的節點健康告警 `NodeNotReady`（#809）**：kubernetes rule pack 新增 node-NotReady 告警，把 node-scoped 的 `kube_node_status_condition`（無 `tenant` 標籤）經穩定的 **node→tenant 映射** recording rule（`tenant:node_owner:info`，由 `kube_pod_info` + Running-pod 過濾建出）歸因到**該節點上每個有運行中 Pod 的租戶**並 fan-out。預設全開、warning severity（migration-parity，客戶遷移前即有節點級保護），maintenance 模式可 opt-out。對抗式 review 護欄：映射僅取 Running pod（排除 Evicted/Failed 幽靈 Pod 誤報、且賦予 Pod 重調度後告警自動 resolve 語意）；fan-out 用 `and on(node)` 而非 `* group_left`（避免 NotReady 值為 0 導致 `>0` 永不觸發）；Alertmanager 既有 `group_by:[alertname,tenant]` 化解 AZ 災難告警風暴。promtool 多租戶 fan-out + 幽靈 Pod 排除 + 健康節點 + maintenance 四向回歸（#692 P0 第②片；topology-join 機制，pod→PVC 等其他映射為後續）。
- **Custom Alerts `==` 等值運算子（threshold recipe 限定，any-match 語意）**：recipe `op` 新增 `==`，精確比對「以指標**值**表達的狀態/錯誤代碼」（例：MariaDB semi-sync errno 1236）— 補上 #810 的表達力缺口。`==` 採 **any-match**：逐 replica 原始值先比對再聚合，故**任一**實例等於該碼即觸發——多副本持不同碼不會互相掩蓋（對抗式 review 揪出 max-then-== 會靜默漏報，#819）。護欄：`==` 僅 `threshold` recipe 可用，其餘 recipe（rate/ratio/p99/forecast/absence）兩側 validator + JSON-schema if/then 三層一致拒絕（fail-loud，避免前後端腦裂）；等值告警文案用「等於設定代碼」（`%.0f`）。Python compiler / Go preflight / JSON schema / portal enums / 治理契約五處 lockstep，golden vector + promtool（含多-pod 掩蓋回歸）雙向驗證（#692 P0 第①片）。
- **Custom Alerts value-form 狀態碼安全採用指引（cookbook + shape-explicit 範例，#832）**：補上 #810 的 last-mile——`==`（#819 已出貨）之外，新增「以指標**值**表達狀態/錯誤碼」撰寫實務（`custom-rule-governance.md` §8）：value→label 首選解法 + 決策樹 + 多碼用 label-form `selectors_re` regex + 重塑不了 exporter 時的 **SRE-mediated** relabel 退路。並把 **exporter 存活性**寫成通用主題（dead-exporter 靜默是所有 value-based 告警通病、非 `==` 獨有）：配 `absence` recipe 補盲，但**取決於 exporter shape**（連續型才配、稀疏型配了反誤報），且 `absence` 為 tenant 聚合（抓全副本缺席、非單副本死亡）、**高基數指標需 `selectors` 限縮掃描**（否則 `count_over_time` 掃全 series，VictoriaMetrics 等後端易記憶體／CPU 峰值；Day-2 外審補強）。example tree 增 `process_status_code` 的 Shape-X `==`+`absence` 安全配對範例（shapes 9→11）。schema 契約 `expect_continuous_series` defer-with-trigger（首個 live `==` 採用）。
- **ADR-025（提案）平台 alerting-plane 自我存活性**：記錄一個獨立平面的決策——告警系統（Prometheus / Alertmanager）自己死掉時要能被**外部**察覺。決策：Watchdog 心跳經獨立置頂路由送平台**外部**監測點（「沒收到才告警」的反向設計，外部 TTL 留緩衝避免抖動誤報）；斷網改用叢集**外部** pull-based 健康檢查（非 K8s 內建 probe）；HA 與大規模儲存維持 backend-agnostic 交由 operator（目標客戶自帶大規模時序後端）。承 #832 的 liveness 討論分出（監控平面，非租戶側 `absence`）。狀態 proposed、實作未啟動；canary 租戶 / 規則 linter / 合成探測列 defer-with-trigger。
- **ADR-025 MVP 實作：Watchdog 心跳 + 外部 dead-man's-switch（#838）**：補上「平台告警系統自己死掉沒人知道」的盲點。新增永遠 firing 的 `Watchdog`（`expr: vector(1)`、`severity:none`）+ 一條**置頂、零聚合、固定頻率**（`routes[0]`、`group_by:[alertname]`、`group_wait:0s`、`repeat_interval:3m`、`continue:false`）的 Alertmanager 路由，把心跳送到 operator 自備的**外部**監測點；URL 為機密，經 `webhook_configs[].url_file` 指向掛載的 Secret（禁明文，免踩 secret-scan）。路由由 `generate_alertmanager_routes.py` 注入 index 0、撐過 `--apply` 的 route-REPLACE（且 receiver 的 `url_file` 不被覆寫）。另加**內部互補告警** `AlertmanagerWebhookNotificationsFailing`（webhook 送不出時先給內部信號，區分「平台死」vs「心跳管路壞」）。抑制免疫機械化：任何 `inhibit_rules.target_matchers` 不得 match `alertname="Watchdog"`，於兩條 render path（assemble / `--apply`）對 base+generated 合併集 **fail-closed** 驗證（Silence 端無法機械強制 → 進 operator 指南）。狀態 proposed→in-progress；operator 合約見 [告警平面自我存活性 Operator 指南](docs/integration/alerting-plane-self-liveness.md)。canary 租戶 / 規則 linter / 合成探測仍 defer-with-trigger。
- **租戶磁碟 recipe inert 偵測 `CustomRecipeDiskInert`（#692 P0③ W2）**：kubernetes rule pack 新增 per-tenant sentinel，把自助磁碟告警（forecast/ratio over `kubelet_volume_stats_*`）的 silent-dud 變 **fail-loud**——CSI 未實作 NodeGetVolumeStats、或 kubelet volume-stats 未歸屬 `tenant` 時，recipe 編得過卻在真叢集**永不 fire**（#731-class 盲點）；此 sentinel 在租戶**宣告了磁碟 recipe 且有 Running pod、但歸屬後的 volume-stats 缺席**時觸發。護欄：per-tenant 隔離（健康租戶不掩蓋 inert 租戶——早期 global-count draft 的 masking bug 由 promtool 多租戶案 C 守護）、either-absent（available/capacity 任一缺即觸發，ratio 兩者皆需）、`max by(tenant)` 做 HA replica dedup（同 #731 contract 要求 user_threshold 用 exact `=` metric 比對）、`kube_pod_status_phase{phase="Running"}` 排除 ghost pod（對齊 #809）、maintenance opt-out；**dual-perspective annotation**（租戶白話 summary + SRE `platform_summary` 三層排查序，dev-rule #10）。promtool 7 案（含多租戶 masking 回歸）；rule-pack alert 數 120→121。
- **pint Prometheus 規則靜態檢查進 CI（ADR-025 deferred「規則 linter」項）**：採用 OSS [`pint`](https://cloudflare.github.io/pint/) linter（thin wrapper `check_pint.py` + repo-root `.pint.hcl`，對齊 hybrid lint policy「adopt OSS engine, don't DIY」）守 rule-pack source。**唯一 hard-gate 的高 ROI check 是 `alerts/template`**——機械化攔截「聚合砍掉 alert template 用到的 label → 告警永遠靜默不觸發」這個本 repo 燒過 5× 的類別（至今只靠手寫註解 + 1 個 regression test 守）。對抗式驗證：probe rule `sum(...)+template 用 tenant` 被抓、`sum without(instance)` 控制組放行、新增非豁免名規則照樣 exit 1。idiom-noisy checks（`absent()`-sentinel 的 always-firing / 防護性 `or vector(0)` / 跨群 recording-rule 依賴）於 `.pint.hcl` disabled；平台級 `*ExporterAbsent`/`Inert` sentinels（required-labels policy 強制帶 `tenant` 但 expr 刻意聚合掉）於 `.pint.hcl` 中央 registry 豁免（name-scoped，不掩蓋真 bug）。`--offline`（CI 無需 Prometheus），baseline 0 blocking、deterministic（非 git-diff 依賴）。見 [pint-lint-baseline.md](docs/internal/pint-lint-baseline.md)。
- **CI 工具二進位安裝加上 SHA-256 供應鏈把關**：`ci.yml` 對所有以 `curl` 下載的 release binary（promtool / hadolint / pint / kube-linter）在 extract/install **之前**先比對 pinned SHA-256，mismatch 即 fail（best-effort 步驟仍照舊落 docker fallback），避免被竄改 / 重推 / 損毀的下載物直接在 runner 上執行——回應 #843 CodeRabbit「pint install 無 checksum」並擴及同類所有安裝點。把關共用 `scripts/ops/_verify_download.sh`（codify 一次、四處呼叫；附 Linux 自測 `tests/ops/test_verify_download.py`）。pin 為 TOFU digest（kube-linter 該版只發 cosign `.sig`、無 sha256 檔，故 digest 直接 pin）。docker fallback image 的 digest-pin 見下一條。
- **CI lint 的 docker fallback image 改用 digest-pin（供應鏈 sweep Part 2，承上條）**：`check_pint.py` / `check_iac_helm.py` / `check_iac_vibe_rules.py` 的 docker fallback（pint / kube-linter / helm / hadolint；`check_k8s_manifests.py` 經 import 共用 kube-linter image）由 mutable tag 改為 `repo:tag@sha256:<digest>`（multi-arch index digest）——上游同 tag 被重推 / 竄改也無法替換鏡像。各 digest 經 registry manifest API 解析，並逐一 **by-digest 複驗**（HTTP 200 + Docker-Content-Digest 相符）；各 `*_VERSION` 常數保留（pint version-sync 測試照過、其餘無 image-string 解析）。binary 安裝仍為主路徑、docker 為 best-effort fallback。
- **第三方容器映像供應鏈三層硬化（#902，L1 偵測 + L2 digest pin + L3 Renovate 自動 bump）**：把 14 個 helm chart / k8s manifest **拉取**的上游第三方映像（envoy / oauth2-proxy / prom-label-proxy / vector / victoria-logs / python / mariadb / mysqld-exporter / grafana / prometheus / alertmanager / kube-state-metrics / configmap-reload / alpine-git）從「零自動 CVE 偵測、tag-mutable」收成完整供應鏈閉環。**L1 偵測**（#907）：`nightly-image-scan.yaml` 加 `scan-thirdparty` job（Trivy 直掃 14 ref、不 build）+ split alerts（self-built vs third-party 分流，避免 release-gated 自建回歸被埋）+ `check_image_refs_resolve.py` PR gate（skopeo 解析每個部署 ref 在 registry 確實存在，逮 #897 typo'd-tag 類）+ drift-guard（scan matrix == 部署 ref SSOT）。**L2 不可變**（#910 + #915）：14 個全部 `@sha256:` digest-pin（trust-boundary 優先：envoy→oauth2-proxy→prom-label-proxy→data-plane→其餘），split-knob chart 帶 `image.digest` knob + ⛔ tag-override 警告、single-string 直接 append digest。**L3 自動 bump**（self-hosted Renovate，本次）：`renovate.json`（`enabledManagers: custom.regex`，3 個 customManager 涵蓋 split-knob / single-line / scan-matrix 三形態的 tag+digest，`# renovate:` annotation 錨定多-image chart 防跨 block 錯配）+ `.github/workflows/renovate.yaml`（週排程 + dry-run dispatch，需 owner 加 `RENOVATE_TOKEN` PAT）。**關閉 L1+L2 的 toil loop**：digest 凍結後 nightly 掃描無法靠上游 tag-patch 自動 close，改由 Renovate 定期重解 digest 並 bump tag；同 depName 的部署 ref 與 scan-matrix ref 在同一 PR 一起更新 → drift-guard 保綠。Offline 守門 `tests/ops/test_renovate_config.py`（把每個 customManager 的 regex 套到真實檔案：擋「靜默零匹配」這個 custom-manager 經典失敗 + 14 完整覆蓋 + matrix==deploy 一致）。**選 self-hosted 非 Mend-hosted app**：本 repo 全鏈自管/pin，不授權外部 SaaS 對 repo 寫入，且 self-hosted 是未來工具-SHA `postUpgradeTasks`（Category B）的必要路徑（PAT 而非 GITHUB_TOKEN 才能讓 Renovate 的 PR 觸發 CI、保住 drift-guard 安全網）。**defer-with-trigger（接同一份 renovate.json 增量擴充）**：下載工具 version+SHA-256 連動（Category B，需 self-hosted postUpgradeTasks + 先把 golangci-lint 改下載 binary tarball）、lang-deps（Category C，CI matrix 穩定後導入）。承 [issue #902](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/902)；owner 活化步驟見 [dx-tooling-backlog.md](docs/internal/dx-tooling-backlog.md)。
- **租戶磁碟告警的 rollout 前哨防線（#692 P0③ W3）**：把「宣告了 disk recipe 但平台收不到 tenant-attributed `kubelet_volume_stats`」的盲點補成**誠實三層**（author→onboarding→runtime，承 W2 的 runtime sentinel）。(1) reference `configmap-prometheus.yaml` 補上 CSI-gated 的 kubelet volume-stats scrape job（apiserver node-proxy）+ `namespace→tenant` relabel，平台 out-of-box 就能產出租戶歸屬的磁碟指標（非 CSI 叢集 keep 0 series、無害）；(2) `byo_check.py` 新增 onboarding step——對 live Prometheus 查「宣告 disk recipe 的 running 租戶是否真有 volume-stats 到貨」，把 sentinel shift-left 到部署前（running-pods guard：未部署的租戶不誤報）；(3) `compile_custom_alerts.py` 編 disk recipe 時印 author-time prerequisite notice。**對抗式 review 定調**：**不做**靜態 scrape-config lint——它驗 config 形狀而非 metric 流動（CSI/RBAC/relabel 不符時綠燈卻仍 inert＝false confidence，違背 pint/operator/mixin「scrape coverage 一律 runtime 驗」的業界實踐）。順補 W2 sentinel 盲點：`CustomRecipeDiskInert` declared-leg 加 `kubelet_volume_stats_used_bytes` 的 OR-of-exact leg（認得 usage-based disk recipe、過 #731 contract）+ 把 sentinel 的 `db-.+` 重新註解為**刻意的 scope 選擇**（對抗式複審糾正：原稱「broaden 是 no-op」是錯的——user_threshold 的 `tenant` 是 conf.d id 非 namespace-relabel，broaden 會開始對非-db 租戶誤觸；正確理由是 scope-by-design，與 scrape-side db-* keep 一致）。promtool 11 案（+used_bytes 雙向 + both-recipes 單一觸發 + non-db-scope 鎖 #9 決策）；thin-provisioning 謊報列 accepted residual（需 node_filesystem 交叉檢查、界外）。
- **跨租戶閾值分布治理 Dashboard（#655，#659 last-mile activation epic 首件）**：新增 Grafana dashboard `k8s/03-monitoring/fleet-threshold-distribution.json`，把早已 scrape 但無人消費的 `user_threshold{tenant,metric,component,severity}`（值即閾值）cash out 成「**跨租戶治理視角**」——對某 `(metric, severity)` 畫出全平台閾值分布（histogram + P5/P50/P95 時序 band），一眼看出設太嚴（alert fatigue）或太鬆（保護不足）的租戶。**引入業界最佳實踐**：離群偵測用 **Tukey 1.5×IQR fences**（只標真正離群、群體健康時為空）而非固定「P95 以外」（永遠機械標 5%）；統計量用**穩健的 median/IQR** 而非易被離群值污染的 mean/stddev；雙尾並陳、**方向不硬編**（`metric > threshold` vs `<` 語義相反，交由 SRE 對照 rule pack；tenant-agnostic）。三個 template 變數 `$metric/$severity/$component` 把每次比較鎖在單一尺度（不同 metric 閾值尺度天差地別）。誠實標註盲點：**disable 態不發 series**（resolve `continue`），最裸奔的「關掉告警」租戶在此不可見。頂列 P50 給出全租戶已設閾值的『共識中心』，與 [recommender #656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656)（取個別租戶觀測指標的歷史百分位）互補——本被動視角先用分布資料判斷 Day-2 真痛點，再決定後續投資。doc 導覽見 [grafana-dashboards.md](docs/grafana-dashboards.md)（Dashboard 3）。並附 **drift-proof promtool 回歸測試**（`tests/dx/test_fleet_threshold_dashboard.py`：從 dashboard JSON 讀出 query、代入測試變數、配合成 user_threshold fixture 跑真 Prometheus 引擎驗 golden——mutation 自證可攔語意破壞；Grafana shape lint 守 panel/transform/gridPos）。**誠實 bounding 離群法可靠度**（對抗式 best-practice 複審揪出）：Tukey fences 在 **mode-heavy（多數租戶吃 default → IQR=0 標光所有客製者，最常見情形）** 與**小樣本**兩種情形退化——Tenants panel 加紅/黃/綠樣本充足度色階、離群表降為「統計提示非 ground truth」並導向 robust 的「Δ from median」表 + histogram，兩種退化邊界由測試 golden 固定（robust 相對偏差法列 defer-with-trigger）。**無障礙合規收尾（ADR-012 / WCAG 1.4.1，#655 可關單前提）**：原頂列「樣本充足度」與「Outliers」狀態為**純紅綠背景色**編碼，違反 repo 自家 ADR-012（嚴重度不得只靠顏色，須配符號+文字）→ 改以 Grafana value-mapping 走**符號+文字**通道（樣本充足度 ❌ Sparse／⚠ Marginal／✓ Adequate；Outliers `✓ 0`／`⚠ ≥1`——**每個顏色階都有符號對應**，含告警態），顏色降為冗餘強化，紅綠色盲使用者亦可判讀；此編碼由純-JSON a11y golden 固定（斷言 symbol-text 數 ≥ 顏色階數、不需 promtool、到處可跑），防日後靜默退化。
- **租戶磁碟 IOPS / 吞吐告警（per-container，#692 P0④）**：租戶可對磁碟 I/O 暴衝（runaway query / backup storm）設 `rate` recipe over cAdvisor `container_fs_*`。reference cadvisor scrape 補上 container_fs 緊 keep（reads/writes 的 ops + bytes 共 4 個）+ `namespace→tenant` relabel + drop `container=""`/`POD`（防 pod-root 重複計）；recipe 框架零改動（`rate` 沿用）。**對抗式 + 業界（Brendan Gregg USE method、cAdvisor #3588/#1702）複審定調的誠實邊界**：此信號 **per-CONTAINER 非 per-PVC**，且**網路儲存（NFS/EFS）走 network stack 繞過 cgroup blkio → `container_fs`=0**；故定位為 **baseline-relative 異常**信號，而非 absolute saturation（後者是 node 級 %util / latency、屬 platform-SRE、無法 per-tenant 拆解）。誠實性靠 **codified fidelity gate**：`byo_check` Step 5 對宣告 IOPS recipe 的 *running* 租戶查 container_fs 是否到貨，**不到貨即 fail-loud**（逮 blkio-bypass / 未 scrape）；**刻意不設 runtime sentinel**（cAdvisor 恆在、inert 必為平台側 → per-tenant 派發是錯觀眾，與 W2 disk-fill 的 tenant-side cause 性質不同）。compile author-time notice + BYO onboarding docs（ZH+EN，含 fidelity-gate SOP）；byo_check 27 案。**完成 #692 P0③/④ disk 全系列（W1–W4 + IOPS）**。
- **Runtime Canary 設計就緒 + CI demo（ADR-025 deferred「runtime canary」項收尾）**：補上「Watchdog 看引擎活性、pint 看作者時規則正確性，但租戶自訂告警的『編譯→投遞』**執行期**活性無人守」的盲點設計。新增 [Runtime Canary 設計](docs/design/runtime-canary.md)（ZH+EN）：保留假租戶走**真實** conf.d→exporter→compiler→Prometheus 鏈、必觸發 + `mode:silent`，其**消失**由 dead-man's-switch meta-alert `CustomAlertPipelineCanaryDown`（刻意盯 core recording rule、非 `absent(ALERTS)`——後者會被 `CustomRecipeSilent` sentinel 掩蓋 → 漏報）page。config-locus 定為 `conf.d/` GitOps + 保留租戶（**不**寫死 `k8s/`——那會繞過最易靜默壞的編譯環節 → false-green 退化成第二個 Watchdog）。並**誠實修正** ADR 原「壞租戶隔離＝故意弄壞設定 canary 仍編得過」敘述：本平台靠**兩層**達成——(1) 編譯/CI **fail-closed + 指名出錯檔**（壞設定整批擋下、永不部署）、(2) 執行期 **per-tenant row 獨立**（`max by(tenant)` + `on(tenant) group_left`）。附**經真實編譯器產出、CI promtool 跑**的 demo（`tests/rulepacks/runtime-canary{.rules,_test}.yaml`，三案：活性／隔離／dead-man's-switch）。**常駐部署仍 defer**（heartbeat emitter + meta-alert 接真實 on-call；trigger：重大編譯/路由重構前佈署當安全網，或首個 prod pipeline 事件後防再犯）。見 [Runtime Canary 設計](docs/design/runtime-canary.md)。
- **閾值推薦引擎接成主動治理迴路（Renovate-for-thresholds，#656；#659 last-mile activation epic 第二件）**：把早已存在、卻只能手動跑的閾值推薦引擎（`threshold-recommend`）接成**主動迴路**，直擊「閾值腐敗」這個最痛 Day-2 場景（某次事故把延遲閾值調鬆到 2000ms、修好後沒人回收 → 長期裸奔）。新增 `da-tools threshold-govern`（**安全預設 dry-run，`--apply` 才寫**）+ 每週離峰 CronJob（`concurrencyPolicy: Forbid`）：跑推薦 → **過濾**出腐敗夠大者（`|delta| ≥ --min-delta-pct`，預設 25，且 confidence ∈ {HIGH, MEDIUM}，樣本不足不開 PR 防破窗）→ 為每個租戶開一個**可一鍵批准的 per-tenant proposed-PR**（reuse tenant-api 單寫者 WritePR，ADR-011/023），而非發一個沒人處理的 Slack nudge（config-rot 的失效模式正是「沒人 act on nudge」，對標 Renovate/Dependabot 的 approvable diff）。**介入階梯 PREVENT→DETECT→DELIVER 本件只落地 DELIVER**；DETECT 直接用引擎現成的 `current vs P95 delta%` 當腐敗幅度訊號，推薦邏輯 / 資料源完全沿用 `threshold-recommend`（Day-N observed recording rule，#719；不與 `baseline_discovery` Day-0 快照混用）。三道護欄：**Dedup**——tenant-api 對「該租戶已有 pending PR」回 409 → 跳過，重跑不洗版；**通道隔離**——PUT 帶新的 `X-DA-Write-Source: threshold-governance` header（tenant-api 小改，allowlist-only、header 值不插入 PR 內容）→ governance PR 走獨立 label / 標題 / 來源、不冒充 tenant-manager UI、不污染 Alertmanager 告警平面（保護 alert-fatigue budget）；**讀-改-寫只 surgical 取代被推薦的值行**（保留註解與其餘設定 byte-identical + parse-before/after 驗證「只動目標 key」→ PR diff 乾淨、永不洗掉租戶其他 config）。`--max-prs`（預設 5）+ `--throttle-seconds` 防單次洪水。in-cluster auth 走 tenant-api 既有 port 8080 api-internal（繞 oauth2-proxy）+ NetworkPolicy + RBAC governance 群組；oauth2-proxy 前置部署改用 `--auth-token`。**defer-with-trigger**：PREVENT 層 threshold `expires:`、直推租戶（tenant-direct，trigger：proposed-PR 準確率連續達 SRE 認可）、`<`-metric 推薦（引擎現況安全 skip）皆 defer。見 [cli-reference `threshold-govern`](docs/cli-reference.md) + `k8s/03-monitoring/cronjob-threshold-govern.yaml`；承 [issue #656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656)。
- **治理迴路浮現「lower-bound `<` 未治理」盲區（#656 DELIVER 收尾，承上條）**：`threshold-govern` 輸出新增「N 個閾值未治理（lower-bound `<`，需人工 review）」計數 + 清單（text 與 `--json` 皆有；後者落 `summary.ungoverned_lower_bound` + 頂層 `ungoverned_lower_bound[]`）。背景：`<` 指標（hit-ratio／availability，腐敗＝**調降** floor）的 P95-upper 推薦會**反向削弱保護**，引擎現況安全 skip（`recommended=None`）→ 這些 key 會**靜默**缺席於治理 gate。本筆把「safe skip」從**隱性**盲區改成**可觀測**計數（比照 #655 dashboard 盲點標註的誠實原則），讓 `<` 推薦的 defer-with-trigger 看得見、而非無聲覆蓋缺口。`<` 推薦本體仍 defer（需方向感知下尾百分位 + 對真實分布驗證）。承 [issue #656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656) locked decision。
- **Node/Cluster 維護告警抑制決策（[ADR-026](docs/adr/026-node-maintenance-liveness-suppression.md) proposed）**：回答「node 維修 / 多叢集 rolling upgrade 時抑制受影響租戶告警、其他叢集照常」的 day2 需求。經 **Gate 1 靜態實證**（零叢集成本，逐條過乾淨 drain 會 fire 什麼）+ 兩輪外部 adversarial review 收斂為 **narrow PARTIAL**：唯一不可抑制殘量＝平台 `*ExporterAbsent` 類 × 單實例 exporter（cordon≠NotReady、graceful evict≠crash、閾值類與 custom recipe 皆已吃 `_state_maintenance` opt-out）。決策**不建 cordon-aware 子系統**——HA exporter 為主（讓 `absent()` 恆 false、殘量歸零）+ 平台 liveness 類做 gated / Max-TTL≤1h 的 maintenance-aware 抑制（技術走 `tenant_metadata_info` anti-join，順手修掉 bare `absent()` 無 tenant label 的既有缺陷）+ 重用既有 silence/inhibit/`maintenance_scheduler`、零新常駐元件（延續 ADR-008 不建 controller）。cordon/taint-driven 宣告式抑制、多叢集 cluster-label、HA-breach load class 皆 defer-with-trigger。同收一份 [{角色 × 生老病死 × gate} 生命週期治理矩陣](docs/internal/monitoring-lifecycle-governance-matrix.md)（對應「降低採用認知門檻」長期目標）。
- **PREVENT 層落地：threshold `expires:` 時限化覆寫（Renovate-for-thresholds 的 PREVENT 段，#656；#659 last-mile activation 第三件）**：threshold-exporter 的結構化閾值新增 `expires:`（RFC3339）+ `reason:`，把「事故中把閾值調鬆」變成**結構上不腐敗**——到期後該覆寫**自動 fail-safe 回平台 default**（更嚴格、**永不靜默**：仍發 `user_threshold`、只是換回 default 值，故 cardinality 不變），並發 `da_config_event{event="threshold_expired"}` 供 cleanup PR 移除 stale YAML + 觀測。複用 silent/maintenance 已驗證 ×2 的到期骨架（`ScheduledValue` 加欄位 / `ResolveThresholdExpiriesAt` / `collectThresholdExpiries`）。**v1 範圍鎖在 6 個標準指標**（`_defaults.yaml` 內、有 default 可回落者）——dimensional / `_critical` / custom-alert（無平台 default、移除會靜默）以 `c.Defaults` membership **結構上排除**，且 `ValidateTenantKeys` 對越界 / 格式錯的 `expires:` **fail-loud 警告**（dev-rule #5）。事件把 metric key 編進 `reason` 以保 (tenant,metric) series 唯一；malformed expires **fail-open**（保留覆寫）+ 警告，對齊 maintenance。Go config + collector 測試（parse / fail-safe-to-default / 範圍+狀態 / fail-loud / da_config_event 比對）。**defer-with-trigger**：custom-alert `expires:`（需先定保底語義）、cleanup-PR controller（事件已發、自動化清理待建）。schema `scheduledValue` 加 `expires`/`reason`；承 [issue #656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656) locked decision PREVENT 段。
- **Per-tenant exporter liveness——修掉全域 `absent()` 的 silent false-negative（#869）**：舊的 4 條 per-db `*ExporterAbsent`（MariaDB/PostgreSQL/Kafka/RabbitMQ）用 `absent(<db>_up{job="tenant-exporters"})` 做**全域**判斷——只要**任一**租戶的 exporter 還活著，`absent()` 就回空、規則不 fire。後果：某租戶 exporter 整個消失、別的租戶還在 → **零告警**（latent silent false-negative）；且 `absent()` 回空向量讓舊規則的 `tenant` label 模板**恆為空字串**（第二個既有缺陷）。新增 always-on `tenant-liveness` rule group（`rule-pack-liveness.yaml`）+ **value-aware per-tenant anti-join** `TenantExporterAbsent`：`tenant_expected_exporter unless on(tenant) (up{job="tenant-exporters"} == 1)`（`for: 3m`、severity critical、`tenant`+`db_type` label 這次**真的會 populate**）。**`up == 1` 是關鍵**（非裸 `unless up`）：service-role SD 下 pod 死但 Service 在會留 `up=0`**在場**，裸式會被騙過漏報；`== 1` 同時覆蓋 `up=0`（pod 死、Service 在）與 series-absent（退租）。新 collector metric `tenant_expected_exporter{tenant,db_type}=1`（threshold-exporter 純加法，**從 `_metadata.db_type` 產、每宣告 db_type 的租戶 1 條**；**絕不** mutate 承重牆 `tenant_metadata_info`，db_type→label 是對 v2.5.0 cardinality 決策的**窄域受限反轉**）。平台側兜底：catastrophic fallback `TenantExporterJobAbsent`（整 job 蒸發 / collector 自己掛 → anti-join 左手邊空）+ page-storm sentinel `MassExporterOutage`（仿 `DefaultsTruncationStorm` idiom，因 `without(tenant)` 落手維護 platform configmap）。**opt-in 契約**：只有宣告 `db_type` 的租戶被納管。**RETIRE-ordering hard-gate**（`check_retire_drift.py`，pre-commit + CI 雙軌）：禁「只刪 K8s target 卻殘留 conf.d `_metadata`」——否則退租殘留 → `tenant_expected_exporter` 還在 + `up` 斷 → 對下線租戶噴 100% false-positive critical。涵蓋面也**擴張**到原本沒有 ExporterAbsent 規則的 DB（Redis/MongoDB/Oracle/...，前提：emit `<x>_up`）。舊 4 條 alertname 以 **deprecation shim 保留一個 release**（防外部 Alertmanager matcher 靜默斷，下版移除）。⚠️ **此過渡版本內**單租戶 exporter 離線會同時觸發新舊兩條同義 critical（新 `TenantExporterAbsent` + 舊全域規則）——預期內的「雙重告警」代價，維運請儘速將舊 routing 遷至 `TenantExporterAbsent`。6 場景 promtool 覆蓋（staleness 吸收 / sibling 活仍 fire / reschedule<for 不 fire / `up==1` vs 裸 unless / HA 一掉 no-fire / HA 全掉 fire）。承 [issue #869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869)；ADR-026 機制冷宮的 retrievable 對照面。
- **租戶 log federation AccountID 單調配發 + audience-bound logs token（[ADR-021](docs/adr/021-tenant-log-query-federation.md) Phase 1 / #609）**：把租戶聯邦從「指標平面」擴到「日誌平面」的第一塊基石。tenant-api 新增**單調、永不回收**的 per-tenant `account_id`（VictoriaLogs 的租戶分區鍵），來源是**顯式計數器**（`_account_registry.yaml`：`next_account_id` high-water mark 從 1000 起、0=平台 default、1–999 系統保留）而非 hash 映射（hash 會碰撞→兩租戶日誌相混）。配發**冪等**（同租戶必得同號）、**永不回收**（退租不釋放 id——退租後同號配新租戶＋舊 log 還在 retention 窗內＝跨租戶洩漏），且配發計算在 gitops 單寫者 mutex 內**鎖內重讀**後才取號，兩個並發 onboarding 不會撞號。**能力模型 = audience-bound（B）**：federation token endpoint 新增 optional `capability`（`metrics`|`logs`，**預設 metrics 完全 back-compat**——既有 metrics token 的 audience（`tenant-federation`）與 payload **逐位元不變**、不含 account_id）；`capability=logs` 才簽 logs-plane token（audience `tenant-federation-logs` + 內嵌 `account_id` 數值 claim），distinct audience 讓 logs token 無法被重放去打 metrics proxy（反之亦然）。registry 為 `_`-prefix 檔→threshold-exporter loader 跳過（非租戶），但走**與 `_groups.yaml`/`_views.yaml` 同一條 commit-on-write GitOps 軌跡**（無外部 stateful DB，ADR-009）。純配發核心（in-memory registry）與 git 持久化分離（可無 shell-out 單元測試）；損毀/越界 registry **fail-closed 拒絕配發**（錯號＝資料洩漏，非可復原小故障）。新增 admin-only `POST /api/v1/federation/accounts/backfill` 為既有租戶一次補齊 id（冪等、單一 committed write）。ProjectID 不在本 PR——logs gateway 後續 PR 會以 `ProjectID=0` 呼叫 VictoriaLogs（(b) 平台營運 log 分區），多專案分區（(a) 應用 log）留 Phase 2。OpenAPI spec（`docs/swagger.{json,yaml}` + `docs.go`）已隨 `capability`/`account_id`/backfill route 重生。**安全加固**（Gemini 對抗審 + CodeRabbit）：(1) **配發時 git-history high-water floor**（#1/#3 主防線）——allocator 每次配號前讀 `_account_registry.yaml` 的 git 歷史最大 `next_account_id` 當下限：`git revert`／手改可降低磁碟上的計數器、但抹不掉歷史中的高值，故被退回的 id **永不重發**（關死「revert 計數器→重發已配發 id→跨租戶 log 合流」；作用於真正的 live conf.d plane、topology-independent、歷史不可讀則 fail-closed 拒配）；(2) 輔以 commit 層 `next_account_id` **單調歷史 lint**（`check_account_registry_monotonic.py`，pre-commit staged-vs-HEAD + CI PR-vs-base，作 committed-registry 的 defense-in-depth；registry 缺檔／未追蹤即 no-op）；(3) tenant-api **啟動自檢**——registry 空白／缺失但 conf.d 已有租戶即 `log.Fatal` 拒啟動（防 0-byte 截斷後從 1000 靜默重發）；git **index.lock 競爭**映射為 `ErrWriteOverloaded`→503（retryable，非 500 告警風暴）；backfill 的 GitOps 寫入改用**獨立 context**（綁 `--write-timeout`、脫離全域 30s request timeout，大 fleet 不被砍）；backfill swag 補 `@Failure 409`；metrics-plane token record 維持 pre-ADR 逐位元 shape（只在 logs 才填 `capability`）。承 [issue #609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609)。
- **#869 liveness 的 Day-3 SRE 硬化（#880）**：對 #869 的三項 scale / 故障注入硬化（皆非 #869 blocker，承外部 Day-3 review）。(1) **db_type typo 的 shift-left 防線**——opt-in 契約的反面風險是 `_metadata` 把 `db_type` 鍵打錯（`dbType` / `db_typ`）或值打錯（`maraidb`）→ collector 看到 `DBType=""` → 該租戶被**靜默**移出 liveness（與 #869 修掉的同一類 silent-failure，反而由 opt-in 機制重新開了一個洞）。`tenant-config.schema.json` 的 `_metadata` 由 `additionalProperties: true→false`（擋鍵錯字）+ `db_type` 加 enum（10 個 DB 型，擋值錯字、SSOT 對齊 `PACK_ORDER`），新增 `check_confd_schema.py`（pre-commit + `ci.yml`，OSS jsonschema 引擎、對齊 hybrid lint policy）對**全部 tenant-shaped conf.d** 驗 schema，把防線從 runtime 推到 author/CI（meta-files `_*.yaml` skip 並列印、不靜默蓋面）；順帶補掉 schema 既有的 `_state_maintenance` 純量↔物件 drift（widen 成 `oneOf`，對齊既有 `_silent_mode`）。(2) **`MassExporterOutage` 由魔術數字 `> 10` 改 ratio + floor**——絕對閾值兩端皆 mis-scale：8 租戶小叢集全掛（<10）→ sentinel 啞、on-call 吃 8 條個別 page；單 node ~15 exporter（>10）→ routine node 故障誤升機房級。改 `count(down) > 5 AND count(down)/count(expected) > 0.1`（絕對下限防小叢集噪聲 + 比例防大叢集誤升；`without(tenant, db_type)` 同時套 numerator/denominator）+ `unless on() absent(up{job})` job-presence gate（整 job 缺席時抑制本規則、避免與 `TenantExporterJobAbsent` 重複轟 critical；CodeRabbit #882）。分母 `count(expected)` 在 collector 自掛時歸空的 caveat 由 `TenantExporterJobAbsent`（`absent(up{job})`）兜底，sentinel 非唯一依賴。promtool 7 場景（small-cluster up=0 → fire、job-absent → 本規則啞 + JobAbsent fire、floor 5-down → silent、ratio-guard 6-of-61 → silent、collector-dead → 啞 + JobAbsent）。(3) **Rule A scope 邊界文件**——`TenantExporterAbsent`（`for: 3m`）只抓「乾淨死透 / 缺席」；CrashLoopBackOff「詐屍」flap（每 <3m 短暫 `up==1` 一次 → reset `for:` 計時器）刻意**不**在此疊邏輯（會把穩定的 anti-join 弄成 flappy），由 `ContainerCrashLoop`（讀 kubelet `CrashLoopBackOff` waiting-reason、backoff 退避期間穩定為 1 不 flap、`_defaults` 預設啟用）覆蓋；殘量＝設了 `_state_container_crashloop: "disable"` 的租戶（如 db-b）兩者皆不接，於 `rule-pack-liveness.yaml` source 註明。承 [issue #880](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/880)、外部 Day-3 review。
- **recipe-preview 接上發布線——image 可發布、chart 真能部署（#657 PR-D2，收部署線）**：把 #883 已 merge 的 recipe-preview helm chart 從 `ImagePullBackOff` 變成可真實部署——新增第 6 條 build 線 `recipe-preview/v*`（`release.yaml` job + `component-docker-build` PR smoke + `pre-tag` 的 `docker-build-all`/`trivy-scan-all` + Makefile `release-tag-recipe-preview`），歸「**同步升**」類（每次平台 release 重 tag、與 rule-pack 同 commit、非獨立 cadence——防打包的 compiler 快照悄悄漂離平台 compiler）。Dockerfile 補 **多架構 inline**（buildx `TARGETARCH` + per-arch promtool SHA-256，amd64/arm64）+ **build `GIT_SHA`**（ARG→ENV→`/healthz`，drift 觀測：確認跑的是哪個 commit 的 bundled compiler）+ image cosign keyless 簽章。新增 **promtool 版本相等 CI gate**（`tests/preview/test_promtool_pin_parity.py`：Dockerfile pin == `ci.yml` pin——firing/inactive 判定格式與 promtool 版本綁、skew 會靜默誤判且無其他守門）。Chart.yaml 同步升至 cohort 版（2.9.0）+ 註冊進 `bump_docs.py` 的 wrap 批次。五線版號→六線（CLAUDE.md / playbook / vibe-release skill 同步）。承 [issue #657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) PR-D2。
- **每晚對 `main` 掃 image CVE——補上「發版夜 CVE 突襲」盲點（平台 CI 韌性）**：新增 `nightly-image-scan.yaml`（schedule，每晚 03:23 UTC + 可手動 `workflow_dispatch`）——對 `main` build 出的 5 個 component image 跑 Trivy（與 `release.yaml` **同契約**：`CRITICAL,HIGH` + `ignore-unfixed`），有 fixable CVE 就開／更新**一個** deduped tracking issue（label `nightly-cve`、全清時自動關）。**為什麼**：PR 階段 Trivy 是 informational（#448，避免上游突發 CVE 擋無關 PR）、唯有 release 階段才 hard gate——於是 base-image（Alpine/distroless/nginx）CVE 在「上次 PR → 真正打 tag」之間落地時，只有**發版當下**才第一次撞見並被擋（`security-audit-runbook.md` §Release-day CVE drift 實戰：v2.9.0 GA 連撞 3 組剛揭露 HIGH）。此第三維度讓 CVE 提早幾天浮現、**non-blocking**（release hard gate 仍是最後一道線）；da-tools 走 stub build 故只掃 OS/base 層（Go binary CVE 由 release 真 build + Go CI 覆蓋）。承 [#886](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/886) 外部 adversarial review（Gemini）建議。
- **租戶可查「平台關於它」的營運 log——Vector 淨化投影 + VictoriaLogs Layer-1 護欄（[ADR-021](docs/adr/021-tenant-log-query-federation.md) Phase 1 / #609 PR-3）**：把 (b) 平台營運 log 的「資料平面」鋪好——讓租戶日後能在平台上**就地查自己的** federation audit log，且**看不到**基礎設施拓樸或他租戶的列。採 **fan-out（雙寫，非搬移）**：平台完整副本（含 `node_name`/`pod_ip`/`pod_node` 等全欄位）**續留 `0:0`**（#539 現狀不變，平台 ops 的跨租戶查詢面）；**新增**對「帶有效 `tenant_id` 的 `federation_audit` 列」的淨化投影——**allowlist 淨化**（只投影 `tenantProjectionKeepFields` 安全欄位、其餘含原始 `.message` 與 `upstream` 後端 IP 結構性排除——對抗 review 把原 denylist 的 fail-open 翻成 fail-closed）後，以**固定 `AccountID` header** 寫進該租戶的 VictoriaLogs `(AccountID, ProjectID=0)` 分區。`gateway_operational`／JWT-fail／`suspicious_audit`／`prometheus_query_log` 列**永遠 platform-only**。三項對抗式硬化：(1) **fail-closed 結構性保證**——`tenant_project` remap 以 `drop_on_abort`+`drop_on_error`+`reroute_dropped:false`，對「非 audit／空或未知 `tenant_id`／parse-error 非 JSON」一律 `abort` 落地丟棄；`tenant_id→AccountID` 走**從 Git 帳號 registry 衍生**的顯式 map（禁 hash 衍生＝防碰撞洩漏），查無對映則 `account_id` 缺席 → `route` 落 `_unmatched`（無 sink 消費＝丟棄），**無 catch-all 租戶 sink**，誤標不可能落他租戶；(2) **靜態 N-sink**（非單一動態 header sink）——Vector 的 per-batch header 在混租戶 batch 內會污染（[vectordotdev/vector#21402](https://github.com/vectordotdev/vector/issues/21402)），故 `route` → 每租戶一個常數-header sink、batch 結構性單租戶；(3) **跨分區關聯 `log_event_id`**——在共用 `demux` 階段（drop 之前）注入 **time-sortable UUIDv7**（Gemini fold-in：VictoriaLogs 時序優化、利 `0:0` 全域檢索 join），**同存於 `0:0` 與租戶副本**，值班可用它把租戶淨化截圖 join 回完整 node 資訊（淨化不拉長 MTTR）。VictoriaLogs **Layer-1 查詢護欄**（`.Values.search` → `-search.maxQueryTimeRange=7d`／`maxQueryDuration=25s`／`maxConcurrentRequests=6`／`maxQueueDuration=10s`）：⛔ `maxQueryDuration 25s` **嚴格 < gateway route 30s**（Gemini fold-in cascading-timeout：storage 先 abort、不留 zombie query 佔並發槽）。**AC（vector test 入 CI）**：`vector validate`（語法）+ `vector test`（`helm/vector/tests/projection_tests.yaml`）餵極端 payload 斷言確定行為——**negative assertion**（餵帶**全部**敏感欄位的 mock，斷言租戶副本中 `upstream`(後端 IP)／`pod_node`／raw `.message`／payload 注入的 `account_id`/`log_event_id` **確實不存在或被覆寫**，測「移除了」非「有產出」，且以 gateway **真實 json_format** seed）。**對抗式硬化（self + 3-lens 對抗 review）**：denylist→**allowlist** fail-closed 淨化（堵 `upstream` 後端 IP 與 raw message 洩漏）、`tenantProjections` **唯一性 render-time `{{ fail }}` guard**（重複 accountId 混租戶分區／重複 tenantId mis-route）+ `values.schema.json`（非整數 accountId）、`log_event_id` 改**無條件覆寫**（防 producer 控 join key）。另收 Gemini #894 資源隔離輪：tenant sink 配 `when_full: drop_newest` buffer（防單一租戶背壓經 `tenant_route→demux` head-of-line-blocking 卡住 `0:0` 與其他租戶）、`query` 長度 cap（`tenantProjectionMaxQueryBytes` 防 `encode_json`/VictoriaLogs 單行爆）、`values.schema.json` 擋非整數 accountId、stream-field 限低基數防 RAM 爆／空與未知與 parse-error `tenant_id` → 僅 `0:0`／`log_event_id` 兩副本同在／operational 僅 `0:0`；另 helm-render 斷言 Layer-1 flags 與 N-sink 固定 header 與 `maxQueryDuration<30s`。預設 `tenantProjections: []` 時零新 transform/sink（單租戶 store byte-相容 #539）。vector chart 0.5.0→0.6.0、victorialogs 0.1.4→0.2.0（log-agg 線 per-change bump）。本 PR 只鋪資料平面；gateway `victorialogs` mode 查詢授權（PR-4/5）、tenant-api AccountID 配發（PR-1 已 merge #887）為他 PR。承 [ADR-021](docs/adr/021-tenant-log-query-federation.md) §Ingestion fan-out + AC；runbook 補 §8。
- **federation-gateway 新增 `victorialogs` mode——租戶日誌查詢的授權平面（[ADR-021](docs/adr/021-tenant-log-query-federation.md) #609 Phase 1 PR-2）**：既有 mode-pluggable 的 Envoy gateway（ADR-020 Layer 2）新增第三個 mode，當租戶查自己的平台營運 log 時強制跨租戶隔離。隔離核心是 **VictoriaLogs 原生 `(AccountID, ProjectID)` 租戶模型**——gateway 從**已驗證**的 federation-logs JWT claim 注入 `AccountID`/`ProjectID` header，VictoriaLogs 只回該分區。三道把關：(1) **audience 強制**——此 mode 的 `jwt_authn` 要求 `aud=tenant-federation-logs`，純 metrics-pull token（`aud=tenant-federation`）直接 401 連 Lua 都進不到（能力模型 B 的防護點，且 `_helpers` 在 render 期 fail-loud 擋設定錯誤）；(2) **fail-closed null-claim 防線**——`revoked_check.lua` 對缺失／空／非正整數／`<1000`（保留區）的 `account_id` claim 一律 `403`，**絕不放行**（VictoriaLogs 對缺 `AccountID` header 預設導向 `0`＝平台分區，放行即越權；Lua 持已驗證 claim、比 route-cache 時序更確定地關死）；(3) **endpoint 預設拒絕白名單**——只放行 VictoriaLogs LogsQL query/metadata endpoint（`/select/logsql/query`、`/hits`、`/facets`、`/stats_query[_range]`、`/streams`、`/stream_ids`、`/{stream_,}field_{names,values}`），其餘（含繞過 `maxQueryDuration` 的長連線 `/tail`、`/insert/*` 寫入面、跨租戶 `/select/tenant_ids` 列舉、與任何未知/未來 endpoint）一律 catch-all `403`；path 先做 `%2F`/`//` 正規化（比照既有 `/api/v1/read` reject 那套嚴謹度）。client 自帶 `AccountID`/`ProjectID` 由 Lua `replace()` 覆寫關死——**刻意不**在 route/vhost `request_headers_to_remove` 列這兩個 header：Envoy 的 route 層移除由 **router filter** 在 Lua **之後**執行，列了會把 Lua 剛注入的值刪掉→無 `AccountID`→落平台分區（cross-tenant breach）；overwrite-at-injection 已無時序窗。access log JSON 加 `account_id` 欄位供 PR-4 mtail 推導 per-tenant metric。allowlist route 另加 **GET+POST method gate**（防未來 VL 把 mutation 掛到 `/select` 路徑時被以寫入 method 觸及）。**跨 chart 連動**：`helm/victorialogs` 的 NetworkPolicy allowlist 補入 federation-gateway（否則真實叢集 gateway→:9428 被 deny、mode DOA），並文件化「Grafana/chargeback 直連 :9428 為平台可信旁路、不受 gateway AccountID enforcement」的 trust boundary。三條安全不變式（catch-all 403 存在且排在 shared route 前／`AccountID`·`ProjectID` 不被 strip／誤設 audience→render fail）以 helm-render 回歸測試（`tests/helm/test_victorialogs_gateway_guard.py`）釘住。**實作期更正一條 ADR footgun**：原 ADR §Blast radius 規定的「route 層 `request_headers_to_remove:[AccountID,ProjectID]`」在 Envoy filter ordering 下反成 breach（router 在 Lua 後執行、刪掉注入值），已改 Lua `replace()` overwrite-at-injection 並 back-annotate ADR-021。Chart `0.2.4`→`0.3.0`。隔離核心由 VictoriaLogs 原生租戶模型擔保，gateway 為授權平面；ingestion 蓋章為解耦的另一份設計（ADR-021 契約）。
- **would-fire 預覽擴到 `absence` 型 recipe（#657 P3，擴覆蓋）**：recipe-builder 的 would-fire 預覽從只支援 `threshold` 擴到也支援 **`absence`**（缺口偵測）。`_recipe_preview.py` 的 `build_preview_test()` 改 **recipe_type 多型**——absence 的合成序列**刻意不發該指標**（規則 `custom:threshold:{id} unless on(tenant) count by(tenant)(count_over_time(metric[window])>0)` 抓不到樣本 → `unless` 觸發），eval 跨過 `window` + `for:`（window 為 Go 時長、parse 取分鐘數，無法 parse 即 **fail-closed error**、不亂猜窗）。absence 是 presence-based 故**不需 scenario value**（該檢查改為 threshold-only）。⛔ 硬 gate：新增**經真實 compiler + promtool 的 FIRING e2e**，斷言 `state=='firing'`（非「不 crash」——P2 `selectors_re` false-inactive 教訓）。rate/ratio/forecast/p99 仍 `supported:false`（誠實標示、不靜默）。設計 §5.2/§7/§8 + 元件 README 同步。承 [issue #657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) P3。
- **租戶日誌查詢的可觀測層——metric + dashboard + 2 告警（[ADR-021](docs/adr/021-tenant-log-query-federation.md) Phase 1 / #609 PR-4）**：把 victorialogs-mode 日誌查詢平面做可觀測，與 ADR-020 metrics-plane 完全對稱。新增 mtail metric `tenant_log_query_requests_total{account_id,project_id,status}`（counter，與 `tenant_federation_requests_total` 同 sidecar 同檔，從**同一條** Envoy audit access log 推導）+ `tenant_log_query_duration_ms`（histogram，Gemini fold-in：查詢延遲分布，來源 access log `duration_ms`=`%DURATION%`；counter 不帶延遲故獨立 metric）。**log-query vs metrics-pull 判別**用「`account_id` 為非空正整數」（metrics-pull token 無此 claim → Envoy render 空字串 → 不 match → 自然排除，免與 envoy.yaml path allowlist 同步漂移）；**`project_id` 來源 = 常數 `0`**（access log 無此欄位、Phase 1 (b) 固定 ProjectID=0；Phase 2 (a) 引入 ProjectID=1 時補 access log 欄位即可，標籤集已預留維度）。新增獨立 Grafana dashboard `k8s/03-monitoring/tenant-log-query-dashboard.json`（uid `tenant-log-query`）：per-account 查詢量／status 分布／**延遲 heatmap + P95**；⚠️ 所有 `histogram_quantile` 聚合**保留 `le`**（缺 `le` 靜默回 NaN——PromQL topology-label 陷阱），由 drift-proof promtool golden（從 JSON 讀 query 驗、含 `le`-present shape lint，mutation 自證）釘住。兩條告警入 `configmap-rules-platform.yaml` `federation-audit` group（warning）：(1) `TenantLogQueryRejectionRateAnomaly`（某 account >50% 查詢被拒持續 15m + ~1 拒/min floor；key `account_id`、`sum by(account_id)` 對齊分子分母防 topology 陷阱）；(2) `TenantProjectionFanoutDiscardSpike`——回填 §8.7「ingestion 投影斷掉」可觀測鉤子，盯 Vector `vector_component_discarded_events_total{component_id="tenant_project"}`。⚠️ **誠實邊界**：後者是**粗粒度 spike tripwire 非精準 gap 偵測**——此 component-level counter 把**所有** abort 原因合計，而設計上**多數** demux 列（non-audit：operational／query-log／JWT-fail）被合法 drop，故**不可**用 `> 0`（恆真噪音），改 `rate > 5/s` 持續 15m；精準的 per-account「可對映租戶投影缺漏」偵測需新增 per-partition row-count metric，列 **defer-with-trigger**。promtool fire/no-fire 同放 `tests/rulepacks/tenant-log-query-platform{.rules,_test}.yaml`（extracted-copy 模式比照 `platform-watchdog`，`configmap-rules-platform.yaml` 非 `rule-packs/` 生成、無 regen）。runbook 補 §8.8 + 回填 §8.7。承 [issue #609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609) PR-4。
- **治理迴路死人開關 `ThresholdGovernanceStale`（#656，補治理矩陣自我一致性洞）**：補上「閾值治理 CronJob 自己靜默停擺、沒人知道」的盲點——每週 `threshold-govern` 若連 8 天沒有成功跑，發 **warning**（config rot 以週計、無 3am 動作，故進 ticket 非 page）。**關鍵是先讓訊號誠實**：`threshold_govern.py` 原本即使 tenant-api 掛一週、每個 PR 都靜默失敗仍 exit 0 → Job 成功 → KSM `kube_cronjob_status_last_successful_time` 照前進 → staleness 告警在**正要抓的事故裡恆綠**（安慰劑）。改為「一輪 `--apply` **無新 PR（`opened==0`）且失敗占比（errors + circuit-breaker skips）≥90%**」即 exit 非零（`_is_systemic_failure`，借用 maintenance-scheduler 同機制 exit 非零→Job Failed，但 trip 條件更嚴：非「任一錯」——單一 flaky 租戶不該凍住時鐘；**用占比、非 `pending==0`**——對抗審查抓到單一倖存 `already_pending` 會把 99% 癱瘓遮成 false-negative，且 **`skipped` 計入失敗分子**——circuit-breaker 全癱時只 ~5 error + N skipped、裸 error 比例會稀釋漏報）→ Job 標 Failed + `last_successful_time` 不前進 → 告警才真會 fire。**業界最佳實踐硬化**（三獨立 subagent 研究 + 對抗式 review + 實證 promtool；kubernetes-mixin 對 cronjob「沒成功跑」本就無標準件、須自造）：`max by(cronjob,namespace)` 投影掉 KSM scrape label（`instance`/`job`）→ alert 身分穩定、避免 OR-arm dedup churn、KSM 未來多副本亦收斂；`unless ... kube_cronjob_spec_suspend==1` → 故意暫停的 CronJob 不誤 page；序列不存在即不 fire → **cold-start 安全**（新叢集首次週排程前 ~7 天不假警，故**不採** pure `absent()` arm，整個 CronJob 被刪的偵測交給 deploy/review——本 repo 無 GitOps drift detector）。放 platform configmap + `tests/rulepacks/platform-watchdog{.rules,_test}.yaml`（KEEP-IN-SYNC；promtool 新增 4 案：fire＋for-debounce＋label-投影 / recent / cold-start / suspend）。承 [issue #656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656)。
- **租戶日誌查詢收尾——可見度治理 + onboarding 指南 + 隔離 E2E（[ADR-021](docs/adr/021-tenant-log-query-federation.md) Phase 1 / #609 PR-5，epic 終章）**：把 (b) 平台營運 log 的租戶查詢從「資料平面＋授權平面＋可觀測層」（PR-1~4）收成可上手、可驗證的完整 feature。三塊：(1) **2-tier 可見度 catalogue**（impl plan item 6）——新增 [`docs/internal/log-visibility-2tier-catalogue.md`](docs/internal/log-visibility-2tier-catalogue.md) 把「哪些 stream class（tier-1）/ field（tier-2）對租戶可見」寫成 maintainer 可審查的策展目錄。⛔ 依 ADR §147 這是**控制平面策展、非 query-path 硬阻擋**（硬隔離已是 VictoriaLogs AccountID 分區 + Vector ingest-time allowlist 淨化）；catalogue **不另列**平行 field allowlist，而是**引用** `helm/vector/values.yaml` 的 `tenantProjectionKeepFields`（PR-3 已定義的 fail-closed allowlist）為**唯一 SSOT**，並由 drift-guard 測試（`tests/dx/test_log_visibility_catalogue.py`，mutation-proven、無需 helm）斷言「文件 Tier-2 表 == enforced allowlist」雙向一致——避免雙寫漂移（改 values.yaml 忘改文件 → 紅燈）。(2) **租戶 onboarding 指南**（ZH-only，比照姊妹件 `tenant-federation.md`）——新增 [`docs/integration/tenant-log-query.md`](docs/integration/tenant-log-query.md) 涵蓋：logs token 取得（`capability=logs` → `aud=tenant-federation-logs` + `account_id` claim，與 metrics token 以 audience 切）、查詢方式（gateway `victorialogs` mode endpoint + default-deny 白名單）、cold-start（剛 onboard 無活動 → 查空結果是**預期非 error**，ADR §285）、2-tier 可見度（看得到哪些 stream/field）、平台側 `tenantProjections` 配發紀律（從 `_account_registry.yaml` 抄 AccountID、永不回收、PR review 對照——PR-4 defer 的 registry-desync 流程防線）。(3) **租戶隔離 E2E**（擴 `tests/federation-e2e/`）——既有 harness 是 metrics-plane（prom-label-proxy + Prometheus），**新增獨立 victorialogs-mode 子 stack**（gateway victorialogs mode + mock log store echo upstream，`victorialogs-compose.yml`，runner 多一個 phase）。scope 為 **gateway-focused**（跨租戶隔離原語是 VictoriaLogs-native＝上游 OSS 保證；gateway 負責的授權平面才是本平台 code）：mock upstream 回顯 gateway 注入的 `AccountID`/`ProjectID`，讓「到底哪個 AccountID 抵達 store」可斷言。14 案含 **Gemini fold-in regression guard**——header-spoofing（client 送 `AccountID`/`accountid`/`ACCOUNTID`/大小寫變體 + 偽造 ProjectID + 偽造 `0` 想讀平台分區）斷言 **JWT-verified AccountID 永遠勝出**（PR-2 Lua `replace()` case-insensitive overwrite，把 footgun 釘成回歸測試）、metrics-token（`aud=tenant-federation`）打 logs endpoint → jwt_authn 403 audience 拒絕、fail-closed null/malformed `account_id`（nil/空/`<1000`/非整數/uint32 溢位）→ 403 never-reach-store、endpoint default-deny 白名單（`/tail`/`/insert`/未知 path → 403）。⚠️ **誠實邊界**：E2E 斷的是 gateway header 處理（隔離 joint property 的 gateway 半），VictoriaLogs 原生分區那半留 full-stack（真 VictoriaLogs + Vector 投影）後續。承 [issue #609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609)。
- **codify「飽和 stroke token 當文字色」lint，防 WCAG 1.4.3 stroke-as-text 復發（[#904](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/904) 後續）**：[#904](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/904) 修了 recipe-builder 把飽和 `--da-color-error`(#ef4444=3.76:1)／`--da-color-warning`(#f59e0b=2.15:1) 當文字色（淺色白底 < AA 4.5:1）的既有 a11y 債，但**同 pattern 在 portal 仍散在 11 檔 43 處**（[#904](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/904) 已修掉 recipe-builder 那 5 處），且既有 `axe-lite-static` 與 design-token compliance 兩 hook 整類盲視。於 `check_design_token_usage.py` 加第三個 check：`text-[color:var(--da-color-error|warning)]` → 提示改 AA 版 `-error-text`／`-warning-text`（`border-`／`bg-` 飽和保留、`-text`／`-soft` 不誤判）。**只認 error/warning**——`--da-color-info`(#2563eb≈5.2:1)／`-success`(#047857≈5.5:1) 本就過 AA 且無 `-text` 變體。繼承既有 **class-(b) diff-only**（[`lint-policy.md` §3](docs/internal/lint-policy.md)：grandfather 既有 43、只擋 PR 新增行）+ `/* token-exempt */` 行豁免（深底色等合法例外）+ PR-body bypass。**選型依據**（外部 research）：業界對 WCAG 對比的權威檢查是 **runtime axe-core**、靜態字串 lint 看不到 rendered bg/theme 故僅前置過濾；髒 baseline 導入規則的共識是 **ratchet／Clean-as-You-Code**（本 repo class-(b) 即此）。**Defer-with-trigger**：(1) runtime axe 對比化（擴現有 E2E、驅動 conditional 狀態）— trigger=a11y epic／客戶稽核；(2) role-based token 命名（結構性消滅 arbitrary-value escape hatch）— trigger=design-system 重構。+8 回歸測試。承 [#904](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/904) 對抗式 review 揪出的 lint 盲區。
- **把 tenant-config schema 接到編輯器 + 守住 Schema/Go/Python 三方不漂移（#658，#659 epic 收尾）**：完整的 [`tenant-config.schema.json`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/docs/schemas/tenant-config.schema.json)（draft-07）早已存在卻沒接到「打字當下」——把 `yaml.schemas` 對映**注入 `.devcontainer/devcontainer.json` 的 `customizations.vscode.settings`**（`.vscode/settings.json` 被 `.gitignore` 的 `.vscode/*` 排除、不可 commit，故走 dev-container 入口），讓編 `conf.d/<id>.yaml` 的人（平台/領域工程師、走 raw GitOps PR 的租戶）即時拿到 inline 驗證 + autocomplete + hover；glob 用字元類 `[^_]` **排除平台檔 `_*.yaml`**（它們形狀彼此不同，硬套 tenant schema 會對合法檔亮紅勾＝對著合法 config 說謊）。新增 `.vscode/extensions.json`（`!.vscode/extensions.json` 放行）推薦 `redhat.vscode-yaml` 給本機 VS Code；非-VS-Code（Neovim coc-yaml / JetBrains / `# yaml-language-server:` file modeline）+ 本機手動設定寫進 [`editor-schema-validation.md`](docs/internal/editor-schema-validation.md)。**drift gate 補成顯式 3-way**：`sync_schema.py` 原只比 Schema↔Go（Go↔Python 另有 `test_reserved_key_py_go_parity.py`、三角靠遞移閉合），現直接讀 Python SSOT（`_lib_constants.py`）於一處比齊 Schema/Go/Python，移除遞移依賴（任一邊 test 被停用不再悄悄打開 Schema↔Python 缺口）+ 集中重複的 Go-regex；Python 端用 `ast` 解析（編譯器級、免疫註解/排版/字串內 `#`——如 `_custom_alerts` 註解的 `component="custom"` 不誤抓），Go 端維持 regex（Python 無 Go AST、`//` 註解先剝除）；mutation 自證 detection。**誠實 defer-with-trigger**：(1) 平台檔 `_*.yaml`（尤其爆炸半徑最大的 `_defaults.yaml`）的編輯器 schema——現有 `defaultsConfig` 定義 `additionalProperties:true` 且未建模 `defaults`/`state_filters`/`_routing_defaults`，接上等於假驗證，須先確實建模（trigger＝收到 value-level 漏網回報／有人踩 `_defaults` typo 進 prod）；(2) payload-level 雙向 fuzz 差分——對抗分析顯示 `ValidateTenantKeys`（只驗 key）與 schema（驗值/型/enum）contract 不同，唯一健全可比的是 key 軸（本 PR 的 3-way 已覆蓋），且該 gate 應為決定性 conformance 語料、fuzz 退 nightly（trigger＝首筆 editor-綠×deploy-紅 的 value-level 回報）。承 [issue #658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/658)（Refs not Closes）。
- **`_defaults.yaml` 頂層 key 守門——擋「靜默掉整塊平台預設」的 typo（#658 fast-follow / Gemini #911 對抗3）**：`_defaults.yaml` 是爆炸半徑最大的設定檔（影響該目錄下全部租戶），但既有 `check_confd_schema.py` 把所有 `_*` 檔全 SKIP → 頂層 key 打錯（`state_flters` / `defalts`）會讓整塊平台預設**被 YAML 解析器靜默忽略**、無人察覺（與 #880 db_type-typo 同 class 的 silent-failure）。新增最小的 [`platform-defaults.schema.json`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/docs/schemas/platform-defaults.schema.json)：**頂層 key 嚴格**（`additionalProperties:false` + `^_state_`/`^_routing` patternProperties 鏡像 reserved-prefix），**巢狀值刻意 loose**（`defaults`/`state_filters` 下的 metric/filter 名是動態的、不建模——full 結構 schema 仍是 deferred item）。接進**兩個 surface**：CI（`check_confd_schema.py` 把 `_defaults*.yaml` 走平台 schema、其餘 `_*` 仍 skip，由 `confd-schema-check` gate）+ 編輯器（devcontainer `yaml.schemas` 把 `_defaults*.yaml`→平台 schema，承 #911 已在 main 的注入點）。**drift guard**：測試斷言平台 schema 的 `_*` 列為 `_lib_constants.py` `VALID_RESERVED_KEYS`/`PREFIXES`（Go/Python/Schema 三方 SSOT）的超集——日後新增 reserved key 時不會讓守門對合法的繼承 override 誤紅。+8 測試（typo 拒絕 / 繼承 override 放行 / 真檔 regression / drift guard）。**選型**（hybrid policy）：用 jsonschema（OSS engine）+ 最小 schema，非 DIY key-loop。Gemini 原建議 Go `DisallowUnknownFields`，但驗證顯示 Go 把 `_defaults.yaml` 解析成 generic `map[string]any`（無 typed struct 可加）→ 改用此更便宜、且跨 surface 的路。承 [issue #658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/658)（Refs not Closes；延續 #911 deferred item a）。
- **tenant-api 讀寫拆分（CQRS）的 defer-trigger codify 成自觸發 alert，並收掉兩張重複的 deferred issue（ADR-023 Deferred A4／[#678](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/678) + [#788](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/788)）**：A4（讀寫拆分部署、給讀路徑 zero-downtime）原以**兩張 open issue**（#678 tracker + #788 RFC 重複）＋ ADR 一句「Portal 上線就做」的模糊 trigger 掛著。檢視後判斷：「讀取 HA 是需求」這個前提**目前無 field data 驗證**（讀路徑為低頻 admin UI、發版數秒讀取 blip 未經量測證實有害），在需求成形前先蓋＝服務一個幻影需求；而模糊 trigger 留 open 只會變 zombie issue。決定 **defer 不變、但把 trigger codify 成會自己叫的 alert** `TenantApiReadHANeeded`（進 `k8s/03-monitoring/configmap-rules-platform.yaml`，severity `info`、**不 page**）：對既有 `tenant_api_sse_clients` gauge 取 **7d 平均並發 > 2**（`avg_over_time(...[7d:1h])`，取平均非峰值＝只認「持續多人讀取面」、不被「單日尖峰」誤觸）即「讀取 HA 成真實需求」→ reopen 實作（互補可看 `rate(tenant_api_requests_total)`）。**⛔ 觸發時注意**：A4 只買**讀** zero-downtime，**寫**路徑（Save）發版仍中斷，須與 A3（K8s Lease，[#787](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/787)）**同排**、勿單獨出。兩張 issue 一併**關閉**（codified-beats-documented：自觸發 alert 取代「關掉的 issue 裡沒人看的 TODO」）。promtool 行為契約 `tests/rulepacks/platform-read-ha-trigger{.rules,_test}.yaml`（sustain-fire／below-threshold-no-fire／transient-spike-no-fire，且跨 Recreate pod-identity）。承 ADR-023 Deferred A4。
- **tenant-api 執行期單寫者破口偵測 + A3 defer-trigger codify（ADR-023 L3／[#787](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/787)）**：ADR-023 單寫者不變式原只有**部署期**靜態守衛（L1 Helm `fail`＋`check_single_writer_invariant.py`、L2 `strategy: Recreate`）；**執行期**把副本拉 >1 的三條路徑（`kubectl scale`／runtime HPA／GitOps reconcile 一個落在 `ignoreDifferences` 的 `replicas`）**完全繞過**靜態檢查，唯一預防是延後中的 L3 K8s Lease，殘留風險原本「等資料毀損事故才知道」（lagging、且零偵測）。補上 alert `TenantApiSingleWriterBreach`（進 `k8s/03-monitoring/configmap-rules-platform.yaml`，severity `critical`、**會 page**）：`max by (namespace, deployment) (kube_deployment_spec_replicas{...tenant-api}) > 1` **或** `... (kube_deployment_status_replicas{...tenant-api}) > 1`、`for: 2m`，live Deployment 的宣告副本**或實際運行 pod** >1 即在 2 分鐘內 page，把殘留風險改成 **leading** 訊號，本身即 #787 的 codify re-eval trigger（對稱於 A4 的 `TenantApiReadHANeeded`）。**設計（雙 gauge `or`）**：spec=意圖（最早訊號＋L1 `replicaCount==1` guard 的執行期鏡像）＋status=現實（抓 spec 仍 1 但實跑 2 的背離，如 eviction 替補撞 stuck-Terminating；Recreate 不 surge 故無正常誤觸）；`max by` 聚合剝 scrape `job`/`instance` label 防靜默永不觸發；`for: 2m` debounce GitOps self-heal 但**不**拉長到蓋過自癒週期（持續雙寫者視窗即使自癒也值得 page）。**誠實殘留**：兩 gauge 皆讀 control-plane 狀態，擋不住網路分割 ghost pod → 此殘留唯 L3 Lease+push fencing 可封（更證 #787 必要）。promtool 行為契約 `tests/rulepacks/platform-single-writer-breach{.rules,_test}.yaml`（5 案：spec 2-副本持續觸發+聚合剝 label／單副本不觸發／sub-`for` self-heal 不觸發／3-副本 HPA 觸發／spec=1 但 status=2 背離經 status 腿觸發）；細節 [tenant-api-hardening §5.7](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/docs/api/tenant-api-hardening.md)。承 ADR-023 Deferred A3（Refs not Closes——#787 留 open，defer 不變、只是把偵測與 leading trigger 補上）。基於外部對抗式 review（Gemini）補強 status 腿與誠實邊界。

### Fixed

- **recipe-preview bundled promtool 2.53.2 → 3.12.0：清掉發版會擋 tag 的 nightly HIGH CVE + 對齊 prod evaluator（#895 recipe-preview 半，承 #657）**：recipe-preview 是 Python 服務，其 nightly fixable HIGH CVE **全數來自 bundle 的 Prometheus `promtool` Go binary**（非任何 first-party code），單靠 first-party dep bump 清不掉。升 `PROM_VERSION` 至 3.12.0（Dockerfile + `ci.yml` lockstep pin + amd64/arm64 SHA-256；parity test 同步）清掉升版可解的（x/net 0.55 + oauth2 + grpc + jwt + docker + 大部分 stdlib；另 2 個只在 Go 1.26.4 修、而 promtool 3.12.0 用 1.26.3 build 的 stdlib 見下方殘留），並讓 would-fire 預覽的 promtool 與 prod Prometheus（已是 v3.12.0）**同版**、消除判定 evaluator skew。升版前以 spike 證實 `classify_promtool_result` 的 `FAILED:`/`got:[` 判定 markers 與 32 個 rule-pack promtool 單元測試在 3.12.0 行為不變（唯一副作用：`rule-pack-mariadb` CPU 告警的 `{{ $value }}` 在 Prometheus 3.0 left-open range-selector 下浮點位移成 0.999…，改 `printf "%.0f"` 取整消除浮點位移——該 metric 實為 rate、'running threads' 文字與整數化屬 pre-existing 語意 mismatch（已開 [issue #942](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/942) 追蹤），configmap + operator 3-copy cascade 同步）。**殘留 11 個 bundled-promtool CVE 以證據治理**：9 個 `x/crypto/ssh` 標 `not_affected`（govulncheck 證實該碼被 linker DCE 出 binary、不存在）、2 個 stdlib（x509 / MIME，fixed Go 1.26.4 但 promtool 3.12.0 用 1.26.3 build）標 time-boxed 風險接受（recipe-preview 僅執行離線 `promtool check/test rules`、不觸發 TLS/MIME 路徑；trigger：上游出 Go≥1.26.4 build 即 bump 清掉）——經 `components/recipe-preview/.trivyignore.yaml`（per-CVE + `expired_at`）+ `promtool.openvex.json`（govulncheck reachability 證據）餵入 release / nightly / PR-time Trivy gate，**hard gate（exit-code 1）不變**，僅豁免這些有證據、有期限的條目。承 [#895](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/895)。
- **da-tools dispatcher 的 missing-binary 錯誤在 Windows 重複轉義路徑反斜線（spawned-task fix）**：`_lib_godispatch._print_binary_missing`（guard / batchpr / parser 三個 dispatcher 共用）用 `{path!r}` 顯示嘗試的 binary 路徑，`repr()` 會轉義反斜線 → Windows 路徑 `C:\x\bin` 印成 `'C:\\x\\bin'`（使用者看到像重複分隔符、且與輸入不符）。Linux 路徑無反斜線 → repr 不加轉義 → CI 長綠，此 bug 為 host-only 且 pre-existing（`test_guard_dispatch.py::test_explicit_binary_not_found_returns_two` 只在 Windows host 紅）。改為手動單引號 `'{path}'`（保留路徑邊界、去掉轉義；POSIX 輸出位元相同、Windows 改顯示原始路徑，三個 dispatcher 的錯誤 UX 一併修好）。並補**跨平台**回歸守門（`tests/shared/test_lib_godispatch.py` 用字面反斜線輸入——`!r` 一旦復發連 POSIX CI 都會紅，因 repr 不分 OS 都轉義反斜線），既有 missing-path 測試也改為斷言原始路徑逐字出現、移除過時的「repr 因平台而異」註解。
- **helm mariadb-instance pin 不存在的 image tag `mariadb:11.8.1` → 全新部署 ImagePullBackOff（#896）**：`helm/mariadb-instance/values.yaml` 的 `mariadb.image` pin 了 `mariadb:11.8.1`，但該 tag 在 Docker Hub **不存在**（`docker manifest inspect` 確認；Hub 上 11.8.x patch 僅 11.8.2~11.8.8、從未發過 11.8.1——疑 chart 撰寫時 typo 或曾短暫存在後被 yank）。mariadb-instance 是平台**唯一** DB workload，在 image 未被 node 快取的全新環境 → `ImagePullBackOff`、DB 起不來（既有環境靠舊快取遮住此問題）。改 pin 現存最新 patch `mariadb:11.8.8`（含後續 CVE/bugfix）。發現自 #892 semi-sync 再評估——想 spin 平台同版實例驗 semi-sync metric 時 pull 不到。
- **maintenance-scheduler CronJob 缺 egress NetworkPolicy → 強制 netpol 下靜默失效（#862 對抗式 review 順手揪出的既有缺口）**：`monitoring` namespace 有 `default-deny-all`（Ingress+Egress），而 egress 白名單是 per-app（prometheus / alertmanager / grafana / kube-state-metrics / threshold-exporter）。maintenance-scheduler CronJob 標籤 `component: maintenance-scheduler` 不匹配任何一條 → 在**有強制** NetworkPolicy 的叢集裡，連 Alertmanager:9093（建 silence＝其核心功能）與 DNS 都到不了，每次跑都靜默 no-op。新增 `allow-maintenance-scheduler-egress`（DNS + Alertmanager:9093，mirror 同檔其他 per-app egress），並在 cron header 記下此依賴。reference manifests 以正式安全 posture 出貨（無「僅示意 / 不強制」免責），故補成「強制下亦正確」；pushgateway egress 因 cron `--pushgateway` 仍註解停用而刻意省略（啟用時再補 9091）。
- **`compile_custom_alerts.py` 對 repo 外的 `--out` 在寫檔後才崩潰（false failure）**：成功訊息用 `out_path.relative_to(repo)` 顯示相對路徑，但 `--out` 指到 repo 外（CI scratch dir、或 Windows 另一個磁碟）時 `relative_to` 會 raise `ValueError`，且發生在 **rule pack 已寫出之後** → 成功的編譯變成 traceback + 非零 exit。改為 try/except fallback（repo 內顯示相對、repo 外顯示原樣），不影響 exit code；補 write-path 回歸測試（既有 `--check` 測試在該行之前就 return、蓋不到）。
- **`sync_schema.py` 的 schema↔Go drift gate 讀錯檔而空轉，並順手清掉未接線的 `_operator` schema stub**：`extract_go_keys` 讀 `app/config.go`（但 `validReservedKeys` map 實在 `pkg/config/types.go`），regex 抓不到任何 key → 回傳空集 → 即使 `--check` 被執行，也會把每個真實 schema key 誤報成 drift（false-positive、exit 1）；而它唯一的 caller 是 `stages: [manual]` 的 `schema-check` hook、CI 從不跑它——這條 gate 因此雙重失效，`_operator` 漂移一直沒被擋（#841 review 揪出）。修好讀檔路徑（＋內容式 fallback 防未來搬移再壞）後，工具正確揪出唯一真 drift：`_operator`——一個 v2.6.0 宣告在 schema（`tenantConfig.properties` ＋ `operatorConfig` 定義）但**從未接線**的 key（無 code 讀它、無 conf.d 用它、無 test 餵它；`operatorConfig` 僅被它自己 `$ref`）。依 fail-loud（dev-rule #5，不宣傳未實作的 key）＋ 投機即刪原則，從 schema 移除 `_operator` ＋ 孤兒 `operatorConfig`（未來真做 operator per-tenant override 時，schema＋validators＋wiring 一次補齊）。新增 `tests/dx/test_sync_schema.py` 作 CI gate（Go↔schema 無 drift ＋ `extract_go_keys` 讀得到真 key），與 #841 的 Python↔Go parity 測試互補成完整雙軸；並修正 `types.go` 與 `docs/schemas/README.md` 指錯檔的 keep-in-sync 註解。
- **`_custom_alerts` 合法 reserved key 被 Python 驗證器誤報為 typo（Python↔Go 單邊漂移）**：`validate_tenant_keys` 的 Python `VALID_RESERVED_KEYS` 漏列 v2.9.0 的 `_custom_alerts`（ADR-024 能力 B, #741）→ 任何使用 Custom Alerts 的租戶（如 live `db-b`）每次 `generate_alertmanager_routes.py --validate`（及共用此 allowlist 的 `validate_config` / `threshold_recommend`）都噴 `unknown reserved key '_custom_alerts' (typo?)`。Go 側 `validReservedKeys`（`pkg/config/types.go`）在 #741 即正確加入、Python 側漏同步——唯一名義上的 gate（`sync_schema.py`）讀錯 Go 檔（找 `app/config.go`，但 map 實在 `pkg/config/types.go`）且只比 JSON schema、從不比 Python set，故 drift 無人察覺。修法：Python 補上 `_custom_alerts` 與 Go 對齊（cross-check 確認此為兩側唯一分歧）；並把這條 drift 機械化——新增 CI 跑的 Python↔Go parity 測試（直接 parse `types.go` 比對 keys + prefixes），且把既有 property 測試從寫死清單改由 live set 驅動（原清單漏列 `_custom_alerts`、謊報覆蓋）。
- **GitOps 渲染對使用 email receiver 的租戶產出 Alertmanager 自家 parser 拒收的組態（潛伏問題；#838 amtool 對抗驗證揪出）**：`generate_alertmanager_routes.py` 的 `--apply` / `--output-configmap` 對實際 `conf.d/` 的 email 路徑有兩個缺陷——(A) `email_configs[].to` 被輸出成 YAML 序列（list），但 AM 的 `to` 是純字串（string）→ `cannot unmarshal !!seq into string`；(B) email receiver 只有 `smarthost`、無 `from`，且部署用基底組態無全域 `smtp_*` → `no global SMTP from set`。兩者皆潛伏（手寫的 `configmap-alertmanager.yaml` 無 email receiver 故驗得過），但對實際 conf.d 跑 GitOps 渲染即壞。修法：(A) 在 `build_receiver_config`（builder 層、與 conf.d 寫法解耦）把序列 `to` 轉換成 AM 慣例的逗號分隔字串；(B) `_defaults.yaml` 的 email 預設為每個 receiver 補 `from`，並把 `from` 升為 email receiver 的**必填**欄位（Python `RECEIVER_TYPES` + Go `internal/guard/routing.go` 同步）——如此租戶 override / profile 等任何路徑缺 `from` 時，`--validate` / da-guard 會提早警告並跳過，而非只靠 amtool 兜底（同 `smarthost` 已是逐 receiver 必填的假設：部署無全域 `smtp_*`）。並封死根因——既有 Python 測試只把組裝後組態當 dict 驗結構、整類「AM parser 拒收」從未覆蓋 → 新增 `amtool check-config`（`prom/alertmanager` 鏡像）回歸測試（`tests/ops/test_alertmanager_amtool_validity.py`，含序列-`to`「守住守門員」＋缺-`from` 跳過反例）＋ `validate.yaml` CI 步驟（渲染 → amtool 權威驗證）。
- **Self-Service Portal 載入即 crash 而 CI smoke 長綠（latent 自 TD-030f ESM 遷移）**：三個 tab 模組在 module scope destructure `window.__portalShared`，但 bundle 的 module graph 從未 import 賦值方 `portal-shared.jsx`（frontmatter `dependencies:` build 時即被 strip、不參與載入）→ committed dist 評估期丟 TypeError、prod 空白頁。修法照 TRK-234 pattern：tabs 直接 ESM import `_common/` 模組、portal-shared 改純 named-export 共享 UI 元件（dev-rules §S6）。同時封死讓它躲過偵測的三層系統性缺口 — (1) ESM 評估期 throw 不觸發 `script.onerror` 且照常 fire `onload` → jsx-loader 補 window error listener 顯示 error banner（取代靜默空白頁）；(2) e2e smoke helper（`portal-tool-smoke.ts`，41 個 spec 共用）新增 same-origin pageerror + `#root` 非空斷言（對壞 dist 紅綠驗證：舊 helper 全綠、新 helper 必紅）；(3) `check_window_x_no_fallback.py` 原 regex 只認單一識別字、對 destructure 形式全盲 → 補 pattern + 首個 self-test。另補 `playwright.yml` paths filter 漏掉的 `docs/assets/dist/**`（CI 實際受測物）與 `jsx-loader.html`。見 [testing-playbook §JSX Dependency Loading](docs/internal/testing-playbook.md)。
- **PreToolUse session-guards 在 Windows host 靜默失效七週（#824 根治）**：hook 命令的裸 `python` 在 Windows 解析到 MS Store stub（exit 49）→ 起手式與 `sed -i` 攔截兩支 guard 自 5/11 起完全不執行；之前三週 `vscode_git_toggle` 又死於 cp950 console 印 ✅ emoji（#489 sweep 漏掃 session-guards/），telemetry 七週謊報 `partial` 且無消費者。根治四件：(1) 新 `run-hooks.sh` launcher 做**功能性**直譯器探測（存在性探測會被 stub 騙）+ 找不到直譯器時以 `additionalContext` JSON fail-loud；(2) session-guards 補 `try_utf8_stdout` 與 subprocess `encoding=` 雙層；(3) session-init 改從 hook stdin payload 取真實 `session_id`（hook env 無 `CLAUDE_SESSION_ID`，同日 session 原本共用 marker 漏 toggle）+ 刷 repo-local heartbeat；(4) 新 pre-commit gate `session-guard-liveness-check` 把「guard 死亡」從靜默變 commit 時大聲失敗。Hook 失敗策略分級（lint fail-open / guard fail-loud / security fail-closed）+「新 hook AC 須 live-fire 證據」codify 進 hook-vs-skill-coverage。
- **pr-preflight fix-push 死鎖（紅 CI 互鎖）**：`check_ci_status` 查的是 PR 遠端 head 的 CI 結果，「push 前要求 CI 綠」對 fix-push 是邏輯悖論——修復紅 CI 的 push 自己被紅 CI 擋住（#543 soft-fail 死鎖的 hard-fail 同族；#818/#819 兩度需要 owner bypass）。修正：以 `gh pr view --json headRefOid` 與本地 HEAD 比對——失敗跑在「不是本地 HEAD」的 PR head 時 hard CI failure 降級 FAIL→WARN（附 stale head 註記；push 重跑後仍紅即真失敗）；SHA 一致或不可判定一律維持 FAIL，merge 仍由 branch protection 把關。不採 `@{u}..HEAD` 計數：`checkout -b X origin/main` 未 `push -u` 的 branch shape 下 upstream 停在 main、count 恆 >0，會拔掉 merge-readiness 的 FAIL 牙齒（對抗式 review 抓出）。
- **Threshold Backtest workflow 從未被 PR 觸發（latent dead filter）**：`.github/workflows/backtest.yaml` 的 paths filter 寫 `conf.d/**`（repo-root-relative，但本 repo 的 conf.d 實際在 `components/threshold-exporter/config/conf.d/`）→ 修正路徑；連帶修復兩個觸發後仍會 silent no-op 的斷點 — script `--git-diff` 的客戶契約 pathspec 改以 `working-directory` 錨定（不動 `da-tools backtest` CLI 行為）、PR comment gate 由 `hashFiles('/tmp/...')`（workspace 外恆空）改為 step output（同 `config-diff.yaml` 模式）。
- **portal dist 一致性 hook 誤擋合法的 src 資產變更（TRK-239 樣式漏配）**：`dist-source-consistency-check` 的 source-of-rebuild 樣式只認 `tools/portal/src/**` 的 `.jsx`/`.js`，但 esbuild `bundle: true` 也會把 import 的 `.json` 資料檔打進 dist bundle → 改 `recipe-enums.json` + 重建 dist 的正當 commit 被誤擋、被迫 `BYPASS_DIST_CHECK=1`。改為認整棵 `tools/portal/src/**` 子樹，並修掉 docstring / 錯誤訊息中殘留的 TRK-242 搬遷前舊路徑（`docs/interactive/` / `docs/getting-started/`）。
- **Deny-list lint 會誤殺 compiled pack 的 forecast recipe（latent CI 衝突）**：`lint_custom_rules.py` 在 CI 遞迴掃整個 `rule-packs/`，而 policy deny `predict_linear` + `max_range_duration: 1h` — 第一個宣告 forecast（ADR-024 能力 B）的租戶 regen 後，平台 compiler 合法產出的 `rule-pack-custom-alerts.yaml` 必然紅燈（deny 理由「大範圍回溯」已被編譯期緩解：lookback 平台導出 + cold-start gate）。新增 policy `file_overrides` 逐檔豁免機制（四層護欄）：path 錨在掃描樹頂層（精確 canonical 路徑、非 suffix，擋巢狀 `rule-packs/*/rule-packs/<file>` 繞過）+ 須帶 GENERATED 檔頭（缺則 ERROR + 全檢）+ 僅 `denied_functions` / `max_range_duration` 可被放寬（白名單，列其他 key 如清空 `required_labels` 會被忽略並 ERROR）+ 其餘檢查照跑；豁免內容為 `predict_linear` 放行 + range 上限 96h（= 2×horizon enum 上限）。`DEFAULT_POLICY` 同步（`validate_config` 無 `--policy` 路徑行為一致）+ 9 個 regression 測試（真 compiler 端對端 fixture + 巢狀路徑繞過 / key 白名單 / 畸形 entry 對抗測試）。見 [custom-rule-governance.md §4.1](docs/custom-rule-governance.md)。
- **`CHANGELOG.md` 變更不觸發任何文件驗證（同類 CI-paths 漏配）**：`CHANGELOG.md` 在 mkdocs nav（渲染進站台）卻不在 `.github/workflows/docs-ci.yaml` 的 `paths` filter 內 → 只改 CHANGELOG 的 PR 不跑 Check Documentation Links / Front Matter / MkDocs Build / Line Count 等（多個是 required check）。把 `CHANGELOG.md` 補進 docs-ci paths，與既有的 `README.md` / `CLAUDE.md` 一致。
- **`jsx-loader-compat-check` hook 自 TRK-242 起從未觸發（同類 dead paths filter）**：hook 的 `files: ^docs/interactive/.*\.jsx$` 在 portal source 搬遷後永遠不匹配（該樹已無 `.jsx`）→ 改為 `^tools/portal/src/.*\.jsx$`；`playwright-e2e` manual hook 的 files filter 同步補上 `tools/portal/` 與 `docs/assets/dist`（E2E 實際載入的是 dist bundle）。連帶清掉 lint / `build.mjs` / 測試檔中殘留的 `docs/interactive/` 舊路徑與已移除的 `transformImports` 機制敘述，並把 `jsx-multi-file-pattern.md` 改寫為 ESM import 現況（frontmatter `dependencies:` + `window.__X` 降為歷史背景）。
- **`jsx-i18n-check` hook 死分支 + 已自停用的 TOOL_META 檢查退役（同批 TRK-242 殘留）**：files filter 三分支中兩個指搬遷前舊路徑（已無 `.jsx`）、lint 掃描常數同病 → `.jsx` 掃描自搬遷起 silent no-op；經對抗式 review 確認該腿本來就是 zero-coverage（portal `.jsx` 內 `window.__t(` 呼叫為 0、tool 級 finding 是 --ci 下不可見的 warning），直接移除而非修活，hook 收斂為 loader-only。TOOL_META ↔ CUSTOM_FLOW_MAP 檢查（TOOL_META 隨 TRK-230z 移除後恆 self-skip）正式退役 — registry ⊆ loader 守備由 auto hook `tool-consistency-check` 把關、flow step → dist bundle 由 `flow-e2e-check` 驗證（其 entry 自 v2.7.0 指向已搬走的 `tests/test_flows_e2e.py`，一併修復；該腳本的 component 解析同步改驗 `dist/<name>.js` 存在，對齊 TD-030z runtime 並補上 flow→404 方向的守備）。`playwright-e2e` filter 再補 E2E runtime 實際讀取的 `platform-data` / `template-data` / `recommendation-data` 資料檔。
- **`tool-consistency-check` hook 的 TRK-242/TRK-230z 殘留（.jsx-only commit 驗證 silent no-op，上兩條同病的第三案）**：`files:` filter 的 `docs/interactive/tools/*.jsx` / `getting-started/wizard.jsx` 分支指 TRK-242 搬遷前舊路徑 → 改 portal source（`tools/portal/src/**.jsx`）不觸發 hook，frontmatter `related:` 引用驗證對 .jsx-only commit 靜默跳過；filter retarget 並補進 lint 實際讀取物（`flows.json` / `dist/*.js` / `mkdocs.yml` / root README）。hook 顯示名稱與 `check_tool_meta` 的 docstring / error / fix-hint 仍指 TRK-230z 已移除的 TOOL_META — 文案改指實際驗證物 `CUSTOM_FLOW_MAP`，比對由鬆散子字串（任意 `'{key}'` 出現在 HTML 任何位置即過）改為 parse 物件實際 key set（exact match、map 區塊缺失 fail-loud）。另把 source-exists-but-never-built→runtime 404 守備升為 error 級 auto gate：`CUSTOM_FLOW_MAP` 每個 entry（bare-key 單一 component 模式，原本無任何 gate）與 flows.json 每個 flow step component（上一條剛補在 manual-stage，本次升 auto）都驗有對應 `docs/assets/dist/<name>.js` — runtime `loadDistBundle` 載入的是 bundle 而非 source。
- **manual-stage `flow-e2e-check` hook 退役併入 `tool-consistency-check`**：上面 jsx-i18n 條目剛修活它（entry 路徑 + dist 解析），但本質限制未變 — manual-stage 易被遺忘、檔內全為 `check_*` 函式 pytest 收集 0 個測試（CI 下靜默空轉），且修活後其主要守備（flows.json schema / `CUSTOM_FLOW_MAP` 覆蓋 / flow step → dist bundle）與上一條強化後的 auto-run lint 完全重複 → 刪 script + hook（manual-stage 15→14），未重複的增量併入 `lint_tool_consistency.py`：flows.json flow/step 雙語欄位（en/zh）+ `condition`/`validation` 結構（loader 對畸形輸入寬容跳過 = 缺洞 silent ship）+ Hub flow section 標記（flow-cards / analytics / builder — Hub 端原本唯一 gate）+ jsx-loader 11 個 infrastructure 標記（對抗式 review 證實 Playwright `?flow=onboarding` 只功能覆蓋 render/load 路徑，persistence key / validation gate / `?tools=` custom-flow 標記原本歸零 → 一併移入靜態 tripwire；loader 內 born-dead 的 `checkValidation` 內聯重複 — 自 v2.0.0-preview.3 進 repo 起零呼叫點、活閘門一直是 `__checkFlowGate` — 直接刪除而非入列）。同輪 review 另補 flows.json 型別護欄（flow / steps / step / `validation.warn` 非預期容器型別回報結構化 error 而非 lint traceback）並清掉恆 no-op 的 `check_related_symmetry` 死碼（迴圈體為 `pass`）。
- **jsx-loader 反射式 DOM XSS（公開 GitHub Pages 工具頁，pre-existing）**：兩個獨立的 URL→`innerHTML` 注入面。(1) `?component=` / `?flow=` 未消毒即經 `showError` 以 `innerHTML` 插入錯誤橫幅（dist 載入失敗、unknown flow、module eval-error 三路徑共用同一 sink）→ `?component=<img onerror=…>` 可執行任意 script；修法 `showError` 改 DOM 構建 + `textContent`（訊息恆為 inert text，固定返回連結保留）＋ `component` 入口加 `[A-Za-z0-9_./-]` allowlist。(2) 對抗式 review 另揪出 `?lang=` 完全未驗證即進 `__DA_LANG`，被 `renderFlowUI` 串接進 stepper/nav 的 href 屬性再 `innerHTML` → 即使搭配**合法** `?flow=onboarding`，`?lang="><img onerror=…>` 仍能沖出屬性注入（且未驗證值會持久化進 localStorage 形成 stored 變體）；修法把 `__DA_LANG` 在所有來源正規化為 `zh`/`en`（與全檔 `=== 'zh'` 比較語義等價、行為不變），同時封死 reflected 與 localStorage 兩條路徑。eval-error 換行由 `<br>` 改 `\n` + `white-space: pre-line`，banner 可見行為不變。三個 vector 各有 red/green 回歸測試於 `portal-error-boundary.spec.ts`。
- **GitHub Pages 文件站首頁（與整站根路徑）持續 404，且部署/lint 全程綠燈**：Pages 服務來源被設成 legacy `main:/docs`，但 CI 的 `mkdocs gh-deploy` 把建好的站推到 `gh-pages` 分支——兩邊完全脫節。Pages 實際是對 MkDocs **原始碼**跑 Jekyll，編不出可服務的根 `index.html`（只有 `index.md`），整站根與 `/en/` 全 404，僅零星原樣靜態檔（如 `/interactive/`）漏出 200。沒有任何 lint/CI 踩過這條 Jekyll 服務路徑（lint 驗的是 `gh-pages` 那份 MkDocs 產物），也沒有任何檢查斷言 Pages 服務來源 → 缺口對所有 linter 隱形、部署 exit 0 是假象。**修法**：部署改為 GitHub Actions 原生管線（`configure-pages` + `upload-pages-artifact` + `deploy-pages`），令**部署的產物 == lint 驗的同一份 `mkdocs build`**，結構性消除「lint 綠但服務壞」的分歧。服務來源需**一次性**切到「GitHub Actions」（Settings→Pages，或 `gh api -X PUT repos/<owner>/<repo>/pages -f build_type=workflow`——`configure-pages` 只供 build metadata、**不會**自動遷移既有 legacy branch source；對抗式 review 揪出原「每次自動釘」敘述為誤），之後此管線接管部署、可淘汰 `gh-pages` 分支。**防再犯守門** `pages-health.yaml`：(1) 斷言 Pages `build_type` 無漂移（抓「UI 改設定、不觸發任何 deploy」的隱形漂移）；(2) live HTTP 200 + 根頁內容 sentinel 檢查（含 retry 吃 CDN 傳播）；每日排程 + 部署後 `workflow_run` 雙觸發，補上 linter 原本對「實際服務路徑」與「服務來源設定」的兩處盲區。
- **recipe-builder 表單 a11y 既有債：WCAG 1.4.3 文字對比不足 + 兩處輔助修正（#901 缺席 UI 審計順手揪出）**：審計 `recipe-builder.jsx` 時發現數處把**飽和 stroke** 設計 token `--da-color-error` / `--da-color-warning`（淺色主題白底對比僅 3.76:1 / 2.15:1，低於 AA 4.5:1 門檻）直接當**文字色**——MetricField 的 bad-format／ghost 提示與主元件的 name-invalid／deprecated／EOL 註記（共 5 處）。改用同檔 `WouldFirePanel` 已正確採用的 AA `-text` 變體（`--da-color-error-text` #991b1b ≈ 8:1／`--da-color-warning-text` #92400e ≈ 6.8:1 on white），`border-l-2` 飽和邊框（非文字、不受 1.4.3 規範）保留。此即 #885 已在 would-fire verdict UI 修過、卻漏掉的同類 stroke-as-text 誤用之表單欄位殘留；深色主題兩 token 同值故僅淺色受影響、無回歸。順手補兩處：測試值 `<input>` 移除與可見 `<label htmlFor>` 衝突的冗餘 `aria-label`（讓 label 成為唯一 accessible name，WCAG 2.5.3），被 `run()` 程式化聚焦的結果區（`role="status"`）補與全站一致的可見 focus ring（WCAG 2.4.7）。新增 4 條 a11y 回歸測試（token 用對／aria-label 消失／focus ring）。`role="alert"` 巢狀於 `role="status"` 的潛在重複播報屬 borderline-by-spec，留待 NVDA／VoiceOver 實測再定，本次未動。
- **統一 Rule Pack 計數為 16，並從源頭殺掉一個凍結兩年的 stale SSOT（#922 spawned cleanup / CodeRabbit + 對抗式 self-review）**：文件對「Rule Pack 總數」長期分歧——badge、`validate_docs_versions`、`rule-packs/README` 與大部分文件用 **16**，但 `platform-data.json` + 計數陷阱 doc（`docs/design/rule-packs.md`）+ `generate_platform_data.py` 用 **15**；另有英文 laggards 與 `architecture-and-design` / `benchmarks` / `doc-map` / `glossary` 殘留 15（部分中英不一致、部分雙邊皆 stale）。**根因（git blame）**：「15」來自 `PACK_ORDER` 這個自 `v2.0.0-preview.3` 凍結的 hardcode 清單，`rule-pack-liveness`（#869）加入後從未補進去——是維護漏更、非「liveness 不算 pack」的設計決策（計數陷阱 doc 同代亦 stale：宣稱「14 個 YAML」但實際 16）。**定 canonical = 16**（liveness 計入；只有編譯產生的 `custom-alerts` 不列入標準計數）。修正：(1) **從源頭殺 stale 15**——`PACK_ORDER += liveness`（+ 補 label/category/exporter/metrics metadata）→ `platform-data.json` regen 為 16 packs；重寫計數陷阱 doc（15→16、修「14 YAML」說明、把 stale 的 §3.1 逐包表重建為當前 16 包 / 148 rec / 135 alert，中英雙語）。(2) bilingual drift（英文 laggards）+ 雙邊皆 stale 的 `architecture-and-design` / `benchmarks` / `doc-map`（中英）→ 16。(3) `glossary`「Currently 15 packs: [含不存在的 HAProxy/Node/Blackbox、漏 clickhouse/db2/oracle 的錯清單]」→ 16 + 正確 16 包清單（中英）。(4) `operator-manifests` 計數 14→16（`operator_generate.py` glob 全部 16 個 `rule-pack-*.yaml`）。(5) ADR-005 code-example「other N rule packs」修為 14（範例顯示 2 個 + 14 = 16，先前被誤傳）。**結果**：badge / docs / `platform-data.json` / `validate` / 計數陷阱 doc 全 = 16；bilingual-numbers 的 Rule-Pack-count warning 清空（殘餘為無關的 Alert-rule-count 既有漂移）。`doc-map` 描述源在 `generate_doc_map.py` `EXTRA_ENTRIES`，改源後 `--generate --lang all` regen。
- **forge（GitHub／GitLab）client 共用 `http.DefaultTransport` → nightly race detector 偽陽性（#932）**：兩個 client 的 `&http.Client{Timeout: 30s}` 未設 `Transport`，回退到行程級單例 `http.DefaultTransport`。Go 的 `httptest.Server.Close()` 會對該單例呼叫 `CloseIdleConnections()`——在 `go test -race -count=10` 下，套件內任一平行子測試 `defer srv.Close()` 觸發，即可清掉 `TestDeleteBranch` 正在重用的 idle 連線 → 偶發 `transport connection broken: CloseIdleConnections called`、整包 `internal/gitlab` 紅燈。**經查並非 data race**（detector 零 `DATA RACE` 警告）；nightly workflow 對 `-race` 步驟任何失敗一律開「race flake」issue 故誤標。修法：新增 `platform.NewHTTPClient(timeout)`（`Clone()` 一份隔離 transport，與既有 `JSONRoundTrip` 同層集中傳輸策略）。同輪掃出 federation `fedpolicy`（`admission.go` / `discovery.go`，各自 Prometheus 查詢 client）為同型潛在 flake（同樣 nil-Transport + 平行 httptest，discovery_test 起 10 個 server，雖尚未實際紅但結構上同源）→ 一併改用。四個 client（2 forge + 2 fedpolicy）各自擁有連線池，跨平行測試不再互相清池；順帶讓連線重用策略可獨立調校。新增 `platform.NewHTTPClient` 隔離性回歸守門（還原為共用 DefaultTransport 即紅）。並 codify 進 CI（外審 Gemini 建議）：golangci `forbidigo` 擋掉 `http.DefaultClient` / `http.DefaultTransport`（`transport.go` clone 點豁免）/ `http.Get|Post|Head|PostForm` 等共用池入口，逼新程式走 `NewHTTPClient`；forbidigo 比對 selector，無法表達「`&http.Client{}` 裸 literal 帶 nil Transport」此一 #932 確切形狀，該 case 仍由上述 unit test + review 守門。
- **`MariaDBHighCPU` 對 gauge 套 `rate()` → 告警永不觸發＋文字誤導（#942；由 #895 PR-B＝#941〔promtool 2.53→3.12 升級〕對抗式 review 揪出）**：#941 只做最小 `%.0f` stopgap（吸收 Prometheus 3.0 left-open range-selector 浮點位移），語意根因留 #942；本修把 metric 改 gauge 後浮點位移問題從根消失、#941 的 mariadb stopgap 即被取代。`tenant:mysql_cpu_usage:rate5m = sum by(tenant)(rate(mysql_global_status_threads_running[5m]))` 對一個 **gauge**（`threads_running`＝並發執行中執行緒數、瞬時值）套 `rate()`——gauge 下降被當成 counter reset → 收斂到 ~0，故預設閾值 75/80 **永不觸發**；annotation 又把這個 per-second rate 標成「running threads」(整數計數) 誤導 operator。連 promtool fixture 都露餡（誤稱 threads_running 為 counter、餵單調 ramp＋假閾值 0.5 才湊出 rate≈1.0）。**修法（語意對齊、lockstep）**：依 USE-method（threads_running 是**飽和**訊號、非主機 CPU%）＋ codebase 自身 SSOT（`metric-dictionary.yaml` 已定 threads_running→`mysql_cpu`→`MariaDBHighCPU`），recording rule 改 gauge 聚合**並 1m 平滑** `max by(tenant)(avg_over_time(mysql_global_status_threads_running[1m]))`、更名 `tenant:mysql_threads_running:avg1m`（`max by(tenant)` 保留 topology-aware＝抓最忙 primary、不被 idle replica 稀釋；`avg_over_time[1m]` 消除 spiky-gauge 的 `for` **reset-trap**——raw gauge＋`for:` 在震盪過載下單次掉閾值即重置計時器 →**永不觸發**，由 Gemini 對抗 review 揪出、promtool 實證並加 regression test）；annotation 留「running threads」(現為真)＋`%.0f`＋`(1m avg)` 標註、`for:30s`（平滑後不需長 for:）；rule-pack 註解＋ALERT-REFERENCE 點明「飽和 proxy、非主機 CPU%」。**保留** `MariaDBHighCPU` 告警名與 `mysql_cpu` 閾值 key（blast radius＋與 metric-dictionary 對齊）。生產預設 80 不動（gauge 後 80 並發 running threads 為合理飽和門檻、可真觸發）、try-local `db-demo` seed 75→40（dormant 範例，刻意不推 threads_running 以維持單一 headline 紅燈）；`scaffold_tenant.py` 的 stale `unit: threads/s`→`threads`／desc 一併修。連動 regen 三副本 cascade（configmap／operator-manifest）＋ALERT-REFERENCE 中英＋platform-data＋observed-map；promtool ×3＋Go contract＋snapshot＋4 drift gate 全綠。**Defer-with-trigger（→ #944）**：全面正名（alert 名＋閾值 key→threads_running）、閾值 80→~30/50（Nichter 分級 30 High/50 Overloaded、PMM 30、pt-osc 25）、**加真 CPU% 告警**（threads_running ≠ CPU 雙向脫鉤：漏單一重查詢釘核、誤報 lock-wait）留作 trigger（真 CPU% metric 上線／operator 反映混淆／客戶 RFP）。
- **`MariaDBHighCPU`→`MariaDBHighThreadsRunning` 正名 ＋ 飽和閾值校準 80/120→30/50 ＋ CPU 盲區哨兵 `ContainerCpuSignalAbsent`（#944，承 #942 defer 的 PR-1；分階段：threshold key 留 PR-2）**：落地 #942 記下三項 defer 中可低風險先做者。**(1) 正名**——`MariaDBHighCPU`/`Critical` → `MariaDBHighThreadsRunning`/`Critical`：此告警**自始**量的就是 `mysql_global_status_threads_running`（並發執行緒**飽和** gauge）、從不是主機 CPU%；git blame 考據，misnomer 源於 v1.0 demo-scaffold（`cd7916e6` 對 gauge 套 `rate()`），**非**抄 Percona/PMM golden rule（同檔「Percona-inspired」標記獨缺此條、PMM canonical 是 `threads_running>30 FOR 1m` raw gauge；且本 repo kubernetes pack 的 container CPU% 一直用對 `cAdvisor÷limit`，反證作者懂正解、mariadb 那條純屬早期 proxy 失誤）。annotation 改誠實標「running-thread saturation，NOT host CPU%」並 cross-link 真 CPU% 的 `PodContainerHighCPU`。routing **不受影響**（severity-dedup 走 `metric_group`、非 alertname）→ 乾淨改名、無雙發 alias、無告警疲勞。**(2) 閾值校準（⚠ 客戶面行為變更）**——平台**預設** 80→30（warning）/120→50（critical），對齊業界（PMM pt-osc throttle 25 / critical-load 50、Nichter 30 High/50 Overloaded）；**僅動平台預設、既有租戶 override 全保留**。連帶：複合告警 `MariaDBSystemBottleneck` 的 CPU 臂改用同一校準閾值（30 起算，原 80）→ 更早偵測多資源瓶頸（仍由 connections **AND** 把關、不單臂觸發）。**(3) CPU 盲區哨兵 `ContainerCpuSignalAbsent`（kubernetes pack，承 #942「加真 CPU%」defer 的 detect 半）**——threads_running（飽和）與 container CPU%（使用率）**雙向脫鉤**（單一重查詢可低 threads_running／高 CPU、lock-wait 可高 threads_running／正常 CPU）；K8s 租戶的真 CPU% 早由 `PodContainerHighCPU` 覆蓋，但它除以 `kube_pod_container_resource_limits`、對**沒設 CPU limit** 的 pod 算不出。本哨兵在 db pod 為 **CPU BestEffort（無 limit 也無 request）**時 per-tenant fail-loud——**刻意不對「只是沒設 limit」誤報**（DB 常刻意不設 limit 避 CFS throttle），只逮 BestEffort 這個共識性 poor-QoS；tenant-level any-bound=covered 防洪、maintenance opt-out、dual-perspective（租戶白話「設 CPU requests/limits」＋ SRE `platform_summary`）。rule-pack alert 數 136→137。**Cascade**：3-copy（configmap/operator）＋ALERT-REFERENCE 中英＋platform-data＋rule-pack-stats＋README badge；promtool ×2 ＋新哨兵 promtool（8 案：besteffort fire／request·limit covered／多租戶 masking／no-pods／maintenance／non-db scope／tenant-level 防洪）＋snapshot＋全 drift gate 綠。**Defer → #944 PR-2（key-rename）**：threshold key `mysql_cpu`→`mysql_threads_running`（含 threshold-exporter dual-key 相容 shim）＋`metric_group`＋recording-rule 更名、portal JSX demo＋dist 重建，以及 limitless pod 的**真 CPU% 量測**（node-allocatable／絕對核數，需新 threshold key 故與 key-rename 同 PR 設計）。承 [issue #944](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/944)。
- **`rule-pack-mariadb-cpu` 的 db-osc 平滑回歸測試間歇性弄紅 required「Lint Rule Packs」CI（#943 既有 flake，承上條）**：#943 為 `MariaDBHighCPU` 新增的 smoothing regression（證明告警讀 1m-avg 而非 raw gauge）以 `alert_rule_test` 斷言 `description: "100 …"`。但 promtool 在同一 eval tick 內以**非確定順序**評估 recording group（`mariadb-normalization`）與 alert group（`mariadb-alerts`）→ 告警讀到的 `tenant:mysql_threads_running:avg1m` 在 eval tick（t=360→100）與前一 tick（t=345→116 on 2.53.2／115 on CI 3.x）間擺盪；兩值皆 >80 故**恆觸發**，但精確值斷言淪為擲銅板（本機 ~14/30、CI 連紅 2 次）。#943 已穩住 left-open/closed 視窗軸（idx-20 stabiliser），卻漏了 eval-tick-vs-prev-tick 這軸；且 raw dip 壓在 eval 樣本上時，確定的精確值數學上**不可能**（四視窗全等 ⇒ 無 dip）。改以**值無關**的 `promql_expr_test` 取代 flaky `alert_rule_test`：(1) raw gauge `@eval == 40`（瞬時低於閾值）＋(2) `avg1m > bool 80 == 1`（平滑值仍跨閾值，raw-revert 讀 40→0 接住回歸）＋(3) `count(ALERTS{…firing}) == 1`（端對端確認 `MariaDBHighCPU` 觸發；`count()` 收掉 promtool↔vmalert 的 `alertgroup` label 差異而後端可攜，回應 CodeRabbit review）——promtool 2.53.2 與 CI 的 3.12.0 各數十次 0 fail、vmalert-tool 亦綠；db-a/db-b 仍以穩定（非 dip）輸入釘住精確 rendered 值。（`flaky-tests.yaml`／TRK-010 retry registry 僅覆蓋 Go test、不含 promtool job，故走根因修復而非註冊重試。）承 [#943](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/943)。

## [v2.9.0] — 租戶自助告警 (Custom Alerts) + 租戶聯邦 + 寫入平面韌性 (2026-06-06)

v2.9.0 把平台從「平台 authored 告警」推進為**租戶自助的宣告式告警引擎**，並讓 v2.8.0 outline 的多項深水區能力落地。戰略主題是**從第一個客戶的實際使用去 harden — reactive > predictive**：三條主線是 (1) **Custom Alerts**（租戶用平台 authored 的參數化 recipe 自訂告警、**不寫 PromQL**，ADR-024 能力 B）、(2) **Tenant Federation**（ADR-020 token endpoint + gateway + policy + offboarding 從 outline 走到可部署）、(3) **寫入平面 single-writer 韌性**（ADR-023 把 GitOps 寫入路徑的幽靈寫者 / 孤兒寫入 / rate-limit / 優雅關機系統性補強）。同時平台日誌彙整、IaC SAST 四層、與一輪 silent-failure 反應式硬化（含一個燒了兩個月的 P0 prod drift）一併收斂。

### Highlights — 5 條

- **Custom Alerts — 租戶自助宣告式告警** — 租戶選平台 authored 的 6 種參數化 recipe（`threshold`/`rate`/`ratio`/`absence`/`p99_latency`/`forecast`）、填參數即得合法告警，**無需寫 PromQL**。向量化編譯器把同指標的跨租戶扇出收斂（規則數 = shape 數、非 ×租戶數），portal `RecipeBuilder` + tenant-manager modal 一鍵 commit 回 GitOps；page/silent 路由復用既有 Sentinel+Inhibit 三態。詳 [ADR-024 §Custom Alerts](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Tenant Federation 走到可部署（ADR-020）** — token endpoint + read-path proxy / API gateway（Envoy）Helm chart + 2-tier policy schema & admission validator + 簽章金鑰輪替 + offboarding（殭屍憑證偵測 + runbook）+ 全域 kill switch + 稽核日誌；請求路徑 E2E 覆蓋。
- **寫入平面 single-writer 韌性（ADR-023）** — `strategy: Recreate` 消滅滾動更新幽靈寫者、鎖內 fetch 新鮮 base 杜絕跨-merge silent data loss、load-shedding + ctx-aware 排隊杜絕孤兒寫入、circuit breaker 認得 GitHub secondary-rate-limit 403、SIGTERM 先廣播 `server_shutdown` 再優雅關 SSE。
- **平台日誌彙整（#539）** — Vector + VictoriaLogs 三階段（Phase 1 採集 → Phase 2 federation chargeback aggregation → Phase 3 SIEM fan-out compliance branch）+ #566 多批 hardening。
- **Reactive silent-failure 硬化** — 修一個 11/14 rule pack 閾值在 prod 靜默失效的 P0（exporter strip metric prefix）+ 加 AST contract test 永久封死迴音壁；連帶修 HA 多副本 `sum` 翻倍告警失效（~2 個月無聲 drift）與 cardinality 截斷非確定性 flapping。

### Custom Alerts（#741，ADR-024 能力 B，epic 收尾）

- **向量化編譯器核心（S1+S2）**：recipe 庫 + conf.d `_custom_alerts` 宣告語法 + `compile_custom_alerts.py`——依 shape signature 分組、每 shape 產一條 `app_metric > on(tenant) group_left(...) user_threshold{recipe_id}` 規則（規則數 = shape 數、跨租戶去重；惟不同 metric 必生不同規則，故隨自訂告警**種類**線性增長、不享 rule-pack 對 [benchmarks.md §2](docs/benchmarks.md) 的 O(M) 保證）。對抗 review 收斂的防禦：metric 嚴格 regex + 安全 selector 組裝（杜絕 PromQL 注入）、ratio 除零護欄、`mode`(page/silent) 搭資料平面 `group_left` 不入 shape signature。
- **資料平面 + 部署（S3a/S3b）**：threshold-exporter emit `user_threshold{component="custom", recipe_id, name, mode}`（`recipe_id` slug 為 Go↔Python 跨語言 golden-vector 契約，malformed 宣告 fail-loud → `da_custom_alert_parse_errors`）；live conf.d 源向量化 pack 正式 commit + 三副本硬閘門部署，custom 告警可在 prod fire。
- **第 6 個 recipe `forecast`（趨勢/耗盡預測）**：線性預測 gauge/餘量比例越界（磁碟/記憶體耗盡）；租戶只填 `horizon`（enum）、平台推導 lookback；cold-start 資料量 gate + `for` sustain 防 false-positive。
- **護欄與路由（S4/S5/S7/S8）**：per-tenant `max_custom_recipes` cap（只計 own、fail-loud；封自訂告警規則數的線性增長——全域 rule-count budget 規劃中、與 rule-eval-duration benchmark 一同 defer）；tenant-api in-process Go shift-left preflight（壞 recipe 絕不進 repo，跨語言驗證契約 fixture）；page 走專屬 `custom-alerts-firehose` route（居首 + `continue:false`，gate 正確性必需），silent 復用 ADR-003 Sentinel+Inhibit 三態（`CustomRecipeSilent` 全域 sentinel + AM inhibit）。
- **Portal 自助 UX（S6a/S6b）**：唯讀 metric discovery endpoint（24h lookback，server-side `tenant` label 強制鎖 + charset 白名單防注入）；免 PromQL 的 `RecipeBuilder` 表單（enum derive 自 schema + drift-guard）；tenant-manager Custom Alerts modal 一鍵 commit 回 GitOps（JIT fresh fetch OCC + dirty-guard + 409 非破壞性，後端擁 YAML round-trip、前端只送 JSON）。
- **GA-polish — recipe 生命週期治理（ADR-024 §8）**：platform-authored recipe 加 `status: [active|deprecated|eol]`（SSOT = `shape.py::RECIPE_STATUS`，6 份治理契約鏡射 + drift guard）；deprecated/eol 既有宣告**照常編譯絕不靜默丟告警**；tenant-api eol-expansion 寫入閘採 **inclusive「B2-寬」**（只拒擴張使用、放行改參數/rename/既有續存，不連坐 → 避免救火時被舊 eol recipe outage-hostage）；portal deprecated/eol 視覺標記 + 燃盡圖 info-metric `custom_recipe_info`。
- **盲點 follow-up**：blast-radius / config-diff 認得 `_custom_alerts` 為 alerting 變更（升 Tier A + recipe-level diff + silencing 高亮）；`describe_tenant` 對 `_custom_alerts` 改走編譯器 UNION 繼承解析（修 override 租戶漏報 P0 [#772](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/772)）；custom-alert `for` 納入 recipe_id slug（修向量化靜默覆蓋 [#751](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/751)）。

### Version-Aware Threshold（#423，ADR-024 能力 A）

- **宣告式 cutover（ADR-024 核心）**：透過既有 dimensional `version` label 達成版本感知閾值——`rule-pack-kubernetes` pilot 落地 + da-guard 雙語 `version` label 驗證；既有未寫 `versioned:` 租戶行為 100% 等價（AC-1）。使用攻略見 [version-aware-thresholds 場景](docs/scenarios/version-aware-thresholds.md)。
- **PR3-pre 深挖出的三個 prod 靜默缺陷**：4 個 rule pack 的 `user_threshold` 聚合在 source/operator 副本誤用 `sum`、HA 多副本下閾值翻倍告警失效（~2 個月無聲 drift；新增 HA-max lint 防復發）；容器告警 metadata enrichment 由 inner-join 改 left-outer-join（消除新租戶 onboarding 真空期盲區）；per-tenant cardinality 截斷改確定性（消除 over-cap 租戶 alert flapping，AC-7）。
- **寫入路徑驗證對稱化**：tenant-only PUT body 不再被誤擋（ADR-024 PR4 探索發現）。

### Tenant Federation（ADR-020，outline → 可部署）

- **授權平面**：tenant federation token endpoint（每租戶 16-上限）+ read-path proxy / API gateway（Envoy）Helm chart + tenant-api chart federation 接線 + 簽章金鑰 bootstrap / 輪替。
- **政策與准入**：2-tier policy schema + endpoint + admission validator + storage blast-radius flags + 稽核日誌 / 異常 metric + 全域緊急斷路器（kill switch）。
- **生命週期與驗證**：offboarding 完整性（殭屍憑證偵測 + runbook）+ 請求路徑 E2E + 使用者文件（ADR-020 IV-2h）。
- **安全 hardening fixes**：proxy 注入 `tenant_id` vs 平台 data layer `tenant` 標籤名地雷修正；token 16-上限 TOCTOU 競態；gateway 在 prom-label-proxy 模式拒 `remote_read`；`/api/v1/read` 403 guard 的 non-canonical path / `%2F` escaped-slash 繞過；稽核 metric `tenant_federation_requests_total` 生產環境從未產出。

### 寫入平面韌性（ADR-023，tenant-api GitOps 寫入路徑）

- **single-writer invariant**：Deployment 補 `strategy: Recreate` 消滅滾動更新交疊期幽靈副本多寫者（#677）；PR 寫入在鎖內 `git fetch` 新鮮 base + 從 `origin/<base>` 開分支，杜絕共享檔跨-merge silent data loss（#671）。
- **過載與孤兒寫入**：寫入平面 load-shedding（token 模型 + `maxWriteAdmit` → 503 + `Retry-After`）+ context 綁排隊階段，client 斷線立即釋放永不執行（#673）；circuit breaker 認得 GitHub secondary-rate-limit 403 / GitLab 429 + 尊重 `Retry-After`（#672）。
- **優雅關機與連線生命週期**：SIGTERM 先廣播 `server_shutdown`（帶 reconnect hint）再關 SSE，消除 15s 關機卡死 + 生硬斷線（#675）；SSE `/api/v1/events` heartbeat + per-write deadline 修 slow-client goroutine leak（#143）；部署後 SSE 重連觀測護欄（#740）。
- **gitops Writer 硬化**：git CLI 逾時（卡住的 `git push` 凍結全租戶寫入，#630）+ SIGKILL 殘鎖自癒 + de-relativize checkout（#638）+ 本地 feature branch ref 慢洩漏（#641）+ PR-mode polling-staleness false 409（#644）。
- **silent-failure 可觀測性**：`da_config_parse_failure_total` / `ConfigReloadStuck` phantom-state alert（#631/#643/#647）+ runtime per-tenant cardinality 截斷可觀測性（#652）。
- **deploy-time 靜態強制（#786，TRK-317）**：ADR-023 單寫者不變式新增 Helm guard + static lint，把「writer `replicaCount` 必須 =1 / `Recreate` strategy」從 runtime 慣例升為**部署時機械強制**（layer-1/2 守門）；tenant-api PR-mode 錯誤處理 + 嚴格 env parsing 硬化（#795/#798）。

### 平台日誌彙整（#539）

- **三階段**：Phase 1 Vector + VictoriaLogs 採集；Phase 2 federation chargeback aggregation（#552）；Phase 3 SIEM fan-out compliance branch。
- **Hardening（#566 batch A–E）**：CSV tamper-evident、compliance-strict path、egress allowlist gate + GitOps-write-boundary doc、extraEnv 平台慣例對齊；含 runtime smoke-test 抓到的 chart bug 與預設值缺口。

### Container/k8s IaC SAST 四層防線（#448）

- **四層**：L1 Dockerfile（hadolint）/ L2 Helm template（kube-linter）/ L3 Helm values secret-shape（Vibe wrapper）/ L4 raw k8s manifest；hybrid policy（open-source engine + Vibe wrapper 取代 DIY-only）。Critical → BLOCK、High → 中央 EXEMPTIONS 列管。基線見 [iac-lint-baseline.md](docs/internal/iac-lint-baseline.md)。
- **Python SAST baseline（#455）**：bandit + dev-rules Rule #5 code-driven enforcement。

### DX / 工具鏈 / Skill 體系

- **Skill 體系（#570 epic）**：新增本地 skill `vibe-release` / `vibe-brainstorm` / `vibe-subagent-review`；hook↔skill 邊界稽核矩陣；CLAUDE.md AI-context 強化（TRK-301~303）；季度 rule-corpus drift 稽核（`audit_rules_drift.py`）。
- **da-tools UX consistency（#452）**：exit-code 0/1/2 SSOT 統一 + `--json` + packaging；新增 `da-tools runtime-audit`（Git rule-packs ↔ Prometheus runtime 唯讀對帳，RFC #747）。
- **Release / CI 安全網**：`#474` PR-time component Docker build + Trivy gate（補 trigger-asymmetry）；`pr-preflight` commit-scope 檢查根除 first-CI-red deadlock；CI `actions/checkout@v4→v6`。
- **Bench gate 根治**：交錯執行 + control canary（Phase 1）→ 降 informational + nightly sustained-trend watchdog（Phase 2）；修非確定性 `MB-sys`/`MB-heap` false-RED + benchstat 格式漂移 silent-pass + watchdog creep 改錨定中位數 / 狀態化 issue lifecycle（#608/#611/#702/#754）。
- **退役 / 收斂**：ADR-024 精煉重寫 + 實作落地對帳（739 → ~225 行、lead-with-decision、護欄三件組標明已實作 vs deferred）；`generate_cheat_sheet.py` 正名手工 SoT；threshold rule-pack contract test 補 orphan-record gate（#734）；SSOT 語言評估報告移出 repo → closed issue #145。
- **內部品質與文件收斂**：多 component god-function 拆解 + behavior-preserving 結構重構（exporter / tenant-api / portal / da-tools）+ testing-suite refactor（idiom / helper / coverage / dedup）；da-tools entrypoint 跨平台健壯性重構（7-cycle loop）+ exporter 增量 reload fast-path 修「同次 reload 內 tenant 跨檔搬移悄悄消失」（#790）；4 component README/QUICKSTART 刷新對齊現況；**文件認知門檻 refactor（#805）**——碼名清理 + 受眾/階段導引 + 導覽索引 + glossary signposts；e2e regression-naming SOP 對齊 TRK-1NN（#804）。

### try-local onboarding & Portal（#449 / #444）

- **try-local 一鍵體驗 stack**：#449 epic 5 PR（#464/#465/#466/#467 + multi-arch #463）——`docker compose up` 起 showcase stack、~1min 看到真實 critical 告警紅燈；onboarding UX polish + component 文件圖連結性（#626/#633）。
- **多架構 image**：release 4 個 component image 發 `linux/amd64,linux/arm64`（da-tools per-arch build），解鎖 Mac M-series 原生 arm64（#463）。
- **Portal design-token 遷移（#444 epic 收尾）**：硬編碼 hex/px → design token 全遷移 + token gate 根治 false-positive；portal dist bundle repo-wide 重建 + CI 同步閘門。
- **real-forge E2E（#616/#615/#636）**：tenant-api GitLab CE + GitHub >100-PR 真分頁端到端；⛔ 永不 `pull_request_target`。

### Benchmark（1000 tenants）

- **Scale 無回歸**：核心 config-load 路徑經本版重構（#789 loader 去重 / #791 incremental fast-path）後，**同機控制證實效能與 v2.8.0 持平**（1000-tenant 冷啟動載入：v2.8.0 172 ms vs v2.9.0 169 ms，同 host/同參數）。reference SLO 維持：**冷啟動 112 ms / 熱重載 1.3 ms @ 1000 tenants**（量測於 v2.8.0 reference 環境；本版量測機慢約 1.5×，故以同機相對比對佐證無回歸而非重列絕對值）。
- **記憶體 readiness**：60 分鐘 / 15s 高頻重載 soak（239 reloads / 120 polls）下 **RSS 持平**（`sys_bytes` +0.0%、44 MiB）、**無記憶體洩漏**（`heap_objects` −1.0%、goroutine 持平）。
- **Custom Alerts 成本誠實**：向量化編譯使「新增一種自訂告警 = **1 條跨租戶共用規則**」（規則數 = shape 數），而非每租戶 N 條；隨自訂告警**種類**增長、由 per-tenant cap 封頂（**非** rule-pack 對租戶數的 O(M) 保證）。

詳 [benchmarks.md](docs/benchmarks.md)。

### ADR 新增（ADR-021 / 022 / 023 / 024，4 條；ADR-020 outline → 實作）

- **ADR-021** Tenant Log Query Federation：Authorization-Plane-Only, Ingestion-Decoupled（TRK-316）
- **ADR-022** Dev Auth Bypass — 四層 containment（try-local `--dev-bypass-auth` 安全姿態）
- **ADR-023** Write-Plane Single-Writer Invariant：GitOps 寫入路徑韌性的設計依據
- **ADR-024** Version-Aware Threshold via Dimensional Label：宣告式 dimensional 告警引擎 + 兩個能力（A version-aware / B custom alerts）

### Breaking changes / Upgrade notes

- **tenant-api Deployment `strategy: Recreate`（#677）**：滾動更新改為先停後起，部署窗會**硬斷所有 `GET /api/v1/events` SSE 連線**（單次重連預期且自癒，client 收 `server_shutdown` + reconnect hint）。升前讀 [tenant-api-hardening.md §5.6](docs/api/tenant-api-hardening.md)。
- **component chart image 預設改由 `Chart.appVersion` 推導（#682）**：根除 image-tag 漂移；若有自訂 `image.tag` override 不受影響，未 override 者升版後 image 跟隨 chart appVersion。
- **Custom Alerts 寫入 eol-expansion 閘**：tenant-api `PUT .../custom-alerts` 對 eol recipe 拒絕**擴張**使用（新增/加量回 400 + Violations）；改參數 / rename / 既有續存放行。
- **新 env / 旋鈕**（皆有預設、不強制）：`TA_WRITE_QUEUE_DEPTH`（寫入排隊深度）、`TA_GIT_FETCH_TIMEOUT`（鎖內 fetch 逾時）、threshold-exporter `--free-os-mem-after-reload`（#459 記憶體 lever）、`networkPolicy.egress`（Custom Alerts discovery opt-in egress）。

---

## [v2.8.1] — secret-scan 四層防線 + Planning SSOT 自動化 + DX 工具鏈收斂 (2026-05-16)

v2.8.1 是 v2.8.0 之後的 **interim DX / 內部工具 release**（平台 tag only — component binary 與 v2.8.0 相同）。三條主線把原本「靠 reviewer / 靠人自覺」的開發紀律機械化：(1) secret leak 防線從單層補成 **L0–L3 四層**（含不可繞的 server-side gate）；(2) Planning SSOT（ADR-019）落地 **Layer 2/3 自動化**；(3) stdout-encoding / CI 排障 / trailer 等 DX 工具鏈收斂。

### Highlights — 3 條

- **Secret-scan 四層防線（#445）** — L0 GitHub push-protection → L1 pre-commit hook → **L2 不可繞 server-side workflow**（trufflehog → SARIF → Code Scanning）→ L3 release-time image digest 驗證；配套 ASSUME-COMPROMISE / ROTATE-FIRST incident SOP。
- **Planning SSOT 自動化（ADR-019 Layer 2/3，#378/#379）** — `TECH-DEBT`/`TD`/`HA`/`REG` 四 namespace 統一為 `TRK-NNN`；planning-index 與 status-sync 成 CI drift gate。
- **DX 工具鏈收斂** — stdout-encoding helper 全面遷移（消滅 Windows cp950 emoji crash）、PR CI 自動排障 CLI、Self-Review-Pass-2 trailer gate、MS Store Python stub 防呆。

### Secret-scan 四層防線（#445，五個 AC 全數完成）

- **L1 pre-commit `secrets-scan-staged`**：staged files 跑 trufflehog（offline），`--no-verify` 明禁 + binary 缺失 soft-skip。
- **L2 server-side `secret-scan.yml`**（不可繞）：PR diff-only（merge-base 起點，避 Git Ancestry Trap）+ nightly full-history；自寫 `trufflehog_to_sarif.py` 持 verified→block / unverified→warn policy；forked-PR 走 artifact fallback，⛔ 不用 `pull_request_target`。
- **L3 release-time digest 驗證**：`release.yaml` 4 個 job push 後 `skopeo inspect` 驗 digest（two-tag semantic：永遠驗 `:vVERSION`，chart appVersion 不符時加驗），寫進 job summary。
- **Remediation SOP**：`secret-leak-remediation-sop.md` 定義不等 approve 直接 rotate 的 5-step response + 反 SOP 表；Gemini 外審補 re-infection vector / JWT mass-logout / build-artifact poisoning 盲區。

### Planning SSOT 自動化（ADR-019 Layer 2/3）

- **`TRK-` namespace 統一（#379）**：四個分散 namespace 機械替換（含 ~150 非-md 檔 328 處 code-side sweep）+ `planning-id-mapping.md` 對映表。
- **planning-index + status-sync 自動化（#379 chunk 2a/2b）**：4-source 發現帶 `tracking_kind:` 的 entry → 渲染 `planning-index.md` + drift gate；PR commit `Resolves` trailer 驗 entry 存在 / `status: done` / `pr_ref` 對齊（ADR-019 Layer 3）。
- **frontmatter migration（#379 chunk 3）**：ADR-001~018 + dx-tooling-backlog 加 ADR-019 frontmatter spec；planning-index 擴至 39 entries。
- **Hub 索引自動化（#378）**：ADR 索引 generator + Migration Guide hub slim（−56%，雙語同步）+ pre-commit drift gate。

### DX 工具鏈收斂

- **stdout-encoding 全面遷移（#489 Phase A/B）**：`_lib_compat.try_utf8_stdout()` 取代 module-level `io.TextIOWrapper` 副作用-on-import；77 工具 + `pr_preflight` 遷移，repo 內 legacy pattern 完全退役。
- **`diag_pr_ci.py` PR CI 自動排障 CLI（#446）**：`make diag-pr` 把失敗 check 摘要成 markdown/JSON（4-endpoint 串接走 `gh api`，3 個 distinct prereq exit code）。
- **Self-Review-Pass-2 trailer CI gate（#454）**：git native trailer parser 掃 `<base>..HEAD`，軟性失敗（adoption 穩定後轉硬性），為 #453 mutmut 的 enabling step。
- **Windows MS Store Python stub 防呆（#436）**：commit-msg + pr_preflight 改 `py -3` candidate probe / `sys.executable`，繞過 exit-49 placeholder。
- **退役 phantom no-op lint `check-techdebt-drift`**：資料源已 phantom-delete，職責移交 `check_planning_status_sync.py`。

---

## [v2.8.0] — 客戶導入管線 + 千租戶 Scale 驗證 + 自動化收斂 (2026-05-12)

v2.8.0 把 v2.7.0 的 Scale Foundation I（`conf.d/` 階層 + dual-hash 熱重載）推進為**可導入既有 kube-prometheus 客戶**的完整 pipeline：4 條 Go binary 把客戶 PromRule corpus 自動化轉成 Profile-as-Directory-Default `conf.d/` 樹，三條交付路徑（Docker / static binary 6-arch / air-gapped tar）+ supply-chain provenance（cosign keyless + SBOM）滿足全光譜部署環境。Tenant Manager 在 1000+ 租戶規模上以 server-side search + virtualization 維持 p99 < 200 ms。56 個 pre-commit hook（39 auto + 14 manual + 3 pre-push）把開發規範從 reviewer convention 升級為 mechanical net。

### Highlights — 5 條

- **客戶導入自動化** — 4 隻新 Go CLI（`da-parser` / `da-tools profile build` / `da-batchpr` / `da-guard`）把 kube-prometheus 客戶現有 `PrometheusRule` corpus 導入到 `conf.d/` Profile-as-Directory 架構；1000 租戶導入從一週縮到一天。安裝見 [Migration Toolkit Installation](docs/migration-toolkit-installation.md)。
- **Tenant Manager 邁入 1000+ 租戶規模** — UI 直打 `/api/v1/tenants/search`（伺服端 search / pagination，page_size 預設 50 / 上限 500）取 live data；Saved Views frontend 接 v2.5.0 已存在的 backend CRUD；TenantCard 加 Alert Builder / Routing Trace deep link。
- **Defaults 變動可預演** — `/api/v1/tenants/simulate` ephemeral primitive 讓 CI 與 UI 在 commit 前預測 `_defaults.yaml` 變動對 inheritance 的影響（無 disk IO、無 manager state mutation）；新 `da_config_blast_radius_tenants_affected` histogram 量化每 tick 受影響 tenant 分佈。
- **Q2 2026 CVE 集中收斂** — Grafana / Prometheus 3.x / Alertmanager / oauth2-proxy / alpine-git / Python base / Go toolchain 一次 bump，清掉 50+ CVE（13 個 CRITICAL、35+ HIGH），涵蓋 auth bypass、RCE、memory safety、TLS DoS。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100)。
- **API 邊界硬化** — `tenant-api` body-content range validation 在邊界 fail-fast（patch key/value 超長、`_timeout_ms` 超過 1 小時、`_silent_mode` 非 enum 都直接 400 + 完整 violations array）；flat + nested mixed-mode 同 tenant id 重複從 WARN 升級為 hard error。

### 客戶導入管線（5-step chain，新功能）

- **`da-parser`**：PromRule YAML → canonical JSON。dialect 偵測（`prom` / `metricsql` / `ambiguous`）+ VM-only function allowlist（`vm_only_functions.yaml` 走 `go:embed`，CI freshness gate 偵測新版 metricsql 上游函數）+ `StrictPromQLValidator` + provenance header。`prom_portable: bool` 旗標讓客戶遷入 VM 後仍能識別「可回 Prom」的子集 — anti-vendor-lock-in 具體承諾
- **`da-tools profile build`**：cluster 相似 rules → median 演算法決定 cluster 共通閾值 → 寫 `_defaults.yaml`、偏離 tenant 寫 `<id>.yaml` 只含 override；fuzzy matching opt-in 套 duration-equivalence canonicalisation（`[5m]` ≡ `[300s]` ≡ `[300000ms]`）；遵循 [ADR-018](docs/adr/018-profile-as-directory-default.md) Profile-as-Directory-Default
- **`da-batchpr apply` + `refresh`**：Hierarchy-Aware 分塊 — `_defaults.yaml` 變更打 Base Infrastructure PR、tenant PRs 標 `Blocked by:`；`refresh --base-merged` 在 Base merge 後自動 rebase tenant PRs；`refresh --source-rule-ids` 對 parser bug fix 細粒度重生 patch PR
- **`da-guard`**：Schema / Routing / Cardinality / Redundant-override 四層檢查；`.github/workflows/guard-defaults-impact.yml` 自動跑 + sticky PR comment（marker-based update vs create）+ artifact 14d retention
- **Migration Toolkit 三條交付路徑**：(a) Docker pull `ghcr.io/vencil/da-tools`；(b) Static binary linux/darwin/windows × amd64/arm64 共 18 個 archive；(c) Air-gapped tar（`docker save` export）。每條路徑 cosign keyless 簽 + SBOM SPDX/CycloneDX；客戶 `make verify-release` 一鍵驗

### Scale Foundation III — 千租戶生產驗證

- **1000-tenant baseline land**：`make benchmark-report` 17 benches × count=6 跑 nightly cron；mixed-mode flat+hierarchy benches 加入 trend tracking。Cold load 112 ms / steady-state reload 1.3 ms @ 1000 tenants
- **`/api/v1/tenants/simulate` + Ephemeral Graph**：tenant.yaml dry-run preview（不污染 watch loop）；CI gate `TestSimulate_VsResolve_ParityHash` 鎖死「simulate=commit-後 preview」契約
- **5-anchor end-to-end alert fire-through harness**（`tests/e2e-bench/`）：從 `conf.d/` 寫入 → exporter reload → Prometheus alert trigger → Alertmanager dispatch → webhook receiver 的完整鏈；n=30 + bootstrap 95% CI；docker-compose 6-service stack
- **Bench-gate 兩層 CI 治理**（[#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433)）：Tier 1 per-PR single-runner sequential（同 VM 同 CPU by construction → INCONCLUSIVE ~0%）；Tier 2 release-time cumulative drift report（informational）；`override: bench-regress-ok` label 走獨立 `bench-override-audit.yaml` workflow event-scoped 驗證

### Customer-Facing Surface

- **Server-side Search API** `GET /api/v1/tenants/search`：page_size cap 500 + closed-field free-text + RBAC-before-pagination + 30s TTL `tenantSnapshotCache`，p99 < 200 ms @ 1000T
- **Tenant Manager JSX**：API-first 3-layer priority chain（API → platform-data.json → DEMO）+ 429 retry-with-backoff + URL state（`useURLState` + `useDebouncedValue`）+ self-written `useVirtualGrid`（>50 才 virtualize）
- **Master Onboarding Dual Entry**：Import Journey 5 步（parser → profile build → batch-pr → guard inline CLI）vs Wizard Journey 5 步（cicd-setup → deployment → alert-builder → routing-trace → tenant-manager — 全 5/5 真 wizards）
- **TenantCard × Wizard 整合**：footer 三鈕（Alert / Route / Preview）deep link + `?tenant_id=` URL 參數預填 + 獨立 `simulate-preview.jsx` widget（4-state machine + 500ms debounce + AbortController）
- **Smart Views**：`useSavedViews` + `SavedViewsPanel` 接 v2.5.0 backend `/api/v1/views` CRUD；RBAC-aware（Save/Delete hidden when `canWrite=false`）

### 測試與 CI 基礎設施

- **`ConfigManager` test-only setter 注入**：`m.SetMetrics` / `m.SetLogger` / `m.SetClock` 取代 v2.7.x 三類 global-swap（`withIsolatedMetrics` / `log.SetOutput` / WatchLoop `time.Sleep`）；過去因 global state race 必須 serial 的測試現在 `t.Parallel`-eligible，full app pkg `-count=5 -race` 12.1s（previously 20.7s）。AI agent quickref → [`test-map.md` §測試注入 Seam](docs/internal/test-map.md)
- **Policy-as-Code lint**：`check_hardcode_tenant.py`（dev-rules #2 PromQL label selector）/ `check_subprocess_timeout.py`（FATAL）/ `check_jsx_loader_compat.py` / `check_playwright_rtl_drift.py` / `check_codename_leak.py`（[#462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/462) / [#468](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/468)）等。56 hooks 共 39 auto + 14 manual + 3 pre-push
- **Tenant API hardening**：rate limit per-pod + `X-Request-ID` echo + tenant-scoped authz（4 endpoints）+ body-content range validation（go-playground/validator + struct tags + reservedKeyValidators registry）
- **`bump_docs.py` 跨四條 release line 機械 bump**：91+ 文件 frontmatter / helm Chart / Dockerfile / k8s cronjob image tag 一次同步（[#439](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/439)）

### 文件治理

- **Customer-facing docs codename leak sweep**（[#462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/462) / [#468](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/468)）：22 customer-facing 文件、~213 處 codename → feature-name 替換；`check_codename_leak.py` PATTERNS 加 `DEC-X` / `v\d+\.\d+\.\d+-(final\|rc\d*\|alpha\|beta\|preview\d*)`；default scope 從 T0 + component README 擴到 T1（`docs/**` 排除 `internal/`）；新 `PER_FILE_ALLOWLIST` 機制收 ADR-019。Layer 2 self-healing glossary gate → [#469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/469)
- **Planning ID namespace 統一為 TRK**（[#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 1）：依 [ADR-019 Option C refined hybrid](docs/adr/019-planning-ssot.md) 把 `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NNN` 四個分散 namespace 統一為 `TRK-NNN`；新增 [`planning-id-mapping.md`](docs/internal/planning-id-mapping.md) 含完整對映表 + 三段分區編號政策
- **`docs/benchmarks.md` customer-first 重寫**（[#460](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/460)）：488/536 → 209/209 行（−57% / −61%），TL;DR 5 個數字 + v2.2.0 → v2.8.0 evolution + 1000-tenant Scale Gate + e2e fire-through + soak + sizing 七段；toolchain micro-bench detail 移到 [`benchmark-playbook.md §Engineering Reference Benchmarks`](docs/internal/benchmark-playbook.md)
- **ZH-primary SSOT policy lock**：v2.5.0 評估文 §7 原推薦切換 EN SSOT，pilot 工具於 `v2.7.0` 完成；v2.8.0 經內部維運手冊的 4-question audit（含「spec premise validation」一條）後 reverse 原計畫。Pilot 工具保留 dormant，trigger conditions 明確 codify

### Benchmark（1000 tenants, Dev Container, Intel Core 7 240H, Go 1.26.2 linux/amd64, `-benchtime=3s -count=6`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 1.3 ms | Dual-hash steady-state reload（86x 快於 cold） |
| `Simulate_DeepChain` | 5 ms | `/simulate` endpoint per-call |
| 5-anchor e2e fire-through P99 | **4.98 s** | conf.d 寫入 → webhook receiver 全鏈（受 Prometheus 5 s scrape quantization 主導，near-flat 1000→5000 tenants；n=30, bootstrap 95% CI） |

SLO 維持 v2.7.0：cold load 112 ms / 1000 tenants；reload 熱路徑 1.3 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md`](docs/benchmarks.md)。

### ADR 新增（ADR-018 / 019 / 020，3 條）

- **ADR-018** Profile-as-Directory-Default：`_defaults.yaml` 為 cluster 共通閾值的權威位置
- **ADR-019** Planning SSOT：`TRK-NNN` namespace 統一政策（Option C refined hybrid）
- **ADR-020** Tenant Federation outline（v2.9.0+ design seed）

### Breaking changes

- **Mixed-mode duplicate tenant id**：flat + nested 同 tenant id 重複從 WARN 升級為 typed `*DuplicateTenantError` hard error。同 tenant id 出現在兩個位置會直接拒絕 load（state preservation invariant 保證舊 state 不被部分覆寫）
- **`tenant-api` body validation**：超出 range 的 patch 從 silently accept 變成 400 + 完整 violations array（`_timeout_ms` > 1 小時 / `_silent_mode` 非 enum / key|value 超長 等）。客戶 CI 若有 silently-tolerated bad payload 會在升 v2.8.0 後立刻 fail
- **`Open-mode` PUT/DELETE Groups**：tenant-scoped authz 補完後，open-mode 環境（缺 `_rbac.yaml`）下 PUT/DELETE Groups 從可寫變成 403。修補：補 5 行 `_rbac.yaml` `groups: [{name: dev, tenants: ["*"], permissions: [admin]}]`

### Upgrade notes

- 既有客戶：升 v2.8.0 後 mixed-mode duplicate tenant id 不再 silently tolerated — 升前先掃 `da-tools validate-config --strict` 確認沒有重複
- 開啟 Customer Migration Pipeline：見 [Migration Toolkit Installation](docs/migration-toolkit-installation.md)，三條交付路徑任選；客戶 `make verify-release` 驗 cosign + SBOM
- Tenant API hardening：升前讀 [`tenant-api-hardening.md`](docs/api/tenant-api-hardening.md) §3 affected endpoints + §5 known gaps（含 open-mode 升級 checklist）

---
## [v2.7.0] — 千租戶配置架構 + 元件健壯化 (2026-04-19)

v2.7.0 把租戶配置的資料結構升級為可支撐千租戶規模（`conf.d/` 階層 + `_defaults.yaml` 繼承引擎 + dual-hash 熱重載），把 v2.6.0 的 Design Token 定義推進到全面採用，並把測試與 CI 從「能跑」升級為「可規模化」。

### Scale Foundation I — 千租戶配置架構（ADR-016 / ADR-017）

- **`conf.d/<domain>/<region>/<env>/` 階層目錄**：任一層可放 `_defaults.yaml`，`L0 defaults -> L1 domain -> L2 region -> L3 tenant` 四層 deep merge，array replace / null-as-delete 語義明確
- **Dual-hash 熱重載**：`source_hash`（原始檔 SHA-256）+ `merged_hash`（canonical JSON SHA-256）並行追蹤，merged_hash 變才 reload；300ms debounce 吸收 K8s ConfigMap symlink rotation 的連續寫入
- **Mixed-mode**：舊扁平 `tenants/*.yaml` 與新 `conf.d/` 可共存，無強制一次遷移
- **`GET /api/v1/tenants/{id}/effective`**：回傳 merged config + 繼承鏈 + dual hashes，方便 debug 實際生效設定
- **新 CLI**：`da-tools describe-tenant`（含 `--what-if <file>` 模擬 `_defaults.yaml` 變動 -> diff merged_hash）+ `da-tools migrate-conf-d`（扁平 -> 階層自動 `git mv`，預設 `--dry-run`）
- **Schema 新增**：`tenant-config.schema.json` 加入 `definitions/defaultsConfig` + `_metadata.$comment`

### 元件健壯化

- **Design Token 全面遷移**：9 個 Tier 1 JSX 工具完成 Tailwind -> arbitrary value token 改寫（`wizard` / `deployment-wizard` / `alert-timeline` / `dependency-graph` / `config-lint` / `rbac` / `cicd-setup-wizard` / `tenant-manager` / `multi-tenant-comparison`）；剩餘 7 個 px-only 工具延 v2.8.0
- **`[data-theme]` 單軌 dark mode**（ADR-015）：移除 Tailwind `dark:` 雙軌橋接，解決 v2.6.0 誤用陷阱
- **Component Health Snapshot**（ADR-013）：`scan_component_health.py` 五維評分（LOC / Audience / Phase / Writer / Recency）-> Tier 1 = 11 / Tier 2 = 25 / Tier 3 = 3；新增 `token_density` 量化 token 採用進度
- **Colorblind 合規**（ADR-012）：`threshold-heatmap` 結構化 severity（不只靠顏色）
- **TECH-DEBT 類別獨立 budget**：從 REG budget 分出，不佔 REG P2/P3 配額
- **新 lint**：`check_aria_references.py` / `axe_lite_static.py` / `check_design_token_usage.py`

### 測試與基礎設施

- **`tests/` 子目錄分層**：`dx/` / `ops/` / `lint/` / `shared/`，匹配 `scripts/tools/` 的分層
- **1000-tenant synthetic fixture**：`generate_synthetic_tenants.py` 產可重現的千租戶資料，供 B-1 Scale Gate 量測
- **Blast Radius CI bot**：PR 變更自動計算影響的 tenants / rules / thresholds，comment 到 PR
- **Pre-commit**：31 auto + 13 manual-stage；`make pre-tag` 整合 `version-check` + `lint-docs` + `playbook-freshness-ll`

### Benchmark（1000 tenants, Intel Core 7 240H, Go 1.26.1, `-benchtime=3s -count=3`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 2.45 ms | Dual-hash reload noop（45x 快於 cold） |
| `IncrementalLoad + MtimeGuard` | 1.30 ms | 加 mtime 短路（86x 快於 cold） |
| `MergePartialConfigs_1000` | 653 us | 階層 merge 本身 |

SLO：cold load 112 ms / 1000 tenants；reload 熱路徑 1.30 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md §1 規模`](docs/benchmarks.md#1-規模能撐多少租戶)。

### ADR 新增（ADR-012~017，6 條）

colorblind 結構化 severity / component health + token_density / TECH-DEBT 獨立 budget / token 遷移策略 / 單軌 dark mode / `conf.d/` 階層 / `_defaults.yaml` 繼承引擎 + dual-hash 熱重載。

### Breaking changes

無。`conf.d/` 與繼承引擎為**新增能力**；舊扁平 `tenants/*.yaml` 完全向後相容，Schema 只新增不改動。

### Upgrade notes

- 既有使用者：不需變更
- 想採用 `conf.d/` 分層：見 `docs/scenarios/multi-domain-conf-layout.md` + `incremental-migration-playbook.md`，或 `da-tools migrate-conf-d --dry-run`
- 熱重載：dual-hash 預設啟用，debounce window 300ms 可用 `--scan-debounce` 調整

---

## [v2.6.0] — Operator 遷移路徑 × PR Write-back × 設計系統統一 (2026-04-07)

v2.6.0 的核心是「讓 enterprise 客戶能信賴地在 Operator 環境下運營」：建立完整的 ConfigMap → Operator 遷移工具鏈與對稱文件（ADR-008 addendum），引入 PR-based 非同步寫入支援 GitHub 與 GitLab 雙平台（ADR-011），統一設計系統消除三套平行 CSS 的技術債，並新增 4 個互動工具強化價值傳達。

### K8s Operator 完整遷移路徑

v2.3.0 引入的 Operator 指南是單一文件；v2.6.0 將其擴展為與 ConfigMap 路徑完全對稱的文件體系與工具鏈。

- **ADR-008 addendum**：正式記錄架構邊界宣言——threshold-exporter 不 watch 任何 CRD，CRD → conf.d/ 轉換由外部控制器或 CI 負責。含 Mermaid 邊界圖 + 三問判斷標準（ZH + EN 雙語）
- **`operator-generate` 大幅增強**：AlertmanagerConfig 6 種 receiver 模板（Slack, PagerDuty, Email, Teams, OpsGenie, Webhook），每種自動產出 `secretKeyRef` 引用 K8s Secret（零明文 credential）。新增 `--receiver-template`、`--secret-name`、`--secret-key` 參數
- **三態抑制規則 CRD 化**：Silent / Maintenance mode 自動包含在每個 AlertmanagerConfig 產出（4 條 inhibit rules）
- **Helm `rules.mode` toggle**：threshold-exporter chart 新增 `configmap | operator` 切換 + ServiceMonitor 條件模板，operator section 含 ruleLabels、serviceMonitor、receiverTemplate、secretRef
- **`da-tools migrate-to-operator`**（新增 CLI）：讀取現有 ConfigMap rules → 產出等效 CRD + 6 階段遷移清單（Discovery → Generate → Shadow → Compare → Switch → Cleanup）+ rollback 程序。`validate_tenant_name()` RFC 1123 驗證確保 CRD apply 不失敗
- **Operator Setup Wizard**（新增 JSX）：互動式偵測環境 → 選 CRD 類型 → 產出命令，每步驟含 contextual help + 常見陷阱提示
- **Kustomization.yaml 自動產生**：`operator-generate --kustomize` 產出標準格式，含 commonLabels + sorted resources + namespace
- **`drift_detect.py` Operator 模式**：`--mode operator` 透過 kubectl 取得 PrometheusRule CRD 的 spec.groups SHA-256，與本地 YAML 比對。kubectl timeout 30s + 三種錯誤處理
- **Decision Matrix**：提升到 Getting Started 層級，決策樹 + 10 維度比較表（ZH + EN）
- **文件對稱化**：`prometheus-operator-integration.md` 拆分為 4 組子文件（Prometheus / Alertmanager / GitOps / Shadow Monitoring）各含 ZH + EN 版本 + 2 hub 導航頁 = 10 篇新文件

### PR-based Write-back + 非同步 API

v2.5.0 的 tenant-api 只支援 direct write（API → YAML → git commit）。v2.6.0 新增 PR 模式與非同步批量操作，讓高安全環境能透過 code review 流程管理配置變更。

- **ADR-011**（新增 ADR，ZH + EN 雙語）：定調 PR lifecycle state model（pending / merged / conflicted）、GitHub PAT 權限與 Secret 管理策略、多 PR 合併衝突處理、eventual consistency 語義
- **PR-based write-back**：`_write_mode: direct | pr` 配置切換（`-write-mode` flag + `TA_WRITE_MODE` env）。UI 操作 → 建立 PR → reviewer 核准 → 合併。PR-mode API response 回傳 `pr_url` + `status: "pending_review"`
- **Batch PR 合併**：群組批量操作合併為單一 PR（非 N 個），減少 reviewer 負擔
- **Async batch operations**：`?async=true` query param 啟用非同步模式，回傳 `task_id` + `status: "pending"`。goroutine pool 執行，GET `/tasks/{id}` polling 查詢進度
- **Orphaned task 容錯**：Pod 重啟後 in-memory task state 遺失，GET `/tasks/{id}` 回傳 404 附帶 `pod_may_have_restarted` hint
- **SSE 即時通知**：`GET /api/v1/events` 端點，gitops.Writer 寫入成功後自動推播 `config_change` 事件。採用 Server-Sent Events 實作，零外部依賴
- **tenant-manager.jsx**：Pending PRs 提示 banner（頂部顯示待審核 PR 數量與連結，30s 輪詢）

### Platform Abstraction Layer + GitLab 支援

為使 PR write-back 成為平台無關的能力，抽取 platform interface 並新增 GitLab MR 支援。

- **`internal/platform/platform.go`**（新增）：`PRInfo` struct、`Client` interface（5 methods: CreateBranch / CreatePR / ListOpenPRs / ValidateToken / DeleteBranch）、`Tracker` interface（6 methods）。handler 只依賴 interface，provider 可替換
- **`internal/gitlab/`**（新增套件）：GitLab REST API v4 client，`PRIVATE-TOKEN` header 認證，`url.PathEscape` 支援含 `/` 的 `group/subgroup/project` 路徑。全部 5 個 `platform.Client` 方法 + 6 個 `platform.Tracker` 方法
- **Write mode 路由**：`--write-mode direct | pr | pr-github | pr-gitlab` 四種模式，`pr` 為 `pr-github` alias（向後相容）
- **On-Premise 支援**：GitHub Enterprise Server（`TA_GITHUB_API_URL`）+ 自託管 GitLab（`TA_GITLAB_API_URL`）。`SetBaseURL()` 已納入 `platform.Client` interface
- **Compile-time interface assertions**：`var _ platform.Client = (*Client)(nil)` + `var _ platform.Tracker = (*Tracker)(nil)` 確保型別安全
- **錯誤訊息衛生化**：`doRequest` 在 HTTP 4xx/5xx 時 log 完整 response body（debugging），回傳 caller 的 error 只含 status code（不洩漏 API body 給前端）。GitHub + GitLab 兩端一致
- **GitLab state 正規化**：`normalizeState()` 將 GitLab `opened` 映射為 `open`（與 GitHub 一致）
- **ListOpenPRs pagination**：per_page=100, 10 pages safety limit

### 設計系統統一

v2.5.0 暴露了三套平行 CSS 系統（CSS variables / Tailwind / inline styles）是所有無障礙問題的根源。v2.6.0 建立 design token SSOT 並全面遷移。

- **`docs/assets/design-tokens.css`**（新增）：統一 CSS variable 定義（11 個類別：color, spacing, typography, shadow, radius, transition 等），按 §1-§11 組織，命名規範 `--da-{category}-{element}-{modifier}`
- **Dark mode 三態切換**：`[data-theme="dark"]` attribute 取代 `@media (prefers-color-scheme: dark)`。Portal 加入 Light / Dark / System 三態切換按鈕，狀態存 localStorage（fallback: in-memory + cookie）
- **tenant-manager.jsx 遷移**：消除 454 行 hardcoded inline styles，全面切換至 CSS variables + Tailwind classes
- **focus-visible 全局化**：CSS 層統一實作，不再依賴各 JSX 檔案自行加入
- **index.html 統一**：legacy aliases（`var(--bg)`, `var(--muted)`）全面遷移至 `var(--da-*)` tokens
- **`docs/internal/design-system-guide.md`**（新增）：design token 命名規範、使用方式、`[data-theme]` 切換機制、Light/Dark/System 三態邏輯

### 價值傳達與互動工具

讓潛在使用者與現有客戶能快速量化平台的採用價值。

- **ROI Calculator 增強**：新增 Quick Estimate 模式（單一輸入即出結果）+ 完整三維計算（Rule Maintenance + Alert Storm + Time-to-Market）
- **Migration ROI Calculator**（新增 JSX）：輸入 PromQL 行數 / rules / tenants → coverage estimation + migration effort + break-even analysis
- **Cost Estimator**（新增 JSX, 827 lines）：tenants × packs × scrape interval × retention × HA replicas × deployment mode → Resource Summary + Monthly Cost + ConfigMap vs Operator 比較 + Quick Recommendation
- **Notification Template Editor**（大幅改版, 897 lines）：從 Previewer 升級為 Editor——可編輯 title/body 模板 + template variable autocomplete + validation（unmatched braces, char limits）+ live preview + export YAML/JSON + template gallery（Detailed/Compact/Bilingual presets）
- **architecture-and-design.md** 每個子主題加入 business impact 欄位（ZH + EN，O(M) vs O(N×M) 複雜度對比、Onboard 2hr→5min 等量化指標）
- **release-notes-generator.jsx** 新增 `generateAutoSummary()` 函式，CHANGELOG 角色分流自動摘要（per-role "What's new for you"）

### 測試與品質

- **Playwright axe-core 整合**：`@axe-core/playwright` 自動偵測 WCAG 違規，整合到既有 5 個 smoke tests + 新增 Operator Wizard 12 tests
- **Property-based testing**（新增 22 tests）：Hypothesis 覆蓋 tenant name RFC 1123 validation、SHA-256 hashing、drift detection symmetry、YAML round-trip、kustomization builder。`@settings(max_examples=100)` 確保覆蓋
- **Go `-race` 全通過**：Phase .e 發現並修復 async/taskmanager.go + ws/hub_test.go data race。`Get()` 改為回傳 deep copy snapshot 防止併發讀寫
- **大型 Python 工具重構**：`generate_alertmanager_routes.py`（1,474→1,645 lines，21 helpers extracted，>100 行函式 4→0）+ `init_project.py`（1,404→1,438 lines，6 helpers extracted）
- **aria-live regions**：tenant-manager.jsx 新增 4 個 region（sidebar, PRs banner, batch, tenant grid）+ threshold-heatmap.jsx 新增 3 個 region
- **Batch response summary**：tenant_batch.go + group_batch.go 回傳 `summary` 欄位（"N succeeded, M failed"）
- **version-consistency hook 擴展**：覆蓋 e2e/package.json、JSX 工具版號
- **tool-registry.yaml 對齊**：補齊 3 個缺失條目（rbac-setup-wizard, release-notes-generator, threshold-heatmap）

### 數字

| 項目 | v2.5.0 | v2.6.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 38 | 42 | +4 |
| ADRs | 10 | 11（+ ADR-011）+ ADR-008 addendum | +1 |
| Operator 子文件 | 1 | 10（4 ZH + 4 EN + 2 hub） | +9 |
| Go test packages（`-race` clean） | — | 11 packages, 0 race | NEW |
| Property-based tests (Hypothesis) | 0 | 22 | NEW |
| Helm chart features | — | `rules.mode` toggle + ServiceMonitor | NEW |
| Write-back 模式 | 1（direct） | 4（direct / pr / pr-github / pr-gitlab） | +3 |
| Platform providers | 0 | 2（GitHub + GitLab） | NEW |
| Python 工具 | 91 | 95 | +4 |
| Pre-commit hooks | 19 auto + 9 manual | 19 auto + 10 manual | +1 manual |
| 環境變數（tenant-api） | ~10 | ~18 | +8（Write-back + GitLab） |

### 🐛 Bug Fixes

- `migrate_to_operator.py`：`discover_tenant_configs()` 靜默過濾無效 tenant 名稱 → 改為回報至 `analysis["issues"]` 清單
- `tracker.go`：`RegisterPR()` 同 tenant 可能重複 append → 改為 replace-or-append 邏輯
- `migration-roi-calculator.jsx`：2 個 label 未翻譯
- index.html：light-mode `.journey-phase-badge` + `.card-icon` 殘留 hardcoded hex color → 全部改用 design tokens
- README.md / README.en.md：badge 版號 v2.5.0 → v2.6.0
- troubleshooting.en.md：缺少 Prometheus Operator 章節 → 新增完整診斷+修正步驟+Rollback 程序
- troubleshooting.md：Operator 章節僅有診斷 → 補充三種修正步驟 + Rollback 程序

---

## [v2.5.0] — Multi-Tenant Grouping × Saved Views × E2E Testing (2026-04-06)

v2.5.0 在 v2.4.0 建立的 Tenant API 基礎上，實現租戶分群管理（ADR-010）、Saved Views、Playwright E2E 測試基礎，並新增 4 個互動工具。

### Multi-Tenant Grouping（ADR-010）

- 新增 `conf.d/_groups.yaml` 儲存結構：靜態 `members[]` 成員清單，Git 版本化，可 code review
- Group CRUD API：`GET/PUT/DELETE /api/v1/groups/{id}` + `POST /api/v1/groups/{id}/batch` 批量操作
- Permission-filtered listing：ListGroups 只回傳使用者有權限存取至少一個成員的 group
- 批量操作逐 tenant 驗證寫入權限，部分失敗不影響已成功項目

### Saved Views API

- 新增 `conf.d/_views.yaml`：持久化篩選條件（environment + domain + 自訂 filter 組合）
- CRUD 端點：`GET/PUT/DELETE /api/v1/views/{id}`，支援使用者自建常用視圖
- 與 Portal tenant-manager 整合：一鍵切換預設篩選

### Tenant Metadata 擴展

- 新增可選欄位：`environment`、`region`、`domain`、`db_type`、`tags[]`、`groups[]`
- 全部向後相容——未設定 metadata 的 tenant 不受影響
- Metadata 僅 API/UI 層使用，不影響 Prometheus metric cardinality

### RBAC 增強

- `_rbac.yaml` 新增 `environments[]` 和 `domains[]` 可選過濾欄位
- 支援「特定 group 只能管理 production 環境」等細粒度控制

### 新增互動工具（34 → 38 JSX tools）

- **Deployment Profile Wizard** (`deployment-wizard.jsx`)：互動式 Helm values 產生器
- **RBAC Setup Wizard** (`rbac-setup-wizard.jsx`)：互動式 `_rbac.yaml` 產生
- **Release Notes Generator** (`release-notes-generator.jsx`)：從 CHANGELOG 自動產生角色導向更新摘要
- **Threshold Heatmap** (`threshold-heatmap.jsx`)：跨 tenant 閾值分佈熱力圖 + 離群偵測 + CSV 匯出

### Playwright E2E 測試基礎

- 5 個 critical path spec（38 個 test case）：portal-home、tenant-manager、group-management、auth-flow、batch-operations
- Mock API 隔離（無外部依賴）、GitHub Actions CI 整合
- `tests/e2e/playwright.config.ts` + `.github/workflows/playwright.yml`

### CI/CD 改進

- tenant-api Go 測試納入 CI pipeline（2,115 行測試程式碼）
- Release 流程強化：`make pre-tag` 閘門、`bump_docs.py` 新增 tenant-api 版號線

### 數字

| 項目 | v2.4.0 | v2.5.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 34 | 38 | +4 |
| ADRs | 9 | 10 | +1（ADR-010） |
| Playwright E2E specs | 0 | 5（38 test cases） | NEW |
| API 端點 | ~10 | ~16 | +6（groups + views） |

---

## [v2.4.0] — 防守深化 × 體質精簡 × 租戶管理 API (2026-04-05)

v2.4.0 的核心是「從能用到好管」：將 v2.3.0 release 暴露的手動痛點全面自動化（Phase A），對膨脹的核心檔案進行結構性重構（Phase B/B.5），引入 Tenant Management API 作為管理平面（Phase C），並重整 Playbook 體系（Phase D）。

### Phase A — 防守工具補強

將 v2.3.0 release 過程中手動發現的 6 類問題轉化為 pre-commit hook，auto hooks 從 13 個增至 19 個。

- **`check_build_completeness.py`**：`build.sh` ↔ `COMMAND_MAP` 雙向同步檢查，防止 Docker image 中工具遺漏
- **`check_bilingual_structure.py`**：ZH/EN 文件 heading hierarchy 骨架比對 + README 雙語導航對稱性
- **`check_jsx_i18n.py`**：`TOOL_META` ↔ `CUSTOM_FLOW_MAP` key set 一致性、`window.__t` 雙參數驗證
- **`check_makefile_targets.py`**：每個 `dx/generate_*.py` 和 `dx/sync_*.py` 工具被至少一個 Makefile target 引用
- **`check_metric_dictionary.py`**：`metric-dictionary.yaml` 與 Rule Pack YAML 交叉驗證，偵測 stale/undocumented entries
- **`check_cli_coverage.py` hook 化**：從測試升級為 pre-commit auto hook，cheat-sheet ↔ cli-reference ↔ COMMAND_MAP 三向一致
- **`_lint_helpers.py`**：抽取 `parse_command_map()`、`parse_build_sh_tools()`、`BUILD_EXEMPT` 等共用邏輯，消除 ~80 行重複

### Phase B — Go config.go 分拆 + 程式碼體質改善

- **config.go 拆分**（2,093 行 → 4 檔案）：`config_types.go`（268 行，型別定義）+ `config_parse.go`（277 行，YAML 解析）+ `config_resolve.go`（750 行，ResolveAt + 驗證）+ `config.go`（823 行，ConfigManager + 公開 API）
- 拆分為純結構移動，public API 語意不變，benchmark 差異 -0.3% ~ -5.0%（±5% 以內）
- **config_test.go table-driven 重構**：4,236 → 3,929 行（-7.2%），38 個重複 test function 收斂為 8 個 table-driven test，test function 總數 145 → 115
- Go 全部 145 測試通過，Python 3,657 passed / 44 skipped / 0 failed

### Phase B.5 — 文件與測試瘦身

Phase B 做到了「結構整理」，B.5 補做「內容精簡」。

- **合併 `context-diagram.md` → `architecture-and-design.md`**：~70% 重疊內容消除，淨刪 ~1,165 行，docs/ 檔案數 115 → 113
- **`incremental-migration-playbook.md` 瘦身**：1,165 行 → 575 行（-50.6%），冗長 JSON 範例改為摘要，手動 kubectl 序列改為 `da-tools` 命令
- **三態說明集中化**：`tenant-lifecycle.md` 的 60 行重複三態解釋改為 hyperlink + 3 行速查
- **版號全域修正**：44 處過時版號更新 + 文件計數修正
- 文件總計：docs/ -2,362 行（-6.4%），-2 個檔案

### Phase C — Tenant Management API（ADR-009）

新增 `components/tenant-api/` Go 元件，為 da-portal 加入 Backend API。

**架構決策（ADR-009）**
- API 語言選 Go：與 threshold-exporter 共用 `pkg/config` 解析邏輯，避免 Go↔Python 雙端維護
- 認證用 oauth2-proxy sidecar：API server 零 auth 程式碼，讀 `X-Forwarded-Email` / `X-Forwarded-Groups` header
- 寫回用 commit-on-write：UI 操作 → API → 修改 YAML → git commit（操作者名義），保留完整 audit trail
- RBAC 用 `_rbac.yaml` + `atomic.Value` 熱更新：lock-free 讀取，與 threshold-exporter reload 模式一致
- 不引入資料庫——Git repo 就是 database

**`pkg/config/` 抽取**
- 將 threshold-exporter 的型別與解析邏輯抽入 `components/threshold-exporter/app/pkg/config/`（`types.go` + `parse.go` + `resolve.go`）
- tenant-api 透過 `go.mod replace` directive 直接 import 共用型別

**API 端點**
- `GET /api/v1/tenants` — 租戶列表（支援 group/env 篩選）
- `GET/PUT /api/v1/tenants/{id}` — 單一租戶 CRUD
- `POST /api/v1/tenants/{id}/validate` — 乾跑驗證（不寫入）
- `POST /api/v1/tenants/batch` — 批量操作（`sync.Mutex` 同步，response 預留 `task_id`）
- `GET /api/v1/tenants/{id}/diff` — 預覽變更差異
- Health check / readiness probe / Prometheus metrics

**Portal 降級安全**：API 不可用時，tenant-manager.jsx 自動降級為 platform-data.json 唯讀模式。

**交付物**：Go binary + Docker image（distroless base）+ Helm chart + K8s manifests + 五線版號新增 `tenant-api/v*`

### Phase D — Playbook 重整 + 文件治理

- Playbook 結構化：testing-playbook 五段分層、benchmark-playbook 加入決策樹、windows-mcp-playbook 32 個 pitfall 分類索引
- `bump_docs.py` 自動計數功能：掃描並更新散落各處的工具數量、Rule Pack 數量等
- doc-map.md 自動生成預設包含 ADR

### 數字

| 項目 | v2.3.0 | v2.4.0 | 變化 |
|------|--------|--------|------|
| Pre-commit hooks | 13 auto + 7 manual | 19 auto + 9 manual | +6 auto, +2 manual |
| Go config.go | 2,093 行 × 1 檔 | 4 檔（268 + 277 + 750 + 823） | 結構拆分 |
| config_test.go | 4,236 行 / 145 函式 | 3,929 行 / 115 函式 | -7.2% / table-driven |
| docs/ 行數 | 37,059 | 34,697 | -2,362（-6.4%） |
| Components | 3 | 4（+ tenant-api） | +1 |
| ADRs | 8 | 9（+ ADR-009） | +1 |
| JSX 互動工具 | 29 | 34 | +5 |
| 版號線 | 4 | 5（+ tenant-api/v*） | +1 |
| Python 工具 | 84 | 91 | +7 |

---

## [v2.3.0] — Operator-Native × Management UI × Platform Maturity (2026-04-04)

v2.3.0 聚焦四大主題：Operator-Native 整合、Multi-Instance Management UI、Portal & Doc 成熟度、品質閘門升級。

### Phase .a — Portal & DX Foundation

**Self-Service Portal 模組化**
- `self-service-portal.jsx`（1,376 行）→ 5 個模組：`portal-shared.jsx`（共用常數/函式/元件）+ `YamlValidatorTab.jsx` + `AlertPreviewTab.jsx` + `RoutingTraceTab.jsx` + coordinator
- 新增 `dependencies` frontmatter 機制：jsx-loader.html 支援 YAML frontmatter 中宣告依賴，依序載入 → `loadDependency()` / `loadDependencies()` / `transformImports()`
- `window.__portalShared` 模式：共用模組透過全域變數註冊，tab 模組解構取用

**Template Gallery 外部化**
- 24 個模板 → `docs/assets/template-data.json`（雙語 `{zh, en}` 物件格式 + `category` 欄位）
- `template-gallery.jsx` 改為 `useEffect` fetch 載入，新增 loading/error 狀態
- 檔案大小：806 → 293 行（-64%）

**Portal Hub 五層重組**
- 29 個工具卡片從 2 區（Interactive / Advanced）→ 5 層級：Start Here、Day-to-Day、Explore & Learn、Simulate & Analyze、Platform Operations
- 新增 Quick Access 面板（5 個常用工具快捷連結）
- 每層級附色彩標籤（Onboarding / Core Workflow / Reference / What-If / Engineer）
- Role filter 同時作用於 Quick Access chips
- Tour 步驟更新、Footer 版號同步

**文件模板系統**
- 新增 `docs/internal/doc-template.md`：定義文件標準結構（frontmatter + 必要 section + Related Resources）
- 新增 `scripts/tools/lint/check_doc_template.py`：frontmatter 完整性 + Related Resources 存在性 + 版號一致性

**`_lib_python.py` 模組拆分**
- `_lib_python.py` → 4 個子模組：`_lib_constants.py`（守護值/常數）+ `_lib_io.py`（檔案 I/O）+ `_lib_validation.py`（驗證邏輯）+ `_lib_prometheus.py`（HTTP/Prometheus 查詢）
- 原檔保留為 re-export facade（向後相容，53 行）

**SAST Rule 7**
- 新增 `TestStderrRouting`：AST 掃描 `print("ERROR..."` / `print("Error..."` 確保附帶 `file=sys.stderr`
- 支援 literal string 和 f-string 兩種格式偵測

---

### Phase .b — Operator-Native + Federation

**ADR-008: Operator-Native Integration Path**
- 雙路整合架構決策：既有 ConfigMap 路徑保留，新增 Operator-Native 模式作為 BYO 方案
- 工具鏈適配而非平台重寫原則——threshold-exporter Go 核心語意不變
- 新增 `detectConfigSource()` 函式：逐級檢測 operator env var → git-sync `.git-revision` 文件 → configmap（預設）

**Prometheus Operator 整合指南**
- 新增 `docs/prometheus-operator-integration.md`（雙語 zh + en）：架構圖、CRD 對應、3 個部署場景（all-in-one / mixed / operator-only）
- BYO 文件清理：移除 Prometheus Operator appendices，改為重定向至新指南
- ServiceMonitor / PrometheusRule / AlertmanagerConfig CRD 映射表

**da-tools Operator 工具**
- **`da-tools operator-generate`** — 從 Rule Packs + Tenant 配置產生 PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML
  - 支援 `--namespace` / `--labels` / `--annotations` 自訂，`--output-format yaml | json`
  - 整合於 da-tools entrypoint + build.sh 打包
- **`da-tools operator-check`** — CRD 驗證工具：PrometheusRule 語法 + AlertmanagerConfig 路由合法性 + ServiceMonitor label 一致性
  - 支援 `--kubeconfig` / `--context` 直連 K8s 驗證，亦支援離線 YAML 驗證
  - Registered in CI lint pre-commit hooks

**Config Info Metric（四層感知）**
- 新增 `threshold_exporter_config_info{config_source, git_commit}` info metric
- 三種模式 + 自動偵測：
  - `configmap`（預設）：從 ConfigMap mount path 讀取 config version
  - `git-sync`：讀取 `.git-revision` 共享 volume 文件，提供 git commit SHA
  - `operator`：讀取 env var `CONFIG_SOURCE=operator` + `GIT_COMMIT=<sha>`
- `detectConfigSource()` 呼叫於 reload 時，確保 metric 實時反映部署形態

**Federation Scenario B（邊緣-中央分裂）**
- **`da-tools rule-pack-split`** — Rule Pack 聯邦分裂工具：
  - Part 1（正規化層）：邊緣側 metric value 驗證、單位轉換、異常值濾除 → 產生 Prometheus RecordingRules
  - Parts 2+3（閾值 + 警報層）：中央側聚合、cross-edge 關聯、全域告警決策 → 產生 Alerting Rules
  - 支援 `--operator` CRD 輸出 + `--gitops` 模式（目錄結構）
  - 關鍵特性：無狀態 split（idempotent）、邊緣 auto-healing（快照回滾）
- **`federation-integration.md` §8** — Scenario B 完整文件：三階段部署（邊緣佈建 → 中央策略 → 端對端驗證）、MTTR 優化、成本模型

**Go 單元測試（+12 tests，覆蓋率 87% → 94%）**
- WatchLoop 整合測試：無檔案變動 / 新增檔案 / 更新現有檔案
- `resolveConfigPath()` 三情案例：configmap flag / git-sync flag / 未設定（預設 configmap）
- `detectConfigSource()` 四情案例：configmap（預設）/ git-sync / operator / precedence（operator > git-sync > configmap）
- Config Info metric 收集器三情案例：各模式 value 驗證 + label 正確性
- Fail-Safe Reload E2E：config 不可讀時 fallback 邏輯

---

### Phase .c — Management UI + Intelligence

**Tenant Manager Data Foundation**
- 新增 `scripts/tools/dx/generate_tenant_metadata.py`：從 `conf.d/` 目錄結構推斷租戶 metadata
  - Rule Pack 推斷：根據 YAML 中 metric prefix 比對 Rule Pack 定義
  - 運營模式推斷：`_silent_mode` / `_state_maintenance` 標誌偵測
  - 路由通道推斷：`_routing` 配置解析
- 擴展 `scripts/tools/dx/generate_platform_data.py`：產出的 `platform-data.json` 新增 `tenant_groups` + `tenant_metadata` 結構
- Tenant metadata 版本化：支援 `--output-dir` 自訂輸出路徑，方便 GitOps 集成

**Tenant Manager UI 元件**
- 新增 `docs/interactive/tools/tenant-manager.jsx`（~650 行）：
  - 響應式卡片牆佈局，環境/層級徽章（dev/staging/prod + app/infra/platform）
  - 運營模式指示器：Normal / Silent / Maintenance 視覺標記 + expires 倒數
  - 批量操作：批次維護/靜默模式 YAML 產生器，支援日期範圍選擇
  - 篩選+搜尋：按環境/層級/模式多維度過濾，模糊搜尋租戶名
- 加入 `tool-registry.yaml` + Portal Hub Tier 1 (Day-to-Day 層級)

**閾值推薦 × Portal 智慧**
- 新增 `docs/assets/recommendation-data.json`：15 個核心指標的 P50/P95/P99 預計算資料
  - 資料來源：歷史基線 + 業界最佳實踐
  - 格式：`{metric_name: {p50, p95, p99, source, last_updated}}`
- 擴展 `docs/interactive/tools/AlertPreviewTab.jsx`：
  - Progress bar 上疊加 recommended value marker 視覺指示
  - Confidence badge（high/medium/low）顯示推薦可信度
  - 新增 "Apply Recommended Values" 按鈕，一鍵生成更新 YAML

**OPA/Rego 策略整合**
- 新增 `scripts/tools/ops/policy_opa_bridge.py`（~450 行）：tenant YAML → OPA input JSON 轉換 + 雙模式評估
  - 轉換函式：YAML 欄位 → OPA JSON 輸入格式映射（支援 nested policies）
  - 評估模式：REST API 模式（連接遠端 OPA 伺服器）+ 本地 opa binary 模式
  - 違規輸出格式轉換：OPA violations → da-tools 標準格式（location + description）
- `scripts/policies/examples/` 新增三個 Rego 範例策略：
  - `routing-compliance.rego`：路由規則命名 / receiver type / group_wait 範圍 validation
  - `threshold-bounds.rego`：閾值範圍檢查 / 關鍵指標預留冗餘
  - `naming-convention.rego`：租戶/告警 ID 命名規範 + Prefix 合法性
- 登記為 `da-tools opa-evaluate` 子命令 + CI lint 整合

**Portal i18n Lint 工具**
- 新增 `scripts/tools/lint/check_portal_i18n.py`（~250 行）：掃描 JSX 檔案尋找硬編碼字串
  - AST 解析：偵測 string literal 未用 `window.__t()` 包裝的情況
  - 支援 `--fix-mode`：自動生成修復建議（帶位置資訊）
  - 排除清單：URL / 特殊字元序列 / i18n 函式呼叫內部字串
- 加入 pre-commit manual-stage hooks 為 `check-portal-i18n`

---

### Phase .d — Quality Gate + CI Maturity

**GitHub Actions CI Matrix**
- 新增 `.github/workflows/ci.yml`：Python 3.10/3.13 × Go 1.22/1.26 矩陣（4 × 2 = 8 組合）
- 4 個主 jobs：lint（文件+工具格式）、python-tests（pytest + coverage）、go-tests（threshold-exporter）、lint-docs（SAST + doc 品質）
- pip/Go module 緩存策略、coverage artifacts 產生、失敗時自動 debug log 產出

**Coverage Gate 強制**
- `pyproject.toml` 新增 `fail_under = 85`，CI 強制 `--cov-fail-under=85` 執行
- README.md 新增 CI badge 與 coverage badge（green ≥85%、yellow 80–85%、red <80%）
- Python 工具預期整體覆蓋率 ≥85%

**Python 型別系統加強**
- `_lib_constants.py`、`_lib_io.py`、`_lib_validation.py`、`_lib_prometheus.py` 加入完整型別提示
- 新增 `mypy.ini`：strict mode for all `_lib_*` modules、relaxed mode for test files
- CI lint job 新增 `mypy scripts/tools/_lib_*.py --config-file=scripts/tools/mypy.ini` 步驟

**Integration + Snapshot 測試**
- `tests/test_tool_exit_codes.py`（parametrized）：全部 84+ 工具的 `--help` + invalid args exit code 合約測試
- `tests/test_pipeline_integration.py`：scaffold → validate → routes 完整 pipeline 端對端測試
- `tests/test_snapshot.py`：help output stability snapshot tests，支援 `--snapshot-update` CI 模式

**Pre-commit Hook 驗證確認**
- 確認 13 個 auto-run hooks + 7 個 manual-stage hooks 全部運作，Phase .a–.c 新增項目完全涵蓋
- `make pre-commit-audit` 新增 make 目標印出 hook 清單與觸發規則

---

## [v2.2.0] — 採用管線 + UX 升級 + 運維工具 (2026-03-17)

v2.2.0 聚焦三大主題：降低採用門檻的 Adoption Pipeline、Portal 互動體驗全面升級、配置運維新工具。新增 2 個 CLI 工具、3 個互動工具、Portal 三大 Tab 重構、24 個 Template Gallery 模板、5-tenant 展演腳本與 Hands-on Lab。

### 採用管線（Phase A — Adoption Pipeline）

- **`da-tools init`** — 專案骨架一鍵產生：CI/CD pipeline（GitHub Actions / GitLab CI）、`conf.d/` 目錄（含 `_defaults.yaml` + tenant YAML）、Kustomize overlays、`.pre-commit-config.da.yaml`，支援 `--non-interactive` 自動模式
- **GitOps CI/CD 整合指南** (`docs/scenarios/gitops-ci-integration.md`) — 三階段管線（Validate → Generate → Apply）、ArgoCD / Flux 整合、PR Comment Bot 工作流
- **Kustomize Overlays** — `configMapGenerator` 模式產生 threshold-config ConfigMap

### UX 升級（Phase B — Portal & Templates）

**Self-Service Portal 重構（3 Tab）**
- **Tab 1 (YAML Validation)**: Rule Pack 多選 → metric autocomplete → 動態 sample YAML 產生 → 即時驗證（含 pack-aware metric key 交叉檢查）
- **Tab 2 (Alert Preview)**: Pack-grouped 滑桿、視覺化閾值條、disabled/no-threshold 狀態顯示、severity dedup 說明
- **Tab 3 (Routing Trace)**: Metric+severity 輸入 → Alert origin → Inhibit check → 四層合併 → Domain Policy check → 通知派送 → NOC 副本

**Template Gallery 擴充（6 → 24 模板）**
- 7 場景模板：ecommerce、iot-pipeline、saas-backend、analytics、enterprise-db、event-driven、search-platform
- 13 Quick Start 模板：每個可選 Rule Pack 各一
- 4 特殊模板：maintenance、routing-profile、finance-compliance、minimal
- View mode 切換（All / Scenarios / Quick Start）+ Pack filter chips + Coverage summary

**新增互動工具**
- **CI/CD Setup Wizard** (`cicd-setup-wizard.jsx`) — 5 步精靈產生 `da-tools init` 命令：CI Platform → Deploy Mode → Rule Packs → Tenants → Review & Generate（第 27 個 JSX 工具）
- **Notification Template Previewer** (`notification-previewer.jsx`) — 6 種 receiver 通知預覽（Slack / Email / PagerDuty / Webhook / Teams / Rocket.Chat）+ Dual-Perspective annotation 展示 + Severity Dedup 說明（第 28 個）
- **Platform Health Dashboard** (`platform-health.jsx`) — 平台健康儀表板：元件狀態、租戶概覽、Rule Pack 使用分佈、Reload 事件時間線（第 29 個）

**展演與教學**
- **Demo Showcase** (`scripts/demo-showcase.sh`) — 5-tenant 完整展演腳本（prod-mariadb / prod-redis / prod-kafka / staging-pg / prod-oracle），7 步驟自動執行，支援 `--quick` 模式
- **Hands-on Lab** (`docs/scenarios/hands-on-lab.md`) — 30–45 分鐘 Docker-based 實戰教程，8 個練習覆蓋 init → validate → routes → routing trace → blast radius → three-state → domain policy

### 運維工具（Phase C — Operations）

- **`da-tools config-history`** — 配置快照與歷史追蹤：`snapshot` / `log` / `show` / `diff` 子命令，`.da-history/` 存儲，SHA-256 變更偵測，git-independent 輕量級版本控制

### 漸進式遷移 Playbook

- **`docs/scenarios/incremental-migration-playbook.md`** — 四階段雙軌並行遷移法（Strangler Fig Pattern）：Phase 0 Audit（`onboard` + `blind-spot`）→ Phase 1 Pilot（單一 domain 影子部署）→ Phase 2 Dual-Run（`shadow-verify` 品質比對）→ Phase 3 Cutover（逐 domain 切換）→ Phase 4 Cleanup。每步有 CLI 指令、預期輸出、回退方式
- **`architecture-and-design.md` §2.13** — 新增效能架構說明：Pre-computed Recording Rule vs Runtime Aggregation 的 PromQL 對比，解釋為什麼 tenant 增加不會導致 Prometheus CPU/Memory 暴增

### GitOps Native Mode

- **`da-tools init --config-source git`** — 產生 git-sync sidecar Kustomize overlay，threshold-exporter 直接從 Git 倉庫讀取配置，省去 ConfigMap 中間層。支援 SSH / HTTPS 認證、自訂分支與路徑。git-sync sidecar 寫入 emptyDir shared volume，threshold-exporter 的既有 Directory Scanner + SHA-256 hot-reload 機制無縫復用
- **`da-tools gitops-check`** — GitOps Native Mode 就緒度驗證工具，三個子命令：`repo`（Git 倉庫可達性 + 分支驗證）、`local`（本地 conf.d/ 結構驗證）、`sidecar`（K8s git-sync 部署狀態檢查），支援 `--json` 和 `--ci` 模式
- **Container Image Security Hardening** — 三層防護：base pin + build-time upgrade + attack surface reduction
  - threshold-exporter：`alpine` → `distroless/static-debian12:nonroot`（零 CVE，無 shell/apk/openssl）
  - da-tools：`python:3.13-alpine` → `python:3.13.3-alpine3.22` multi-stage build（修復 CVE-2025-48174, CVE-2025-15467）
  - da-portal：`nginx:1.28-alpine` → `nginx:1.28.0-alpine3.22` + `apk del libavif gd libxml2`（移除未使用 library，消除掃描器 false positive）

### 數字

| 項目 | v2.1.0 | v2.2.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 73 | 77 | +4 |
| da-tools CLI 命令 | 27 | 36 | +9 |
| JSX 互動工具 | 26 (+1 wizard) | 29 | +3 |
| Template Gallery 模板 | 6 | 24 | +18 |
| 場景文件 | 6 | 9 | +3 |
| Makefile targets | — | +1 (`demo-showcase`) | NEW |

---

## [v2.1.0] — 運維自助 + 告警智能化 + 性能優化 + 跨域路由 (2026-03-16)

v2.1.0 自 v2.0.0 起的全量升級。涵蓋 Go Exporter 增量熱載入、告警關聯分析、跨域路由架構 (ADR-006/007)、生態整合 (Backstage Plugin)、5 個新 CLI 工具、3 個互動工具、測試 +75%（1,759 → 3,070）、文件治理與正確性全面校正。

### Go Exporter 核心

**Incremental Hot-Reload (§5.6)**
- per-file SHA-256 index + parsed config cache，WatchLoop 增量重載路徑
- `ConfigManager` 新增 `fileHashes` / `fileConfigs` / `fileMtimes` 欄位
- `scanDirFileHashes()` — mtime guard + 輕量 hash check（mtime 未變直接跳過 I/O）
- `IncrementalLoad()` — 比對 per-file hash → 只重新解析 changed/added files → `mergePartialConfigs()`
- `fullDirLoad()` — 完整載入並初始化 cache（首次載入或 fallback）
- `applyBoundaryRules()` — 提取為獨立函式供共用
- **效能優化**：logConfigStats 取代 Resolve()、mtime guard、incremental merge（tenant 檔變動直接 patch）、byte cache（scan 快取復用，免除重複 I/O）
- 15 個 Go tests + 5 個 benchmarks（含 NoChange / OneFileChanged / ScanHashes / MergePartials）

**程式碼品質**
- 4 處 error print 修正為 `stderr` 輸出
- `parsePromDuration` / `isDisabled` / `clampDuration` 新增單元測試
- Go test 增加 config_test.go（801 行）+ config_bench_test.go（268 行）+ main_test.go（97 行）

### 跨域路由架構（ADR-006 + ADR-007）

**ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)**
- 資料面映射方案：Prometheus Recording Rules 實現 1:N 映射（exporter 零修改）
- `generate_tenant_mapping_rules.py` — 讀取 `_instance_mapping.yaml`，產出 Recording Rules（36 tests）
- `scaffold_tenant.py` 新增 `--topology=1:N`、`--mapping-instance`、`--mapping-filter` 參數（9 tests）
- 範例設定檔 `_instance_mapping.yaml`

**ADR-007: Cross-Domain Routing Profiles**
- 四層合併管線：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`
- `generate_alertmanager_routes.py` 擴展：profile 解析 + `check_domain_policies()` 驗證（21 tests）
- `scaffold_tenant.py` 新增 `--routing-profile` 參數
- 重構 `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` 子函式
- 範例設定檔 `_routing_profiles.yaml`、`_domain_policy.yaml`

**ADR-007 工具生態**
- `explain_route.py` — 路由合併管線除錯器：四層展開、`--show-profile-expansion`、`--json`、da-tools CLI 整合（25 tests）
- `check_routing_profiles.py` — CI lint 工具：未知 profile ref、孤立 profile、格式錯誤 constraints、`--strict` 模式（28 tests + pre-commit hook）

### 新增 CLI 工具

- **`da-tools test-notification`** — 6 種 receiver 連通性測試（webhook/slack/email/teams/pagerduty/rocketchat），Dry-run / CI gate / per-tenant 批次。57 tests，97% 覆蓋率
- **`da-tools threshold-recommend`** — 基於歷史 P50/P95/P99 的閾值推薦引擎，純 Python 統計，信心等級分級。54 tests，96% 覆蓋率
- **`da-tools alert-correlate`** — 告警關聯分析：時間窗口聚類 + 關聯分數 + 根因推斷，支援線上/離線模式。95% 覆蓋率
- **`da-tools drift-detect`** — 跨叢集配置漂移偵測：SHA-256 manifest 比對，pairwise 多目錄分析 + 修復建議。99% 覆蓋率
- **`da-tools explain-route`** — 路由合併管線除錯器（ADR-007），25 tests

### 生態整合

- **Backstage Plugin**：`components/backstage-plugin/` TypeScript/React plugin
  - `DynamicAlertingPage` + `DynamicAlertingEntityContent`
  - `PrometheusClient` API 層：via Backstage proxy 查詢 threshold / silent_mode / ALERTS
  - Entity 整合：`dynamic-alerting.io/tenant` annotation → 自動對應租戶

### 互動工具

- **Multi-Tenant Comparison** (`multi-tenant-comparison.jsx`)：Heatmap 色彩矩陣 + Outlier detection + Divergence Ranking（第 25 個 JSX 工具）
- **Alert Noise Analyzer** (`alert-noise-analyzer.jsx`)：MTTR 計算、震盪偵測、去重空間分析、Top noisy alerts（第 26 個）
- **ROI Calculator** (`roi-calculator.jsx`)：Rule 維護 / Alert Storm / Time-to-Market 三模型成本分析（第 27 個）

### DX Tooling

- **`check_frontmatter_versions.py`** — frontmatter version 全域掃描 + `--fix` 自動修復（29 tests）
- **`coverage_gap_analysis.py`** — per-file 覆蓋率排行報表（22 tests）
- **`check_bilingual_content.py`** — 雙語內容 CJK 比例 lint
- **`check_doc_links.py`** — 跨語言對應檔案驗證
- **`validate_all.py`** 增強：`--notify`（桌面通知）、`--diff-report`（CI 失敗自動 diff）
- **`generate_rule_pack_stats.py --format summary`** — Badge 風格單行輸出
- **Snapshot tests v2** — alert_correlate、drift_detect、bilingual_content 快照測試

### 安全加固

- SAST 規則擴充：6 rules 自動掃描（encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions），189 patterns
- NetworkPolicy 精細化、container security context 強化
- 憑證掃描 + `.env` 防護 + `os.chmod 0o600` 補齊
- **CVE 緩解**：CVE-2025-15467 (openssl CVSS 9.8 pre-auth RCE) + CVE-2025-48174 (libavif buffer overflow)
  - 所有 Dockerfile 加入 `apk --no-cache upgrade` 拉取安全修補
  - `da-tools` base image pin 從 `python:3.13-alpine` → `python:3.13.2-alpine3.21`
- **CI Image Scanning**：release workflow 三個 image 均加入 Trivy 掃描（CRITICAL + HIGH 阻斷）

### 品質閘門

- Pre-commit hooks：12 → **13** 個 auto-run（新增 `routing-profiles-check`）
- `build.sh` 修補：新增遺漏的 `alert_correlate`、`notification_tester`、`threshold_recommend` 打包

### 測試覆蓋率

Python 測試總數從 v2.0.0 的 1,759 提升至 **3,070**（+75%）。v2.1.0 新增工具均達 95%+ 覆蓋率，5 個既有工具從 41–74% 提升至 63–99%。Coverage gate 維持 `fail_under=64`，實際整體覆蓋率高於此基線。

### 數字

| 項目 | v2.0.0 | v2.1.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 62 | 73 | +11 |
| da-tools CLI 命令 | 23 | 27 | +4 |
| JSX 互動工具 | 24 | 26 (+1 wizard) | +3 |
| ADRs | 5 | 7（006/007 Accepted） | +2 |
| Python 測試 | 1,759 | 3,070 | +1,311 |
| Pre-commit hooks | 12 + 5 manual | 13 + 5 manual | +1 |
| Go tests (new files) | — | +3 files (1,166 lines) | NEW |

### Benchmark — Incremental Hot-Reload（Go, `-count=3` median）

| Benchmark | ns/op | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| IncrementalLoad_NoChange_10 | 165,700 | 34,272 | 176 |
| IncrementalLoad_NoChange_1000 | 1,528,000 | 2,027,264 | 13,085 |
| IncrementalLoad_OneFileChanged_10 | 220,600 | 73,280 | 241 |
| IncrementalLoad_OneFileChanged_1000 | 6,908,000 | 6,652,880 | 22,211 |
| ScanDirFileHashes_1000 | 1,206,000 | 1,985,200 | 13,012 |

### 文件治理與正確性

**Root README (zh/en) 增強**
- 開頭改為問題導向定位（規則膨脹 + 變更瓶頸），新增「適用場景」聲明與版本 badge
- 「關鍵設計決策」表新增 ADR 連結欄 + Sentinel 三態控制、四層路由合併兩行
- Quick Start 下方新增「生產部署」指引，文件導覽新增 Day-2 Operations 路徑

**ADR 生命週期更新**
- ADR-006/007：`📋 Proposed` → `✅ Accepted (v2.1.0)`，checklist 改為實作摘要 + 後續方向
- ADR-004：「現況與後續方向」替代舊 Roadmap 段落
- ADR-001/003：新增 v2.1.0 living-doc 狀態行
- ADR-002/004：新增「相關決策」交叉引用（ADR-005/006）

**architecture-and-design.md**
- 新增 §2.12 Routing Profiles 與 Domain Policies（四層合併管線 Mermaid 圖）
- 「本文涵蓋內容」補上三態模式、Dedup、路由系統
- 拆分文件導覽表移除過時 §N 前綴
- ADR-006 工具引用、Rule Pack 數量修正、雙語 annotation 章節翻譯

**benchmarks.md 重構**
- §8（Alertmanager Idle-State）合併至 §10（Under-Load）作為 baseline 比較表
- §13（pytest-benchmark）去重：移除與 §7 重複的 route generation 行
- 傳統方案估算加註推算基礎（per-rule ~0.3ms / ~60KB）
- 自引用修正、相關資源連結格式修正

**docs/README.md (zh/en) 去重**
- 移除與 root README 重複的「工具速查」22 行表 → 精簡為摘要 + 連結
- 移除重複的「快速命令」和「版本與維護」段落

**Component README 修正**
- threshold-exporter：斷裂 §11.1 引用 → 指向 `gitops-deployment.md`
- da-tools：版號表 `v2.0.0` → `v2.1.0`，移除過時措辭
- da-portal：`24 JSX tools` → `26`，image tag `v2.0.0` → `v2.1.0`
- backstage-plugin：移除不存在的 `(§5.13)` 引用

**交叉引用修正**
- ADR-006 (zh/en)：`§2.6` → `§2.3`（Tenant-Namespace 映射模式）
- ADR README (zh/en)：ADR-006/007 badge 更新為 ✅ Accepted

### 🐛 Bug Fixes

- 修復 `entrypoint.py` help text 遺漏 `validate-config` 命令
- 4 處 Python error output 修正為 stderr
- `da-tools build.sh` TOOL_FILES 補齊遺漏工具

---

## [v2.0.0] — Alert Intelligence + Full-Stack DX Overhaul (2026-03-15)

v2.0.0 正式版。自 v1.11.0 起的全量升級：76 個 commits、346 個檔案變更（+73,057 / -12,023）。涵蓋 Go Exporter 增強、Rule Pack 擴展、告警智能化、互動工具生態、文件全面重構、測試工程化、專案結構正規化。

> **版號說明**：v1.12.0 / v1.13.0 / v2.0.0-preview 系列皆為開發中版本（無 Git tag / GitHub Release），統一於 `v2.6.0` 正式釋出。

### 🔧 Go Exporter 增強

**Tenant Profiles（四層繼承）**
- Go schema 新增 `Profiles map[string]map[string]ScheduledValue` 欄位
- `applyProfiles()` fill-in pattern：Load 階段展開 profile 至 tenant overrides（僅填入未設定的 key）
- `_profiles.yaml` boundary enforcement：LoadDir 限制 profiles 只能從該檔載入
- `ValidateTenantKeys()` 擴展：`_profile` 引用不存在的 profile → WARN
- 繼承順序：Global Defaults → Profile → Tenant Override（tenant 永遠勝出）
- 13 個新 Go 測試案例

**Dual-Perspective Annotation**
- `platform_summary` annotation：Alert 同時攜帶 Platform 視角（NOC）和 Tenant 視角 summary
- 與 `_routing_enforced` 整合：NOC 收到 `platform_summary`，tenant 收到原始 `summary`

### 📦 Rule Pack 擴展（13 → 15）

- **JVM Rule Pack** (`rule-pack-jvm.yaml`)：GC pause rate、heap memory usage、thread pool — 7 alert rules（含 composite `JVMPerformanceDegraded`）
- **Nginx Rule Pack** (`rule-pack-nginx.yaml`)：active connections、request rate、connection backlog — 6 alert rules
- Projected Volume 13 → 15 ConfigMap sources，scaffold_tenant / metric-dictionary 同步更新

### 🚀 告警智能化（3 個新工具 + 1 個 Self-Service Portal）

**Alert Quality Scoring (`da-tools alert-quality`)**
- 4 項品質指標：Noise（震盪偵測）、Stale（閒置 14 天）、Resolution Latency（flapping 警告）、Suppression Ratio
- 三級評分（GOOD/WARN/BAD）+ per-tenant 加權分數（0–100）
- 輸出：text / `--json` / `--markdown`，CI gate：`--ci --min-score 60`
- 57 個測試，89.8% 覆蓋率

**Policy-as-Code (`da-tools evaluate-policy`)**
- 宣告式 DSL：10 種運算子（required / forbidden / gte / lte / matches / one_of ...）
- `when` 條件式、萬用字元目標（`*_cpu`）、dot-path 嵌套（`_routing.receiver.type`）
- Duration 比較、tenant 排除、error/warning 雙嚴重度
- CI gate：`--ci` 有 error 違規 exit 1
- 106 個測試，94.0% 覆蓋率

**Cardinality Forecasting (`da-tools cardinality-forecast`)**
- 純 Python 線性回歸（無 numpy）：趨勢分類（growing/stable/declining）+ 風險等級（critical/warning/safe）
- 觸頂天數預測 + 預計日期，可設基數上限（`--limit`）和預警天數（`--warn-days`）
- CI gate：`--ci` 有 critical 風險 exit 1
- 61 個測試，93.5% 覆蓋率

**Tenant Self-Service Portal (`self-service-portal.jsx`)**
- 三分頁 SPA：YAML 驗證（schema + routing guardrails）、告警預覽（滑桿模擬）、路由視覺化（樹狀圖）
- 瀏覽器端執行，零後端依賴，雙語支援（zh/en）

**Self-Hosted Portal (`da-portal` Docker image)**
- `ghcr.io/vencil/da-portal` — nginx:alpine 靜態 image，打包 24 JSX tools + Hub + Guided Flows + vendor JS
- 企業內網 / air-gapped 部署：`docker run -p 8080:80`，免 build step
- Volume mount 客製化：`platform-data.json`、`flows.json`、`nginx.conf`（含 Prometheus reverse proxy placeholder 解決 CORS）
- CI/CD：`portal/v*` tag 觸發 `release.yaml` 自動 build + push GHCR

### 🛠️ DX 自動化工具（+8 個新工具）

**Operations**
- **`shadow_verify.py`**：Shadow Monitoring 三階段驗證（preflight / runtime / convergence）
- **`byo_check.py`**：BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq）
- **`grafana_import.py`**：Grafana Dashboard ConfigMap 匯入（sidecar 掛載 + verify + dry-run）
- **`federation_check.py`**：多叢集 Federation 整合驗證（edge / central / e2e 三模式）

**Scalable Configuration Governance**
- **`assemble_config_dir.py`**：Sharded GitOps 組裝工具 — 多來源 conf.d/ 合併、SHA-256 衝突偵測、assembly manifest
- **`da_assembler.py`**：ThresholdConfig CRD → YAML 輕量 controller（Watch / One-shot / 離線渲染 / Dry-run）
- **ThresholdConfig CRD**（`dynamicalerting.io/v1alpha1`）：namespace-scoped + RBAC + printer columns

**DX 工具迭代**
- `validate_all.py`：`--profile` + `--watch`（CSV timing trend）、`--smart`（git diff → affected-check 自動跳過）
- `bump_docs.py`：`--what-if`（全 238 rules 審計）
- `generate_cheat_sheet.py` / `generate_rule_pack_stats.py`：`--lang zh/en/all` 雙語
- `check_doc_freshness.py`：false-positive 修正 + `--fix`
- `check_translation.py`：cross-dir + lang fix
- `check_includes_sync.py`：`--fix`（自動建立缺失 .en.md stub）

### 🎯 互動工具生態（0 → 24 JSX tools）

**工具矩陣**：23 個位於 `docs/interactive/tools/` + 1 個 `docs/getting-started/wizard.jsx`
- Config：Playground、Lint、Diff、Schema Explorer、Template Gallery
- Rule Pack：Selector、Matrix、Detail、PromQL Tester
- 運維：Alert Simulator/Timeline、Health Dashboard、Capacity Planner、Threshold Calculator
- 學習：Architecture Quiz、Glossary、Dependency Graph、Runbook Viewer、Onboarding Checklist
- 展示：Platform Demo、Migration Simulator、CLI Playground、Self-Service Portal

**基礎設施**
- **tool-registry.yaml**（單一真相源）→ `sync_tool_registry.py`（`make sync-tools`）自動同步 Hub 卡片 + TOOL_META + JSX frontmatter
- **platform-data.json**（共用資料源）：從 Rule Pack YAML 萃取（15 packs, 139R + 99A），JSX 工具 fetch 共用
- **jsx-loader.html**：瀏覽器端 JSX transpiler + `TOOL_META`（related footer）+ `__PLATFORM_DATA` 預載 + Guided Flow 模式
- **tool-consistency-check**（pre-commit）：Registry ↔ Hub ↔ TOOL_META ↔ JSX ↔ MD 一致性驗證

**Guided Flows**
- `flows.json` 多步引導流程（onboarding / tenant-setup / alert-deep-dive），`?flow=onboarding` 啟動
- Cross-step data（`__FLOW_STATE` + sessionStorage）、progress persistence、completion tracking
- Conditional steps + checkpoint validation（`__checkFlowGate()` Next 按鈕閘門）
- Custom flow builder：`?flow=custom&tools=...` Hub 互動式 builder，24 工具全覆蓋
- Flow analytics：進度條、完成率、drop-off 步驟偵測

### 🌐 Bilingual Annotations (i18n)

- **Rule Pack 雙語 annotation**：`summary_zh` / `description_zh` / `platform_summary_zh` — 三個 Pilot Pack（MariaDB, PostgreSQL, Kubernetes）
- **Alertmanager template fallback**：Go `or` function 優先中文、自動 fallback 英文（所有 receiver 類型）
- **CLI i18n**：`detect_cli_lang()` 偵測 `DA_LANG`/`LANG` → argparse help 雙語切換（23 個 CLI 命令）
- **check_bilingual_annotations.py**：Rule Pack 雙語覆蓋率驗證（pre-commit manual stage）

### 📄 文件全面重構

**結構重組**
- architecture-and-design.md 拆分為 6 個專題文件（benchmarks / governance-security / troubleshooting / migration-engine / federation-integration / byo-prometheus-integration）
- 3 個角色入門指南（for-platform-engineers / for-domain-experts / for-tenants）zh/en
- 全面雙語化：33 → 46 對 `.en.md` 文件
- MkDocs Material 站點：CJK 搜尋、tags、i18n 切換、abbreviation tooltips
- Glossary（30+ 術語）+ 5 ADRs + JSON Schema（VS Code 自動補全）

**內容修訂**
- 根 README (zh/en) 重寫：角色導向痛點敘事（Platform / Tenant / Domain / Enterprise）
- architecture-and-design.en.md：補 §2.3 Tenant-Namespace Mapping、修 §3.1（15 packs + `prometheus-rules-*` 命名）、補 Bilingual Annotations
- Benchmarks 重寫：5 輪實測數據統一採集（idle + under-load + routing + alertmanager + reload）
- 6 份文件精簡（avg -23%）：移除過時內容、手動 curl 改為 da-tools CLI 引用
- Scenario CLI 修正：`tenant-lifecycle.md` (zh/en) 修正 4 個不存在的 CLI flags
- Tool-map 重生成：62 個工具完整覆蓋（之前僅 18 個）

**文件 CI 工具鏈（13 tools）**
- `validate_mermaid.py` / `check_doc_links.py` / `check_doc_freshness.py` / `doc_coverage.py`
- `add_frontmatter.py` / `doc_impact.py` / `check_translation.py` / `check_includes_sync.py`
- `sync_glossary_abbr.py` / `sync_schema.py` / `generate_cheat_sheet.py` / `inject_related_docs.py`
- `validate_all.py`：統一驗證入口

### 🔒 Security Audit & Hardening

- **程式碼安全**：ReDoS 防護（regex 長度限制）、URL 注入白名單、SSRF scheme 白名單（http/https only）、Prototype pollution 過濾（`__proto__`/`constructor`）、YAML 100KB 上限、`os.chmod` 補齊
- **文件安全加固**：HTTP→HTTPS 範例、webhook 驗證升為 error、`--web.enable-lifecycle` 安全註解、Grafana 密碼警告、新增「生產環境安全加固」章節

### 🏗️ 專案結構正規化

- **scripts/tools/ 三層子目錄化**：62 個工具分入 `ops/`（30）、`dx/`（18）、`lint/`（13）+ root（1 + 1 lib）
  * Docker flat layout 相容（dual sys.path + build.sh 自動 strip）
- **JSX 工具搬遷**：22 個工具 `docs/` → `docs/interactive/tools/`，registry/flows/loader/hub 路徑同步
- **測試歸位**：`test_assemble_config_dir.py`、`test_da_assembler.py`、`test_flows_e2e.py` 統一搬入 `tests/`
- **generate_tool_map.py 重寫**：自動掃描 ops/dx/lint/root 子目錄

### 🧪 測試工程化（14 輪系統化重構）

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| 測試檔案 | 5 | 40 | +35 |
| 測試數量 | ~790 | 1,759 | +969 |
| Go 測試 | 97 | 110 | +13 |
| Coverage gate | 無 | 64%（`setup.cfg`） | NEW |
| Test markers | 無 | 5（slow/integration/benchmark/regression/snapshot） | NEW |
| Factories | 無 | 12（`factories.py` + `PipelineBuilder`） | NEW |

**關鍵里程碑**：
- Wave 5-6：pytest 遷移、SAST 掃描器（189 rules）、整合測試
- Wave 7-8：property-based tests（Hypothesis）、snapshot tests（18 JSON）、coverage gate
- Wave 9-10：factories 拆分、domain policy、deepdiff structured diff
- Wave 11-12：unittest→pytest batch migration、metric_dictionary fixture
- Wave 13：conftest re-export cleanup、duplicate removal、factory docstrings
- Wave 14-16：parametrize、scaffold snapshots、benchmark baseline、validate_all coverage
- Wave 17：coverage attack — baseline_discovery（31→55%）、backtest_threshold（32→70%）、batch_diagnose（49→71%）
- Wave 18：parametrize sweep — 合併重複測試方法

### 🛡️ 品質閘門

- **Pre-commit hooks**：0 → 12 個 auto-run + 5 個 manual-stage（schema / translation / flow E2E / jsx-babel / i18n coverage）
- **新增 hooks**：`tool-map-check`、`doc-map-check`、`rule-pack-stats-check`、`glossary-check`、`changelog-lint`、`version-consistency`、`includes-sync`、`platform-data-check`、`repo-name-check`、`tool-consistency-check`、`structure-check`、`doc-links-check`
- **Docker CI 修正**：build.sh 自動 strip sys.path hack + 觸發路徑 `**/*.py` + 3 個遺漏工具打包修正
- **Conventional Commits** + `generate_changelog.py` 自動化

### 📦 Dependency Upgrades

- **Prometheus**: v2.53.0 → v3.10.0（PromQL 相容性已驗證，15 個 Rule Pack 無影響）
- **Alertmanager**: v0.27.0 → v0.31.1
- **configmap-reload**: v0.14.0 → v0.15.0
- **Grafana**: 11.1.0 → 12.4.1
- **kube-state-metrics**: v2.10.0 → v2.18.0
- **Go**: 1.22 → 1.26.1（go.mod + Dockerfile + CI）
- **Frontend CDN**: React 18.2.0 → 18.3.1、Babel 7.23.9 → 7.26.4、Lucide 0.383.0 → 0.436.0

### 📊 Numbers

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| Rule Packs | 13 | 15 | +2 |
| Python 工具 | ~20 | 62 | +42 |
| da-tools CLI 命令 | 20 | 23 | +3 |
| JSX 互動工具 | 0 | 24 | +24 |
| 文件（docs/ .md） | ~20 | 68 | +48 |
| 雙語文件對 | 0 | 46 | +46 |
| Python 測試 | ~790 | 1,759 | +969 |
| 測試檔案 | 5 | 40 | +35 |
| Pre-commit hooks | 0 | 12 + 5 manual | +17 |
| Docker images | 2 | 3 | +1 (da-portal) |

### 📈 Benchmark（v2.0.0，15 Rule Packs，Kind 叢集）

**Idle-State（2 tenant，237 rules，43 rule groups）：**

| 指標 | v1.11.0 (13 packs) | v2.0.0 (15 packs) | 變化 |
|------|-------|-------|------|
| Total Rules | 141 | 237 | +96 |
| Rule Groups | 27 | 43 | +16 |
| Eval Time / Cycle | 20.3ms | 23.2ms | +2.9ms |
| p50 per-group | 1.23ms | 0.39ms | 改善 |
| p99 per-group | 6.89ms | 4.89ms | 改善 |
| Prometheus CPU | 0.014 cores | 0.004 cores | — |
| Prometheus Memory | 142.7MB | 112.6MB | — |
| Exporter Heap (×2 HA) | 2.4MB | 2.2MB | — |
| Active Series | ~6,037 | 6,239 | +202 |

**Go Micro-Benchmark（Intel Core 7 240H，`-count=5` median）：**

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 12,209 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 100,400 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 1,951,206 | 3,848,574 | 5,039 |
| ResolveAt_10Tenants_Mixed | 34,048 | 40,052 | 271 |
| ResolveAt_100Tenants_Mixed | 405,797 | 462,636 | 2,622 |
| ResolveAt_1000Tenants_Mixed | 5,337,575 | 5,258,548 | 26,056 |
| ResolveAt_NightWindow_1000 | 5,404,213 | 5,223,925 | 25,056 |
| ResolveSilentModes_1000 | 86,700 | 186,086 | 10 |

**Route Generation Scaling（Python `generate_alertmanager_routes.py`）：**

| Tenants | Wall Time | Routes | Inhibit Rules |
|---------|-----------|--------|---------------|
| 2 | 181ms | 3 | 2 |
| 10 | 196ms | 8 | 10 |
| 50 | 248ms | 41 | 50 |
| 100 | 327ms | 80 | 100 |

---

> **歷史版本 (v0.1.0–v1.11.0)：** 詳見 [`CHANGELOG-archive.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG-archive.md)
