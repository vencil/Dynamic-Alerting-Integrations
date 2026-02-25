# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander 操作 Dev Container 的最佳實踐與已知陷阱。

## 前提

kubectl/kind/go/helm 僅在 Dev Container (`vibe-dev-container`) 內可用。
Go module 路徑: `components/threshold-exporter/app/` (非根目錄)。

## 核心模式 — docker exec + workspace mount 重定向

```bash
# ✅ 推薦: 重定向在 bash -c 內部，寫入 container 可見路徑
docker exec vibe-dev-container bash -c "\
  kubectl get pods -A > /workspaces/vibe-k8s-lab/output.txt 2>&1"
# → 用 Read tool 讀 /sessions/.../mnt/vibe-k8s-lab/output.txt

# ✅ 多指令串接 (重定向也在 bash -c 內)
docker exec vibe-dev-container bash -c "{ \
  echo '=== Step 1 ===' ; \
  kubectl get pods -n monitoring ; \
  echo '=== Step 2 ===' ; \
  kubectl get deploy -n monitoring ; \
} > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# ❌ 絕對不要: PS 會搶走 > 重定向
docker exec vibe-dev-container kubectl get pods > output.txt
```

## 已知陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | docker stdout 被 PowerShell 吞掉（Desktop Commander / Windows-MCP Shell 皆會發生） | **一律** 在 `bash -c` 內重定向至 `/workspaces/vibe-k8s-lab/*.txt`，再用 Read tool |
| 2 | PS `> file.txt` 重定向走 host path `C:\workspaces\...` 導致 `DirectoryNotFoundException` | 重定向 **必須在 `bash -c "..."` 內部**，寫 container 路徑 `/workspaces/...` |
| 3 | `bash -c '...'` 引號被 PowerShell 拆解 | **外層用雙引號** `bash -c "..."`，內部用單引號；heredoc 用 `<<'EOF'` |
| 4 | UTF-8 emoji 輸出完全消失 (✅❌📦📄) | workspace mount 重定向；判斷通過用 exit code (`set -euo pipefail`) |
| 5 | Go test `./...` 找不到 module | `-w /workspaces/vibe-k8s-lab/components/threshold-exporter/app` |
| 6 | 長時間測試 timeout | Desktop Commander `start_process` (支援 600s) |
| 7 | kubeconfig 過期 | `kind export kubeconfig --name dynamic-alerting-cluster --kubeconfig /root/.kube/config` |
| 8 | port-forward 殘留 | `docker exec vibe-dev-container pkill -f port-forward` |
| 9 | Python inline 腳本含引號衝突 | 用 `python3 -c "..."` 包單引號；或寫檔再執行 |
| 10 | Helm upgrade ConfigMap field-manager 衝突 | 先用 `kubectl apply --server-side --force-conflicts --field-manager=helm` 取回 ownership，再 `helm upgrade` |
| 11 | `helm upgrade --force` 與 server-side apply 互斥 | 不要用 `--force`；改用陷阱 #10 的 server-side apply 流程 |

## Helm Upgrade 防衝突流程

當 ConfigMap 被 `kubectl patch` 手動修改過，Helm upgrade 會報 field-manager conflict。標準修復：

```bash
# Step 1: helm template 渲染 → server-side apply 取回 ownership
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "\
  kubectl apply --server-side --force-conflicts --field-manager=helm \
    -f <(helm template threshold-exporter components/threshold-exporter/ -n monitoring) \
  > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# Step 2: 正常 helm upgrade (不再衝突)
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "\
  helm upgrade threshold-exporter components/threshold-exporter/ -n monitoring \
  > /workspaces/vibe-k8s-lab/output.txt 2>&1"
```

## 批量 YAML 驗證

```bash
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container \
  python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['k8s/03-monitoring/configmap-rules-mariadb.yaml','k8s/03-monitoring/deployment-prometheus.yaml']]"
```

## 指令快速參考

```bash
# 叢集狀態
docker exec vibe-dev-container kind get clusters
docker exec vibe-dev-container bash -c "kubectl get pods -A > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# Go 編譯 & 靜態分析
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go build -o /dev/null .
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go vet ./...

# Python 工具測試
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-migrate-tool.sh
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-migrate-multidb.sh
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-scaffold.sh

# K8s manifests apply (projected volume 架構，含 platform rule pack)
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "\
  kubectl apply -f k8s/03-monitoring/ > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# Helm upgrade (threshold-exporter)
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash -c "\
  helm upgrade threshold-exporter components/threshold-exporter/ -n monitoring \
  > /workspaces/vibe-k8s-lab/output.txt 2>&1"

# HA 驗證
docker exec vibe-dev-container bash -c "{ \
  echo '=== Deploy ===' ; kubectl get deploy threshold-exporter -n monitoring ; \
  echo '=== Pods ===' ; kubectl get pods -n monitoring -l app=threshold-exporter ; \
  echo '=== PDB ===' ; kubectl get pdb -n monitoring ; \
} > /workspaces/vibe-k8s-lab/output.txt 2>&1"
```
