---
title: "Benchmark 操作手冊 (Benchmark Playbook)"
tags: [documentation, performance]
audience: [platform-engineer, sre]
version: v2.7.0
verified-at-version: v2.8.0
lang: zh
---
# Benchmark 操作手冊 (Benchmark Playbook)

> Benchmark 方法論、執行環境、已知陷阱與踩坑記錄。與測試排錯分離，便於獨立查閱。
> **相關文件：** [benchmarks.md](../benchmarks.md)（量測數據）· [Testing Playbook](testing-playbook.md)（K8s/測試排錯）· [Test Map § Benchmark 基線](test-map.md#benchmark-基線)· [Windows-MCP Playbook](windows-mcp-playbook.md)（docker exec 陷阱）

### Quick Action Index

| 我要做什麼 | 跳到 |
|-----------|------|
| 跑 benchmark 指令 | [§自動化指令](#自動化指令) |
| 方法論（輪數/格式） | [§方法論](#方法論) |
| Dev Container 內執行 | [§在 Dev Container 內執行](#在-dev-container-內執行) |
| Routing Bench | [§Routing Bench 注意事項](#routing-bench-注意事項) |
| Under-Load Bench | [§Under-Load Bench 注意事項](#under-load-bench-注意事項) |
| 踩坑記錄 | [§踩坑記錄](#踩坑記錄-lessons-learned) |

---

## 自動化指令

```bash
make benchmark                    # idle-state 基礎報告
make benchmark ARGS="--under-load --routing-bench --alertmanager-bench --reload-bench --json"
```

## 方法論

| Benchmark 類型 | 建議輪數 | 報告格式 | 耗時 |
|----------------|---------|---------|------|
| idle-state | 5 輪，間隔 45s | mean ± stddev | ~4min |
| under-load (N=100) | 5 輪 | median ± stddev | ~15min |
| Go micro-bench (`-count=N`) | 5 次 | median, stddev | ~1min (8 benchmarks), ~3min (含 1000-tenant) |
| routing-bench (N=2/10/50/100/200) | 10 輪 | median ± stddev | ~2min |
| alertmanager-bench | 5 輪 (idle) / under-load | 快照 | ~1min |
| reload-bench | 5 輪 | median | ~2min |
| pytest-benchmark | min_rounds=20, warmup=on | median | ~30s |
| v2.0.0 功能基線 | 20 輪 | median | ~1min |

**統計原則：**

- 永遠取多輪 median，避免單次值偏差。minimum 5 輪，routing-bench 建議 10 輪
- 報告 median ± stddev，讓讀者判斷穩定性
- pytest-benchmark 自帶統計引擎（min_rounds, warmup），直接引用其 median
- Go micro-bench 用 `-count=5` 確保統計意義

## 在 Dev Container 內執行

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

**MCP 背景執行（Cowork VM → MCP → WSL → Docker）：**

```bash
# docker exec -d 背景執行，日誌寫到 /tmp
wsl docker exec -d vibe-dev-container bash -c \
  "cd /workspaces/vibe-k8s-lab && bash scripts/benchmark.sh --under-load --tenants 100 > /tmp/bench.log 2>&1"

# 定期檢查進度（MCP PowerShell timeout 預設 30s，長指令必須背景化）
wsl docker exec vibe-dev-container tail -20 /tmp/bench.log
```

**Go micro-bench 同理（建議 redirect 到檔案以避免 log 噪音影響）：**
```bash
cd /workspaces/vibe-k8s-lab/components/threshold-exporter/app
go test -bench=. -benchmem -count=5 -run="^$" ./... > /tmp/bench.txt 2>/tmp/bench_err.txt
grep "ns/op" /tmp/bench.txt   # 僅看結果行
```
> 注意：incremental reload benchmark 已內建 `silenceLogs(b)`，但 `Resolve*` 系列不需要（無 log 輸出）。

## benchmark.sh 已知問題

- **`local` 關鍵字限制**：`local` 只能在 function 內使用。若 for loop 在 top-level scope 使用 `local`，bash 不報錯但部分 shell 會。移除即可。
- **`grep -E '--- [0-9]+'` pattern**：`---` 被 grep 解讀為選項旗標。用 `grep -E -- '--- [0-9]+'` 或 `grep -E '\-\-\- [0-9]+'` 避免。此問題不阻塞執行（routes 有 fallback 解析）。
- **MCP timeout**：Under-Load benchmark 含 90s reload 等待 + 35s scrape cycle，MCP PowerShell 預設 30s timeout 不夠。必須用 `docker exec -d` 背景化。

## Routing Bench 注意事項

- **純 Python 操作**，不需 K8s 環境；Cowork VM 可直接跑
- 產出解析：用 summary line `--- N route(s), M receiver(s), K inhibit rule(s) ---` 取計數，不要 grep YAML 內容（`- match:` vs `- matchers:` 格式隨版本變化）
- 合成 tenant 需包含 routing（6 種 receiver type）+ severity_dedup + routing overrides，才能代表真實複雜度
- **CLI E2E vs 純 route generation**：CLI wall time 含 Python 啟動（~130-400ms，佔 ~70%），純 route generation 為 sub-ms。兩者分開報告，避免誤導

## Under-Load Bench 注意事項

- 合成 100 個 tenant → patch ConfigMap → 等 exporter SHA-256 hot-reload → 等 2 個 scrape cycle → 採樣
- **reload 偵測依賴 `user_threshold` series 數量增長**，若 exporter 未連接 ConfigMap volume 則永遠 timeout
- **scrape cycle 等待必須 ≥ 35s**（2 × 15s scrape_interval + buffer）
- 完成後自動清理合成 tenant（移除 `synth-*` key）
- Kind 環境 100 tenant 安全；1000+ tenant 可能撐爆記憶體

## Alertmanager Bench 注意事項

- idle 狀態下 notification latency histogram 為空（無 alert 觸發）→ 需 `--under-load` 或 `make demo-full` 產生流量才有數據
- Alertmanager port-forward: `kubectl port-forward svc/alertmanager 9093:9093 -n monitoring`
- 關鍵 metrics: `alertmanager_notification_latency_seconds`、`alertmanager_alerts_received_total`、`alertmanager_nflog_maintenance_errors_total`

## Reload Bench 注意事項

- `/-/reload` API 本身 sub-millisecond（~0.3ms）
- `--apply` E2E 瓶頸在 kubectl API server 交互（~600ms），非 route generation 或 reload
- **sidecar 不可靠觸發**：僅改 annotation 不觸發 sidecar（見 testing-playbook sidecar 行為章節）
- Kind 環境 E2E ~760ms；生產環境（dedicated etcd）預期 < 500ms

## v2.0.0 功能基線注意事項

- Policy-as-Code、Alert Quality、Cardinality Forecasting 皆為純 Python 計算
- 瓶頸在 Prometheus 查詢 I/O（1-3s），非運算本身
- pytest-benchmark 量測 in-process 效能（20 輪），CLI E2E 量測含 I/O 全鏈路

---

## 踩坑記錄 (Lessons Learned)

### Alertmanager email_configs.to 格式問題 (v2.0.0-preview.4)

**現象：** Alertmanager CrashLoopBackOff，日誌顯示 `cannot unmarshal !!seq into string`。

**根因：** `generate_alertmanager_routes.py` 產生的 `email_configs[].to` 為 YAML list（`["addr@example.com"]`），但 Alertmanager 期望純字串（`"addr@example.com"`）。

**修復：** 在 route generation 時確保 `to` 欄位為 string type，而非 list。

**延伸問題：** 修復 `to` 格式後，Alertmanager 仍 crash，錯誤為 `no global SMTP from set`。需在 `global` 區塊加入 `smtp_from: alertmanager@example.com`。

**教訓：**

- Alertmanager 的 email_configs schema 對型別嚴格（string vs list），YAML dump 時需注意
- 有 email receiver 時，`global.smtp_from` 為必填
- 測試 Alertmanager config 時，先用 `amtool check-config` 本地驗證再 patch

### Go benchmark log 噪音致 output 爆量 (v2.1.0 → v2.8.0 A-15 codified)

> **v2.8.0 A-15 收束**：本節原約 40 行的 per-case guidance 已被 `scripts/tools/ops/bench_wrapper.sh` + `bench_filter.go` 取代為**單一標準路徑**。新用法：
>
> ```bash
> make go-bench-clean                     # 對等於原 go-bench，但 stdout 乾淨
> BENCH_OUT_DIR=_out make go-bench-clean  # 指定輸出目錄
> COUNT=3 make go-bench-clean             # 覆寫 -count
> ```
>
> 產出：`bench.out.txt`（僅 `goos:` / `goarch:` / `pkg:` / `cpu:` / `BenchmarkX ns/op` / `PASS` / `FAIL` 行）、`bench.err.log`（log.Printf 原 stderr）、`bench.raw.jsonl`（`go test -json` 原 event stream 供 debug）。
>
> 底層實作：`go test -bench ... -json <args>` → `bench_filter.go` 解析 JSON event，只保留「benchmark result pattern」+ 「suite header/summary」。-json 把 stdout/stderr 分流，彻底消除 log 污染。

**以下原 v2.1.0 LL 保留為背景，供 `bench_wrapper.sh` 出問題時的 root-cause 參考：**

**現象：** `go test -bench="FullDirLoad|IncrementalLoad" -benchmem -count=5` 產出 ~732KB 的 stdout，benchmark 結果行被淹沒在 log 噪音中。`2>/dev/null` 在某些 Docker exec 管線下無效（stdout 也被丟棄）。

**根因：** `fullDirLoad()` 和 `IncrementalLoad()` 每次呼叫都寫一行 `log.Printf("Config loaded ...")`，100 tenant × N iterations × 5 rounds = 數千行 log。

**原修復（仍建議搭配 wrapper）：** 在 benchmark 函數中加入 `silenceLogs(b)` helper：

```go
func silenceLogs(b *testing.B) {
    b.Helper()
    orig := log.Writer()
    log.SetOutput(io.Discard)
    b.Cleanup(func() { log.SetOutput(orig) })
}
```

`silenceLogs(b)` 要放在 setup `fullDirLoad()` **之前**（不只是 `b.ResetTimer()` 前），否則每次 benchmark invocation 的 setup phase 仍會產生 log。`b.Cleanup()` 確保 benchmark 結束後恢復正常 log 輸出。Wrapper + silenceLogs 雙層防禦：即使新 benchmark 作者忘了加 silenceLogs，wrapper 的 -json 分流也能把污染隔離到 `bench.err.log`。

### 連續多輪 benchmark port-forward 不穩定 (v2.0.0-preview.4)

**現象：** 用 for loop 連續跑 5 輪 `benchmark.sh --under-load`，第 3 輪起 Prometheus metrics 全部返回 0。

**根因：** benchmark.sh 的 cleanup trap 會 kill port-forward process，但下一輪啟動時 port-forward 可能尚未完全釋放埠號，或 Prometheus pod name 已變更（因上一輪的 ConfigMap patch 觸發 restart）。

**解法：** 每輪獨立執行（手動或間隔 30s），不要用 tight loop。或在 loop 間加入 `sleep 30` + `kill_port 9090` 確保埠號乾淨。

---

## v2.8.0 1000-Tenant Hierarchical Baseline (Phase 1, B-1)

> ⛔ **此 baseline 非 definitive SLO 承諾** ⛔
>
> 本節數字為 Phase 1 synthetic fixture 量測，**不能直接寫進客戶合約 SLA**。definitive SLO sign-off 必須等 Phase 2 customer anonymized sample 校準後重跑（DEC-B in v2.8.0-planning §10）。下游文件（pitch deck / proposal / 客戶 onboarding 文件）若引用本節數字，**必須**附帶「Phase 1 synthetic baseline」前綴。

> **Scope + honest caveats**：
>
> - **包含**：infrastructure SLOs（scan / reload / blast-radius）+ resource metrics（heap / goroutines / virtual memory）+ 1000/2000/5000 三點 scaling characterization
> - **不包含**：(a) Prometheus + Alertmanager alert fire-through latency（需 full-stack e2e，Phase 2）；(b) 客戶真實 workload 校準（fixture 分布 per `generate_tenant_fixture.py` 不代表客戶 domain/region/env 比例，等 customer anonymized sample 到位後 Phase 2 re-run）
> - **Variance**：Dev Container CI runner timing 有 20-50% noise（observed 跨兩次重跑 same code: scan 32→51ms, FullDirLoad 146→237ms）。下節數字以 **3-run min/median/max 範圍**呈現。需 statistical SLO 鎖定請走 count=10+ 並排除外部 IO 干擾

### 量測方法論

- **Fixture**: `buildDirConfigHierarchical(b, 1000)` — 8 domains × 6 regions × 3 envs = 144 leaf dirs，1000 tenant files（~7 per leaf），`_defaults.yaml` at L0/L1/L2/L3（4 層）
- **Harness**: A-15 `bench_wrapper.sh`（PR #48 stdout-clean `-json` filter）
- **Runs**: `-count=3 -timeout=15m` — 3 runs per bench for variance; report (min / median / max)
- **Env**: Dev Container（Ubuntu 22.04.5 LTS / Intel Core 7 240H / Go 1.26）
- **Resource metrics**: `runtime.GC()` ×2（reap finalizers）+ `runtime.ReadMemStats` → `HeapAlloc` after GC / `Sys` / `NumGoroutine`

### Latency Baseline 1000-tenant（3-run min / median / max, ns → ms）

> **註**：以下 1000-tenant 數字為**第一次量測** run（PR #59 v1）；第二次 run（v2，加 2000/5000 後重跑）數字略高（OS noise）。**scaling 分析下節用第二次 run 完整數據**。

| Operation | Min ms | Median ms | Max ms | Variance | Notes |
|---|---|---|---|---|---|
| `scanDirHierarchical` (bare walk + hash + graph) | 32.3 | **32.4** | 35.1 | 8.6% | 純掃描熱路徑，A-10 (PR #54) 新的 WatchLoop 入口 |
| `fullDirLoad` (cold load, 1000 tenants) | 143.0 | **146.0** | 147.0 | 2.7% | 含 YAML parse + merge + populateHierarchyState |
| `diffAndReload` (no change) | 186.4 | **189.4** | 215.8 | 13.9% | **註**：diffAndReload L341 最後仍會呼 `fullDirLoad` — no-change case 時間 ≈ fullDirLoad，per-tenant diff 階段只 ~5μs |
| `diffAndReload` (1 tenant file change) | 201.5 | **203.0** | 224.6 | 10.2% | 與 NoChange 相近（dominant cost: 尾段 fullDirLoad）|
| Blast-radius (1 region `_defaults.yaml` change, 21 tenants affected) | 195.8 | **212.0** | 214.3 | 8.4% | 21 tenants re-merged + fullDirLoad |

### Scaling Characterization 1000 / 2000 / 5000（3-run median ms）

> **目的**：empirically 驗證「線性外推到 10000 = 2 秒可接受」這個 sharding 決策假設是否成立 — **不要從單一 1000 點外推**。本節用 1000 / 2000 / 5000 三點建立趨勢。

| Operation | 1000 | 2000 | 5000 | 1000→2000 ratio | 1000→5000 ratio | Linearity |
|---|---|---|---|---|---|---|
| `scanDirHierarchical` | 51 | 105 | 273 | **2.06×** | **5.35×** | 略 super-linear (~7% over linear) |
| `fullDirLoad` | 237 | 570 | 1097 | **2.41×** | **4.63×** | 混合（2× super-linear, 5× sub-linear；variance 影響）|
| `diffAndReload NoChange` | (run 中 OOM-fixture-ENOSPC) | — | — | — | — | bench 在 5000 跑時 fixture 寫入磁碟壓力大；measurement omitted |
| `BlastRadius (defaults change)` | 266 | 535 | 1308 | **2.01×** | **4.92×** | near linear |
| `affected-tenants` (BlastRadius scope) | 21 | 42 | 105 | **2.0×** | **5.0×** | 嚴格 linear（geometric expectation 1 region × 3 envs × N/144 per leaf）|

#### 觀察

1. **Linear-ish 但不完美**：`scanDirHierarchical` 5× 規模 → 5.35× 時間（+7% over linear），`BlastRadius` 5× → 4.92× 時間（near linear）。沒有發現 O(N²) 級的劣化。
2. **Memory linear**：allocs/op 隨 N 線性（172K → 338K → 834K for scan）。Sys (RSS) 1000=19MB / 2000=29MB / 5000=42MB — 線性。
3. **Goroutines 穩定 = 2**：1000、2000、5000 都 2 個 goroutine — **無 leak signal at scale**。
4. **`diffAndReload NoChange`** 在 5000-tenant fixture 重跑時遇到 fixture write contention；數據 inconclusive，留 follow-up 加 count=5 重測。

#### 10000-tenant 線性外推（不是實測 — 標 caveat）

| Operation | 5000 實測 | 10000 線性外推 | 10000 +20% safety | 是否 acceptable for production |
|---|---|---|---|---|
| `scanDirHierarchical` | 273 ms | ~550 ms | ~660 ms | ✅ acceptable（per-tick scan，scrape 15-30s 完全容忍）|
| `fullDirLoad` | 1097 ms | ~2200 ms | ~2640 ms | ⚠️ borderline — Alertmanager rule reload 在 cascading batch 時可能出現 stall |
| BlastRadius | 1308 ms | ~2620 ms | ~3144 ms | ⚠️ 同上，每 region defaults 變更需 ~3s reload |
| Allocs / FullDirLoad | 4.17 M | ~8.3 M | ~10 M | ⚠️ Go GC pressure 提升；建議搭 GOGC tuning |
| Memory (sys RSS) | 42 MB | ~80 MB | ~100 MB | ✅ 仍小，無記憶體瓶頸 |

#### Sharding 決策建議（empirical, not extrapolated）

- **≤ 2000 tenant**: 完全無瓶頸；現架構充裕應對
- **2000-5000 tenant**: 可運行，但每次 reload 1-1.3 秒；批次 PR pipeline 後 cascading reload 需要 staggering
- **5000-10000 tenant**:
  - **可行 IF** (a) 落地 `diffAndReload` skip-trailing-fullDirLoad 優化（省 ~70% reload time on no-change tick） (b) 接受 ~2-3 秒 reload latency (c) GOGC tuning
  - **可行 ELSE** sharding 為 2-4 個獨立 conf.d/ tree，各自 mount → 平行掃描
- **> 10000**: sharding 強烈建議，不再依賴單 process 路徑

#### 不要做的判斷

- ❌ 不要從單一 1000-tenant 數字線性外推到 10000 並斷言「不需 sharding」
- ❌ 不要把上面 "10000 線性外推" 表當實測（標 ~ 字符 + 「不是實測」明示）
- ❌ 不要在客戶合約寫「10000 tenant 2 秒 reload」— 沒實測過

### Resource Baseline

| Metric | Value | Notes |
|---|---|---|
| Heap after GC (steady state) | **0.46-0.50 MB** | 極小 working set；GC 後 |
| Sys (total virtual) | **18-20 MB** | OS 視角總 VM |
| Goroutines (benchmark steady) | **2** | main + test runner；無 leak signal |
| Allocs per diff-reload op | ~1,000,000 | ~1K allocs/tenant；主要 YAML parse |
| Allocs per bare scan op | ~172,000 | ~172/tenant |

### 發現（Phase 1 baseline 量測時順手踩出）

1. **`diffAndReload` NoChange/OneTenantChanged/BlastRadius 時間相近**：因為 diffAndReload L341 尾段呼 `fullDirLoad` — per-tenant diff 階段快（5-50μs），但尾段 full-reload 占 dominant 150ms。**Phase 2 優化候選**：當 diff stage 顯示無變化時，skip 尾段 fullDirLoad 可省 ~150ms/tick
2. **"Quiet defaults edit" noOp 生效**：blast-radius bench 初版用 `container_memory` key（tenants 有 override）→ defaults 變更被 shadowed → `affected-tenants: 0`（對應 `config_debounce.go` L313-318 的 quiet edit detection）。改用 `region_alert_schedule`（tenants 無 override）後 → 21 affected，符合預期。**Bench design 教訓**：測 blast-radius 必用 tenants 不 override 的 key，否則量到的是噪音。**v2.8.0 (Issue #61) production-equivalent metric**：`b.ReportMetric("affected-tenants")` 對應 production 的 `da_config_blast_radius_tenants_affected{effect="applied"}` histogram；當前 bench 量到的 21/42/105 等同於 `_sum / _count` 的單一觀測值。Shadowed/cosmetic 案例在 production 由 `effect={shadowed,cosmetic}` 分流，與此 bench 互補
3. **`IncrementalLoad` ≠ hierarchical 熱路徑**：v2.6.0 保留的 `IncrementalLoad` 使用 `scanDirFileHashes`（root-only flat），不走 hierarchical。**A-10 後 production 走 WatchLoop → `diffAndReload` → `scanDirHierarchical`**。Benchmark 作者注意 call site 選對

### Phase 2 延伸（blocked）

- **Alert fire-through e2e**：需 Prometheus + Alertmanager + receiver，目前只量內部 SLO
- **Customer sample 校準**：synthetic fixture 分布未必等同客戶 workload；Phase 2 帶客戶 anonymized sample re-run
- **SLO definitive sign-off**：B-2 hard SLO 需 3+ 輪 customer 環境驗證

---

## v2.8.0 Phase 2 e2e Alert Fire-through (B-1 Phase 2, design + skeleton)

> **Status**: design contract 完成（PR #64 / `design/phase-b-e2e-harness.md`）；implementation 進行中（B-1.P2-a/b 在本 PR 落地，c-g 後續）。本節是 playbook 操作層 skeleton — design doc 是 SSOT，這裡只放 ops 視角的「怎麼跑、看哪幾個數字、customer sample 校準怎麼操作」。

### 5-anchor 量測模型（摘要）

完整 5-anchor 鏈與 stage 拆解見 `design/phase-b-e2e-harness.md` §2.5。Ops 視角速查：

| Anchor | Time | 量測來源 | 對應 stage |
|---|---|---|---|
| **T0** | now() | driver 寫 fixture | — |
| **T1** | exporter `last_scan_complete_unixtime_seconds` 值 | 本 PR (B-1.P2-a) 加的 gauge | A: scan |
| **T2** | exporter `last_reload_complete_unixtime_seconds` 值 | 本 PR (B-1.P2-a) 加的 gauge | B: reload |
| **T3** | Prometheus `/api/v1/alerts` `activeAt` | Prom internal time | C: scrape+eval |
| **T4** | receiver POST timestamp | webhook receiver 記錄 | D: alertmanager dispatch |

stage(s) = T1−T0 (A) / T2−T1 (B) / T3−T2 (C: scrape+eval 不拆分) / T4−T3 (D)
e2e_ms = T4 − T0

**Ops 為何要會看 T1/T2 兩個 gauge**：
1. **Production stuck-detection** — `time() - da_config_last_scan_complete_unixtime_seconds > 60` → scanner 卡死（gauge 永不會 backfill 過去值，所以 stale gauge 一定是 stuck，不是「忘了 emit」）
2. **E2E harness 對齊** — harness driver 寫 fixture 後輪詢這兩個 gauge，看到值大於寫入 timestamp 才往下走。**Driver / exporter 同 kernel clock**（皆在 docker-compose 內）所以無 clock skew 問題

### Fixture kinds & calibration gate

完整 calibration gate 規格見 design doc §6.5。Ops 速查：

| `fixture_kind` | 來源 | `gate_status` 預設 | 用途 |
|---|---|---|---|
| `synthetic-v1` | PR #59 `buildDirConfigHierarchical` (uniform 分布) | `pending` | Phase 1 baseline，不再為 Phase 2 主基準 |
| `synthetic-v2` | 本 PR (B-1.P2-b) `generate_tenant_fixture.py --layout synthetic-v2` (Zipf+power-law 分布) | `pending` | Phase 2 主基準；customer sample 抵達前唯一 baseline；customer sample 抵達後做 ±30% 校準 gate 對照 |
| `customer-anon` | 客戶 anonymized sample（gitignored, manual 取得） | `pending` → `passed/failed/voided` | Definitive baseline 來源；填回 `synthetic-v2` 校準狀態 |

**Customer sample 校準操作流程**（cutover 前 2 週執行；客戶 fixture 拿到後）：

```bash
# 1. 把客戶 sample 解壓到 tests/e2e-bench/fixture/customer-anon/conf.d/
#    (整個 customer-anon/ 目錄 gitignored)
tar -xzf customer-sample.tar.gz -C tests/e2e-bench/fixture/customer-anon/

# 2. 跑 e2e harness 對 customer-anon (n>=30)
COUNT=30 E2E_FIXTURE_KIND=customer-anon make bench-e2e  # Makefile target 預定 PR-3 加

# 3. 比對最近一次 synthetic-v2 P95 — gate 通過條件:
#    customer-anon P95 與 synthetic-v2 P95 差距 ≤30%
python3 scripts/tools/dx/compare_e2e_baseline.py \
    --customer bench-results/e2e-customer-anon-*.json \
    --baseline bench-results/e2e-synthetic-v2-*.json \
    --threshold-pct 30  # PR-3 預定加的工具
```

通過後填回 synthetic-v2 對應 run 的 `gate_status: "passed"`（calibration confirmed）；失敗則 `gate_status: "voided"` 並在本節章節加紅框。

**Kill switch**：v2.9.0 cut 前若 customer sample 未抵達，強制 go/no-go review — 要嘛 explicit 接受 synthetic-v2 為定案 baseline（含 fixture 假設文件化），要嘛 rescope phase 2。詳 design doc §6.5 + §11。

### Implementation 進度 tracker

| 子項 | 內容 | 狀態 | PR |
|---|---|---|---|
| **B-1.P2-a** | exporter timestamp gauges (`last_{scan,reload}_complete_unixtime_seconds`) | 🟢 | PR #78 |
| **B-1.P2-b** | `generate_tenant_fixture.py --layout synthetic-v2` (Zipf+power-law) | 🟢 | PR #78 |
| **B-1.P2-c** | docker-compose stack (exporter + Prometheus + Alertmanager + pushgateway + receiver) | 🟢 | PR #79 |
| **B-1.P2-d** | host driver (5-anchor 量測 + run isolation + fire+resolve 對稱) | 🟢 | PR #79 |
| **B-1.P2-e** | n≥30 aggregation + bootstrap 95% CI + output JSON `fixture_kind`/`gate_status` | 🟢 | 本 PR (PR-3) |
| **B-1.P2-f** | `make bench-e2e` + `bench-e2e-record.yaml` workflow (main only, manual dispatch) | 🟢 | 本 PR (PR-3) |
| **B-1.P2-g** | playbook 完整章節（含實測數字 + customer-sample calibration flow） | 🟡 ops-flow 完成；**首批 baseline 數字未填**（待第一輪 main workflow_dispatch 後 maintainer 從 aggregate JSON 抽數寫入 §「首批 baseline 數字」） | 本 PR (PR-3) + 後續 doc-only commit |

### 跑一輪 baseline 速查（B-1.P2-f）

**Local（local-only per design §8.1，5-8 min wall-clock）**：

```bash
# Default: synthetic-v2, 1000 tenants, 30 runs.
make bench-e2e

# Override fixture / count / seed.
COUNT=10 E2E_FIXTURE_KIND=synthetic-v1 FIXTURE_TENANT_COUNT=500 make bench-e2e

# Customer-anon vs latest synthetic-v2 baseline (calibration gate ±30%):
BASELINE_GLOB='tests/e2e-bench/bench-results/e2e-*-synthetic-v2.json' \
    E2E_FIXTURE_KIND=customer-anon make bench-e2e
```

**CI（main only, manual dispatch）**：

```bash
# Trigger from CLI (must be on main branch).
gh workflow run bench-e2e-record.yaml -f fixture_kind=synthetic-v2 -f count=30
# Inputs: fixture_kind / count / fixture_tenant_count.
# Artifacts retained 30 days; gate_banner surfaces in run summary.
```

### Tier 1 fail-fast smoke gate（cycle-6 RCA 後加，issue #83 §S#37d）

Driver 跑完 warm_up run 0 後立刻檢查 `per-run-0000.json` 的 5 個 T anchor — 任一為 0（== 沒觀察到）→ exit code 2 → `--abort-on-container-exit driver` → `bench_e2e_run.sh` 整段失敗 → workflow 在 ~2 min（而非 30-60 min timeout）內 fail。**之前 6 個 cycle 每個都浪費 30+ min wall-clock 才 timeout；Tier 1 把這個延遲縮到 ~90s**。

**對應信號**：
- T1=0：exporter scan-complete gauge 從未 advance（multi-tenant 大 fixture cold-load 沒撐過 wait_for_services 的 60s window）
- T2=0：reload gauge 從未 advance（content hash 同前次 → diffAndReload short-circuit）
- T3=0：Prometheus 沒 fire alert（label/expr mismatch、defaults 未載入、tenant 沒 register `bench_trigger`）
- T4=0：webhook receiver 沒收到（Alertmanager 路由斷或 alert 沒持續到 dispatch）

**診斷工件**：workflow 失敗時自動把 `docker compose logs threshold-exporter / prometheus / alertmanager / receiver / driver` dump 到 `bench-results/*.log`，與 `per-run-0000.json` 一起 upload artifact。Exporter `WARN: skip unparseable file` / `WARN: tenant=X: unknown key` 兩條是最高訊息密度的線索。

**Opt-out**：本機 debug harness 本身（不是想抓 cycle-6 那種）想看完整 30 runs 的 timeline，加 `--no-smoke-abort` 或 `NO_SMOKE_ABORT=1`：

```bash
# Driver 預設 ON；只在診斷 driver 自己時 opt-out
NO_SMOKE_ABORT=1 COUNT=3 make bench-e2e
```

### Aggregator 輸出與 gate banner

`make bench-e2e` 完工後（或 `make bench-e2e-aggregate` 對既有 per-run JSONs 重算），在 `tests/e2e-bench/bench-results/` 產出 `e2e-{ISO}-{kind}.json`，schema：

```json
{
  "schema_version": 1,
  "generated_at": "2026-04-26T00:00:00+00:00",
  "fixture_kind": "synthetic-v2",
  "n_runs_total": 30,
  "gate_status": "pending",
  "gate_banner": "🟡 synthetic-v2 pending customer-anon validation; baseline not yet calibrated against real workload",
  "gate_threshold_pct": 30,
  "baseline_p95_fire": null,
  "fire": {
    "n_valid": 30,
    "e2e_ms": {
      "p50": 4145, "p95": 4280, "p99": 4290,
      "p50_ci95": [4135, 4150], "p95_ci95": [4220, 4290],
      "ci_too_wide": false
    },
    "stage_ms": {"A": {"p95": 50}, "B": {"p95": 145}, "C": {"p95": 4255}, "D": {"p95": 50}},
    "stage_c_histogram": [{"le": 5000, "count": 30}, ...]
  },
  "resolve": { ... }
}
```

**Banner 渲染矩陣**（per design §6.5）：

| `fixture_kind` × `gate_status` | Banner |
|---|---|
| `synthetic-v*` × `pending` | 🟡 「pending customer-anon validation」(synthetic-v* 永遠 pending — 只有 customer-anon 能 flip) |
| `customer-anon` × `pending` | ⚠️ 「baseline 未到位 — 先跑 synthetic-v2 再重 aggregate」 |
| `customer-anon` × `passed` | ✅ 「calibration passed: customer P95 X ms 在 ±30% of synthetic-v2 P95 Y ms」 |
| `customer-anon` × `failed` | ❌ 「calibration failed: synthetic-v2 baseline marked voided, all external references must be reviewed」 |

### Customer sample calibration gate operational flow

當 customer anonymized sample 抵達後：

1. 解壓 sample 到 `tests/e2e-bench/fixture/customer-anon/conf.d/`（`gitignored`，never commit；詳 `tests/e2e-bench/fixture/customer-anon/README.md`）
2. 確保有最近一次 synthetic-v2 baseline aggregate JSON 存在（若無，先跑 `make bench-e2e` with default fixture）
3. 跑 customer-anon harness with baseline glob:
   ```bash
   BASELINE_GLOB='tests/e2e-bench/bench-results/e2e-*-synthetic-v2.json' \
       E2E_FIXTURE_KIND=customer-anon \
       make bench-e2e
   ```
4. 看 banner：
   - `passed` → 該批 customer-anon run 標 `gate_status: "passed"`；最近一次 synthetic-v2 P95 retroactively 標 calibration confirmed（可手動 commit a JSON sidecar 標記）
   - `failed` → 兩件事必做：(a) synthetic-v2 baseline 標 `voided`，(b) playbook 本節加紅色 banner，(c) 評估 fixture 假設要怎麼修
5. 若 `failed`，下個 phase planning 必須含「fixture 校正」工作項（synthetic-v2 的 Zipf alpha / power-law alpha / size 範圍 sweep）

### Kill switch — v2.9.0 cut 前 customer sample 未抵達

Per design §6.5 + §11：v2.9.0 cut 前若 customer sample 未抵達，**強制 go/no-go review**：

| 選項 | 後果 |
|---|---|
| Explicit 接受 synthetic-v2 為定案 baseline | 必須在 `pitch-deck-talking-points.md` + `docs/benchmarks.md` 雙處明寫「baseline derived from synthetic-v2 fixture; not customer-validated」disclaimer，DEC-B 在無 customer-data 條件下 sign-off |
| Rescope phase 2 | 把 customer sample 從 acceptance criteria 移除；e2e harness 仍交付但**不**列為 SLO 來源；DEC-B 延 v2.9.0 |

決策時間點：v2.9.0 release-please cut 前 4 週 maintainer review。

### 首批 baseline 數字（待第一輪 workflow_dispatch 後填入）

> **Status**: 本表將於 PR-3 merge 後第一輪 `gh workflow run bench-e2e-record.yaml ...` 完成後，由 maintainer 從 aggregate JSON 抽數填入；customer-anon 數字待 sample 抵達後另填。
>
> **不要**從 PR-3 直接讀數字 — 數字未經 main runner 量測。
>
> **Design §9.3 acceptance**：1000-tenant + 5000-tenant 各跑 30 runs 是 hard requirement。第一輪 baseline 完成 1000-tenant；5000-tenant 列為 v2.8.x **doc-only follow-up**（call workflow with `fixture_tenant_count=5000`，把第二批數字填到下表第二行）。

```
fixture_kind=synthetic-v2  tenants=1000  fire P50=___ms  fire P95=___ms  fire P99=___ms  resolve P50=___ms  resolve P95=___ms  resolve P99=___ms
fixture_kind=synthetic-v2  tenants=5000  fire P50=___ms  fire P95=___ms  fire P99=___ms  resolve P50=___ms  resolve P95=___ms  resolve P99=___ms
fixture_kind=customer-anon                fire P50=___ms  fire P95=___ms  fire P99=___ms  gate_status=___ delta=___% vs synthetic-v2 1000-tenant baseline
```

### Pending follow-ups（design §9 partial-green deferred items）

兩條 design §9 acceptance 在 PR-3 內沒做，明確 deferred：

| § | Item | Plan |
|---|---|---|
| §9.3 | 5000-tenant × 30 runs baseline | Workflow input 已預留 `fixture_tenant_count: 5000` 選項；merge 後在第一輪 1000-tenant baseline 落地後立即跑第二輪 5000-tenant，數字填上表 |
| §9.4 | Design doc 升格 + archive | `docs/internal/design/phase-b-e2e-harness.md` → `docs/internal/archive/design/phase-b-e2e-harness.md`，本 playbook §v2.8.0 Phase 2 e2e 章節加 `archive-of: design/phase-b-e2e-harness.md` cross-ref；待第一輪實測數字進本節後做 doc-only follow-up PR |

### 產出 fixture 速查（B-1.P2-b）

```bash
# Synthetic-v2 (Zipf+power-law skews; B-1 Phase 2 主基準)
python3 scripts/tools/dx/generate_tenant_fixture.py \
    --layout synthetic-v2 --count 1000 --with-defaults \
    --output tests/e2e-bench/fixture/synthetic-v2/conf.d \
    --seed 42

# Synthetic-v1 (uniform; PR #59 baseline reuse, 不變)
python3 scripts/tools/dx/generate_tenant_fixture.py \
    --layout hierarchical --count 1000 --with-defaults \
    --output tests/e2e-bench/fixture/synthetic-v1/conf.d \
    --seed 42

# Customer-anon (manual; 不在 generator 範圍 — 客戶 sample 解壓後直接放)
```

### 重跑本 baseline 指令

```bash
# In Dev Container:
cd /workspaces/vibe-k8s-lab/components/threshold-exporter/app
BENCH_OUT_DIR=/tmp/b1_out bash /workspaces/vibe-k8s-lab/scripts/tools/ops/bench_wrapper.sh \
  -run='^$' \
  -bench='BenchmarkFullDirLoad_Hierarchical_1000|BenchmarkDiffAndReload_Hierarchical_1000_NoChange|BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged|BenchmarkScanDirHierarchical_1000|BenchmarkBlastRadius_DefaultsChange_Hierarchical_1000' \
  -benchmem -count=3 -timeout=15m .
cat /tmp/b1_out/bench.out.txt
```

---

> 本文件從 [Testing Playbook](testing-playbook.md) § Performance Benchmark 獨立拆分（v2.0.0-preview.4）。
