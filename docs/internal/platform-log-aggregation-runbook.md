---
title: "Platform Log Aggregation Runbook"
tags: [internal, runbook, observability, federation, logs]
audience: [platform-engineer, sre]
version: v2.8.1
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

## Refs

- 源 issue：[#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)
- 產生 audit JSON 的 Envoy access_log：`helm/federation-gateway/files/envoy.yaml`
- ADR-020 §Audit log
- Consumer #2：[#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)
