---
title: "Platform Engineer 快速入門指南"
tags: [getting-started, platform-setup]
audience: [platform-engineer]
version: v2.0.0
lang: zh
---
# Platform Engineer 快速入門指南

> **v2.0.0** | 適用對象：Platform Engineers、SRE、基礎設施管理員
>
> 相關文件：[Architecture](../architecture-and-design.md) · [Benchmarks](../architecture-and-design.md) · [GitOps Deployment](../gitops-deployment.md) · [Rule Packs](../rule-packs/README.md)

## 你需要知道的三件事

**1. threshold-exporter 是核心。** 它讀取 YAML 設定、產生 Prometheus Metrics、支援 SHA-256 hot-reload。兩個副本以 HA 方式運行在 port 8080。

**2. Rule Pack 是自成一體的單位。** 15 個 Rule Pack 透過 Projected Volume 掛載到 Prometheus，每個涵蓋一個資料庫或服務類型（MariaDB、PostgreSQL、Redis 等）。用 `optional: true` 機制安全卸載不需要的 Rule Pack。

**3. 一切都由配置驅動。** `_defaults.yaml` 控制平台全局行為，tenant YAML 覆蓋預設值，`_profiles.yaml` 提供繼承鏈。沒有硬編碼，沒有秘密。

## 30 秒快速部署

最小可用平台配置：

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_cpu: "75"
  mysql_memory: "85"
  # 其他預設閾值...
```

### 部署 threshold-exporter ×2 HA

```bash
kubectl apply -f k8s/02-threshold-exporter/
# 驗證副本運行
kubectl get pod -n monitoring | grep threshold-exporter
```

### 掛載 Rule Pack

```bash
# Prometheus StatefulSet 使用 Projected Volume
# 確認 k8s/03-monitoring/prometheus-statefulset.yaml 的 volume 部分
kubectl get configmap -n monitoring | grep rule-pack
```

> 💡 **互動工具** — 不確定需要哪些 Rule Pack？用 [Rule Pack Selector](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-selector.jsx) 互動選取。想估算叢集資源需求？試試 [Capacity Planner](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/capacity-planner.jsx)。不確定該選哪種架構？[Architecture Quiz](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/architecture-quiz.jsx) 幫你做決定。想在瀏覽器中體驗完整的工作流？[Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) 展示 scaffold → validate → deploy。

## 常見操作

### 管理全局預設值

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_connections_critical: "95"
  container_cpu: "70"
  container_memory: "80"
  # 維度維持空閾值（跳過）
  redis_memory: "disable"      # 禁用
  _routing_defaults:
    group_wait: "30s"
    group_interval: "5m"
    repeat_interval: "12h"
```

驗證預設值語法：

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/ --schema
```

### 管理 Rule Pack

檢視已掛載的 Rule Pack：

```bash
kubectl get configmap -n monitoring | grep rule-pack
# 可能輸出：rule-pack-mariadb, rule-pack-postgresql, rule-pack-redis...
```

移除不需要的 Rule Pack（編輯 Prometheus StatefulSet）：

```bash
kubectl edit statefulset prometheus -n monitoring
# 在 volumes.projected.sources 中移除對應的 configMapRef
# 或設定 Projected Volume 的 optional: true 實現安全卸載
```

### 設定平台強制路由 (_routing_enforced)

啟用雙軌通知（NOC + Tenant）：

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_enforced:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/B/xxx"
      channel: "#noc-alerts"
    group_wait: "10s"
    repeat_interval: "2h"
```

NOC 收到的通知使用 `platform_summary` annotation，內容聚焦容量規劃和升級決策。Tenant 仍收到各自的 `summary`，不受影響。

### 設定路由預設值 (_routing_defaults)

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_defaults:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/{{tenant}}-alerts"
      channel: "#{{tenant}}-team"
    group_wait: "30s"
    repeat_interval: "4h"
```

`{{tenant}}` 佔位符自動展開為各 tenant 的名稱。Tenant YAML 的 `_routing` 可覆蓋此預設。

### 配置 Tenant Profile

```yaml
# conf.d/_profiles.yaml
profiles:
  standard-db:
    mysql_connections: "80"
    mysql_cpu: "75"
    container_memory: "85"
  high-load-db:
    mysql_connections: "60"     # 更嚴格
    mysql_cpu: "60"
    container_memory: "80"
```

Tenant 可透過 `_profile` 繼承：

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    _profile: "standard-db"
    mysql_connections: "70"     # 覆蓋 profile 的值
```

### 設定 Webhook Domain Allowlist

限制 webhook receiver 的目標域名：

```bash
python3 scripts/tools/ops/generate_alertmanager_routes.py \
  --config-dir conf.d/ \
  --policy "*.example.com" \
  --policy "hooks.slack.com" \
  --validate
```

fnmatch 模式支援萬用字元。⚠️ 空清單表示不限制 — **生產環境強烈建議設定白名單**，避免 tenant 將告警發送到未授權的外部端點。

## 驗證工具

### 一站式配置驗證

```bash
python3 scripts/tools/ops/validate_config.py \
  --config-dir conf.d/ \
  --schema
```

檢查項目：
- YAML 語法正確性
- 參數 schema 符合
- Route 轉換成功
- Policy 檢查通過
- 版本一致性

### 告警品質評估（v2.0.0）

```bash
# 掃描所有 tenant 的告警品質（Noise / Stale / Latency / Suppression）
da-tools alert-quality --prometheus http://localhost:9090 --config-dir conf.d/

# CI gate：低於 60 分 exit 1
da-tools alert-quality --prometheus http://localhost:9090 --ci --min-score 60
```

### Policy-as-Code 策略驗證（v2.0.0）

```bash
# 用 _defaults.yaml 中的 _policies DSL 評估所有 tenant
da-tools evaluate-policy --config-dir conf.d/

# CI gate：有 error 違規時 exit 1
da-tools evaluate-policy --config-dir conf.d/ --ci
```

### 基數趨勢預測（v2.0.0）

```bash
# 預測 per-tenant 基數成長趨勢、觸頂天數
da-tools cardinality-forecast --prometheus http://localhost:9090

# CI gate：有 critical 風險時 exit 1
da-tools cardinality-forecast --prometheus http://localhost:9090 --ci
```

### 配置差異比對

```bash
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline \
  --new-dir conf.d/ \
  --format json
```

輸出：新增 tenant、移除 tenant、變更的預設值、變更的 profile。用於 GitOps PR review。

### 版號一致性檢查

```bash
make version-check
python3 scripts/tools/dx/bump_docs.py --check
```

確保 CLAUDE.md、README、CHANGELOG 的版號同步。

## 效能監控

### 執行 Benchmark

```bash
make benchmark ARGS="--under-load --routing-bench --alertmanager-bench --reload-bench --json"
```

輸出指標：
- Idle memory footprint
- 延展曲線（QPS vs memory/latency）
- Routing throughput
- Alertmanager 反應時間
- ConfigMap reload 延遲

結果保存為 JSON，供 CI 比較。

### Platform Rule Pack 自監控

Platform 本身提供 Rule Pack alert（如 exporter 離線、Alertmanager delay > 1m）：

```bash
kubectl get alerts -n monitoring | grep platform
```

## 生產環境安全加固

### Lifecycle Endpoint 保護

Prometheus 和 Alertmanager 的 `--web.enable-lifecycle` 會暴露 `/-/reload` 和 `/-/quit` 端點，**不需要任何認證**即可觸發。任何能存取該 port 的人都能透過 `POST /-/quit` 關閉服務。

建議做法：

```yaml
# NetworkPolicy：僅允許 configmap-reload sidecar 存取 lifecycle 端點
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: prometheus-lifecycle-restrict
  namespace: monitoring
spec:
  podSelector:
    matchLabels:
      app: prometheus
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: prometheus  # 同 pod 內的 sidecar
      ports:
        - port: 9090
```

或部署 auth proxy（如 oauth2-proxy）保護 `/-/` 路徑。

### Grafana 預設密碼

本專案的 `deployment-grafana.yaml` 使用 `admin:admin` 作為初始密碼，**僅供開發環境使用**。生產環境部署前**必須**透過 K8s Secret 設定強密碼，並建議搭配 auth proxy 或 SSO 整合。

### Webhook Domain Allowlist

`generate_alertmanager_routes.py --policy` 的空清單表示不限制。**生產環境強烈建議設定白名單**，防止 tenant 配置將告警通知發送到未經授權的外部端點。

### Port-forward 安全

本地 `kubectl port-forward` 預設綁定 `127.0.0.1`（僅本機）。**切勿使用 `--address 0.0.0.0`**，這會將 Prometheus/Alertmanager/Grafana 暴露到所有網路介面，任何能存取該機器的人都能直接存取服務。

### Secrets 管理 — 從 ConfigMap 遷移至 K8s Secret

Alertmanager receiver 配置中的敏感資訊（Slack token、webhook URL、PagerDuty service key 等）不應以明文存放在 ConfigMap 中。`kubectl get configmap -o yaml` 即可看到所有內容，而 K8s Secret 至少提供 base64 編碼並支援 RBAC 細粒度存取控制。

**基本做法 — K8s Secret + secretKeyRef：**

```yaml
# 1. 建立 Secret（一次性，或由 CI 管理）
kubectl create secret generic alertmanager-secrets \
  --from-literal=slack-api-url='https://hooks.slack.com/services/T.../B.../xxx' \
  --from-literal=pagerduty-key='your-service-key' \
  -n monitoring

# 2. 在 Alertmanager Deployment 中引用
env:
  - name: SLACK_API_URL
    valueFrom:
      secretKeyRef:
        name: alertmanager-secrets
        key: slack-api-url
  - name: PAGERDUTY_KEY
    valueFrom:
      secretKeyRef:
        name: alertmanager-secrets
        key: pagerduty-key
```

`generate_alertmanager_routes.py` 產生的 receiver config 中，使用 `<secret>` 或環境變數引用取代明文值。

**進階做法 — External Secrets Operator + HashiCorp Vault：**

對於需要集中式 secrets 管理、自動輪換、審計日誌的生產環境，建議整合 External Secrets Operator (ESO)：

```yaml
# 1. 安裝 External Secrets Operator
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace

# 2. 設定 SecretStore（連接 Vault）
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: vault-backend
  namespace: monitoring
spec:
  provider:
    vault:
      server: "https://vault.internal:8200"
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "alertmanager"
          serviceAccountRef:
            name: alertmanager

# 3. 定義 ExternalSecret（自動同步 Vault → K8s Secret）
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: alertmanager-secrets
  namespace: monitoring
spec:
  refreshInterval: 1h          # 每小時同步，支援自動輪換
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: alertmanager-secrets  # 產生的 K8s Secret 名稱
    creationPolicy: Owner
  data:
    - secretKey: slack-api-url
      remoteRef:
        key: dynamic-alerting/alertmanager
        property: slack-api-url
    - secretKey: pagerduty-key
      remoteRef:
        key: dynamic-alerting/alertmanager
        property: pagerduty-key
```

Vault 側設定：

```bash
# 啟用 KV v2 引擎
vault secrets enable -path=secret kv-v2

# 寫入 secrets
vault kv put secret/dynamic-alerting/alertmanager \
  slack-api-url="https://hooks.slack.com/services/T.../B.../xxx" \
  pagerduty-key="your-service-key"

# 建立 policy（最小權限）
vault policy write alertmanager - <<EOF
path "secret/data/dynamic-alerting/alertmanager" {
  capabilities = ["read"]
}
EOF

# 綁定 K8s ServiceAccount
vault write auth/kubernetes/role/alertmanager \
  bound_service_account_names=alertmanager \
  bound_service_account_namespaces=monitoring \
  policies=alertmanager \
  ttl=1h
```

此架構的優勢：secrets 永遠不進 Git、支援自動輪換（`refreshInterval`）、Vault 提供完整審計日誌、RBAC 可精確控制誰能存取哪些 secrets。

### TLS 加密通訊指引

生產環境中，threshold-exporter、Prometheus、Alertmanager 之間的通訊應啟用 TLS，防止 metrics 資料和告警內容在網路傳輸中被竊聽。

**Step 1 — 使用 cert-manager 簽發憑證（推薦）：**

```yaml
# 安裝 cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.0/cert-manager.yaml

# 建立自簽 CA（開發環境）或引用正式 CA（生產環境）
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: monitoring-ca
spec:
  selfSigned: {}  # 生產環境改用 ACME 或內部 CA

# 簽發 Prometheus TLS 憑證
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: prometheus-tls
  namespace: monitoring
spec:
  secretName: prometheus-tls
  issuerRef:
    name: monitoring-ca
    kind: ClusterIssuer
  commonName: prometheus.monitoring.svc.cluster.local
  dnsNames:
    - prometheus.monitoring.svc.cluster.local
    - prometheus.monitoring.svc
    - prometheus
```

**Step 2 — threshold-exporter 啟用 TLS：**

```yaml
# Deployment args 增加 TLS 參數
args:
  - "--tls-cert-file=/etc/tls/tls.crt"
  - "--tls-key-file=/etc/tls/tls.key"
volumeMounts:
  - name: tls
    mountPath: /etc/tls
    readOnly: true
volumes:
  - name: tls
    secret:
      secretName: exporter-tls
```

**Step 3 — Prometheus scrape_configs 加上 TLS：**

```yaml
scrape_configs:
  - job_name: "dynamic-thresholds"
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/tls/ca.crt
      # 如使用 mTLS，加上 client cert
      # cert_file: /etc/prometheus/tls/tls.crt
      # key_file: /etc/prometheus/tls/tls.key
```

**Step 4 — Alertmanager HTTP config TLS：**

```yaml
# alertmanager.yml 中 webhook_configs 加上 TLS
receivers:
  - name: "secure-webhook"
    webhook_configs:
      - url: "https://internal-webhook.example.com/alert"
        http_config:
          tls_config:
            ca_file: /etc/alertmanager/tls/ca.crt
```

### Config Reload Endpoint 安全

Prometheus 的 `/-/reload` 和 Alertmanager 的 `/-/reload` 是 HTTP POST 端點，用於觸發配置重新載入。本專案透過 `configmap-reload` sidecar 自動呼叫此端點。

**安全影響：** 這些端點不需要認證。若攻擊者能存取 Prometheus/Alertmanager 的 port，可以反覆觸發 reload 造成效能影響，或在 `/-/quit` 啟用時直接關閉服務。

**生產環境建議：** 使用上方「Lifecycle Endpoint 保護」章節的 NetworkPolicy 限制存取。確保 Prometheus 和 Alertmanager 使用 ClusterIP Service（非 NodePort/LoadBalancer），僅在叢集內部可達。

## 常見問題

**Q: 我要如何新增一個 Rule Pack？**
A: 新 Rule Pack 需在 `rule-packs/` 目錄新增 YAML 檔案，並在 Prometheus Projected Volume 配置中掛載對應的 ConfigMap。請參考 Rule Pack README 的模板。

**Q: 如何強制 NOC 接收所有通知？**
A: 在 `_defaults.yaml` 中設定 `_routing_enforced`。通知會發送給 NOC 的 channel 和各 tenant 的 receiver，獨立進行。

**Q: Webhook allowlist 為何拒絕我的 domain？**
A: 用 `--policy` 檢查你的 webhook URL 是否符合 fnmatch 模式。例如 `*.example.com` 不會匹配 `webhook.internal.example.com`（多層子域名）。

**Q: 如何驗證新 tenant 的配置不會造成 alert noise？**
A: 先用 `validate_config.py` 檢查語法和 schema，再用 `config_diff.py` 看 blast radius，最後在 shadow monitoring 環境中測試（參考 shadow-monitoring-sop.md）。

**Q: Rule Pack 的 optional: true 是什麼？**
A: Kubernetes Projected Volume 的特性。設定 `optional: true` 後，如果該 ConfigMap 不存在，Prometheus 仍可啟動（卷掛載為空）。用於安全卸載 Rule Pack。

**Q: 我需要自定義某個 Rule Pack 中的規則嗎？**
A: 不直接修改 Rule Pack。在 tenant YAML 中用 `_routing.overrides[]` 覆蓋單個規則的路由，或用 custom rule governance（lint_custom_rules.py）新增自訂規則。

> 💡 **互動工具** — 驗證配置可用 [Config Lint](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-lint.jsx)。比較配置變更用 [Config Diff](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-diff.jsx)。查看 Rule Pack 依賴用 [Dependency Graph](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/dependency-graph.jsx)。完整 [Onboarding Checklist](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/onboarding-checklist.jsx) 追蹤上線進度。所有工具見 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/)。需要在企業內網部署？用 `da-portal` Docker image：`docker run -p 8080:80 ghcr.io/vencil/da-portal`（[部署說明](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)）。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Platform Engineer 快速入門指南"](for-platform-engineers.md) | ⭐⭐⭐ |
| ["Domain Expert (DBA) 快速入門指南"](for-domain-experts.md) | ⭐⭐ |
| ["Tenant 快速入門指南"](for-tenants.md) | ⭐⭐ |
| ["Migration Guide — 遷移指南"](../migration-guide.md) | ⭐⭐ |
