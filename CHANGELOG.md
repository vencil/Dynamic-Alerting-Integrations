# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [v0.8.0] - Testing Coverage, SRE Runbook & Baseline Discovery (2026-02-27)

本版本為 Phase 7 測試覆蓋強化 + B6/B7 交付

### 🧪 Testing Coverage
* **`run_load.sh --type composite`**: 複合負載 — connections + cpu 同時啟動，驗證 `MariaDBSystemBottleneck` 複合警報。
* **`tests/scenario-e.sh`**: Multi-Tenant 隔離測試 — 修改 tenant A 不影響 tenant B。支援 `--with-load`。
* **`tests/scenario-f.sh`**: HA 故障切換測試 — Kill Pod → alert 持續 → 恢復 → 閾值不翻倍 (max by)。

### 📋 SRE Runbook & Discovery Tooling
* **`docs/shadow-monitoring-sop.md`**: Shadow Monitoring SRE SOP — 啟動/巡檢/異常處理/收斂判定/退出完整 runbook。
* **`scripts/tools/baseline_discovery.py`**: Baseline Discovery — 觀測 p50~p99 統計，建議 warning (p95×1.2) / critical (p99×1.5) 閾值。

### 🎭 Demo 強化
* **`make demo`**: Step 5d 新增 `baseline_discovery.py` 快速觀測（15s 取樣 + 閾值建議），展示完整工具鏈。
* **`make demo-full`**: Step 6 改用 `--type composite` 一次啟動 connections + stress-ng（取代原本分開注入），步驟從 6a–6j 精簡為 6a–6i。

### 📖 文件與版本
* **Migration Guide**: 開頭加入「遷移安全保證」陳述；Phase C 的「99.9%」修正為準確工程描述。
* **README.md / README.en.md**: 文件導覽表新增 Shadow Monitoring SOP；工具表新增 `baseline_discovery.py`；Makefile 目標與專案結構補齊 Scenario E/F、composite、baseline。
* **全域版本一致性**: Helm Chart 0.8.0、CI image tag v0.8.0、所有文件統一 v0.8.0。
* **清理**: 刪除根目錄殘留的 `test-legacy-rules.yaml`（測試輸入已收斂至 `tests/legacy-dummy.yml`）。

---

## [v0.7.0] - Live Observability & Load Injection (Phase 6) (2026-02-27)

本版本為 Phase 6 真實負載注入與動態展演，讓系統價值「肉眼可見」，徹底解決「改設定觸發警報像作弊」的痛點。

### 🔥 Load Injection Toolkit
* **`scripts/run_load.sh`**: 統一負載注入入口腳本，支援三個展演劇本：
  * **Connection Storm** (`--type connections`): 使用 PyMySQL 持有 95 個 idle 連線，觸發 `MariaDBHighConnections`（保留 exporter 連線槽位，確保 Prometheus 能持續回報指標）。
  * **CPU & Slow Query Burn** (`--type cpu`): 使用 `sysbench oltp_read_write` 執行高密度 OLTP 查詢（16 threads, 300s），觸發 `MariaDBHighSlowQueries` 與 `MariaDBSystemBottleneck` 複合警報。
  * **Container Weakest Link** (`--type stress-ng`): Alpine CPU burn Pod（CPU limit: 100m），故意造成 CPU throttling，驗證 `PodContainerHighCPU` 弱環節偵測精準度（實測 97.3%）。
* **`--dry-run` 模式**: 預覽 K8s manifest 而不實際 apply，方便審查與教學。
* **`--cleanup` 模式**: 一鍵清除所有負載注入資源，trap 確保異常退出也能清理。

### 🏗️ Testing 模組化重構
* **`scripts/_lib.sh` 擴充**: 新增 `setup_port_forwards`, `cleanup_port_forwards`, `prom_query_value`, `get_alert_status`, `wait_for_alert`, `get_exporter_metric`, `wait_exporter`, `require_services` 共 8 個共用函式，取代 4 個 scenario + demo.sh 中重複的 inline Python + port-forward 管理程式碼。
* **Scenario A/B/C/D 重構**: 移除各腳本中重複的 alert polling、port-forward 建立、exporter metric 查詢邏輯，統一透過 `_lib.sh` 提供。
* **清除 7 個 debug 暫存腳本**: 刪除 `_check_alerts.sh`, `_check_alerts2.sh`, `_check_load.sh`, `_final_check.sh`, `_retest_load.sh`, `_test_conn.sh`, `_test_conn95.sh` — 已被正式工具取代。
* **淨減 ~580 行**: 正式腳本總行數從 ~2,200 降至 ~1,625 行（含 _lib.sh 從 94 行擴充至 260 行）。

### 🎭 Demo & Testing 整合
* **`make demo-full`**: 完整 demo 含 Live Load Injection — stress-ng + connection storm → 等待 alerts FIRING → 清除 → alerts 自動消失，展示「負載→觸發→清除→恢復」完整循環。
* **`make demo`**: 保持原始快速模式（`--skip-load`），僅展示工具鏈。
* **`make load-demo`**: 單獨啟動 stress-ng + connections 壓測，手動觀察 alerts。
* **Scenario A (`--with-load`)**: 保持原始閾值(70)，真實 95 connections > 70 → alert fires → 清除 → resolves。不再需要人為壓低閾值。
* **Scenario B (`--with-load`)**: 保持原始閾值(70)，stress-ng 97.3% > 70% → alert fires → 清除 → resolves。
* 所有 load 路徑加入 `trap cleanup EXIT`，確保 Ctrl+C / 錯誤退出時自動清除 load-generator 資源。

### 📋 SRE Runbook & Discovery Tooling
* **`docs/shadow-monitoring-sop.md`**: Shadow Monitoring SRE SOP — 完整 runbook 涵蓋：啟動（本地 / K8s Job）、日常巡檢流程與頻率、異常處理 Playbook（mismatch / missing / 工具故障）、收斂判定標準（7 天 0 mismatch + 覆蓋業務高低峰）、退出與回退步驟。
* **`scripts/tools/baseline_discovery.py`**: Baseline Discovery 工具 — 在負載注入環境下持續觀測指標（connections / cpu / slow_queries / memory / disk_io），計算 p50/p90/p95/p99/max 統計摘要，自動建議 warning (p95×1.2) / critical (p99×1.5) 閾值。產出時間序列 CSV + 統計摘要 CSV + patch_config.py 建議指令。
* **`make baseline-discovery TENANT=db-a`**: Makefile target 快捷入口。

### 🧪 Testing Coverage Expansion (Phase 7)
* **`run_load.sh --type composite`**: 複合負載 — 同時啟動 connections + cpu 負載，用於驗證 `MariaDBSystemBottleneck` 複合警報在真實負載下觸發。
* **`tests/scenario-e.sh`**: Scenario E — Multi-Tenant 隔離測試。修改 tenant A 的閾值/disable metric，驗證 tenant B 完全不受影響。支援 `--with-load` 真實負載模式。
* **`tests/scenario-f.sh`**: Scenario F — HA 故障切換測試。殺掉一個 threshold-exporter Pod → 驗證 alert 持續 → Pod 恢復 → 驗證閾值不翻倍（max by vs sum by）。
* **Migration Guide**: 開頭加入「遷移安全保證」定心丸陳述；Phase C 的「99.9% 一致」修正為準確的工程描述。
* **全域版本一致性**: 統一 6 個文件的 v0.5.0 → v0.7.0 標示。

### 📖 文件更新
* **README.md / README.en.md**: Quick Start 加入 `make demo-full`（動態負載展演）與 `make test-alert`（硬體故障測試）的語義區分。新增「企業級價值主張」表格（Risk-Free Migration, Zero-Crash Opt-Out, Full Lifecycle, Live Verifiability）融入痛點與解決方案區塊。
* **rule-packs/README.md**: 補充「動態卸載 (optional: true)」文件 — 說明 Projected Volume 的 `optional: true` 機制，含卸載/恢復操作範例。
* **Makefile**: `test-alert` 重新定義為「硬體故障/服務中斷測試 (Hard Outage Test)」；`demo-full` 定義為「動態負載展演 (Live Load Demo)」。

### 🎯 Makefile Targets
* `make load-connections TENANT=db-a` — 連線數風暴
* `make load-cpu TENANT=db-a` — CPU 與慢查詢
* `make load-stress TENANT=db-a` — 容器 CPU 極限
* `make load-composite TENANT=db-a` — 複合負載 (connections + cpu)
* `make load-cleanup` — 清除所有壓測資源
* `make load-demo TENANT=db-a` — 壓測 Demo（啟動 → 觀察 → 手動 cleanup）
* `make demo-full` — 完整端對端 Demo（含 Live Load）
* `make test-scenario-a ARGS=--with-load` — Scenario A 真實負載模式
* `make test-scenario-b ARGS=--with-load` — Scenario B 真實負載模式
* `make test-scenario-e ARGS=--with-load` — Scenario E 多租戶隔離（可選真實負載）
* `make test-scenario-f TENANT=db-a` — Scenario F HA 故障切換

---

## [v0.6.0] - Enterprise Governance (Phase 5) (2026-02-27)

本版本為 Phase 5 企業級治理，針對大型客戶（1500+ 條規則）的遷移場景提供完整的工具鏈與安全機制。

### 🏗️ Architecture: Rule Pack 動態開關
* **Projected Volume `optional: true`**: 所有 6 個 Rule Pack ConfigMap 加上 `optional: true`，允許客戶透過 `kubectl delete cm prometheus-rules-<type>` 卸載不需要的黃金標準 Rule Pack，Prometheus 不會 Crash。大型客戶可關閉黃金標準，改用自訂規則包。

### 🔧 Tooling: migrate_rule.py v3 (企業級遷移)
* **Triage Mode (`--triage`)**: 大規模遷移前的分析報告，輸出 CSV 檔案可在 Excel 中批次決策。自動將規則分為 auto / review / skip / use_golden 四桶。
* **Prefix 隔離 (預設 `custom_`)**: 遷移產出的 Recording Rule 自動加上 `custom_` 前綴，在命名空間層面與黃金標準徹底隔離，避免 `multiple matches for labels` 錯誤。
* **Prefix Mapping Table**: 自動產出 `prefix-mapping.yaml`，記錄 custom_ 前綴與黃金標準的對應關係，方便未來收斂。
* **Metric Heuristic Dictionary**: 外部 `metric-dictionary.yaml` 啟發式比對，自動建議使用者改用黃金標準。平台團隊可直接維護字典，不需改 Python code。
* **收斂率統計**: 報告中顯示壓縮率，讓客戶看到規則收斂的成效。
* **Shadow Labels**: 遷移產出的 Alert Rule 自動帶上 `source: legacy` 與 `migration_status: shadow` label，支援 Alertmanager 雙軌並行。

### 🔍 Tooling: Shadow Monitoring 驗證
* **`validate_migration.py`**: 透過 Prometheus API 比對新舊 Recording Rule 的數值輸出（而非 Alert 狀態），精準度 100%。支援批次比對（讀取 prefix-mapping.yaml）、持續監控模式（`--watch`）、CSV 報告輸出。

### 🗑️ Tooling: 下架工具
* **`offboard_tenant.py`**: 安全 Tenant 下架工具，含 Pre-check（檔案存在、跨引用掃描）+ 執行模式。
* **`deprecate_rule.py`**: 規則/指標三步下架工具 — (1) _defaults.yaml 設 disable (2) 掃描清除 tenant 殘留 (3) 產出 ConfigMap 清理指引。支援批次處理多個 metric。

---

## [v0.5.0] - Enterprise High Availability (Phase 4) (2026-02-26)

本版本為 Phase 4 企業級高可用性 (HA) 架構的重大升級。系統現在具備了容錯轉移能力、避免閾值重複計算的底層防護，以及專屬的平台自我監控網。

### 🚀 Architecture & High Availability
* **預設 2 Replicas**: `threshold-exporter` 的預設副本數提升至 2，消除單點故障 (SPOF) 風險。
* **Pod Anti-Affinity**: 引入軟性反親和性調度 (`preferredDuringSchedulingIgnoredDuringExecution`)，確保 Pod 盡可能分散於不同節點，同時相容本地 Kind 單節點叢集。
* **Pod Disruption Budget (PDB)**: 新增 PDB 確保在 K8s Node 維護期間，至少有 1 個 Exporter Pod (`minAvailable: 1`) 存活提供服務。
* **Platform Self-Monitoring (平台自我監控)**: 新增專門監控 Exporter 自身健康的第 6 個 Rule Pack (`configmap-rules-platform.yaml`)，並已透過 Projected Volume 預載入 Prometheus。包含 `ThresholdExporterDown`、`ThresholdExporterAbsent`、`ThresholdExporterTooFewReplicas` 與 `ThresholdExporterHighRestarts` 等防護警報。

### 🛠️ Fixes & Documentation
* **修復 Double Counting 數學陷阱**: 將所有 Rule Packs 內的 Threshold Normalization Recording Rules 聚合函數由 `sum by(tenant)` 全面修正為 **`max by(tenant)`**。徹底解決了當 Replica > 1 時，Prometheus 抓取多個 Pod 導致閾值翻倍的致命問題。
* **文件對齊**: 更新 `README.md`、`migration-guide.md` 與 `rule-packs/README.md`，明確標示 HA 架構與 6 個預載 Rule Packs，並同步更新測試斷言以符合最新輸出格式。

---

## [v0.4.0] - Ease of Adoption & Zero-Friction (Phase 3) (2026-02-25)

本版本為 Phase 3 的集大成之作！系統全面轉向「開箱即用」與「零阻力導入」，並大幅重構了底層 ConfigMap 掛載架構與安全性。

### 🚀 Features & Enhancements
* **Rule Packs 解耦與預載 (Projected Volumes)**: 
  * 將龐大的單一 Prometheus ConfigMap 拆解為 5 個獨立的 `configmap-rules-*.yaml` (MariaDB, Kubernetes, Redis, MongoDB, Elasticsearch)，不同維運團隊可獨立維護自己的領域。
  * 透過 Kubernetes Projected Volume 將所有 ConfigMap 無縫投射至 Prometheus 中。
  * **100% 預載入**: 平台預設載入所有 5 大權威 Rule Packs。受惠於 Prometheus 的空集合 (Empty Vector) 運算特性，未部署的 DB 不耗費效能。租戶只需寫入閾值即刻生效，不需再做 Helm 掛載設定。
* **Scaffold 工具 (`scaffold_tenant.py`)**: 互動式租戶設定精靈，一鍵產生新租戶的 ConfigMap 架構 (`_defaults.yaml` 與 `<tenant>.yaml`)。
* **遷移工具 UX 終極進化 (`migrate_rule.py` v2)**:
  * **智能聚合猜測 (Heuristics)**: 自動根據 PromQL 語法 (如 `rate`, `percent`) 猜測聚合方式 (`sum` vs `max`)。
  * **視覺化防呆 (ASCII Warnings)**: 當套用 AI 猜測時，自動在生成的 YAML 中插入醒目的 ASCII 警告區塊，強制人工 Double Check。
  * **檔案化輸出與 Boilerplate**: 工具輸出至 `migration_output/`，自帶合法 YAML 縮排結構，並自動對重複的 Recording Rule 進行去重 (Deduplication)。

### 🛡️ Proactive Security (SAST Fixes)
* **OS Command Injection**: 全面移除 Python 工具中的 `shell=True`，改用 List 安全傳遞參數。
* **Gosec G112 (Slowloris)**: 於 Go exporter 的 HTTP Server 中補齊 `ReadHeaderTimeout: 3 * time.Second` 防護。
* **CWE-276 (File Permissions)**: Python 自動寫檔與 Go 測試建立假目錄時，嚴格限制權限為 `0600`/`0700`。
* **SSRF False Positive**: 為 `check_alert.py` 增加 `# nosec B310` 排除本機 API 誤判。

---

## [v0.3.0] - Dimensional Metrics Milestone (Phase 2B) (2026-02-25)

系統現在具備了處理 Redis、Elasticsearch、MongoDB 等多維度指標的能力。

### 🚀 Features
* **Label Selector Syntax**: 租戶現在可以透過 PromQL 風格的標籤選擇器來設定特定維度的閾值 (例如 `"redis_queue_length{queue='tasks'}": "500"`)。
* **Unchecked Collector Refactor**: `threshold-exporter` Go 核心升級為動態 Descriptor 模式，能將解析出的自訂維度標籤直接輸出為 Prometheus metric 標籤。
* **Authoritative Templates**: 新增業界標準的設定範本 (`config/conf.d/examples/`)，涵蓋 Redis (Oliver006)、Elasticsearch (Prometheus Community) 與 MongoDB (Percona) 的最佳實踐。
* **Smart Dimension Hints**: `migrate_rule.py` 現在能偵測傳統 PromQL 中的維度標籤，並在終端機輸出對應的 YAML 設定提示。

---

## [v0.2.0] - GitOps Directory Scanner & Migration Tooling (Phase 2A/C/D) (2026-02-24)

大幅提升擴展性，徹底解耦 ConfigMap，為 GitOps 鋪平道路。

### 🚀 Features
* **Directory Mode (`-config-dir`)**: `threshold-exporter` 支援掃描並深度合併 `conf.d/` 目錄下的多個 YAML 檔案 (`_defaults.yaml` + `<tenant>.yaml`)，完美解決單一 ConfigMap 的合併衝突問題。
* **Robust Hot-Reloading**: 捨棄 ModTime，改用 **SHA-256 Hash 比對**，完美解決 Kubernetes ConfigMap volume symlink 輪轉時的熱重載延遲與漏抓問題。
* **Boundary Enforcement**: 實作嚴格邊界規則，禁止租戶檔案覆寫平台級設定 (`state_filters`, `defaults`)。
* **Automated Migration Tooling (`migrate_rule.py` v1)**: 首個版本的傳統 PromQL 警報轉換工具，支援 80/20 法則自動拆解三件套，複雜語義優雅降級為 LLM Prompt。
* **Migration Guide**: 釋出第一版完整的架構遷移指南。

---

## [v0.1.0] - The Composite Priority Milestone (Phase 1) (2026-02-23)

首個正式版本。完成了所有基礎場景的驗證，確立了 Config-driven 與 Hot-reload 的動態警報架構。

### 🚀 Features
* **Dynamic Thresholds (Scenario A)**: 實作 Go `threshold-exporter`，支援三態邏輯 (Custom Value / Default / Disable)。
* **Weakest Link Detection (Scenario B)**: 整合 `kubelet-cadvisor`，實現容器層級資源 (CPU/Memory) 的最大值 (Max) 瓶頸監控。
* **State Matching (Scenario C)**: 透過乘法邏輯 (`count * flag > 0`) 結合 `kube-state-metrics`，實現 Kubernetes 狀態 (如 CrashLoopBackOff) 的動態開關。
* **Composite Priority Logic (Scenario D)**:
  * **Maintenance Mode**: 使用 `unless` 邏輯全域抑制特定租戶的常規警報。
  * **Composite Alerts**: 結合 `and` 邏輯，僅在多重症狀同時發生時觸發警報 (如高連線數 + 高 CPU)。
  * **Multi-tier Severity**: 支援 `_critical` 後綴配置，具備 Critical 觸發時自動降級 Warning 警報的功能。
