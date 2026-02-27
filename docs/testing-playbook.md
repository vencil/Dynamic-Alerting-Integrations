# 測試注意事項 — 排錯手冊 (Testing Playbook)

> 測試前置準備、已知問題、負載注入陷阱、Benchmark 基線。
> **相關文件：** [Windows-MCP Playbook](windows-mcp-playbook.md) (docker exec 模式、Helm 防衝突、Prometheus 查詢)

## 測試前置準備

1. **Dev Container**: `docker ps` → `vibe-dev-container` 運行中。
2. **Kind 叢集**: `docker exec vibe-dev-container kubectl get nodes` 正常。
3. **PyYAML**: `docker exec vibe-dev-container python3 -c "import yaml"` (失敗則 `pip3 install pyyaml`)。
4. **清理殘留**: `docker exec vibe-dev-container pkill -f port-forward` + `make load-cleanup`。

## K8s 環境問題

| # | 問題 | 修復 |
|---|------|------|
| 1 | Helm upgrade 不清理舊 ConfigMap key | `kubectl delete cm threshold-config -n monitoring` → `helm upgrade` |
| 2 | Helm field-manager conflict | 見 [Windows-MCP Playbook → Helm 防衝突流程](windows-mcp-playbook.md#helm-upgrade-防衝突流程) |
| 3 | ConfigMap volume 更新延遲 30-90s | hot-reload 驗證需等 45+ 秒 |
| 4 | Metrics label 順序 (`component,metric,severity,tenant`) | grep 用 `metric=.*tenant=`，不要反過來 |
| 5 | 場景測試殘留值 | 測試前用 `patch_config.py` 恢復預設，負載測試用 `make load-cleanup` |

## Projected Volume 架構

6 個獨立 ConfigMap 透過 `projected` volume 投射至 `/etc/prometheus/rules/`：

```
configmap-rules-{mariadb,kubernetes,redis,mongodb,elasticsearch,platform}.yaml
```

每個 DB Rule Pack 含 `*-recording.yml` + `*-alert.yml`；Platform 含 `platform-alert.yml`。

**`optional: true` 機制（v0.4.0+）：** 所有 projected volume source 均設定 `optional: true`，允許刪除任一 ConfigMap 而不影響 Prometheus 運行。這是 Zero-Crash Opt-Out 的基礎。

**常見問題:**
- 修改單個 Rule Pack 只需 apply 對應的 ConfigMap，不影響其他。
- 刪除 ConfigMap 後 Prometheus 自動移除該 Rule Pack 的規則（需等 volume 同步，約 30-90s）。
- YAML 驗證: `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`。

## 負載注入 (Load Injection) — 設計原則與已知陷阱

### 負載類型概覽

| Type | 指令 | 觸發 Alert | 用途 |
|------|------|-----------|------|
| `connections` | `run_load.sh --type connections` | MariaDBHighConnections | 95 idle connections via PyMySQL |
| `cpu` | `run_load.sh --type cpu` | MariaDBHighSlowQueries | sysbench OLTP 16 threads |
| `stress-ng` | `run_load.sh --type stress-ng` | PodContainerHighCPU | Alpine CPU burn (100m limit) |
| `composite` | `run_load.sh --type composite` | MariaDBSystemBottleneck | connections + cpu 同時啟動 |

### 連線數與 Exporter 共存

MariaDB `max_connections=100`，`mysqld_exporter` 需要至少 1 個連線槽位才能回報 `SHOW STATUS`。

| 連線數 | 結果 | Prometheus 指標 |
|--------|------|-----------------|
| 150 | 101 成功 + 49 拒絕，exporter 被鎖死 | `Threads_connected` 停滯在舊值 (stale) |
| 100 | 全滿，exporter 被鎖死 | 同上 |
| **95** | 95 成功，exporter 正常 | `Threads_connected=96` (95+exporter)，**alert fires** |

**原則：負載連線數 < `max_connections` - 5**，保留足夠槽位給 exporter + 管理操作。

### Container Image 選擇

| 用途 | Image | 原因 |
|------|-------|------|
| Connection Storm | `python:3.12-alpine` + PyMySQL | 單進程持有多連線，記憶體 ~128Mi |
| CPU Burn (sysbench) | `severalnines/sysbench` | 標準 sysbench OLTP image |
| Container CPU 壓測 | `alpine:3.19` + shell loop | `alexeiled/stress-ng` 在 PATH 找不到 `stress-ng`；alpine shell `while true; do :; done` 最可靠 |

❌ 不要用 `mariadb:11` + bash loop 做 Connection Storm（每個 CLI 進程 ~5MB → 150 個 = OOM）。

### 單進程 vs 多進程連線持有

✅ **單進程方式** (PyMySQL)：一個 Python process 持有 95 個 `pymysql.connect()` objects，記憶體 ~50MB。
❌ **多進程方式** (`for i in ...; do mariadb -e "SELECT SLEEP(600)" & done`)：750MB → OOM Kill。

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
  import pymysql, time, os
  conns = [pymysql.connect(host='$HOST', ...) for _ in range(95)]
  time.sleep(600)
  "
```

### 負載清理機制

所有負載路徑必須有 cleanup trap：

```bash
cleanup() {
  "${SCRIPT_DIR}/run_load.sh" --cleanup 2>/dev/null || true
  cleanup_port_forwards
}
trap cleanup EXIT
```

`run_load.sh --cleanup` 透過 label selector 一次清除：`kubectl delete jobs,pods -l app=load-generator --all-namespaces`。

**Type-specific cleanup（dispatch 前）：** 每種負載類型只清自己的前一個 instance（如 `connections` 只刪 `load-conn-*`），不影響其他正在運行的負載。這使 `composite` 類型得以將 `connections` + `cpu` 疊加運行。

### 場景執行順序

同時執行多場景時，Connection Storm 會搶光連線槽位：

| 順序 | 推薦原因 |
|------|---------|
| 1. stress-ng | 不需要 DB 連線 |
| 2. CPU Burn | 需要 DB 連線，先確保能連上 |
| 3. Connection Storm | 放最後，避免搶光連線 |
| *或* `composite` | 自動按正確順序啟動 connections + cpu |

## HA 相關測試

### Recording Rules — max vs sum

threshold-exporter 多 replica 時，每個 Pod 匯出相同的 `user_threshold`。聚合必須用 `max by(tenant)`，否則閾值翻倍。

```bash
# 驗證: 所有 threshold recording rules 使用 max
grep -n 'user_threshold' k8s/03-monitoring/configmap-rules-*.yaml
# 期望: 全部行含 'max by'，0 行含 'sum by'
```

**注意**: 真實 DB 指標 (如 `rate(mysql_...)`) 仍用 `sum by(tenant)`，這是正確的。

### HA 部署驗證清單

| 項目 | 驗證指令 | 期望 |
|------|----------|------|
| 2 Pods Running | `kubectl get pods -n monitoring -l app=threshold-exporter` | 2/2 Ready |
| PDB 存在 | `kubectl get pdb -n monitoring` | `minAvailable: 1` |
| AntiAffinity | `kubectl get deploy ... -o jsonpath='{.spec.template.spec.affinity}'` | 含 `podAntiAffinity` |
| RollingUpdate | `kubectl get deploy ... -o jsonpath='{.spec.strategy}'` | `maxUnavailable: 0` |
| Platform alerts | `kubectl exec -n monitoring deploy/prometheus -- ls /etc/prometheus/rules/` | 含 `platform-alert.yml` |

### HA 故障切換驗證 (Scenario F)

Kill Pod → 驗證三件事：

1. **PDB 保護**：`kubectl delete pod <name> --force` 後至少 1 Pod 仍 Running
2. **Alert 持續**：alert 不因 Pod 重啟而中斷（port-forward 需重建）
3. **閾值不翻倍**：Pod 恢復後 `tenant:alert_threshold:connections` 仍為原值（非 2 倍）

### kubectl scale 注意

`helm upgrade` 後 replicas 可能被覆蓋。根因通常是 server-side apply 與 Helm 的 field ownership 衝突。補救：`kubectl scale deploy threshold-exporter -n monitoring --replicas=2`。

## Multi-Tenant 隔離測試 (Scenario E)

驗證租戶間完全隔離的兩個維度：

| 維度 | 操作 (tenant A) | 驗證 (tenant B) |
|------|----------------|----------------|
| 閾值修改 | `mysql_connections=5` → alert fires | 閾值不變，alert 不觸發 |
| Disable metric | `container_cpu=disable` → metric 消失 | `container_cpu` 仍正常 |

**陷阱**：`--with-load` 模式下，確保負載只注入到 tenant A 的 namespace。`run_load.sh --tenant db-a` 會在 `db-a` namespace 建立 Job，不影響 `db-b`。

## Performance Benchmark 測試

### 自動化 Benchmark（推薦）

```bash
make benchmark              # 完整報告
make benchmark ARGS=--json  # JSON 輸出（CI/CD 消費）
```

### 關鍵指標基線 (v0.8.0, 2 tenants, Kind 單節點)

| 指標 | 值 | 說明 |
|------|----|------|
| 規則總數 | 85 | 33 recording + 35 alert + 17 threshold-norm |
| 規則群組數 | 18 | 6 pack × 3 groups (部分合併) |
| 每週期總評估時間 | ~20.8ms | `sum(prometheus_rule_group_last_duration_seconds)` |
| p50 per-group | 0.59ms | `prometheus_rule_group_duration_seconds{quantile="0.5"}` |
| p99 per-group | 5.05ms | `prometheus_rule_group_duration_seconds{quantile="0.99"}` |
| 空向量 pack 成本 | <1.75ms | ES/Redis/MongoDB 無 exporter 時 |
| Prometheus CPU | ~0.02 cores | `rate(process_cpu_seconds_total{job="prometheus"}[5m])` |
| Prometheus Memory | ~150MB RSS | `process_resident_memory_bytes{job="prometheus"}` |
| 活躍 Series | ~2,800 | `prometheus_tsdb_head_series` |
| user_threshold Series | ~16 (8/tenant) | `count(user_threshold)` |

### 負載下的 Alert 驗證基線 (v0.8.0)

| 場景 | 觸發指標 | 實測值 | 閾值 | Alert |
|------|---------|--------|------|-------|
| Connection Storm (95 conn) | `mysql_global_status_threads_connected` | 96 | 70 | `MariaDBHighConnections` FIRING ✅ |
| stress-ng (CPU limit 100m) | `tenant:pod_weakest_cpu_percent:max` | 97.3% | 70% | `PodContainerHighCPU` FIRING ✅ |
| sysbench (16 threads, 300s) | `mysql_global_status_slow_queries` | 運行中 | — | `MariaDBHighSlowQueries` (視 long_query_time) |
| composite (conn + cpu) | connections AND cpu | — | — | `MariaDBSystemBottleneck` (複合警報) |

## Demo & Scenario 工作流

| 指令 | 行為 | 耗時 |
|------|------|------|
| `make demo` | scaffold → migrate → diagnose → check_alert → patch_config → baseline_discovery | ~45s |
| `make demo-full` | 上述 + composite load (conn+cpu) → alerts FIRING → cleanup → resolved | ~5min |
| `make load-demo` | 啟動 stress-ng + connections，手動觀察 alerts，手動 `make load-cleanup` | 手動 |
| `make test-scenario-a ARGS=--with-load` | 真實連線負載觸發 MariaDBHighConnections | ~3min |
| `make test-scenario-b ARGS=--with-load` | 真實 CPU 壓力觸發 PodContainerHighCPU | ~3min |
| `make test-scenario-e` | Multi-tenant 隔離（閾值修改 + disable metric） | ~3min |
| `make test-scenario-e ARGS=--with-load` | 隔離測試 + 真實負載到 tenant A | ~4min |
| `make test-scenario-f` | HA 故障切換（Kill Pod → 恢復 → 不翻倍） | ~4min |
| `make load-composite TENANT=db-a` | 複合負載 connections + cpu | 手動 |
| `make baseline-discovery TENANT=db-a` | 觀測指標統計 + 閾值建議 | ~10min |

`--with-load` 模式的意義：展示「相同閾值下，真實負載觸發 alert」，比手動壓低閾值更具說服力。

## Shell 測試腳本陷阱

### grep 正則特殊字元 (PromQL)

```bash
# ❌ ( 和 * 是 regex
grep "sum by.tenant..*(rate" file.yaml
# ✅ 用 -F 做 literal match
grep -F "max by(tenant)" file.yaml
```

### 測試斷言必須隨程式碼同步更新

修改程式輸出格式後，舊斷言會失敗。**原則**: 修改輸出格式時，搜尋所有 `assert_.*contains` 確認是否需要更新。

### migrate_rule.py 分類規則

- **Perfect**: `metric > threshold` (無函式無運算)
- **Complex**: 含 `rate()`, `/`, 運算符 → 觸發多行 ASCII 警告方塊
- **Unparseable**: 含 `absent()`, `predict_linear()` 等語義不可轉換函式

### set -euo pipefail 與測試腳本

負載測試的輔助腳本不宜用 `set -euo pipefail`（`kubectl logs` 對 ContainerCreating 的 Pod 返回非零）。但**正式場景測試** (scenario-a~f) 保留 strict mode。

## SAST 合規驗證

| 項目 | 規則 | 驗證 |
|------|------|------|
| Go G112 | `ReadHeaderTimeout` 必設 | `grep ReadHeaderTimeout components/threshold-exporter/app/main.go` |
| Python CWE-276 | 寫檔後必須 `os.chmod(path, 0o600)` | `grep -rn "os.chmod" scripts/tools/` |
| Python B602 | `subprocess` 禁止 `shell=True` | `grep -rn "shell=True" scripts/` (期望: 0 match) |

**全部 10 個 Python 工具均已通過 SAST 驗證** (v0.8.0)：patch_config, check_alert, diagnose, migrate_rule, scaffold_tenant, validate_migration, offboard_tenant, deprecate_rule, baseline_discovery, metric-dictionary.yaml (非 Python，不適用)。

新增 Python 工具寫檔時，**每個 `open(..., "w")` 後必須接 `os.chmod(path, 0o600)`**。
