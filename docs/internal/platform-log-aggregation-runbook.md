---
title: "Platform Log Aggregation Runbook"
tags: [internal, runbook, observability, federation, logs]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---

# Platform Log Aggregation Runbook

#539 Phase 1。federation-gateway 把 audit log 用 JSON 寫到 stdout
（ADR-020 IV-2f）；本 pipeline 把它搬進中央 log store，讓「過去 6h 這
顆 token 碰過什麼」這種 incident query 有得問。

```
federation-gateway (stdout JSON + stderr operational)
    │
    ▼
node DaemonSet (Vector)   ── helm/vector chart
    │  VRL: parse_json on success → log_type=federation_audit
    │       parse_json on fail   → log_type=gateway_operational
    │
    ▼
VictoriaLogs (single pod) ── helm/victorialogs chart
    │
    ▼
Grafana datasource / LogsQL
```

**Hard rule**：producer 永遠不直接 HTTP push 到 log store。gateway →
stdout、delivery → shipper。log-store 掛掉時 federation gateway 不能
被連累。

## 1. Deploy 順序

```sh
# 1) Store first — Vector 啟動時會嘗試握手 sink。
helm install victorialogs ./helm/victorialogs -n monitoring \
  --set persistence.size=20Gi \
  --set retentionPeriod=30d

# 2) Shipper second。
helm install vector ./helm/vector -n monitoring \
  --set victorialogs.host=victorialogs.monitoring.svc

# 3) Grafana datasource：plugin 用 GF_INSTALL_PLUGINS 自動裝
#    （k8s/03-monitoring/deployment-grafana.yaml 已加好）；
#    datasource provisioning 已寫進 configmap-grafana.yaml。
kubectl rollout restart -n monitoring deploy/grafana
```

驗 datasource 載入完成：

```sh
kubectl logs -n monitoring deploy/grafana | grep -i 'victoriametrics-logs-datasource'
# 期待看到「plugin loaded」之類的訊息；CrashLoopBackOff 通常是
# Grafana 連不上 grafana.com 抓 plugin —— air-gapped cluster 必須
# pre-bake 一個含 plugin 的 image，把 GF_INSTALL_PLUGINS 拿掉。
```

## 2. Smoke-test LogsQL（#539 §4 AC5）

從 Grafana **Explore → VictoriaLogs** 跑這幾條。每條都應該有結果；
0 結果代表 pipeline 中間斷掉，往下看 §4 troubleshooting。

| 用途 | LogsQL |
|---|---|
| 「現在有 log 進來」 | `*` |
| 「每個 tenant 的 audit 量」 | `log_type:federation_audit \| stats by (tenant_id) count()` |
| 「過去 6h 5xx 是誰」 | `log_type:federation_audit AND status:~"5.."` （時間範圍：6h） |
| 「JWT 拒絕的攻擊掃描」 | `log_type:federation_audit AND status:401 \| stats by (token_id) count()` |
| 「Envoy 操作層異常」 | `log_type:gateway_operational AND ("error" OR "warning")` |

**注意**：JWT-fail request 在 audit JSON 裡 **沒有 tenant_id**（jwt_authn
在 claim injection 之前就拒了），所以 `stats by (tenant_id)` 不會把它
們算進去。要找它們改用 `token_id` 或 `path`。

從 CLI 跑同樣的 query：

```sh
kubectl exec -n monitoring deploy/victorialogs -- \
  wget -qO- 'http://localhost:9428/select/logsql/query?query=log_type:federation_audit&limit=10'
```

## 3. Stream-field schema（#539 §3 load-bearing table）

VictoriaLogs 把 fields 分兩類：**stream fields**（定義 logical stream，
基數爆炸會炸索引）vs **data fields**（per-line、可查、不入 stream
index）。本 pipeline 的選擇凍結在 `helm/vector/values.yaml` 的
`streamFields`：

| field | 類 | 為什麼 |
|---|---|---|
| `app` | stream | container_name，bounded |
| `k8s_namespace` | stream | bounded |
| `log_type` | stream | 2 值（federation_audit / gateway_operational） |
| `tenant_id` | stream | bounded（百量級 MVP） |
| `status` | stream | enum，~7 個 HTTP code |
| `pod_name` | **data** | HPA churn 會炸 stream index |
| `token_id` | data | 高基數、短命 |
| `query` (PromQL) | data | 每個 request 都不同 |
| `path` / `method` / `ts` / `duration_ms` | data | per-request payload |

**改 `streamFields` 是破壞性變更**：已 ingest 的資料的 stream 樹會
被切，需要 re-index。改之前先評估保留 vs 重灌。

## 4. Troubleshooting

### 4.1 「Grafana 查不到 log」

```sh
# a) Vector 在每個 node 都跑？
kubectl get pods -n monitoring -l app.kubernetes.io/name=vector -o wide

# b) Vector 有沒有看到 gateway 的 pod？
kubectl logs -n monitoring -l app.kubernetes.io/name=vector --tail=50 \
  | grep -i 'federation-gateway\|added file\|matched'

# c) Vector → VictoriaLogs 連得到嗎？
kubectl exec -n monitoring deploy/victorialogs -- \
  wget -qO- 'http://localhost:9428/metrics' | grep -E 'vl_(rows_ingested|free_disk)'
```

### 4.2 「VictoriaLogs pod 起不來」

```sh
# 99% 是 PVC 沒 bind。
kubectl get pvc -n monitoring victorialogs-data
kubectl describe pvc -n monitoring victorialogs-data
```

storageClass 不對 → `--set persistence.storageClass=<your-class>` 重裝。
測試環境可以 `--set persistence.enabled=false`（資料隨 pod 蒸發，
NOTES.txt 會印警告）。

### 4.3 「Vector pod 在 read 失敗」

`kubernetes_logs` 讀 `/var/log/pods/...` 需要能讀 root-owned file。
本 chart 預設 `runAsUser: 0` + `DAC_READ_SEARCH` cap（其他 cap 全掉）。
如果叢集有 PSP/PSA 把 root 擋掉，需要：

- 切到 `--set containerSecurityContext.runAsUser=472` 並讓 host 把
  `/var/log/pods` group-readable，或
- 換 image 到 `timberio/vector:0.55.0-distroless-static`（有 `nobody`
  user），同上需 host 端配合，或
- 在該 namespace 例外放行 root。

### 4.4 升 Vector 版本時的 VRL 編譯爆炸

Vector ≥ 0.55 把 `unnecessary error coalescing operation` 從 warning
升成編譯錯，pod 直接 crashloop（首跑 #539 smoke-test 中招）。VRL
的 infallible 運算（例如 `merge(object!, object!, deep:true)`、
`to_string(string)`）後面**不要**接 `??` fallback —— 編譯器會說
「this expression can't fail」。升版前先 `helm template vector
./helm/vector | yq '... | .data."vector.yaml"' | vector validate
--config-yaml /dev/stdin` 本地驗一次。

### 4.5 Vector `data_dir` vs `readOnlyRootFilesystem`

Vector 預設 `data_dir: /var/lib/vector`（容器 root 下）；本 chart 開
`containerSecurityContext.readOnlyRootFilesystem: true`，兩者直接撞
「Could not create subdirectory ... Read-only file system」。所以
configmap.yaml 顯式設 `data_dir: /vector-data-dir`，對齊 daemonset.yaml
hostPath 掛載點。**改掛載路徑時兩處要同步**，否則 pod 起不來。

## 5. Capacity / Retention

預設 `retentionPeriod=30d` + `persistence.size=10Gi`，依 federation
audit 預估流量（~50 RPS × 1KB/line × 30d ≈ 130GB）**不夠長期跑**——
應該按環境調整 size。Re-estimate 規則：

```
size ≈ (audit_RPS × 1 KB × retention_seconds × 1.3 overhead) / compression_ratio
```

VictoriaLogs 平均壓縮比 ~10x，所以實務上 130GB raw ≈ 13GB on disk。
保守起見 `size = 30 GiB` 給 30d。

#552 chargeback query log 進來時（consumer #2）流量會翻倍以上，
那時要重新估。

## 6. Phase 2 — chargeback（#552 consumer #2）

Phase 2 的啟用步驟 / tenant attribution / failure modes / capacity 等內容單獨拆給 [`chargeback-aggregator-runbook.md`](chargeback-aggregator-runbook.md) —— 上線 chargeback 時看那一份就好，本 runbook 保留 Phase 1 + Phase 3 的 cross-cutting 內容。

重點摘要：
- Prometheus `query_log_file: /dev/stderr`（k8s/03-monitoring/configmap-prometheus.yaml）→ Vector `additionalSources[0]` tail prometheus pod → VictoriaLogs `log_type=prometheus_query_log` stream → daily 02:00 UTC CronJob 算 per-tenant CSV
- `tenant` label（不是 `tenant_id`）—— federation-proxy 注入的就是 `tenant`
- 平台自身 rule / alert eval 帶 `ruleGroup` field、VRL 強制砍 tenant，bucket 成 `tenant=platform`（不可計費）
- `exec_time_s` 單位是秒（不是 ms）

## 7. Phase 3 — SIEM fan-out（compliance branch）

VictoriaLogs 跟 Loki 一樣**不是 tamper-evident / WORM**。strict
compliance（legal hold, immutable retention）來了，**不要**換掉
VictoriaLogs —— 在 `helm/vector` values 的 `additionalSinks` 加一條
sink 指向 SIEM 就好，pipeline 上游不變（Phase 2 加的 `prometheus_query_log`
stream 也跟著一起 fan-out，不需要二次配線）。

### 7.1 啟用步驟

> Smoke-test fixture（mock-siem + Vector values overlay）見 [`docs/internal/examples/log-aggregation-smoketest/`](examples/log-aggregation-smoketest/) —— 不在 prod cluster 跑，但本機 kind 重現紅隊 T2-2 / §2 isolation 走它。

```sh
# Splunk HEC 範例（其他 SIEM 改 sink type 與 endpoint 即可）
helm upgrade vector ./helm/vector -n monitoring --reuse-values \
  -f - <<EOF
additionalSinks:
  - name: splunk_compliance
    type: splunk_hec_logs
    inputs: [demux]                  # 拿 VRL-tagged stream，不是 raw
    endpoint: https://splunk.example.com:8088
    default_token: \${SPLUNK_TOKEN}  # via envFrom secret
    _buffer_when_full: drop_newest   # 鐵則：不可設 block
    _buffer_max_events: 10000
EOF
```

驗 fan-out 兩條都收到：

```sh
# 看 VictoriaLogs（既有 query 一樣）
kubectl run vlq --rm -i --restart=Never --image=busybox:1.36 --quiet -- \
  wget -qO- 'http://victorialogs.monitoring.svc:9428/select/logsql/query?query=*&limit=1'

# 看 SIEM 端是否到貨（依 SIEM 操作；若是 mock HTTP 就 kubectl logs）
```

驗 back-pressure isolation（SIEM 掛了 VictoriaLogs 不能被連累）：

```sh
# 模擬 SIEM 不可達：把 endpoint 改成擋住的 IP，或 scale SIEM 到 0
helm upgrade vector ./helm/vector -n monitoring --reuse-values \
  --set 'additionalSinks[0].endpoint=https://10.0.0.1:8088'  # blackhole
sleep 60
# 期望：vector pod 仍 Running、VictoriaLogs 仍持續收 row。
# Vector internal_metrics 會記 dropped-events 對應指標（精確 metric
# 名稱依 Vector 版本不同，0.55 之後是 `buffer_*_events_total` 系列；
# 開 metrics.enabled=true 後從 :9598/metrics 撈）。
kubectl get pod -n monitoring -l app.kubernetes.io/name=vector  # Running
```

### 7.2 鐵則 + 失敗模式

| 規則 | 為什麼 |
|---|---|
| `_buffer_when_full: drop_newest`（預設） | SIEM 慢/掛時，新事件對該 sink **被丟**，但 VictoriaLogs **絕不**被 back-pressure（#539 §2）。chart 自動 inject 這個 buffer block 除非 entry 自己有 `buffer:` |
| **絕對不要** `_buffer_when_full: block` 在 fan-out sink | block 會 back-pressure 上游，VictoriaLogs 也卡。**只有**當 SIEM 是 system of record（compliance-only mode、VictoriaLogs disabled）才合理 |
| `inputs: [demux]` 不要寫 `[kubernetes_logs]` | demux 是 VRL-tagged stream（有 `log_type`/`tenant_id`），raw 是 Envoy 原始行 —— compliance 通常要前者 |
| 不要叫 `name: victorialogs` | 跟內建 primary sink 撞名 → helm lint 抓得到（duplicate YAML key），fail-loud |

### 7.3 「Compliance 的責任落在哪」

加 SIEM fan-out **不會**把 VictoriaLogs 變 tamper-evident —— 平台這
端的責任就是把同一份 demuxed stream **餵給** SIEM；以下三件事**完全
在 SIEM 端**：

| 屬性 | VictoriaLogs | SIEM |
|---|---|---|
| Tamper-evidence（hash chain / signed ts） | ❌ | ✅（Splunk / Sumo / Elastic + immutable storage） |
| Legal hold（operator 用 kubectl 也無法刪） | ❌（`wget DELETE` 砍得掉） | ✅（SIEM RBAC + retention policy） |
| Immutable retention window | ❌（`-retentionPeriod` operator 設） | ✅（compliance 規範） |

SIEM owner（通常是 SecOps 團隊）扛 chain-of-custody；平台只負責
delivery 不漏 row（buffer.events_dropped metric 是該被告警的）。

### 7.3.1 §2 vs compliance — 真實 trade-off（#566 T3-1）

§2 hard rule（`drop_newest`，SIEM 掛時丟新事件 / 不阻 VictoriaLogs）
跟 compliance（**不可漏任何 row**）**根本衝突**。SIEM down window 內
時機性 attacker 動手，VictoriaLogs 仍有 row 但 SIEM 沒收到 → SIEM
端的 forensic timeline 真空、operator 砍 VictoriaLogs row 後沒
evidence chain。

三條設計路徑、各自的取捨：

| 模式 | `_buffer_type` | `_buffer_when_full` | 取捨 |
|---|---|---|---|
| **availability-first（預設）** | `memory` | `drop_newest` | SIEM 掛 → 丟 row 不阻流。VictoriaLogs 仍正常。**Vector pod 重啟也丟 in-memory buffer 的 row**（X-3 timeline gap） |
| **compliance-degraded（多數合規場景）** | `disk` | `drop_newest` | SIEM 掛 → 寫 disk buffer。pod 重啟不丟（hostPath 持久）→ SIEM 恢復後 drain。max_size 滿才開始 drop。**§2 仍守**（VictoriaLogs 不被影響） |
| **compliance-strict（SIEM 是 SoR）** | `disk` | `block`（自帶 `buffer:` block override 才能設） | SIEM 掛 → Vector 整個 back-pressure，VictoriaLogs 也卡。**§2 被打破**，但只在 VictoriaLogs *沒部署*的 compliance-only 拓樸下合理（log primary path 走 SIEM、不需要 VictoriaLogs） |

**chart 預設走 availability-first**（同 Phase 3 行為）。compliance 客戶
進來時：第一步切 `_buffer_type: disk`（X-3 修了 OOM-kill timeline 洞），
看 disk buffer 容量夠不夠 cover 該客戶 SLA-定義的 SIEM downtime
window。第二步才考慮 strict mode（disable VictoriaLogs）—— 那是
產品定位變更等級。

### 7.4 Failure modes

| 症狀 | 原因 | 救法 |
|---|---|---|
| Vector pod CrashLoop after adding additionalSinks | sink type 拼錯 / 必填欄位漏（splunk_hec_logs 漏 default_token 等） | `kubectl logs vector-XXX`；Vector 啟動時印 config validation error；改 values 後 `helm upgrade` |
| VictoriaLogs 突然慢下來 | 不小心把 fan-out sink 設 `block` | `helm get values vector` 查 `_buffer_when_full`；改回 `drop_newest` |
| SIEM 端只收到 raw Envoy 行不是 JSON | `inputs:` 設成 `[kubernetes_logs]` 而非 `[demux]` | 改 inputs，`helm upgrade` |
| dropped-events metric 一直爆高 | SIEM 處理慢於進入速度 | 調大 `_buffer_max_events`；或 SIEM 端擴容 |

## 7.5 Egress / tamper hardening（#566 batch D）

紅隊 T4-1/T4-2（惡意 `additionalSinks` 把 audit row 外洩）+ T3-2/T3-3
（`kubectl edit cm` 篡改 VRL / aggregator script 無 Git 軌跡）是
**兩條不同攻擊路徑**，要兩種不同防線。

### 7.5.1 GitOps 路徑：egress allowlist gate（已實作）

`scripts/tools/lint/check_log_egress_policy.py`（`make lint-egress`）在
**PR / pre-deploy 階段**渲染 log-aggregation charts，擋三件事：

1. `additionalSinks[].{endpoints,uri}` 的 host 不在 allowlist
2. 覆寫 `VECTOR_*` 保留 env（非 downward-API fieldRef 形式）
3. sensitive-named env（`*TOKEN*`/`*KEY*`/`*SECRET*`…）用字面 `value:`
   而非 `valueFrom.secretKeyRef`

```sh
# 對 committed env-values 跑（CI 已在 Python Tests job 跑同邏輯的 pytest）
make lint-egress ARGS="--values helm/values-prod-vector.yaml --allow-host splunk.example.com"
```

政策也以 illustrative rego 鏡像在 `policies/examples/log-egress.rego`
（repo 路徑、不入 mkdocs 站台；同其他 rego examples），core rules 寫成解耦形式 —— 未來真要上
**OPA Gatekeeper runtime admission** 時只需薄 wrapper 接 `AdmissionReview`，
規則不必重寫。

### 7.5.2 Runtime 路徑：GitOps-only write boundary（production checklist）

egress gate **只擋 GitOps PR 路徑**。有 `kubectl` / `helm --set` 權限的
operator 直接改叢集，gate 不會被觸發。Production 真正的防線是 RBAC +
GitOps self-heal，**不在 chart 內、是部署叢集的責任**：

| 控制 | 做法 | 效果 |
|---|---|---|
| **人類無寫權** | Production 的人類 RoleBinding **不得**有 `update`/`patch`/`delete` on `deployments`/`configmaps`/`secrets`；只有 ArgoCD / Flux 的 ServiceAccount 能寫 cluster | 直接 `kubectl edit cm` 改 VRL → 被 RBAC 拒（T3-2/3-3 真正 fix） |
| **GitOps self-heal** | 把平台自身的 Helm release 也納入 ArgoCD `selfHeal: true` / Flux `interval` 自動同步（**注意**：目前 `gitops-deployment.md` 的 GitOps scope 只到 `conf.d/` 租戶配置，**尚未**涵蓋平台 Helm chart —— 要擴 scope） | 即使有人手動篡改 ConfigMap，GitOps controller 數分鐘內覆寫回 Git 版本，把 window-of-compromise 壓到 sync interval |

> ⚠️ kind reference cluster 裡大家都 cluster-admin，上述 RBAC 邊界是
> **production 部署 checklist**，不在 demo 環境 enforce。但這是把 T3
> 從「偵測」升級到「預防 + 自癒」的唯一正解 —— 比自建 drift-detector
> CronJob 更省、更可靠（借力既有 GitOps controller，不增元件）。

## 7.6 殘餘風險 / 未強化項（deferred — 追蹤於 #566）

紅隊 9 項已交付 5 項 + X-2 schema seam（見 §7.3.1 / §7.5）。以下 4 項
為**已知、被接受、暫不強化**的殘餘攻擊面 —— 依「不超前需求建設」原則，
待 compliance 客戶 / 威脅模型需求浮現再 pick off。incident triage 時要
知道這條邊界**目前不被偵測 / 不被擋**：

| # | 殘餘風險 | 目前邊界 |
|---|---|---|
| **T2-3** | Vector DaemonSet 以 root + `DAC_READ_SEARCH` 讀整個 node 的 `/var/log/pods` —— 被 RCE 後可讀同 node 上**所有** pod stdout，非僅 gateway | 未縮限;fix shape 是 distroless + `nobody` user + host 端 `/var/log/pods` group-readable 協調，需 host-side 配合 |
| **T3-2/3** | `kubectl edit cm` 篡改 chargeback script / Vector VRL（改演算法 under-bill、改路由）**不留 GitOps commit trace** | §7.5.2 的 RBAC + GitOps self-heal 是正解;但在該邊界**未 enforce** 的環境（kind demo、或 GitOps scope 尚未涵蓋平台 Helm chart 者）**無 in-cluster drift detector** —— 疑似竄改須手動 diff live ConfigMap vs chart baseline |
| **X-2** | 被 RCE 的 Vector 可偽造與真實**無異**的 audit row（timestamp / tenant_id / query 皆可填），SIEM 無法 attest「此 row 真的來自 gateway」 | 無 producer-side 簽章;#568 已預留 schema seam，full chain-of-custody 是 gateway-side 架構改動，待真實 compliance 客戶觸發（屆時開 ADR） |
| **T5** | chart image 以 tag pin（`timberio/vector:0.55.0-…` 等），非 `@sha256:` digest;upstream registry 被攻陷即拉到惡意 binary | chart-local digest knob 已有（#567），但**無 repo-wide 強制 hook** —— 全域 enforce 屬 platform 供應鏈 backlog，非 #539-specific |

> 狀態與 fix shape 以 [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566) 為 SSOT;本表只列「operator 該知道的殘餘邊界」，不重複 issue 內的 severity / rollout 細節。

## 8. (b) 租戶淨化投影（ADR-021 Phase 1 / #609）

讓租戶在平台上**就地查自己的**營運 log（federation audit），又**看不到**基礎設施拓樸或他租戶的列。資料平面（本 PR）鋪好後，查詢授權平面（gateway `victorialogs` mode + tenant-api AccountID 配發）才把租戶接上。完整設計見 [ADR-021](../adr/021-tenant-log-query-federation.md)；本節是 operator 的 how-to。

> **可見度治理 / 租戶端視角（#609 PR-5）**：本節講投影**怎麼運作**；「哪些 stream / field
> 對租戶可見」的策展邊界見 [2-Tier 日誌可見度 Catalogue](log-visibility-2tier-catalogue.md)，
> 租戶端操作（取 logs token、查詢、cold-start）見
> [租戶日誌查詢 onboarding 指南](../integration/tenant-log-query.md)。

### 8.1 Fan-out（雙寫，非搬移）拓樸

```
demux (VRL，注入 log_event_id)
   │
   ├─▶ victorialogs sink ─────────▶ VictoriaLogs (0:0)   平台完整副本（全欄位，#539 現狀不變）
   │
   └─▶ tenant_project (remap)      eligibility gate + tenant_id→AccountID 富集 + allowlist 淨化（重建）
          │                        （drop_on_abort/error + reroute_dropped:false = fail-closed）
          └─▶ tenant_route (route, by account_id)
                 ├─▶ vl_tenant_<id> sink (固定 AccountID header) ─▶ VictoriaLogs (AccountID:X, ProjectID:0)
                 └─▶ _unmatched（無 sink 消費 = 丟棄）
```

- **平台完整副本續留 `0:0`**——平台 ops 的跨租戶查詢面**不變**；`gateway_operational`／JWT-fail／`suspicious_audit`／`prometheus_query_log` 列**永遠只在 `0:0`**，不進任何租戶分區。
- **租戶淨化投影是疊加層**——只有「`log_type=federation_audit` 且帶有效 `tenant_id`」的列會被投影。租戶只有 Day-0 起的歷史（新 feature，無需 backfill）。

### 8.2 啟用：把 registry 配發投影進 `tenantProjections`

`tenant_id(str) → AccountID(uint32)` 的 SSOT 是 Git 帳號 registry（`_account_registry.yaml`，PR-1 #887：`next_account_id` + `allocations: {tenant_id: uint32}`）。本 PR (b) 為**靜態 N-sink**：把已配發的租戶 `tenant_id: account_id` **逐筆抄進** `helm/vector` values 的 `tenantProjections`（一份經 review、GitOps 版控的 registry 投影）。Phase 2 (a) 才從 registry **render 時自動產生**（config-from-SSOT，解決海量租戶不 scale）。

```yaml
# helm/vector values（或 values-prod-vector.yaml overlay）
tenantProjections:
  - tenantId: "tenant-alpha"   # 必須等同 audit JSON 的 .tenant_id（JWT tenant_id claim）
    accountId: 1000            # 抄自 _account_registry.yaml 的 allocations，勿自創、勿重用退租 id
  - tenantId: "tenant-beta"
    accountId: 1001
```

> ⛔ **配發紀律（違反即跨租戶洩漏）**：`accountId` 一律從 registry 抄；registry 單調發號、**永不回收**（退租後重用同 id → 新租戶讀得到舊租戶 retention 窗內殘留 log）。`tenantId` 打錯（如 typo）→ 該租戶查無自己的 log（fail-closed，不會誤給他人）；`accountId` 抄錯 → 可能落他租戶分區，**這是唯一會洩漏的人為錯誤**，PR review 必對照 registry。
>
> **render-time 守門**（對抗 review 補）：**重複** `accountId`（兩租戶混入同分區）或**重複** `tenantId`（mis-route）→ `helm template` 直接 `{{ fail }}`；**非整數/quoted** `accountId`（會 render 出無效 VRL → 靜默空分區）→ `values.schema.json` 在 render 時擋。但「抄成**另一個合法租戶**的 id」是唯一守門擋不到的、仍須 PR review 對照 registry 抓。
>
> 預設 `tenantProjections: []` → 投影**整個關閉**，pipeline 為 byte-相容 #539 單租戶行為（不 render 任何 `tenant_project`/`tenant_route`/`vl_tenant_*`）。

### 8.3 敏感欄位淨化（allowlist，fail-closed）

`tenant_project` 在寫租戶分區**前從零重建** event，只保留 `tenantProjectionKeepFields` 列舉的安全欄位（`tenant_id`/`status`/`method`/`path`/`query`/`token_id`/`duration_ms`/`response_flags` + 結構性注入的 `account_id`/`log_event_id`/`timestamp`），**其餘一律結構性排除**。租戶分區的 `_msg` 由這些安全欄位 **re-serialize**，原始 audit 行（內含 `upstream`=後端 IP:port）直接丟棄。

> ⛔ **為何 allowlist 而非 denylist**（對抗 review 修正——曾為 denylist 而漏）：demux 把整包 gateway audit JSON `merge(deep)` 進 event root，所以 denylist（`del()` 固定清單）是 **fail-open**——它接不到原始 `.message` 字串（內嵌 `upstream` 後端 IP），也接不到 gateway／未來 producer 新增或巢狀的任何欄位。allowlist **fail-closed**：未列舉者一律不進租戶分區。**新增要給租戶看的欄位＝改 `tenantProjectionKeepFields`**——這是資安相關變更，須確認該欄位不洩漏平台基礎設施或他租戶資訊（把「記得 drop」的負擔反轉成「明確 opt-in」）。

> ingest-time 重建不可逆（改清單只影響改後新進的列；舊資料等 retention 自然汰換）。動態 read-time strip 留 Future Work。`projection_tests.yaml` 的 allowlist 案例以 gateway **真實 json_format**（含 `upstream`）+ 注入值 seed，斷言租戶副本只剩安全欄位、`upstream`/raw-message/注入 id 皆無。

### 8.4 `log_event_id` 跨分區 join SOP（淨化不拉長 MTTR）

值班拿到租戶報修（其畫面只有淨化過的列、無 node 資訊）時：

1. 從租戶提供的列取 `log_event_id`（time-sortable UUIDv7，`0:0` 與租戶副本**同值**）。
2. 在 `0:0`（平台完整副本）反查它，拿回完整 node/pod 拓樸：

```sh
kubectl exec -n monitoring deploy/victorialogs -- \
  wget -qO- 'http://localhost:9428/select/logsql/query?query=log_event_id:"<那個 id>"&limit=5'
# 不帶 AccountID header = 查 0:0（平台分區），可見 pod_node / pod_name / 完整 audit
```

> `log_event_id` 在共用 `demux` 階段**無條件**注入（平台所有、**覆寫** producer 自帶值——防 producer 經 deep-merge 控制 join key、污染值班的 `0:0` 反查），故必在兩副本且為平台產生的 UUIDv7；它在 `tenantProjectionKeepFields` 內（移除它會斷 join 鏈）。用 UUIDv7 而非隨機 v4：VictoriaLogs 時序優化，k-sortable id 讓 `0:0` 全域檢索 join 便宜（Gemini fold-in）。audit-signing seam 落地後可改回「verified-origin 才 idempotent 保留 producer 值」。

### 8.5 VictoriaLogs Layer-1 查詢護欄

租戶查詢是疊在 single-pod store 上的**新讀負載**（#539 容量只估 ingestion）。LogsQL 無 Prometheus 式 sample cap，主護欄是 time-range + 執行時間上限（`helm/victorialogs` values 的 `search:`）：

| flag | 值 | 防護 |
|---|---|---|
| `-search.maxQueryTimeRange` | `7d` | 擋無時間過濾／過寬查詢（log 世界主要 blast-radius）。≤ `retentionPeriod` |
| `-search.maxQueryDuration` | `25s` | ⛔ 單查詢執行上限，**必須 < gateway route 30s**——cascade 讓 VictoriaLogs **先** abort、不留 zombie query 佔並發槽（Gemini fold-in） |
| `-search.maxConcurrentRequests` | `6` | 並發上限（RAM/CPU backstop；亦兜 multi-replica gateway 限流非對稱——不論幾個 replica 放行，實際並發執行封頂） |
| `-search.maxQueueDuration` | `10s` | 並發滿時排隊等待上限 |

調整走 `--set search.maxConcurrentRequests=N`；或用 `extraArgs`（render 在 search flags **之後**，last-flag-wins 可覆寫）。**改 `maxQueryDuration` 勿觸及/超過 gateway 30s**，否則 cascading-timeout 失效。

### 8.6 驗證 / fail-closed 自證（`vector test` 入 CI）

語法 + 行為都進 CI（`tests/shared/test_vector_projection_vrl.py`，本機重現）：

```sh
# 1) 語法（codify 本 runbook §4.4 手動步驟）
helm template vector ./helm/vector -n monitoring \
  --set 'tenantProjections[0].tenantId=tenant-alpha' --set 'tenantProjections[0].accountId=1000' \
  --set 'tenantProjections[1].tenantId=tenant-beta'  --set 'tenantProjections[1].accountId=1001' \
  | yq '.[] | select(.kind=="ConfigMap" and (.metadata.name|test("vector-config"))) | .data."vector.yaml"' \
  > /tmp/rendered-vector.yaml
vector validate --no-environment /tmp/rendered-vector.yaml

# 2) 行為（餵極端 payload 斷言確定行為）
vector test /tmp/rendered-vector.yaml helm/vector/tests/projection_tests.yaml
```

`projection_tests.yaml` 守的不變式：**negative assertion**（餵帶全部敏感欄位的 mock，斷言租戶副本中 `pod_node`/`pod_name`/`node_name`/`pod_ip`/`host` **確實不存在**——測「移除了」非「有產出」）／空與未知與 parse-error `tenant_id` → 僅 `0:0`（`no_outputs_from` 租戶 routes）／`log_event_id` 兩副本同在／`gateway_operational`+query-log 僅 `0:0`／跨租戶 routing 不交叉。

> ⚠️ **CI 前置**：這兩步要 `vector` binary 在 PATH。`helm` 已在 `python-tests` job（`azure/setup-helm`）；`vector` **本 PR 已補裝**（`ci.yml` 的 `Install Vector` step，checksum-pinned，照 promtool 先例）——`vector validate` + `vector test` 現為 CI **真 gate**（非 SKIP；否則安全關鍵的 allowlist/fail-closed 行為在 CI 零覆蓋）。

### 8.7 Fan-out 韌性 / 資源隔離（Gemini #894）

- **Tenant sink 不阻塞 shared pipeline**：每個 `vl_tenant_<id>` sink 配 `buffer: {when_full: drop_newest}`（`tenantProjectionBufferMaxEvents` 可調）。VictoriaLogs 對**單一租戶**分區背壓時，該 sink **本地丟最新**，不會經 `tenant_route → demux` 反壓卡住 `0:0` 寫入與其他租戶（head-of-line blocking）。`0:0` 是 source of truth、投影為 best-effort；bounded memory buffer 同時擋 noisy-neighbor 的 RAM 突波（無 OOM）。需 restart-durability 的 operator 可改 disk buffer。
- **query 長度上限**：`tenantProjectionMaxQueryBytes`（預設 8 KiB）`truncate` 租戶副本的 `query`——防 500 KB 超長 query 撐爆 `encode_json` 或 VictoriaLogs 單行限制。
- **Stream-field 高基數**：`tenantProjectionStreamFields` 僅低基數維度（`tenant_id`/`log_type`/`status`）；⛔ **絕不**放 `query`/`token_id`/`path` 等動態值（每個 distinct 值建一條 stream → RAM 爆）。
- **K8s 資源 / 排程**（ops）：fan-out 增加 Vector CPU。確保 `resources.requests/limits` 對 N 租戶有餘裕，並考慮給 ingestion DaemonSet 一個 `PriorityClass`（node 資源枯竭時優先驅逐低優先 batch job、保 ingestion 存活）。⛔ **Phase 2 (a) 觸發時這從「考慮」升為硬不變式**（Gemini #905）：成千上萬 pod 應用 log 湧入時，Vector CPU 因大量 `uuid_v7()` 配發 + `encode_json` 階躍式暴增；若不綁高 `PriorityClass`，Kubelet 在 node starvation 時可能誤殺 shipper → **全叢集日誌斷流**。Phase 2 排程時 ingestion DaemonSet 的高 PriorityClass 列為生產 hard requirement。
- **無聲丟棄的可觀測性（PR-4 已補）**：fail-closed 的 `abort`+`drop_on_abort` 讓異常列**無聲消失**——平台須監控 Vector 原生 `vector_component_discarded_events_total{component_id="tenant_project"}`（registry 未同步／惡意 payload 導致大量 drop 時要有能見度，而非等租戶報修）。#609 PR-4 落地 `TenantProjectionFanoutDiscardSpike`（`configmap-rules-platform.yaml` `federation-audit` group，warning）。⚠️ **標籤是 `component_id` 非 `component`**（Vector internal_metrics 原生標籤；本文件原寫 `component` 是非正式 prose，照 sibling `VectorBufferEventsDropped` 用 `component_id`），且需 `helm/vector metrics.enabled=true` + Prometheus scrape。⚠️ **這是粗粒度 spike tripwire 非精準 gap 偵測**：此 component-level counter 把**所有** abort 原因合計，而設計上**多數** demux 列為 non-audit（`gateway_operational`／`prometheus_query_log`／JWT-fail／`suspicious_audit`）被合法 drop，故**不可**用 `> 0`（恆真噪音）——改用 `rate > 5/s` 持續 `15m`（floor 須照各 gateway 營運 log 量的 steady-state drop rate 調）。精準的 per-account「可對映租戶投影缺漏」偵測需新增 per-partition row-count metric（option b），列 **defer-with-trigger**。⛔ **但精準 runtime 偵測器是 band-aid 非根治**：desync 的根因是 **config drift**（`tenantProjections` 落後 `_account_registry.yaml`），真正的修法是 **Phase 2 (a) config-from-SSOT**——從 registry **自動生成** `tenantProjections`、drift 根本不可能發生，屆時 option b 即不需要。在那之前流程防線＝onboarding guide 的配發紀律（§8.2）+ 本 coarse tripwire 接大規模事件。**trigger（任一）**：首次真實 registry desync 事故、租戶報修「看不到自己的 log」、或 Phase 2 (a) 自動生成排程時（屆時重估是否還需 option b）。需 mtail / metric 對照見下 §8.8。

### 8.8 租戶日誌查詢可觀測層（ADR-021 #609 PR-4）

把 victorialogs-mode 的**查詢平面**做可觀測（與 §8.7 的 **ingestion 平面**互補：§8.7 盯「投影斷掉」，本節盯「誰在查、查得順不順」）。三件交付（皆與 ADR-020 metrics-plane 對稱）：

- **mtail metric**（`helm/federation-gateway/files/federation-audit.mtail`，與 `tenant_federation_requests_total` 同檔同 sidecar）：
  - `tenant_log_query_requests_total{account_id, project_id, status}`（counter）——從**同一條** Envoy audit access log 累計租戶日誌查詢請求。
  - `tenant_log_query_duration_ms{account_id, project_id}`（histogram，buckets 5…25000ms）——查詢延遲分布（Gemini fold-in a），來源 access log `duration_ms`(=`%DURATION%`，整數 ms)。counter 不帶延遲，故延遲必為獨立 metric。
  - **log-query vs metrics-pull 判別**：用「`account_id` 為非空正整數」(`(?P<account>\d+)`)——metrics-pull token 無 account_id claim → Envoy render 空字串 → 不 match → 自然排除（**不**靠 path allowlist，免與 envoy.yaml 同步漂移）。
  - **`project_id` 來源 = 常數 `"0"`**：access log **無** project_id 欄位，Phase 1 (b) 平台營運 log 固定 ProjectID=0（對齊 Vector `tenantProjectionProjectId=0`）。Phase 2 (a) 引入 ProjectID=1（租戶**應用** log）時，需在 `&audit_json` 補 `project_id` 欄位、把 mtail 從常數改成擷取值——metric 標籤集已預留此維度，dashboard/alert 不必重塑。
  - ✅ **mtail 程式編譯已驗**（對抗 review #900）：用 pinned `mtail 3.0.8`（== Dockerfile `MTAIL_VERSION`）跑 `--compile_only`，含「注入語法錯誤」正控確認檢查真在驗 grammar，real 檔乾淨；histogram `by … buckets …` 順序 + comma-index 經官方 `parser.y` 查證合法。dev container/host 皆**無 mtail binary** 故 CI 未自動驗——**follow-up**：加 `mtail --compile_only` CI job（與 promtool gate 同層），讓未來 mtail 改動不靠人工。
- **Grafana dashboard**：`k8s/03-monitoring/tenant-log-query-dashboard.json`（uid `tenant-log-query`，獨立 dashboard、非塞進 federation-audit）——per-account 查詢量／status 分布／**延遲 heatmap + P95**（platform-wide 與 per-tenant）。⚠️ **PromQL topology-label 陷阱**：所有 `histogram_quantile` 的聚合**保留 `le`**（`sum by(account_id, le)`／`sum by(le)`），缺 `le` 會靜默回 NaN——drift-proof golden（`tests/dx/test_tenant_log_query_dashboard.py`，從 JSON 讀 query、promtool 驗、含 `le`-present shape lint）釘住。檔案部署沿用 sibling（`federation-audit-dashboard.json` 同樣為 standalone、非 configmap-grafana 內聯）。
- **alert `TenantLogQueryRejectionRateAnomaly`**（`configmap-rules-platform.yaml` `federation-audit` group，warning）：>50% 某 account 的查詢被拒（rate_limited／auth_failed／bad_request）持續 15m + floor ~1 拒/min。key 為 `account_id`（audit line 帶數值 account_id 非 conf.d tenant 名，故**無** `tenant_metadata_info` join，改用 min-rate floor 當 idle 守門）。`sum by(account_id)` 同時套分子分母（ratio 對齊；裸 `sum` 會 strip account_id 致 mis-pair——topology 陷阱）。

promtool fire/no-fire 與上述 §8.7 spike alert 同放 `tests/rulepacks/tenant-log-query-platform{.rules,_test}.yaml`（extracted-copy 模式，比照 `platform-watchdog`；`configmap-rules-platform.yaml` 非由 `rule-packs/` 生成、無 regen）。

**⚠️ 觀測層的限制與調校（對抗 review #900 + Gemini fold-ins）**：

- **兩條 alert 的預設啟用不對稱**：`TenantLogQueryRejectionRateAnomaly` 靠 mtail sidecar（`auditLog.enabled` 預設 **on**）→ 預設可 fire；但 `TenantProjectionFanoutDiscardSpike`（§8.7）靠 Vector `internal_metrics`（`helm/vector` `metrics.enabled` 預設 **off**）→ **預設靜默、永不 fire**。開啟 (b) 投影時務必一併設 `metrics.enabled=true` + Prometheus scrape，否則投影斷掉無告警。
- **FanoutDiscardSpike 的 floor 是猜測值、須量測**：`>5/s` 非依實測 baseline 調——`tenant_project` 對**每條** non-audit 列（gateway_operational／JWT-fail／未對映／suspicious）都 `abort`、全計入 `vector_component_discarded_events_total`；忙 gateway 的 operational-log 量很容易 >5/s → **false-fire**（summary 已改為「先比對 baseline 再懷疑 desync」避免誤導 triage）。部署後 query 該 component 的 steady-state rate、設 floor = baseline × N。長線可改 **dynamic threshold**（`avg_over_time` + stddev，如 `rate > baseline + 3*stddev`）自適應，繫 §8.7 精準偵測的 defer-with-trigger（Gemini）。
- **mtail 在 log flood 下的殘留丟失**：logrotate 用 **rename + Envoy `/reopen_logs`**（非 copytruncate，rotation 當下**不丟行**），但極端洪流（瞬間海量 401/403）下 mtail（CPU limit `100m`）parse 落後到 >`keep` 次 rotation 時，最舊的 renamed 檔會在讀完前被刪 → 靜默丟數 → rejection alert 分子採樣不足而延後/漏 fire。**load-test 50MB/10s 洪流、量 mtail CPU + 丟失率**，必要時放寬 mtail CPU limit；順帶監控 mtail 自身 `mtail_progs_processing_errors_total` 與延遲（Gemini）。
- **metric 能見度邊界**：counter 只計**通過 JWT 後**的拒絕；過期/壞簽/錯 audience token 由 `jwt_authn` 在 account_id 注入**前** 401、**不計入**（攻擊噪音，由 jwt_authn 自身 stats 觀測，與 ADR-020 一致）。`account_id`<1000 的誤發 token 若被 Lua 403 仍會計入並標成該低 id（實務 tenant-api 不發 <1000，latent）。3xx/1xx status 不落任何 bucket（VictoriaLogs read API 幾乎不回，刻意不計）。

### 8.9 投影 GATE degrade/enforce 觀測與復原（ADR-021 Phase 2(a) / #908 PR-3a）

§8.7 盯「投影被 abort 丟棄」，本節盯**更上游**的「投影 GATE 本身降級/卡死」——`tenantProjections` 與 conf.d `_account_registry.yaml` 不符（unique-but-wrong accountId 跨租戶洩漏類）或 boot 時讀不到 registry 時，fail-closed gate 的反應與**復原 SOP**。

**觀測管線（為何不是 node-exporter / 不是 per-node absent）**：gate init-container 把判定寫進 pod-local emptyDir 的 Prometheus textfile（`--metrics-file /gate-metrics/gate.prom`，metric `vector_tenant_projection_gate_info{category,mode}`）；一支長駐 **exposer sidecar**（`serve_metrics.py`，同 image、`command` override）把該檔以 HTTP 重新 serve，經**獨立 headless Service**（`<vector>-projection-gate-metrics`，gated on `tenantProjections`、**不**綁 `metrics.enabled`）被 Prometheus `monitoring-components`（role:service）scrape。本叢集**無 node-exporter / textfile collector**，Vector 自身 exporter 又只讀 `internal_metrics`，故走 sidecar——且 sidecar 與 pod 同生死 → pod 死時 series **變 absent 而非殘留 stale-"ok"**（crash 不會假性 resolve 真實 mismatch）。Vector 是 DaemonSet、每 node 掛**相同** registry+projections → 每 node 判定**相同**，故 alert 一律 `max by(...)` **跨 pod 聚合**（per-node absent 非必要，且 role:service scrape 本就不給穩定 per-node series）。⚠️ **enforce 死鎖時 sidecar 不會啟動**（init 非零退出 → pod 卡 Init → Vector 與 sidecar 皆未起 → verdict metric **無人 serve**），故那條 alert 改走 **KSM**（見 §8.9.3）。

對應三條 alert（`configmap-rules-platform.yaml` `federation-audit` group，promtool 契約 `tests/rulepacks/platform-projection-gate{.rules,_test}.yaml`）：

#### 8.9.1 `VectorProjectionGateMismatch`（critical，degrade 模式抄錯號碼）

某 `tenantProjections` entry 的 accountId 與 registry 配發不符（render-time `{{fail}}` 唯一性 + integer schema 都放行的「唯一但錯」洩漏類）。degrade 模式下 gate **丟掉**租戶投影片段 → Vector 只送平台 0:0（**洩漏已被擋下**，從未寫到錯分區），但租戶淨化投影**靜默關閉**。復原：

```bash
# 1) 抓出抄錯的那對 tenantId→accountId（gate 把違規逐條印到 stderr）
kubectl logs <vector-pod> -c projection-gate
#    → 例：tenantId 'tenant-alpha' projects accountId 1001 but the registry allocates 1000 ...
# 2) 以 registry 為 SSOT 對帳、修正 helm values 的 tenantProjections（或 --set override）
#    切勿反向改 registry——registry 是單調權威，錯的是投影。
# 3) 重新部署/套用後滾動 DaemonSet 讓 gate 重新驗證（projection 改動本就會經
#    checksum/config 觸發 rollout；手動補一刀保險）：
kubectl rollout restart ds/<vector> -n <ns>
```
復原後該 node 重啟、gate 判定回 `ok` → 片段重新 place → alert 自動 clear。

#### 8.9.2 `VectorRegistryUnreadableAtBoot`（critical，ghost race / registry 讀不到）

gate boot 時無法**信任** registry（degrade 到 0:0，`category="registry_unreadable"`）。最陰險的觸發＝**fresh-deploy 順序競態**：Vector 早於 registry ConfigMap 同步就啟動（`optional:true` 讓 pod 起得來）→ gate 讀到缺檔→降級，而 **one-shot-at-boot** 使它在 registry 之後到達仍**維持降級**（症狀：全綠、但租戶路由永久靜默關）。也可能是 registry 格式壞/schema 較新（fail-closed 拒驗）。復原：

```bash
# 1) 確認 registry ConfigMap 存在且 well-formed（projectionGate.registry.configMapName 指的那個）
kubectl get configmap <registry-cm> -n <ns> -o jsonpath='{.data._account_registry\.yaml}' | head
# 2) 確認無誤後，滾動 DaemonSet 讓 one-shot gate 對「現在存在」的 registry 重跑：
kubectl rollout restart ds/<vector> -n <ns>
```
⚠️ **這是唯一需要人工介入離開 degrade 的設計點**（gate 不自動重驗；重新啟用租戶路由是 operator 的明確動作，非自動）。若反覆 ghost-race，檢查部署順序（registry ConfigMap 應先於 Vector DaemonSet apply）。

#### 8.9.3 `VectorProjectionGateStuck`（critical，gate init 卡死 → rollout 全卡）

**任何**讓 gate init **無法跑完**的狀態：pod 卡在 Init → Vector 與 exposer sidecar **皆不啟動**（故 verdict metric 無人 serve，degrade alert 看不到 → 本條改靠 KSM `kube_pod_init_container_status_waiting_reason{container="projection-gate", reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|InvalidImageName|CreateContainerConfigError"}`）。DaemonSet 上 `maxUnavailable` 會**卡死整個 rollout**（rolling 中的 node 失去日誌、未滾到的 node 維持舊 config＝split-brain）。三類成因：
- **`enforce` + config-bug mismatch**（原始場景）：gate 依設計非零退出 → `CrashLoopBackOff`
- **image 打錯/清壞**（任何模式；Gemini #970 空殼 review 揪出——原本只 match CrashLoopBackOff 對這類**零告警**）→ `ImagePullBackOff`/`ErrImagePull`/`InvalidImageName`
- **args/entrypoint 壞掉、staging ConfigMap 缺失**（任何模式）→ `CrashLoopBackOff`/`CreateContainerConfigError`

（更名註記：原名 `VectorProjectionGateEnforceDeadlock`——matcher 擴到 image-level 卡死後「enforce」成為 misnomer（degrade 模式也會中），比照 #944 名實相符原則趁早改。）復原：

```bash
# 1) 先看卡住的 waiting reason（分辨 config-bug vs image vs configmap 缺失）
kubectl describe pod <stuck-vector-pod> -n <ns> | grep -A3 "State:\|Reason:"
# 2a) CrashLoopBackOff（enforce config-bug）→ 看 gate 退出原因、REVERT 錯的 tenantProjections
kubectl logs <vector-pod> -c projection-gate --previous
# 2b) ImagePullBackOff 類 → 修 projectionGate.image（tag/repo 打錯、或 overlay 清壞了 image）
# 2c) CreateContainerConfigError → staging/registry ConfigMap 名字或存在性
kubectl delete pod <stuck-vector-pod> -n <ns>   # 修好後，讓它以正確 spec 重排
```
📝 **解除滯後（正常現象，勿驚慌）**：本 alert 的 expr 用 `max_over_time(...[5m])` 防抖（見規則註解）。你修好 config、看到新 pod 回 `Running` 後，**alert 仍會續鳴約 5 分鐘才自動 resolve**——因為那條回溯窗還會掃到剛才的 waiting 樣本，`> 0` 在窗口滑出前仍成立。這是換取告警穩定（不因 backoff 間短暫重啟而漏發）的合理代價，不是卡住。
**模式取捨**：若「硬卡 rollout」比「fail-available 退回 0:0」更糟（多數平台是），用預設 `degrade` 模式——它把 config-bug 抄錯轉成 §8.9.1 的可觀測降級而非死鎖（但 image/configmap 類卡死與模式無關，兩種模式都會中本 alert）。`enforce` 只適合「寧可全斷也不要錯路由」的偏執環境。

**觀測層自身的盲點（誠實標註）**：(a) 若 exposer sidecar 全掛（或 Service/scrape 斷），§8.9.1/8.9.2 的 verdict-metric alert 會失明——但那同時是一場 Vector 全面故障，另有其面向；KSM 路徑（§8.9.3）不受影響。(b) `tenantProjections` 未啟用時不部署 sidecar/Service，故**不**設「verdict metric absent」哨兵 alert（否則每個未用投影的部署都假性 fire）；gate 觀測只在投影啟用時存在。

#### 8.9.4 anti-silent-disarm：ValidatingAdmissionPolicy（#908 PR-3，防 gate 被靜默移除）

§8.9.1-8.9.3 盯 gate **執行後**的降級/死鎖；本節是**部署時**的防線——防有人把 `projection-gate` init-container 從 live 的 Vector DaemonSet **編輯掉**（`kubectl edit`／Kustomize/GitOps overlay 拿掉它）而 tenant routing 管線還在＝**靜默解除** fail-closed 跨租戶洩漏 gate（#945 mtail 教訓的 K8s 版）。

- **機制**：[`k8s/03-monitoring/validatingadmissionpolicy-projection-gate.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/k8s/03-monitoring/validatingadmissionpolicy-projection-gate.yaml) — 一條 `ValidatingAdmissionPolicy`（API-server 內建、無 webhook deadlock、GA 1.30）+ Binding。CEL 斷言：一個**還掛著 gate `registry` volume**（＝ tenant projection 仍啟用）的 Vector log-shipper DaemonSet **必須**含名為 `projection-gate` 的 init-container；否則違規。matchConditions（registry volume）+ 綁定的 objectSelector（`app.kubernetes.io/name=vector` + `component=log-shipper`）確保只評估「有 gate 的 Vector DaemonSet」，單租戶無投影者跳過（零誤報）。
- **⚠️ Day-1 是 `validationActions: [Warn, Audit]`（非 Deny）**：這是 repo 第一條 VAP，且 dev loop 無真叢集可驗證 CEL 的 match/allow 行為。Warn+Audit 經 K8s reference 查證**永不阻擋請求**（即使 CEL runtime error，只要無 `Deny` action 就只回報不強制）→ blind ship 零 blast-radius。**訊號通道（誠實認識）**：`Warn` = HTTP warning header——`kubectl apply/edit` 當場印黃字，是**主訊號**；`Audit` = API **audit event 的 annotation**（`validation.policy.admission.k8s.io/...`），只進 audit log **且叢集要先配好 audit pipeline**——⛔ **不會**產生 `kubectl get events` 可見的 core Event。
- **⚠️ Day-1 偵測盲區（獨立 review 揪出，誠實標註）**：對「**GitOps overlay 拿掉 init-container**」這條主要威脅，Warn+Audit 的實際覆蓋**近乎為零**——Argo/Flux 會吞掉 API warning（頂多留在 controller log），audit annotation 依賴多數叢集沒配的 audit pipeline；且 init 被移除後 `gate.prom` **從未被寫**、verdict metric absent，而 §8.9(b) 刻意不設 absent 哨兵 → §8.9.1-8.9.3 的 runtime alert 也蓋不到。**Day-1 的 GitOps-disarm 防線實質只剩 PR review**；本 policy 在升 Deny 前主要價值＝攔 `kubectl` 互動路徑 + audit 佐證。**Promotion trigger：任一真叢集（kind/staging 即可）可用時立即跑下方檢查表升 Deny**，不要讓 Warn+Audit 變成永久狀態。
- **升級 Deny 檢查表（真叢集驗證後）**：(1) `kubectl apply` 一個正常 gated Vector DaemonSet → **不**該 warn；(2) `kubectl edit` 拿掉 init-container → **終端當場出現黃色 Warning**（勿用 `kubectl get events` 驗證——見上，Audit 不產 Event；有 audit pipeline 者可另查 audit log annotation）；(3) apply 一個單租戶（無 registry volume）Vector → **不**該 warn；(4) apply 無關 DaemonSet（kube-proxy 等）→ policy 完全不評估。四項都對後，把 Binding 的 `validationActions` 從 `["Warn","Audit"]` 改成 `["Deny"]`（Deny 與 Warn 互斥，是**取代**非新增）。⛔ 升 Deny 時**同步重新決定 `failurePolicy`**：「Fail 永不 block」只在 Warn+Audit 下成立——翻成 Deny 後，任何 CEL eval error（如結構異常的物件在 mid-path traversal 出錯）會變成**硬拒**；要嘛接受（fail-closed）、要嘛改 `Ignore`（fail-open），要有意識選。同時考慮給 Binding 加 `namespaceSelector` 限縮到 Vector 所在 namespace——cluster-wide Deny 下，別團隊若巧合湊齊同組標籤 + 一個叫 `registry` 的 volume 會被誤擋。⛔ 未經真叢集驗證**勿**直接上 Deny：CEL 若有誤會把**全叢集** Vector 部署擋死。
- **威脅模型（誠實 scope）**：擋的是「操作者善意誤刪 live gated DaemonSet 的 init-container（volume 還在）」。**Out of scope（列全）**：(a) 惡意 cluster-admin 同時拿掉 volume+init、或降級 pre-gate chart（兩者都不 render）；(b) **保名神經化**——`kubectl patch` 把 `projection-gate` 的 image/args 換成 no-op 但名字不動 → policy 只驗 presence-by-name，全綠但 gate 已解除；(c) **兩步 label-strip**——update 1 只剝 DaemonSet 標籤（gate 完好，policy pass；註：單次 update 同時剝標籤+剝 init **會**被抓，objectSelector 對 old/new 任一 match 即評估）、update 2 再剝 init（binding 不 match → skip）。沒有任何叢集內 admission policy 能約束控制該叢集的人；(b)(c) 屬蓄意繞過，防線是 GitOps PR review。offline 驗證（manifest 結構 + CEL 邏輯 model + 整條 CEL 全文釘死）見 `tests/shared/test_projection_gate_vap.py`；真 enforcement 靠上述真叢集檢查表。
- **部署面註記**：本 manifest 放在 `k8s/03-monitoring/`，會隨 getting-started 的 `kubectl apply -f k8s/03-monitoring/*.yaml` loop 一併安裝——需 **K8s ≥1.30**（GA API）與 **cluster-scope** 權限（admissionregistration 資源非 namespaced）；1.29- 叢集或 namespace-scoped 安裝者會在這兩個檔上吃 error（可安全跳過，policy 是縱深非必需）。

## Refs

- 源 issue：[#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)
- 產生 audit JSON 的 Envoy access_log：`helm/federation-gateway/files/envoy.yaml`
- ADR-020 §Audit log
- ADR-021 §Audit log + anomaly metric（#609 PR-4 觀測層）
- ADR-021 Phase 2(a) 投影 GATE 觀測（#908 PR-3a，§8.9）：[#908](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/908)
- Consumer #2：[#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)
