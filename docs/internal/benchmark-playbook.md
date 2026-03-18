---
title: "Benchmark 操作手冊 (Benchmark Playbook)"
tags: [documentation, performance]
audience: [platform-engineer, sre]
version: v2.2.0
lang: zh
---
# Benchmark 操作手冊 (Benchmark Playbook)

> Benchmark 方法論、執行環境、已知陷阱與踩坑記錄。與測試排錯分離，便於獨立查閱。
> **相關文件：** [benchmarks.md](../benchmarks.md)（量測數據）· [Testing Playbook](testing-playbook.md)（K8s/測試排錯）· [Test Map § Benchmark 基線](test-map.md#benchmark-基線)· [Windows-MCP Playbook](windows-mcp-playbook.md)（docker exec 陷阱）

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

### Go benchmark log 噪音致 output 爆量 (v2.1.0)

**現象：** `go test -bench="FullDirLoad|IncrementalLoad" -benchmem -count=5` 產出 ~732KB 的 stdout，benchmark 結果行被淹沒在 log 噪音中。`2>/dev/null` 在某些 Docker exec 管線下無效（stdout 也被丟棄）。

**根因：** `fullDirLoad()` 和 `IncrementalLoad()` 每次呼叫都寫一行 `log.Printf("Config loaded ...")`，100 tenant × N iterations × 5 rounds = 數千行 log。

**修復：** 在 benchmark 函數中加入 `silenceLogs(b)` helper：

```go
func silenceLogs(b *testing.B) {
    b.Helper()
    orig := log.Writer()
    log.SetOutput(io.Discard)
    b.Cleanup(func() { log.SetOutput(orig) })
}
```

**關鍵：** `silenceLogs(b)` 要放在 setup `fullDirLoad()` **之前**（不只是 `b.ResetTimer()` 前），否則每次 benchmark invocation 的 setup phase 仍會產生 log。`b.Cleanup()` 確保 benchmark 結束後恢復正常 log 輸出。

**延伸：** Docker exec + PowerShell 的引號嵌套問題使得 `2>/dev/null` / `grep` 管線不可靠。最可靠的做法是：(1) 將 output redirect 到容器內檔案（`> /tmp/bench.txt 2>/tmp/err.txt`），(2) 事後用 `grep ns/op` 過濾。

### 連續多輪 benchmark port-forward 不穩定 (v2.0.0-preview.4)

**現象：** 用 for loop 連續跑 5 輪 `benchmark.sh --under-load`，第 3 輪起 Prometheus metrics 全部返回 0。

**根因：** benchmark.sh 的 cleanup trap 會 kill port-forward process，但下一輪啟動時 port-forward 可能尚未完全釋放埠號，或 Prometheus pod name 已變更（因上一輪的 ConfigMap patch 觸發 restart）。

**解法：** 每輪獨立執行（手動或間隔 30s），不要用 tight loop。或在 loop 間加入 `sleep 30` + `kill_port 9090` 確保埠號乾淨。

---

> 本文件從 [Testing Playbook](testing-playbook.md) § Performance Benchmark 獨立拆分（v2.0.0-preview.4）。
