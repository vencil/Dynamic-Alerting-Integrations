# 測試注意事項 — 排錯手冊 (Testing Playbook)

> K8s 環境排錯、負載注入陷阱、Benchmark 方法論、程式碼品質規範。
> **相關文件：** [Windows-MCP Playbook](windows-mcp-playbook.md) (docker exec 模式、Shell 陷阱) | [GitHub Release Playbook](github-release-playbook.md) (push + release 流程)

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

13 個獨立 ConfigMap 透過 `projected` volume 投射至 `/etc/prometheus/rules/`：

```
configmap-rules-{mariadb,kubernetes,redis,mongodb,elasticsearch,oracle,db2,clickhouse,postgresql,kafka,rabbitmq,custom,platform}.yaml
```

每個 DB Rule Pack 含 `*-recording.yml` + `*-alert.yml`；Platform 含 `platform-alert.yml`。全部設定 `optional: true`（Zero-Crash Opt-Out）。

修改單個 Rule Pack 只需 apply 對應 ConfigMap。刪除後 Prometheus 自動移除規則（volume 同步延遲 30-90s）。

## configmap-reload Sidecar 行為

**關鍵洞察：** configmap-reload sidecar 監聽的是 Projected Volume 的**檔案內容變更**，不是 ConfigMap annotation 或 metadata。

- `--apply` 模式：直接更新 ConfigMap `data` → 觸發 `/-/reload` API → **不依賴 sidecar 輪詢週期**
- 僅修改 annotation 而 data 不變 → sidecar **不會偵測到變更**
- 測試 reload 時，必須改變實際 config 內容；kubectl annotate 無法觸發 sidecar

## 負載注入 (Load Injection)

### 負載類型

| Type | 指令 | 觸發 Alert | 機制 |
|------|------|-----------|------|
| `connections` | `run_load.sh --type connections` | MariaDBHighConnections | 95 idle connections via PyMySQL |
| `cpu` | `run_load.sh --type cpu` | MariaDBHighSlowQueries | sysbench OLTP 16 threads |
| `stress-ng` | `run_load.sh --type stress-ng` | PodContainerHighCPU | Alpine CPU burn (100m limit) |
| `composite` | `run_load.sh --type composite` | MariaDBSystemBottleneck | connections + cpu 同時 |

### 關鍵設計約束

**連線數 < `max_connections` - 5：** MariaDB `max_connections=100`，`mysqld_exporter` 需至少 1 個連線槽。設 95 條連線時 exporter 正常，alert fires（`Threads_connected=96`）。100 以上 exporter 被鎖死，指標 stale。

**單進程持連：** 一個 Python process 持有 95 個 `pymysql.connect()` objects（~50MB）。不要用多進程 `mariadb -e "SELECT SLEEP(600)" &`（OOM）。

**Composite 在 Kind 可能觸發 MariaDBDown：** 95 idle + sysbench 額外連線可超 `max_connections`。僅觸發 connections alert 用 `--type connections` 單獨注入。

### Container Image

| 用途 | Image | 原因 |
|------|-------|------|
| Connection Storm | `python:3.12-alpine` + PyMySQL | 單進程持多連線，~128Mi |
| CPU Burn (sysbench) | `severalnines/sysbench` | 標準 OLTP image |
| Container CPU 壓測 | `alpine:3.19` + shell loop | `stress-ng` image 有 PATH 問題；`while true; do :; done` 最可靠 |

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

### 場景執行順序

同時跑多場景時，Connection Storm 會搶光連線：1) stress-ng → 2) cpu → 3) connections。或直接用 `composite`（自動按正確順序）。

## HA 相關測試

### Recording Rules — max vs sum

threshold-exporter 多 replica 時，每個 Pod 匯出相同 `user_threshold`。聚合必須用 `max by(tenant)`，否則閾值翻倍。真實 DB 指標仍用 `sum by(tenant)`。

### HA 部署驗證

| 項目 | 驗證指令 | 期望 |
|------|----------|------|
| 2 Pods Running | `kubectl get pods -n monitoring -l app=threshold-exporter` | 2/2 Ready |
| PDB 存在 | `kubectl get pdb -n monitoring` | `minAvailable: 1` |
| AntiAffinity | `kubectl get deploy ... -o jsonpath='{.spec.template.spec.affinity}'` | 含 `podAntiAffinity` |
| RollingUpdate | `kubectl get deploy ... -o jsonpath='{.spec.strategy}'` | `maxUnavailable: 0` |

### HA 故障切換 (Scenario F)

Kill Pod → 驗證：1) PDB 保護 1 Pod Running；2) Alert 持續不中斷；3) 閾值不翻倍。

`helm upgrade` 後 replicas 可能被覆蓋 → `kubectl scale deploy threshold-exporter -n monitoring --replicas=2`。

## Performance Benchmark

### 自動化

```bash
make benchmark                    # idle-state 基礎報告
make benchmark ARGS="--under-load --scaling-curve --routing-bench --alertmanager-bench --reload-bench --json"
```

### 方法論

| Benchmark 類型 | 建議輪數 | 報告格式 | 耗時 |
|----------------|---------|---------|------|
| idle-state | 5 輪，間隔 45s | mean ± stddev | ~4min |
| scaling-curve (12→6→3 packs) | 3 輪 | median (range) | ~12min |
| Go micro-bench (`-count=N`) | 5 次 | median, stddev | ~1min |
| under-load | 1 輪（功能性） | 單次值 | ~3min |
| routing-bench (N=2/10/50/100/200) | 5 輪 | median | ~1min |
| alertmanager-bench | 1 輪 (idle) / under-load | 快照 | ~30s |
| reload-bench | 5 輪 | median | ~2min |

**scaling-curve 注意：** 每輪需刪 Rule Pack → 重啟 Prometheus → 等穩定 → 取樣。port-forward 在 Pod 重建後斷開，需重連。Median 比 mean 更能代表穩態。

### Routing Bench 注意事項

- **純 Python 操作**，不需 K8s 環境；Cowork VM 可直接跑
- 產出解析：用 summary line `--- N route(s), M receiver(s), K inhibit rule(s) ---` 取計數，不要 grep YAML 內容（`- match:` vs `- matchers:` 格式隨版本變化）
- 合成 tenant 需包含 routing（6 種 receiver type）+ severity_dedup + routing overrides，才能代表真實複雜度

### Alertmanager Bench 注意事項

- idle 狀態下 notification latency histogram 為空（無 alert 觸發）→ 需 `--under-load` 或 `make demo-full` 產生流量才有數據
- Alertmanager port-forward: `kubectl port-forward svc/alertmanager 9093:9093 -n monitoring`
- 關鍵 metrics: `alertmanager_notification_latency_seconds`、`alertmanager_alerts_received_total`、`alertmanager_nflog_maintenance_errors_total`

### Reload Bench 注意事項

- `/-/reload` API 本身 sub-millisecond（~0.3ms）
- `--apply` E2E 瓶頸在 kubectl API server 交互（~600ms），非 route generation 或 reload
- **sidecar 不可靠觸發**：僅改 annotation 不觸發 sidecar（見上方 sidecar 行為章節）
- Kind 環境 E2E ~760ms；生產環境（dedicated etcd）預期 < 500ms

### 負載下的 Alert 驗證基線

| 場景 | 觸發指標 | 實測值 | 閾值 | Alert |
|------|---------|--------|------|-------|
| Connection Storm (95 conn) | `mysql_global_status_threads_connected` | 96 | 70 | `MariaDBHighConnections` FIRING |
| stress-ng (CPU limit 100m) | `tenant:pod_weakest_cpu_percent:max` | 97.3% | 70% | `PodContainerHighCPU` FIRING |
| sysbench (16 threads) | `mysql_global_status_slow_queries` | 運行中 | — | `MariaDBHighSlowQueries` |
| composite | connections AND cpu | — | — | `MariaDBSystemBottleneck` |

## Demo & Scenario 工作流

| 指令 | 行為 | 耗時 |
|------|------|------|
| `make demo` | scaffold → migrate → diagnose → check_alert → patch_config → baseline_discovery | ~45s |
| `make demo-full` | 上述 + composite load → alerts FIRING → cleanup → resolved | ~5min |
| `make test-scenario-a ARGS=--with-load` | 真實連線負載觸發 MariaDBHighConnections | ~3min |
| `make test-scenario-b ARGS=--with-load` | 真實 CPU 壓力觸發 PodContainerHighCPU | ~3min |
| `make test-scenario-e` | Multi-tenant 隔離（閾值修改 + disable metric） | ~3min |
| `make test-scenario-f` | HA 故障切換（Kill Pod → 恢復 → 不翻倍） | ~4min |

## 程式碼品質規範

### Shell → Python 注入防護

```bash
# ❌ $1 含單引號時注入/斷行
python3 -c "import urllib.parse; print(urllib.parse.quote('$1'))"
# ✅ 透過 stdin 傳遞
echo "$1" | python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))"
```

### Python import 規範

所有 import 放模組頂部（標準庫 → 第三方 → 本地），test method 內不應有 import。

### Shell 測試注意

- **grep PromQL 特殊字元**：用 `grep -F "max by(tenant)"` 做 literal match
- **修改輸出格式時**：搜尋所有 `assert_.*contains` 確認同步更新
- **`set -euo pipefail`**：負載測試輔助腳本不宜用（`kubectl logs` 對 ContainerCreating Pod 返回非零）
- **migrate_rule.py 分類**：Perfect / Complex / Unparseable

### CI 同步守衛

CI workflow 與 build script 的工具清單容易 drift。用守衛測試自動偵測。`bump_docs.py` 的 rules 應涵蓋所有帶版號的檔案。

### 文件計數驗證

CHANGELOG / CLAUDE.md 的測試計數必須在 `pytest -v` 執行後才寫入。先跑 pytest → 逐 class 加總交叉驗證 → 再更新文件。

## SAST 合規

| 項目 | 規則 | 驗證 |
|------|------|------|
| Go G112 | `ReadHeaderTimeout` 必設 | `grep ReadHeaderTimeout components/threshold-exporter/app/main.go` |
| Python CWE-276 | 寫檔後 `os.chmod(path, 0o600)` | `grep -rn "os.chmod" scripts/tools/` |
| Python B602 | `subprocess` 禁止 `shell=True` | `grep -rn "shell=True" scripts/` (期望: 0) |
| Python 編碼 | `open()` 帶 `encoding="utf-8"` | `grep -rn "open(" scripts/tools/ \| grep -v encoding` (期望: 0) |

新增 Python 工具寫檔時，每個 `open(..., "w")` 後必須接 `os.chmod(path, 0o600)`。
