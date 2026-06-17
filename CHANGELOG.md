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

- **合成探測 sinkhole route 對接介面（ADR-025 synthetic-probe interop）**：平台預留一條 Alertmanager sinkhole route——任何帶 `component="synthetic-probe"` 的告警**保證**路由到專屬 `synthetic-receiver` 且 `continue:false`，讓客戶用**自己現有的** blackbox / synthetic 探測器送測試告警「穿過」平台 Alertmanager、端到端驗證投遞鏈，且**零風險**（絕不外溢到人類頻道 / 吵醒 on-call）。route 經既有 `generate_alertmanager_routes.py` injector 注入並 pin 在 index 2（Watchdog→Custom→synthetic-probe，撐過 `--apply` route-REPLACE）、base `configmap-alertmanager.yaml` 同序、路由 orchestration + amtool 測試守護。設計邊界：平台**不自建**探針（這是 interop 對接面 only；自建探針仍 defer-with-trigger）。承 ADR-025；見 [synthetic-probe-interop.md](docs/integration/synthetic-probe-interop.md)。
- **後端相容性 PromQL/value parity smoke（VictoriaMetrics；ADR-025 deferred「後端相容性」Part 1）**：把「平台 backend-agnostic」從行銷宣稱變成**可驗證 CI 事實**——新增 CI job 對真實 VictoriaMetrics 跑代表性 rule-pack golden，斷言它與 Prometheus 評估**我們編譯出的 expr**（含 `and on()` 拓樸 join / `group_left` 富集 / `label_replace` / `max by(tenant)` 等 idiom + 多層 recording-rule 鏈）結果一致（label-set + fire/no-fire + 值 epsilon）。**復用既有 promtool golden 當 Prometheus 參考**（SSOT、不漂移）。業界工具 `promql-compliance-tester` 評估後不適用（固定通用題庫 ≠ 編譯產出、1hr scrape 資料模型 ≠ CI smoke、非 offline）→ hybrid-policy DIY-exception。確定性護欄：固定 epoch + VM `-retentionPeriod=100y` + 唯一時間窗 + `nocache=1`；`VM_PARITY_REQUIRE=1` 令 VM 連不上即硬 fail（不靜默假綠）；VM image digest-pin（#851 政策）。**staleness/時間語意仍 defer**（需真實 gap，trigger：首客戶整合自有後端）。見 [backend-compat-baseline.md](docs/internal/backend-compat-baseline.md)。
- **租戶歸因的節點健康告警 `NodeNotReady`（#809）**：kubernetes rule pack 新增 node-NotReady 告警，把 node-scoped 的 `kube_node_status_condition`（無 `tenant` 標籤）經穩定的 **node→tenant 映射** recording rule（`tenant:node_owner:info`，由 `kube_pod_info` + Running-pod 過濾建出）歸因到**該節點上每個有運行中 Pod 的租戶**並 fan-out。預設全開、warning severity（migration-parity，客戶遷移前即有節點級保護），maintenance 模式可 opt-out。對抗式 review 護欄：映射僅取 Running pod（排除 Evicted/Failed 幽靈 Pod 誤報、且賦予 Pod 重調度後告警自動 resolve 語意）；fan-out 用 `and on(node)` 而非 `* group_left`（避免 NotReady 值為 0 導致 `>0` 永不觸發）；Alertmanager 既有 `group_by:[alertname,tenant]` 化解 AZ 災難告警風暴。promtool 多租戶 fan-out + 幽靈 Pod 排除 + 健康節點 + maintenance 四向回歸（#692 P0 第②片；topology-join 機制，pod→PVC 等其他映射為後續）。
- **Custom Alerts `==` 等值運算子（threshold recipe 限定，any-match 語意）**：recipe `op` 新增 `==`，精確比對「以指標**值**表達的狀態/錯誤代碼」（例：MariaDB semi-sync errno 1236）— 補上 #810 的表達力缺口。`==` 採 **any-match**：逐 replica 原始值先比對再聚合，故**任一**實例等於該碼即觸發——多副本持不同碼不會互相掩蓋（對抗式 review 揪出 max-then-== 會靜默漏報，#819）。護欄：`==` 僅 `threshold` recipe 可用，其餘 recipe（rate/ratio/p99/forecast/absence）兩側 validator + JSON-schema if/then 三層一致拒絕（fail-loud，避免前後端腦裂）；等值告警文案用「等於設定代碼」（`%.0f`）。Python compiler / Go preflight / JSON schema / portal enums / 治理契約五處 lockstep，golden vector + promtool（含多-pod 掩蓋回歸）雙向驗證（#692 P0 第①片）。
- **Custom Alerts value-form 狀態碼安全採用指引（cookbook + shape-explicit 範例，#832）**：補上 #810 的 last-mile——`==`（#819 已出貨）之外，新增「以指標**值**表達狀態/錯誤碼」撰寫實務（`custom-rule-governance.md` §8）：value→label 首選解法 + 決策樹 + 多碼用 label-form `selectors_re` regex + 重塑不了 exporter 時的 **SRE-mediated** relabel 退路。並把 **exporter 存活性**寫成通用主題（dead-exporter 靜默是所有 value-based 告警通病、非 `==` 獨有）：配 `absence` recipe 補盲，但**取決於 exporter shape**（連續型才配、稀疏型配了反誤報），且 `absence` 為 tenant 聚合（抓全副本缺席、非單副本死亡）、**高基數指標需 `selectors` 限縮掃描**（否則 `count_over_time` 掃全 series，VictoriaMetrics 等後端易記憶體／CPU 峰值；Day-2 外審補強）。example tree 增 `process_status_code` 的 Shape-X `==`+`absence` 安全配對範例（shapes 9→11）。schema 契約 `expect_continuous_series` defer-with-trigger（首個 live `==` 採用）。
- **ADR-025（提案）平台 alerting-plane 自我存活性**：記錄一個獨立平面的決策——告警系統（Prometheus / Alertmanager）自己死掉時要能被**外部**察覺。決策：Watchdog 心跳經獨立置頂路由送平台**外部**監測點（「沒收到才告警」的反向設計，外部 TTL 留緩衝避免抖動誤報）；斷網改用叢集**外部** pull-based 健康檢查（非 K8s 內建 probe）；HA 與大規模儲存維持 backend-agnostic 交由 operator（目標客戶自帶大規模時序後端）。承 #832 的 liveness 討論分出（監控平面，非租戶側 `absence`）。狀態 proposed、實作未啟動；canary 租戶 / 規則 linter / 合成探測列 defer-with-trigger。
- **ADR-025 MVP 實作：Watchdog 心跳 + 外部 dead-man's-switch（#838）**：補上「平台告警系統自己死掉沒人知道」的盲點。新增永遠 firing 的 `Watchdog`（`expr: vector(1)`、`severity:none`）+ 一條**置頂、零聚合、固定頻率**（`routes[0]`、`group_by:[alertname]`、`group_wait:0s`、`repeat_interval:3m`、`continue:false`）的 Alertmanager 路由，把心跳送到 operator 自備的**外部**監測點；URL 為機密，經 `webhook_configs[].url_file` 指向掛載的 Secret（禁明文，免踩 secret-scan）。路由由 `generate_alertmanager_routes.py` 注入 index 0、撐過 `--apply` 的 route-REPLACE（且 receiver 的 `url_file` 不被覆寫）。另加**內部互補告警** `AlertmanagerWebhookNotificationsFailing`（webhook 送不出時先給內部信號，區分「平台死」vs「心跳管路壞」）。抑制免疫機械化：任何 `inhibit_rules.target_matchers` 不得 match `alertname="Watchdog"`，於兩條 render path（assemble / `--apply`）對 base+generated 合併集 **fail-closed** 驗證（Silence 端無法機械強制 → 進 operator 指南）。狀態 proposed→in-progress；operator 合約見 [告警平面自我存活性 Operator 指南](docs/integration/alerting-plane-self-liveness.md)。canary 租戶 / 規則 linter / 合成探測仍 defer-with-trigger。
- **租戶磁碟 recipe inert 偵測 `CustomRecipeDiskInert`（#692 P0③ W2）**：kubernetes rule pack 新增 per-tenant sentinel，把自助磁碟告警（forecast/ratio over `kubelet_volume_stats_*`）的 silent-dud 變 **fail-loud**——CSI 未實作 NodeGetVolumeStats、或 kubelet volume-stats 未歸屬 `tenant` 時，recipe 編得過卻在真叢集**永不 fire**（#731-class 盲點）；此 sentinel 在租戶**宣告了磁碟 recipe 且有 Running pod、但歸屬後的 volume-stats 缺席**時觸發。護欄：per-tenant 隔離（健康租戶不掩蓋 inert 租戶——早期 global-count draft 的 masking bug 由 promtool 多租戶案 C 守護）、either-absent（available/capacity 任一缺即觸發，ratio 兩者皆需）、`max by(tenant)` 做 HA replica dedup（同 #731 contract 要求 user_threshold 用 exact `=` metric 比對）、`kube_pod_status_phase{phase="Running"}` 排除 ghost pod（對齊 #809）、maintenance opt-out；**dual-perspective annotation**（租戶白話 summary + SRE `platform_summary` 三層排查序，dev-rule #10）。promtool 7 案（含多租戶 masking 回歸）；rule-pack alert 數 120→121。
- **pint Prometheus 規則靜態檢查進 CI（ADR-025 deferred「規則 linter」項）**：採用 OSS [`pint`](https://cloudflare.github.io/pint/) linter（thin wrapper `check_pint.py` + repo-root `.pint.hcl`，對齊 hybrid lint policy「adopt OSS engine, don't DIY」）守 rule-pack source。**唯一 hard-gate 的高 ROI check 是 `alerts/template`**——機械化攔截「聚合砍掉 alert template 用到的 label → 告警永遠靜默不觸發」這個本 repo 燒過 5× 的類別（至今只靠手寫註解 + 1 個 regression test 守）。對抗式驗證：probe rule `sum(...)+template 用 tenant` 被抓、`sum without(instance)` 控制組放行、新增非豁免名規則照樣 exit 1。idiom-noisy checks（`absent()`-sentinel 的 always-firing / 防護性 `or vector(0)` / 跨群 recording-rule 依賴）於 `.pint.hcl` disabled；平台級 `*ExporterAbsent`/`Inert` sentinels（required-labels policy 強制帶 `tenant` 但 expr 刻意聚合掉）於 `.pint.hcl` 中央 registry 豁免（name-scoped，不掩蓋真 bug）。`--offline`（CI 無需 Prometheus），baseline 0 blocking、deterministic（非 git-diff 依賴）。見 [pint-lint-baseline.md](docs/internal/pint-lint-baseline.md)。
- **CI 工具二進位安裝加上 SHA-256 供應鏈把關**：`ci.yml` 對所有以 `curl` 下載的 release binary（promtool / hadolint / pint / kube-linter）在 extract/install **之前**先比對 pinned SHA-256，mismatch 即 fail（best-effort 步驟仍照舊落 docker fallback），避免被竄改 / 重推 / 損毀的下載物直接在 runner 上執行——回應 #843 CodeRabbit「pint install 無 checksum」並擴及同類所有安裝點。把關共用 `scripts/ops/_verify_download.sh`（codify 一次、四處呼叫；附 Linux 自測 `tests/ops/test_verify_download.py`）。pin 為 TOFU digest（kube-linter 該版只發 cosign `.sig`、無 sha256 檔，故 digest 直接 pin）。docker fallback image 的 digest-pin 見下一條。
- **CI lint 的 docker fallback image 改用 digest-pin（供應鏈 sweep Part 2，承上條）**：`check_pint.py` / `check_iac_helm.py` / `check_iac_vibe_rules.py` 的 docker fallback（pint / kube-linter / helm / hadolint；`check_k8s_manifests.py` 經 import 共用 kube-linter image）由 mutable tag 改為 `repo:tag@sha256:<digest>`（multi-arch index digest）——上游同 tag 被重推 / 竄改也無法替換鏡像。各 digest 經 registry manifest API 解析，並逐一 **by-digest 複驗**（HTTP 200 + Docker-Content-Digest 相符）；各 `*_VERSION` 常數保留（pint version-sync 測試照過、其餘無 image-string 解析）。binary 安裝仍為主路徑、docker 為 best-effort fallback。
- **租戶磁碟告警的 rollout 前哨防線（#692 P0③ W3）**：把「宣告了 disk recipe 但平台收不到 tenant-attributed `kubelet_volume_stats`」的盲點補成**誠實三層**（author→onboarding→runtime，承 W2 的 runtime sentinel）。(1) reference `configmap-prometheus.yaml` 補上 CSI-gated 的 kubelet volume-stats scrape job（apiserver node-proxy）+ `namespace→tenant` relabel，平台 out-of-box 就能產出租戶歸屬的磁碟指標（非 CSI 叢集 keep 0 series、無害）；(2) `byo_check.py` 新增 onboarding step——對 live Prometheus 查「宣告 disk recipe 的 running 租戶是否真有 volume-stats 到貨」，把 sentinel shift-left 到部署前（running-pods guard：未部署的租戶不誤報）；(3) `compile_custom_alerts.py` 編 disk recipe 時印 author-time prerequisite notice。**對抗式 review 定調**：**不做**靜態 scrape-config lint——它驗 config 形狀而非 metric 流動（CSI/RBAC/relabel 不符時綠燈卻仍 inert＝false confidence，違背 pint/operator/mixin「scrape coverage 一律 runtime 驗」的業界實踐）。順補 W2 sentinel 盲點：`CustomRecipeDiskInert` declared-leg 加 `kubelet_volume_stats_used_bytes` 的 OR-of-exact leg（認得 usage-based disk recipe、過 #731 contract）+ 把 sentinel 的 `db-.+` 重新註解為**刻意的 scope 選擇**（對抗式複審糾正：原稱「broaden 是 no-op」是錯的——user_threshold 的 `tenant` 是 conf.d id 非 namespace-relabel，broaden 會開始對非-db 租戶誤觸；正確理由是 scope-by-design，與 scrape-side db-* keep 一致）。promtool 11 案（+used_bytes 雙向 + both-recipes 單一觸發 + non-db-scope 鎖 #9 決策）；thin-provisioning 謊報列 accepted residual（需 node_filesystem 交叉檢查、界外）。
- **跨租戶閾值分布治理 Dashboard（#655，#659 last-mile activation epic 首件）**：新增 Grafana dashboard `k8s/03-monitoring/fleet-threshold-distribution.json`，把早已 scrape 但無人消費的 `user_threshold{tenant,metric,component,severity}`（值即閾值）cash out 成「**跨租戶治理視角**」——對某 `(metric, severity)` 畫出全平台閾值分布（histogram + P5/P50/P95 時序 band），一眼看出設太嚴（alert fatigue）或太鬆（保護不足）的租戶。**引入業界最佳實踐**：離群偵測用 **Tukey 1.5×IQR fences**（只標真正離群、群體健康時為空）而非固定「P95 以外」（永遠機械標 5%）；統計量用**穩健的 median/IQR** 而非易被離群值污染的 mean/stddev；雙尾並陳、**方向不硬編**（`metric > threshold` vs `<` 語義相反，交由 SRE 對照 rule pack；tenant-agnostic）。三個 template 變數 `$metric/$severity/$component` 把每次比較鎖在單一尺度（不同 metric 閾值尺度天差地別）。誠實標註盲點：**disable 態不發 series**（resolve `continue`），最裸奔的「關掉告警」租戶在此不可見。頂列 P50 給出全租戶已設閾值的『共識中心』，與 [recommender #656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656)（取個別租戶觀測指標的歷史百分位）互補——本被動視角先用分布資料判斷 Day-2 真痛點，再決定後續投資。doc 導覽見 [grafana-dashboards.md](docs/grafana-dashboards.md)（Dashboard 3）。並附 **drift-proof promtool 回歸測試**（`tests/dx/test_fleet_threshold_dashboard.py`：從 dashboard JSON 讀出 query、代入測試變數、配合成 user_threshold fixture 跑真 Prometheus 引擎驗 golden——mutation 自證可攔語意破壞；Grafana shape lint 守 panel/transform/gridPos）。**誠實 bounding 離群法可靠度**（對抗式 best-practice 複審揪出）：Tukey fences 在 **mode-heavy（多數租戶吃 default → IQR=0 標光所有客製者，最常見情形）** 與**小樣本**兩種情形退化——Tenants panel 加紅/黃/綠樣本充足度色階、離群表降為「統計提示非 ground truth」並導向 robust 的「Δ from median」表 + histogram，兩種退化邊界由測試 golden 固定（robust 相對偏差法列 defer-with-trigger）。**無障礙合規收尾（ADR-012 / WCAG 1.4.1，#655 可關單前提）**：原頂列「樣本充足度」與「Outliers」狀態為**純紅綠背景色**編碼，違反 repo 自家 ADR-012（嚴重度不得只靠顏色，須配符號+文字）→ 改以 Grafana value-mapping 走**符號+文字**通道（樣本充足度 ❌ Sparse／⚠ Marginal／✓ Adequate；Outliers `✓ 0`／`⚠ ≥1`——**每個顏色階都有符號對應**，含告警態），顏色降為冗餘強化，紅綠色盲使用者亦可判讀；此編碼由純-JSON a11y golden 固定（斷言 symbol-text 數 ≥ 顏色階數、不需 promtool、到處可跑），防日後靜默退化。
- **租戶磁碟 IOPS / 吞吐告警（per-container，#692 P0④）**：租戶可對磁碟 I/O 暴衝（runaway query / backup storm）設 `rate` recipe over cAdvisor `container_fs_*`。reference cadvisor scrape 補上 container_fs 緊 keep（reads/writes 的 ops + bytes 共 4 個）+ `namespace→tenant` relabel + drop `container=""`/`POD`（防 pod-root 重複計）；recipe 框架零改動（`rate` 沿用）。**對抗式 + 業界（Brendan Gregg USE method、cAdvisor #3588/#1702）複審定調的誠實邊界**：此信號 **per-CONTAINER 非 per-PVC**，且**網路儲存（NFS/EFS）走 network stack 繞過 cgroup blkio → `container_fs`=0**；故定位為 **baseline-relative 異常**信號，而非 absolute saturation（後者是 node 級 %util / latency、屬 platform-SRE、無法 per-tenant 拆解）。誠實性靠 **codified fidelity gate**：`byo_check` Step 5 對宣告 IOPS recipe 的 *running* 租戶查 container_fs 是否到貨，**不到貨即 fail-loud**（逮 blkio-bypass / 未 scrape）；**刻意不設 runtime sentinel**（cAdvisor 恆在、inert 必為平台側 → per-tenant 派發是錯觀眾，與 W2 disk-fill 的 tenant-side cause 性質不同）。compile author-time notice + BYO onboarding docs（ZH+EN，含 fidelity-gate SOP）；byo_check 27 案。**完成 #692 P0③/④ disk 全系列（W1–W4 + IOPS）**。
- **Runtime Canary 設計就緒 + CI demo（ADR-025 deferred「runtime canary」項收尾）**：補上「Watchdog 看引擎活性、pint 看作者時規則正確性，但租戶自訂告警的『編譯→投遞』**執行期**活性無人守」的盲點設計。新增 [Runtime Canary 設計](docs/design/runtime-canary.md)（ZH+EN）：保留假租戶走**真實** conf.d→exporter→compiler→Prometheus 鏈、必觸發 + `mode:silent`，其**消失**由 dead-man's-switch meta-alert `CustomAlertPipelineCanaryDown`（刻意盯 core recording rule、非 `absent(ALERTS)`——後者會被 `CustomRecipeSilent` sentinel 掩蓋 → 漏報）page。config-locus 定為 `conf.d/` GitOps + 保留租戶（**不**寫死 `k8s/`——那會繞過最易靜默壞的編譯環節 → false-green 退化成第二個 Watchdog）。並**誠實修正** ADR 原「壞租戶隔離＝故意弄壞設定 canary 仍編得過」敘述：本平台靠**兩層**達成——(1) 編譯/CI **fail-closed + 指名出錯檔**（壞設定整批擋下、永不部署）、(2) 執行期 **per-tenant row 獨立**（`max by(tenant)` + `on(tenant) group_left`）。附**經真實編譯器產出、CI promtool 跑**的 demo（`tests/rulepacks/runtime-canary{.rules,_test}.yaml`，三案：活性／隔離／dead-man's-switch）。**常駐部署仍 defer**（heartbeat emitter + meta-alert 接真實 on-call；trigger：重大編譯/路由重構前佈署當安全網，或首個 prod pipeline 事件後防再犯）。見 [Runtime Canary 設計](docs/design/runtime-canary.md)。

### Fixed

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
