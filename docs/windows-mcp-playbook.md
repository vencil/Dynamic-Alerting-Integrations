# Windows-MCP — Dev Container 操作手冊 (Playbook)

> AI Agent 透過 Windows-MCP Shell / Desktop Commander 操作 Dev Container 的最佳實踐與已知陷阱。

## 前提

kubectl/kind/go 僅在 Dev Container (`vibe-dev-container`) 內可用。
Go module 路徑: `components/threshold-exporter/app/` (非根目錄)。

## 核心模式 — docker exec + workspace mount 重定向

```bash
# 推薦: 輸出重定向至 workspace mount，再用 Read tool 讀取
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container \
  bash -c 'bash tests/test.sh > /workspaces/vibe-k8s-lab/output.txt 2>&1'
# → 用 Read tool 讀 /sessions/.../mnt/vibe-k8s-lab/output.txt

# 快速指令 (無 UTF-8 emoji 輸出時可直接用)
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <command>
```

> **避免**: `Start-Process -RedirectStandardOutput` 對 UTF-8 emoji 輸出會產生空檔案。

## 已知陷阱

| # | 陷阱 | 解法 |
|---|------|------|
| 1 | docker 直接呼叫無輸出 (PS pipeline 問題) | workspace mount 重定向 + Read tool |
| 2 | `bash -c '...'` 引號被 PowerShell 拆解 | 用雙引號包 bash -c，內部用單引號；或簡化指令 |
| 3 | UTF-8 emoji 輸出完全消失 (✅❌📦📄) | workspace mount 重定向；判斷通過用 exit code (`set -euo pipefail`) |
| 4 | Go test `./...` 找不到 module | `cd components/threshold-exporter/app/` 再跑 `go test ./...` |
| 5 | 長時間測試 timeout | Desktop Commander `start_process` (支援 600s) |
| 6 | kubeconfig 過期 | `kind export kubeconfig --name dynamic-alerting-cluster --kubeconfig /root/.kube/config` |
| 7 | port-forward 殘留 | `docker exec vibe-dev-container pkill -f port-forward` |
| 8 | PS `> file.txt` 重定向到 host path 失敗 | 重定向必須在 `bash -c` 內部，寫入 container 可見路徑 (如 `/workspaces/...`) |
| 9 | Python inline 腳本含引號衝突 | 用 `python3 -c "..."` 包單引號；或寫成多行 Python string 避免跳脫 |

## 批量 YAML 驗證 (快速做法)

```bash
# 容器內用 python3 inline 驗證，exit code=0 表示全過
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container \
  python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['k8s/03-monitoring/configmap-rules-mariadb.yaml','k8s/03-monitoring/deployment-prometheus.yaml']]"
```

## 指令快速參考

```bash
# 叢集狀態
docker exec vibe-dev-container kind get clusters
docker exec vibe-dev-container kubectl get pods -A

# Go 編譯 & 靜態分析
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go build -o /dev/null .
docker exec -w /workspaces/vibe-k8s-lab/components/threshold-exporter/app vibe-dev-container go vet ./...

# Python 工具測試
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-migrate-tool.sh
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-migrate-multidb.sh
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/test-scaffold.sh

# K8s manifests apply (projected volume 架構)
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container kubectl apply -f k8s/03-monitoring/
```
