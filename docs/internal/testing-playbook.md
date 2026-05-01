---
title: "測試注意事項 — 排錯手冊 (Testing Playbook)"
tags: [documentation]
audience: [all]
version: v2.7.0
verified-at-version: v2.8.0
lang: zh
---
# 測試注意事項 — 排錯手冊 (Testing Playbook)

> K8s 環境排錯、負載注入陷阱、程式碼品質規範。
> **相關文件：** [Benchmark Playbook](benchmark-playbook.md)（方法論、踩坑）· [Windows-MCP Playbook](windows-mcp-playbook.md)（docker exec 模式）· [GitHub Release Playbook](github-release-playbook.md)（push + release 流程）

### Quick Action Index

> AI agent 直接跳到需要的操作步驟，跳過敘事。

| 我要做什麼 | 跳到 |
|-----------|------|
| 跑測試前準備 | [§測試前置準備](#測試前置準備) |
| K8s 環境問題排錯 | [§K8s 環境問題](#k8s-環境問題) |
| 負載注入（connections/cpu） | [§負載注入](#負載注入-load-injection) |
| conf.d/ YAML 格式問題 | [§conf.d/ YAML 格式陷阱](#confd-yaml-格式陷阱) |
| SAST 規則合規 | [§SAST 合規](#sast-合規) |
| Playwright E2E | [§Playwright E2E](#playwright-e2e-測試portal-smoke-tests) |
| Go 並發 flake 修法 | [§v2.6.x Go 並發測試 flake](#v26x-lessons-learned-go-並發測試-flake2026-04-11) |
| 程式碼品質規範 | [§程式碼品質規範](#程式碼品質規範) |

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

CI matrix 配置：Python 3.13 × Go 1.26（自 v2.5.0 起統一為單一版本）。新工具應在本地通過 `pytest -v --cov-fail-under=85` 且 `mypy scripts/tools/_lib_*.py` 無誤後才提交。

### Snapshot 測試工作流

`test_snapshot.py` 驗證 help text 穩定性。首次執行用 `pytest --snapshot-update`，將 help 輸出存至 `.snapshot/`；後續執行自動比對。修改工具 help text 時：

```bash
python3 -m pytest tests/test_snapshot.py::test_tool_help_mariadb --snapshot-update
# 驗證 .snapshot/ 變更後再 commit
```

Exit code 合約測試（`test_tool_exit_codes.py`）覆蓋全部 84+ 個 CLI entrypoint 的 `--help` / invalid args，預期 exit code 0 (成功) 或 2 (CLI 誤用)。（全 repo 含 96 個 Python 工具模組，此處僅測有 CLI 進入點的工具。）

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

### 版號管理

> **完整版號治理流程見 [GitHub Release Playbook](github-release-playbook.md)**。
> 五條獨立版號線（`v*` platform / `exporter/v*` / `tools/v*` / `portal/v*` / `tenant-api/v*`）各有各的生命週期。

測試相關的版號重點：

- **`bump_docs.py --check`** 驗證全 repo 版號一致性（pre-commit hook `version-consistency` 自動執行）
- **避免 `replace_all` 批次改版號**：用 `bump_docs.py` 按版號線分別處理，改完後 `--check` 驗證
- **文件計數**必須在 `pytest -v` 執行後才寫入（見上方「文件計數驗證」）

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

## v2.1.0 Lessons Learned — Go / Backstage / 覆蓋率（2026-03-15）

> **自動化覆蓋摘要：** 本節 11 條中，#9 frontmatter 解析已有 `test_check_frontmatter_versions.py` 覆蓋（🛡️），#10 coverage 格式已有 `test_coverage_gap_analysis.py` 覆蓋（🛡️），#11 triple globals 已有 `test_check_frontmatter_versions.py` 覆蓋（🛡️）。其餘為設計模式知識，不適合自動化。

### Go Incremental Reload 設計模式

1. **per-file hash + parsed config cache 是增量 reload 的核心**：`fileHashes map[string]string` 追蹤每個檔案的 SHA-256，`fileConfigs map[string]ThresholdConfig` 快取已解析的部分配置。變更偵測只需比對 hash，未變更檔案直接從 cache 取用
2. **4 phase 增量載入保證正確性**：(1) scan hashes → (2) diff changed/added/removed → (3) selective re-parse only changed → (4) mergePartialConfigs from cache。Phase 4 的 deterministic merge（sorted filenames）確保結果與 fullDirLoad 一致
3. **boundary rules 需要在 merge 後重新套用**：cardinality guard (500 per-tenant) 和 schema validation 必須在最終合併後執行，不能在 partial config 階段做

### Backstage Plugin 整合模式

> 🗄️ #4-5 已歸檔至 [archive/lessons-learned.md](archive/lessons-learned.md)（Backstage 整合已穩定）

### 覆蓋率攻略技巧

6. **time.sleep mock 用 module-level patch**：`monkeypatch.setattr(baseline_discovery.time, "sleep", lambda s: None)` 而非 `@patch("time.sleep")`，確保只 mock 目標模組的 sleep 不影響其他模組
7. **觀測迴圈測試需要同時 mock query + sleep + file I/O**：`query_prometheus` 回傳固定數據、`time.sleep` no-op、`tmp_path` 接收 CSV 輸出，三者缺一不可
8. **CSV 輸出驗證用 csv.reader 而非字串比對**：`csv.reader()` 自動處理 quoting 和 escaping，比 `split(",")` 更健壯

### DX 工具測試模式

9. 🛡️ **frontmatter 解析需處理 `---` delimiter edge cases**：檔案開頭非 `---`、frontmatter 未閉合、多個 `---` 區段都需要測試。用 `re.compile(r"^---\s*$")` 比固定字串比對更寬鬆 `[已自動化於 test: test_check_frontmatter_versions]`
10. 🛡️ **coverage text output 格式依 pytest-cov 版本不同**：regex pattern 需足夠寬鬆以匹配不同版本的空白和對齊，`r"^(\S+\.py)\s+(\d+)\s+(\d+)\s+(\d+)%\s*(.*)?$"` 涵蓋主流格式 `[已自動化於 test: test_coverage_gap_analysis]`
11. 🛡️ **monkeypatch triple globals 隔離檔案系統**：`check_frontmatter_versions` 同時依賴 `DOCS_DIR`、`REPO_ROOT`、`CLAUDE_MD` 三個 module-level 常數，全部需要 monkeypatch 到 tmp_path `[已自動化於 test: test_check_frontmatter_versions]`

---

## v2.1.0 Lessons Learned — 關聯分析 / 漂移偵測 / Lint（2026-03-15）

> **自動化覆蓋摘要：** 本節 11 條中，#1-3 已有 `test_alert_correlate.py` 覆蓋（🛡️），#4-6 已有 `test_drift_detect.py` / `test_config_diff.py` 覆蓋（🛡️），#7-8 已有 `test_validate_all.py` 覆蓋（🛡️），#9 已有 `test_bump_docs.py` 覆蓋（🛡️），#10-11 已有 `test_check_bilingual_content.py` 覆蓋（🛡️）。本節全部已有測試防守。

### 關聯分析演算法測試

1. 🛡️ **時間相關測試需 freeze 或相對值**：`_time_overlap()` 內部用 `datetime.now()` 處理 "still firing" 告警。測試中 `end==start` 不等於 "零長度" 而是 "still firing"，需理解業務語義再寫斷言 `[已自動化於 test: test_alert_correlate]`
2. 🛡️ **4 因子關聯分數好測試**：每個因子獨立 0-1 且權重固定，可分別測試 identical、diff namespace、diff everything 三種極端情況快速驗證正確性 `[已自動化於 test: test_alert_correlate]`
3. 🛡️ **根因推斷用 severity rank + earliest 雙排序**：比單一排序穩定且可預測，測試只需驗證返回的 alertname 和 tenant `[已自動化於 test: test_alert_correlate]`

### 漂移偵測測試模式

4. 🛡️ **tmp_path fixture + write_text 是最佳 config-dir mock**：不需要真實 YAML 結構，只需要可 hash 的內容。SHA-256 確定性保證測試穩定 `[已自動化於 test: test_drift_detect]`
5. 🛡️ **pairwise 組合數 = n*(n-1)/2**：三目錄產生 3 個 report，四目錄產生 6 個——測試時注意斷言 report 數量而非內容 `[已自動化於 test: test_drift_detect]`
6. 🛡️ **expected vs unexpected 用 prefix tuple**：`EXPECTED_PREFIXES = ("_cluster_", "_local_")` 可自定義，測試時用自定義 prefix 驗證分類邏輯 `[已自動化於 test: test_drift_detect]`

### 覆蓋率提升技巧

7. 🛡️ **main() 的 sys.exit() 要 catch**：validate_all.py main() 結尾固定 `sys.exit(0/1)`，pytest 需要 `pytest.raises(SystemExit)` 包裹 `[已自動化於 test: test_validate_all]`
8. 🛡️ **mock _run_one 跳過子進程**：validate_all 內部用 subprocess 跑其他 Python 腳本，mock `_run_one` 回傳 `(name, "pass", 0.1, "ok", "output")` tuple 即可覆蓋 main() 邏輯 `[已自動化於 test: test_validate_all]`
9. 🛡️ **_init_changelog_entry 需 monkeypatch REPO_ROOT**：bump_docs 的 CHANGELOG 操作依賴 REPO_ROOT，tmp_path mock 後可安全測試插入邏輯 `[已自動化於 test: test_bump_docs]`

### DX lint 測試模式

10. 🛡️ **CJK ratio 測試用純中文/純英文/混合三極端**：count_cjk_ratio("你好世界")=1.0, ("Hello")=0.0, 混合在 0-1 之間，避免浮點精確比較用 range 斷言 `[已自動化於 test: test_check_bilingual_content]`
11. 🛡️ **monkeypatch 雙 global**：check_bilingual_content 同時用 DOCS_DIR 和 PROJECT_ROOT，兩者都需要 monkeypatch 到 tmp_path 才能隔離真實文件系統 `[已自動化於 test: test_check_bilingual_content]`

---

## v2.1.0 Lessons Learned — Ops 工具 / DX 增強（2026-03-15）

> **自動化覆蓋摘要：** 本節 8 條中，#1 的 main() 覆蓋模式已在多個 test 檔案實踐（🛡️），#3 Help text 同步已有 `check_cli_coverage.py` hook 防守（🛡️），#5 反向驗證模式已有 `check_cli_coverage.py` 實作（🛡️）。其餘為測試技巧知識。

### Ops 工具測試模式

1. 🛡️ **main() 覆蓋是低垂果實**：大多數 ops 工具的 `main()` CLI entry point 佔 30-40% 程式碼但常被忽略。使用 `monkeypatch.setattr(sys, "argv", [...])` + mock 外部依賴即可快速提升覆蓋率（batch_diagnose 71%→99%，blind_spot 74%→99%） `[已自動化於 test: test_batch_diagnose, test_blind_spot_discovery 等]`
2. **mock 外部 API 的安全模式**：`query_prometheus_targets()` 等函式用 `@patch("module.http_get_json")` 而非 `@patch("urllib.request.urlopen")`，mock 粒度在自己的 wrapper 層
3. 🛡️ **Help text 與 COMMAND_MAP 不同步是常見漏洞**：`validate-config` 在 COMMAND_MAP 中但 help text 沒列。`check_cli_coverage.py` lint 工具可捕獲此類不同步 `[已自動化於 hook: cli-coverage-check]`
4. **Triple-quoted string parsing**：解析 Python help text 時，用 `re.findall(r'"""(.*?)"""', content, re.DOTALL)` 限制在三引號字串內，避免匹配到 Python 程式碼中的變數名

### Lint 工具模式

> 🗄️ #5-6 已歸檔至 [archive/lessons-learned.md](archive/lessons-learned.md)（反向驗證已固化為 `cli-coverage-check` hook）

### DX 增強模式

> 🗄️ #7-8 已歸檔至 [archive/lessons-learned.md](archive/lessons-learned.md)（純功能記錄，不是陷阱）

## Playwright E2E 測試（Portal Smoke Tests）

### 架構概覽

5 個 spec 檔案（33 tests）覆蓋 Portal 首頁、Tenant Manager、Group Management、Auth Flow、Batch Operations。全部使用 Chromium，由 `tests/e2e/playwright.config.ts` 統一配置。

| 檔案 | 測試數 | 涵蓋範圍 |
|------|--------|---------|
| `portal-home.spec.ts` | 5 | 首頁載入、工具卡片渲染、Phase 標題、語言切換、RWD |
| `tenant-manager.spec.ts` | 6 | 載入、名稱過濾、metadata 過濾、計數、狀態持久、降級 |
| `group-management.spec.ts` | 7 | 導覽、建立群組、API 隔離、sidebar、成員管理 |
| `auth-flow.spec.ts` | 8 | Dev 模式、OAuth2 redirect、/api/v1/me mock、401 處理、session 過期 |
| `batch-operations.spec.ts` | 7 | 群組選取、批次選單、silent mode、確認對話框、API payload |

### 本地執行

```bash
# 一鍵執行（自動啟動 HTTP server + 跑測試）
make test-e2e

# 手動執行（debug 用）
cd tests/e2e
npm install --include=dev          # 首次安裝
npx playwright install chromium    # 首次安裝瀏覽器
npx playwright test                # 跑全部
npx playwright test --headed       # 有頭模式觀察
npx playwright test --ui           # Playwright UI 互動模式
```

**前置條件：** Node.js ≥ 20（`npx playwright install chromium` 需要網路）。Windows 環境下 `npm install` 務必加 `--include=dev`（npm 11 預設 `omit=dev` 會跳過 devDependencies）。

### 關鍵陷阱與已知解法

#### 1. Server Root 必須是 `docs/`，不是 `docs/interactive/`

Portal 首頁 `index.html` 透過相對路徑 `fetch('../assets/tool-registry.yaml')` 載入工具資料。若 HTTP server root 設為 `docs/interactive/`，`../assets/` 會超出 server root 導致 404。

```
✅ python -m http.server 8080 --directory docs     → ../assets/ → docs/assets/
❌ python -m http.server 8080 --directory docs/interactive → ../assets/ → 404
```

因此 `baseURL` 設為 `http://localhost:8080/interactive/`，讓 server root 留在 `docs/`。

#### 2. `page.goto('/')` vs `page.goto('./')`

Playwright 的 `page.goto('/')` 將 `/` 視為**絕對路徑**，解析為 `http://localhost:8080/`（忽略 baseURL 的 `/interactive/` path）。改用 `page.goto('./')` 是正確做法——相對路徑會正確解析 baseURL：

```
baseURL = http://localhost:8080/interactive/
page.goto('/')   → http://localhost:8080/           ← 錯：看到 "Directory listing for /"
page.goto('./')  → http://localhost:8080/interactive/ ← 對：載入 Portal
```

**所有 spec 檔案統一用 `page.goto('./')`，禁止 `page.goto('/')`。**

#### 3. Windows npm 11 的 `omit=dev` 行為

npm 11 預設 `npm config get omit` 回傳 `dev`，導致 `npm install` 跳過 devDependencies（`@playwright/test` 就在 devDependencies）。解法：

```bash
npm install --include=dev
```

#### 4. 動態卡片 selector

Portal 工具卡片是從 `tool-registry.yaml` 動態產生的 `.cards a.card`，不要用 `#linter-cards` 裡的靜態 `a.card`（那個 div 是 `display:none`）。

#### 5. CI vs Local 差異

| 項目 | CI (GitHub Actions) | Local |
|------|-------------------|-------|
| Server | `npm run serve:portal` 背景啟動 | `playwright.config.ts` 的 `webServer` 自動管理 |
| Browser | `npx playwright install chromium` 每次安裝 | 本地快取，首次安裝即可 |
| Workers | 1（避免 race） | 自動（CPU 核心數） |
| Retries | 1 | 0 |
| `BASE_URL` | env 注入 | 讀 config 預設值 |

### 測試設計原則

所有 spec 採 **defensive assertion** 風格：先檢查 UI 元素是否存在（`count() > 0`），才進行互動。這是因為 Portal 依賴 Mock API 注入資料，非 mock 路徑下只驗「不爆炸」而非「資料正確」。

### v2.7.0 Lesson Learned：Locator Calibration + `test.fixme()` 治理（2026-04-17）

> **觸發**：Phase .a0（Design Token 遷移）新增 3 個 Tier 1 spec 骨架（`config-lint.spec.ts`、`cost-estimator.spec.ts`、`playground.spec.ts`），加上原有 `wizard.spec.ts` 共 8 檔共 32 個 `test.fixme()`。寫骨架時 locator 沒在真實瀏覽器校準過，直接 landing 成 "TODO in real browser"。Phase .e review 發現這是個體系問題：**骨架 ≠ 可跑**。

#### 1. Locator calibration 必須在 headed 模式完成，才能寫進 spec

`page.locator('.preview-header h2')` 這種 selector 看起來合理，但實際上 wizard.jsx 的 heading 可能是 `<h3>`、可能在 `.preview-content` 裡、可能被 shadow DOM 包住。Cowork session 的 headless smoke 只能驗「元素存在某處」，不能驗「這個 selector 唯一指向預期元素」。**規則**：Tier 1 spec 的每個 assertion 必須在 `npx playwright test --ui` 裡點過一次 locator panel 確認「1 match」才能脫 `test.fixme()`。

#### 2. `test.fixme()` 是債務標記，不是「先通過 CI」的工具

✅ **Codified（v2.8.0 PR #57）**：bare `test.fixme()` / `test.skip()` 已由 `tests/e2e/eslint.config.mjs` + pre-commit `playwright-lint` hook 在 commit-time 直接擋下（`eslint-plugin-playwright/no-skipped-test` `{ allowConditional: false, disallowFixme: true }`）。條件式 `test.skip(isChrome, 'reason')` 仍允許——「debt 標記」vs「環境閘門」的區分在自動化層生效。E2E 之外要 skip 改走 Python `pytest.skip`。

**仍是人類判斷的兩件事**（lint 不管）：

- **登記義務**：lint 擋 bare 形式，但 `test.skip(condition, ...)` 與 `test.fixme(false, ...)` 可繞過。這類仍需在 [`frontend-quality-backlog.md`](frontend-quality-backlog.md) 登記（測試名 / spec / 原因 / 預計移除版本），review 階段把關
- **Calibration sprint trigger**：單一 spec `test.fixme()` 跨版本存留超過 1 個 minor 或單檔超過 5 條，排 calibration sprint 走 §5 checklist 清倉

#### 3. Locator 穩定性優先順序（遷移 Token 後仍適用）

Token 遷移改 CSS 屬性不改 HTML 結構，但部分 class 名會變（`.btn-primary` → 用 token 的 `.btn` + `data-variant="primary"`）。**選 locator 的順序**：

1. `page.getByRole('button', { name: 'Submit' })` — 語義化、最穩定
2. `page.getByTestId('wizard-next-btn')` — 顯式 `data-testid`，跟 Token 無關
3. `page.getByText('Generate YAML')` — 文案改動頻率低於 CSS class
4. `page.locator('[data-variant="primary"]')` — Token-native 屬性
5. `page.locator('.btn-primary')` — ⛔ 避免：Token 遷移時會壞

Phase .a0 已將主要互動工具加 `data-testid`（wizard、playground、config-lint 的 next/preview/generate 按鈕）。Tier 2/3 工具 calibrate 時若缺 testid，補上是先決步驟。

#### 4. Cowork 側能做什麼、不能做什麼

| 操作 | Cowork (headless/sandbox) | Dev Container (headed, 真實瀏覽器) |
|------|---------------------------|--------------------------------|
| 寫 spec 骨架（import / test.describe 結構） | ✅ | ✅ |
| 看 `tool-registry.yaml` 推論 selector 候選 | ✅ | ✅ |
| 驗 selector 在 DOM 實際匹配幾個元素 | ⛔ 無 headed 瀏覽器 | ✅ `--ui` 模式 |
| `test.fixme` → 真測試 | ⛔ 只能標記 | ✅ |
| 跑 full e2e suite | 部分（chromium headless） | ✅（headed + slow-mo 可見） |

**結論**：Cowork session 適合「長待辦、寫骨架、標 fixme」；**真正的 locator calibration 必須排 Dev Container session**，且該 session 的 spec/wizard/playground 不排其他工作（calibration 需要持續看 UI panel，不能邊改邊 commit）。

#### 5. `test.fixme()` 清倉 checklist

排程 calibration sprint 時，按下列順序執行單一 spec：

1. `cd tests/e2e && npx playwright test wizard.spec.ts --ui`
2. 逐條 unlock `test.fixme(true, ...)` → 改 `test(...)` 或 `test.fixme(false, ...)`（後者保留將來再關）
3. Locator panel 驗「exactly 1 match」；不是 1 時先補 `data-testid`（jsx 側）再回 spec
4. `npx playwright test wizard.spec.ts` headless 跑過 → 再 `--count=3` 驗穩定性
5. `docs/internal/frontend-quality-backlog.md` 該檔的登記條目逐條劃掉
6. PR 標題：`test(e2e): calibrate <spec> (remove N fixme)`，body 附 `--count=3` 輸出

## v2.8.0 Lessons Learned（2026-04-23, Phase .a）

> **觸發**：PR #49 / PR #50 / PR #51（Phase .a 軌道一 bundle 鏈）各自踩到一類容易重複的 agent pattern error。都是「工具輸出看似完成、但實際隱藏另一個失敗模式」的形狀；本節 codify 三條鐵律避免下次 session 再踩。

### 1. Subprocess-based CLI test **不計 coverage**（PR #49 S#19）

**觸發**：PR #49 新增 `scripts/tools/dx/bump_playbook_versions.py` + `scripts/tools/lint/check_path_metadata_consistency.py`，CLI-level 測試走 `subprocess.run([sys.executable, str(script), ...])` end-to-end，總計 **35 tests**（bump_playbook 19 + check_path_metadata 16）全過。CI 卻 `Python Tests (3.13)` 失敗：coverage 74.94% < 75% fail_under，兩檔 53% / 64%，`main()` 整段 uncovered。

**根因**：`coverage.py` 的 trace hook 不跨 process inherit。subprocess 啟新 interpreter，走的是自己的 `sys.settrace`，不會把結果回報給父行程的 coverage collector。看起來在跑 `main()`，coverage 只見 import-time 程式碼。

**正解**：
1. **新工具的 CLI surface test 一律 in-process** — `monkeypatch.setattr(module, 'sys', ...)` 設 `sys.argv`，`monkeypatch.chdir(repo_root)`，直接呼 `module.main()`，`capsys` 吃 stdout/stderr，catch `SystemExit`（argparse exit path）。
2. **End-to-end subprocess test 可以保留但不作 coverage 主力** — 只跑 1-2 個「確實從 shell 呼得到」的 smoke。
3. **若必須 subprocess 驗 PATH / env 行為**：配 `COVERAGE_PROCESS_START` env var + `sitecustomize.py` 掛 sub-process coverage；本 repo 目前沒此需求。

**Range 指標**：PR #49 改法後兩檔 coverage 59.2% → 98.2%（`bump_playbook_versions` 100% / `check_path_metadata` 96.7%）。詳見 `v2.8.0-planning-archive.md §S#19`。

### 2. 本地工具輸出**被截斷 / 被 encoding 吃掉**後必須二次驗證（PR #49 anchor drift + PR #50 journey `—`）

**觸發**：
- **PR #49**：A-11 寫 `#已知陷阱` anchor 指 windows-mcp-playbook，實際章節名 `#已知陷阱速查`。裸跑 `python3 scripts/tools/lint/check_doc_links.py --ci` 的 exit code **同時被兩個來源污染**：(a) broken anchor（真 fail）、(b) 工具本身 final `✓ All links ...` print 在 cp950 console 觸發 `UnicodeEncodeError`（即使無 broken anchor 也會 exit 1）。fix 完 anchor 後仍 exit 1，差點下錯結論「工具壞了」；走 `pre-commit run doc-links-check`（config 自帶 `-X utf8`）才分清兩個來源。
- **PR #50**：design-system-guide §3.4 journey 表 `dark mode` 欄我填 `—`，實際 canonical 有值（`#fcd34d` / `#c4b5fd`）。`grep "journey-" | head -10` 截斷了 dark-mode rows，我憑記憶 filed 成「無值」。

**共同根因**：本地工具輸出被**截 / 爆 encoding / 分頁**後沒補驗一次就下結論。

**正解**：
1. **任何 Python lint / generator 輸出**一律 `PYTHONIOENCODING=utf-8 python3 -X utf8 <script>` 確保 stdout/stderr 走 UTF-8；或走 `pre-commit run <hook>`（hook 在 `.pre-commit-config.yaml` 已統一帶 `-X utf8`）。
2. **看到 `head -N` / `--head N` / `head_limit`**：每次截斷後必須補一次精確 grep 或 `| head -50` 確認底部內容。寧可輸出多一點也不要靠記憶。
3. **凡是 exit code 與 stdout 不一致**（e.g. exit 1 但 stdout 印 "0 errors"），第一反應是**工具本身 encoding/console bug**，不要下結論 "工具壞了"；改走 hook 或 UTF-8 env 再跑一次。

### 3. `--no-verify` 使用規範 — 只跳 FUSE 已知卡死，不跳 commit-msg（PR #50 自己踩到）

**觸發**：PR #50 commit 用 `git commit --no-verify` 繞 FUSE Trap #57 `head-blob-hygiene` 17+ 分鐘 0 output。`--no-verify` 同時 bypass `commit-msg` hook（PR #44 C2 裝的 commitlint 本地 validator），commit header 寫了 104 chars，CI 才擋下，需 force-push-with-lease 修。

**正解**：
1. **首選**：`SKIP=<hook-name> git commit ...` 精準跳。例：`SKIP=head-blob-hygiene git commit -F msg.txt`
2. **次選**：`pre-commit run --hook-stage manual` 事前跑所有 manual hooks 手動驗證，再 `SKIP=...` 跳該單一 hook
3. **鐵則 — 禁用 `git commit --no-verify`**，除非能明確寫出「這次真的同時要跳過哪幾個 hook」。本 repo 目前唯一合法場景是 **FUSE Trap #57**（head-blob-hygiene 卡死），請改用 `SKIP=head-blob-hygiene`
4. commit message 必須記錄：(a) 哪個 hook 被跳過、(b) 原因（引 Trap #N）、(c) 手動補跑了哪些 hook 確認通過
5. **長期 enforcement** 追蹤於 [Issue #53](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/53)（narrow `--no-verify` bypass）

> **Extension（PR #51 self-review 新發現，PR #55 enforcement 落地）**：本地 `commit-msg` hook（PR #44 C2 安裝的 `scripts/hooks/commit-msg` → `pr_preflight.py --check-commit-msg`）v2.8.0 Issue #53 前**只驗 header**（type / scope / header length），**不驗 body / footer**。CI commitlint 多驗 `footer-max-line-length ≤ 100`、`footer-leading-blank` 等 body 規則；long pytest path / long file list 塞在 commit message 末段會被 commitlint 當 footer → 觸發 `footer-max-line-length`。PR #51 self-review commit 踩到：local 過、CI 擋、force-push-with-lease 修。
>
> **PR #55 Issue #53 enforcement 落地**：
>
> 1. `pr_preflight.py --check-commit-msg` 擴 `validate_commit_msg_body()`：每個 post-header 非註解行 > 100 chars → ERROR；缺 blank-line-after-header → WARN。保守策略 — 比 CI 還嚴（CI body-max-line-length 放寬到 200），但可靠防 PR #51 類 class of error。`test_preflight_msg_validator.py` 從 20 → 29 tests
> 2. `make commit-bypass-hh ARGS="-F _msg.txt" [EXTRA_SKIP=hook1,hook2]`：codified narrow bypass — `SKIP=head-blob-hygiene git commit <ARGS>`，commit-msg hook 仍跑，防 `--no-verify` 的 all-or-nothing 災
>
> **規則更新**：從本 PR 起，FUSE Trap #57 繞道改為 `make commit-bypass-hh`；`git commit --no-verify` 只在 commit-bypass-hh 本身失效（如 commit-msg 自己 bug）時才用，且 commit message 需寫明 bypass 原因

### 4. Subprocess hang 要設 SLA — 沒進度的等待是浪費（PR #164 / S#74）

**觸發**：PR #164 commit 時 `pre-commit` hook `head-blob-hygiene` 卡 14+ 分鐘 0 output。Trap #57 過去的記錄都是 FUSE 側 stale temp，但這次跑在 NTFS Cowork VM（無 FUSE），不對應任何已知 mitigation 路徑。Monitor 持續輪詢 → 持續沒事件 → 很容易停在「再等一下，可能快好了」的狀態。**關鍵介入**：user 一句「你的觀察應該要有合理期限，如果太久都沒跑過要審視合理性」逼出 escalation，bisect 30 分鐘內找到 Popen pipe deadlock，PR #164 順手把 fix 包進去 ship。

**根因**：agent 在「等待」mode 下傾向繼續等而不是切換成 investigate。本身沒有「這個操作的合理 SLA 是多少」的 prior，超出後又沒有 escalation policy。

**正解**：
1. **任何 subprocess / hook 在等待時都要有 SLA prior**：pre-commit hook 通常秒級，整個流程 < 1 min；CI step < 10 min；`make` target 視內容，不過 ad-hoc 字串建議 `< 5 min`。沒有 prior 時，第一次跑就記下實際時間做為下次 baseline。
2. **超出 SLA 即 escalate to investigation**，三步固定：
   (a) `ps -ef | grep <suspect>` 看哪個子程序還活著
   (b) 把卡住的程序 isolate 出來單獨重現（例：把 hook 的 entry script 直接跑），確認是 deterministic hang 還是 transient 慢
   (c) 該程序內部 instrument（加 `print(..., flush=True)`、用 `--verbose`、或讀 source 找 deadlock 模式）
3. **拒絕「再等一下」誘惑** — wait loop 內 elapsed > SLA 時就要切到 (2)；繼續等只是把時間白燒進 prompt cache。
4. **`Monitor` / `run_in_background` 工具預設要設 timeout**，不要用 default 5min 不思考；有 prior 就設 prior，沒 prior 就想 1 分鐘有沒有理由要這麼久。

**衍生規則 — verify-reference applies to hook scripts too**：S#73（`vibe-dev-rules` 的 self-review check #6）原本只要求 verify 自己寫的 code 引用的 API；S#74 extension：**讀別人寫的 hook / lint script 假設「它跑得通」前，最好先讀關鍵 path（subprocess Popen / pipe handling / file I/O）跟自己 verify 一遍**。本次 Popen 死鎖是 PR #164 之前就在的 latent bug，但所有 session 都假設「pre-commit hook = 跑得通」沒檢查；這個 prior 是錯的，要 calibrate 下來。

### 5. Dev Container mount scope（Trap #62 連帶工作流）

Dev Container 只 bind-mount 主 worktree（`C:\Users\vencs\vibe-k8s-lab\`），claude worktree 的 Edit **不會進 container**。詳 `windows-mcp-playbook.md` Trap #62。**Go test / Playwright E2E** 在 claude worktree 做 Edit 後，一律走：

1. `cp <claude-worktree-path> <main-worktree-path>` 同步單檔
2. 在主 worktree 跑 `make dc-go-test` / `bash scripts/ops/dx-run.sh ...`
3. 跑完在主 worktree `git checkout -- <path>` revert，claude worktree 保留為 SoT

**不要**用 `git commit + push + fetch` 同步 — 會污染 commit history。**不要**改 `dx-run.sh` 的 `-w` 參數除非你願意同步調整 container bind-mount。

## v2.8.0 Lessons Learned — Race-flake battles（2026-04-26, Phase .b）

> **觸發**：Phase .b session #32（PR #75）+ session #35（PR #79）兩次踩同一個 `withIsolatedMetrics` + async-callback goroutine-leak race，每次都燒 1-3 個 fix-up commits 才收斂 CI。Lessons 一直困在 planning archive，下個 session 不一定看得到。本節 codify 三條規範升 cross-version SSOT。
>
> **Authority**：本節為「production code 含 async callback（`time.AfterFunc` / goroutine spawn）+ 測試用 `withIsolatedMetrics` swap global metric 實例」class of test 的 hard rule。違反任一條的 PR 預期會在 CI 隨機 flake。

### 1. `withIsolatedMetrics` + async-callback isolation 不完整 — 用 lockstep + `>=` invariant，不要 exact-equality

**現象**：`withIsolatedMetrics` swaps the global metric instance to `fresh` for the test's lifetime. But production code with async callbacks (e.g. `fireDebounced` spawned by `time.AfterFunc`) may complete its work **after**:

1. The previous test's `defer m.Close()` returned (Close does **not** wait for in-flight callbacks — see [`config_debounce.go::Close` docstring](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/app/config_debounce.go))
2. The next test's `withIsolatedMetrics` already swapped to its own `fresh`

The leaked callback's late `getConfigMetrics()` returns the **NEW** test's `fresh` (the global is now swapped), inflating the metric count.

**Insufficient fix (S#32, PR #75)**: snapshot baseline counts at test start, assert deltas. **Doesn't help** if the leak lands BETWEEN snapshot and final read (5-50ms `diffAndReload` window). PR #79 reproduced the same flake despite the baseline-snapshot fix.

**Insufficient fix (S#35, PR #79 commit `0abc2ff`)**: claimed `deltaReload == deltaBatch` "lockstep" because both are observed in `fireDebounced`. **WRONG** — only `ObserveDebounceBatch` + `atomic.AddUint64(&m.debounceFired)` are atomic (steps 1+2 under `m.debounceMu`). `ObserveReloadDuration` happens AFTER `diffAndReload` (step 4), giving reload a much wider leak window than batch. PR #90 CI run #24946980383 observed `deltaReload=2, deltaBatch=1` — invalidated the lockstep claim.

```text
fireDebounced timeline (per config_debounce.go):
  1. ObserveDebounceBatch(len(reasons))    ← batch leaks here
  2. atomic.AddUint64(&m.debounceFired)    ← per-instance fire counter
     ----- diffAndReload runs (~ms-100ms) -----
  4. ObserveReloadDuration(elapsed)        ← reload leaks LATE
```

**Durable fix (S#37, PR #90)**: assert what's actually atomic + a `>=` lower bound on what isn't. Three invariants:

```go
// Capture baseline before triggering our own work.
baseFire := m.DebounceFiredCount()
baseReload := histogramSampleCount(t, fresh.reloadDuration)
baseBatchCount := histogramSampleCount(t, fresh.debounceBatch)

// ... trigger our N calls, wait for quiescence ...

deltaFire := m.DebounceFiredCount() - baseFire
deltaReload := histogramSampleCount(t, fresh.reloadDuration) - baseReload
deltaBatchCount := histogramSampleCount(t, fresh.debounceBatch) - baseBatchCount

// (1) batch + fire ARE atomic per fireDebounced steps 1+2 → lockstep:
if deltaBatchCount != deltaFire {
    t.Errorf("batch-fire lockstep violated: deltaBatch=%d != deltaFire=%d", deltaBatchCount, deltaFire)
}
// (2) reload is observed AFTER diffAndReload → only `>=` invariant holds:
if deltaReload < deltaFire {
    t.Errorf("at-least invariant violated: deltaReload=%d < deltaFire=%d", deltaReload, deltaFire)
}
// (3) Exact equality only when no leak detected.
if deltaReload == deltaFire && deltaFire == 1 {
    // assert sum/count exactly here
}
```

**Reference implementations**: see `components/threshold-exporter/app/config_debounce_test.go::TestFireDebounced_EmitsBatchAndDuration` for the canonical example.

> **Generality note**: the example uses `m.DebounceFiredCount()` which is exporter-specific. The pattern generalizes to **any** Go test that:
> (a) swaps a global metric instance via a `withIsolated*` helper, AND
> (b) exercises production code that spawns goroutines / `time.AfterFunc` callbacks whose `Close()` doesn't wait.
>
> Identify which observations sit **inside** vs **after** the critical section in the production callback:
> - **Inside critical section** (atomic with the per-instance counter): assert `delta == fire`
> - **After critical section** (separated by I/O / heavy compute): assert `delta >= fire`
> Substitute `DebounceFiredCount()` with whatever per-instance counter your subject exposes (e.g. tenant-api could use a `RequestCount()` accessor).

### 2. 時間敏感 test 用 quiescence detection，**不要**「sleep + assert exactly N」

**Anti-pattern** (PR #75 v1):

```go
for i := 0; i < N; i++ { trigger() }
time.Sleep(window + buffer)         // <-- timing assumption!
assert.Equal(t, 1, fireCount)       // <-- "exactly 1" timing claim
```

`time.Sleep(buffer)` may overshoot under `-race` instrumentation, splitting the batch across two debounce windows. "Exactly 1 fire" tests **timing**, not the actual contract ("one observation per fire").

**Pattern**:

```go
for i := 0; i < N; i++ { trigger() }

// Wait for stability: no new fires for `stableWindow` consecutive ms.
stable := uint64(0)
stableSince := time.Time{}
deadline := time.Now().Add(2 * time.Second)
for time.Now().Before(deadline) {
    now := m.DebounceFiredCount()
    if now != stable {
        stable = now
        stableSince = time.Now()
    } else if !stableSince.IsZero() && time.Since(stableSince) > 150*time.Millisecond {
        break  // stable
    }
    time.Sleep(10 * time.Millisecond)
}
fireCount := m.DebounceFiredCount()
// Assert per-fire invariants regardless of fireCount value.
```

This decouples the test from window-size choice and CI scheduling jitter.

#### Worked example: `TestSlowWriteTornStateStress_FinalConvergence`（PR #159, issue #157）

The B-7 slow-write stress test originally asserted **two** wall-clock claims that lesson §2 prohibits:

1. `for i := 0..N { trigger; t.Sleep(jitter); assert fireCount == 0 }` — "no fire DURING the burst"
2. `t.Sleep(2 * window); assert fireCount == 1` — "exactly 1 fire AFTER settle"

Both pass under healthy CI but flake when scheduler jitter overshoots a sleep, splitting the 50-write burst into 2 fired windows. PR #151 + #155 each took the flake. After two adjacent occurrences, opened issue #157 and codified the rewrite per this lesson.

**Rewrite shape**:

```go
// Drive the burst — DO NOT sample fireCount mid-burst.
for i := 0; i < numFiles; i++ {
    writeFile(...)
    m.triggerDebouncedReload(ReloadReasonSource)
    time.Sleep(jitter)
}

// Quiescence — fireCount stable for stableWindow consecutive ms.
if !waitForQuiescence(t, settleTimeout, stableWindow, m.DebounceFiredCount) {
    t.Fatalf("counter never stabilized — debounce may be broken")
}
fireCount := m.DebounceFiredCount()
t.Logf("debounce fires: %d (informational, not asserted)", fireCount)

// Per-fire invariants — INDEPENDENT of fireCount value:
// (a) every trigger coalesced into SOME fire
assert h.GetSampleSum() == numFiles
// (b) every mutated tenant advanced
assert mergedHash[tid] != baseline[tid] for all tid
// (c) fire count not absurd (catches genuinely-broken debounce)
assert h.GetSampleCount() <= 2  // CI-jitter envelope
```

**Why `_count <= 2` not `_count == 1`**: a 50-write burst with 5-25ms gaps under a 100ms window legitimately splits into 1 OR 2 fired windows depending on scheduler jitter. Both are contract-compliant. `_count <= 2` is the **CI-jitter envelope** — outside this means debounce is genuinely broken (e.g. window not coalescing). The test FAILS bench injection of "skip every-other trigger" via `_sum=25 != 50` (verified during PR #159 implementation).

**Reusable helper** (`config_slow_write_stress_test.go`):

```go
func waitForQuiescence(t *testing.T, deadline, stableWindow time.Duration, counterFn func() uint64) bool
```

Generalizes to any test polling a monotonic counter for "no new events for K ms" semantics.

### 3. `testutil.CollectAndCount` 對 plain Histogram **回 family count, 不是 sample count**

**Footgun**: `prometheus.testutil.CollectAndCount(h)` for a plain `prometheus.Histogram` returns **1** after registration, regardless of how many `Observe()` calls happened. It returns the number of metric families, not samples.

**Wrong** (PR #75 first attempt — caught in self-review):

```go
if got := testutil.CollectAndCount(fresh.reloadDuration); got != 0 {
    t.Errorf("expected no observations, got %d", got)  // always fails!
}
```

**Right** — gather and read `SampleCount` directly:

```go
func histogramSampleCount(t *testing.T, h prometheus.Histogram) uint64 {
    t.Helper()
    reg := prometheus.NewRegistry()
    if err := reg.Register(h); err != nil {
        t.Fatalf("register: %v", err)
    }
    families, err := reg.Gather()
    if err != nil {
        t.Fatalf("gather: %v", err)
    }
    for _, fam := range families {
        for _, metric := range fam.Metric {
            return metric.Histogram.GetSampleCount()
        }
    }
    return 0
}
```

This helper exists at `components/threshold-exporter/app/config_metrics_test.go::histogramSampleCount` for reference.

**Note**: `HistogramVec` (with labels) has different semantics — `CollectAndCount` returns the active series count, which is meaningful. The footgun is specifically plain `Histogram`.

### PR review checklist item

When reviewing a PR that adds a Go test using `withIsolatedMetrics` + production code with async callbacks (`time.AfterFunc`, goroutine spawn), verify:

- [ ] Snapshot baseline counts at test start (don't rely on `fresh` starting at 0)
- [ ] **Identify which metric observations are `inside` vs `after` the production critical section** — assert `delta == fire` only for those inside; use `delta >= fire` for those after (e.g. observed post-I/O)
- [ ] **Do NOT assume two metrics in the same callback function are atomic** — check whether they bracket I/O / heavy compute (which makes their leak windows different sizes)
- [ ] Use quiescence detection for "wait for fire" (poll + stable-window), not `time.Sleep(window+buffer)` + exact-count assert
- [ ] If using `testutil.CollectAndCount` on plain `Histogram`, replace with `histogramSampleCount` helper

Apply this checklist as part of any PR that adds new Go tests touching `config_metrics.go` / `config_debounce.go` paths.

### Cross-refs

- `docs/internal/v2.8.0-planning-archive.md` §S#32 (PR #75 — initial fix; partial)
- `docs/internal/v2.8.0-planning-archive.md` §S#35 (PR #79 — durable lockstep+>= upgrade)
- [Issue #81](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/81) — codification tracking (this section is its deliverable)

## v2.8.0 Lessons Learned — Validation ordering + typed errors（2026-04-30, Phase .b, PR #147 / issue #127）

> **觸發**：Phase B Track B follow-up PR #147 重新打了同一個 class 的 production gap — duplicate-tenant misconfig 被「flat-mode loadDir 先 silently last-wins-merge → 隨後的 hierarchical scan WARN-and-ignore」鏈路吞掉。修法不只是「把 WARN 升 error」這麼簡單；牽涉到三條互相依賴的紀律。本節 codify 這三條，避免同類 reorder 問題在 v2.9.0 重複。
>
> **Authority**：本節為「validator 跑在已 commit 的 state 之後 + state 已 atomic-swap」class 的 design rule。違反會出現「reject 完成但 partial state 已 leak」這類 invariant 破壞，customer hard-to-reproduce。

### 1. 驗證跑在 commit 之前，不要跑之後再吞 error

**現象（PR #147 修的 v2.8.0-pre 行為）**：

```go
// config.go Load() — pre-v2.8.x order
m.config = &cfg                            // (1) flat-mode commit (silently last-wins-merged duplicate)
m.loaded = true
// ...
if err := m.populateHierarchyState(); err != nil {  // (2) detects duplicate AFTER commit
    log.Printf("WARN: ...")                          //     → swallowed as WARN, returns nil
}
return nil
```

Customer deploy 看到 `Load()` returns nil → "deploy succeeded" — 但 served config 是 map iteration 順序決定的「last-wins」，極易在 production 漏察。

**Durable fix**：把 validator 的 call **移到 commit 之前**，reject 時 caller 看到 `Load returned error` + 完全沒有 partial state 洩漏：

```go
// config.go Load() — v2.8.x order
if hierErr := m.populateHierarchyState(); hierErr != nil {
    var dupErr *DuplicateTenantError
    if errors.As(hierErr, &dupErr) {
        return fmt.Errorf("config rejected: %w", hierErr)  // (1) reject BEFORE commit
    }
    log.Printf("WARN: ...")  // generic scan errors keep prior policy
}

m.mu.Lock()                  // (2) commit only if validation passed
m.config = &cfg
m.loaded = true
// ...
```

**State invariant** under the new order：cold start (`Load`) reject → `m.config = nil`, `m.loaded = false`；hot reload (`fullDirLoad`) reject → prior known-good state preserved，跑著的 service 不會被半途切到 broken state。

**反例（不能用的「先 commit 再 unwind」設計）**：commit 了 `m.config`，validator 失敗，再 `m.config = oldConfig` rollback。問題：commit 與 rollback 之間若有 reader 讀到新 config，就觀察到了「應該被 reject 的中間狀態」。Atomic-swap 的點是同一個鎖內，validator 必須跑在那個 swap 之前。

### 2. 用 typed error 區分「misconfig（hard fail）」vs「flaky（log + continue）」

**Pre-v2.8.x**：`scanDirHierarchical` 對所有失敗都 return generic `fmt.Errorf`，caller 沒有訊息可以區分「customer 寫錯設定（fail hard 強迫 fix）」 vs「個別檔案 permission / malformed（log + 跳過繼續跑）」。結果 caller 只能一律 `log.Printf("WARN: ...")` — 兩個截然不同的 class 被同一個 log line 吞掉。

**Durable fix**：misconfig 用 typed error，scan 機制錯誤保留 generic error：

```go
// config_hierarchy.go
type DuplicateTenantError struct {
    TenantID string
    PathA    string
    PathB    string
}

func (e *DuplicateTenantError) Error() string {
    return fmt.Sprintf("duplicate tenant ID %q: defined in both %s and %s",
        e.TenantID, e.PathA, e.PathB)
}
```

Caller 用 `errors.As` 區分：

```go
if hierErr := m.populateHierarchyState(); hierErr != nil {
    var dupErr *DuplicateTenantError
    if errors.As(hierErr, &dupErr) {
        return fmt.Errorf("config rejected: %w", hierErr)  // misconfig: fail hard
    }
    log.Printf("WARN: ...")  // flaky: log + continue
}
```

**設計要點**：
- Typed error 必須**包含定位資訊**（`TenantID` / `PathA` / `PathB`），讓 operator 不需要 unwrap 就能 grep / `git rm`。`Error()` 文字格式跟 generic `fmt.Errorf` 保持 byte-identical，向後相容做 string-match 的舊 test（例：`cmd/da-guard/main_test.go::TestRun_DuplicateTenantID_ExitsTwo`）
- 不要 over-type：只有 misconfig class 開 typed error。malformed file / permission / IO failure 仍走 generic `fmt.Errorf` — 過度 typed 會讓 caller 寫一堆無意義的 `errors.As` switch
- 用「opt-in 機制」做語意分流：hierarchical mode 是 opt-in（沒有 `_defaults.yaml` 就不會啟用），所以 hierarchical scan 的 generic error 不該擊倒整個 flat-only deploy → 對應 `log + continue`；duplicate tenant 是真實 misconfig → 對應 `fail hard`

### 3. Test 規範：「鎖死當前 gap」test 過渡到「鎖死新 contract」test 必須同時存在於同一 PR

**Pre-v2.8.x test (`TestMixedMode_DuplicateAcrossModes_DetectedButNotPropagated`)**：刻意鎖死 v2.8.0-pre 的 gap 行為（assert `Load() == nil` + WARN log 存在）。test name 直接寫 `DetectedButNotPropagated` — future hardening PR 必須改寫這個 test 才能 land，無法 silently 留 gap。

**Durable fix（PR #147）**：

1. **重寫**（不是新增）原 test，改名為 `_RejectedAtLoad`，鎖 4 個新合約：
   - `Load()` 返回 hard error
   - `errors.As(err, &DuplicateTenantError{})` 解出 typed error
   - `dupErr.PathA` / `PathB` 兩條路徑 populated 且 distinct
   - `m.config == nil` / `m.loaded == false`（state 不洩漏 invariant）
2. **新增**對稱 test (`_RejectedAtFullDirLoad`) 鎖 hot-reload state preservation：load clean → introduce duplicate → fullDirLoad 拒絕 → `m.config` / `m.lastHash` 仍指向 prior known-good

**為什麼不能只新增 test 不重寫舊 test**：舊 test 名字 (`_DetectedButNotPropagated`) 與新合約矛盾，留著 → future reader 困惑「到底誰才是當前合約」。**重寫的 PR diff 本身就是 contract migration 的 audit trail**。

### Cross-refs

- PR [#147](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/147) (closes [#127](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/127)) — landed lessons; commit `2458466` on main
- `components/threshold-exporter/app/config_hierarchy.go::DuplicateTenantError` — typed error 範例
- `components/threshold-exporter/app/config.go::Load`, `::fullDirLoad` — reorder-before-commit pattern 範例
- `components/threshold-exporter/app/config_mixed_mode_test.go::TestMixedMode_DuplicateAcrossModes_RejectedAtLoad` + `_RejectedAtFullDirLoad` — test 重寫範例

## v2.2.0 Lessons Learned（2026-03-18）

1. **`apk del` 後必須驗證移除成功**：`|| true` 吃掉錯誤導致 CVE 殘留。Dockerfile 加 `if apk info -e <pkg>; then exit 1; fi` 做 build-time 斷言
2. **git-sync `--link=current` 建立 symlink**：下游 `--config-dir` 路徑必須含 `/current/`。加 initContainer `--one-time` 防 exporter 啟動時讀到空目錄
3. **不寫虛構 benchmark**：沒有實測數據就用 O() 定性分析 + 引導客戶用工具自行驗證。虛構數字被質疑會失去信任
4. **Dockerfile 註解必須與 base image 版本同步**：改 pin 版本時連註解一起改，否則 review 時製造混淆

## v2.6.x Lessons Learned — Go 並發測試 flake（2026-04-11）

> **觸發**：CI 報 `FAIL github.com/vencil/tenant-api/internal/async` 但仍印出 `coverage: 87.8%`。症狀是 `t.Errorf` 而非 `t.Fatal`，package 跑完但標記失敗。根因三條全是測試對並發行為的假設過緊，production code 無 bug。

### 初始狀態斷言的 race 陷阱

1. **零成本 `TaskFunc` 不會停在 pending/running**：Manager.Submit 在 `mu.Lock` 內寫 workCh，釋鎖後 worker 可以在數 μs 內完成 `setStatus(running) → fn → setCompleted`。測試在 Submit 之後立即呼叫 `Get()`，看到的 status 可能是 pending / running / completed 任何一個。寫 `if status != pending && status != running { t.Errorf(...) }` 必然會 flake，`-race` 下頻率放大。
2. **解法 A — barrier 釘住 worker**：在 `fn` 內 `select { case <-barrier: case <-ctx.Done(): }`。defer 順序必須 LIFO：`defer m.Close()` 先寫、`defer close(barrier)` 後寫；teardown 時 close(barrier) 先跑，worker 才能 drain 結束，接著 m.Close() 的 `wg.Wait()` 才不會卡住。`ctx.Done` 是 panic fallback，避免測試異常退出時 worker 永遠卡在 barrier。
3. **解法 B — 刪掉初始斷言**：若測試的主旨是「最終狀態」而非「初始狀態」，直接刪掉初始檢查，靠 polling loop 驗證 converge 行為即可（e.g. `TestWorkerCompletion`）。redundant 的斷言 = 多一個 flake source。

### 時間戳斷言的 happen-before 陷阱

4. **`before/after` bounds 必須夾到 snapshot 之外**：`after := time.Now()` 要測在 `Get()` **之後**，不是 `Submit()` 之後。證明：worker 最後一次寫 `UpdatedAt` 發生在鎖下 → `Get` 在鎖下取 snapshot → `Get` 返回 → 測量 `after`。三段 happen-before 鏈保證 `UpdatedAt ≤ after`。把 `after` 放在 Submit 後 Get 前，會被 Go `sync.RWMutex` 的 writer-prefers 排隊機制打爆：worker 已經 pending 在 Lock 上 → 測試的 `after` 量測 → 測試 RLock 被排在 writer 之後 → snapshot 讀到的 `UpdatedAt` 已經晚於 `after`。
5. **RWMutex 的 writer starvation prevention 是 Go-runtime-level 保證**：Go 1.20+ 後 `RLock` 在已有 writer 等待時主動 yield，不是 FIFO 也不是 reader-prefers。這讓「測試先量 after 再 Get」的直覺式寫法在 race build 下特別脆。

### `-race` 放大機制與驗證強度

6. **`-race` 不是「讓 race 更容易看到」而是「讓 scheduling 更不對稱」**：race instrumentation 在每次記憶體存取插入 happens-before 檢查，對不同 goroutine 的減速比例不同。原本 1/1000 頻率的 logical race，race build 下可能變成 1/10。本機跑綠不代表 CI 跑綠。
7. **修並發 flake 的驗證門檻 ≥ `-count=20`**：`go test ./... -race -count=1` 通過只算 smoke。修 flake 後必須 `-count=20` 甚至 `-count=50` 才能排除 survivorship bias。CI 的 `-count=1` 是成本考量，不是正確性保證。
8. **`FAIL` + coverage 同時出現 = `t.Errorf` 非 `t.Fatal`**：package 跑完所以有 coverage，但至少一個 assertion 失敗。這是識別「邏輯斷言錯誤」vs「建置錯誤」vs「panic/timeout」的快速訊號——看到這個組合直接往 flaky assertion 方向找，不要先懷疑 compile error 或 infra。

### 適用範圍

本節的三個 pattern 不限於 `tenant-api/internal/async`。任何具備 **worker goroutine + shared mutex + 可變 timestamp/status** 的 Go 套件（如 `ws/hub`、`gitops/reconcile`）都該用同樣的三條 checklist 掃過：(a) 初始狀態斷言是否寫死 pending/running、(b) time bounds 是否跨過 Get/snapshot、(c) 是否只跑 `-count=1`。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["GitHub Release — 操作手冊 (Playbook)"](github-release-playbook.md) | ⭐⭐ |
| ["測試注意事項 — 排錯手冊 (Testing Playbook)"](testing-playbook.md) | ⭐⭐ |
| ["Windows-MCP — Dev Container 操作手冊 (Playbook)"](windows-mcp-playbook.md) | ⭐⭐ |
