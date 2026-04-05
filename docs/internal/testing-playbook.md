---
title: "測試注意事項 — 排錯手冊 (Testing Playbook)"
tags: [documentation]
audience: [all]
version: v2.4.0
lang: zh
---
# 測試注意事項 — 排錯手冊 (Testing Playbook)

> K8s 環境排錯、負載注入陷阱、程式碼品質規範。
> **相關文件：** [Benchmark Playbook](benchmark-playbook.md)（方法論、踩坑）· [Windows-MCP Playbook](windows-mcp-playbook.md)（docker exec 模式）· [GitHub Release Playbook](github-release-playbook.md)（push + release 流程）

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

## JSX Dependency Loading & Portal Modularization

Portal 中的三個核心模組（portal-shared、tabs 三層）由 `jsx-loader.html` 透過 **`dependencies` frontmatter** 控制載入順序：

- **Sequential Order**: `portal-shared.jsx` (共享元件) → `{tab1,tab2,tab3}.jsx` (三個頁籤)
- **Frontmatter 格式**: 各檔案開頭宣告 `dependencies: [portal-shared]`；portal-shared 無依賴
- **測試陷阱**: 修改 portal-shared 會連動影響三個 tabs，需完整迴歸測試；單獨改某 tab 風險較低

修改任何 portal 檔案後，在 `docs/interactive/tools/` 驗證載入順序無誤，並確認 jsx-loader 的 CUSTOM_FLOW_MAP 已同步新增工具。

## CI Matrix × Snapshot Testing（Phase .d）

### GitHub Actions CI 運行

CI matrix 配置：Python 3.10/3.13 × Go 1.22/1.26，8 種組合平行執行。新工具應在本地通過 `pytest -v --cov-fail-under=85` 且 `mypy scripts/tools/_lib_*.py` 無誤後才提交。

### Snapshot 測試工作流

`test_snapshot.py` 驗證 help text 穩定性。首次執行用 `pytest --snapshot-update`，將 help 輸出存至 `.snapshot/`；後續執行自動比對。修改工具 help text 時：

```bash
python3 -m pytest tests/test_snapshot.py::test_tool_help_mariadb --snapshot-update
# 驗證 .snapshot/ 變更後再 commit
```

Exit code 合約測試（`test_tool_exit_codes.py`）覆蓋全部 84+ 工具的 `--help` / invalid args，預期 exit code 0 (成功) 或 2 (CLI 誤用)。

## Performance Benchmark

> **已拆分為獨立 Playbook：** [Benchmark Playbook](benchmark-playbook.md)（方法論、執行環境、踩坑記錄）
> **量測數據：** [benchmarks.md](../benchmarks.md) · **pytest-benchmark 基線表：** [test-map.md § Benchmark 基線](test-map.md#benchmark-基線)

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
python3 scripts/tools/dx/bump_docs.py --platform 1.9.0 --tools 1.9.0
# ⚠️ 不加 --exporter 則 exporter 版號不動

# 2. 驗證一致性
python3 scripts/tools/dx/bump_docs.py --check

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

## v2.1.0 Lessons Learned（2026-03-15）

### Go Incremental Reload 設計模式

1. **per-file hash + parsed config cache 是增量 reload 的核心**：`fileHashes map[string]string` 追蹤每個檔案的 SHA-256，`fileConfigs map[string]ThresholdConfig` 快取已解析的部分配置。變更偵測只需比對 hash，未變更檔案直接從 cache 取用
2. **4 phase 增量載入保證正確性**：(1) scan hashes → (2) diff changed/added/removed → (3) selective re-parse only changed → (4) mergePartialConfigs from cache。Phase 4 的 deterministic merge（sorted filenames）確保結果與 fullDirLoad 一致
3. **boundary rules 需要在 merge 後重新套用**：cardinality guard (500 per-tenant) 和 schema validation 必須在最終合併後執行，不能在 partial config 階段做

### Backstage Plugin 整合模式

4. **Entity annotation 是 Backstage ↔ 外部系統的慣例橋樑**：`dynamic-alerting.io/tenant` annotation 標註在 Backstage entity 上，plugin 讀取此 annotation 自動映射到對應 tenant 的 Prometheus 查詢
5. **Backstage proxy 避免 CORS 問題**：PrometheusClient 透過 `/api/proxy/prometheus/` 路徑查詢，不直接從前端連 Prometheus。proxy 配置在 `app-config.yaml`

### 覆蓋率攻略技巧

6. **time.sleep mock 用 module-level patch**：`monkeypatch.setattr(baseline_discovery.time, "sleep", lambda s: None)` 而非 `@patch("time.sleep")`，確保只 mock 目標模組的 sleep 不影響其他模組
7. **觀測迴圈測試需要同時 mock query + sleep + file I/O**：`query_prometheus` 回傳固定數據、`time.sleep` no-op、`tmp_path` 接收 CSV 輸出，三者缺一不可
8. **CSV 輸出驗證用 csv.reader 而非字串比對**：`csv.reader()` 自動處理 quoting 和 escaping，比 `split(",")` 更健壯

### DX 工具測試模式

9. **frontmatter 解析需處理 `---` delimiter edge cases**：檔案開頭非 `---`、frontmatter 未閉合、多個 `---` 區段都需要測試。用 `re.compile(r"^---\s*$")` 比固定字串比對更寬鬆
10. **coverage text output 格式依 pytest-cov 版本不同**：regex pattern 需足夠寬鬆以匹配不同版本的空白和對齊，`r"^(\S+\.py)\s+(\d+)\s+(\d+)\s+(\d+)%\s*(.*)?$"` 涵蓋主流格式
11. **monkeypatch triple globals 隔離檔案系統**：`check_frontmatter_versions` 同時依賴 `DOCS_DIR`、`REPO_ROOT`、`CLAUDE_MD` 三個 module-level 常數，全部需要 monkeypatch 到 tmp_path

---

## v2.1.0 Lessons Learned（2026-03-15）

### 關聯分析演算法測試

1. **時間相關測試需 freeze 或相對值**：`_time_overlap()` 內部用 `datetime.now()` 處理 "still firing" 告警。測試中 `end==start` 不等於 "零長度" 而是 "still firing"，需理解業務語義再寫斷言
2. **4 因子關聯分數好測試**：每個因子獨立 0-1 且權重固定，可分別測試 identical、diff namespace、diff everything 三種極端情況快速驗證正確性
3. **根因推斷用 severity rank + earliest 雙排序**：比單一排序穩定且可預測，測試只需驗證返回的 alertname 和 tenant

### 漂移偵測測試模式

4. **tmp_path fixture + write_text 是最佳 config-dir mock**：不需要真實 YAML 結構，只需要可 hash 的內容。SHA-256 確定性保證測試穩定
5. **pairwise 組合數 = n*(n-1)/2**：三目錄產生 3 個 report，四目錄產生 6 個——測試時注意斷言 report 數量而非內容
6. **expected vs unexpected 用 prefix tuple**：`EXPECTED_PREFIXES = ("_cluster_", "_local_")` 可自定義，測試時用自定義 prefix 驗證分類邏輯

### 覆蓋率提升技巧

7. **main() 的 sys.exit() 要 catch**：validate_all.py main() 結尾固定 `sys.exit(0/1)`，pytest 需要 `pytest.raises(SystemExit)` 包裹
8. **mock _run_one 跳過子進程**：validate_all 內部用 subprocess 跑其他 Python 腳本，mock `_run_one` 回傳 `(name, "pass", 0.1, "ok", "output")` tuple 即可覆蓋 main() 邏輯
9. **_init_changelog_entry 需 monkeypatch REPO_ROOT**：bump_docs 的 CHANGELOG 操作依賴 REPO_ROOT，tmp_path mock 後可安全測試插入邏輯

### DX lint 測試模式

10. **CJK ratio 測試用純中文/純英文/混合三極端**：count_cjk_ratio("你好世界")=1.0, ("Hello")=0.0, 混合在 0-1 之間，避免浮點精確比較用 range 斷言
11. **monkeypatch 雙 global**：check_bilingual_content 同時用 DOCS_DIR 和 PROJECT_ROOT，兩者都需要 monkeypatch 到 tmp_path 才能隔離真實文件系統

---

## v2.1.0 Lessons Learned（2026-03-15）

### Ops 工具測試模式

1. **main() 覆蓋是低垂果實**：大多數 ops 工具的 `main()` CLI entry point 佔 30-40% 程式碼但常被忽略。使用 `monkeypatch.setattr(sys, "argv", [...])` + mock 外部依賴即可快速提升覆蓋率（batch_diagnose 71%→99%，blind_spot 74%→99%）
2. **mock 外部 API 的安全模式**：`query_prometheus_targets()` 等函式用 `@patch("module.http_get_json")` 而非 `@patch("urllib.request.urlopen")`，mock 粒度在自己的 wrapper 層
3. **Help text 與 COMMAND_MAP 不同步是常見漏洞**：`validate-config` 在 COMMAND_MAP 中但 help text 沒列。`check_cli_coverage.py` lint 工具可捕獲此類不同步
4. **Triple-quoted string parsing**：解析 Python help text 時，用 `re.findall(r'"""(.*?)"""', content, re.DOTALL)` 限制在三引號字串內，避免匹配到 Python 程式碼中的變數名

### Lint 工具模式

5. **反向驗證 > 正向驗證**：以 COMMAND_MAP 為 single source of truth，反向檢查 4 份文件是否涵蓋，比在每份文件中各自維護清單更可靠
6. **Warning vs Error 分級**：docs 裡多出的命令（已規劃但未整合）是 warning 非 error，避免 CI 假陽性

### DX 增強模式

7. **`--diff-report` 實作要注意 git restore**：fix → diff → `git checkout .` 三步驟，timeout 時仍須執行 restore
8. **`--format summary` badge 風格**：一行輸出適合嵌入 CI badge 或 Makefile target echo

## v2.2.0 Lessons Learned（2026-03-18）

1. **`apk del` 後必須驗證移除成功**：`|| true` 吃掉錯誤導致 CVE 殘留。Dockerfile 加 `if apk info -e <pkg>; then exit 1; fi` 做 build-time 斷言
2. **git-sync `--link=current` 建立 symlink**：下游 `--config-dir` 路徑必須含 `/current/`。加 initContainer `--one-time` 防 exporter 啟動時讀到空目錄
3. **不寫虛構 benchmark**：沒有實測數據就用 O() 定性分析 + 引導客戶用工具自行驗證。虛構數字被質疑會失去信任
4. **Dockerfile 註解必須與 base image 版本同步**：改 pin 版本時連註解一起改，否則 review 時製造混淆

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["GitHub Release — 操作手冊 (Playbook)"](github-release-playbook.md) | ⭐⭐ |
| ["測試注意事項 — 排錯手冊 (Testing Playbook)"](testing-playbook.md) | ⭐⭐ |
| ["Windows-MCP — Dev Container 操作手冊 (Playbook)"](windows-mcp-playbook.md) | ⭐⭐ |
