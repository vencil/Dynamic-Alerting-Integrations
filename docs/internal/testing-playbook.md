---
title: "測試注意事項 — 排錯手冊 (Testing Playbook)"
tags: [documentation]
audience: [all]
version: v1.13.0
lang: zh
---
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

15 個獨立 ConfigMap 透過 `projected` volume 投射至 `/etc/prometheus/rules/`：

```
configmap-rules-{mariadb,kubernetes,redis,mongodb,elasticsearch,oracle,db2,clickhouse,postgresql,kafka,rabbitmq,jvm,nginx,custom,platform}.yaml
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
| scaling-curve (15→6→3 packs) | 3 輪 | median (range) | ~12min |
| Go micro-bench (`-count=N`) | 5 次 | median, stddev | ~1min |
| under-load | 1 輪（功能性） | 單次值 | ~3min |
| routing-bench (N=2/10/50/100/200) | 5 輪 | median | ~1min |
| alertmanager-bench | 1 輪 (idle) / under-load | 快照 | ~30s |
| reload-bench | 5 輪 | median | ~2min |

**scaling-curve 注意：** 每輪需刪 Rule Pack → 重啟 Prometheus → 等穩定 → 取樣。port-forward 在 Pod 重建後斷開，需重連。Median 比 mean 更能代表穩態。

### Benchmark 在 Dev Container 內執行

benchmark.sh 需要 K8s + Go 環境，**必須在 Dev Container 內跑**。由於 PowerShell 引號嵌套問題，直接 `docker exec bash -c "..."` 複雜指令常失敗。

**標準做法：寫輔助腳本 → docker exec 執行 → 讀結果：**

```bash
# Step 1: Write tool 寫輔助腳本
cat > /workspaces/vibe-k8s-lab/_run_bench.sh << 'SCRIPT'
#!/bin/bash
cd /workspaces/vibe-k8s-lab
exec > /workspaces/vibe-k8s-lab/_bench_result.txt 2>&1
./scripts/benchmark.sh --routing-bench --json
echo "EXIT_CODE=$?"
SCRIPT

# Step 2: 背景執行
docker exec -d vibe-dev-container bash /workspaces/vibe-k8s-lab/_run_bench.sh

# Step 3: 等待 + Read tool 讀 _bench_result.txt
# Step 4: 清理
docker exec vibe-dev-container rm -f /workspaces/vibe-k8s-lab/_run_bench.sh /workspaces/vibe-k8s-lab/_bench_result.txt
```

**Go micro-bench 同理：**
```bash
cd /workspaces/vibe-k8s-lab/components/threshold-exporter/app
go test -bench=. -benchmem -count=5 ./...
```

### benchmark.sh 已知問題

- **`local` 關鍵字限制**：`local` 只能在 function 內使用。若 for loop 在 top-level scope 使用 `local`，bash 不報錯但部分 shell 會。移除即可。
- **`grep -E '--- [0-9]+'` pattern**：`---` 被 grep 解讀為選項旗標。用 `grep -E -- '--- [0-9]+'` 或 `grep -E '\-\-\- [0-9]+'` 避免。此問題不阻塞執行（routes 有 fallback 解析）。

### Dev Container 重啟

系統重開機後 Dev Container 會停止（`Exited (255)`）：

```bash
docker start vibe-dev-container
# 驗證
docker exec vibe-dev-container kubectl get nodes
```

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

### 三線版號管理

Platform、Exporter、da-tools 三條版號線獨立演進。Release 自檢流程：

```bash
# 1. 先用 bump_docs.py 批次更新（只升有變的版號線）
python3 scripts/tools/bump_docs.py --platform 1.9.0 --tools 1.9.0
# ⚠️ 不加 --exporter 則 exporter 版號不動

# 2. 驗證一致性
python3 scripts/tools/bump_docs.py --check

# 3. ⚠️ 避免 replace_all 批次改版號
#    用 Edit tool 的 replace_all 把 "1.8.0" 全改 "1.9.0" 會誤改跨元件版號
#    例：da-tools README 的 exporter 版號被意外升版
#    正確做法：用 bump_docs.py 按版號線分別處理，改完後 --check 驗證
```

## SAST 合規

| 項目 | 規則 | 驗證 |
|------|------|------|
| Go G112 | `ReadHeaderTimeout` 必設 | `grep ReadHeaderTimeout components/threshold-exporter/app/main.go` |
| Python CWE-276 | 寫檔後 `os.chmod(path, 0o600)` | `grep -rn "os.chmod" scripts/tools/` |
| Python B602 | `subprocess` 禁止 `shell=True` | `grep -rn "shell=True" scripts/` (期望: 0) |
| Python 編碼 | `open()` 帶 `encoding="utf-8"` | `grep -rn "open(" scripts/tools/ \| grep -v encoding` (期望: 0) |

新增 Python 工具寫檔時，每個 `open(..., "w")` 後必須接 `os.chmod(path, 0o600)`。

## conf.d/ YAML 格式陷阱

### tenants: wrapper 格式

`conf.d/` 實際使用 **wrapped format**，所有新工具必須處理：

```yaml
# ✅ 實際格式（conf.d/db-a.yaml）
tenants:
  db-a:
    mysql_connections: "70"
    _routing:
      receiver: { type: "webhook", url: "..." }

# ❌ flat 格式（僅用於簡化測試或文件範例）
mysql_connections: "70"
```

**偵測方法：** 載入 YAML 後檢查 `"tenants" in data and isinstance(data["tenants"], dict)`，若存在則 drill into nested structure。

**測試要求：** 每個讀取 conf.d/ 的工具至少需要兩組測試：`test_*_flat()` 和 `test_*_wrapped()`。已知踩坑工具：`config_diff.py`、`blind_spot_discovery.py`。

### 字串匹配：segment vs substring

Job name 到 DB type 的推斷不能用 substring matching：

```python
# ❌ "es" in "prometheus" → True（誤判為 elasticsearch）
# ❌ "db" in "dashboard-backend" → True（誤判為 db2）

# ✅ Segment matching — 以 -_./空格 切割後做完整匹配
segments = set(re.split(r'[-_.\s/]+', job_lower))
for keyword, db_type in JOB_DB_MAP.items():
    if keyword in segments:
        return db_type
```

**回歸測試必備：** 加一個 false-positive case（如 `"prometheus"` → `"unknown"`）防止未來改動退化。

## 自檢方法論

新功能完成後執行兩輪自檢：

**第一輪（正確性）：**
1. 逐檔重讀原始碼 — 聚焦實際 conf.d/ 格式是否對齊
2. 重讀測試 — 確認測試用的 fixture 與真實格式一致
3. 交叉比對 CLAUDE.md 工具表、CHANGELOG 計數、README 工具數

**第二輪（完整性）：**
1. 測試覆蓋盲區 — edge case（timeout、空輸入、格式混合）
2. 跨文件一致性 — `grep -rn` 搜尋版號、工具數、§ 交叉引用
3. `bump_docs.py --check` + `pytest -v` 全套驗證

## 文件敘述風格

### da-tools 容器優先

所有面向使用者的文件（byo-*、migration-guide、shadow-monitoring-sop）中的工具用法，以 `docker run ghcr.io/vencil/da-tools:<ver>` 為主要範例。raw `python3 scripts/tools/...` 寫法僅在「本地開發」或「CI pipeline」上下文使用。

**檢查方式：** `grep -rn "python3 scripts/tools/" docs/` — 面向使用者的文件中不應出現（`docs/internal/` 例外）。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["GitHub Release — 操作手冊 (Playbook)"](internal/github-release-playbook.md) | ⭐⭐ |
| ["測試注意事項 — 排錯手冊 (Testing Playbook)"](internal/testing-playbook.md) | ⭐⭐ |
| ["Windows-MCP — Dev Container 操作手冊 (Playbook)"](internal/windows-mcp-playbook.md) | ⭐⭐ |
