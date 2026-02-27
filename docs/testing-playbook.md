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
| 6 | Projected volume ConfigMap 未生效 | 確認所有 6 個 `configmap-rules-*.yaml` 已 apply；projected volume 要求每個 ConfigMap 都存在 |

## Projected Volume 架構 (v0.5+)

6 個獨立 ConfigMap 透過 `projected` volume 投射至 `/etc/prometheus/rules/`：

```
configmap-rules-{mariadb,kubernetes,redis,mongodb,elasticsearch,platform}.yaml
```

每個 DB Rule Pack 含 `*-recording.yml` + `*-alert.yml`；Platform 含 `platform-alert.yml`。

**常見問題:**
- 少 apply 一個 ConfigMap → Prometheus Pod 啟動失敗（projected volume 要求所有 source 都存在）。
- 修改單個 Rule Pack 只需 apply 對應的 ConfigMap，不影響其他。
- YAML 驗證: `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`。

## 負載注入 (Load Injection) — 設計原則與已知陷阱

### 連線數與 Exporter 共存

MariaDB `max_connections=100`，`mysqld_exporter` (exporter user) 需要至少 1 個連線槽位才能回報 `SHOW STATUS`。

| 連線數 | 結果 | Prometheus 指標 |
|--------|------|-----------------|
| 150 | 101 成功 + 49 拒絕，exporter 被鎖死 | `Threads_connected` 停滯在舊值 (stale) |
| 100 | 全滿，exporter 被鎖死 | 同上 |
| **95** | 95 成功，exporter 正常 | `Threads_connected=96` (95+exporter)，**alert fires** |

**原則：負載連線數 < `max_connections` - 5**，保留足夠槽位給 exporter + 管理操作。95 > 70 (threshold) 即可觸發 `MariaDBHighConnections`。

### Container Image 選擇

| 用途 | Image | 原因 |
|------|-------|------|
| Connection Storm | `python:3.12-alpine` + PyMySQL | 單進程持有多連線，記憶體 ~128Mi |
| CPU Burn (sysbench) | `severalnines/sysbench` | 標準 sysbench OLTP image |
| Container CPU 壓測 | `alpine:3.19` + shell loop | `alexeiled/stress-ng` 在 PATH 找不到 `stress-ng`；alpine shell `while true; do :; done` 最可靠 |
| ~~Connection Storm~~ | ~~`mariadb:11` + bash loop~~ | ❌ 150 個 `mariadb` CLI 進程各吃數 MB → OOM；且 `apt-get install python3` 太慢 (>90s) |

### 單進程 vs 多進程連線持有

❌ **多進程方式** (`for i in $(seq 150); do mariadb -e "SELECT SLEEP(600)" & done`)：
- 每個 CLI 進程 ~5MB，150 個 = 750MB → 超過 container memory limit → OOM Kill
- 回收困難，`kill` 訊號傳遞不確定

✅ **單進程方式** (PyMySQL in Python)：
- 一個 Python process 持有所有 `pymysql.connect()` objects
- 記憶體穩定 ~50MB，即使 95 連線
- `time.sleep(duration)` 後乾淨關閉所有連線

### YAML Heredoc 中嵌入 Python

在 `run_load.sh` 的 YAML `|` block 中放 Python 代碼時：

```yaml
# ❌ heredoc 內嵌 heredoc → YAML parser 崩潰 ("could not find expected ':'")
command: |
  python3 - <<'PYEOF'
  import pymysql
  PYEOF

# ✅ 用 python3 -c "..." inline（雙引號包裹，內部用單引號）
command: |
  python3 -c "
  import pymysql, time, os
  conns = []
  for i in range(95):
      c = pymysql.connect(host='$HOST', ...)
      conns.append(c)
  time.sleep(600)
  "
```

### 負載測試的清理保障

所有負載路徑必須有 cleanup trap，覆蓋三種退出情境：

```bash
# 在 demo.sh / scenario-*.sh 中
cleanup() {
  "${SCRIPT_DIR}/run_load.sh" --cleanup 2>/dev/null || true
  kill ${PF_PID} 2>/dev/null || true
}
trap cleanup EXIT  # Ctrl+C、錯誤退出、正常結束都觸發
```

`run_load.sh --cleanup` 透過 label selector 一次清除所有場景：
```bash
kubectl delete jobs,pods -l app=load-generator --all-namespaces
```

K8s Job 的 `ttlSecondsAfterFinished: 600` 是第二道防線，但不可依賴（controller 可能未啟用）。

### 場景執行順序

同時執行多場景時，Connection Storm 會搶光連線槽位，導致 CPU Burn (sysbench) 無法連線：

| 順序 | 推薦原因 |
|------|---------|
| 1. stress-ng | 不需要 DB 連線，獨立運行 |
| 2. CPU Burn | 需要 DB 連線，先確保能連上 |
| 3. Connection Storm | 放最後，避免搶光連線影響其他場景 |
| *或者* | 每個場景獨立測試，跑完 cleanup 再跑下一個 |

## HA 相關測試 (v0.5+)

### Recording Rules — max vs sum

threshold-exporter 跑多 replica 時，每個 Pod 匯出相同的 `user_threshold`。聚合必須用 `max by(tenant)`，否則閾值翻倍。

```bash
# 驗證: 所有 threshold recording rules 使用 max (不應有 sum)
docker exec vibe-dev-container bash -c "\
  grep -n 'user_threshold' k8s/03-monitoring/configmap-rules-*.yaml \
  > /workspaces/vibe-k8s-lab/output.txt 2>&1"
# 期望: 全部行含 'max by'，0 行含 'sum by'
```

**注意**: 真實 DB 指標 (如 `rate(mysql_...)`) 仍用 `sum by(tenant)`，這是正確的 — 只有 `user_threshold` 需改 `max`。

### HA 部署驗證清單

| 項目 | 驗證指令 | 期望 |
|------|----------|------|
| 2 Pods Running | `kubectl get pods -n monitoring -l app=threshold-exporter` | 2/2 Ready |
| PDB 存在 | `kubectl get pdb -n monitoring` | `minAvailable: 1` |
| AntiAffinity | `kubectl get deploy ... -o jsonpath='{.spec.template.spec.affinity}'` | 含 `podAntiAffinity` |
| RollingUpdate | `kubectl get deploy ... -o jsonpath='{.spec.strategy}'` | `maxUnavailable: 0` |
| Platform alerts | `kubectl exec -n monitoring deploy/prometheus -- ls /etc/prometheus/rules/` | 含 `platform-alert.yml` |

### kubectl scale 注意

`helm upgrade` 後 replicas 可能被覆蓋。若只有 1 個 Pod：
1. 檢查 `values.yaml` 的 `replicaCount` 是否為 2
2. `kubectl scale deploy threshold-exporter -n monitoring --replicas=2` 補救
3. 根因通常是 server-side apply 與 Helm 的 field ownership 衝突

## Performance Benchmark 測試

### 自動化 Benchmark（推薦）

```bash
make benchmark              # 完整報告（規則評估 + CPU/Memory + 儲存/基數 + 擴展估算）
make benchmark ARGS=--json  # JSON 輸出（CI/CD 消費）
```

腳本自動處理 port-forward 建立與清理，無需手動操作。

### 關鍵指標基線 (v0.5.0, 2 tenants, Kind 單節點)

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

### 負載下的 Alert 驗證基線 (v0.7.0)

| 場景 | 觸發指標 | 實測值 | 閾值 | Alert |
|------|---------|--------|------|-------|
| Connection Storm (95 conn) | `mysql_global_status_threads_connected` | 96 | 70 | `MariaDBHighConnections` FIRING ✅ |
| stress-ng (CPU limit 100m) | `tenant:pod_weakest_cpu_percent:max` | 97.3% | 70% | `PodContainerHighCPU` FIRING ✅ |
| sysbench (16 threads, 300s) | `mysql_global_status_slow_queries` | 運行中 | — | `MariaDBHighSlowQueries` (視 long_query_time) |

**用途：** 驗證負載注入改動未破壞 alert pipeline，或確認新場景的觸發條件。

## Demo 工作流

| 指令 | 行為 | 耗時 |
|------|------|------|
| `make demo` | 快速展示：scaffold → migrate → diagnose → check_alert → patch_config | ~30s |
| `make demo-full` | 完整展示：上述 + stress-ng + connection storm → alerts FIRING → cleanup → alerts resolved | ~5min |
| `make load-demo` | 僅負載：啟動 stress-ng + connections，手動觀察 alerts，手動 `make load-cleanup` | 手動 |
| `make test-scenario-a ARGS=--with-load` | 真實連線負載觸發 `MariaDBHighConnections`（不修改閾值） | ~3min |
| `make test-scenario-b ARGS=--with-load` | 真實 CPU 壓力觸發 `PodContainerHighCPU`（不修改閾值） | ~3min |

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

修改程式輸出格式後，舊斷言會失敗。
**原則**: 修改輸出格式時，搜尋所有 `assert_.*contains` 確認是否需要更新。

### 完美 vs 複雜分類規則

`migrate_rule.py` 的分類取決於表達式結構:
- **Perfect**: `metric > threshold` (無函式無運算)
- **Complex**: 含 `rate()`, `/`, 運算符 → 觸發多行 ASCII 警告方塊
- **Unparseable**: 含 `absent()`, `predict_linear()` 等語義不可轉換函式

新增測試案例前，先跑 `--dry-run` 確認實際分類。

### set -euo pipefail 與測試腳本

負載測試的輔助腳本不宜用 `set -euo pipefail`：
- `kubectl logs` 對 ContainerCreating 的 Pod 返回非零 → 腳本提前退出
- 解法：移除 strict mode，對可能失敗的指令加 `|| true`

但 **正式的場景測試腳本** (scenario-a/b/c/d) 保留 `set -euo pipefail`，因為它們需要精確的錯誤偵測。

## SAST 相關測試

| 項目 | 驗證指令 |
|------|----------|
| Go G112 (ReadHeaderTimeout) | `grep ReadHeaderTimeout components/threshold-exporter/app/main.go` |
| Python CWE-276 (file permissions) | `grep -n "os.chmod" scripts/tools/migrate_rule.py scripts/tools/scaffold_tenant.py` |
| Go vet | `docker exec -w .../app vibe-dev-container go vet ./...` |

新增 Python 工具寫檔時，**每個 `open(..., "w")` 後必須接 `os.chmod(path, 0o600)`**。
