# Kube-State-Metrics

提供 Kubernetes 原生指標，用於 Scenario C (State/String Matching)。

## 部署方式

```bash
# 透過 Helm 部署
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kube-state-metrics prometheus-community/kube-state-metrics \
  -n monitoring \
  --create-namespace
```

## 提供的關鍵 Metrics

- `kube_pod_status_phase` - Pod 狀態 (Running, Pending, Failed, etc.)
- `kube_pod_container_status_waiting_reason` - 等待原因 (ImagePullBackOff, CrashLoopBackOff, etc.)
- `kube_deployment_status_replicas` - Deployment 副本數
- `kube_node_status_condition` - Node 狀態

## 驗證

```bash
# 檢查 Pod
kubectl get pods -n monitoring -l app.kubernetes.io/name=kube-state-metrics

# 查詢指標
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=kube_pod_status_phase{namespace="db-a"}'
```
