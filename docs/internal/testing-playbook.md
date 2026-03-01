# 測試注意事項 — 排錯手冊 (Testing Playbook)

> K8s 環境排錯、負載注入陷阱、Benchmark 基線、程式碼品質規範。
> **相關文件：** [Windows-MCP Playbook](windows-mcp-playbook.md) (docker exec 模式、Helm 防衝突、Prometheus 查詢)

## 測試前置準備

1. **Dev Container**: `docker ps` → `vibe-dev-container` 運行中
2. **Kind 叢集**: `docker exec vibe-dev-container kubectl get nodes` 正常
3. **PyYAML**: `docker exec vibe-dev-container python3 -c "import yaml"` (失敗則 `pip3 install pyyaml`)
4. **清理殘留**: `docker exec vibe-dev-container pkill -f port-forward` + `make load-cleanup`

## K8s 環境問題

| # | 問題 | 修復 |
|---|------|------|
| 1 | Helm upgrade 不清理舊 ConfigMap key | `kubectl delete cm threshold-config -n monitoring` → `helm upgrade` |
| 2 | Helm field-manager conflict | 見 [Windows-MCP Playbook → Helm 防衝突](windows-mcp-playbook.md#helm-upgrade-防衝突) |
| 3 | ConfigMap volume 更新延遲 30-90s | hot-reload 驗證需等 45+ 秒 |
| 4 | Metrics label 順序 (`component,metric,severity,tenant`) | grep 用 `metric=.*tenant=`，不要反過來 |
| 5 | 場景測試殘留值 | 測試前用 `patch_config.py` 恢復預設，負載測試用 `make load-cleanup` |

## Projected Volume 架構

9 個獨立 ConfigMap 透過 `projected` volume 投射至 `/etc/prometheus/rules/`：

```
configmap-rules-{mariadb,kubernetes,redis,mongodb,elasticsearch,oracle,db2,clickhouse,platform}.yaml
```

每個 DB Rule Pack 含 `*-recording.yml` + `*-alert.yml`；Platform 含 `platform-alert.yml`。全部設定 `optional: true`（Zero-Crash Opt-Out）。

修改單個 Rule Pack 只需 apply 對應 ConfigMap。刪除後 Prometheus 自動移除規則（volume 同步延遲 30-90s）。

## 負載注入 (Load Injection)

### 負載類型

| Type | 指令 | 觸發 Alert | 機制 |
|------|------|-----------|------|
| `connections` | `run_load.sh --type connections` | MariaDBHighConnections | 95 idle connections via PyMySQL |
| `cpu` | `run_load.sh --type cpu` | MariaDBHighSlowQueries | sysbench OLTP 16 threads |
| `stress-ng` | `run_load.sh --type stress-ng` | PodContainerHighCPU | Alpine CPU burn (100m limit) |
| `composite` | `run_load.sh --type composite` | MariaDBSystemBottleneck | connections + cpu 同時 |

### 關鍵設計約束

**連線數 < `max_connections` - 5：** MariaDB `max_connections=100`，`mysqld_exporter` 需至少 1 個連線槽才能回報 `SHOW STATUS`。設 95 條連線時 exporter 正常，alert fires（`Threads_connected=96`）。100 條以上 exporter 會被鎖死，指標變 stale。

**單進程持連：** 一個 Python process 持有 95 個 `pymysql.connect()` objects（~50MB）。不要用多進程 `mariadb -e "SELECT SLEEP(600)" &`（每個 ~5MB → OOM）。

**Composite 在 Kind 單節點可能觸發 MariaDBDown：** 95 idle + sysbench 額外連線可超過 `max_connections=100`，導致 exporter 鎖死。如需只觸發 `MariaDBHighConnections`，改用 `--type connections` 單獨注入。

### Container Image 選擇

| 用途 | Image | 原因 |
|------|-------|------|
| Connection Storm | `python:3.12-alpine` + PyMySQL | 單進程持多連線，~128Mi |
| CPU Burn (sysbench) | `severalnines/sysbench` | 標準 OLTP image |
| Container CPU 壓測 | `alpine:3.19` + shell loop | `stress-ng` image 有 PATH 問題；alpine `while true; do :; done` 最可靠 |

### YAML Heredoc 中嵌入 Python

```yaml
# ❌ heredoc 內嵌 heredoc → YAML parser 崩潰
command: |
  python3 - <<'PYEOF'
  import pymysql
  PYEOF

# ✅ 用 python3 -c "..." inline
command: |
  python3 -c "
  import pymysql, time
  conns = [pymysql.connect(host='$HOST', ...) for _ in range(95)]
  time.sleep(600)
  "
```

### 清理機制

所有負載路徑必須有 cleanup trap：

```bash
cleanup() {
  "${SCRIPT_DIR}/run_load.sh" --cleanup 2>/dev/null || true
  cleanup_port_forwards
}
trap cleanup EXIT
```

`run_load.sh --cleanup` 透過 label selector 一次清除：`kubectl delete jobs,pods -l app=load-generator --all-namespaces`。
Type-specific cleanup（dispatch 前）只清自己的前一個 instance，不影響 composite 疊加。

### 場景執行順序

同時跑多場景時，Connection Storm 會搶光連線：1) stress-ng → 2) cpu → 3) connections。或直接用 `composite`（自動按正確順序）。

## HA 相關測試

### Recording Rules — max vs sum

threshold-exporter 多 replica 時，每個 Pod 匯出相同的 `user_threshold`。聚合必須用 `max by(tenant)`，否則閾值翻倍。真實 DB 指標（如 `rate(mysql_...)`）仍用 `sum by(tenant)`。

### HA 部署驗證

| 項目 | 驗證指令 | 期望 |
|------|----------|------|
| 2 Pods Running | `kubectl get pods -n monitoring -l app=threshold-exporter` | 2/2 Ready |
| PDB 存在 | `kubectl get pdb -n monitoring` | `minAvailable: 1` |
| AntiAffinity | `kubectl get deploy ... -o jsonpath='{.spec.template.spec.affinity}'` | 含 `podAntiAffinity` |
| RollingUpdate | `kubectl get deploy ... -o jsonpath='{.spec.strategy}'` | `maxUnavailable: 0` |
| Platform alerts | `kubectl exec -n monitoring deploy/prometheus -- ls /etc/prometheus/rules/` | 含 `platform-alert.yml` |

### HA 故障切換 (Scenario F)

Kill Pod → 驗證三件事：1) PDB 保護：至少 1 Pod 仍 Running；2) Alert 持續：不因 Pod 重啟中斷（port-forward 需重建）；3) 閾值不翻倍：Pod 恢復後 `tenant:alert_threshold:connections` 仍為原值。

`helm upgrade` 後 replicas 可能被覆蓋（field ownership 衝突）。補救：`kubectl scale deploy threshold-exporter -n monitoring --replicas=2`。

## Multi-Tenant 隔離 (Scenario E)

| 操作 (tenant A) | 驗證 (tenant B) |
|----------------|----------------|
| `mysql_connections=5` → alert fires | 閾值不變，alert 不觸發 |
| `container_cpu=disable` → metric 消失 | `container_cpu` 仍正常 |

`--with-load` 模式下確保負載只注入 tenant A 的 namespace。`run_load.sh --tenant db-a` 在 `db-a` namespace 建立 Job。

## Performance Benchmark

### 自動化

```bash
make benchmark              # 完整報告
make benchmark ARGS=--json  # JSON 輸出
```

### 多輪 Benchmark 方法論

單次量測受環境干擾大。GA 品質的 benchmark 應跑多輪並報告統計量：

| Benchmark 類型 | 建議輪數 | 報告格式 | 耗時 |
|----------------|---------|---------|------|
| idle-state | 5 輪，間隔 45s | mean ± stddev | ~4min |
| scaling-curve (9→6→3 packs) | 3 輪 | median (range) | ~12min |
| Go micro-bench (`-count=N`) | 5 次 | median, stddev | ~1min |
| under-load | 1 輪（功能性） | 單次值 | ~3min |

**scaling-curve 注意：** 每輪需刪 Rule Pack → 重啟 Prometheus → 等穩定 → 取樣。`benchmark.sh` 的 port-forward 會在 Prometheus Pod 重建後斷開，需手動重連（或用自訂腳本處理）。Median 比 mean 更能代表穩態。

### 關鍵指標基線 (v1.0.0, 2 tenants, 9 Rule Packs, Kind 單節點)

> 以下數據取自 **5 輪獨立量測**（2026-03-01），報告 mean ± stddev。

| 指標 | 值 (n=5) | 說明 |
|------|----|------|
| 規則總數 | 141 | 9 Rule Packs (53R + 56A + 32 threshold-norm) |
| 規則群組數 | 27 | 9 pack × 3 groups |
| 每週期總評估時間 | 20.3 ± 1.9ms | `sum(prometheus_rule_group_last_duration_seconds)` |
| p50 per-group | 1.23 ± 0.28ms | `prometheus_rule_group_duration_seconds{quantile="0.5"}` |
| p99 per-group | 6.89 ± 0.44ms | `prometheus_rule_group_duration_seconds{quantile="0.99"}` |
| Prometheus CPU | ~0.014 ± 0.003 cores | `rate(process_cpu_seconds_total{job="prometheus"}[5m])` |
| Prometheus Memory | 142.7 ± 1.4MB RSS | `process_resident_memory_bytes{job="prometheus"}` |
| Exporter Heap | 2.4 ± 0.4MB | `go_memstats_alloc_bytes` |
| 活躍 Series | ~6,037 ± 10 | `prometheus_tsdb_head_series` |
| user_threshold Series | 8 (4/tenant) | `count(user_threshold)` |
| Scrape Duration | 4.1 ± 1.2ms | `scrape_duration_seconds{job="threshold-exporter"}` |
| Scaling Curve (median, n=3) | 3→6→9 packs: 7.7→17.3→22.7ms | 線性增長已驗證 |

### 負載下的 Alert 驗證基線

| 場景 | 觸發指標 | 實測值 | 閾值 | Alert |
|------|---------|--------|------|-------|
| Connection Storm (95 conn) | `mysql_global_status_threads_connected` | 96 | 70 | `MariaDBHighConnections` FIRING |
| stress-ng (CPU limit 100m) | `tenant:pod_weakest_cpu_percent:max` | 97.3% | 70% | `PodContainerHighCPU` FIRING |
| sysbench (16 threads, 300s) | `mysql_global_status_slow_queries` | 運行中 | — | `MariaDBHighSlowQueries` (視 long_query_time) |
| composite (conn + cpu) | connections AND cpu | — | — | `MariaDBSystemBottleneck` (複合警報) |

## Demo & Scenario 工作流

| 指令 | 行為 | 耗時 |
|------|------|------|
| `make demo` | scaffold → migrate → diagnose → check_alert → patch_config → baseline_discovery | ~45s |
| `make demo-full` | 上述 + composite load → alerts FIRING → cleanup → resolved | ~5min |
| `make test-scenario-a ARGS=--with-load` | 真實連線負載觸發 MariaDBHighConnections | ~3min |
| `make test-scenario-b ARGS=--with-load` | 真實 CPU 壓力觸發 PodContainerHighCPU | ~3min |
| `make test-scenario-e` | Multi-tenant 隔離（閾值修改 + disable metric） | ~3min |
| `make test-scenario-f` | HA 故障切換（Kill Pod → 恢復 → 不翻倍） | ~4min |
| `make load-composite TENANT=db-a` | 複合負載 connections + cpu | 手動 |
| `make baseline-discovery TENANT=db-a` | 觀測指標統計 + 閾值建議 | ~10min |

`--with-load` 模式的意義：展示「相同閾值下，真實負載觸發 alert」，比手動壓低閾值更具說服力。

## 程式碼品質規範

### Shell → Python 注入防護

`_lib.sh` 多處用 inline Python 解析 JSON/YAML。傳入參數時禁止直接嵌入 shell 變數到 Python string literal：

```bash
# ❌ $1 含單引號時注入/斷行
python3 -c "import urllib.parse; print(urllib.parse.quote('$1'))"
# ✅ 透過 stdin 傳遞
echo "$1" | python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))"
```

### Python import 規範

所有 import 放模組頂部（標準庫 → 第三方 → 本地），test method 內不應有 import。

### Shell 測試腳本

- **grep PromQL 特殊字元**：用 `grep -F "max by(tenant)"` 做 literal match，不要用 regex
- **修改輸出格式時**：搜尋所有 `assert_.*contains` 確認是否需要同步更新
- **`set -euo pipefail`**：負載測試輔助腳本不宜用（`kubectl logs` 對 ContainerCreating Pod 返回非零），正式場景測試保留 strict mode
- **migrate_rule.py 分類**：Perfect (metric > threshold)、Complex (含 rate/運算符)、Unparseable (absent/predict_linear)

### CI 同步守衛

CI workflow 與 build script 的工具清單容易 drift。用守衛測試自動偵測（比較 `build.sh` 工具列表 vs CI YAML）。同理 `bump_docs.py` 的 rules 應涵蓋所有帶版號的檔案。

### 文件計數驗證

CHANGELOG / CLAUDE.md 的測試計數必須在 `pytest -v` 執行後才寫入。先跑 pytest → 逐 class 加總交叉驗證 → 再更新文件。

## SAST 合規

| 項目 | 規則 | 驗證 |
|------|------|------|
| Go G112 | `ReadHeaderTimeout` 必設 | `grep ReadHeaderTimeout components/threshold-exporter/app/main.go` |
| Python CWE-276 | 寫檔後必須 `os.chmod(path, 0o600)` | `grep -rn "os.chmod" scripts/tools/` |
| Python B602 | `subprocess` 禁止 `shell=True` | `grep -rn "shell=True" scripts/` (期望: 0 match) |
| Python 編碼 | `open()` 必須帶 `encoding="utf-8"` | `grep -rn "open(" scripts/tools/ \| grep -v encoding` (期望: 0 match) |

全部 11 個 Python 工具均已通過 SAST 驗證 (v1.0.0)。新增 Python 工具寫檔時，每個 `open(..., "w")` 後必須接 `os.chmod(path, 0o600)`。
