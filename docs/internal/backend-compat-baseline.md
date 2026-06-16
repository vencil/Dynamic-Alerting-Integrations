# Backend-compat parity baseline（VictoriaMetrics）

> ADR-025 deferred「後端相容性」Part 1。把「平台 backend-agnostic」（[ADR-020](../adr/020-tenant-federation.md)/021）從行銷宣稱變成**可驗證的 CI 事實**：對真實 VictoriaMetrics 跑代表性 rule-pack golden，斷言它與 Prometheus 評估**我們編譯出的 expr** 結果一致。

## 為什麼自己寫（不接 OSS 工具）

業界標準是 `prometheus/compliance` 的 `promql-compliance-tester`。**評估後不適用**，三個硬傷：

1. **跑固定的內建通用題庫，不吃自訂 query** → 測不到我們的**編譯產出**（強制注入 `tenant`、`or vector(0)`、`label_replace`、`and on()`、`max by(tenant)`）。我們的相容性風險在編譯產出，不在標準 PromQL。
2. **資料模型是 scrape-based、需等「至少 1 小時」灌資料** → CI smoke 不可行。
3. **不能 offline**，需兩個 live endpoint。

→ 這是 [hybrid lint policy](lint-policy.md) 的 **DIY-exception**（meaningful divergence）：薄 harness（`tests/rulepacks/test_vm_backend_parity.py`），**復用既有 promtool golden 當 Prometheus 參考**（不另寫 fixture、不漂移）。

## 方法

1. 取既有 promtool fixture（`tests/rulepacks/*_test.yaml`）的 `input_series` → 灌進 VM（`/api/v1/import/prometheus`）。
2. 按 pack 順序 **materialize recording 規則**（leaf→recording→alert 多層鏈；順帶把 recording 規則自身 PromQL 過引擎）。
3. 對 VM 跑**我們的 expr**，與 golden 比對：
   - `alert_rule_test` → **alert-decision parity**：fire/no-fire + label-set（= golden `exp_labels` 減掉 evaluator 加的 **literal** static label，如 `severity`；templated passthrough 如 `tenant: "{{ $labels.tenant }}"` 保留）。
   - `promql_expr_test` → label-set **+ 值（epsilon）parity**（對 `exp_samples`）。

## 誠實 scope（覆蓋邊界）

| 涵蓋 | 不涵蓋（不同層 / 仍 defer） |
|---|---|
| 後端對**編譯 expr（含 recording 鏈）**的函數 / label / 值（epsilon）評估一致 | `for:` duration、alert templating → rule **evaluator**（Prometheus/vmalert）的事，與儲存後端無關 |
| alert fire/no-fire 決策一致 | **staleness / gap 上的 `absence` / `predict_linear` 時間外插** → 需真實時間軸 gap，dense fixture 測不出 → **仍 defer**（trigger：首個客戶整合自有後端）|

## 關鍵實作決策（gotchas，動它前先讀）

- **VM 須帶 `-retentionPeriod=100y`**：fixture 用**固定 epoch T0**（確定性，不用 `now`；CI 快慢不影響結果）。VM 預設 1 個月 retention 會把 2023 的 T0 樣本**靜默丟棄**（import 回 204 卻查不到）。
- **唯一性時間窗**：每個 (worker, case, test-block) 用唯一且確定性的 `T0 + slot*GAP`（`GAP=3600s` ≫ VM 5m staleness lookback）→ 不跨 fixture/worker 污染、重跑冪等。`-n auto` 安全。
- **fail-loud**：CI job 設 `VM_PARITY_REQUIRE=1` → VM 連不上即**硬 fail**，絕不靜默 skip→假綠；`force_flush` 失敗在 REQUIRE 下也 raise（避免 unflushed race）。
- **plumbing guard**：每個 case 斷言 recording-rule 鏈**有寫出 ≥1 series**——否則空的 alert 結果可能是 no-op 假裝「no-fire」。
- **VM image digest-pin**（供應鏈，#851 政策）：CI 用 `victoriametrics/victoria-metrics:v1.111.0@sha256:…`；bump 版本時用 `docker buildx imagetools inspect <img>:<ver>` 重解 digest（勿用 `docker inspect`，arm64 會給 arch-specific digest）。
- **本地**：無 VM → parity 自動 skip、純函式單元測試照跑；要本地跑 parity：`docker run -d -p 8428:8428 …:v1.111.0 -retentionPeriod=100y` 後 `VM_PARITY_ENDPOINT=http://localhost:8428 pytest …`。

## 加覆蓋

擴 `_CASES`（alert）/ `_EXPR_CASES`（value）一行即可。挑「跨引擎最會炸的 idiom」（pattern-breaking detection，非 100% 覆蓋）。⚠️ literal-static-label 減法是 heuristic：若新 fixture 的 rule 有「literal static label 又被 expr 產出」或「templated static label 卻不在 expr 輸出」，需調整（現有 case 無此形）。

相關：[pint-lint-baseline.md](pint-lint-baseline.md)、[iac-lint-baseline.md](iac-lint-baseline.md)、[ADR-025](../adr/025-alerting-plane-self-liveness.md)。
