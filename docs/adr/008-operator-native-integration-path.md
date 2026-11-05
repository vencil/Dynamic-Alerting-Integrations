---
title: "ADR-008: Operator-Native 整合路徑"
tags: [adr, architecture, operator]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-008
tracking_kind: adr
status: accepted
domain: k8s
created_at: 2026-04-04
updated_at: 2026-11-05
---
# ADR-008: Operator-Native 整合路徑

> **Language / 語言：** **中文 (Current)** | [English](./008-operator-native-integration-path.en.md)

## 狀態

✅ **Accepted**（v2.3.0）

- 2026-11-05 修訂（同時把先前分段附加的修訂併進正文）：Alertmanager 路由不再產生成 `AlertmanagerConfig` CRD。兩條路徑的路由都由 `generate-routes` 產生；Operator 路徑把產出的 `alertmanager.yaml` 放進 Alertmanager 的 `configSecret`。`operator-generate` 只產 PrometheusRule 與 ServiceMonitor（[#3572](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3572)）。

## 決策

**核心平台（threshold-exporter + Rule Pack）不分路徑；工具鏈處理兩種部署方式的差異。**

- **Prometheus 這一側分兩條路徑。** ConfigMap 路徑用 projected volume 掛 Rule Pack；Operator 路徑由 `da-tools operator-generate` 產出 PrometheusRule（每個 Rule Pack 一份）與 threshold-exporter 的 ServiceMonitor，`da-tools operator-check` 驗證它們有沒有被 Operator 載入。
- **Alertmanager 路由只有一個產生器：`da-tools generate-routes`。** 輸出＝合併（你自己的 Alertmanager 設定, conf.d），順序固定是：平台路由（Watchdog 心跳、`component="custom"` 隔離子樹、synthetic probe、sentinel）→ 你自己的路由（原順序）→ 租戶路由。ConfigMap 路徑把結果放進 Alertmanager 的 ConfigMap（或 `--apply`）；Operator 路徑把同一份結果放進 Secret，再讓 Alertmanager CR 的 `spec.configSecret` 指向它。兩條路徑套用的是**同一份**設定檔，不是兩個各自產生、要保持一致的版本。

### 範例

conf.d 一個租戶、你自己的設定一條 critical 路由：

```yaml
# conf.d/team-a.yaml
tenants:
  team-a:
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/team-a"
```

```yaml
# alertmanager.yaml（你自己的設定，來源）
route:
  receiver: ops-default
  routes:
  - matchers: ['severity="critical"']
    receiver: ops-pager
receivers:
- name: ops-default
- name: ops-pager
  webhook_configs: [{url: 'https://pager.example.com/hook'}]
```

```bash
da-tools generate-routes --config-dir conf.d --output-alertmanager \
  --base-config alertmanager.yaml -o out.yaml
# Operator 路徑：放進 configSecret（key 必須是 alertmanager.yaml）
kubectl create secret generic alertmanager-routing -n monitoring \
  --from-file=alertmanager.yaml=out.yaml --dry-run=client -o yaml | kubectl apply -f -
```

`out.yaml` 的 `route.routes` 依序送往：`watchdog-heartbeat`、`custom-alerts-firehose`、`synthetic-receiver`、`sentinel-sinkhole`、`ops-pager`、`tenant-team-a`。用 `amtool config routes test --config.file=out.yaml` 測去向：

| 告警標籤 | 送往 |
|---|---|
| `alertname=Watchdog` | `watchdog-heartbeat` |
| `alertname=HighCPU tenant=team-a severity=warning` | `tenant-team-a` |
| `alertname=MyRule component=custom tenant=team-a severity=critical` | `tenant-team-a`（custom 子樹的租戶子路由，不經 `ops-pager`） |
| `alertname=NodeDown severity=critical` | `ops-pager` |

kube-prometheus-stack 用 `alertmanager.alertmanagerSpec.configSecret: alertmanager-routing` 指過去；Operator 讀這個 Secret 的 `alertmanager.yaml` key。

## 背景

Prometheus Operator（kube-prometheus-stack）是 Kubernetes 上部署 Prometheus 的主流方式。它用 CRD（`PrometheusRule`、`ServiceMonitor`、`AlertmanagerConfig`）取代 ConfigMap，以 label selector 載入設定。平台要讓 ConfigMap 使用者與 Operator 使用者都能用，而不改 threshold-exporter。

要處理的問題：

1. **兩種掛載方式共存**：ConfigMap 使用者以 projected volume 掛 Rule Pack；Operator 使用者需要 PrometheusRule。
2. **Operator 只載入 label 對得上的 CRD**：PrometheusRule／ServiceMonitor 的 label 要符合 Prometheus 的 `ruleSelector`／`serviceMonitorSelector`，各安裝方式不同。
3. **GitOps 要可重現的輸出**：帶 `resourceVersion`、`creationTimestamp` 這類伺服器端欄位的 YAML，會讓 ArgoCD／Flux 一直報 OutOfSync。
4. **平台路由必須排在最前面**。Watchdog 心跳要送到外部 dead-man's switch（告警管線本身死掉時，靠「心跳停了」被發現）；租戶的 custom 告警要隔離在自己的子樹，不能流進叢集的 NOC 路由。這兩件事都靠路由順序：Alertmanager 由上往下比對，先命中、又沒有 `continue: true` 的路由就收走告警。

第 4 點是路由不產生成 `AlertmanagerConfig` 的原因。Operator 合併 `AlertmanagerConfig` 的方式（Prometheus Operator v0.95.0，`pkg/alertmanager/amcfg.go`）：

- CR 的路由一律插在全域路由**之前**，頂層路由強制 `continue: true`（比對成功後繼續往下比）；
- 預設的 `alertmanagerConfigMatcherStrategy: OnNamespace` 會在 CR 的路由與 inhibit rule 加上 `namespace="<CR 所在 namespace>"`。

所以每個租戶一份 CR 的做法下：平台路由排不到最前面；租戶路由只命中帶 `namespace="monitoring"`（CR 所在處）的告警；命中了也會因 `continue: true` 再流進全域路由，custom 隔離不成立。kube-prometheus-stack 預設還把 Watchdog 送往 `null`。

## 後果與已知限制

- **Operator 路徑多一步**：conf.d 改了，要重跑 `generate-routes` 並更新 Secret（GitOps 時把產出放進 pipeline）。換到的是兩條路徑同一份路由、同一組測試。
- **Alertmanager CR 若同時選取 `AlertmanagerConfig`**（設了 `alertmanagerConfigSelector`；kube-prometheus-stack 預設會設），那些 CR 的路由仍會被插在 configSecret 的路由之前。平台自己不再產 CR；舊版 `operator-generate` 留下的（帶 `app.kubernetes.io/part-of=dynamic-alerting`）要刪掉，`operator-check` 會對它們報 WARN。別的團隊的 CR 因為 `continue: true` 攔不走告警，但會多送一份；要讓平台路由之前什麼都沒有，就把 selector 設成不選任何 `AlertmanagerConfig`。
- **Breaking**：`operator-generate --components alertmanager` 與只給 `AlertmanagerConfig` 用的旗標（`--config-dir`、`--api-version`、`--receiver-template`、`--secret-name`、`--secret-key`）結束碼 2，訊息附上 `generate-routes` 與 configSecret 的做法；`migrate-to-operator` 也不再產 `AlertmanagerConfig`。
- **PrometheusRule 的 label 要對上 selector**：`operator-generate` 預設帶 `release=kube-prometheus-stack`，Helm release 名稱不同時用 `--selector-label` 改；`operator-check` 會報出沒被載入的情況。
- **`--gitops`** 輸出排序過的鍵、不帶時間戳與伺服器端欄位，同一輸入產出相同 YAML；`--kustomize` 另產 `kustomization.yaml` 供多叢集部署；`drift_detect.py --mode operator` 比對跨叢集的 CRD 漂移。
- **判斷叢集走哪條路徑**：`operator-check` 先看 `prometheusrules.monitoring.coreos.com` CRD 在不在；在就是 Operator 路徑。
- **還沒做的**：常見 Operator 部署的 kube-prometheus-stack values 範例；多叢集 Federation 下以 ArgoCD ApplicationSet 部署 CRD 的自動化。

### 架構邊界

- **threshold-exporter 不 watch 任何 CRD**，只讀 `conf.d/`（SHA-256 比對內容變更後重載）。這讓 exporter 在非 Kubernetes 環境也能跑。
- **工具只做「讀取 → 轉換 → 輸出」**。`operator-generate` 讀 `rule-packs/`，產出 CRD YAML；不連叢集、不 `kubectl apply`。`generate-routes` 的 `--apply` 是它唯一會改叢集的模式，屬於 ConfigMap 路徑。
- 新增 Operator 相關工具前，問三件事：會不會改 threshold-exporter 的輸入介面？會不會要 exporter 連 Kubernetes API？是不是只是讀取、轉換、輸出？前兩個是「是」就不做；第三個是「是」才屬於工具鏈。
- **CRD 原生的自助介面不另立 operator。** 規則一律在 CI 編譯（recipe → 編譯 → promtool → PR → ConfigMap）；`da_assembler` 的 direct-render 只留本機除錯用，因為它會繞過這條管線，讓 CRD 宣告的告警送出指標、CI 卻沒有對應規則。`ThresholdConfig` CRD 預設不安裝：沒有消費者，不裝的話 `kubectl apply` 會當場報錯。日後客戶明確需要 kubectl 原生介面時，做成 tenant-api 內嵌、可開關的監聽模式，沿用它既有的單一寫入機制（[ADR-023](023-write-plane-single-writer-invariant.md)），不另開元件與發版線。

## 考慮過的替代方案

- **把 threshold-exporter 改成 Kubernetes Operator**（監聽 `DynamicAlertTenant` CRD）：核心要引入 controller 與 CRD，複雜度大增，也綁死 Kubernetes；現有的檔案熱重載已驗證可用。不採用。
- **只寫文件、不做工具**：使用者回報手工把每個 Rule Pack 轉成 PrometheusRule 費時易錯，GitOps 也需要可重現的輸出。不採用。
- **Operator 路徑每個租戶產一份 `AlertmanagerConfig`**（v2.3.0 起的做法）：因上方「背景」第 4 點的合併方式，平台路由排不到最前面、租戶路由受 namespace matcher 限制、custom 告警會流進全域路由。改為 configSecret。
- **一份平台專用的 `AlertmanagerConfig` 放平台路由**：CR 一樣被強制 `continue: true` 並加上 namespace matcher，Watchdog 與 sentinel 告警的 namespace 不一定是 CR 所在處，排序也無法保證在別的 CR 之前。不採用。
- **文件化 `alertmanagerConfigMatcherStrategy: None` 繼續用 per-tenant CR**：解決 namespace matcher，但 `continue: true` 與排序問題仍在；而且要維護第二個路由產生器，與 `generate-routes` 保持一致。不採用。

## 相關

| 文件 | 關係 |
|-----|------|
| [ADR-001](001-severity-dedup-via-inhibit.md) | severity 去重的 inhibit rule，由 `generate-routes` 產生 |
| [ADR-004](004-federation-central-exporter-first.md) | Federation 場景的 CRD 部署要考慮 edge／central 分層 |
| [ADR-005](005-projected-volume-for-rule-packs.md) | ConfigMap 路徑的 projected volume；Operator 路徑以 PrometheusRule 取代 |
| [ADR-007](007-cross-domain-routing-profiles.md) | 路由 profile，由 `generate-routes` 展開 |
| [Operator Alertmanager 整合](../integration/operator-alertmanager-integration.md) | configSecret 的完整步驟 |
| [Operator 整合手冊](../integration/prometheus-operator-integration.md) | Operator 路徑總覽 |
| [CLI 參考 §generate-routes](../cli-reference.md#generate-routes) | 合併規則與結束碼 |
| [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) | 上游 Helm chart |
| [Prometheus Operator API](https://prometheus-operator.dev/docs/api-reference/api/) | CRD API 文件 |
