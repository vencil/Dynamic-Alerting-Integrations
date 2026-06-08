---
title: "Benchmark 操作手冊 (Benchmark Playbook)"
tags: [documentation, performance]
audience: [platform-engineer, sre]
version: v2.9.0
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
| **PR-time bench gate 看懂 + 處理 regression** | [§Tier 1 PR-Time Bench Gate Operations](#tier-1-pr-time-bench-gate-operations) |
| **Regression 處理（informational）+ nightly trend watchdog** | [§Regression 處理（Phase 2 — informational，不擋 merge）](#regression-處理phase-2-informational不擋-merge) |
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

## Tier 1 PR-Time Bench Gate Operations

> **Source of truth**：`.github/workflows/bench-gate-pr.yaml`（PR-time）+ `.github/workflows/bench-record.yaml`（nightly baseline + sustained-trend watchdog）。本節是 operational 對應文件 — SRE 防線 codification 見 user memory `feedback_github_actions_workflow_gotchas.md`。
>
> **Phase 2（informational）**：bench gate **不再阻擋 merge**。真 regression → 自動貼 PR comment + 上 `perf-regression` label（清掉後自動移除）；INCONCLUSIVE → 只標註。`override: bench-regress-ok` label + `bench-override-audit.yaml`（角色驗證 + stale-label strip）**已移除**——沒有 gate 要 override。main 的持續退化改由 nightly trend watchdog 守望（見 [§Nightly sustained-trend watchdog](#nightly-sustained-trend-watchdog)）。

### 概觀

| 項目 | 值 |
|---|---|
| 工作流檔案 | `.github/workflows/bench-gate-pr.yaml` |
| 觸發條件 | `pull_request: [opened, synchronize, reopened]` + path filter |
| Path filter | `components/threshold-exporter/app/**` / `rule-packs/**` / 此 workflow 自己 |
| 拓樸 | **單 runner INTERLEAVED + control canary**（v6）：兩個 worktree（base + pr 同時 checkout）→ `go test -c` 預編譯 → 迴圈交替跑 base,pr,base,pr,…（含 canary）→ benchstat。harness = `scripts/tools/ops/bench_interleave.sh` |
| Wall time | **~20–25 min**（與 v5 count=6 同總 benchtime；多 process 啟動由「只編譯一次」抵銷）|
| 統計判定 | `benchstat -alpha=0.01` AND `|Δ| ≥ 5%` 雙閾值 |
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
   - **看起來像 noise** — 同 benchmark 在 nightly `bench-record.yaml` 沒這個趨勢？到 GitHub Actions UI 的 Checks panel 右上點 **"Re-run failed jobs"** 重跑。GH-hosted runner ~10% FP rate per design（hardware floor）。**不要**用 `git commit --allow-empty` 或 close+reopen PR 重跑 — 那會污染 git history + 觸發無關的 webhook noise（Slack / Jira / labels）
   - **是 deliberate trade-off**（正確性修補 / 安全 patch / 演算法本質約束）→ Phase 2 起 gate 是 informational，**直接 merge**，在 PR 寫明 trade-off rationale 即可（無需 override）。`perf-regression` label 留作標記，待後續 perf 回收

3. **重跑後仍 flag** → 多半是真的 regression；不擋 merge，但請當作待辦處理

### Regression 處理（Phase 2 — informational，不擋 merge）

Phase 2 起 PR bench gate **不阻擋 merge**。真 regression（主 bench ≥5% 顯著退化 **且** CPU canary 穩定）時 workflow 自動：

1. 貼一則 PR comment（含 benchstat regression 行）。
2. 上 `perf-regression` label。
3. job 仍 **exit 0**（綠燈）——merge 不被擋。

regression 清掉後（下次 push 不再 flag）label 自動移除。**沒有 override 流程**——沒有 gate 要 override。若是 deliberate trade-off，在 PR description 寫明即可；若需回收 perf，留著 label 當追蹤。

> **Fork PR 限制**：`pull_request` event 對 fork 給 read-only token，comment/label 會失敗 → 降級成 `::warning::` + step summary（**不**用 `pull_request_target`，避免 pwn-request 洩 token）。

main 的**持續退化**由 nightly trend watchdog 守望（下節），不靠 PR-time 單點判定。

### Nightly sustained-trend watchdog

`bench-record.yaml` 的第二個 job `trend-watch`（nightly baseline 上傳後跑）用 `analyze_bench_history.py --trend-watch` 比對最近 N 晚，**只在「持續多晚」退化時自動開 `perf-trend` issue**（`--assignee` 預設 repo owner = email 通知;若 owner 是 GitHub **Org** 無法 assign,自動 fallback 成**不指派**、仍照常開 issue,靠 `perf-trend` label 訂閱通知),perf 回到 baseline 時**自動關閉**（closed loop）：

- **R1 sustained**：最近 K 晚（預設 3）全部高於**錨定 baseline**（settled 舊窗中位數，非「跟昨天比」）≥ floor。
- **R2 creep**：最近窗的**典型晚（recent median）**高於**同一個錨定 baseline** ≥ creep floor。容忍「最近 3 晚有一晚因雜訊回落」的 step-change——這種會被 R1 的 `all()` 漏掉。⚠️ creep 比的是**中位數對中位數**（非舊版的「對窗內最佳晚」）：舊版用 `min` 當基準,單一異常快的夜（lighter run / 量測 glitch）就把基準釘死,讓平坦無退化的序列也永遠 creep、closed-loop issue 永遠關不掉（#702）。註:14 晚窗本就抓不到「每晚退 0.5%×數週」的慢性 creep（累積 < floor）,故唯一誠實的訊號是「近期典型 vs 過往典型」。
- **noise floor**：sustained floor = max(固定 5%, min(3× control canary 夜間 CV, **10% 上限**))；**creep floor = max(10%, min(3× canary CV, 20% 上限**))。creep 是噪音敏感規則（median 對 anchor 的差隨 runner 噪音放大）,故 cap 較高、噪音夜會把 creep floor 拉到 15–20%；舊版誤與 sustained 共用 10% cap → `max(0.10, ≤0.10) ≡ 0.10` 死胡同,canary 對 creep 形同無效（#702）。nightly 也跑 `BenchmarkControlCanaryCPU`（同 PR gate 那支）→ 小於 runner 自身噪音的移動永不告警；sustained 的 canary 貢獻**封頂 10%**,避免噪音 runner 反而把真退化消音(fail-toward-silence 防線)。
- 單晚 blip 被「最近窗多晚」條件過濾;**bench 從最近窗消失(perf timeout 徵兆)直接 skip**——不讓舊夜冒充「今天」或藏掉 spike。
- **狀態化 issue lifecycle（#754 follow-up,治洗版）**:已開 issue 時**每晚原地改寫 body**(`gh issue edit`,永遠反映當前表格),**只在「被旗標的 bench 集合改變」時才留 comment**(新增 / 復原 / creep↔sustained 升降級);狀態以 body 內隱藏 HTML-comment marker（`<!-- perf-trend-state v1 [...] -->`）持久化、下次 run 解析回來比對。舊版每晚 append 一整張表 → #702 累積數十筆雜訊。**recovering label**:當 sustained 已清、只剩 creep,自動上 `perf-trend:recovering`(任一 sustained 回來或 issue 關閉即移除),讓訂閱者一眼分辨「仍惡化 vs 復原中」。無 marker 的 legacy body 視為「無變化」→ 遷移那一晚靜默(只補 marker、不誤發 comment)。
  - **`/ack <bench>` 手動靜音 — defer-with-trigger(刻意不做)**:#754(creep 假陽性根治)+ #755(原地改寫 + 轉變才 comment)後,洗版痛點已從源頭+投放兩端解決,open issue 現為低噪音態,**目前無 muting 需求 demand signal**(沒有「accepted regression 一直 nag、想單獨靜音」的真實案例)。`/ack` 需 issue-comment 指令 parser(新輸入面 + 權限 + acked-set 持久化),對單人維運 ~20 條內部 bench 投報率差。**任一觸發再做**:(1) 出現 ≥1 條已決定不修/可接受的 bench 退化持續產生 transition comment 造成困擾;(2) 多人協作需「誰 ack 了什麼」審計;(3) bench 數量成長到逐條人工判讀不可行。**觸發時優先 repo config 檔列管 muted benches**(watchdog 讀檔)而非 comment-parser——簡單一個量級。臨時靜音的既有逃生門:close issue(閉環只在新 transition 才動)/ 調 threshold / 從 nightly set 移除。

本機 dry-run：`py scripts/tools/dx/analyze_bench_history.py --trend-watch --fixture-json <nights.json> --dry-run`（`--fixture-open-issue N` + `--fixture-open-body '<marker>'` 可離線測 update-in-place / transition-only comment / recovering label / close 閉環）。

### Troubleshooting

| 症狀 | 原因 | 處理 |
|---|---|---|
| `⚠️ INCONCLUSIVE (runner drift)` — CPU canary 漂移 ≥4% | 這次 run 的 runner 真的在漂移（thermal/頻率/鄰居）；交錯已盡力抵銷但殘餘超標 | **正常防線**，綠燈非紅燈：push 再一次或 "Re-run failed jobs" 拿乾淨比較。連續多次才需查 runner |
| `::warning::Control canary absent` | canary 沒 emit（harness / 編譯 / benchstat 格式變動）| 判 INCONCLUSIVE re-run；若持續，查 `bench_interleave.sh` 編譯 log 與 benchstat 版本 |
| `perf-regression` label 上了但想 merge | Phase 2 informational — label 不擋 merge | 直接 merge；deliberate 就在 PR 寫明，否則排進 perf 回收 |
| `perf-trend` issue 一直開著 | main 持續退化未修 | 修 perf 後 watchdog 下晚自動關；或人工確認後手動關 |
| fork PR 沒收到 regression comment | fork token read-only（WAI）| 看 step summary（已降級寫在那）；maintainer 可手動轉述 |
| 整個 workflow 不跑 | path filter 沒匹配（doc-only PR） | 正常行為，不需處理 |
| Wall time > 25 min | runner load 高 / build cache cold / Go module download 慢 | 通常下次 PR build cache 暖了就回 ~20 min；持續異常查 runner metrics |

### Cross-references

- **Design rationale**（原 `bench-gate-rollout.md`，已隨內部 doc 清理移除）：Tier 1 + Tier 2 split、Scapegoat Trap / Noisy Neighbor Illusion / Broken Window / hardware floor escalation
- **Codified gotchas**：user memory `feedback_github_actions_workflow_gotchas.md` — 13 traps from 12 rounds of adversarial review (Virtual-merge HEAD / Labeled trigger / fetch-depth / fork-PR write / fabricated SHA / Partial shard / alpha vs confidence / Ghost Green race / Blanket Immunity / Redundant Wait + Ghost Comment + Zero-regression crash / hetero CPU silent FN / sharding-on-hetero-pool antipattern / sequential dirty workspace + timeout tightrope)
- **Issue tracker**：[#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433) — W1 ✅ / W2 ✅ / W3 (FP rate observation, ~2 weeks) / W4 ✅ (Tier 2 shipped)

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
>
> **Bench regression gate**:
> - Phase 1 (nightly informational) 已落地（`bench-record.yaml`）。
> - Phase 2 **redesigned** via issue [#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433) (supersedes closed #67) — split into **Tier 1** (PR-time, base-vs-PR, blame correctness) + **Tier 2** (release-time, release-vs-release, cumulative drift). Tier 1 已 ship 並啟用：`.github/workflows/bench-gate-pr.yaml`。Tier 2 待 W4 開發。
> - 完整 design + 5 個 SRE 防線 codification + hardware floor escalation：codified gotchas 見 memory `feedback_github_actions_workflow_gotchas.md`（原 `bench-gate-rollout.md` 已移除）。
> - PR-time operational flow（觸發條件 / step summary 解讀 / regression 處理 informational / nightly trend watchdog）：[§Tier 1 PR-Time Bench Gate Operations](#tier-1-pr-time-bench-gate-operations) below。

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

soak harness 已支援所有 sweep；exporter 端調 env/flag，harness 端只負責量測：

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
  -bench='BenchmarkFullDirLoad_Hierarchical_1000|BenchmarkDiffAndReload_Hierarchical_1000_NoChange|BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged|BenchmarkScanDirHierarchical_1000|BenchmarkBlastRadius_DefaultsChange_Hierarchical_1000' \
  -benchmem -count=3 -timeout=15m .
cat /tmp/b1_out/bench.out.txt
```

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

**與 Rule Evaluation 的關係**：[benchmarks.md §2](../benchmarks.md#2-為什麼能-scale-架構保證-om-向量匹配) 量測 Prometheus 規則評估（O(M)，與租戶數無關），本表量測 threshold-exporter 設定解析（O(N)，線性增長）。兩者互補：最關鍵瓶頸（規則評估）恆定，次要成本（設定解析）1000 租戶仍僅 ~10ms，遠低於 15 秒抓取週期。

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

Fixture 供 §v2.8.0 1000-Tenant Hierarchical Baseline (上方) 與 §Phase 2 e2e harness 使用。**ADR-016 引用**：[`docs/adr/016-conf-d-directory-hierarchy-mixed-mode.md`](../adr/016-conf-d-directory-hierarchy-mixed-mode.md) flat vs hierarchical 效能對照即此表。

### Schema Validation — `validate_tenant_keys`

`scripts/tools/ops/validate_config.py` 內 `validate_tenant_keys()` 逐 tenant 驗證 key 合法性（20 輪 in-process median）：

| Tenants | Median |
|---:|---:|
| 10 | 0.010 ms |
| 100 | 0.128 ms |
| 500 | 0.498 ms |
| 1,000 | 0.978 ms |

純 dict 操作，1000 tenant < 1ms，可安全嵌入 hot-reload path。

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

