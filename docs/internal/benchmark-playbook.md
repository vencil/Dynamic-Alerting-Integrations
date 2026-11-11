---
title: "Benchmark 操作手冊 (Benchmark Playbook)"
tags: [documentation, performance]
audience: [platform-engineer, sre]
version: v2.9.0
verified-at-version: v2.9.0
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
| **PR-time bench gate 看懂 + 處理 regression** | [§Tier 1 PR-Time Bench Gate Operations](#tier-1-pr-time-bench-gate-operations) |
| **Regression 處理（informational）** | [§Regression 處理（Phase 2 — informational，不擋 merge）](#regression-處理phase-2--informational不擋-merge) |
| **Release-time cumulative drift report (Tier 2)** | [§Tier 2 Release-Time Bench Gate Operations](#tier-2-release-time-bench-gate-operations) |
| **Reload 下記憶體 creep / GOMEMLIMIT / FreeOSMemory lever** | [§Memory characteristics under reload pressure](#memory-characteristics-under-reload-pressure-459) |
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

- idle 狀態下 notification latency histogram 為空（無 alert 觸發）→ 需 `--under-load` 或 `make load-composite TENANT=<tenant>` 產生流量才有數據（`TENANT` 須為 namespace 名稱等於 tenant 的 MariaDB tenant；結束後 `make load-cleanup` 刪除 `db-*` namespace 內的壓測 Job／Pod）
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

## Tier 1 PR-Time Bench Gate Operations

> **Source of truth**：`.github/workflows/bench-gate-pr.yaml`（PR-time）+ `.github/workflows/bench-record.yaml`（nightly baseline + 配對量測，summary only）。本節是 operational 對應文件 — SRE 防線 codification 見 user memory `feedback_github_actions_workflow_gotchas.md`。
>
> **Phase 2（informational）**：bench gate **不再阻擋 merge**。真 regression → 自動貼 PR comment + 上 `perf-regression` label（清掉後自動移除）；INCONCLUSIVE → 只標註。`override: bench-regress-ok` label + `bench-override-audit.yaml`（角色驗證 + stale-label strip）**已移除**——沒有 gate 要 override。main 的累積漂移目前沒有逐夜的自動偵測（發版時的 Tier 2 比對（`bench-gate-release.yaml`）仍會把相對前一個平台版本 ≥10% 的退化寫進報告）：舊的 nightly trend watchdog 已退役（見 [§Nightly sustained-trend watchdog（已退役）](#nightly-sustained-trend-watchdog已退役)）。

### 概觀

| 項目 | 值 |
|---|---|
| 工作流檔案 | `.github/workflows/bench-gate-pr.yaml` |
| 觸發條件 | `pull_request: [opened, synchronize, reopened]` + path filter |
| Path filter | `components/threshold-exporter/app/**` / `rule-packs/**` / 此 workflow 自己 |
| 拓樸 | **單 runner INTERLEAVED + control canary**（v6）：兩個 worktree（base + pr 同時 checkout）→ `go test -c` 預編譯 → 迴圈交替跑 base,pr,base,pr,…（含 canary）→ benchstat。harness = `scripts/tools/ops/bench_interleave.sh` |
| Wall time | **~20–25 min**（與 v5 count=6 同總 benchtime；多 process 啟動由「只編譯一次」抵銷）|
| 統計判定 | `benchstat -alpha=0.01` AND `|Δ| ≥ 5%` 雙閾值 |
| 指標分級（[#3468](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3468)） | 判定集中在 `scripts/tools/dx/bench_verdict.py`，依 benchstat 的單位區段判讀被標的列。`B/op`／`allocs/op` 是確定性指標（計的是工作量，兩側跑同一份輸入時逐次相同），被標就成立。**只有 `sec/op` 被標**時，`bench_gate_compare.sh` 再跑一輪獨立的交錯量測，兩輪都標到的 bench 才成立；沒重現的列只在 summary 列出（`unreplicated`），不留言、不貼 label、不開票。第二輪 canary 不穩 → INCONCLUSIVE。依據：#2324、#2628、#2806、#3401 都只有 sec/op 被標，重量後確定性指標完全相同；但 #3262 是 sec/op 單獨退化而且是真的，所以要重現，不能直接丟掉 |
| Metric scope | **確定性 per-op metric only**（`sec/op` / `B/op` / `allocs/op`）via `benchstat -filter`；排除非確定性 process-level metric（`MB-sys` / `MB-heap-after-gc` / `goroutines`）—— GC/runtime high-water 雜訊，非 per-op work（[#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)）|
| Control canary | `benchcanary` module（`scripts/tools/ops/bench-canary/`，自帶 go.mod、stdlib-only、stash 到 checkout 外、base/pr 共用同一份）。`BenchmarkControlCanaryCPU` = **gating**（base↔pr 顯著漂移 ≥4% → 判 INCONCLUSIVE）；`BenchmarkControlCanarySleep` = **informational-only**（µs 級排程 jitter，gating 會狂閃）|
| INCONCLUSIVE 語意 | **runner 漂移 → re-run，非 regression**（綠燈 + warning）。canary 漂移 / 缺席 / 異質 CPU 三者任一觸發 |
| Merge 阻擋 | **無（Phase 2 informational）**：真 regression → PR comment + `perf-regression` label（不擋 merge、清掉後自動移除）。fork PR token read-only → 降級成 `::warning::` + step summary（不用 `pull_request_target`）|
| Override | **已移除**（沒有 gate 要 override；`override: bench-regress-ok` + `bench-override-audit.yaml` 同步刪除）|

> **設計演進**：v1 (parallel 2 runners) → v2 (sharded 6 runners) → v5 (single-runner **sequential**) → v6 (single-runner **interleaved + canary**)。v2 因 GH-hosted runner pool 有 3+ CPU 類型、6-runner 全同構機率僅 ~1.7% → ~98% INCONCLUSIVE 退役。v5「同 VM 先跑完 base 再跑完 pr」解決 CPU 異質性，但引入更隱蔽的殺手：base/pr 分兩個時間塊跑，runner 中途漂移（thermal/頻率/鄰居）系統性偏置 pr 那塊，benchstat 的 independent-samples 假設被破壞 → 時間相關 bias 被誤讀成 regression（[#502](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/502)/[#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)/[#611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/611)/[#695](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/695)，#695 零 Go 改動照樣紅）。v6 交錯跑使漂移兩邊抵銷 + canary 偵測殘餘漂移判 INCONCLUSIVE，根治 false-RED。詳：memory `feedback_github_actions_workflow_gotchas.md`。

### 觸發條件深掘

只有 PR 動到下列 path 才會跑（doc-only PR 自動 skip 整個 gate）：

- `components/threshold-exporter/app/**` — exporter Go 程式碼
- `rule-packs/**` — rule pack YAML
- `.github/workflows/bench-gate-pr.yaml` — 工作流自己（用於 dogfood self-test）

（Phase 2 移除了 `labeled` trigger 與 override 機制——gate 不再阻擋，沒有 label 需要被 honor。）

### 為什麼 v6 沒有 drop_caches（v5 有，且其實有害）

v5 在 base / pr 兩塊之間做 `drop_caches=3` 緩解 cache-warmth 偏差。**v6 刻意拿掉**：在交錯迴圈裡，單一 drop_caches 會變**不對稱**——base 永遠帶著上一輪 pr 留下的暖快取、pr 永遠冷快取——反而系統性偏置 pr 變慢，正是要消滅的那類 bias。交錯本身已讓兩側對稱暖快取，不需要、也不能放 drop_caches。

> **為什麼預編譯（`go test -c`）**：`go test -bench` 每次即時編譯，Go 編譯器吃 CPU 會在每輪開頭製造 thermal spike，量到的是「降頻殘局」。v6 在迴圈前用 `go test -c` 把每側兩個 package（`app` = package main、`app/pkg/config`）+ canary 各編一次成 `.test` binary，迴圈內只純執行 → 量測點之間無編譯熱負載。

### 看懂 step summary

成功時：

```
## ✅ Bench Gate — no statistically significant regression
Compared PR HEAD (<sha>) against merge-base (<sha>).
Gate threshold: p < 0.01 AND |Δ| ≥ 5% (... + magnitude floor).
Metric scope: deterministic per-op metrics only (sec/op, B/op, allocs/op) via benchstat -filter (#608).
Topology: single-runner INTERLEAVED (base,pr,base,pr,… pre-compiled, no drop_caches) + control canary.

### Full benchstat output
< benchstat 2-column comparison，含 BenchmarkControlCanaryCPU/Sleep 兩列 >
```

失敗時：

```
## ❌ Bench Gate — REGRESSION DETECTED
... 同上 ...
### Regressions flagged
< 通過雙閾值的 benchmark 行（canary 已被 grep -v ControlCanary 排除）>
```

INCONCLUSIVE 時（綠燈、非 regression）：

```
## ⚠️ Bench Gate — INCONCLUSIVE (runner drift, re-run)
<reason: CPU canary 漂移 ≥4% / canary 缺席 / 異質 CPU>
This run is not counted as a regression — push again or re-run the job.
```

> **regression 與 INCONCLUSIVE 的關係**：真 regression = 主 bench ≥5% 顯著退化 **且** CPU canary 穩定。若 canary 同時漂移 → INCONCLUSIVE 優先（這次 run 的數據本來就不可信，不該 gate）。

### Regression 偵測到怎麼辦

**Decision tree**：

1. **檢查 benchstat 輸出**，flagged regression 是哪些 benchmark？
2. **這是真的 regression 嗎？**
   - **是真的** — 你的 diff 引入了 perf 問題：fix code（O(n²) 改 O(n log n)、減少 allocations、加 cache 等）後重 push
   - **看起來像 noise** — 只有 `sec/op` 的旗標已經由 gate 自己重現過一次（見上表「指標分級」），還留著的是兩輪都標到的。仍懷疑時：同 benchmark 在 nightly `bench-record.yaml` 沒這個趨勢？到 GitHub Actions UI 的 Checks panel 右上點 **"Re-run failed jobs"** 重跑。GH-hosted runner ~10% FP rate per design（hardware floor）。**不要**用 `git commit --allow-empty` 或 close+reopen PR 重跑 — 那會污染 git history + 觸發無關的 webhook noise（Slack / Jira / labels）
   - **是 deliberate trade-off**（正確性修補 / 安全 patch / 演算法本質約束）→ 在**同一個 PR** 於 `.github/bench-accepted.toml` 新增一筆紀錄（benchmark 完整名稱、指標、上限、理由；見 [ADR-039](../adr/039-bench-acceptance-ledger.md)）。實測在上限內時 gate 判為「已接受」，不貼 label，合併後也不開票；超出上限仍是退化。只寫在 PR 說明裡的取捨，合併後的歸因讀不到，會照樣開票

3. **重跑後仍 flag** → 多半是真的 regression；不擋 merge，但請當作待辦處理

### Regression 處理（Phase 2 — informational，不擋 merge）

Phase 2 起 PR bench gate **不阻擋 merge**。真 regression（主 bench ≥5% 顯著退化 **且** CPU canary 穩定）時 workflow 自動：

1. 貼一則 PR comment（含 benchstat regression 行）。
2. 上 `perf-regression` label。
3. job 仍 **exit 0**（綠燈）——merge 不被擋。

regression 清掉後（下次 push 不再 flag）label 自動移除。**沒有 override 流程**——沒有 gate 要 override。若是 deliberate trade-off，在同一個 PR 寫進 `.github/bench-accepted.toml`（[ADR-039](../adr/039-bench-acceptance-ledger.md)），label 會因為沒有未涵蓋的退化而清掉；若需回收 perf，留著 label 當追蹤。

> **Fork PR 限制**：`pull_request` event 對 fork 給 read-only token，comment/label 會失敗 → 降級成 `::warning::` + step summary（**不**用 `pull_request_target`，避免 pwn-request 洩 token）。

⚠️ 逐 commit 都低於 5%、累積起來很大的漂移，目前**沒有逐夜的自動偵測**；只有發版時的 Tier 2 比對（`bench-gate-release.yaml`）仍會把相對前一個平台版本 ≥10% 的退化寫進報告。舊的絕對 ns/op 趨勢監測已退役（見下方「Nightly sustained-trend watchdog（已退役）」）；接手的是確定性指標（`B/op`、`allocs/op`）的累積預算，設計待資料累積後定案（[ADR-039 §尚未決定](../adr/039-bench-acceptance-ledger.md)）。

### 夜跑成對交錯量測（[ADR-032](../adr/032-paired-interleaved-bench-measurement.md) 實作第一段）

`bench-record.yaml` 的第一個 job 現在**每夜在同一台 runner、同一個 job 內交錯量測 main HEAD 與一個固定參考版本**，產出 per-bench 比值。機器速度在比值裡相消，所以不需要知道今晚拿到什麼機器。

⚠️ **本段只量測，不判定。** 比值寫進 artifact（`bench-paired.json`）；配對判定只寫 step summary（見下方「配對判斷引擎」），不開票。`bench-paired/v3` 起另記 `B/op`、`allocs/op` 的兩側中位數與比值（`deterministic` 欄位），供累積預算定案用。

- **參考版本釘在 `.github/bench-reference.yaml`，不自動跟隨最新 tag。** ⛔ 換這個值 = 一次**吸收事件**：換版當下 main 上有什麼、新參考點就吸收什麼，包含一個已開票但決定暫不修的退化 ⇒ 之後比值回 1.00 ⇒ 監測器讀成「不再超標」（ADR-032 §待決 1）。自動跟隨會讓這件事在任何人打 tag 的瞬間悄悄發生，所以改它需要一個 PR——**這個摩擦是刻意的**。
- **綁 `exporter/v*` 而非平台 `v*` 線。** 夜跑量的是 `components/threshold-exporter/app`；平台線會為了與 exporter 無關的發布多做一次吸收（`v2.8.1` 就是平台 tag only）。換版次數越少，吸收事件越少。
- **參考版本建置失敗 → fail-loud 但不 fail-job。** 配對序列當夜記 `INCONCLUSIVE` + `::error::` annotation；`bench-baseline.txt` 走 fallback 路徑（等同 ADR-032 之前的行為）照常產出，因為它是 `release-attach-bench-baseline.yaml` 的 release 資產來源。⛔ **絕不降級為 warning**——「量不到」與「量了沒事」必須在資料上可區分，這正是本 ADR 要消滅的失效模式。
- **新測試沒有分母是預期狀態，不是錯誤。** 參考版本裡不存在的 bench 由 `pair_bench_ratio.py` 記為 `missing-in-reference` 並放進 `inconclusive`，不會被算成乾淨。
- **成本 2× 而非 3×。** 交錯量測的 main 側**直接當** `bench-baseline.txt`：樣本數（6 輪 × `count=1` vs 現行 `count=6`）、benchtime、bench 集合都相同，且 `bench_interleave.sh` 把單一 canary binary 跑進兩側故 canary 列本就在內。release 資產無需改動。
- 交錯參數 `ROUNDS=6 BENCHTIME=3s`；job timeout 30 → 45 分鐘。⏱️ **已實測 1.92×**（2026-08-16 run 31924848480：1249s vs 前一夜 649s），與 ADR-032 §待決 4「翻倍」的估計相符；舊的 30 分其實夠用，45 是餘裕。

### 工作定義效應歸因（on-demand triage）

夜跑的比值消掉了機器，**沒有消掉工作定義**：參考版本一釘就是數週到數月，而 main 的 benchmark fixture 會繼續被改，改動造成的是**永久性階梯**，形狀和真退化一模一樣（ADR-032 §工作定義漂移）。`bench-paired.json` 的 `workload_drift` 是那個揭露——但**揭露不是答案**：實測三夜（2026-08-16/17/18）該清單兩夜逐字相同的四行，四個 `*bench_test.go` **全部**漂移 ⇒ 映射到 20 支夜跑 benchmark 就是 20/20，而同期只有一支有持續階梯（`BenchmarkMergePartialConfigs_1000`，+7.11 / +8.75 / +7.37%，跨三種 CPU）。**清單指向所有人，等於沒有指向任何人。**

**PR-A 的處置（ADR-032 §工作定義漂移 修訂）**：範圍從 4 支 `*bench_test.go` 補齊為 8 檔閉包（SSOT 在 [`.github/bench-reference.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.github/bench-reference.yaml) 的 `workload_closure`），並加 `workload_digest`——每側一個純量，內容改／新增／刪除／改名都會動。counterfactual（`3fd96b51`..main 兩棵真實的樹）：舊範圍涵蓋 4 檔、`config_test.go` 出現 **0 次**；新範圍涵蓋 8 檔、出現 **1 次**，`cmp` 與 sha256 獨立確認兩側確實不同。⚠️ 新範圍**沒有**更飽和：8 檔中只有 6 檔漂移。⛔ 夜跑**不做跨夜比較**——它只記錄今晚的 digest，轉變由讀序列的人導出；在夜跑裡記昨夜的值就是凍結基準值原型死掉的那種跨次狀態。`bench-paired.json` schema 因此 `v1` → `v2`。

`.github/workflows/bench-workload-effect.yaml`（`workflow_dispatch`）不猜，直接量。同一台 runner、同一個 job 內交錯三棵樹的兩兩配對：

| | 組成 | 用途 |
|---|---|---|
| `R` | 參考實作 + 參考測試碼 | 夜跑今天的分母 |
| `W` | 參考實作 + **main** 測試碼（overlay 造出來） | 中介 |
| `M` | main 實作 + main 測試碼 | 夜跑今天的分子 |

- **`W / R` = 工作定義效應的估計**（兩側同一份實作，只有測試碼不同）
- **`M / W` = 實作效應的估計**（兩側同一份測試碼，只有實作不同）
- **`M / R` = 夜跑發布的那個混淆值 = (W/R) × (M/W)**

⛔ **這兩個是估計，不是已證明的因果分解**，各自帶著一個本 workflow 只能揭露、無法證明的前提：`W/R` 要讀成**純**工作定義效應，前提是 overlay 集合涵蓋了所有影響工作定義的測試檔——而 helper 集合是**手工列舉**；`M/W` 要讀成**純**實作效應，前提是 `W` 與 `M` 的測試樹等價、**含 helper 閉包與執行期初始化**——而封閉性探測只到**編譯期**。因此本節與 workflow 一律不寫「純」與「免疫」：那是無條件說法，這裡的前提是有條件的。

判讀：某支 bench 的 `W/R` 若也在 +7~9%，那 +7~9% 是 fixture 改動不是退化；`W/R ≈ 0` 而 `M/W` 仍是 +7~9% 就是真的。

```bash
gh workflow run bench-workload-effect.yaml --repo vencil/Dynamic-Alerting-Integrations -f bench_re='MergePartialConfigs_1000'
```

⚠️ **`bench_re` 的成本會咬人，而且咬人的輸入長得很正常。** Go 的 `-bench` 是**未錨定的 RE2**：`.` 或 `...` 會匹配**全部 41 支**。2026-08-19 有一次 dispatch 就是填了 `...`，跑滿當時的 60 分上限後 timeout，只留下半套結果（`W/R` 有、`M/W` 沒有）。

因此 job 開頭有一道**成本閘門**：用 `go test -list` **問工具鏈**實際選中幾支（不用 shell regex 猜——那與 Go 的 RE2 不是同一套），印出預估耗時，並在超出預算時擋下。成本模型是兩個實測點且互相吻合（**約 1 分鐘/支/次交錯**：41 支 → 42.2 分/次、2 支 → 2.4 分/次）；`timeout-minutes` 依此定為 **120**（全選 × 2 次交錯 ≈ 84 分）。

- **空的 `bench_re` 會被擋下**（rc=1）——留空原本會落到 `bench_interleave.sh` 的預設集合，那是「安靜地跑了你沒要求的東西」。
- **選不到任何 benchmark 也擋下**——兩側都只剩 canary，比值毫無意義。
- **`go test -list` 自己失敗（樹編不過、或 regex 語法非法）會被單獨報成「建置或 regex 語法問題」並附原文**，不會被收斂成「選不到 benchmark」。⛔ 這是自審時實測補的：初版把兩者都變成 `sel=0`，於是一個編不過的樹會把人送去改 regex，而真正的問題在別處——fail-closed 沒錯，但**診斷指向錯的地方**。
- ⛔ **目前真正在發揮作用的是「第一分鐘印出預估 + warning」**：實測最多 41 支 ⇒ 預估上限 84 分 < 預算 100 分，所以那條硬擋**現在打不到**，它是給 benchmark 數量成長之後用的後備。（寫出來是因為「宣稱有一道擋著、實際上打不到」比沒有那道更糟。）

⛔ **讀數字之前先做兩個對照**：(1) `bench_interleave.sh` 把**同一個** canary 執行檔跑進兩側，所以 `BenchmarkControlCanary*` 的真值恆為 1.000——不接近 0% 就是這次執行本身髒了；(2) **只在參考是夜跑的釘死參考時**：`(W/R) × (M/W)` 應該對得上當夜 `bench-paired.json` 的比值（其他參考下沒有這道對照，只剩 canary；見下方「參考版本與夜跑解耦」）（要扣掉「夜跑的 M 是那一夜的 HEAD、本 job 的 M 是現在」這個合法差異）。**一個沒有對照的量測不是量測。**

⚠️ **overlay 的已知弱點與它的守衛。** `*bench_test.go` 由 `find` **推導**（新增／改名／刪除自動跟上），但 helper 檔是一份**列舉**（`overlay_helpers` 輸入），而列舉錯了的失效模式本來是**靜默的**：漏掉的 helper 會綁到參考版本的版本，`W` 就變成混血樹而畫面上看不出來。

守衛是**封閉性探測**——造一棵「只留 overlay 集合」的樹去 `go test -c -gcflags=-e`，**問 Go 工具鏈而不是用 regex 猜誰引用了誰**：

- 編得過 ⇒ overlay 集合達成**編譯期封閉**（compile closure），即沒有未滿足的靜態相依。⚠️ **這不等於執行期隔離**：residue 的測試檔仍會被編進 `W` 的測試執行檔、package 層級初始化照常執行（本 repo 實測 residue 內有 `promParser` / `aliasTestNow` / `fileHeaderRe` 等 package 層級 var），而本探測**沒有**檢查那些初始化的副作用。所以正確的讀法是「residue 與比值無關**未經證明**」，不是「已證明無關」；
- 編不過 ⇒ **編譯器自己點名**缺哪些符號，報告再用那個名字反查定義檔、並標出它在不在 residue 裡（＝兩側是否有差異）。

`-gcflags=-e` 是必要的：Go 預設印 10 個錯就 `too many errors` 截斷，實測 `SV` 一支就吃掉 10 行、把 `SVScheduled` 整個藏掉——**一份被截斷的清單長得跟一份完整的清單一模一樣**。

`overlay_helpers` 的預設值是**實測迭代出的閉包**（2026-08-18，`3fd96b51`..main）：4 支 `*bench_test.go` ＋ `config_test.go` / `config_debounce_test.go` / `config_metrics_test.go` / `watchloop_test.go`，共 8 個檔，第 4 輪達到不動點。起點是 `config_bench_test.go` 用到 `config_test.go` 的 `SV` / `SVScheduled`（fixture 值建構子，影響 8 支夜跑 bench，而它**不是** `*bench_test.go`，所以夜跑的 drift 清單看不到它）。⚠️ 範圍不能無限外推：覆蓋兩個 package 的**全部** `*_test.go` 會因 `ExpiryMeta` / `canonicalKeyFor` / `ValidateTenantKeys().Errors` 等參考版本沒有的產品 API 而編譯失敗——所以「全都蓋、絕不遺漏」這個安全方向**不可用**。

⛔ **參考版本與夜跑解耦（[#2348](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2348)）。** 這支 workflow 是診斷工具，不寫任何跨次狀態，所以它的參考版本不必等「吸收事件」：

- **`reference_sha` 留空（或填入釘死的那個 SHA）**：用夜跑釘死的參考（`.github/bench-reference.yaml`）。只有這個選擇下，`(W/R)×(M/W)` 才能拿去對當夜的 `bench-paired.json`；其他參考下 summary 會把那道對照拿掉，只剩 canary。
- **`reference_sha` 填一個 SHA**：必須是 40 碼 hex、而且是 main 已包含的 commit（compare API 回 `ahead`／`identical`），否則 exit 1、不 checkout。summary 會標明「不是夜跑參考」，`M/R` 那一行不能拿去對夜跑。拆一個階梯時，通常填階梯前一個 commit。
- **PR self-test**（只對目標為 main 的 PR）：參考是 checkout 出來的 merge commit 的第一個 parent，也就是這支 PR 合上去的那個 main tip，所以 R 與 W 的測試碼只差這支 PR 自己的改動。⛔ 不用 merge base：分岔得早的 PR 會把今天的 benchmark 疊到舊樹上而紅，與 PR 無關。另外對釘死的參考跑一次同樣的編譯探測，結果只以 notice 呈現（能編／不能編／go 失敗但沒點名檔案＝無法判定），不讓 job 失敗——否則自測的綠會被讀成「預設的 dispatch 也能用」。

為什麼不等下一個 exporter tag：overlay 能編過的前提是 main 的 benchmark 碼沒用到參考版本沒有的 API，而這個前提幾週內就會失效。2026-11-10 實測（main 的 `*bench_test.go` 加 4 支 helper 疊上去 `go test -c -gcflags=-e`）：疊到 `exporter/v2.9.0`（`3fd96b51`）47 個錯、疊到 2026-10-15 的 main 11 個、疊到 2026-11-07 的 main 0 個；而 exporter 線上一個 tag 是 2026-06 打的。

⚠️ **拆不開的情形**：2026-09-28 起改到 benchmark 閉包的 11 支 PR 裡有 10 支同時改了產品碼——benchmark 是跟著 API 重構一起改的。跨過這種 PR，任何參考版本都編不過，工作定義效應與實作效應**本質上拆不開**；這時改用逐 PR 的量測：只改 benchmark 的 PR 由 `bench-workload-record.yaml` 當下量 W/R（下一節），其餘由 `bench-gate-pr.yaml` 量那支 PR 的合併效應，並把它記為「耦合、無法分解」。

- 編不過時，錯誤訊息依**編譯器點名的檔案**分流：落在任一 `*bench_test.go` ⇒ 縮小 helper 無效，換一個較新的 `reference_sha`；只落在 helper／其他檔 ⇒ 才提示縮小 `overlay_helpers`。編譯帶 `-gcflags=-e`，避免 10 個錯就截斷而漏掉後面 helper 檔的錯。
- 夜跑 `bench-record.yaml` 的 `M/R` 不做 overlay，兩側各編自己的樹，不受這個編譯問題影響；夜跑參考的政策（換它是吸收事件、不得關閉未結案的效能票）不變。

residue 完整清單留在 artifact 供稽核，但**不整份貼進 step summary**：首次 self-test 實測 44 個檔、其中真正被 overlay 過的檔引用到的只有 1 個（精確度 2.3%，比它要修的 `workload_drift` 清單 1/20 還差）。summary 印的是封閉性判定，因為那才是「residue 重不重要」的答案。

⛔ **這是診斷，不是判定**：不開票、不關票、不寫任何跨次執行狀態、也不改夜跑。ADR-032 §工作定義漂移 的決定仍然是「揭露、不介入」。

ℹ️ 另有一個只匹配「這支 workflow 自己」的 `pull_request` 觸發做 **self-test**（`bench-gate-pr.yaml` 同一 pattern）——因為 `workflow_dispatch` 的 workflow 必須先在 default branch 上才跑得動，沒有 self-test 的話第一次執行就會是正式判讀那一次。self-test 的參數刻意調到最小（1 支 bench／2 輪／100ms／不量 `M/W`），summary 會掛一條橫幅講明**那些比值不是量測結果**。

### 只改 benchmark 測試碼的 PR：當下量一次（[#1471](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1471) 候選 (a)）

上一節是**回溯**：階梯出現之後才用 overlay 去拆。`bench-gate-pr.yaml` 與 `bench-attrib-main.yaml` 都排除 `*_test.go`，所以只改 fixture 的 PR 原本沒人量。而「產品碼沒動、只有測試碼變」這個情境**本身就是 `W/R`**，`.github/workflows/bench-workload-record.yaml` 就在那一刻用既有的 `bench_gate_compare.sh`（merge-base vs PR head，參數同 `bench-gate-pr`）量它。

- **觸發**：`pull_request.paths` 是 `workload_closure` 的字面投影，兩邊一致由 `tests/ops/test_bench_workload_record_trigger.py` 釘住。
- **量不量由 `Classify diff` 現場判斷**：diff 裡有任何會觸發 `bench-gate-pr` 的檔（照它自己的 `paths` 求值）⇒ 跳過，那支 PR 由 `bench-gate-pr` 量；沒有閉包成員 ⇒ 跳過；其餘 ⇒ 量。跳過原因寫進 step summary 與 artifact。
- ⛔ **只記錄、不判定**：不留言、不貼 label、不開票，數字再差 job 也不紅；只有量測腳本本身出錯（編譯失敗、benchstat 形狀漂移）才紅。這裡的 `+N%` 是工作定義的效果，不是退化。

### Nightly sustained-trend watchdog（已退役）

`bench-record.yaml` 原本的 `trend-watch` job（`analyze_bench_history.py --trend-watch`）與它專用的完整性標記（`bench-baseline.rows`、`write_baseline_marker.py`）已移除，`perf-trend` label 不再有自動開關票的來源。

退役的理由是實績：它開過 9 張票，8 張被它自己在噪音裡反覆開關後自動關掉，1 張是誤判，沒有一張促成修正。絕對 ns/op 序列混了 runner 換機與時段噪音，分層、三態、帳本守衛修了七輪盲審仍收斂不了。

- **取代者**：確定性指標（`B/op`、`allocs/op`）的累積預算。同一份輸入下逐次相同，不需要噪音地板。資料由 `bench-paired/v3` 的 `deterministic` 欄位每夜累積，至少四週後定預算（[ADR-039 §尚未決定](../adr/039-bench-acceptance-ledger.md)）。在那之前，低於逐 commit 門檻的累積漂移**沒有逐夜的自動偵測**，只有發版時的 Tier 2 比對（`bench-gate-release.yaml`）仍會把相對前一個平台版本 ≥10% 的退化寫進報告。
- **配對 ns/op**（下節）維持 informational：只寫 step summary，不開票。[ADR-032](../adr/032-paired-interleaved-bench-measurement.md) 原訂的「並行後切換」協議（§待決 6）已撤回。
- **舊資料與分析**：[`audit-reports/bench-trend-2026-08/`](audit-reports/bench-trend-2026-08/README.md) 存檔保留；它的 harness import 的是已移除的程式碼，要重跑請 checkout 退役之前的 commit。
- `analyze_bench_history.py` 仍保留 GO/NO-GO 變異表模式（`make bench-history`），以及 `paired_trend_watch.py` 匯入的 `list_recent_runs` / `download_artifact`。

### 配對判斷引擎（ADR-032 第二段 PR-B1 — summary-only）

`paired-trend-watch` job 跑 [`paired_trend_watch.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/paired_trend_watch.py)，把**配對版判定**寫進 step summary。

⛔ **它不開票、不更新票、不關票。** 配對 ns/op 是 informational；ADR-032 原訂的「並行 2–4 週後切換」（§待決 6）已撤回，趨勢偵測改由確定性指標的累積預算接手（[ADR-039](../adr/039-bench-acceptance-ledger.md)）。

三層保證，由弱到強：模組註解 → `tests/dx/test_paired_trend_watch.py` 走 AST 檢查禁用名稱與網路 import → **job 的 token 不含 `issues: write`**。最後一層在前兩層都寫錯時依然成立，這也是唯一值得在並行期依賴的一層。

| 判定 | 意思 |
|---|---|
| `FINDINGS` | ≥1 支在**連續 2 個計數日曆夜**比值 > 5% |
| `CLEAR` | ⛔ **規則跑得起來**（≥1 支確實湊得出連續 2 夜的答案）**且無人達成持續判準**。⚠️ 不等於「沒有任何一夜超標」——單夜超標但沒連續的讀數會列在「Over the threshold, but not sustained」 |
| `INCONCLUSIVE` | 沒有夜被計數／沒有 bench 有分母／**沒有任何一支湊得出連續 2 夜** ⛔ **不是 CLEAR** |

⛔ 第三個 `INCONCLUSIVE` 條件是外部 review 抓到的缺陷，而它牴觸 ADR-032 §待決 6 的明文：
「比值序列從零開始時，持續判準需要幾夜才能發射——**那幾夜必須回報「無法評估」，不得回報「無發現」。**」
實測形狀：六夜、某支**每夜都 +42%**、對照測試隔夜擋一次使得沒有兩個計數夜相鄰 ⇒ 規則算術上不可能發射 ⇒ 原本回報 `CLEAR`，而那支的名字與 +42% **整份報告一個字都沒提**。
⚠️ **這個 job 自己的頭幾夜就會踩到**：它跑 `--from-gh --limit 14`，而配對管線上線前的夜會載成 `unreadable`，所以一開始只有 1 個計數夜。

**「日曆夜」不是「執行次數」。** `bench-record.yaml` 有 `workflow_dispatch`、夜跑也會被 re-run，所以同一個 UTC 日期兩次執行是常態。連續判準數的是**日曆夜**；同一夜的多次執行若對門檻**意見不一致**，該夜記為 undecidable（不投票、不取平均——平均出來的是沒有人量過的數字）。

**怎麼讀 step summary**

- **Nights 表**：`counted` / `not-counted (原因)` / `unreadable (原因)`。⛔ 沒有第四種「看起來還好」。
- **Work-definition transitions**：`workload_digest` 的第一個消費者。`UNKNOWN` 表示某一端的 digest 缺席（schema v1）／`not-requested`／`unreadable` ⛔ **不表示工作定義沒動**。`reference pin CHANGED` 是**吸收事件**（§待決 1），不是 fixture 編輯——跨越它的比值分屬不同基準，不可讀成同一條序列。
- **Benchmarks the rule could NOT judge**：湊不出連續 K 夜讀數的那些。⛔ 沒判 ≠ 乾淨。
- **Over the threshold, but not sustained**：超過門檻但沒連續的讀數。**不是** finding，列出來的理由是「報告從不提及的讀數，與從未發生的讀數無法區分」——六夜資料裡這一節會列出 #1497 那支的 +25.02%。
- **Counterfactuals**：門檻 5/3/2/1% × K=2/3、閘門 0.5/1/2%。§待決 5 要的 2–4 週實測分布由此累積，不需另寫收集程式。
  ⛔ **`not judgeable` 欄要先看。** `fires` 回空集合有兩種意思——「跑了沒發現」與「根本答不出來」。標 **n/a** 的列是後者，**不得讀成 0 fires**。判得動與否**隨門檻改變**（同夜多次執行跨在某個門檻兩側時該夜無解），所以逐列不同。實測過的誤讀形狀：某序列印「5%: 0、3%: 0、2%: 0、1%: 1」，讀者會得出「收緊到 3% 沒好處、要下到 2%」——而 3% 與 2% 其實一個答案都沒產生。
- **canary 兩欄**：`canary (gating)` 只有 CPU 那支且**帶正負號**（方向本身是診斷資訊：thermal throttle vs frequency boost）；閘門比的是絕對值。`canary (info)` 是 Sleep，不參與准駁。

**⚠️ 三件不要誤讀的事**

1. **閘門門檻 1.0% 是 provisional**，不是已決定值。而且它**幾乎攔不到東西**：六夜實測 canary 從未偏離超過 0.12%，同期卻有兩夜某支擺了 −19.34% / +25.02%。它回答「配對量測今晚有沒有壞掉」，**不是**「這支今晚穩不穩」。
   ⛔ **只有 `BenchmarkControlCanaryCPU` 參與閘門。** `BenchmarkControlCanarySleep` 是 informational——[`canary_test.go`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/bench-canary/canary_test.go) 明文寫「3-4% 漂移只有 ~30-40us，落在健康 GH runner 的抖動範圍內，**gating on it would flap INCONCLUSIVE constantly**；NOT part of the gate decision」，`analyze_bench_history.py` 的 `CANARY_BENCH` 與 `bench_gate_compare.sh` 的 presence fail-safe 也都只認 CPU 那支。
   ⚠️ 本工具第一版**兩支都當 gating**，實測後果：某支每夜 +9%、Sleep 在文件宣稱的健康值 3% 隔夜抖動 ⇒ 判定永遠停在 `INCONCLUSIVE`、一次都發射不了。**擋住偵測器與讓它說謊是同一種病。**
2. **被閘門擋下的夜對「連續 N 夜」是中斷還是跳過 —— ADR 沒有裁決。** 預設取中斷（不憑空製造連續性），另一種以反事實同時輸出。⛔ 封存的六夜**無法**裁決這題（那六夜從未被擋下），錨點測試通過不構成支持預設值的證據。
3. **已被接受的成本不會被抑制。** 第一張會發射的是 `MergePartialConfigs_1000`（六夜重播算出來的，不是預測），而它已由 [#1474](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1474) 量測、歸因、決定接受。`ACCEPTED` 帳本是 PR-B2 的範圍；在那之前**標註而不隱藏**——臨時的手寫排除清單正是靜音的原型。

⚠️ **`--gap skip` 與 `break` 必須兩個閘門一致。** 判定「湊不湊得出連續 K 夜」與判定「有沒有發射」讀的是同一個 `gap`；兩者不一致時，同一份報告會同時說某支「發射了」與「無法判定」（第三輪盲審實測）。

**一夜落差**：本次 run 自己的 artifact 在 job 執行當下還沒完成，所以今晚的數字落在明天的判定裡。

本機重播（不需網路、不需 `gh`）：`python3 scripts/tools/dx/paired_trend_watch.py --dataset docs/internal/audit-reports/bench-paired-2026-08`

### Troubleshooting

| 症狀 | 原因 | 處理 |
|---|---|---|
| `⚠️ INCONCLUSIVE (runner drift)` — CPU canary 漂移 ≥4% | 這次 run 的 runner 真的在漂移（thermal/頻率/鄰居）；交錯已盡力抵銷但殘餘超標 | **正常防線**，綠燈非紅燈：push 再一次或 "Re-run failed jobs" 拿乾淨比較。連續多次才需查 runner |
| `::warning::Control canary absent` | canary 沒 emit（harness / 編譯 / benchstat 格式變動）| 判 INCONCLUSIVE re-run；若持續，查 `bench_interleave.sh` 編譯 log 與 benchstat 版本 |
| `perf-regression` label 上了但想 merge | Phase 2 informational — label 不擋 merge | 直接 merge；deliberate 就在同一個 PR 寫進 `.github/bench-accepted.toml`，否則排進 perf 回收 |
| fork PR 沒收到 regression comment | fork token read-only（WAI）| 看 step summary（已降級寫在那）；maintainer 可手動轉述 |
| 整個 workflow 不跑 | path filter 沒匹配（doc-only PR） | 正常行為，不需處理 |
| Wall time > 25 min | runner load 高 / build cache cold / Go module download 慢 | 通常下次 PR build cache 暖了就回 ~20 min；持續異常查 runner metrics |

### Cross-references

- **30 夜真實序列 + counterfactual harness**：[`audit-reports/bench-trend-2026-08/`](audit-reports/bench-trend-2026-08/README.md) —— 已退役的絕對 ns/op 趨勢監測的證據存檔（每夜 CPU 型號 + 20 支 bench 的 median）。harness import 的是已移除的程式碼，只能在退役之前的 commit 上重跑。
- **Design rationale**（原 `bench-gate-rollout.md`，已隨內部 doc 清理移除）：Tier 1 + Tier 2 split、Scapegoat Trap / Noisy Neighbor Illusion / Broken Window / hardware floor escalation
- **Codified gotchas**：user memory `feedback_github_actions_workflow_gotchas.md` — 13 traps from 12 rounds of adversarial review (Virtual-merge HEAD / Labeled trigger / fetch-depth / fork-PR write / fabricated SHA / Partial shard / alpha vs confidence / Ghost Green race / Blanket Immunity / Redundant Wait + Ghost Comment + Zero-regression crash / hetero CPU silent FN / sharding-on-hetero-pool antipattern / sequential dirty workspace + timeout tightrope)
- **Issue tracker**：[#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433)

---

## Tier 2 Release-Time Bench Gate Operations

> **Source of truth**：`.github/workflows/bench-gate-release.yaml`（Tier 1 + Tier 2 split rationale）。

### 概觀

| 項目 | 值 |
|---|---|
| 工作流檔案 | `.github/workflows/bench-gate-release.yaml` |
| 觸發條件 | `release: published`（自動）+ `workflow_dispatch`（手動 pre-tag review） |
| Scope filter | 只跑 `v*` platform tags（exporter/v*, tools/v*, portal/v*, tenant-api/v* 不在 scope）|
| 拓樸 | 單 runner sequential（同 Tier 1 v5）：checkout prior tag → bench → drop_caches → clean workspace → checkout current tag → bench → benchstat |
| Wall time | ~20-25 min |
| 統計判定 | `benchstat -alpha=0.05` AND `|Δ| ≥ 10%`（looser than Tier 1's 0.01 / 5%）|
| Metric scope | **確定性 per-op metric only**（`sec/op` / `B/op` / `allocs/op`）via `benchstat -filter`（同 Tier 1，[#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)）—— 對 Tier 2 尤其重要：α=0.05／10% 較鬆，v2.9.0-vs-v2.8.0 首跑時 `MB-sys` GC-pacing creep（[#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459)／[#606](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/606)）會被誤判為 drift |
| 結果遞送 | **Step summary**（不 mutate release notes、不 post comment 到 release）+ **workflow failure on regression**（觸發 GitHub email 通知 maintainer）|
| Blocking? | **❌ 不阻擋 release**（release 已 published；workflow 在那之後跑）。但偵測到 drift 會故意 `exit 1` 標紅燈 — 這是 alerting 機制（GitHub 只在 workflow fail 時寄信通知 maintainer，綠燈無通知）|
| Override | 無 label 機制；maintainer 自行在 release notes 文字承認 drift 即可 |

### 「Fail to inform, not fail to block」設計

Tier 2 在偵測到 drift 時故意 `exit 1` 讓 workflow 顯紅燈。這**不是** release blocker（release 在這個 workflow 觸發前已經 published 了，事後失敗無法 un-publish），而是**通知機制**：

- ❌ Workflow exit 0（綠燈）→ GitHub 不寄任何通知 email → maintainer 永遠不會主動點開綠燈 workflow 讀 step summary → drift 報告等於沒寫
- ✅ Workflow exit 1（紅燈）→ GitHub 自動寄 "Workflow Failed" email 給 maintainer → maintainer 收信點進來看 step summary → 真正讀到 drift 報告

這個取捨叫 **Fail to inform, not fail to block**。在 GitHub Actions 的 UX model 下，沒有更好的 silent-pass alternative — 自動 post 到 release notes 會 clobber maintainer 手打的文字，自動發 issue 會 spam 兒 issue tracker。Workflow failure 是當下最 idiomatic 的「請看一下這個」訊號。

副作用：release 列表上會看到 release 旁邊一個紅 X check。**這不代表 release 有問題**，只代表「Tier 2 偵測到值得 review 的 drift」。Maintainer 看了 step summary 後可選擇：

1. **承認** — 在 release notes 補一段 perf trade-off rationale
2. **跟進** — 開 perf follow-up issue 給下個 release 處理
3. **嚴重時** — hotfix release（罕見；通常 Tier 1 已先 catch 過了）



| 維度 | Tier 1 (PR-time) | Tier 2 (release-time) |
|---|---|---|
| 目的 | catch per-PR regression（blame correct）| catch 累積 release drift（user-visible scale）|
| Trigger | PR events (opened / synchronize / reopened) | `release: published` |
| 比對基準 | merge-base | 前一個 `v*` release tag |
| 統計閾值 | α=0.01 + 5% floor | α=0.05 + 10% floor |
| Metric scope | sec/op · B/op · allocs/op（`-filter`，#608）| sec/op · B/op · allocs/op（`-filter`，#608）|
| 嚴格度 | 中（catch per-PR diff intent，informational）| 低（只抓 user-visible drift）|
| Blocking? | ❌ informational（Phase 2；comment + `perf-regression` label）| ❌ informational |
| Override | 無（不擋 merge，無需 override）| free-form release notes acknowledgment |
| Wall time | ~20-25 min | ~20-25 min |

### 為什麼 Tier 2 threshold 比 Tier 1 鬆

Tier 1 的職責是「每個 PR 不要偷偷塞 perf regression」— 嚴格的 α=0.01 + 5% 適合 catch 小但故意的 regression。

Tier 2 的職責是「告訴 maintainer release 之間有沒有累積 drift」— 跨多週 / 數十個 PR，runner variance 累積、Go 版本變動、依賴升級 etc. 都會貢獻 noise。若用 Tier 1 等級閾值，幾乎每個 release 都會 flag 一堆 ±5% drift；雜訊掩蓋真正的 user-visible regression。10% magnitude floor 配 α=0.05 → 大概是「客戶實際會抱怨」的 scale。

### 第一個 release 行為

v2.8.0 是 Tier 2 上線後的第一個 release。`git describe` 找不到比它更早的 `v*` tag（除了 v2.7.0，但 v2.7.0 的 bench-baseline 資產 predates PR #117），workflow 會輸出：

```
## ℹ️ Tier 2 — No prior baseline to compare against
Current release: v2.8.0
No prior v* tag found in history. This is expected for the first
platform release after Tier 2 went live. Subsequent releases will
compare against this one.
```

第一個有意義的 Tier 2 比對是 v2.9.0 vs v2.8.0（兩邊都會 freshly re-bench 在同一個 runner）。

### Pre-tag manual review

Maintainer 想在 tag 前先看 drift：

```bash
gh workflow run bench-gate-release.yaml \
  --repo vencil/Dynamic-Alerting-Integrations \
  --ref main \
  -f prior_ref=v2.8.0 \
  -f current_ref=$(git rev-parse main)
```

workflow_dispatch 跑完後讀 step summary，決定要不要 tag。

### Cross-references (Tier 2 段)

- **設計**：見 Tier 1 段 Cross-references（原 `bench-gate-rollout.md` 已隨內部 doc 清理移除）
- **資產供應**：`.github/workflows/release-attach-bench-baseline.yaml`（PR #117）— 把 nightly bench-record artifact 附到每個 release 作為 trend record。**注意：Tier 2 不依賴此資產**（會 freshly re-bench prior tag），但資產可作獨立的 long-term 趨勢檢視工具
- **Nightly source**：`.github/workflows/bench-record.yaml` — 每天 03:00 UTC 跑，artifact 留 90 天

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
> 產出：`bench.out.txt`（僅 `goos:` / `goarch:` / `pkg:` / `cpu:` / `BenchmarkX ns/op` / `PASS` / `FAIL` 行）、`bench.err.log`（`go test` 的 fd 2，成功跑時為空——見下）、`bench.raw.jsonl`（`go test -json` 原 event stream 供 debug）。
>
> 底層實作：`go test -bench ... -json <args>` → `bench_filter.go` 解析 JSON event，只保留「benchmark result pattern」+ 「suite header/summary」。
>
> ⚠️ **`-json` 並不是把 stdout/stderr 分流**（本行原文如此敘述，是錯的）。`cmd/go` 把測試 binary 的 stdout **與 stderr** 一起餵給 test2json，所以 `log.Printf`（乃至直接寫 `os.Stderr`）的輸出會以 `{"Action":"output"}` event 出現在 **stdout 的 JSON 串流裡**，不會落到 fd 2。實測 go1.23.12：呼叫 `log.Println` 的 benchmark 產出 `bench.err.log` **0 bytes**、stdout 6 個對應 event。
>
> ⚠️ **編譯失敗的診斷落在哪裡取決於 Go 版本**：go1.24+ 的 `bench.err.log` 連編譯錯誤都是空的，診斷由 `bench_filter.go` 印到 stderr（[#1872](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1872)）。各版本的當前語意以 `bench_wrapper.sh` 檔頭為 SSOT，不在本檔複述。
>
> ⛔ 本行原文括號裡的「`bench_wrapper.sh` 的 exit code 1 就指向它」**是假的**，已刪：那個 `exit 1` 是 `set -euo pipefail` 下的死碼，從來沒有執行過（TRK-381）。exit code 與 err.log 的**當前**語意以 `scripts/tools/ops/bench_wrapper.sh` 檔頭為 SSOT，不在本檔複述。
>
> 消除 log 污染的機制不是「分流」，而是 `bench_filter.go` 依 event 形狀**篩選**：所有測試輸出都被包成 JSON event，過濾器只保留 benchmark 結果與 suite header/summary。`bench_filter.go` 檔頭「Why this exists」那一段對這件事的敘述一直是對的，可以當 SSOT（刻意不寫行號：沒有機制在守行號引用，`gofmt` 一次重排就會讓它指錯）。

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

`silenceLogs(b)` 要放在 setup `fullDirLoad()` **之前**（不只是 `b.ResetTimer()` 前），否則每次 benchmark invocation 的 setup phase 仍會產生 log。`b.Cleanup()` 確保 benchmark 結束後恢復正常 log 輸出。Wrapper + silenceLogs 雙層防禦：即使新 benchmark 作者忘了加 silenceLogs，`bench_filter.go` 也會把 log 事件擋在 `bench.out.txt` 之外。⚠️ **原文寫「wrapper 的 -json 分流也能把污染隔離到 `bench.err.log`」是假的**，與上面 §「`-json` 並不是把 stdout/stderr 分流」自相矛盾（同一份檔案相隔 25 行），已更正：實測 go1.24.7，`log.Println` 與直接寫 `os.Stderr` 兩者的輸出**都**進 `bench.raw.jsonl`（各自成為 `{"Action":"output"}` event），`bench.err.log` 是 **0 bytes**、`bench.out.txt` 0 筆。⇒ 防護是**篩選**不是分流；要看那些 log 請讀 `bench.raw.jsonl`。

### 連續多輪 benchmark port-forward 不穩定 (v2.0.0-preview.4)

**現象：** 用 for loop 連續跑 5 輪 `benchmark.sh --under-load`，第 3 輪起 Prometheus metrics 全部返回 0。

**根因：** benchmark.sh 的 cleanup trap 會 kill port-forward process，但下一輪啟動時 port-forward 可能尚未完全釋放埠號，或 Prometheus pod name 已變更（因上一輪的 ConfigMap patch 觸發 restart）。

**解法：** 每輪獨立執行（手動或間隔 30s），不要用 tight loop。或在 loop 間加入 `sleep 30` + `kill_port 9090` 確保埠號乾淨。

---

## v2.8.0 1000-Tenant Hierarchical Baseline (Phase 1, B-1)

> ⛔ **此 baseline 非 definitive SLO 承諾** ⛔
>
> 本節數字為 Phase 1 synthetic fixture 量測，**不能直接寫進客戶合約 SLA**。definitive SLO sign-off 必須等 Phase 2 customer anonymized sample 校準後重跑（DEC-B in v2.8.0-planning §10）。下游文件（pitch deck / proposal / 客戶 onboarding 文件）若引用本節數字，**必須**附帶「Phase 1 synthetic baseline」前綴。
>
> **Bench regression gate**:
> - Phase 1 (nightly informational) 已落地（`bench-record.yaml`）。
> - Phase 2 **redesigned** via issue [#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433) (supersedes closed #67) — split into **Tier 1** (PR-time, base-vs-PR, blame correctness) + **Tier 2** (release-time, release-vs-release, cumulative drift). Tier 1 已 ship 並啟用：`.github/workflows/bench-gate-pr.yaml`。
> - 完整 design + 5 個 SRE 防線 codification + hardware floor escalation：codified gotchas 見 memory `feedback_github_actions_workflow_gotchas.md`（原 `bench-gate-rollout.md` 已移除）。
> - PR-time operational flow（觸發條件 / step summary 解讀 / regression 處理 informational / 配對夜跑）：[§Tier 1 PR-Time Bench Gate Operations](#tier-1-pr-time-bench-gate-operations) below。

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
   - ⚠️ **#1577 後**：`IncrementalLoad()` 已移除，`BenchmarkIncrementalLoad_*` 改走 watch path——`_NoChange` 系列量一次 `tickOnce`（無變更時 production 只跑 `detectChange`，不 reload），`_OneFileChanged` 系列量一次 `diffAndReload`。`buildDirConfig` 的樹含 `_defaults.yaml`，所以 `_OneFileChanged` 量到的是**階層式 reload**，與 #1577 之前（走 `incrementalLoadFrom` 的 tenant-patch 快速路徑）**不可比**；快速路徑只有「整棵樹沒有任何 `_defaults` carrier」時才會被 watch path 走到，目前沒有 benchmark 覆蓋它

### Phase 2 延伸（blocked）

- **Alert fire-through e2e**：需 Prometheus + Alertmanager + receiver，目前只量內部 SLO
- **Customer sample 校準**：synthetic fixture 分布未必等同客戶 workload；Phase 2 帶客戶 anonymized sample re-run
- **SLO definitive sign-off**：B-2 hard SLO 需 3+ 輪 customer 環境驗證

---

## Memory characteristics under reload pressure (#459)

> **TL;DR**：sustained reload pressure 下 `go_memstats_sys_bytes` + `go_memstats_heap_idle_bytes` 會 high-water creep，但 `heap_objects` 持平 — 這是 **Go runtime GC pacing 行為，不是 code leak**。預設不需處理；abnormally high reload cadence 才動 lever。Customer sizing guidance 見 [`deployment-sizing.md`](../integration/deployment-sizing.md)。

### Soak 發現（v2.8.0 closure readiness, 2026-05-12）

1000-tenant production-config soak（60 min / 15s reload interval / default `GOGC=100`）：

| Metric | First | Last | Drift | Verdict |
|---|---|---|---|---|
| `go_goroutines` | 10 | 9 | −10.0% | ✅ 無 goroutine leak |
| `go_memstats_sys_bytes` | 35.0 MiB | 39.3 MiB | +12.4% | 🟡 creep |
| `go_memstats_heap_inuse_bytes` | 15.7 MiB | 13.5 MiB | −13.7% | ✅ |
| `go_memstats_heap_idle_bytes` | 11.5 MiB | 17.4 MiB | **+52.0%** | ❌ high-water creep |
| `go_memstats_heap_objects` | 192404 | 187351 | −2.6% | ✅ 無 live-object leak |

**Key insight**：`heap_objects` 持平 → 沒有 reference-held leak；`sys_bytes` + `heap_idle` 上漲 → runtime 在 reload churn 下持有 OS pages 高水位、GC 不積極 return-to-OS。對照 Run A（`GOGC=20` stress，同 60min/15s）：`sys_bytes +1.7%` / `heap_idle +0.1%` — aggressive GC 壓住 creep，**證明是 GC pacing，不是 leak**。

> ⚠️ **2-tenant fixture 會 mask 這個現象** — `.build/v2.8.0-soak-2tenant-archive/` 的 2-tenant run 看不到 creep。量 memory characteristics 必用 **production-shape（1000-tenant）fixture**，否則 working-set 太小、scan/parse churn 不足以觸發 runtime 的高水位保留。

### Mitigation levers（皆 opt-in，default off = 不改 runtime 行為）

| Lever | 機制 | 預設 | 何時動 |
|---|---|---|---|
| **`GOMEMLIMIT`**（env，Go 1.19+）| soft heap ceiling；逼近時 GC 變積極 + 提早 scavenge | unset | **首選**。Helm `exporter.goMemLimit`（建議起點 ≈ `resources.limits.memory` × 0.75）|
| **`-free-os-mem-after-reload`**（flag）| 每次 reload 後呼 `runtime/debug.FreeOSMemory()`（1 次 STW GC + 立即 scavenge）| off | GOMEMLIMIT 仍壓不住 **且** reload cadence 低到 per-reload GC 成本可忽略。Helm `exporter.freeOsMemAfterReload` |
| **`-reload-interval` 調高** | 給 GC 更多收斂時間 | 30s（**不改**）| 由 customer 依 config-change 頻率調，不改 chart default（blast radius 大、需 Track 1 數據佐證）|

> ⛔ **不重設 `GOGC` default** — `GOGC=100` 是 Go runtime convention + customer expectation；soak 用 `GOGC=20` 只是診斷 stress，不是建議值。

### 量測訊號

- `go_memstats_heap_released_bytes`（soak harness 已追蹤）— **直接的 return-to-OS 訊號**；GOMEMLIMIT / FreeOSMemory 生效時會上升。
- `da_config_free_os_memory_total`（exporter metric）— FreeOSMemory lever 啟用時每次 reload +1；預設 0（非 0 即代表 lever 已開）。

### 如何 reproduce / characterize（Track 1）

soak harness 已支援所有 sweep；exporter 端調 env/flag，harness 端只負責量測。harness 要擾動的檔取自 da-guard 對 conf.d 的索引，直接跑要先設 `DA_GUARD_BINARY`（或 da-guard 在 `$PATH`；`make soak-readiness` 會自己建）：

```bash
# 4-hour baseline（確認 sys_bytes 是 asymptotic 還是 linear unbounded）
python3 scripts/tools/dx/run_chaos_soak.py --target-url http://localhost:8080 \
    --config-dir <1000-tenant-conf.d> --duration-min 240 \
    --reload-interval-sec 15 --metrics-poll-sec 30 \
    --output-dir .build/soak-459/run-baseline-15s

# Reload-interval sweep（customer-realistic cadence 的 growth rate）
#   重跑上式，--reload-interval-sec 換 60 / 300 / 3600，output-dir 各自分開

# GOMEMLIMIT 實驗：exporter 端啟動帶 GOMEMLIMIT=50MiB 再跑同一條 harness 指令
#   GOMEMLIMIT=50MiB ./threshold-exporter -config-dir ... -reload-interval 15s
#   比 heap_released_bytes / sys_bytes 兩條曲線即知 lever 是否壓住 creep
```

比對用 `python3 scripts/tools/dx/render_soak_diff.py --input-dir <output-dir>`。

## v2.8.0 Phase 2 e2e Alert Fire-through (B-1 Phase 2)

> **Status**: 🟢 Implementation 完整 land + 首批 baseline 數字落地（synthetic-v2 1000-tenant + 5000-tenant，2026-04-26）。本節為 SSOT；design 文件已 archive。
>
> **archive-of**: `archive/design/phase-b-e2e-harness.md` (read-only historical reference for design rationale, decision log, rejected alternatives)
>
> **Implementation PRs**: #78 (gauges) → #79 (compose+driver) → #80 (aggregator+workflow) → #85/#86/#88/#90/#105 (cycle-1-6 RCA chain) → #112 (Tier 1 fail-fast)
>
> **Tier 1 fail-fast guard** is now active — any future bench harness regression that produces a zero T anchor in the warm_up run will abort the workflow within ~90s instead of waiting the full 60-min timeout. See §「Tier 1 fail-fast smoke gate」below.

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
| **B-1.P2-g** | playbook 完整章節（含實測數字 + customer-sample calibration flow） | 🟢 完整 land — ops flow + 首批 1000+5000 baseline 數字（2026-04-26 main workflow_dispatch runs 24951460457 + 24955478536）+ design doc archived | PR #80 (skeleton) + S#37 (numbers + design archive) |

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

**對應信號**（v2.8.0 Track A A8 起，driver SMOKE FAIL 輸出以 `fire.<anchor>` / `resolve.<anchor>` 前綴標示 phase；phase 名稱即運行時看到的 key）：

Fire phase（fixture 寫入 → alert dispatch，全 5 anchors 必須非 0）:
- `fire.T1=0`：exporter scan-complete gauge 從未 advance（multi-tenant 大 fixture cold-load 沒撐過 `wait_for_services` 的 60s window）
- `fire.T2=0`：reload gauge 從未 advance（content hash 同前次 → diffAndReload short-circuit）
- `fire.T3=0`：Prometheus alert `activeAt` 沒前進（label/expr mismatch、defaults 未載入、tenant 沒 register `bench_trigger`）
- `fire.T4=0`：webhook receiver 沒收到 fire POST（Alertmanager 路由斷或 alert 沒持續到 dispatch）

Resolve phase（fixture 不變、driver push 低值、alert 應 resolve；T1/T2 在 `stage_ab_skipped: True` 路徑跳過合理，只檢 T0/T3/T4）:
- `resolve.T0=0`：driver 沒記錄 resolve phase 起始時間（driver 內部 bug，少見）
- `resolve.T3=0`：Prometheus alert `activeAt` 沒被 evict / state 沒進 resolve（rule TTL 設太短或 `for: 0s` 與 expression 互動異常）
- `resolve.T4=0`：webhook receiver 沒收到 resolve POST（**Alertmanager `send_resolved: false` 是最常見原因**；其次是 `inhibit_rules` 把 resolve 也 inhibit 掉了）

**診斷工件**：workflow 失敗時自動把 `docker compose logs threshold-exporter / prometheus / alertmanager / receiver / driver` dump 到 `bench-results/*.log`，與 `per-run-0000.json` 一起 upload artifact。Exporter `WARN: skip unparseable file` 與 `config keys no row of this exporter reads`（列出沒有任何一列讀的鍵）兩條是最高訊息密度的線索。

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

### 首批 baseline 數字（synthetic-v2 main workflow_dispatch, 2026-04-26）

> **Source**: `gh workflow run bench-e2e-record.yaml` on `main`, `n=30` runs each (warm_up + 30 measured).
>
> | Fixture | Tenants | Run | n | fire P50 | fire P95 | fire P99 | resolve P50 | resolve P95 | resolve P99 | gate |
> |---|---|---|---|---|---|---|---|---|---|---|
> | synthetic-v2 | 1000 | [24951460457](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24951460457) | 30 | 4748.5 ms | 4953.95 ms | 4977.88 ms | 4766.0 ms | 4974.5 ms | 4985.39 ms | 🟡 pending |
> | synthetic-v2 | 5000 | [24955478536](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24955478536) | 30 | 4763.5 ms | 4971.55 ms | 4984.07 ms | 3769.5 ms | 3985.2 ms | 3996.71 ms | 🟡 pending |
> | customer-anon | TBD | (待 sample 抵達) | — | — | — | — | — | — | — | ⚠️ awaiting sample |
>
> Bootstrap 95% CI not too wide on either run (`ci_too_wide: false`).

**Stage breakdown** (fire phase P95):

| tenants | A (scan) | B (reload) | C (scrape+eval) | D (dispatch) | total ≈ |
|---|---|---|---|---|---|
| 1000 | 1440.5 ms | 1000.0 ms | 3305.0 ms | 3.0 ms | 4953.95 |
| 5000 | 3663.55 ms | 2000.0 ms | 0.0 ms | 3.0 ms | 4971.55 |

**Observations**:

1. **Near-flat e2e at P95 across 1000 → 5000 tenants** (+0.4% on fire P95, +0.4% on fire P99). The e2e latency is dominated by the 5s scrape quantization (Stage C in 1000-tenant; absorbed into A+B in 5000-tenant — see note 3), exactly the floor design §5.4 predicted.
2. **Stage A and B scale ~2.5× and 2× from 1000 → 5000** (scan walks more files; reload diff range is wider). Both scale within `ResolveAt` per-tenant linear cost expectation. The increase is hidden inside the scrape window.
3. **Stage C = 0 ms at 5000-tenant** is an artifact, not zero scrape time. With wider Stage A+B, the alert's `activeAt` (T3) sometimes lands BEFORE the post-reload scrape that produces T2 — Stage C = max(T3 − T2, 0). Real scrape latency is still ~5s but it's hidden in A+B; the e2e number (T4 − T0) is unaffected. Tier 2 follow-up could either redefine Stage C or document that Stage C → 0 means "T2 lagged T3" rather than "scrape was instant".
4. **Resolve faster than fire at 5000-tenant** (3985 vs 4971 ms P95) is real — resolve only has Stage C+D (Stage A+B skipped because no fixture mutation), so it bypasses the wide scan window.
5. **Gate status `pending` for both rows** is the design (synthetic-v2 → pending until customer-anon validation per design §6.5; the row will flip to `passed` / `voided` retroactively when customer-anon lands).

### 已完成的 design §9 acceptance follow-ups

| § | Item | Done in |
|---|---|---|
| §9.3 | 1000-tenant + 5000-tenant × 30 runs baseline | S#37 (this PR) — see §「首批 baseline 數字」above |
| §9.4 | Design doc archive | S#37 (this PR) — moved to `archive/design/phase-b-e2e-harness.md`; this section now SSOT |

剩餘 follow-up（不阻擋 v2.8.0 ship）：

- **Customer-anon sample 抵達後 calibration gate run** — 客戶 sample 還未抵達；待抵達後 maintainer 跑 `BASELINE_GLOB=... E2E_FIXTURE_KIND=customer-anon make bench-e2e`，把 customer-anon row 數字填入上表，並 retroactively 標 synthetic-v2 row `gate_status` 為 `passed` / `voided`。詳 §「Customer sample calibration gate operational flow」。
- **Stage C 渲染語意** — 5000-tenant Stage C=0 是觀測 artifact 不是真零；考慮 Tier 2 follow-up 改用 `max(T3-T2, T1-T0)` 或 explicit "absorbed-into-AB" 標記。

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
  -bench='BenchmarkFullDirLoad_Hierarchical_1000|BenchmarkDiffAndReload_Hierarchical_1000_NoChange|BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged|BenchmarkScanDirTree_Hierarchical_1000_Cold|BenchmarkBlastRadius_DefaultsChange_Hierarchical_1000' \
  -benchmem -count=3 -timeout=15m .
cat /tmp/b1_out/bench.out.txt
```

⚠️ `BenchmarkDiffAndReload_Hierarchical_{1000,2000,5000}_NoChange` 已固定在 Warm 模式（[#2048](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2048)）：載入前把共用 fixture 的 mtime 回溯到 `TreeScanMtimeGuard` 之外，所以單獨跑或窄 regex 下不再因執行順序與 `-benchtime` 在「全檔重讀」與「mtime fast-path」兩個模式間翻轉（改前 1000 單獨 1s 量到 42943 allocs/op，改後恆為約 33333）。PR gate 與 nightly 的完整 regex 下它本來就落在 Warm，數字不會出現斷層。冷路徑改看 `..._1000_NoChange_Reread`。

⚠️ **同名不同模式**：平面的 `BenchmarkIncrementalLoad_1000_NoChange` 與 `..._OneFileChanged` 固定在 **Reread** 模式（[#2344](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2344)），和上面固定在 Warm 的階層 `..._NoChange` 相反：載入前把 fixture mtime 推到一小時後，`TreeScanMtimeGuard` 永遠不放行 fast-path，每個 tick 全檔重讀。改前兩者每次都從 `b.TempDir()` 新建 fixture，迴圈越過 2s 後才轉進 fast-path，所以 `-benchtime 3s` 量到的是兩個模式的混合（NoChange 單獨跑 1s 為 16107 allocs/op、3s 為約 10.2k–10.7k）。平面的 Warm 模式由 `..._NoChange_MtimeGuard` 量。⛔ **nightly 會出現一次階梯，那是工作定義改變、不是退化**：PR gate（`bench_gate_compare.sh`，1s）改前就落在 Reread，沒有斷層；但 nightly（`bench-record.yaml` 與 `make benchmark-report`，3s）改前量到的是混合值，main 側從 #2344 起跳到 Reread，而參考版本側仍是混合值，這兩支的配對比值會一次性上升（當夜 `workload_drift` 會列出 `config_bench_test.go`，見 ADR-032 §工作定義漂移）。

---

## Engineering Reference Benchmarks

> v2.8.0 benchmarks.md customer-first 重寫時，以下「工程參考用」micro-bench 數據從 customer-facing perf doc 搬到本 playbook（per [PR #460](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/460)）。**Audience**：要做 perf regression analysis / 確認 specific 數字級距 的 maintainer + SRE，不適合給 prospective customer 看。

### Go Micro-Bench — `Resolve` 系列 (threshold-exporter config 解析)

`config_bench_test.go` 量測 threshold-exporter 設定解析效能（`go test -bench=. -benchmem -count=5`, Intel Core 7 240H）：

| Benchmark | ns/op (median) | B/op | allocs/op |
|---|---:|---:|---:|
| `Resolve_10Tenants_Scalar` | 19,590 | 26,488 | 61 |
| `Resolve_100Tenants_Scalar` | 163,839 | 202,777 | 520 |
| `Resolve_1000Tenants_Scalar` | 4,076,536 | 3,848,575 | 5,039 |
| `ResolveAt_10Tenants_Mixed` | 71,536 | 40,032 | 271 |
| `ResolveAt_100Tenants_Mixed` | 927,426 | 461,872 | 2,621 |
| `ResolveAt_1000Tenants_Mixed` | 10,274,749 | 5,244,817 | 26,054 |
| `ResolveAt_NightWindow_1000` | 8,438,156 | 5,220,583 | 25,055 |
| `ResolveSilentModes_1000` | 156,172 | 187,218 | 10 |

10→100→1000 租戶呈線性增長，1000 租戶完整 `ResolveAt`（含排程式閾值）在 ~10ms 以內。`ResolveSilentModes_1000` 僅 156µs，flag metric 查詢近乎零成本。

**與 Rule Evaluation 的關係**：[benchmarks.md §1.1](../benchmarks.md#11-平台規則為什麼與租戶數無關om) 量測 Prometheus 規則評估（O(M)，與租戶數無關），本表量測 threshold-exporter 設定解析（O(N)，線性增長）。兩者互補：最關鍵瓶頸（規則評估）恆定，次要成本（設定解析）1000 租戶仍僅 ~10ms，遠低於 15 秒抓取週期。

### Synthetic Fixture Generation 速率對照

`scripts/tools/dx/generate_tenant_fixture.py` — 合成 `conf.d/` 產生速度與輸出規模（seed=42）：

| Tenants | Layout | Files | Size (KB) | Gen Time (s) | Avg File Size (bytes) |
|---:|:---|---:|---:|---:|---:|
| 100 | flat | 101 | 71.4 | 0.045 | 724 |
| 100 | hierarchical | 107 | 73.1 | 0.055 | 699 |
| 500 | flat | 501 | 363.9 | 0.076 | 744 |
| 500 | hierarchical | 509 | 367.5 | 0.106 | 739 |
| 1,000 | flat | 1,001 | 723.9 | 0.116 | 741 |
| 1,000 | hierarchical | 1,009 | 727.2 | 0.133 | 738 |
| 2,000 | flat | 2,001 | 1,446.5 | 0.203 | 740 |
| 2,000 | hierarchical | 2,009 | 1,449.9 | 0.212 | 739 |

**觀察**：
- **線性擴展**：Gen time 與 tenant 數接近線性（100→2000 = 20× tenants, ~4.5× time），I/O 為主要瓶頸
- **Layout 差異微小**：hierarchical 多出 `mkdir -p` 開銷約 5-15%，可忽略
- **平均檔案大小穩定**：~740 bytes/file，不隨規模變化
- **Seed 可重現性已驗證**：同一 seed 兩次生成產出 byte-identical 輸出
- ⚠️ 上表量於 [#3489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3489) 之前：此後每棵樹都有一份根 `_defaults.yaml`，宣告每個產生的閾值鍵（`optional_overrides:`）與維度標籤（`dimensions:`），synthetic-v2 的 `extra_threshold_NN` 也會送出 row；表中 flat 的 `count + 1` 檔數已含根檔，加 `--with-defaults` 時檔數不變、大小比表中多，不加 `--with-defaults` 的產出則多一個根檔；exporter 的 series 數也比表中多，重量前不能直接拿來對照

Fixture 供 §v2.8.0 1000-Tenant Hierarchical Baseline (上方) 與 §Phase 2 e2e harness 使用。**ADR-016 引用**：[`docs/adr/016-conf-d-directory-hierarchy-mixed-mode.md`](../adr/016-conf-d-directory-hierarchy-mixed-mode.md) flat vs hierarchical 效能對照即此表。

### Schema Validation — `validate_tenant_keys`（已移除）

這支 Python 鍵名驗證已刪除（[ADR-038](../adr/038-unread-keys-judged-by-readers.md) 決定 11）：一個 key 有沒有讀取端讀改由共用的鍵判決判，`version` label 與改名提示改由 da-guard served-values 的 `key_checks` 給。舊數據（1000 tenant 約 1 ms）不再對應任何現行程式，不再列出。

### pytest-benchmark 微觀基線

`pytest -m benchmark`（min_rounds=20, warmup=on）。用於版本間趨勢偵測：

| 測試 | Median | Rounds | 說明 |
|---|---:|---:|---|
| `test_parse_integer` | ~102 ns | 100,161 | `parse_duration_seconds` 最快路徑 |
| `test_parse_seconds` | ~634 ns | 164,555 | 含字串解析 |
| `test_parse_minutes` | ~624 ns | 168,039 | 含字串解析 |
| `test_parse_hours` | ~619 ns | 168,663 | 含字串解析 |
| `test_format_seconds` | ~128 ns | 80,167 | `format_duration` |
| `test_format_minutes` | ~160 ns | 59,443 | format 分鐘 |
| `test_format_hours` | ~147 ns | 70,872 | format 小時 |
| `test_within_bounds` | ~796 ns | 131,303 | `validate_and_clamp` (無 clamp) |
| `test_clamped` | ~1.2 µs | 85,129 | `validate_and_clamp` (含 clamp) |

### Route Generation 工具鏈（pytest-benchmark in-process）

`generate_alertmanager_routes.py` 將 tenant YAML 轉 Alertmanager route + receiver + inhibit。純 generation 邏輯（不含 Python 啟動 / 載入 YAML），合成 tenant 規格：6 種 receiver type 輪替、`_severity_dedup` 啟用：

| Tenants | Median | Rounds |
|---:|---:|---:|
| 10 | ~38 µs | 27,678 |
| 50 | ~197 µs | 5,415 |
| 100 | ~394 µs | 2,773 |

純邏輯 sub-millisecond，CLI 啟動開銷（Python interp + import）佔 CLI wall time 的 ~55-70%。

