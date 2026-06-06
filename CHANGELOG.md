---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.8.1
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [Unreleased]

<!-- 下一版 in-flight 工作暫存區。每筆 entry 目標 3-6 行使用者重點 + 一行指回內部 artifact；session 過程 / FUSE trap / 完整 commit list 不入此處。release 收尾時做最終 condensation 並切正式 `## [vX.Y.Z]` heading。 -->

### Fixed

- **portal `recipe-status.json` 衍生 copy 在 Windows 上被寫成 CRLF，污染 esbuild bundle 致 Portal Tests dist gate 在 CI 永遠 red（#741 #6 increment 2 follow-up）**：`gen_recipe_status_json.py` 用 `Path.write_text` 預設換行 → Windows 產出 **CRLF**。`eol=lf` gitattribute 使 `git status` 與產生器自身 `--check`（`read_text` universal-newlines 正規化）**雙雙看不到**，但 esbuild 讀 working-tree 原始 bytes → sourcemap `sourcesContent` 變、shared chunk 重新 hash（`chunk-KVT42DRD`→`chunk-FXJK44R5`），committed dist 與 CI 原生 build 永遠對不上。**修法**：產生器強制 `newline="\n"`（跨平台 LF）+ 新增 raw-bytes 回歸測試盯住「磁碟上必為 LF」這條被繞過的不變式；portal dist 以 LF source 重建對齊 CI。順帶把 Portal Tests CI 由 Node 20 升 24 對齊 dev container，移除 node/npm 版本作為 dist 可重現性的潛在變數。詳 [`gen_recipe_status_json.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/gen_recipe_status_json.py)。
- **tenant-api SIGTERM 優雅關機處理 SSE，消除 15s 關機卡死 + 生硬斷線（issue [#675](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/675) / TRK-322，ADR-023 §2）**：`main.go` 關機只 `srv.Shutdown(15s ctx)`，**無 SSE 廣播、不關 hub**。SSE 連線永不 idle → `http.Server.Shutdown` 會**等滿 15s 逾時**才退出、且 SSE 連線被生硬切斷（client 看到 connection reset）；滾動更新時所有 client 同時重連砸向尚未 ready 的新 pod（reconnect storm）。**修法**：新增 `ws.Hub.Shutdown(reconnectDelay)`——SIGTERM 時**先**廣播一則 `server_shutdown` 控制事件（帶 `reconnect_delay_ms:2000` 提示）**再** close 所有 client channel，**然後**才 `srv.Shutdown`。兩個效果：(1) 關閉 never-idle 連線讓 Shutdown 毫秒級完成（不再卡 15s）；(2) client 收到可行動訊號 + 重連提示而非 raw reset，well-behaved client 應等 `reconnect_delay_ms` + 自身 jitter 再重連、打散流量。`Event` 加 `reconnect_delay_ms`（omitempty，僅 `server_shutdown` 帶、其餘事件 byte-identical）。**late-subscriber 護欄**（CodeRabbit review）：`Hub` 加 `shuttingDown` latch（同 mutex），Shutdown 起即拒新訂閱、`ServeHTTP` 回 **503** —— 否則 Shutdown 與 `srv.Shutdown` 之間抵達的 late request 仍可開新 SSE、既錯過 hint 又重演 stall。**前端 jitter 重連屬 Portal 對接 future work**（目前無 SSE consumer，server 先發出契約事件作為基礎）。新增 4 條 `hub_test`（buffered-event-then-close 排序 + hint 值 / **ServeHTTP 在 Shutdown 後即時返回**——載重測試證明不再卡 srv.Shutdown / 零 client no-op 安全 / **Shutdown 後拒新訂閱 + 503**）；`-race` 綠。無 chart / env 變更。詳 [tenant-api-hardening.md §5.6](docs/api/tenant-api-hardening.md)。
- **`describe_tenant` 的 `_custom_alerts` 改走編譯器 UNION 繼承解析，修 blast-radius 對 override 租戶漏報 P0（[#772](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/772)，#773 的 Known Issue follow-up）**：`describe_tenant.deep_merge` 對陣列用 **REPLACE**（ADR-017），但編譯器 `loader.py` 對 `_custom_alerts` 是 **UNION across levels**（ADR-024，「租戶不得悄悄抹掉平台/domain 政策 recipe」）。兩者脫鉤 → 有自有 `_custom_alerts` 的 **override 租戶**，其 `effective_config._custom_alerts` 只含 own、**漏掉 inherited 政策**，使 blast_radius 對「`_defaults.yaml` 政策變更」這個**最高 blast-radius 卻最少被審計**的變更類**全盲**。**修法（守 SSOT，不打補丁）**：**不動 `deep_merge`**（通用陣列 REPLACE 語意完整保留，守 ADR-017）；而是讓 `describe_tenant` 對 `_custom_alerts` 這**單一欄位** delegate 給編譯器自己的繼承解析器 `collect_instances`（唯一事實來源）——effective_config 的 `_custom_alerts` 變成 own+inherited 的 union，且新增 `_custom_alerts_resolution`（每筆標 `name`/`origin`/`is_own`）讓「此欄位走 union、與其他陣列的 REPLACE 不同」**顯式可見、非靜默不一致**（採 Gemini 外審的 mental-model 點，但用 `collect_instances` 免費已有的 origin 標註，非另闢側欄位）。`collect_instances` **掃整樹一次**建 tenant→instances map（effective_config 為查表，非 `--all` 下 O(N²) 重走）；compiler 套件不可用時 graceful 降回 deep_merge 行為。**駁回 Gemini 終案（Array→Dict schema 重構）**：dict-merge 撞 key=**覆蓋**≠UNION → 租戶重用平台 recipe 名即可悄悄抹掉政策，**重新引入 ADR-024 要禁止的 footgun**；且 list 形態貫穿整個 epic（Go exporter/tenant-api AST merge/portal/fixtures/遷移），blast radius 不成比例。**config_diff 不動**：它對 metric diff 也忽略 `_defaults`，custom-alert 比照=一致（繼承層級 diff 是 blast_radius 領域，config_diff 是 tenant-own snapshot）。測試：override 租戶含 inherited（union+origin）/ inherit 租戶得政策 / 無告警則無 `_custom_alerts` key；既有 deep_merge array-REPLACE 測 + golden parity 全綠（未動 deep_merge、golden fixtures 無 `_custom_alerts`）。詳 [ADR-024 §S6b-2](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **ops-review 工具認得 `_custom_alerts` 為 alerting 變更，消除 blast-radius / config-diff 盲點（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) ADR-024 能力 B follow-up）**：`blast_radius.py`（v2.7.0）與 `config_diff.py` 都早於 custom-alert recipe（reserved key `_custom_alerts`）→ 租戶新增/改/刪 `_custom_alerts` 時，blast-radius 把它誤判為 **Tier C format-only**、config-diff 顯示 **No changes detected**（PR #771 db-b 加 2 個 recipe 即實證——自訂告警是真實 alerting 變更卻顯示無影響）。**修法**：(1) blast_radius `_custom_alerts` 升 **Tier A**（與 threshold/routing 同級 highlight；`flatten_dict` 保留整個 list 為單一不透明值，故 field path 恰為 `_custom_alerts`，原本「alerts/_alerting」Tier-B pattern 不會 substring 命中而落到 C）；(2) config_diff 新增專屬 recipe-diff path（`load_custom_alerts_from_dir` + `compute_custom_alert_diff`，依 per-tenant-unique `name` 配對 → 增/刪/改，modified 列出 field-level 變更如 threshold/op/window/mode），surface 進 markdown「Custom Alert Changes」段 + JSON `custom_alert_diffs` key + exit-code（**scope=租戶自有宣告**，與既有 metric diff 一致不解析 `_defaults.yaml` 繼承，inheritance-resolved 全貌歸 `scripts/tools/dx/custom_alerts/` 編譯器）。**外審（Gemini）後強化**：(F2) blast_radius 對 `_custom_alerts` 改 **recipe-level 摘要**（`changed (count 8→8): ~['queue_deep']`）取代整份 list raw dump，免 review fatigue；(Reef 1) config_diff 對 **silencing 變更語意高亮**——`threshold→disable` 標 `:warning: DISABLED (alert silenced)`、`mode page→silent` 標 `paging suppressed`（披著「modified」外衣的拔插頭一眼可見）；(Reef 2) config_diff markdown **truncation safeguard**（`COMMENT_SAFETY_LIMIT` 60K，超量截斷不讓 bot 留言 422 猝死 → 高風險大 PR 不裸奔）；(F5) PR bot 留言**注入防禦**——recipe 值（含 free-form selectors）一律進 code span + 中和 backtick（pre-compile 未驗證輸入不可信）；(F3) 新增 describe_tenant→blast_radius **整合測試**鎖契約（防 effective_config 哪天 strip `_`-key 致修法靜默失效，#731 教訓）；(Reef 3) name-based identity 的 rename→remove+add 折衷已於 docstring 註明。**第二輪對抗**：(N1) **silencing 高亮補進 blast_radius**——關鍵在 D2 下 config_diff（pinned image）的 Reef-1 高亮 dormant、blast_radius（跑 source）才是 live 工具，故 `threshold→disable` / `mode→silent` 的 camouflage 高亮**必須**在 blast_radius 也有（否則保護沒上線）；(N3) blast_radius 對 `_custom_alerts` **純 reorder 降級 Tier A→C**（recipe 依 shape 向量化、順序無關，zero alerting impact；只在 recipe multiset byte-identical 時降級，不藏真實變更——否則 YAML-sorter reorder 會 flag substantive 並在規模下翻 summary-mode 埋掉真改）。**第三輪對抗（bot bulletproofing）**：(Reef 4) `_format_recipe_value` 中和**換行符**（raw `\n` 會撐破 code span / list item 把整段 markdown 打碎；本段用 code span 故折成空格而非 `<br>`）；(Reef 5) `load_custom_alerts_from_dir` 加 **strict `isinstance(list)` guard**（mistyped `_custom_alerts: "oops"` 是可迭代字串、naive `for r in` 會迭代字元 → `r.get()` AttributeError → CI bot crash 擋 PR；改 skip 非 list）；(Reef 6) blast_radius 對 `_custom_alerts` **None ≡ []** 等價（缺 key→空陣列是 no-op，不再誤觸 Tier A noise；config_diff 本就等價）。**第四輪 self-review（R2）**：disable 偵測**兩工具同位**——對齊 exporter `custom_alert.go` 權威語意（拆 `:severity` 後比對完整 disabled set `disable/disabled/off/false`，非僅字面 `disable`），否則 blast_radius（live 工具）會漏報 `threshold: off` / `disable:warning` 類 silencing。regression：blast_radius classify/classify_diff/compute（Tier A 非 C / reorder→C / 真改不降級 / None≡[] / 非空仍 A）/ F2 摘要 / N1 silencing 高亮 / F3 整合 + config_diff add/remove/modify/mode-toggle/disable-highlight/injection/newline-shatter/string-type-guard/none-empty-equiv/truncation/render/json。**⚠️ Known Issue (F1 → [#772](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/772))**：`_defaults.yaml` 的 `_custom_alerts` 政策變更，對「有自有 `_custom_alerts` 的 override 租戶」在 blast_radius 漏報——根因為 `describe_tenant.deep_merge` 對陣列用 REPLACE，與 compiler/loader 的 UNION 繼承語意矛盾（effective_config 與實際 compile 不一致）；本 PR 守 tenant-leaf scope，deep_merge 語意修正切 P0 [#772](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/772)（連結 epic #741）。N5（tenant-id heading markdown 注入，pre-existing 兩工具共業）DEFER 至 security hardening sprint。**Note (D2)**：config_diff 走 pinned `da-tools` image，本修法待下個 da-tools image 發版 + workflow pin bump 才生效（blast_radius.yml 跑 source、merge 即生效）。詳 [ADR-024 §S6b-2](docs/adr/024-version-aware-threshold-via-dimensional-label.md)、custom-alert schema `customAlertInstance`。
- **tenant-api 寫入平面 load-shedding + context 綁排隊階段，杜絕 goroutine 堆積與孤兒寫入（issue [#673](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/673) / TRK-320，ADR-023 §C）**：所有寫入序列化在單一 `w.mu`，**無 in-flight 上限、無 load shedding**；更關鍵 `sync.Mutex.Lock()` **不吃 context** —— `middleware.Timeout(30s)` 讓 client 收到逾時、卻不釋放卡在 `Lock()` 的 goroutine，而 `gitCmd` 用自己的 `context.Background()` → **client 早逾時、git 寫入仍照跑（孤兒寫入）**。突發 `PUT` 下資源無上限堆積。**修法**（token 模型）：寫入方法簽名加 `ctx`（根因即 Lock 不吃 context），在 `w.mu` 前過 `acquireWrite(ctx)` —— 單一執行 token（`writeExec` cap 1）序列化唯一 in-flight 寫入，`writeInFlight` 把「執行中＋排隊中」總額封在 `maxWriteAdmit`（= 1 + `TA_WRITE_QUEUE_DEPTH`，預設 5），超過即時 `ErrWriteOverloaded` → handler 回 **503 + `Retry-After`**（machine-actionable，含 `retry_after_s`）。**排隊（等 token）ctx-aware**：client 排隊期間斷線/逾時立即釋放、**永不執行（杜絕孤兒寫入）**，含「拿號後進鎖前」微秒夾縫的 `ctx.Err()` 補查；**一旦進臨界區就讓寫入跑完**，不中途砍 git（半路砍留 dirty tree / 誘發重試重複 PR）—— in-flight 由 git timeout 圈住。涵蓋全部 7 個寫入路徑（tenant / batch / groups / views / federation policy+subset），handler 與 federation 子包共用匯出的 `WriteOverloaded`。新 env `TA_WRITE_QUEUE_DEPTH`（0 為合法的最激進設定）。regression：env parse、超上限即時 shed（mutation 證實——拔掉 shed 即排隊 block）、排隊中 ctx cancel 立即釋放不洩 goroutine、cancelled ctx 的 Write 不寫檔（孤兒防護）、struct-literal nil-admission 向後相容；`-race` 綠（atomic 計數 + chan token 並發）。helm chart `2.9.5`→`2.9.6`（`tenantApi.writeQueueDepth` 旋鈕）+ raw k8s manifest 同步。與 TRK-318 配對。詳 [ADR-023 §C](docs/adr/023-write-plane-single-writer-invariant.md)。
- **tenant-api circuit breaker 認得 GitHub secondary-rate-limit 403 + 尊重 `Retry-After`（issue [#672](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/672) / TRK-319，ADR-023 §C）**：`isForgeDegradation` 原本只判 `StatusCode >= 500`，所有 4xx（含 GitHub secondary rate limit / abuse 的 **403**、GitLab 的 **429**）落在 `IsSuccessful=true`、**不計入熔斷** —— rate-limit 期間寫入平面（writer + PollingTracker polling 共用同一 breaker）零保護，且無處尊重 `Retry-After`。GitHub 用 403 同時表示「權限不足」（不該熔斷）與「secondary rate limit」（該熔斷），status code 單獨無法區分。**修法**：(1) `platform.APIError` 加 `RateLimited bool` + `RetryAfter time.Duration`，由 client `roundTrip` 經新的 `DetectRateLimit`（**429 無條件**視為 rate-limit（定義即如此，連 bare 429 / HTTP-date Retry-After 都認）；**403 須有訊號**——`Retry-After` header / `X-RateLimit-Remaining==0`（GitLab `RateLimit-Remaining`）/ body 提及 "rate limit"·"abuse"——避免誤判 permission 403；gated 在 403/429 避免誤判 404/422；`Retry-After` parse 時 clamp 在 1h 上限，防 bogus 巨值 overflow duration 或經閘把寫入平面壓制數小時；**body 只在傳輸層 sniff、不留存於 `APIError`，零洩漏**）填入；(2) `isForgeDegradation` 改判 `StatusCode>=500 || RateLimited` → rate-limit 連續達 `cbConsecutiveFailures` 觸發熔斷，permission 403 仍維持現行「不熔斷」語意；(3) `APIError.Is` 排除 rate-limited 403（否則 handler 會把暫時性 rate-limit 誤映成永久 `insufficient permissions` 403，現正確改走 503）；(4) **Retry-After 對齊（方案乙）** —— breaker 熔斷後依 `Retry-After` 設 `notBefore` 閘，連 gobreaker 固定 60s 的 half-open probe 都壓制到 back-off 窗結束，避免「每分鐘醒來被 still-active limit 揍一次」（clock seam 注入、process-wide 共用、`mu` 護欄）。regression：DetectRateLimit 訊號矩陣 ×11、rate-limit 連續熔斷（mutation 證實——拔掉 `|| RateLimited` 即不熔斷）、permission 403 不熔斷、Retry-After 閘壓制 half-open probe；`-race` 綠（writer/tracker 並發共用 breaker）。無新 env / chart 變更。詳 [ADR-023 §C](docs/adr/023-write-plane-single-writer-invariant.md)。
- **`make sync-tools`（`sync_tool_registry.py`）兩個 silent bug — data-audience 全清空 + TOOL_META 同步路徑早已失效（PR #763 衍生）**：(1) script 自帶的 `parse_registry` 對 block-list 欄位（`audience:` 後接 `- platform` 等）會先寫 `current[key] = ""`、shadow 掉後續 `- item` append → audience 解析成空字串，使 Hub-card 同步把**每張卡片**的 `data-audience` 從 `"platform,domain,tenant"` 改寫成 `""`，連帶踩響 `lint_tool_consistency` 的 `[hub] audience mismatch` warning（warnings→EXIT_VIOLATION）擋住 commit。修法：解析器改為**縮排感知**、空 scalar 不 eager 寫入（block list / nested dict `en:`/`zh:` 都正確還原），Hub-card 同步改 **populate 為 sorted + comma-joined**。(2) TOOL_META 更新器的 regex（`var TOOL_META = {...}`）在 ESM dist-bundle 遷移後早已不匹配 `jsx-loader.html`（其活著的 key→path map 是扁平的 `var CUSTOM_FLOW_MAP = {...}`），同步路徑變成永遠 `ERROR` 的 no-op；改為直接同步 `CUSTOM_FLOW_MAP`（插新卡片 + map entry 為 codified 路徑，不再需手動加）。新增 `tests/dx/test_sync_tool_registry.py` regression（block-list 解析 / 不清空且 sorted / 插卡不動其他 / flow-map 插值 / 冪等）。詳 [tool-map.md](docs/internal/tool-map.md)。
- **Nightly bench trend watchdog — 狀態化 issue lifecycle，治每晚 comment 洗版（#754 follow-up，延伸 [#702](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/702)）**：watchdog 原本對已開的 `perf-trend` issue **每晚 append 一整張表 comment**（#702 累積數十筆雜訊）。改為**原地改寫 body**（`gh issue edit`，永遠反映當前表格）+ **只在「被旗標 bench 集合改變」時才留 comment**（新增 / 復原 / creep↔sustained 升降級）。watchdog 跨夜無狀態，故把前次狀態以隱藏 HTML-comment marker（`<!-- perf-trend-state v1 [...] -->`）存進 body、下次解析回來比對（legacy 無 marker body 視為無變化 → 遷移夜靜默）。新增 **`perf-trend:recovering` label**：sustained 已清、只剩 creep 時自動上、任一 sustained 回來或關閉即移除。`/ack <bench>` 手動靜音為 **defer-with-trigger（刻意不做）**——#754+#755 後洗版已根治、無 muting demand signal，觸發條件見 playbook。regression：同狀態只改 body 不留言 / 狀態變才留言 / 升降級文案 / recovering label add·remove·none / marker round-trip（含巢狀陣列）/ legacy body 靜默遷移。詳 [benchmark-playbook.md §Nightly sustained-trend watchdog](docs/internal/benchmark-playbook.md)。
- **Nightly bench trend watchdog — creep 規則改錨定中位數、修復永不關閉的 closed-loop（issue [#702](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/702)）**：`analyze_bench_history.py --trend-watch` 的 R2 creep 原以「窗內最佳晚」（`min`）為基準,單一異常快的夜（lighter run / 量測 glitch）就把基準釘死 → 平坦無退化的序列每晚 `recent_median ≥ min×1.10`、creep 永遠觸發 → 自稱 closed-loop 的 `perf-trend` issue 永遠關不掉、每晚洗一筆 comment（#702 實況:20 條 bench `vs anchor`≈0% 卻 `vs best-night` +36~102%）。修法（採外審建議,棄 p10——N=14 下 p10≈倒數第 2 小、只是「一顆地雷變兩顆」）：creep 改比**最近窗中位數 vs 同一個 settled 錨定中位數**（抗噪、且 14 晚窗本就抓不到多週慢性 creep,唯一誠實訊號即「近期典型 vs 過往典型」）;保留 creep 對 sustained 的獨立價值（容忍單一雜訊夜的 step-change）。連帶修 **creep floor cap 死胡同**（原與 sustained 共用 10% cap → `max(0.10,≤0.10)≡0.10`,canary 對 creep 形同無效;改 creep cap=20% → 噪音夜 creep floor 升至 15–20%）+ issue body `+-1.2%` 雙符號渲染（signed format）。regression：lone-fast-outlier 不再 creep（閉環可關）/ 單雜訊夜的 step-change 仍被 creep 抓 / 噪音 canary 拉高 creep floor / 負 % 正確渲染。詳 [benchmark-playbook.md §Nightly sustained-trend watchdog](docs/internal/benchmark-playbook.md)。
- **Custom-alert `for` 向量化靜默覆蓋修復（[#751](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/751) / TRK-326，ADR-024 能力 B P0）**：custom-alert 的 `for` 原既不在 `recipe_id` 也不在 `shape_signature` → `build_shapes` 取**首見實例**的 `for` 凍結整個 shape，兩租戶共用 shape 但 `for` 不同時，後者的 `for` 被**靜默丟棄**（無錯無警）。`mode` 可經 `group_left` 搭資料平面，但 Prometheus `for:` 是控制平面**靜態規則屬性**、`group_left` 救不了。修法（a+ Strict Enum Bounding）：`for` 納入 `recipe_id` slug（`shape.py` + `pkg/config/custom_alert.go::RecipeID` 跨語言契約，`recipe_id_vectors.json` golden vector **雙邊**鎖）+ schema 由 free-form pattern 改 **enum `["0s","1m","5m","15m","30m","1h"]`**（保留 `default:"1m"`）→ 不同 `for` ＝ 不同 rule（語意正確），cardinality 鎖常數（免 free-form 把向量化 O(M) 退化成 O(N)）。regression：兩租戶不同 `for` → 兩條獨立規則、同 `for` 仍向量化為一條（Py + Go 雙邊）。詳 [ADR-024 §Forecast Recipe 連帶節](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **tenant-api PR 寫入前在鎖內 fetch 新鮮 base，杜絕共享檔跨-merge silent data loss（issue [#671](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/671) / TRK-318，ADR-023 §B）**：`internal/gitops/` 全套**無存活期間的 `git fetch`**——本地 base 只由 git-clone initContainer 在 pod 啟動時 `pull --ff-only` 同步一次，long-lived pod 在遠端 PR merge 後本地 base 即停滯。災難（限共享檔 `_groups.yaml` / `_views.yaml` / `_federation_policy.yaml`）：Tenant A 的 PR 遠端 merge → pod 不重啟、本地 base 不動 → Tenant B 從過期 base 開 PR → diff 把 A 已 merge 的內容算成「要刪」→ **silent data loss**（per-tenant `{id}.yaml` 只寫自己、不受影響；direct 模式的 HEAD-before/after conflict 偵測只防同 pod 內並發、對「遠端 base 已前進」無感）。**修法**：`WritePR` / `WritePRBatch` 在臨界區內、開分支前先 `git fetch --prune origin <base>`，再以 `git checkout -b <branch> --no-track origin/<base>` **從 origin/<base> 開分支**（方案甲鎖內 fetch，非鎖外預載的 TOCTOU 方案乙）。**刻意不 `reset --hard origin/<base>`**——硬 reset 會丟棄本地 base 上未 push 的 commit（特殊檔 `WriteGroupsFile` 等即使 PR 模式也直接 commit 到本地 base），只錨**新分支**、不動本地 base 即兩全。fetch 用**獨立**的 `TA_GIT_FETCH_TIMEOUT`（預設 5s、與 60s 的 `TENANT_API_GIT_TIMEOUT` 解耦），逾時強殺 → 清 stale lock → 釋放寫鎖 → handler 回 **503 `FORGE_UNAVAILABLE`**（`ErrForgeDegraded`，帶標準 `Retry-After: 5` header + `retry_after_s` 欄位，讓自動化 GitOps controller / CI pipeline machine-actionable 地退避；訊息 sanitized 不洩漏內部 git/stale 細節），不 silently 用過期 base；無 origin remote（dev/local）或 non-timeout fetch error 則 best-effort 退回本地 base（不阻擋每次寫入）。regression：(1) 遠端先 merge 一個 `_groups.yaml` 變更，驗 PR 分支挾帶之、不回滾共享檔（mutation 證實——拔掉 fetch 即 fail）；(2) 本地 base 未 push 的 commit 經 `WritePR` 後仍存活。helm chart `2.9.3`→`2.9.4`（新增 `tenantApi.gitFetchTimeout` 旋鈕，emitted only when set）。前置 TRK-324 Recreate 已落地。詳 [ADR-023 §B](docs/adr/023-write-plane-single-writer-invariant.md)。

### Added

- **Custom Alerts recipe 生命週期 portal UX（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) GA-polish increment 2 C1，ADR-024 §8）**：portal `RecipeBuilder` 反映 recipe `status`——**deprecated** → 下拉標 `⚠ deprecated` + 黃 banner（仍可選/可加，「仍可用、建議盡早遷移」）；**eol** → 下拉 option **disabled**（add-flow 不可採用新 eol），但**既有 eol 實例的 option 仍可選**（edit-flow 讓參數續編）+ 紅 banner「**可存此既有告警的變更、但無法新增使用它的告警**」（對齊後端 **B2-寬**，刻意非「必須切換才能存」）。前端純 advisory UX，`gitops.Writer.validate` 才是 eol-expansion 權威。**status 餵前端**：`gen_recipe_status_json.py` 改產**兩份**衍生 `recipe-status.json`（Go `go:embed` + portal import 各一份、同 SSOT `shape.py::RECIPE_STATUS`、共用 drift gate `recipe-status-json-check`）。3 個新 Vitest（deprecated 警示 / add-flow eol disable / edit-flow 既有 eol 可選+banner）+ portal dist rebuild。燃盡圖 info-metric（D1）仍為後續 increment。詳 [ADR-024 §8](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts eol 寫入拒絕（B2-寬）+ recipe-status SSOT 衍生（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) GA-polish increment 2，ADR-024 §8）**：tenant-api `PUT /api/v1/tenants/{id}/custom-alerts` 新增 **eol-expansion 寫入閘**——對每個 eol recipe，PUT 中的實例數**不得超過現況**（擋「新增 / 加量 / 換用另一個 eol」，**放行**「改參數 / rename / 移除 / 既有續存」）。**刻意非**「擋任何含 eol recipe 的 PUT」：那是 full-overlay 連坐，會在租戶凌晨救火時被一個無關的舊 eol recipe 鎖死（outage hostage）。檢查在 **`gitops.Writer.validate`（所有 tenant-api 寫入的 choke point）** 強制——讀現況 on-disk tenant 檔（validate 在寫入前 → 仍是舊）+ 新 body 算 delta，故 **PutTenant / PutCustomAlerts / batch 全覆蓋**（無 bypass）；`/custom-alerts` handler 另先做一次回 **structured Violations** 給 portal。configDir-less 測試模式（無 on-disk base）跳過；CI/GitOps-direct compiler 無法分新舊 → warn-only。**recipe status 不手寫雙份**（消 split-brain）：`recipe-status.json` 從 `scripts/tools/dx/custom_alerts/shape.py::RECIPE_STATUS` SSOT 生成（`make recipe-status-json`）、Go 經 `go:embed` 消費（`customalerts.RecipeStatus` / `EolExpansionViolations`）；**committed artifact 非 build-time 生成 → 無 build-order flaky**；drift gate `recipe-status-json-check`（pre-commit + CI HARD gate）鎖 json↔SSOT 同步。portal 黃標 + 燃盡圖 info-metric 仍為後續 increment。詳 [ADR-024 §8](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **`da-tools runtime-audit` — Git rule-packs ↔ Prometheus runtime 唯讀對帳（RFC [#747](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/747)）**：補上 [#711](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/711)/[#714](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/714) PR-期 drift gate **蓋不到的 runtime 那條腿**——硬比對 Git 宣告規則與 Prometheus `GET /api/v1/rules` 實際載入態，分類 **MISSING**（宣告未載入：reload 失敗 / projected-volume lag / 手刪 configmap）/ **UNHEALTHY**（載入但 `health!=ok`，帶 `lastError`——series 觀測無法與「metric 本就不存在」區分）/ **ORPHAN**（仍載入但 Git 不再宣告，限已宣告群組內，不誤報無關 infra 規則）。**唯讀、不自癒**：incident 時 `kubectl port-forward` 後跑（零新基礎設施）或排程 `--ci`；exit 0/1/2 契約；可吃 `--runtime-json` 離線 fixture。**設計邊界 locked（codified residue）**：明確 **reject 自癒 / 常駐 reconciliation Operator**（機器回寫人類平面 + 觀測者悖論遞迴），對齊既有 silent-failure 範式（#631/#643/#652）；`version_orphaned` sentinel 保留為 visibility 訊號、非被取代。heavier 持續形態（in-cluster CronJob / per-layer-pair drift 指標）為 defer-with-trigger。決策見 [custom-rule-governance.md §7.1](docs/custom-rule-governance.md)，完整評析見 #747。
- **Custom Alerts recipe 生命週期狀態 — compile-side（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) GA-polish，ADR-024 #6）**：platform-authored recipe 新增 `status: [active|deprecated|eol]` 治理欄位（**RECIPE 版本控管**，有別於能力 A 的 APP `version` label）。executable SSOT = `scripts/tools/dx/custom_alerts/shape.py::RECIPE_STATUS`，6 份人類治理契約 `rule-packs/recipes/*.yaml` 鏡射 `status:`（drift guard `tests/dx/test_recipe_lifecycle.py` 鎖 yaml↔code SSOT 同步，比照既有 recipe-contract drift 紀律）。編譯器（`collect_lifecycle_notices`）對 in-use 的 deprecated/eol recipe 印 **non-fatal 警告**列受影響租戶——**deprecated/eol 既有宣告照常編譯、絕不靜默丟告警**（batch compiler 不因平台退役 recipe 而砍掉已部署租戶的 rule）。eol 的**寫入端拒絕採 inclusive 語意（「B2-寬」）**：tenant-api preflight 只拒**擴張** eol 使用（**per-eol-recipe 實例數不得增加** → 擋「新增」與「換用另一個 eol」、放行「改參數 / rename / 既有續存」），**不連坐**含 eol recipe 的整次 PUT——否則一個無關的舊 eol recipe 會在租戶凌晨救火時擋住新增救命告警（outage hostage）。**persona 不對稱**：hard-reject 只在 tenant-api preflight 有牙；CI/GitOps-direct compiler 無法分新舊 → warn-only。eol 寫入拒絕（Go preflight + `go:embed` 衍生 `recipe-status.json`，非手寫 mirror）+ portal 黃標 + 燃盡圖 info-metric `custom_recipe_info` 為 follow-up increment。設計詳 [ADR-024 §8](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts 部署上線（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S3b，ADR-024 能力 B，epic 真正收尾）**：把 custom-alert 向量化 pack **正式 commit + 部署**——S7/S8 routing gate 已 merge，rollout-order 硬閘門滿足，custom 告警現可在 prod fire。**source = LIVE conf.d**（`components/threshold-exporter/config/conf.d/`，exporter 實際服務的同一份 → recipe_id 對得上 emit 的 `user_threshold`；若編自 example tree 會脫鉤→永不 fire 的 #731 靜默類）。db-b 加 2 個 demo recipe（`mysql_global_status_threads_connected` threshold/**page** + `mysql_global_status_slow_queries` rate/**silent**——真實 mysqld_exporter metric + namespace→tenant 自動貼標；對抗式否決了「mariadb disk critical」因 mariadb 無 tenant-branded disk metric→dead demo）。三檔同 commit（3-copy 硬閘門）：`rule-packs/rule-pack-custom-alerts.yaml` + `k8s/03-monitoring/configmap-rules-custom-alerts.yaml` + `operator-manifests/da-rule-pack-custom-alerts.yaml`。**修一個 S7/S8 埋下、只在 pack commit 才觸發的硬 break**：`CustomRecipeSilent` sentinel 的 selector `user_threshold{component="custom",mode="silent"}` 無 `metric` matcher → `rulepack_contract_test.go::assertComponentMetric` 對該 sentinel shape 豁免 metric（其餘一律仍要）。**count 排除**：custom-alerts 是租戶自訂、非平台覆蓋 → 從 platform stats/alert-reference/rule-pack-README 三個 glob generator 排除（platform-data 的 `PACK_ORDER` 本就不含），badge 不變。**部署-pack promtool golden** `tests/rulepacks/rule-pack-custom-alerts_test.yaml`（測 committed pack 的 db-b 2 recipe + sentinel，跑 `make rulepack-promtool-test` + Go fixture guard 掃；tests/dx 編譯器 goldens 保留測 6 recipe 編譯器）。新 **drift 硬閘門** `custom-alerts-compile-check`（pre-commit + CI）：committed pack 必須與 live conf.d 源同步。詳 [ADR-024 §S7/S8](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **tenant-api SSE 部署後重連觀測護欄（issue [#740](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/740) / TRK-326，#677·#678 interim guard）**：#677（TRK-324）的 `strategy: Recreate`（消滅幽靈寫者）副作用是**每次部署硬砍所有 `GET /api/v1/events` SSE 連線**。單次重連是預期且自癒（SSE auto-reconnect + #143 heartbeat-hardened hub），但先前**無任何訊號**告訴 operator client 是否成功恢復、或卡在異常持續掉線。新增**輕量** alert `TenantApiSSEReconnectFailure`（`k8s/03-monitoring/configmap-rules-platform.yaml`，platform-self-monitoring group）：三條件 `for: 10m` → warning（皆對 `sse_clients`/`uptime_seconds` 做 `sum/min without (pod, instance, endpoint)` 聚合）—— 聚合 client `== 0` 且 **`min(uptime_seconds) < 1800`**（pod 近 30m 內重啟過）且 `max_over_time(sum(...)[30m:1m]) > 0`（近 30m 有過 client）。**刻意無誤報**：`uptime < 1800` 把告警錨在**部署窗**是 load-bearing —— 否則無法區分「重連失敗」與「使用者正常關掉 Tenant-Manager 分頁」（同樣是「曾有 client、現在 0」），低頻 admin UI 會狂誤報。**聚合是 silent-failure 防護**（Gemini 外審）：Recreate 換 pod 後若 scrape 帶 per-pod label（ServiceMonitor/endpoints role，本 repo threshold-exporter 已用），新 pod 是不同 series、30m 歷史全 0、`and` 精確 match 失敗 → alert 靜默永不觸發；聚合掉易變 label（單寫者＝單 pod 安全）讓歷史橫跨重啟，對任何 scrape role 都正確。正常單次重連（秒級回升）/ 無人連線 / 非部署期關分頁（uptime 大）皆不觸發；client 回連或 uptime 過 30m 即自動 resolve。**復用既有 `tenant_api_sse_clients` gauge**（無新 metric），且**同時坐實 #678（TRK-325 讀寫拆分 CQRS）的可量測 defer-trigger**——sustained 並發 clients > N over 7d 即「read-HA 成真實需求」。behavioural contract 由 promtool 測試 `tests/rulepacks/platform-sse-reconnect_test.yaml`（fire / 快速重連不誤報 / idle 不誤報）鎖定。明確 **out of scope**：讀寫拆分 / `TA_READ_ONLY`（那是 #678）。doc：[tenant-api-hardening.md §5.6](docs/api/tenant-api-hardening.md)。
- **Custom Alerts routing / 靜音 / 隔離（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S7/S8，ADR-024 §S7/S8，epic 收尾）**：Alertmanager 端消費資料平面早已帶到 alert 的 `mode`（page/silent）label。**核心 reframe（專案引導者直覺觸發、Gemini 六波外審收斂）**：silent **不**走 route-to-null，而是**完全複用 ADR-003 既有 Sentinel+Inhibit 三態典範** —— 消除 YAML `null`/discard-receiver footgun、與平台一致、AM UI 可見度更高。**三軸全 reuse**：maintenance（recording-rule 層 `unless user_state_filter{filter=maintenance}`，已 ride）· 租戶級 silent（`TenantSilent*` sentinel 既有 inhibit 已連帶涵蓋 custom）· **per-recipe silent（唯一 net-new）**＝編譯器在 `custom-alerts` group **注入一次**全域 sentinel `CustomRecipeSilent`（`max by(tenant,name)(user_threshold{component="custom",mode="silent"})`、`severity:none`）+ AM inhibit `equal:[tenant,name]` → **只**抑制該 (租戶,recipe) 通知，同 shape 的 page 租戶照常通知；靜音者仍是 `ALERTS{...,mode="silent"}` 時序 → **silent ≡ dashboard-only 告警**。**判別子（A1）**：每條 `Custom_*` alert 加靜態 `component="custom"` label（平台零 `component`，精確 match 無歧義）。**路由（靜態，base config）**：`component="custom"` route **居首 + `continue:false`** → page 落專屬 `custom-alerts-firehose`、`group_by:[tenant, alertname]`、debounce `30s/5m`。「居首」是 **gate 正確性必需**：enforced NOC route 是 `continue:true` + matchers optional（無 matcher = match-all）→ 不先攔則 custom 漏進平台 NOC。**firehose MVP = 空 receiver**（與平台 `default` 隔離，AM UI + `ALERTS` series 仍可見）；接 outbound 時須 **webhook adapter → log backend（非 Slack/PagerDuty**，避 429→AM queue 堆積→OOM→平台 P0 發不出；接死的 placeholder URL 會自釀此 retry-OOM，故 MVP 留空最安全），屆時設 `send_resolved:false` + `max_alerts`（truncate cap）。**Durability 強健化**：generator 組裝對 `route.routes` 是 **REPLACE 非 append** → 純放 base config 的 route 會被 `--apply` 靜默清掉，故在**組裝層** `_inject_custom_alert_isolation` prepend（idempotent，兩條輸出路徑都恆在居首）。`group_by` **駁回 radar 舊註的 `recipe_id`**：`alertname=Custom_<recipe_id>` 已含 metric → 不同 metric 必不同 alertname、不揉成一團，而 `recipe_id` **非 alert label**（放 group_by 會 group-by 不存在 label）。Go/Python 測：compiler unit（component label + sentinel 注入一次）+ promtool（sentinel 只為 silent 租戶 firing）+ ops 組裝注入（空 routes 仍注入 / idempotent / `--apply` 保留 + silent inhibit 存活）。**⛔ rollout-order gate**：S3b 部署 live custom pack 必須相依本切片先 merge。詳 [ADR-024 §S7/S8](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts tenant-manager modal（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S6b-2b，ADR-024 §S6b-2）**：portal `tenant-manager` 新增 **Custom Alerts** live 編輯器（單選租戶 → 開 modal）——列現有 recipe、mount `<RecipeBuilder>` 新增/編輯/刪除、一鍵 commit 回 GitOps（打 S6b-2a 端點）。**client 只處理 JSON**:GET `/tenants/{id}` 現回結構化 `custom_alerts`(後端 `customalerts.Extract`),前端零 YAML parse。**九道前端防禦**(Gemini 三+四波外審):JIT fresh fetch(modal-open 重抓 source_hash,讓 OCC 真可用)· 嚴格 `isSubmitting`(防 double-submit 假 409)· dirty-guard(攔 backdrop/ESC/X 誤關抹除)· 409 非破壞性(保輸入 + 複製備份)· 400 Violations 標出問題 recipe(含既存毒藥)· name-based rename-safe(`onSubmit(recipe, originalName)`)。Vitest ×10。詳 [ADR-024 §S6b-2](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts tenant-manager 寫入端點（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S6b-2a，ADR-024 §S6b-2）**：新增 `PUT /api/v1/tenants/{id}/custom-alerts` —— 讓 portal recipe-builder 把租戶 recipe **commit 回 GitOps**(不只產 YAML)。**後端擁有 YAML round-trip,前端只送 JSON 陣列**(portal 無 serializer + PUT full-overlay 的雙 blocker 解法)。server:`base_hash` 樂觀並發(整檔 hash 不符 → **409**)→ **yaml.Node AST 手術**替換/刪除 `_custom_alerts`(**保留註解/縮排**、空陣列乾淨刪 key、canonical key 序 + `value:severity` 引號)→ **S5 `ValidateTenantCustomAlerts`** 整陣列(無效含既存壞 recipe → **400 + Violations[]** 可定位)→ 經既有 `gitops.Writer` commit。MVP direct mode;**PR mode → 501**(該 persona 用 S6b-1 standalone + PR flow)。GET `/tenants/{id}` 新增 `source_hash`(供 client 取 base_hash)。**Spike gate** 先驗 yaml.v3 註解保留(`internal/customalerts/merge.go`)再動工 —— 非 struct-Marshal 抹除版。Path B(機器專屬 sidecar)列 future-radar。詳 [ADR-024 §S6b-2](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts `RecipeBuilder` portal 表單（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S6b-1，ADR-024 §S6b）**：新增**免 PromQL** 的 portal 工具 `recipe-builder` —— 選指標、選 recipe（6 種：threshold / rate / ratio / absence / p99_latency / forecast）、填參數即產出合法 `_custom_alerts` recipe。心智模型 **Smart Form, Dumb Handoff**：純 `(Context)=>RecipeObject` 元件、**不擁有寫入**（S6b-2 才折進 tenant-manager 的 PUT+S5 寫入路徑）。本刀服務 **GitOps-direct persona**：吐帶完整 `tenants:<id>:_custom_alerts:-` wrapper 的 copy-paste snippet（消除「貼哪裡」摩擦）。前端防線（外審四暗礁全納）：**enum 全部 derive 自 `tenant-config.schema.json`**（`recipe-enums.json` + Vitest drift-guard，零 hardcode、schema 漂移即 CI 紅）；每個 metric 欄（metric / denominator_metric / capacity_metric）**獨立 debounce+AbortController fetcher** 打 S6a discovery；ghost validation 與 autocomplete 清單**脫鉤**（blur 精確查 + `Validating…` 中間態，async race 不誤報）；**白話摘要狀態機**（必填齊且合法才渲染動態句、永不露 PromQL/series）；discovery 不可用優雅降級（手打 + 提示）。client 驗證純 UX（S5 preflight + CI 才是權威）。詳 [ADR-024 §S6b](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts metric discovery 端點（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S6a，ADR-024 §S6）**：新增唯讀 `GET /api/v1/tenants/{id}/metrics?q=<prefix>`——列出該租戶**自有** app-metric 名稱(24h lookback，可選名稱前綴過濾)，給 portal recipe authoring UX 當 metric 自動完成來源(租戶不需懂 PromQL)。**無狀態 Prometheus proxy**(複用 admission validator 的 triple-bound 模式:5s timeout + `io.LimitReader` 1MB + `limit=` → label-values metadata API，index-only 不讀 sample chunk)。**跨租戶隔離兩層**:route middleware 強制 caller 對 `{id}` 的 RBAC read + discoverer **server-side 強制鎖** `match[]={tenant="<id>",__name__=~"^<q>.*"}`(`tenant` label 由 scrape 端 branded、不可偽造)。**注入雙重防線**:`q` 以 metric-name **charset 白名單**(`[a-zA-Z0-9_:]` + ≤256 長度)驗證、非法回 **400**(同時擋 quote-break 與 regex-metachar);`tenant` 值在 selector 邊界 **escape**(backslash-first → quote，鏡 Python `_escape_value`)——因 RBAC open mode 對任何 tenant id 放行 read，惡意 path id 含 `"` 否則可跳脫 literal 注入跨租戶枚舉,escape 使其 correct-by-construction 不可能。**lookback 24h** 涵蓋 daily batch/CronJob 間歇 metric。discovery 共用既有 per-caller rate limiter(`TA_RATE_LIMIT_PER_MIN`，預設 100/min → 429)+ per-query 三護欄;專屬更嚴限流為 defer-with-trigger(§S6 Reef 1，避免重造限流子系統)。新增 helm **opt-in egress** `networkPolicy.egress.enabled`(預設 false → 維持 Ingress-only/egress 不限制的歷史 posture;⛔ 設 true 會 deny-by-default 所有 egress，須補 K8s API/git forge 的 `extraEgress`)。詳 [ADR-024 §S6](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts tenant-api shift-left preflight（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S5，ADR-024 §S5）**：tenant-api 在 GitOps 寫入**前**用 **in-process Go** 驗證租戶 `_custom_alerts` recipe,無效回 **HTTP 400**(壞 recipe 絕不進 repo)。**不打包 promtool/Python 進 image**(deviates 原 ADR AC#2 字面;複用既有 prod Go 驗證器 `pkg/config.ValidateTenantCustomAlerts` = `resolveOneCustomAlert` per-recipe + within-tenant name/severity/slug 唯一性 + own-recipe cap)。掛進共用 `gitops.validate()` → **PUT/batch/dry-run-validate 一致覆蓋**。**兩層架構**:Go preflight(stateless per-tenant 輸入閘、fail-closed)→ CI compiler(stateful 全域權威 + promtool 守模板 regression)。新增**跨語言驗證契約** fixture(`custom_alert_validation_vectors.json`,Go 與 Python 雙邊斷言 accept/reject 一致 —— 關掉 slug golden vector 未覆蓋的驗證決策 drift)+ promtool 對抗測(selector value 含 `"` → escaped 合法 PromQL)。cross-inheritance 碰撞 + 模板 bug 留 CI(SOT 權威,OQ-S5-1)。詳 [ADR-024 §S5](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts `max_custom_recipes` per-tenant cap（成本護欄，[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S4）**：編譯期強制每租戶**自有**(own) custom-alert recipe 數上限（預設 20、`--max-custom-recipes` 可調，待 benchmark 反推）;超過 → `CustomAlertConfigError` **fail-loud**（非靜默截斷——GitOps PR 明確報錯、可行動）。**只計 own**:繼承的 platform/domain 政策 recipe 是**向量化**（整棵子樹共用一條規則、O(1) 於租戶數）→ 不佔租戶 cap;封住的是 own unique-metric recipe 的 N×cap 規則爆炸。`loader.collect_instances` 標記 own/inherited、`build_shapes(max_custom_recipes=)` 強制。詳 [ADR-024 §Custom Alerts](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts `forecast` recipe（趨勢/耗盡預測，[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741)，ADR-024 能力 B 第 6 個 recipe）**：第一個「預測型」recipe——線性預測 gauge/餘量比例會否在提前量內越過閾值（典型：磁碟/記憶體耗盡）。**雙模式**：有 `capacity_metric` → 比例 mode（預測 `avail/capacity` 掉破 (0,1) 的 floor，仍有餘量時即**提前**響）；無 → 原始值 mode（預測 gauge 穿越絕對門檻）。**租戶只填 `horizon`（enum `1h/2h/4h/12h/24h/48h`），不填 lookback**——平台推導 `lookback = max(2·horizon, 1h)`（整數秒，杜絕 `1.5h` 壞 duration），砍掉 naive `predict_linear` 最大 foot-gun。**防 false-positive**：長平滑 lookback + `for` sustain + **cold-start 資料量 gate**（`count_over_time(base[lookback]) > 3`，sparse/剛部署不亂跳）；比例 mode threshold 強制 ∈ (0,1)（防 floor≥1 + op`<` 永遠觸發）；gauge-only（counter reset 會壞斜率）。`horizon` 進 recipe_id slug（Go `RecipeID` + `shape.py` + golden vector **雙邊**鎖；`forecast` 用 `h{horizon}` 取代 `w{window}`、`capacity_metric` 復用 `den_` 槽）。promtool golden 以**真實 K8s scrape label** 驗證 lead-time 觸發 + 穩定租戶靜默。詳 [ADR-024 §Forecast Recipe](docs/adr/024-version-aware-threshold-via-dimensional-label.md)、[recipe 治理契約](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/recipes/forecast.yaml)。
- **Custom Alerts exporter emission（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S3a，能力 B data-plane）**：threshold-exporter 現會解析 tenant `tenants.<id>._custom_alerts` 宣告並 emit `user_threshold{component="custom", metric=<metric>, severity, recipe_id=<shape slug>, name, mode}` 系列——使 S1+S2 編譯器產的向量化規則真能 join 到資料（**label 形態 A**：`recipe_id` 當 shape 消歧 selector label，`metric` 保留真實指標名）。`recipe_id` slug 為 **Go↔Python 跨語言契約**（`pkg/config/custom_alert.go::RecipeID` 逐字 port `shape.py`，由 `tests/dx/fixtures/recipe_id_vectors.json` golden vector **雙邊**鎖定）。防禦：YAML list passthrough 不再 kill 整個 tenant 檔（`parse.go` SequenceNode 分支）；metric 嚴格 regex + selector 保留 label 封鎖（防注入）；malformed 宣告 **fail-loud** → 新 gauge **`da_custom_alert_parse_errors{tenant}`**（不靜默吞噬，可 `> 0` 告警）；`threshold: "disable"` 三態 opt-out（不 emit series、**不**算 parse error）；溢出截斷決定性（`truncationSortKey` 既有 `canonicalLabelKey` 已折入 recipe_id/name/mode）。#731 closed-label 契約 **surgically 放寬**：`recipe_id`/`name`/`mode` 僅在 `component="custom"` 時放行（全域守門員不動）。**範圍**：tenant-level；platform/domain `_defaults.yaml` 繼承 emission + 部署 committed pack 留 S3b。詳 [config-driven.md §2.15](docs/design/config-driven.md)。
- **ADR-024 擴充：折入 Custom Alerts 設計（產品北極星，epic [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) 延伸）**：ADR-024 從「version-aware threshold 單一功能」重構為「**宣告式 dimensional 告警引擎 + 兩個能力**」——能力 A version-aware（平台 authored，已 merge）、能力 B **custom alerts**（租戶用平台 authored 的**參數化 recipe** 自訂告警，**非寫 PromQL**、守 declarative-only 地基；設計收斂 + Pass-4 外審，實作未起）。**刻意不分 Phase**（兩者同屬 v2.9.0，差別只在實作現況）。核心決策：Level 1 recipe MVP + metric onboarding 納入 scope；metric catalog = 唯讀 discovery（併吞 [#716](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/716) `/effective`）；編譯權威留 CI + tenant-api **shift-left promtool preflight**；跨租戶隔離靠 scrape-stamp `tenant` label 結構性解；`mode:[active|silent]` dry-run 復用既有 Shadow Monitoring inhibition。**folded-not-ADR-025**（共用引擎 / tenant-api 寫入邊界 / CI pipeline）。實作追蹤 epic [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741)。詳 [ADR-024 §Custom Alerts](docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
- **Custom Alerts 向量化編譯器核心（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) S1+S2，能力 B 第一刀）**：交付**編譯器本身**——recipe 庫（5 個平台 authored recipe：`threshold`/`rate`/`ratio`/`absence`/`p99_latency`，`rule-packs/recipes/`）+ conf.d `_custom_alerts` 宣告語法（schema 擴充 `customAlertInstance`/`customAlertList`）+ **向量化編譯器** `compile_custom_alerts.py`（`make custom-alerts-compile`）。編譯器把宣告依 **shape signature** 分組，**每個 shape 產一條** `app_metric > on(tenant) group_left(...) user_threshold{recipe_id=...}` 規則（規則數 = shape 數、**非租戶數** → 守 [benchmarks.md §2](docs/benchmarks.md) 的 rule-pack O(M)）。對抗 review（Gemini 多輪 + 自挖）收斂的防禦：metric 嚴格 regex + 安全 `selectors`/`selectors_re` 組裝（杜絕 PromQL 注入、救活 rate/ratio 的 label 過濾）、ratio 分母 `(>0)` 除零護欄（回空集不噴 +Inf）、absence 以 threshold series 自我圈定（scope 不洩漏）、severity 租戶決定（不強制鏡像）、`recipe_id` 跨語言 slug 契約（golden vector）、**`mode`（page/silent）搭資料平面 `group_left(name, mode)`**（不入 shape signature、守 O(M)，避免同 shape 不同 mode 兩租戶共用規則時 S8 無從區分的路由死鎖）。pytest ×24 + promtool 行為 golden ×6（fire/no-version 主流路徑/fallback/maintenance/selectors/除零/scope 隔離/quantile/**mode 雙租戶路由**，pytest 編譯 example→temp pack 後跑 promtool）。**不在本切片 commit 已部署 pack**：repo 的 #731 closed-label 契約（`rulepack_contract_test.go`）證明「committed pack」與「exporter emit `user_threshold{recipe_id}`」被結構性耦合——故 pack 產生 + exporter emission + 最終 label 形態整包留 **S3**（連同 Go `validReservedKeys` 註冊 + 契約更新）。詳 [`rule-packs/recipes/`](https://github.com/vencil/Dynamic-Alerting-Integrations/tree/main/rule-packs/recipes) / [config-driven.md §2.15](docs/design/config-driven.md)。
- **`threshold-recommend --export-patch` — STAGE-1 conf.d override 輸出（[#720](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/720)，#457 DEC-F R0 採納）**：新增 `--export-patch` flag，把 `threshold-recommend` 的結果輸出成一段 `tenants:`-rooted、可直接 merge 進 `conf.d/<tenant>.yaml` 的 override 片段。**只含有實際建議的 key**（`|delta| ≥ 5%` 且有 observed-map 對映、上界 `>` metric）；within-margin / 未對映 / 下界(`<`) / version-aware 略過的 key 以**註解**列出（透明、不污染可套用 YAML）。operator review → 套用 → 自開 PR 後，既有 `backtest.yaml` CI 自動貼 old-vs-new 觸發次數風險報告——閉合 #457 規劃的 calculator→conf.d 資料流（STAGE-1 價值基石）。採 **T1 advisory fragment（0 新依賴、零 YAML-edit 風險）**；in-place ruamel round-trip 編輯為 defer（#457 R0 §5 / #721）。值為 quoted string、整數不帶小數點（對齊 conf.d 慣例）；輸出經 `yaml.safe_load` round-trip 驗證合法。雙語 cli-reference 同步。詳 [#720](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/720)。
- **portal dist bundle repo-wide 重建 + CI 同步閘門（#444 / TRK-239 衍生）**：production 在 single-component mode 無條件載入 committed `docs/assets/dist/*.js`，而 `mkdocs-deploy` 部署時**不**重建 dist —— 但 committed dist 自 monorepo restructure（b439c427）後 **repo-wide stale**，多個工具 .jsx 改了 source 卻沒重建 dist（含 [#726](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/726) dependency-graph 分類色 token 遷移、[#727](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/727) token migration B1-B3，此前只在 source 未上線）。本 PR 在 canonical 環境（dev container，esbuild `0.24.2` pinned）重建全部 43 entry 讓 production 追上 source（73 改 / 8 新 content-hash chunk / **24 個 orphan chunk 檔刪除**）。**根治 stale**：(1) `tools/portal/build.mjs` 於 build 前清 `DIST_DIR`（杜絕 content-hash chunk 改名遺留的 orphan 累積——正是這 24 檔的成因）；(2) `ci.yml` 的 portal-tests job 新增 `git diff --exit-code docs/assets/dist/` 閘門——source 改了卻沒重建 dist 會 CI red（先前只 `npm run build` 驗「可建」、不驗「committed == fresh build」，stale dist 靜默過 CI）。build 經 **Node 20 == Node 24 byte-identical 實證**（CI 與 dev container 同 esbuild linux-x64 binary），閘門不 false-red。詳 [#444](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/444)。
- **dependency-graph 節點分類色改用 design token（#444 token-migration epic 衍生）**：`dependency-graph` 工具的 `CATEGORY_COLORS`（infra / database / middleware / runtime / custom 五類 × bg/light/text 共 15 個硬編 hex）改用新設的 `--da-color-dep-<cat>-{bg,light,text}` token，於 `docs/assets/design-tokens.css` light + dark 兩主題定義（dark 值依既有 light→dark 慣例推導：bg→brand-400、light→該色相 soft-dark、text→bright-200，WCAG-AA ≥7:1）。**使用者可見改善**：此前節點色為固定 hex，dark mode 下不切換；現隨主題自動切換。此為**獨立語意軸**（節點類型），與 icon-*/journey-*/mode-* 不重疊。`check_design_token_usage` 對該檔歸零、`check_undefined_tokens` 全綠。設計系統文件見 `docs/internal/design-system-guide.md` §3.6。⚠️ portal dist bundle 由 `make portal-build` 另行重建（repo-wide stale，不在本 PR churn）。詳 [#444](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/444)。
- **Portal JSX 硬編碼 hex/px → design token 遷移完成 + token gate 3 個 false-positive/negative 根治（#444 Phase 1，epic 收尾）**：portal 互動工具的硬編碼色碼 / px 數值全面遷移到 `var(--da-*)` token，**repo-wide `check_design_token_usage` 歸零**。分批 PR：[#727](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/727)（`_common` 共用元件 + multi-tenant-comparison + layout px；含 fallback 移除順帶修正 2 個「同 token 配不同 fallback」語意 bug）、[#730](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/730)（tenant-manager cluster + fontSize px → `--da-font-size-*`）、[#726](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/726)（dependency-graph 分類色，見上）。**使用者可見改善**：先前硬編碼色在 dark mode 不切換，遷移後隨主題自動切換。過程中根治 gate **4 個量測 bug**（全附 regression test）：(1) JSX 搬到 `tools/portal/src/` 後 gate 掃舊 `docs/` 路徑 → 空掃永遠綠（[#722](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/722)）；(2) `width: '100%'` 被誤判為 `100px`（drop unit）；(3) `/* */` block comment 與 jsx-loader YAML frontmatter 內的 `#NNN` issue 引用被誤判為色碼（單批 22 findings 中 14 個是此 FP）；(4) URL `https://` 的 `//` 被當行註解 → blank 掉同行後續真違規（adversarial self-review 抓到的 false-negative）。**gate 維持 diff-only fatal**（(b) class，符合 lint-policy §3；既有違規 Phase 1 清零、新增行 diff-only 即擋，full-scan 對防債零增益卻重引 collateral damage）。token 遷移耐久準則上抬 [`lint-policy.md` §7](docs/internal/lint-policy.md)（一次性逐值對照 cheatsheet 已歸檔）。詳 [#444](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/444)。
- **DEC-F 跨版本拍板 — Rule Pack × threshold-calculator 資料流（RFC [#457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/457) R0）**：v2.8.0 planning §10 遺留的「未定」DEC 經 R0 三方 review（Claude 評估書 + Gemini 對抗式外審 + maintainer 採納）定版為 **definitive**。**STAGE-1 採納** `threshold_recommend --export-patch`（吐可 `git apply` 的 conf.d unified diff；~100 LOC / 0 新依賴，自動繼承既有 `backtest.yaml` CI 的 old-vs-new 風險報告——閉環其實已建 ~90%，缺口僅一個 patch formatter；另開實作票）；**DEFER** 全自動 da-batchpr PR adapter（省的只有 operator commit、帶 auto-merge 漏報 blast-radius、無客戶拉力 → trigger：客戶 toil / RFP / maintainer 主動 poll）；**REJECT** 寫回 rule pack schema / `calculator:` 子段（破壞 declarative 純度 + 衝擊客戶 GitOps repo——閾值 operative 數值屬 conf.d 領域，rule pack 僅持 doc-comment 鏡像）。另識別**前置票**（先驗 `threshold_recommend` 的 `build_metric_query` 取 `user_threshold`＝閾值設定歷史而非觀測負載）+ Future Work（drift lint / global default drift 報表 / portal inline）。本 issue **不 ship feature code**（decision-only）。完整論證 + Gemini take/reframe/reject ledger 見 [#457 R0 結案 comment](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/457#issuecomment-4587096761)（依「issue = 決策記錄、repo 只留 operative 殘渣」原則，repo 不另存評估書）。
- **Bench gate Phase 2 — 降 informational + nightly sustained-trend watchdog + 移除 override 繃帶（接續 Phase 1）**：Phase 1 的交錯執行 + control canary 讓偵測可信後，PR bench gate **不再阻擋 merge**——真 regression（主 bench ≥5% 顯著退化且 canary 穩定）改為自動貼 PR comment + 上 `perf-regression` label（regression 清掉後自動移除），job 永遠 exit 0。fork PR 的 read-only token 會讓 comment/label 失敗 → 降級成 `::warning::` + step summary（**不**用 `pull_request_target`，避免 pwn-request）。因為沒有 gate 要 override，`override: bench-regress-ok` label + 整個 `bench-override-audit.yaml`（角色驗證 + stale-label strip 繃帶）**一併移除**，`labeled` trigger 與 preflight override-check 也拆掉。main 的**持續退化**改由 `bench-record.yaml` 新增的 `trend-watch` job 守望：`analyze_bench_history.py --trend-watch` 比對最近 14 晚，**雙判據**——R1 sustained（最近 K 晚全高於**錨定** baseline，非「跟昨天比」）+ R2 creep（最近窗典型晚高於窗內最佳晚，抓「每晚只退 2% 累積卻大」的慢性 creep），floor = max(5%, min(3× control canary 夜間 CV, 10% 封頂))（nightly 也跑同一支 canary 建噪音地板，小於 runner 自身噪音的移動永不告警；canary 貢獻封頂避免噪音 runner 反把真退化消音）；sustained 才自動開 `perf-trend` issue（指派 maintainer = email）、perf 回到 baseline 自動關閉（closed loop，gh write 全 best-effort 不 red nightly）、單晚 blip 被多晚窗過濾、bench 從最近窗消失（perf timeout 徵兆）直接 skip 不誤判、已開則 comment 不重開。**離線實證**：`--trend-watch --fixture-json` 七情境全綠（sustained +11% / 單晚 blip 靜默 / +6% 落 canary 噪音地板靜默 / +2%×9 晚 creep 抓到 / 回 baseline 關 issue / 消失的 bench skip / +15% 真退化在 8% 噪音 canary 下仍開 issue）。⚠️ **本 PR stacked 於 Phase 1，須待 Phase 1 實跑數輪綠後才 merge**。詳 [`benchmark-playbook.md`](docs/internal/benchmark-playbook.md)。
- **Bench gate 根治 false-positive — 交錯執行 + control canary（Phase 1，issues [#502](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/502)/[#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)/[#611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/611)/[#695](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/695)）**：PR bench gate（`bench-gate-pr.yaml`）在共享 GitHub runner 上反覆假性 RED 的**根因不是 path/門檻、而是 v5「先跑完 base 再跑完 pr」的 sequential 拓樸**——base/pr 分兩個時間塊，runner 中途漂移（thermal/頻率/鄰居）系統性偏置 pr 那塊，破壞 benchstat 的 independent-samples 假設、把時間相關 bias 誤讀成 regression（#695 零 Go 改動照樣紅）。改為 **v6：單 runner 交錯執行（base,pr,base,pr,…）**——新 `scripts/tools/ops/bench_interleave.sh`，用 `go test -c` 預編譯（剝離編譯熱負載）、兩個 git worktree 同時 checkout、**移除原本不對稱的 mid-loop drop_caches**（交錯後對稱暖快取，保留它反而偏置 pr）。任何時間相關漂移同時打兩邊 → 抵銷 → benchstat 假設恢復。加 **control canary**（`scripts/tools/ops/bench-canary/` 自帶 go.mod、stdlib-only、stash 到 checkout 外、base/pr 共用同一份）：`BenchmarkControlCanaryCPU` 為 gating（base↔pr 顯著漂移 ≥4% / 缺席 / 異質 CPU → 判 **INCONCLUSIVE re-run**，非 regression），`BenchmarkControlCanarySleep` 為 informational-only（µs 級排程 jitter，gating 會狂閃）。真 regression = 主 bench ≥5% 顯著退化**且** canary 穩定，Phase 1 仍 blocking。新增 `make bench-interleave` 本機 smoke（已在 dev container 實證：5 binary 編譯+交錯+canary+benchstat 解析全通；CPU canary ~0.7% spread / Sleep ~4.5% jitter 驗證 gating/informational 切分正確）。Phase 2（降 informational + nightly trend watchdog + 移除 override 繃帶）另 PR。詳 [`benchmark-playbook.md`](docs/internal/benchmark-playbook.md)。
- **Version-Aware Thresholds 使用攻略 + ADR-024 定稿（docs，epic [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)）**：ADR-024 status `proposed`→`accepted`（Phase 1 kubernetes pilot `container_cpu`+`container_memory` 已上線，逐階段 As-built 進度入內文）。新增雙語使用攻略 [docs/scenarios/version-aware-thresholds.md](docs/scenarios/version-aware-thresholds.md)——租戶宣告語法 / emergent cutover / 動態降級 / ⛔ pilot scope（只 `container_cpu`+`container_memory`，非-pilot metric 寫 `{version=}` 被 da-guard 拒），外加獨立 **For Platform Operators: KSM Allowlist Remediation** 搶修專區。**lifecycle JIT 投放**：`VersionAwareThresholdInert` sentinel 的 `runbook_url` deep-link 該搶修 anchor，平台 SRE 被告警叫醒即直達修復步驟。⚠️ **租戶採用版本語法前，請先與平台團隊確認 kube-state-metrics 已設 `--metric-labels-allowlist=pods=[app.kubernetes.io/version]`**——否則版本閾值靜默失效（sentinel 為 runtime 安全網、`check_ksm_version_allowlist.py` 為 CI static 攔截）。
- **[ADR-024](docs/adr/024-version-aware-threshold-via-dimensional-label.md) 草案：Version-Aware Threshold — 透過既有 Dimensional `version` Label 達成 Declarative Cutover（epic [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)）**：把 #423（rfc+epic）三輪設計討論收斂成雙語 ADR 草案（status: proposed，v2.9.0 GA 時定稿）。**核心現況校正**：既有 dimensional-label 機制（`container_cpu{version="v1"}: "80"`）已能在 threshold-exporter parse/emit **零改動**下產出 `user_threshold{...,version="v1"}` 目標 metric shape，故 Phase 1 核心為 **rule pack normalize layer**（`:vlabeled` recording rule + `by(tenant, version)` join + `label_replace(..,"version","default","version","^$")` 補 fallback，**不用** `or vector(0)`），而非新 config schema——即 **Option A reuse-over-build**，`versioned:` 專用 block 降為 defer-with-trigger。cutover 為 emergent（升版後 metric 帶哪個 `version` 就 join 對應閾值），自動免疫 K8s rolling/rollback/GitOps 傳遞延遲。與 [`config-driven.md` §2.6 排程式閾值](docs/design/config-driven.md) 正交並存（時間軸 vs 狀態軸），#423 §4 R1 已否決把 absolute date 塞進 `ScheduledValue`。最高風險為 observed-but-not-declared = silent alerting gap（`version_unknown` sentinel 即時 emit 緩解）。經 architecture 技能組織 + 兩輪外審（對抗式子代理 + Gemini Pass-2）打磨：動態降級 fallback（消 silent-gap）、多嚴重度保留、確定性截斷（消 flapping）、非 pilot pack 防禦硬化、sentinel buffer。OQ 待辦：OQ-1 pipeline 契約需 tenant team 簽核、OQ-6 guard scope 限 pilot 元件。同步 ADR README 索引（中英）。
- **Version-Aware Threshold 在 rule-pack-kubernetes pilot 落地（ADR-024 核心 feature，AI-2，epic [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)）**：ADR-024 的真正目標——讓 tenant 預先放多版本閾值、配合 app 升版「emergent cutover」生效——首次在 kubernetes pilot pack 以 PromQL 落地並 **promtool 實證**。實作四層：**(0a) version 注入**——`tenant:container_cpu_percent:by_container` 先算純百分比、最外層單次 `* on(namespace,pod) group_left(version) label_replace(kube_pod_labels,"version","$1","label_app_kubernetes_io_version","(.+)")` 注入 `app.kubernetes.io/version`（省一半 join、避滾動瞬間分子分母版號漂移吐 NaN）；**(0b) 透傳**——`pod_weakest` 加 `by(...,version)`；**normalize**——`tenant_version:pod_weakest_cpu_percent:vlabeled` + `tenant_version:alert_threshold:container_cpu` 皆 `max by(...,version[,severity])` + `label_replace(..,"version","default","version","^$")`（未版本化→default，**非** `or vector(0)`）；**Route 2 per-severity 精確-or-降級 core**——固定 severity 使 threshold RHS 退化 singleton → 精確分支 one-to-one、fallback many(版號)-to-one（`group_left()` 保留真實版號 v2/v3 供 on-call），**避開 version×severity many-to-many 死鎖**（ADR Pass-3）。alert 沿用 PR3-pre-3 的 metadata left-outer-join（真空期裸火）。新增 `PodContainerHighCPUCritical`（per-severity 鏡像，保留既有 `PodContainerHighCPU` 名）。**promtool 實證**（`tests/rulepacks/rule-pack-kubernetes-version-aware_test.yaml`，**不同 pod 名 app-v1-x/app-v2-y 並存**模擬 rolling）：AC-2 v1 pod join v1 閾值(80)/v2 pod join 收緊 v2 閾值(60) 各自對齊、AC-3 v1+v2 並存 exact-match 不崩、fallback v3→default(80) 保版號、critical 不交叉誤發；既有 test 補 `kube_pod_labels`（無 version→default）驗 **inert-by-design**（未版本化行為與改造前等價）+ mutation dogfood（破 fallback→v3 降級失效）。**scope：CPU pilot**（memory mirror + 13 pack sweep = Phase 2）；configmap regen + kind e2e（AC-3/4/9）= PR3b。
- **da-guard 雙語 `version` dimensional label 驗證（ADR-024 AI-3 / OQ-6，epic [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)）**：dimensional key 的 label value 過去**完全無驗證**（`parseLabelsStringWithOp` 照單全收）。本 PR 對 `{version="..."}` 加 OQ-6 護欄，**Go 與 Python 雙語同步**（ADR 要求的「雙語 da-guard」）：(1) charset regex `^[a-z0-9][a-z0-9._-]*$`（Phase-1 baseline，**pilot-calibratable**——真實 `app.kubernetes.io/version` 可能含大寫 / 長 Git SHA，pilot 觀察後放寬）；(2) 禁用保留值——空字串（與未版本化 baseline 碰撞）與字面 `default`（保留給 normalize-layer fallback）；(3) **元件 scope 白名單**——`version` label 僅允許用於 pilot metric（`container_cpu` / `container_memory`），非 pilot 元件寫 version key 會 warn（防跨 pack `sum by(tenant)` 跨版號 double-count）；(4) 只收 exact `version="..."` selector，regex matcher 標記。**Go 側**（`pkg/config/resolve.go::validateVersionLabel`，串進 `ValidateTenantKeys`）為 exporter config-load 時的 observability 警告；**Python 側**（`scripts/tools/ops/_grar_validate.py::_validate_version_label`，串進 `validate_tenant_keys`）為 da-guard schema 警告（CI 可 `--warn-as-error` 升 reject）。兩側註明須保持同步；Python regex 以 `[{,]` anchor 避免 `app_version="v2"` 子字串誤配（Go 走 label-map parse 無此問題）。Go 8 + Python 8 test。Phase 1 為 warn-level（OQ-6 pilot 校準 regex 後再升 hard reject，避免誤攔真實部署）。
- **tenant-api real-forge E2E — Track 2 GitLab CE（issue [#616](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/616)，姊妹票 [#615](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/615)）**：新增 build-tag 隔離（`//go:build forge_e2e`）的 real-forge E2E（`components/tenant-api/test/forgee2e/`），補 httptest mock（#615）測不到的環境擬真；正常 `go test ./...` 不編不跑、env 未設時全 SKIP（CI 永不打真 forge）。**GitLab 端已對 `gitlab/gitlab-ce:18.11.3-ce.0` 實測全綠**：真分頁（seed >100 MR → `ListOpenPRs` 翻頁不漏不重）、真 403（read_api token → `insufficient_scope` → clean `platform.ErrForbidden`、無 body 外洩）、full-loop（branch→commit→MR→list + `CreateBranch`/`DeleteBranch` lifecycle）。配套 [`scripts/ops/forge_e2e_run.sh`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/ops/forge_e2e_run.sh)（pinned image、`docker inspect` 就緒判斷、phantom-readiness retry、Files-API `start_branch` 原子 seeding 避 Gitaly race、logrotate boot retry、`/var/log/gitlab` 失敗 artifact）+ nightly workflow `forge-e2e-gitlab.yaml`（02:00 UTC）+ PR-CI compile-check（防 tag-gated code 腐爛）。**Track 1（GitHub per-PR/post-merge）為 follow-up**——待 dummy repo + scoped PAT secret provision；嚴禁 `pull_request_target`（pwn-request 洩 PAT）。雷與起手式見 [`testing-playbook.md` §Forge E2E](docs/internal/testing-playbook.md)。
- **tenant-api real-forge E2E — GitHub >100-PR 真分頁（issue [#636](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/636)，補完 #616 DoD；Track 1 的 403/full-loop/janitor 已隨 [#634](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/634) 落地）**：補上 GitHub 端真實 Link-header `rel="next"` 分頁驗證。**為何不照 GitLab 那樣 per-run seed**：在真 `api.github.com` 上每跑一次建 105 個 PR ≈ 315 content ops，會踩 GitHub secondary rate limit，而 production `gh.Client` 不重試 → 直接 fail。改採 **pre-seeded fixture（Option 1）**：一次性在 sandbox 建 ~105 個長存 open PR（前綴 `tenant-api/fixture/`），分頁測試 `TestForgeE2E_GitHub_Pagination` **只讀**（`ListOpenPRs` → 斷言 >100 且每個 PR number 只出現一次 = 翻頁不漏不重）。seed 由 gated `TestForgeE2E_GitHub_SeedPaginationFixture`（`E2E_GITHUB_SEED_FIXTURE=1`）負責，**冪等 top-up** 到 105、全程走會重試 secondary limit 的 `ghSeeder`（`createBranchRaw`/`commitFile`/`createPRRaw`）+ 節流，經 workflow `workflow_dispatch` 的 `seed_pagination_fixture` 一次性跑。**janitor 跳過 `tenant-api/fixture/`** 故 fixture 永存；read 測試在 fixture 就位前 **skip-until-seeded**（非阻塞）。分頁 logic 本身 #615 已 unit-tested、#628 已對真 GitLab 驗過，本項補 GitHub-specific 真 Link-header。
- **try-local 一鍵體驗 stack — #449 onboarding epic（[#464](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/464) / [#466](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/466) / [#465](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/465) / [#463](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/463) / [#467](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/467)）**：新增 [`try-local/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md) —— `cd try-local && cp .env.example .env && docker compose up -d`（不需 K8s）把整套平台跑在筆電上，~1 分鐘看到 da-portal Tenant Manager + threshold-exporter→Prometheus→Alertmanager 的真實 critical 告警紅燈。**Mode 0 核心雙星**（`docker compose up da-portal tenant-api`）只起 live Tenant Manager（按 Save → 真實 git commit）。配套四件：(1) tenant-api 新增 `--dev-bypass-auth`（本機免 oauth2-proxy 注入 dev 身分；四層防線 [ADR-022](docs/adr/022-dev-auth-bypass-four-layer-containment.md)：預設 off / 可觀測 tripwire / 在 k8s 內 panic / deploy-time SAST）；(2) 4 個 component QUICKSTART.md；(3) release images 轉 **multi-arch（linux/amd64+arm64）**（Go cross-compile + da-tools per-arch + `verify_multiarch.sh` manifest 把關，解 Apple Silicon 原生）；(4) root README 加「在本機試用」4-產品 at-a-glance 展廳 + 綁 nightly-smoke 的動態 badge，nightly `try-local-smoke.yaml` 每日驗證 + 連 3 次失敗自動開 issue。詳 [`try-local/README.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md) + epic [#449](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/449)。
- **新增 [ADR-021](docs/adr/021-tenant-log-query-federation.md)：Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled（TRK-316）** — 新架構決策：tenant 在平台上**就地查**自己的 log（query-in-place，非拉回；ADR-020 的姊妹件、方向相反）。平台**只 own 授權平面**，複用既有 `helm/federation-gateway` 新增第三個 `victorialogs` mode；隔離 100% 來自 VictoriaLogs 原生 `(AccountID, ProjectID)` 租戶模型 + JWT claim→header 注入（**非** prom-label-proxy —— LogsQL ≠ PromQL）。ingestion 蓋章解耦為**顯式可驗證契約**（零信任 payload + node-edge 強蓋章 + AccountID 單調配發永不回收）。3-layer blast radius 對 LogsQL 重校（無 sample cap，改靠 time-range 上限）。**Phase 1 — (b) 平台營運 log → targets v2.10.0**；**Phase 2 — (a) 租戶應用 log → defer-with-trigger**。ZH-primary、依語言政策不另製 `.en.md`（同 ADR-019 / ADR-020）。
- **threshold-exporter reload-pressure 記憶體 lever（issue [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459) Track 2+3）**：v2.8.0 closure soak 發現 sustained reload 下 `go_memstats_sys_bytes` / `heap_idle` high-water creep（`heap_objects` 持平 → 是 Go runtime GC pacing，**非 code leak**）。本 PR 交付**兩個 opt-in lever（皆 default off，不改既有行為）**：(1) Helm `exporter.goMemLimit` 注入 `GOMEMLIMIT` env（Go 1.19+ runtime 原生 soft heap ceiling，首選）；(2) `-free-os-mem-after-reload` flag（Helm `exporter.freeOsMemAfterReload`）每次 reload 後呼 `runtime/debug.FreeOSMemory()`，新 metric `da_config_free_os_memory_total` 計次。soak harness（`run_chaos_soak.py`）加追 `go_memstats_heap_released_bytes`（return-to-OS 直接訊號）。文件：[benchmark-playbook](docs/internal/benchmark-playbook.md#memory-characteristics-under-reload-pressure-459) 新增「Memory characteristics under reload pressure」段 + 新增雙語 [`deployment-sizing.md`](docs/integration/deployment-sizing.md)（reload-interval × uptime ≈ 成長 proxy + 記憶體 sizing 指引）。**Track 1（4h soak / reload-interval sweep / GOMEMLIMIT 實驗）未含本 PR**——先把 lever + 量測訊號到位讓實驗可跑。詳 [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459)。
- **threshold-exporter chart 補上 generic `extraEnv` / `extraEnvFrom`（issue [#607](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/607)）**：對齊 `helm/vector` / `helm/victorialogs` / `helm/chargeback-aggregator`（#565/#566）的平台慣例，讓 operator 跨 chart 心智模型統一 —— 注入 chart 未模型化的 env（`HTTP_PROXY` / APM agent 變數 / 自訂憑證路徑等，支援 `valueFrom` Secret ref）。採**混合模式（hybrid）**：保留 #459 的 dedicated `exporter.goMemLimit`（一等公民、可發現、可型別化、與 `freeOsMemAfterReload` 成對）為記憶體調校首選，`extraEnv` 為逃生艙（escape hatch）。**碰撞防呆**：template 先渲染 dedicated 欄位、後渲染 `extraEnv`，故同名 key（如 `GOMEMLIMIT`）依 K8s env「後蓋前（last-one-wins）」由 `extraEnv` 取得最終覆寫權，無需 Helm 端比對 key。兩者皆 default `[]`（no-op，不改既有 deploy）；chart 版本不 bump（threshold-exporter chart 版本與平台 release 線耦合，僅 release wrap-up 時 bump，同 #606）。詳 [#607](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/607)。
- **Container/k8s IaC SAST Layer 4 — raw k8s manifest（kube-linter + Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-314）** — 新 `scripts/tools/lint/check_k8s_manifests.py`：以 L2 同一個 kube-linter 直掃 `k8s/**/*.yaml`（不需 helm render，檔案已是 concrete objects），重用 L2 的 severity 分類 + `.kube-linter.yaml`，並用自有 `(path, check)` 中央 `EXEMPTIONS` registry。**ticket 原假設「0 raw manifest → stub」已過時**：pre-flight grep 發現 `k8s/` 有 42 個真 manifest，故為**真層**。Baseline：42 manifest，**0 Critical** ✅ / 1 baseline-High（tenant-api 可寫工作區；原 maintenance-scheduler CronJob 的 2 筆同 PR 加固 securityContext—runAsNonRoot/readOnlyRootFilesystem/drop-ALL + /tmp emptyDir + PYTHONDONTWRITEBYTECODE，runtime-test 過—故解除豁免而非列管）。hook `k8s-manifests-sast-check`（manual stage，需 engine），CI 併入「Container SAST (Helm L2 + raw k8s L4)」job。**AC5 CI integration**：於 `docs/internal/iac-lint-baseline.md` 收斂全 4 層共用的 **Severity → Action SSOT 表** + branch-protection required-check checklist（owner action）；trivy image-CVE scan 維持 informational。**附帶修復**：擴 L3 secret-shape scope 到 `k8s/`（69→111 檔），唯一命中 `k8s/03-monitoring/secret-grafana.yaml` 的 `admin-password: admin`（committed 弱憑證、trufflehog 低熵漏抓）已改為 `REPLACE_WITH_STRONG_PASSWORD` placeholder。
- **Container/k8s IaC SAST Layer 3 — Helm values secret-shape（純 Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-313）**：4-layer 第三層、最輕（純 Python，無 engine）。`scripts/tools/lint/check_helm_values_secrets.py` 抓「key 名像 secret（`password`/`token`/`apiKey`/`secret`/`clientSecret`…）卻設成非空字面字串」於 `helm/*/values*.yaml` + `helm/values*.yaml` + `helm/*/templates/*.yaml`（**含 ConfigMap 等所有 template**——secret 誤置於 ConfigMap 是最常見外洩,key-name 語意掃描適用所有 manifest）。key 採 **endswith**（`passwordPolicy`/`tokenTTL` 等 config 不誤報）。class (b)（negative pattern + escape）：diff-only + PR-body bypass（`bypass-lint: helm-values-secrets`）+ 雙語錯誤。**與 [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) trufflehog 互補**：trufflehog 抓高熵、本 lint 抓 YAML shape（連低熵 `password: hunter2` 都抓）；兩者不雙重 fire（本 lint ship 在 0，只對新硬編才響）。**白名單**：空值 / `${VAR}` / `{{ .Values.* }}` / placeholder（`<changeme>`/`REPLACE_WITH_*`）/ bool / numeric / Go-duration（給 `tokenTTL: 4h`）/ `valueFrom`·`secretKeyRef` / YAML alias（`*anchor`）；**key-allowlist**：`createSecret`/`secretName`/`secretRef`/`secretKeyRef`/`secretKey`/`tokenTTL`。**已知限制**：block scalar（`|`/`>`）與 list item（`-`）內的硬編值不掃,交 trufflehog 高熵捕捉。Baseline：**69 檔 0 findings**（self + Gemini 對抗式 review 後擴 scope + 修 endswith/placeholder/YAML-alias，仍 0）。pre-commit hook `helm-values-secrets-check`（default stage，diff-only）+ CI Lint job。詳 [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md) §Layer 3。

- **Container/k8s IaC SAST Layer 2 — Helm template security（kube-linter + Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-312）**：4-layer 的第二層。引擎為 **單一 kube-linter**（render-then-lint），wrapper `scripts/tools/lint/check_iac_helm.py` 跑兩模式 + 套 severity→action：**Mode A** 源碼文字掃 `ALLOW_EMPTY_*`/`INSECURE_*`（在 render 前抓、連 `{{ if }}` 包住的也抓，ERROR 無 escape）；**Mode B** 對每個 chart `helm template --namespace=lint-test`（含 `values-tier*.yaml` 變體）→ kube-linter，外加 wrapper 自己 parse 渲染 YAML 抓 `capabilities.add`。**嚴重度**：Critical（privileged / privilege-escalation / host-network / host-pid / host-ipc / docker-sock）→ BLOCK 無 escape；High（run-as-non-root / no-read-only-root-fs / unset-cpu·memory-requirements / capabilities-add）→ 須登記**中央 EXEMPTIONS 註冊表**才豁免（否則 BLOCK，逼 review），其餘 → INFO。**設計決策（偏離初始 AC，已記錄）**：(1) **廢 trivy-config**——與 kube-linter 對 K8s misconfig 高度重疊、雙引擎會 desync；trivy 仍為既有 image-CVE informational scan（不同關注點）。(2) **例外採中央註冊表而非 in-chart 註解**——`helm template` 會剝掉註解（values 經 `toYaml` 渲染尤甚），且集中式給 SecOps 單一稽核面。(3) `runAsNonRoot:false` 由 Critical 改 **High + 中央豁免**——否則會硬擋合法的 vector log-collector DaemonSet（需 root 讀 host log）。**Baseline**：9 chart **0 Critical**；5 baseline-High（mariadb ×2 / tenant-api ×1 / vector root 三件套含 `DAC_READ_SEARCH` ×2）+ 3 INFO（pdb），全列管於 [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md)。**附帶修復**：L2 抓到 `helm/da-portal/.helmignore` 用了 Helm 不支援的 `**/` glob → `helm template`/`install` 直接中止（chart 原本無法部署、CI 無處 render 故長期未爆），改為 bare `README.md`。CI 走獨立 parallel job「Container SAST L2 (Helm)」（setup-helm + kube-linter binary，Docker fallback）；pre-commit hook `iac-helm-sast-check` 為 manual stage（render 9 chart ~1-2 min,不拖慢每次 commit）。詳 [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)。

- **Container/k8s IaC SAST Layer 1 — Dockerfile（hadolint + Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-311）**：4-layer IaC SAST 的第一層落地，並**試點 hybrid policy**（open-source engine + Vibe wrapper，取代過去「DIY `check_*.py` 看到就打」的路線）。hadolint v2.12.0 當引擎（containerized 或 binary，`.hadolint.yaml` config），Vibe wrapper `scripts/tools/lint/check_iac_vibe_rules.py` 聚合 hadolint JSON、套 severity→action（error=BLOCK / warning=baseline-High / info=INFO）並補三條 hadolint 無法表達的規則：每個 Dockerfile 須有 **HEALTHCHECK 或 `# rationale:` 註解**（distroless 自動豁免）、**禁過寬 `COPY`/`ADD`**（source 為裸 `.`/`./`/`*`，正是 v2.7.0 silent-break 的成因）、**`.dockerignore` baseline**（在每個 build-context root；用 `pathspec` 正規化等價 glob）。pre-commit hook `iac-sast-check` + CI `Lint` job hard-gate。**Pre-flight 校正**：repo 已成長至 7 個 Dockerfile（非 6）、9 個 Helm chart（非 ~4）；`.dockerignore` 從 1/7 補到全 context 覆蓋——且 da-portal / tenant-api 是從 **repo root** build（`context: .`），故補一份 repo-root `.dockerignore`（含 `!docs/...` re-include，本地實測兩者 image build 正常），而非各 component 目錄各放（會是 no-op）。issue AC1 的 DL3025 標號修正（真 DL3025 是 CMD/ENTRYPOINT JSON，over-broad COPY 改由 wrapper rule 攔）。非阻擋 High findings（5 筆，多為刻意的 `--no-cache` 策略）列管於 [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md)。Layer 2-4（Helm template / values / k8s manifest）+ 完整 hybrid policy 段見 TRK-312~315。詳 [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)。

- **#566 batch D — egress allowlist gate + GitOps-write-boundary doc（T4-1/T4-2 + T3-2/T3-3，issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：紅隊 backlog 最後一批可動工項。**經 Gemini 對抗式 review 修正方向後定案**（take/reframe：Gemini 假設 GitOps self-heal 已覆蓋平台 chart —— pre-flight grep 打臉，repo 的 GitOps scope 只到 `conf.d/` 租戶配置，故 self-heal 改標**條件式建議**；Gemini 建議的 extraEnv 扁平 key-blacklist 會打死合法 `secretKeyRef` 用途 —— 改為**區分 literal-value vs valueFrom**）。**T4 egress gate**（`scripts/tools/lint/check_log_egress_policy.py` + `make lint-egress`）：PR/pre-deploy 階段渲染 log-aggregation charts，擋 (1) `additionalSinks[].{endpoints,uri}` host 不在 allowlist、(2) 覆寫 `VECTOR_*` 保留 env（非 downward-API fieldRef，避免誤殺 chart 自己的 `VECTOR_SELF_*`）、(3) sensitive-named env（`*TOKEN*`/`*KEY*`/`*SECRET*`…）用字面 `value:` 而非 `valueFrom`。資料流照 multi-doc YAML caveat：`helm template` → `safe_load_all` → 過濾 None → 迭代 manifest，Vector ConfigMap 額外解析內嵌 `vector.yaml` 取結構化 sinks。**選型**：repo 的 `policies/examples/*.rego` 是 illustrative（grep 證實無 CI/Makefile/pre-commit 引用），真 gate 是 ~50 個 `check_*.py` + `opa` binary 不在 dev/CI（外部下載也被擋）—— 故 live gate 走 Python lint，政策同時鏡像為 `policies/examples/log-egress.rego`（modular core rules，為未來 OPA Gatekeeper runtime admission 留無痛遷移 seam）。**T3 doc-only**（runbook §7.5.2 production checklist）：人類 RoleBinding 不得有 `update/patch/delete` on deploy/cm/secret（只 GitOps SA 能寫）+ 把平台 Helm release 納入 ArgoCD/Flux self-heal（篡改 window 壓到 sync interval；明標目前 GitOps scope 只到 conf.d、需擴）。**CI gap 修補（review 抓到的真問題）**：`Python Tests` job **原本沒裝 helm** → 整個 session 加的 helm-gated chart test（NetworkPolicy / VRL / buffer guard / egress 共 34 個）在 CI **靜默 skip**、只在本地驗過 —— 加 `azure/setup-helm@v4` 讓它們從本地 safety net 升級成真 CI gate。chart test 28 → 34。詳 runbook §7.5 / `policies/examples/log-egress.rego`。

- **#566 batch C — CSV tamper-evident + extraEnv platform 慣例對齊（T2-4 + Q5，issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：紅隊 backlog C 批。**T2-4 chargeback CSV integrity**（`helm/chargeback-aggregator` `0.1.2`→`0.2.0`）：每筆 `chargeback-YYYY-MM-DD.csv` 旁邊寫 `.csv.sha256` sidecar（`sha256sum -c` 格式，finance pipeline 可單檔驗證）+ append-only `manifest.jsonl`（每次跑寫一行 `{date, sha256, generated_at, rows}`）。tamperer 要同時改 CSV + .sha256 + manifest 三檔才能不留痕跡，門檻拉高（vs 原本 free-edit）。retention prune 連 .sha256 sidecar 一起砍（避免 orphan hash 誤導），但 manifest **永不 prune** —— 它就是長期 audit trail。**邊界明示**（runbook §2.1）：這是 **tamper-evident**（有改動的訊號）不是 **tamper-proof**（PVC write 權還是能造假三檔）—— 真正 compliance-grade WORM 是 #566 X-2 / SIEM 端責任。**Q5 extraEnv/extraEnvFrom 平台慣例對齊**：把 #565 給 helm/vector 加的 `extraEnv` + `extraEnvFrom` 同形狀加到 `helm/victorialogs` （chart `0.1.3`→`0.1.4`）+ `helm/chargeback-aggregator`（chart 同上版本 bump）—— 三個 chart 現在 cred 注入 shape 一致。VictoriaLogs 未來加 `-httpAuthKey` 走 Secret 透過 extraEnvFrom 餵；chargeback 對 multi-tenant VictoriaLogs `ACCOUNT_ID` 也能走 Secret。**chart test**：23 → 28 case（sha256 sidecar 格式 / manifest append-only / sidecar prune-with-csv / chargeback + vlogs extraEnv 渲染）。runtime kind 驗證：CronJob 跑出 chargeback-2026-05-21.csv + .sha256 + manifest.jsonl 三檔，busybox `sha256sum -c` 對 sidecar 回 `OK` ✓。手動驗證指令文件化進 chargeback runbook §2.1。詳 [`chargeback-aggregator-runbook.md`](docs/internal/chargeback-aggregator-runbook.md) §2.1。

- **#566 batch B — compliance-strict path（X-3 + T3-1 + X-2 schema reserve，issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：紅隊 backlog B 批，三條互相依賴的 compliance hardening 一 PR 走完。**X-3 Vector disk buffer mode**（`helm/vector` `0.4.0`→`0.5.0`）：`additionalSinks` 新增 `_buffer_type: memory|disk`（預設 memory，無 regression）。disk mode 寫進 daemonset 既有的 hostPath data_dir（`/var/lib/vector`），Vector pod 重啟 / OOM-kill 都不丟 in-flight events（修了 X-3 audit timeline 洞）。`max_size` 預設 1 GiB（covers ~1h SIEM outage 中等流量）；分支 render `memory → max_events` / `disk → max_size`，Vector 對錯欄位名會 reject 啟動所以模板要選對。未知 `_buffer_type` 走 template `{{ fail }}` 同 §2 護欄一樣 fail-loud。**runtime 抓到的細節**：helm/sprig 對大整數會 round-trip 成 float scientific notation（`268435488` 渲染成 `2.68435488e+08`），Vector BufferConfig parser 直接 reject 「untagged enum」error。Template 加 `| int64` cast 強制整數渲染；新增 regression test 鎖死字面值。Vector 自帶 disk buffer 最小 256 MiB 限制，低於這個值 runtime 拒，doc 內提及。**T3-1 §2 vs compliance trade-off 文件**（runbook §7.3.1）：明寫 §2 hard rule（drop_newest）跟 compliance（不可漏 row）根本衝突 —— 時機性 attacker 趁 SIEM-down window 動手，VictoriaLogs 仍有 row 但 SIEM 端 forensic timeline 真空。三條設計路徑表格：**availability-first**（memory + drop_newest，預設，pod 重啟丟 row）/ **compliance-degraded**（disk + drop_newest，§2 仍守、SIEM downtime cover 到 max_size 滿）/ **compliance-strict**（disk + block，VictoriaLogs 必須 disable，產品定位變更等級）。預設不變、operator 按 compliance SLA 選層。**X-2 audit-signing schema seam**（VRL doc-only）：comment block 在 VRL demux 後標出 `audit_signature` / `audit_signed_at` field shape 未來會由 federation-gateway HMAC 簽。今日 no-op（`merge` 已自動 pass-through 任何 source JSON 欄位到 sink），但 schema 預留 + chain-of-custody primitive 的整體論述就位 —— 真做 producer-side signing 時不必動 Vector。**chart test**：22 → 23 case（新增 buffer memory/disk/invalid-type render + max_size 字面值 integer-shape 反 regression）。runtime kind 驗證：vector pod 用 disk buffer config Ready=True、`max_size: 268435488` 渲染成 bare integer（不再 float），既有 VictoriaLogs ingest + chargeback CronJob 不受影響。詳 [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) §7.3.1。

- **#566 batch A+E — log-aggregation hardening cheap wins（issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：#539 closure 後紅隊 + quality review 留下 17 條 backlog 拆批處理的第一批；都不阻 production-flow，但每條補一道目前單薄的縱深。**T5 image digest knob**：`helm/{victorialogs,vector,chargeback-aggregator}` 各新增 `image.digest`（預設空字串、render 出仍是 `repo:tag`，set 後變 `repo:tag@sha256:...`，kubelet pull-by-digest 不再 trusts upstream tag）。chart versions vlogs `0.1.2`→`0.1.3` / vector `0.3.2`→`0.4.0` / chargeback `0.1.1`→`0.1.2`。**T2-1 VRL gateway-origin filter**：`helm/vector` 新增 `audit.gatewayPodOwnerPrefix`（預設 `federation-gateway-`，空字串停用） —— 若 `log_type=federation_audit` 但 `kubernetes.pod_owner` 不符 prefix，VRL 強制改 `log_type=suspicious_audit` 分流；防 spoofer pod 用 gateway label 推假 audit row（紅隊 T2-1，最終 fix 仍須 RBAC，本層是 defense-in-depth + 留可 alert 的訊號）。**T1-4 + X-4 Prometheus alerts**（`k8s/03-monitoring/configmap-rules-platform.yaml`）：(a) `FederationAuditPipelineSilent`（severity: critical，5m）—— `absent_over_time(tenant_federation_requests_total[10m])` 一旦消失代表 audit pipeline 斷掉，解決原本「CSV 上某 tenant 0 row 不知是真沒用還是 pipeline 死掉」的歧義；(b) `VectorBufferEventsDropped`（severity: warning）—— `rate(vector_buffer_discarded_events_total[5m])>0` 偵測 SIEM fan-out buffer 溢流（§2 預設 drop_newest 不擋上游、但會丟 SIEM-bound row，這個 alert 把「靜默丟 row」變成 actionable）。runtime 在 kind 驗證：rules 已 load、`FederationAuditPipelineSilent` 因 gateway 本來就沒部進 kind 正確 fire pending。**Q4 chargeback runbook 拆檔**：把 `platform-log-aggregation-runbook.md` §6 整段（80 行）抽出成獨立 [`chargeback-aggregator-runbook.md`](docs/internal/chargeback-aggregator-runbook.md)，主 runbook 留 5 行摘要 + pointer；對齊 `federation-key-rotation-runbook.md` 體積。**Q6 VRL refactor watermark**：在 VRL demux 前加架構註解標出「現在 4 branches 還可讀，加第 5 個 log_type 該拆 Vector `route` transform」的決策點（doc-only、不改 VRL）。**chart test**：`tests/shared/test_helm_chart_log_aggregation.py` 從 15 case 補到 **19** case（image digest knob 兩種 render path、VRL origin filter enable/disable/**nil-audit-block-safe**）。**Pre-merge review 抓到的兩個 issue**：(a) VRL origin-filter 用 `.Values.audit.gatewayPodOwnerPrefix` 直接 access，operator 用 `helm upgrade --reuse-values` 從 0.3.x 升到 0.4.0 時，舊 stored values 沒有 `audit:` block → template **nil pointer panic**（自己 dogfood 升 chart 時撞到的問題，當下 work around 沒進 fix）；改用 `(default dict .Values.audit).gatewayPodOwnerPrefix` nil-safe lookup，補一條 unit test 鎖死這個升級路徑。(b) 之前 self-review 為了過 `bump_docs --check` 把 `docs/internal/known-regressions.md` v2.8.0 改 v2.8.1 寫到本地 disk —— 但該檔 gitignored，never ships。Revert 為原狀，避免 dev 環境跟 git tree drift。kind cluster runtime 驗證：Prometheus 重啟後新 alerts 都 `health=ok` 載入 / Vector helm upgrade 後 VRL 含 `suspicious_audit` 分流 + 正確 prefix / 既有 fan-out + chargeback CronJob 不受影響。

- **Platform 日誌彙整 Phase 3 — SIEM fan-out compliance branch（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 3）**：`helm/vector` chart `0.2.1`→`0.3.1`，values 新增 `additionalSinks: []` 列表，每筆 entry 是 free-form Vector sink config，跟 primary VictoriaLogs sink 共用 demux transform 輸出 —— 當 strict compliance 客戶到（tamper-evidence / WORM / legal hold），加一筆 sink 指向 SIEM 就好，上游 source/transform 不動（同一份 demuxed stream 包含 Phase 1 federation_audit/gateway_operational + Phase 2 prometheus_query_log 三條 stream 一起 fan-out，不需要二次配線）。**§2 hard rule 落地**：chart template 自動把每筆 entry 包 `buffer: {type: memory, when_full: drop_newest, max_events: 10000}`，意思是 SIEM 慢/掛時對該 sink **丟新事件**而不是 back-pressure 上游，primary VictoriaLogs 永不被連累。Entry 自帶 `buffer:` block 就跳過自動 wrap（給要 disk buffer 的 advanced operator）。**fail-loud 護欄**：template 偵測 `_buffer_when_full: block` 直接 `{{ fail }}` 中止 render，配 helpful error message（明寫違反 §2 + 給可用值 + 真要 block 該怎麼做）——self-review 預防 operator foot-gun。**SIEM 認證**：新增 `extraEnv` / `extraEnvFrom` values + daemonset 對應 render，operator 能透過 `secretRef` 把 SPLUNK_TOKEN 等 cred 注成 env var 讓 Vector `${VAR}` 展開（self-review 抓到原本文件範例引用 `${SPLUNK_TOKEN}` 但 chart 沒對應 knob 的 cold-start 斷層）。**架構釐清**（runbook §7.3）：加 fan-out **不會**把 VictoriaLogs 變 tamper-evident —— 平台只負責「同一份 demuxed stream 餵給 SIEM」，三件事完全在 SIEM 端（tamper-evidence / legal hold / immutable retention）；SIEM owner（通常 SecOps）扛 chain-of-custody，平台只負責 delivery 不漏 row（dropped-events metric 該被告警）。**runtime smoke-test 在 kind cluster 驗證**：起 mock-siem deployment（Python stdlib HTTP server 印收到的 JSON），helm/vector 加 additionalSinks 指向 mock-siem → 同時收到 prometheus_query_log row；scale mock-siem 到 0 模擬 SIEM outage 60s，Vector pod RESTARTS=0、VictoriaLogs row count 72366→73039（+673 in 30s，primary 完全沒被 back-pressure）；restore mock-siem replicas → 立即恢復收 row。runbook §7 + §7.1/7.2/7.3/7.4 涵蓋啟用步驟、4 條鐵則（drop_newest 預設 / 不可 block / inputs 用 demux 不用 raw / 不可叫 victorialogs）、compliance 責任落點對照表、4 個 failure mode。Phase 3 完成 → #539 三 phase 全交付，issue 關 umbrella。**紅隊 review 兩條 critical 同步補進**：(a) `helm/victorialogs` `0.1.1`→`0.1.2`：NetworkPolicy 預設 `enabled: true` + 預設 `allowedPodSelectors` 鎖 :9428 只給 vector / chargeback-aggregator / grafana —— 之前 VictoriaLogs HTTP API 無 built-in auth、同 ns 任何 pod 可 `wget DELETE` 砍歷史 audit（紅隊 T2-2）。(b) `helm/vector` `0.3.1`→`0.3.2`：`_buffer_when_full: block` 護欄補完 —— 之前只擋 shorthand，operator 自帶 `buffer: {when_full: block}` 直接 override 會繞過、違反 §2 hard rule；新規則 template 偵測自備 buffer block 內的 `when_full: block` 也 fail-loud（紅隊 T4-4）。runtime kind smoke：NetworkPolicy 套用後 vector + vlogs 正常 ingest（row count 持續爬升 ~108k），`describe networkpolicy` 確認三條 podSelector 規則就位（kindnet 不 enforce，production CNI 如 Calico/Cilium 才生效）；buffer guard 兩種繞法（shorthand + 自帶 block）都 fail template render。其餘紅隊 finding（producer-side audit signing / sink URL allowlist / Vector disk buffer / image digest pinning / SIEM-down evidence gap 等共 9 條）開一張 follow-up issue 統一收，本 PR 不擴 scope。**Quality review 兩條補進**：(c) 新增 `tests/shared/test_helm_chart_log_aggregation.py` 15 case —— pattern 抄 `test_helm_portal.py`，gate 在 helm CLI 可用（match `test_federation_keygen.py` 的 openssl-skipif 慣例）；驗 vlogs NetworkPolicy 三條 consumer / vector VRL `ruleGroup` override + `tenant=` regex + `_s` 單位 / `_buffer_when_full=block` 兩條繞法都 fail / extraEnv 進 daemonset / 內嵌 Python aggregate.py `py_compile` 通過。(d) Smoke-test fixture 進 repo：`docs/internal/examples/log-aggregation-smoketest/{mock-siem.yaml, vector-phase3-values.yaml, README.md}`，runbook §7.1 加 pointer —— red-team T2-2 / Phase 3 fan-out 在 main 從此可重現，不再是「我 host 上的 tmp file」口頭證據。詳 #539 §4 Phase 3 / [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) §7。

- **Platform 日誌彙整 Phase 2 — federation chargeback aggregation（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) / [#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)）**：把 #552 scoping ticket 指明的「正確 chargeback 來源」端到端接起來 —— Prometheus query log → Vector → VictoriaLogs `log_type=prometheus_query_log` stream → daily CronJob 算 per-tenant `samples_scanned` + `exec_time_s` CSV。**為什麼**：`tenant_federation_requests_total{tenant,status}`（gateway mtail sidecar 產出）只能數**請求次數**，無法分辨重 query（掃 10 萬 series）跟輕 query（10 series）—— 計費把兩者收同樣錢、不可行（#552）。IV-2f（#511）刻意不讓 Envoy 加 `series_returned` 欄位（要 buffer + 解壓 response body、成本太高；blast-radius 交給 storage `--query.max-samples` cap），正確 cost signal 在 Prometheus 自己 `query_log_file` 每條 query 的 `stats.samples.totalQueryableSamples` + `stats.timings.execTotalTime`。**改了什麼**：(1) `k8s/03-monitoring/configmap-prometheus.yaml` global 加 `query_log_file: /dev/stderr` —— 每條 PromQL query 變成 stderr 上的一行 JSON，跟 Prom 自己的 log 交織但可由 JSON-shape 區分；(2) `helm/vector` chart `0.1.2`→`0.2.0`，values 新增 `additionalSources` list（per-entry kubernetes_logs source；理由：gateway 用 `app.kubernetes.io/name` label，Prometheus 用 `app` label，單一 selector 無法同時 match —— 不引入 OR 邏輯反而展開成多個 source 比較乾淨），configmap.yaml 對應 range 渲染、demux transform inputs 拼進去；VRL demux 加第三 branch：parse 成功且有 `params.query` field → `log_type=prometheus_query_log`，用 regex 從 `params.query` 抽出 federation-proxy 注入的 `tenant="X"`（label 名為 `tenant` 不是 `tenant_id`，per PR #505 / federation-label-enrichment-audit；輸出 field 名仍叫 `.tenant_id` 對齊 §3 stream-field schema）；同時偵測 entry 是否有 `ruleGroup` field —— 有則屬於 Prometheus rule / alert evaluation、強制砍掉 `.tenant_id` bucket 成 `platform`（不可計費，防止規則表達式內部 `{tenant="X"}` selector 被誤分租戶），hoist `samples_scanned` / `exec_time_s` / `eval_time_s` 三個 cost 欄位到 top-level 讓 LogsQL `stats sum()` 不必走 nested path（**單位明示**：Prometheus 原生 `execTotalTime` / `evalTotalTime` 輸出單位是**秒**，self-review 在 push 前抓到原本欄位名誤標 `_ms`、會讓下游 finance pipeline 少收 1000× 帳，已於 amend 統一改為 `_s`；aggregator 查詢同時 sum 兩個欄位名以平滑 chart 升級期間混合舊／新 row 的 24h overlap window，VictoriaLogs retention rolls 過後 legacy `_ms` slot 自然消失）。(3) 新增 `helm/chargeback-aggregator` chart（v0.1.0）—— CronJob daily 02:00 跑 Python script（純 stdlib，無 pip）查 VictoriaLogs LogsQL `_time:24h log_type:prometheus_query_log | stats by (tenant_id) sum(samples_scanned), sum(exec_time_s), count()`，寫 `chargeback-YYYY-MM-DD.csv` 到 PVC（`output.retentionDays=90` 預設、舊檔自動刪），含 `manualJob.enabled=true` 一鍵跑 bootstrap / smoke-test Job；security 同 Phase 1（distroless 不可用就用 python:3.13-slim + non-root + readOnlyRootFS + drop all caps + automountServiceAccountToken: false）。**架構邊界**：chargeback 算的是 raw cost dimension（samples、exec time、query 數），**不**算錢（unit pricing 是 finance 的事）、**不** push metric（沒拉 pushgateway dependency；要 Prometheus 看的話 wrap 一個 textfile-collector 在外層）、**不**做 real-time 聚合（CronJob 才能保證慢的 chargeback run 永不拖累 federation 熱路徑、#552 hard rule）。runbook 新章 §6 + §6.1/6.2/6.3 涵蓋 deploy 順序、tenant attribution 邏輯（platform-bucket 不可計費）、4 個常見 failure mode；§7 Phase 3 連結改為「Phase 2 stream 自動跟著 fan-out」明確不需要二次配線。**Runtime smoke-test 在 kind cluster 驗證**：手動 inject `up{tenant="db-a"}` → VictoriaLogs row `_stream{...,tenant_id="db-a"}` ✓；platform rule eval row 沒 tenant_id ✓；CronJob 一次跑出 CSV 兩 bucket（platform=1055 samples / 6050 queries / 24h，db-a=1 sample / 1 query）正確。Python 3.13 `datetime.utcnow()` deprecation 一併修為 timezone-aware `datetime.now(timezone.utc)`。詳 #539 §4 Phase 2 / [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) §6。

- **Platform 日誌彙整 Phase 1 — Vector + VictoriaLogs（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)）**：兩個新 chart + Grafana 接線，把 federation-gateway audit log 從「跟著 HPA 死亡的 pod stdout」搬進中央 log store，讓 incident query「過去 6h 這顆 token 碰過什麼」有得問。新增 `helm/victorialogs`（單 pod + PVC + `-retentionPeriod=30d`，#539 §3 operational-simplicity 選的 single-binary store）+ `helm/vector`（DaemonSet + ClusterRole list/watch pods/namespaces/nodes + ConfigMap 帶 VRL 設定）；DaemonSet 走 `VECTOR_SELF_NODE_NAME` 讓 `kubernetes_logs` 只看本節點 pod（不必 cluster-wide list 翻全部 pod、API server 負擔線性可控）；source `extra_label_selector` 預設 `app.kubernetes.io/name=federation-gateway` 把 Phase 1 範圍鎖在這條 audit pipeline（§7 非目標：不做平台級通吃，後續每個 consumer 各開 ticket）。**VRL demux 兩條鐵則**（#539 §3）：(1) `parse_json(.message)` 成功 → `log_type=federation_audit`、merge 解出的欄位上來；失敗 → `log_type=gateway_operational`、保留原始 `.message`。**故意不**用 `exists(.tenant_id)` 當分流條件 —— JWT 失敗的請求在 audit JSON 裡沒 `tenant_id`（jwt_authn 在 claim injection 之前就拒了）但它們**有 forensic 價值**（攻擊掃描證據）。(2) **不丟**操作層日誌，把 Envoy stderr 路到第二條 stream，讓它第一次能被中央查詢，否則 pipeline 嚴格劣於現狀。`_stream_fields=app,k8s_namespace,log_type,tenant_id,status`（bounded 集合）；`pod_name`、`token_id`、`query` 刻意**不**進 stream（HPA churn / 高基數會炸 stream 索引，#539 §3 schema 表）。Vector → VictoriaLogs 走 elasticsearch `_bulk` sink + gzip。Grafana 接線：`k8s/03-monitoring/configmap-grafana.yaml` 新增 `victoriametrics-logs-datasource` provisioning、`deployment-grafana.yaml` 加 `GF_INSTALL_PLUGINS` 自動裝 plugin（air-gapped cluster 必須改 pre-bake image，runbook 有寫）。新 runbook [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) 涵蓋 deploy 順序、smoke-test LogsQL 範本（5 條：現在有 log 進來、`by tenant_id` 量、`status:~"5.."`、JWT 拒絕掃 token、操作層異常）、stream-field schema 鎖（改它是破壞性變更）、troubleshooting（Grafana 查不到 / VictoriaLogs PVC bind 不到 / Vector 讀 hostPath 失敗）、capacity 公式（`size ≈ audit_RPS × 1KB × retention × 1.3 / compression`，VictoriaLogs ~10x 壓縮）、Phase 3 SIEM fan-out 是 sink 加一條、上游不變。**硬規則**（#539 §2）：producer 永不 HTTP-push 到 store、log-store 掛掉永不能拖累 federation gateway —— 故 chart 刻意不暴露 direct-push 便利旋鈕。**架構意義**：[ADR-020](docs/adr/020-tenant-federation.md) IV-2f 在這之前的 `helm/federation-gateway` 留下「durable 中央 forensic store 為 follow-up #539」缺口，本 PR 端到端補齊；envoy.yaml 與 `audit-sidecar/Dockerfile` 內的 `#539 follow-up` 註解一併翻新為「已交付」。**範圍邊界**（§7 反創意）：本 PR **不**動 chargeback query log（#552 consumer #2、blocked on 觸發）、**不**做 SIEM fan-out（Phase 3、需 strict compliance customer 觸發）、**不**做 log-based dashboard / alert（另開 ticket）。詳 [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) §0–§7。

- **Python SAST baseline — bandit + dev-rules Rule #5 code-driven enforcement（issue [#455](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/455)）**：dev-rules.md Rule #5 從 reviewer-convention 升級為 code-driven。新增 `.bandit` config + `.github/workflows/security-audit.yaml`（`scripts/tools/**` + `components/da-tools/**`、`bandit -ll -ii` = MEDIUM severity × MEDIUM confidence baseline）。Baseline 對 169 檔（52,816 LOC）跑出 13 個 medium+ findings、全部 triage 收斂為 0：1× B324 MD5 用 `usedforsecurity=False` 修正、1× B314 XML 與 11× B310 urlopen 走 inline `# nosec B<ID>  # rationale` 雙井號慣例（避開 bandit prose-as-test-id 警告）。**Rollout**：workflow `continue-on-error: true` 2 週 soak，無 false-positive flood 則翻硬 fail。bandit 覆蓋 Rule #5 shell / yaml_load / eval-exec-pickle / hardcoded-password（部分）；encoding / chmod 0o777 / stderr routing 三項 bandit 無原生 rule、暫留 reviewer convention。詳 [dev-rules.md §5](docs/internal/dev-rules.md#5-sast7-條安全-review-準則)。

- **Federation gateway 全域緊急斷路器（kill switch，issue [#551](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/551)）**：`helm/federation-gateway` 新增 `emergencyGlobalBlock` value（預設 `false`）。設為 `true` 時，gateway 的 Envoy route table 最上層多一條 `prefix: "/"` 的 `direct_response` 503 —— 每個請求直接回 `503`，不轉給 Layer 3 proxy 或 storage backend。用於 federation 事故（`prom-label-proxy` 0-day、storage 雪崩）時一鍵卸載**所有** federation 流量，取代逐租戶撤 token（撤銷另有 ~1-2min 最終一致延遲）。`tcpSocket` 健康探針不受影響（listener 仍接受連線）—— pod 不會被殺、開關可乾淨切回。經 GitOps 同步 + pod reload 生效（~3min）；需要瞬間切斷時 chart README 另記 `kubectl scale --replicas=0` 逃生門（即時但掉 in-flight、不入 git）。原 [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521) Gemini Day-2 review 的發想。chart version `0.2.3`→`0.2.4`。詳 chart README §Emergency global block。
- **Tenant federation 使用者文件（ADR-020 IV-2h，issue [#513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/513)）**：新增 [`docs/integration/tenant-federation.md`](docs/integration/tenant-federation.md) —— 租戶 onboarding 的操作指南：取得 4h RS256 token（`POST /api/v1/federation/tokens`，需 tenant admin）、設定租戶側 Prometheus `/federate` scrape 與 Grafana data source（Bearer header、不走 URL）、支援的 read API（query／series／labels；**不支援** remote_read → 403）、2-tier policy（platform whitelist + tenant subset，附 sample subset YAML；明寫 whitelist 是治理機制非查詢期安全邊界）、配額回應碼對照（429／422／413／403／401 各自怎麼退避）。**Day-2 行為**兩條（Gemini round-4 review 要求）：撤銷的 ~1-2min 最終一致性（合規用語、明寫非缺陷）、斷線後「報復性 catch-up 查詢」反模式（超大範圍 `query_range` 撞 `--query.max-samples` → 422 → retry loop 卡死；正解是 `sample_limit` + 手動分段拉歷史）。定位明確區隔 [`federation-integration.md`](docs/integration/federation-integration.md)（ADR-004 平台內部 federation，方向相反）。順帶更新 `victoriametrics-integration.md` §7 的 federation 列：狀態 設計階段 → 已交付、修正「vmauth」措辭為 prom-label-proxy。詳 [ADR-020](docs/adr/020-tenant-federation.md)。
- **Federation offboarding 完整性：殭屍憑證偵測 + offboarding runbook（ADR-020，issue [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521)）**：租戶 offboarding 在本平台是「移除 `conf.d/<tenant>.yaml`」的 git 操作，但 federation 有兩樣東西不在那個檔裡、不會被一併帶走 —— token records（存在 `tenant-federation-store` ConfigMap，runtime state）與 per-tenant subset 檔（`conf.d/_federation/<tenant>.yaml`），形成殭屍憑證與孤兒設定檔。風險**低**（殭屍 token 對已刪租戶注入 `{tenant="X"}` 只回空集、無 live 資料外洩，且受 gateway per-token/per-tenant 限流 + 4h TTL 約束）—— 屬 offboarding-completeness／合規（SOC 2／ISO 27001）問題。交付 **Option A**：(1) 新增 [`tenant-offboarding-runbook.md`](docs/internal/tenant-offboarding-runbook.md) —— offboarding 的 federation 收尾程序（撤銷 token、刪 subset 檔、撤銷最終一致性的合規用語 ~1-2min）；(2) tenant-api 內建**被動偵測器** `OrphanDetector` —— 週期掃描，發現 token／subset 檔的母租戶已不在 conf.d 就噴 `slog` WARN + 更新 `/metrics` 的 `tenant_api_federation_orphaned_tokens`／`tenant_api_federation_orphaned_subset_files` gauge。偵測器**只觀測、不自動撤銷／刪檔**：自動依「租戶不在 conf.d」推論去撤銷，在 conf.d 暫態異常（GitOps sync 中、壞檔）時會誤殺活租戶憑證；warn-only 給同樣的「防遺忘」安全網卻零誤殺風險（A vs 自動 reconciler vs CI-pipeline 的決策過程見 [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521)）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Token model。
- **Federation 請求路徑 E2E 測試（ADR-020 IV-2j，issue [#516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/516)）**：新增 `tests/federation-e2e/` —— docker-compose 把 federation 整條鏈拉起來（fixture-exporter → Prometheus ← prom-label-proxy ← Envoy gateway → mtail），host 端 pytest driver 走完整路徑驗 9 個情境：happy-path（簽 token → gateway 驗章 → proxy 注入 `{tenant="db-a"}` → storage）、跨租戶隔離（db-a token 的明確 `{tenant="db-b"}` selector 被 proxy 改寫回 db-a）、JWT enforcement（缺 token／偽簽章／錯 issuer／過期 token → 401，過期測試刻意把 exp 設在 clock-skew leeway 外）、撤銷傳播（改 revoked set → Lua reload 後 → 403）、Sybil 限流（多 token 打穿 per-tenant → 429 + mtail `rate_limited`）、1.5 MiB payload → Envoy buffer filter → 413、storage cap（查詢超 `--query.max-samples` → 422 + audit `bad_request`）、remote_read（`/api/v1/read` 及尾斜線變體 → 403）、Metadata API surface audit（IV-2g，issue [#512](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/512)：走完整 Prom HTTP API 表面 —— metadata endpoint 全部租戶隔離、無法隔離的 endpoint（`/targets`、`/status/*`、`/admin/*` 等）一律 404 不可達，杜絕 passthrough 洩漏）。**設計**：用 `helm template` 渲染**真實** gateway chart config 餵給 compose（no config drift —— 測的就是生產 artifact）；刻意不用 kind —— 對齊 repo 既有 `tests/e2e-bench` 不上 K8s 的決策、避開 Windows/WSL2/VirtioFS 環境的 I/O 成本（"test what you fly" 的範圍是請求路徑安全邏輯，那 100% 在 config + binary 裡）。**保真邊界**：測 request-path 安全邏輯（Envoy filter chain／Lua／proxy／storage cap），**不**測 K8s 編排層（Deployment／projected-volume swap／sidecar 起停）；tenant-api 不進 stack（其 token store 為 K8s ConfigMap-coupled），driver 以 tenant-api 同 claim shape 直接 RS256 簽 token。獨立 CI job `federation-e2e`，不進 `make test`／pre-commit／coverage gate。`make federation-e2e` 本地執行。詳 [ADR-020](docs/adr/020-tenant-federation.md) §後果（3-component coordination 風險）。
- **Federation storage blast-radius flags（ADR-020 IV-2c，issue [#508](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/508)）**：平台 Prometheus（`k8s/03-monitoring/deployment-prometheus.yaml`，即 federation-proxy 的 upstream）加上 ADR-020 Layer 1 的查詢硬上限 —— `--query.max-samples=5000000`（5M;tuning range 5M–50M）+ `--query.timeout=25s`,擋住單一 federation read 把 storage 打到 OOM 或 hang。兩 flag 皆為 **global**(此 Prometheus 同時服務平台 rule eval 與 federation read),5M 是 §Blast radius Layer 1 的 federation-driven 起點,內部 eval 若 false-positive 再上調(Prom 原生預設 50M / 2m)。`--query.timeout=25s` 刻意**短於** Layer 2 gateway 的 30s route timeout（**cascading timeout**,非等值）—— inner layer 先逾時才能砍 query、釋放記憶體並回精確 error code;等值會 race、gateway 先 504 而 Prometheus 仍在跑。`--query.max-samples` 與 pod `resources.limits.memory` **耦合**:cap 要當真正的 OOM 護欄,記憶體上限須裝得下「近上限查詢 + TSDB baseline」,故 pod limit 由 512Mi 上調 1Gi。global cap 也會誤殺平台自身的 recording/alerting rule —— 新增 `severity: critical` alert `PrometheusRuleEvaluationFailing`（`configmap-rules-platform.yaml`,監控 `prometheus_rule_evaluation_failures_total`),讓 rule eval 因撞 cap 而靜默失效時能被偵測,而非靠 YAML 註解。**範圍澄清**:本 repo 的 storage backend 是 raw k8s manifest 而非 Helm chart,VictoriaMetrics 為 BYO 整合（未由平台部署）—— 故 flag 落在 deployment manifest、issue 與 ADR Stage-4 的「Helm chart」措辭已修正;VM 對應 flag（`-search.maxUniqueTimeseries` 等）見 ADR §Blast radius Layer 1 表。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 1。
- **Federation 稽核日誌 + 異常 metric（ADR-020 IV-2f，issue [#511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/511)）**：`helm/federation-gateway` 補上 federation data-plane 稽核 —— Envoy access log 改成每 request 一筆結構化 JSON（`ts` / `tenant_id` / `token_id` / `method` / `path` / `query` / `status` / `duration_ms`），寫**兩個 sink**：`stdout`（持久、collector-ready 的合規軌跡）與 in-pod `emptyDir` 鏡像（供 metrics）。`query` 由獨立的 audit Lua filter **統一抽取** —— GET 從 URL query-string、POST 從 `application/x-www-form-urlencoded` body（`buffer` filter 提供 body，1 MiB 上限），取 `query=` 或 `match[]=`、URL-decode、截斷 2048；GET / POST 同一路徑、輸出格式一致。**filter 順序**：auth Lua（撤銷檢查 + header 注入）在限流器前、`buffer` + audit Lua 在限流器後 —— 被限流拒絕的請求不進 Envoy 記憶體緩衝，限流真正 bound 住 buffer 用量。新 metric `tenant_federation_requests_total{tenant,status}` 由 gateway pod 的 **mtail sidecar** tail `emptyDir` 鏡像產出（Envoy 原生 stats 無法產 per-tenant 高基數 label），`status` 為 HTTP code 分桶 enum（`ok` / `client_aborted`（status 0，client 提早中斷如 Grafana 取消查詢）/ `rate_limited` / `auth_failed` / `bad_request` / `backend_error`）；**logrotate sidecar** 以 10s 迴圈壓住鏡像大小（rename + Envoy `/reopen_logs`，不掉行；快迴圈 + 1 GiB emptyDir 上限防日誌洪流撐爆 pod 觸發 Kubelet 驅逐）。整組 metrics pipeline（sidecar + emptyDir + scrape）可由 `auditLog.enabled: false` 關閉 —— audit-sidecar image 尚未備妥時 gateway 仍能獨立運行（觀測 sidecar 不該能 down 掉主 gateway），stdout audit log 不受影響。新增 alert `FederationRejectionRateAnomaly` + `FederationGatewayBackendErrors`（`configmap-rules-platform.yaml` 的 `federation-audit` group，`severity: warning` — federation 屬平台自監控，併入 platform rule pack 而非另開 pack）+ `federation-audit` Grafana 儀表板。**架構修正**：原 ADR audit schema 的 `matched_whitelist_rule`（Data Plane Mirage 下查詢路徑不執行白名單）與 `series_returned`（Envoy 不解 response body 數 series）為幽靈欄位，砍除；`status` enum 砍掉不可能發生的 `rejected_whitelist`。**持久化邊界**：稽核日誌**不寫 PVC**（單一 RWO PVC 在 gateway 多副本 / `podAntiAffinity` 下無法掛載）—— durable 中央 forensic log store（Loki / SIEM）為 follow-up [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)；IV-2f 交付 aggregate 層（metric，本即 durable + queryable）+ collector-ready emitter。control-plane 稽核（簽 token / 改 whitelist）軌跡沿用既有的 token Record ConfigMap + GitOps commit 歷史，不另建。sidecar 兩者共用 image（`audit-sidecar/Dockerfile`，Alpine + mtail + logrotate）。chart version `0.1.1`→`0.2.0`。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Audit log + anomaly metric。
- **Federation admission validator（ADR-020 IV-2e PR-B，issue [#510](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/510)）**：在 PR-A 的 2-tier policy 之上補上 admission validator —— whitelist 加入新 metric 時，驗該 metric 是否真能 federate。tenant-api 首次新增 Prometheus client（`internal/federation/admission.go`），**只走 Series metadata API**（`GET /api/v1/series`）、不用 range query —— range query 會把 24h raw sample 載進記憶體、對高基數 metric 把 Prometheus 打到 OOM。三態：metric 有帶 `tenant` 的 series → **Pass**；有 sample 但**沒有任何** series 帶 `tenant` → **Hard block**（federate 後對所有租戶都是 empty vector）；24h 無 sample → **Warn**（cold-start / sparse metric 合法，需 `--force`）。判準是「**沒有任何** series 帶 `tenant`」而非「有 series 缺 `tenant`」—— K8s 共享叢集裡 `up` / `container_*` 等 metric 租戶 pod 帶 label、平台 pod 不帶，proxy 已隔離、平台 series 無害；探測用 `metric{tenant!=""}`（非空即 Pass，該 series 也是 PII 掃描的真實租戶樣本）。每次查詢三重 bound（`limit=1` + `io.LimitReader` + `context` 5s timeout），validator 自身不會變 DoS 來源；後端不可達 / timeout 視為 Warn。另含 **PII label-name heuristic**：label 名命中 `email` / `customer` / `user_ip` 等樣式 → advisory soft warning。`--force` bypass：`PUT /api/v1/federation/policy` body 加 `force` / `reason`，**hard block 不可 force**；soft warning（Warn / PII）force 通過時，user + reason + metrics 寫進該次 git commit message 的 `[Bypass-Validator]` trailer —— GitOps 不可繞、不 rotate 的稽核軌跡（trailer 欄位 CR/LF 淨化，防注入偽造）。admission 只驗**新增**的 metric（與現行 whitelist 取差集），並行檢查（bounded 8）、單次 PUT 新增上限 30；寫 git 前檢查 request context 未取消（防 timeout 後的殭屍寫入）。新 flag `--federation-prometheus-url`（空值停用 admission，whitelist 編輯僅 schema 檢查）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §前提約束。
- **Federation 2-tier policy schema + endpoint（ADR-020 IV-2e PR-A，issue [#510](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/510)）**：tenant-api 新增 federation 的 2-tier metric 政策 —— **platform whitelist**（maintainer 管，`_federation_policy.yaml`，平台策展、提供 federation 的 metric catalogue）+ **per-tenant subset**（租戶自選子集，`conf.d/_federation/<tenant>.yaml`）。**定位澄清**：whitelist 是 **governance / discovery** 機制，非 query-time 安全邊界 —— prom-label-proxy 只做 `{tenant="<X>"}` label 注入、無 metric-name allowlist 能力，跨租戶隔離 100% 來自 label 注入、與 whitelist 無關（ADR-020 §MVP 範圍 流程圖原列「proxy 拒絕白名單外 metric」係 hallucination，本 PR 一併修正，並補上 hard-revocation 警告：從 whitelist 移除 metric 擋不住已知名稱的查詢，緊急阻斷須走 ingestion 階段 `relabel_configs` drop）。新 endpoint：`GET` / `PUT /api/v1/federation/policy`（whitelist，PUT 需 platform admin —— 即透過 `tenants: ["*"]` 的 admin group；`HasPermission(groups, "*", admin)` 只有 `*`-scoped rule 會中）、`GET` / `PUT /api/v1/tenants/{id}/federation`（tenant subset，PUT 需該租戶 admin，門檻對齊 token 簽發 #509）。核心驗證為 **2-tier containment**：tenant subset 的每個 metric 必須在 platform whitelist 內，超出即 `400`；另驗 metric 名合法性（Prometheus grammar）與去重。`GET /tenants/{id}/federation` 採 **read-repair**：回傳前把存檔的 subset 與當前 whitelist 取交集 —— whitelist 縮減後既有 subset 檔會殘留過期 metric，讀取端取交集即得當前合法子集，毋須掃改租戶檔（GitOps mass-commit 災難）。兩層 policy **刻意分檔**：subset 一檔一租戶，租戶自助改 subset 不會在共用檔上互相 Git merge conflict，維持 per-tenant blast-radius 隔離（subset 不放進 `<tenant>.yaml` —— 該檔的 `PUT /tenants/{id}` 是 full-file replace，混放會被覆蓋）。驗證為手寫 Go（對齊 codebase 慣例，不引入 json-schema 依賴）。新增 `internal/federation/policy.go`（`PolicyManager` embed `configwatcher.Watcher`，SHA-256 熱載）+ handler + gitops `WriteFederationPolicyFile` / `WriteFederationSubsetFile`。此為 #510 的 **PR-A**；admission validator（Prometheus metadata 查詢 + 三態 + PII heuristic + `--force` commit trailer）為 PR-B。詳 [ADR-020](docs/adr/020-tenant-federation.md) §MVP 範圍 / §前提約束。
- **Tenant-api Helm chart federation 接線（ADR-020 IV-2m，issue [#519](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/519)）**：`helm/tenant-api` chart 新增 `federation` 區塊（預設關閉），把已 merge 的 federation token endpoint 實際接上 Helm 部署 —— 在此之前 endpoint 程式碼雖在 main，但 chart 沒掛簽章金鑰、沒接 token store，`helm install` 起不來。啟用後 chart：(1) 預建空的 `tenant-federation-store` ConfigMap —— tenant-api 在 runtime 才寫 `store.json` / `revoked.txt`，template 刻意不渲染 `data`，Helm three-way merge 因此不會在 `helm upgrade` 時重置它、不會清掉有效 token（`resource-policy: keep` 再擋 `helm uninstall`）；(2) 加一組 `Role` + `RoleBinding`，以 `resourceNames` 把 tenant-api 的 K8s API 權限鎖死成「對那一個 ConfigMap 的 `get` + `update`」，無 namespace-wide `create`；(3) 掛載 `da-tools fed-key`（IV-2l）帶外產出的簽章金鑰 Secret 為 `defaultMode: 0440` volume（檔主為 root、process 非 root，靠 fsGroup 65534 群組位讀取）；(4) pod 開回 `automountServiceAccountToken`（in-cluster client 需要，ServiceAccount 預設關閉）；(5) 透過 values 暴露 `--federation-key` / `--federation-store` / `--federation-token-ttl` 三個 flag。既有部署的啟用步驟見 `values.yaml` 的 `federation` 區塊註解。chart version `2.8.0`→`2.9.0`。注意：issue #519 body 寫的「store 是 pod-local JSON 檔、MVP 限 `replicaCount=1`」已被 Posture B（#520 ConfigMap-backed store）取代，本實作以 ConfigMap store 為準。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Token model。
- **Federation 簽章金鑰 bootstrap / 輪替（ADR-020 IV-2l，issue [#518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/518)）**：新增 `da-tools fed-key` 命令（`scripts/tools/ops/federation_keygen.py`）—— 產生 / 輪替 federation JWT 的 RS256 簽章金鑰。私鑰直接吐成 Kubernetes Secret manifest 到 stdout（`da-tools fed-key | kubectl apply -f -`，記憶體→pipe→etcd,不落地、不進剪貼簿）；stdout 為互動式終端時拒絕輸出（防 operator 漏接 `| kubectl` 把私鑰印進 terminal scrollback）；公鑰寫成 JWKS 檔供 federation-gateway 的 `jwt.jwks`。每把公鑰的 `kid` 是它的 **RFC 7638 JWK thumbprint**；`--rotate --existing-jwks` 把新公鑰併入現有 JWKS（kid 區分舊新,grace-period overlap）。tenant-api 同步改動：簽 token 時對載入金鑰算同一個 RFC 7638 thumbprint、注入 `kid` JWT header —— gateway 的 `jwt_authn` 因此能用 `kid` O(1) 選鑰,不必遍歷 JWKS（關閉輪替期「壞簽章 flood × N 把鑰 = N 倍 RSA」的放大攻擊面）。輪替標準流程（計畫性 grace overlap / 私鑰外洩緊急汰換）見新增的 [`federation-key-rotation-runbook.md`](docs/internal/federation-key-rotation-runbook.md)。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Token model。
- **Federation API gateway Helm chart（ADR-020 IV-2b，issue [#507](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/507)）**：新增 `helm/federation-gateway` chart — ADR-020 Layer 2 的 federation API gateway，以 **Envoy**（`distroless-v1.38.0`）實作。它是「簽發 token 不做 server-side revocation list」的對價控制。每個 request 走 cheap-before-expensive 的 filter chain：per-IP 粗粒度 rate limit（在任何 crypto 前先擋偽造 token flood）→ `jwt_authn` RS256 驗章（local JWKS + jwt_cache + 60s clock-skew leeway，`from_headers` only 故 URL 帶 token 一律拒絕、不會進 log）→ Lua filter（查 revoked-set + 把驗證過的 `tenant_id`/`token_id` 用 `replace()` 覆寫進 trusted header，故 header spoofing 結構上不可能）→ per-token + per-tenant 雙層 `local_ratelimit`（防單一 token 濫用 + 防租戶 round-robin 16 個 token 的 Sybil）→ 轉送 upstream。`mode` 二選一：`prom-label-proxy`（注入 header 轉 Layer 3 proxy）或 `vm-cluster`（rewrite path 到 `/select/<tenant_id>/prometheus/` 轉 vmselect）。revoked-set 由 tenant-api 寫進 `tenant-federation-store` ConfigMap、gateway 掛載後 Lua 以 time-gated cache 重讀（tmpfs projected volume，microsecond 記憶體讀；缺檔 fail-open）。audit log 在此層做（JSON access log 帶驗證過的 claim）。rate limit 為 per-instance 軟性控制 — 硬上限是 Layer-1 storage cap。Day-4 resiliency 比照 IV-2a（HPA / PDB / anti-affinity / graceful shutdown）。`envoy --mode validate` 驗證 config 通過。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 2。
- **Federation read-path proxy Helm chart（ADR-020 IV-2a，issue [#506](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/506)）**：新增 `helm/federation-proxy` chart — ADR-020 Layer 3 的租戶隔離 proxy，部署 `prom-label-proxy`：改寫 PromQL、把 `{tenant_id="<X>"}` 強制注入每個 selector 與 metadata API，front 任何相容 Prometheus query API 的後端（Prometheus / Thanos / VictoriaMetrics 單機）。實作盤點修正了 ADR 的兩個架構誤解：(1) vmauth 是 auth router、**不**解析 PromQL 也不注入 label；(2) vmauth 靠靜態 `auth.yml` 路由，無法消化動態簽發的 federation JWT — 故 **vmauth 不納入本 chart**，VM cluster 的 Layer 3 隔離改由 gateway（IV-2b）直接 URL rewrite 到 accountID 路徑處理。metadata API enforcement（`-enable-label-apis`）hardcode 不可 override；`-error-on-replace` 刻意不啟用（預設靜默覆蓋租戶 label — 隔離等價但允許 SRE 直接複製貼上帶 `tenant_id` 的 query）；NetworkPolicy 預設限定只有 federation gateway 能連入（proxy 信任 gateway 設的 tenant header，跳層即破防）；HPA 依 CPU 70% 擴容。prom-label-proxy image pin `v0.13.0`。Day-4 resiliency：soft pod anti-affinity（replica 跨節點分散）+ PodDisruptionBudget（node drain / cluster upgrade 保活）+ 原生 `preStop.sleep` 與 `terminationGracePeriodSeconds` 45s（rollout / scale-down 不腰斬 in-flight query）+ `GOMEMLIMIT`（防 AST 解析 burst 撞 cgroup 上限被 OOMKill）。audit log 因 prom-label-proxy 無原生支援，移交 gateway（IV-2b）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 3。
- **Tenant federation token endpoint（ADR-020 IV-2，issues [#509](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/509) / [#520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/520)）**：tenant-api 新增 `POST` / `GET` / `DELETE /api/v1/federation/tokens` — 為租戶簽發短效（預設 4h）RS256 JWT，供其向 label-injection proxy（vmauth / prom-label-proxy）拉取自己的 metrics 子集回租戶側 infra 自管（ADR-020 §Token model）。簽發需對目標租戶具 `admin` 權限（資料域外持出，門檻高於 config write）；token claim 帶 `tenant_id` / `token_id`（proxy 注入 label、gateway 取 rate-limit key 的跨組件契約）+ `aud=tenant-federation`（防 cross-service replay）。`DELETE` 為真撤銷（ADR-020 Posture B）：移除 bookkeeping record 並把 token id 寫入 gateway 消費的 revoked set，最終一致 — 約 1-2 分鐘內隨 ConfigMap projected-volume sync 生效。token record 存於跨 replica 共用的 Kubernetes ConfigMap（`--federation-store` 指定其名、Helm chart 預建），tenant-api 維持 stateless、可多 replica，不入 db 也不入 git conf.d。濫用防線：每租戶同時最多 16 個有效 token、每分鐘簽發上限，超出分別回 `409` / `429`。新增 `internal/federation` package（RS256 簽章器 + ConfigMap-backed record store）；`--federation-key` 未設時整個 endpoint 不註冊。詳 [ADR-020](docs/adr/020-tenant-federation.md)。

### Changed

- **ADR-024 精煉重寫——降認知門檻、對齊已落地實作（[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) epic 收尾後文件收斂）**：ADR-024 隨整個 epic 有機長大成 739 行，混了架構決策與 S1-S8 逐切片的 build journal、外審輪數、OQ/R/AC 編號清單、戰略弧/北極星/三-Plane 等內部黑話 —— 讀者難在其中抓到關鍵決策。**重寫為 ~225 行的清晰 ADR**：lead-with-decision（「一個引擎、兩個能力」+ 7 條關鍵決策，每條附 trade-off + 範例）、資料流（Ingest→Define→Compile）、復用既有機制、Consequences（含驗收不變式）、否決的替代方案、與 §2.6 界線。**所有 durable 架構決策按主題折入並逐條確認存活**（dimensional label / normalize+per-severity / recipe 模型+階層 scope / 向量化成本誠實 / 雙層驗證 / silent sentinel+inhibit / forecast / route-to-null 與 Array→Dict 之否決）；砍掉的是 build-process 外衣（切片 ID、外審輪數、編號清單、海量 file:line / issue#、內部 codename）。**更正過時內容**：status 由「實作進行中」改「已隨 v2.9.0 落地」；補 ops-review 工具的 `_custom_alerts` UNION 繼承一致性 consequence（[#772](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/772)）。ZH/EN 雙語結構同步（10 h2 / 7 h3）；bilingual 結構 0 錯誤、codename-gate 0 violations、mkdocs strict PASS。
- **Lint adoption policy：open-source engine + Vibe wrapper（取代 DIY-only，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-315）**：新 lint 預設採「既有 open-source engine（hadolint／kube-linter）優先 + Vibe wrapper 疊上專案政策（severity／中央 exemption／scope）」，取代過去逐案 DIY `check_*.py` 的 reactive whack-a-mole。**僅 greenfield 套用** —— 既有 ~50 支 DIY lint 不回頭遷移（明列 out of scope）。Container/k8s IaC SAST 4 層（TRK-311~314）為首批落地：L1 Dockerfile（hadolint）、L2 Helm template + L4 raw k8s manifest（kube-linter）、L3 values/manifest secret-shape（純 Vibe wrapper —— YAML-shape 檢查無對應 open-source engine）。統一 Severity→Action（Critical → BLOCK required-check／High → 中央 EXEMPTIONS 列管／其餘 INFO）+ consolidated 4 層 baseline（0 Critical / 11 baseline-High）收斂於 `docs/internal/iac-lint-baseline.md`；hybrid policy 規範寫入 `dev-rules.md §安全紀律`。
- **da-tools UX consistency sweep — exit-code 0/1/2 SSOT 統一（epic [#452](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/452)）**：新增 `scripts/tools/_lib_exitcodes.py` 作為 Python 工具與 Go binary（da-guard/da-parser/da-batchpr）共用的 exit-code SSOT——`EXIT_OK=0`（乾淨）/ `EXIT_VIOLATION=1`（user-actionable 發現）/ `EXIT_CALLER_ERROR=2`（bad args／檔案不存在／連線失敗／malformed 輸入／缺前置／crash）。掃過 `scripts/tools/{ops,dx,lint}/` ~160 個工具：將 caller/環境錯誤從混用的 `1` 正規化為 `2`、改 import 具名常數取代 magic number；收斂三套既有 ad-hoc 慣例（`_lib_godispatch`／`trufflehog_to_sarif`／`diag_pr_ci`）改綁 SSOT。**Track C**：customer-facing `check-alert` / `diagnose` 補顯式 `--json` flag（JSON 本為預設輸出，維持既有 `scenario-d.sh` / `batch_diagnose` parser 消費者，行為不變）。**Track D**：`components/da-tools/README.md` 補「選擇安裝路徑」章節指向 `docs/migration-toolkit-installation.md`，並點明 Docker image=全部工具、static binary=僅 3 顆 Go CLI、本機 checkout=Python 工具。**規範**：`dev-rules.md` 新增 §13（新子命令須守 0/1/2 + 提供 `--json` + Go binary 不引入 `--ci`）；`testing-playbook.md` 新增 exit-code 合約章節；`test_tool_exit_codes.py` 由「invalid args ≠0」收緊為「**恰好 exit 2**」+ 驗 SSOT 常數。**Scope 註記**（vs 原 epic）：Track B（Go 補 `--ci`）經查證為解假想題（無跨 Python/Go 統一 wrapper 消費者）→ resolved as no-op（改在 §13 釘約定不改 code）；Track D 大部分已由既有 `docs/migration-toolkit-installation.md` 滿足。**認可例外**（非純 0/1/2，文件載明、未改）：`diag_pr_ci.py`（0/1/2/3，exit 3=network-blocked）、`tenant-verify`（倒置契約 2=驗證失敗，rollback runbook checklist 第 6 項依賴——customer-facing 契約刻意保留未變）。非 breaking：consumer 一律 non-zero=fail，已確認無 CI/Makefile/pre-commit 對這些工具做 `==1` 特定碼分流。`dev-rules.md` size cap 520→535（§13 成長，附理由）。
- **tenant-api server timeout + body-size 改 Helm-tunable（issue [#144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/144)）**：`tenant-api` 的 HTTP server timeouts 與每 handler 1 MiB request body cap 從 hardcoded 改為 `TA_READ_TIMEOUT` / `TA_WRITE_TIMEOUT` / `TA_IDLE_TIMEOUT` / `TA_MAX_BODY_BYTES` env 驅動（時間 timeouts 早於本 issue 已 env-driven，本 PR 補 body cap + 全部接 Helm value）。`helm/tenant-api` 新增 `tenantApi.server.{timeouts.read,timeouts.write,timeouts.idle,maxBodyBytes}` values（預設值對齊 binary built-in，default upgrade 為 no-op），chart 條件式發 env vars（未 override 時不長 env block）。malformed env（負數、0、非數字）→ `slog.Warn` + fallback 預設，沿用 `RateLimitConfigFromEnv` pattern。8 個 handler 的 `io.LimitReader(r.Body, 1<<20)` 統一走 `d.MaxBody()` helper（unset 時 fallback `DefaultMaxBodyBytes`，避免 12 個既有 test fixture 連帶改動）。Chart `2.9.0`→`2.9.1`。`docs/api/tenant-api-hardening.md §5.2` 標 "moved to Helm"。詳 [ADR-009](docs/adr/009-tenant-manager-crud-api.md)。

### Fixed

- **tenant-api Deployment 補 `strategy: Recreate`，消除滾動更新交疊期的「幽靈副本」多寫者破口（issue [#677](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/677) / TRK-324，ADR-023 §2）**：`replicaCount: 1` **不等於**執行期單寫者 —— `helm/tenant-api/templates/deployment.yaml` 無 `strategy:` 欄位 → 預設 RollingUpdate（`maxSurge` 進位 1），`kubectl rollout restart` / 改 env 發版時 K8s 先起新 pod、待其 Ready 才 SIGTERM 舊 pod → **數十秒交疊內兩個 Ready pod 各持 in-process 寫鎖 + pending-PR tracker**（皆 per-process、不跨 pod 協調，見 [#615](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/615) 與 `internal/platform/tracker.go`）對同一 git 寫入平面同時動手：`confDir.type: emptyDir`(預設) 為各自 push 的 dual-writer race、RWO PVC 為共享 tree 損毀 / 跨節點 Multi-Attach 卡死。Helm 靜態檢查抓不到此 runtime 交疊。**修法（零程式碼）**：寫入端 deployment 設 `strategy: type: Recreate`（鏡像 `helm/victorialogs` 的 RWO Recreate 先例），殺舊再起新消除交疊窗口；代價為部署期短暫不可用 —— **非新 regression**（`replicaCount:1` 讀取本就無 HA）。水密艙版（K8s Lease / `client-go` leaderelection、新 pod 未持 Lease 則 `/ready` 失敗不接寫入）**deferred** 至寫入部署需 zero-downtime 時。chart version `2.9.2`→`2.9.3`；`helm template` 驗證 strategy 區塊正確渲染（replicas:1 → strategy: Recreate）。
- **P0 — 11/14 rule pack 的告警閾值在 prod 靜默失效（exporter strip metric prefix、rule pack 查完整名）+ mariadb 潛在 cross-component 撞名，並加 AST contract test 永久封死迴音壁（bug [#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731)）**：threshold-exporter 的 `parseMetricKey` 把 conf.d key 按第一個 `_` 切成 `(component, metric)`，collector **一律** emit `user_threshold{component=<prefix>, metric=<stripped>}`（如 `redis_connected_clients` → `{component="redis", metric="connected_clients"}`）；但 11 個 pack 的 threshold-normalization recording rule 查**完整前綴名** `user_threshold{metric="redis_connected_clients"}` → 與真實輸出對不上 → `tenant:alert_threshold:<key>` recording rule **空集合 → 告警永不觸發（fail-silent，最糟失效模式）**。promtool fixture 手寫 `user_threshold` 形態、且寫成「對上 broken 查詢」的完整名 → 全綠的迴音壁（kubernetes 不小心寫對 `{component="container", metric="cpu"}` 才一直正確）。**修法**：12 個 pack（11 broken + mariadb）的 selector 統一改 `user_threshold{component=<prefix>, metric=<stripped>}`——`(component, metric)` 是 conf.d key 的無損 re-encoding，與原意 100% 等價且跨包零撞名。**深挖補強**：mariadb 原查裸 `user_threshold{metric="connections"}`（無 component），但 `pg_connections`/`nginx_connections`/`rabbitmq_connections` strip 後皆 `connections` → 單租戶跑多元件時 `max by(tenant)` 跨元件取錯閾值；故 mariadb 也補 `component="mysql"`。**防迴音壁 contract test**（`components/threshold-exporter/app/rulepack_contract_test.go`）：用 Prometheus 官方 PromQL AST parser（`prometheus/prometheus` 既有 dep、非 regex）走訪**全 14 包所有** `user_threshold` selector，斷言必含 `component`+`metric` 精確(`=`)matcher 且 `(component, metric) == config.ParseMetricKey(record-name)`（同 exporter 唯一真相、零跨語言 reimpl）；含反空集合護欄（< 14 包 / < 44 selector 即 fail-loud）、前瞻命名護欄（digit-leading metric → version 應進 `{version=}` label）、fixture 防退化掃描。全 12 包補 promtool 行為 fixture（`tests/rulepacks/rule-pack-*-threshold_test.yaml`，真實 exporter shape 證明 recording rule 非空）。pre-commit `forbid-legacy-mariadb-threshold-name`（pygrep hard-gate，CHANGELOG 豁免）擋裸名復活。⚠️ **BREAKING（純內部閉環，外部 dashboard/federation 需遷移）**：mariadb 的 recording rule 由裸 `tenant:alert_threshold:{connections,cpu,connections_critical,cpu_critical}` **正規化為 `mysql_*` 前綴**（與 conf.d key `mysql_*` 一致），連帶修好 [#719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/719) `threshold-recommend` observed-map 的 mariadb 查找 miss（`mysql_cpu` 並自動從 needs_review 解析）。外部若直接查詢這些 recording rule，請全域搜 `tenant:alert_threshold:connections`/`:cpu` 換成 `:mysql_connections`/`:mysql_cpu` 前綴。**部署期註記**：config reload 後舊 series 進入 ~5 分鐘 TSDB staleness、新名即時生效，告警無影響，值班交接期勿把殘留舊 series 誤判為異常。configmap + operator-manifest + observed-map 同步重生。詳 [#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731)。
- **mariadb CPU 補 warning + critical 告警，消除孤兒 `cpu_critical` 閾值（#719 自審衍生）**：`rule-pack-mariadb` 定義了 `tenant:alert_threshold:cpu_critical` recording rule，但**無任何 alert 引用**（CPU 只靠 composite `MariaDBSystemBottleneck` 用 warning 級 `cpu`）→ critical 閾值設了也永不觸發（與 connections 等 pack 不對稱）。新增獨立 `MariaDBHighCPU`（warning）+ `MariaDBHighCPUCritical`（critical），鏡像 connections pair 結構（maintenance unless + metadata left-outer-join 真空裸火），消費孤兒並補齊 CPU severity 雙階。新增 promtool 契約測試 `tests/rulepacks/rule-pack-mariadb-cpu_test.yaml`（enriched / vacuum / maintenance-suppressed / below-threshold × warning+critical，**這缺的測試正是孤兒能長存的原因**）。configmap + operator-manifest + ALERT-REFERENCE + rule-pack-stats + platform-data 同步重生（alert 總數 114→116）。
- **`threshold-recommend` 查錯 metric 修復 — 改查觀測 recording rule（bug [#719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/719)，#457 STAGE-1 前置）**：`threshold_recommend` 過去查 `user_threshold{key="..."}[lookback]` 取百分位推薦——**雙重錯誤**：(1) `user_threshold` 的 label 是 `{tenant, metric, component, severity}`、**沒有 `key`**（`collector.go`），prod 比對不到任何 series → 每個 key 都「no data points」=**長期非功能**；(2) 即使修對 label，`user_threshold` 是 operator 已**設定**的閾值值（config-driven gauge），對它取 P95 = 回音室（用過去人工設定推未來設定）。**修法**：改查每個閾值 key 在 rule-pack alert 中**實際比對**的觀測 recording rule（如 `tenant:mysql_threads_connected:max`）——該 series 與閾值同單位/拓撲（alert 直接比較），故 P95(觀測) 即可直接當建議值、無需單位換算。新增 SoT 對映 `scripts/tools/ops/metric_observed_map.yaml`（`conf.d key → 觀測 series`，60 key、55 clean / 5 needs_review），由 `threshold-recommend --generate-observed-map`（包含性掃描 rule-pack alert，**非** PromQL AST 解析——避 #709 多行 regex 災難）自動產生；無對映 / 下界(`<`) / version-aware(`tenant_version` scope) / 待人工解析的 key 一律 **fail-loud skip**（附原因），取代過去靜默回傳空集合。新增 CI drift-guard `check_threshold_observed_map.py` + pre-commit hook `threshold-observed-map-drift-check`（map↔rule-pack 不一致 → 紅；known-deferred(`container_cpu`/`container_memory` version-aware，#721-7)→ INFO 綠燈、orphan/coverage→ WARN）。`baseline_discovery`(Day 0 冷啟動 raw-exporter 粗估) 與 `threshold_recommend`(Day N recording-rule 精確微調) 的領域邊界寫進兩者 docstring（⛔ 勿合併）。衍生 deferred（下界 direction-aware 推薦、version-aware 全量覆蓋）→ [#721](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/721) 項目 6/7。
- **tenant-api 寫入路徑驗證對稱化：tenant-only PUT body 不再被誤擋（ADR-024 PR4 探索發現，epic [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)，延伸 [#704](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/704)）**：`GET /tenants/{id}`、`GET /{id}/effective`、`POST /{id}/validate` 三條讀/驗證路徑都會 merge `_defaults.yaml`，唯獨 `PUT` 的 `gitops.validate()` 不 merge → 對任何 metric key（含 ADR-024 `container_cpu{version="v2"}`）warn「unknown key not in defaults」並 block 寫入；連 dry-run `/validate` 剛判定 valid 的 tenant-only body（生產 `conf.d/{id}.yaml` 形態，註明「Only 'tenants' block」）都會被寫入端拒絕（「驗證說 OK、儲存說 NO」）。把 root-defaults merge 邏輯上提為 `cfg.MergeTenantWithRootDefaults`，GET / validate / 寫入三條路徑共用同一實作消除分歧；body 仍 verbatim 寫入（不把 defaults 污染進租戶檔）。修正讓 version threshold 宣告 UX 乾淨化，並新增 `writer_test` 覆蓋（先前成功案例全靠 reserved key 繞過此盲點）。**同時收斂寫入邊界契約（fold [#705](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/705)）**：新增 `cfg.CheckTenantRootKeys` enforce「tenant body 只准 top-level `tenants` block」（對齊 `docs/schemas/tenant-config.schema.json` 的 `additionalProperties:false`），PUT 寫入路徑與 `POST /validate` dry-run 共用此檢查 → 夾帶 `defaults:`/`state_filters:`/`profiles:`/typo 的 body 一律 fail-loud 400（而非被 verbatim 寫成髒檔：scanner 雖會 strip 不致跨租戶污染，但髒檔會經 GET→edit→PUT round-trip 持續回灌 + 誤導 SRE 排錯）。

- **rule-pack-kubernetes 容器告警的 metadata enrichment 由 inner-join 改 left-outer-join，消除「新租戶 onboarding 真空期」告警盲區（ADR-024 [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) PR3-pre-3 pilot）**：`PodContainerHighCPU/Memory` 原本以 `* on(tenant) group_left(runbook_url,owner,tier) tenant_metadata_info`（**inner join**）富化標籤 → 對「pod 已吐 metrics 但 `conf.d/` 租戶 config 尚未熱載入完 `tenant_metadata_info`」的 onboarding 真空期，**整條告警向量塌成空 → 核心告警靜默**（系統最不穩時失明）。修法（Gemini 外審 + sub-agent 路由分析）：抽 `rule_pack_kubernetes:pod_container_high_{cpu,memory}:core` recording rule（threshold-cross + maintenance 抑制、無 metadata），alert 改**互斥 left-outer-join**：`(core * group_left(..) metadata) or (core unless on(tenant) metadata)`——metadata 在→富化 fire（branch A）、不在→裸 fire（branch B，少 runbook_url 但 SRE 仍收到通知），兩分支互斥剛好一條。**只 pilot pack**（其餘 13 pack Phase 2 sweep；簡單 alert 如 `redis_up==0` 不抽 core）。新增 promtool 行為測試 `tests/rulepacks/rule-pack-kubernetes_test.yaml`（leaf inputs 跑全 recording chain，4 租戶：富化 fire / **真空期裸火** / 低於閾值不發 / 維護抑制）+ CI `make rulepack-promtool-test` gate（裝 promtool）。**注**：通知端富化（Alertmanager template 查表）更優雅但 Phase 1 否決（不支援動態查表），記 #692 operator 終極方向。
- **4 個 rule pack 的 `user_threshold` 聚合在 source/operator 副本誤用 `sum`，HA 多副本下閾值翻倍告警失效（修 ~2 個月無聲 prod drift；新增 HA-max lint 防復發，ADR-024 [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) PR3-pre 深挖出）**：`docs/design/high-availability.md §4.3` 早已規範——threshold-exporter 預設 `replicaCount=2`、無 leader-election、`user_threshold` metric 無 per-pod label（`collector.go` label set=`{tenant,metric,component,severity}`），故 Prometheus 抓到**兩條相同 series**；`sum by(tenant)(user_threshold)` 會把兩副本相加 → 閾值翻倍（70→140）→ 告警永不觸發，必須用 `max`。commit `2bc7d77b "support HA"` 當時只把 **configmap**（live 部署副本）改成 `max`，**source `rule-packs/` 與 `operator-manifests/` 兩份副本沒同步、仍 `sum`**——影響 **elasticsearch / kubernetes / mongodb / redis** 共 13 條 `tenant:alert_threshold:*`（×2 副本=26 處）。operator-mode 部署者一路吃此 HA 雙倍 bug 約 2 個月。本 PR 把 4 pack 的 source+operator `sum→max` 對齊 live configmap。**並加 lint 機械化此不變式**：`scripts/tools/lint/check_ha_threshold_aggregation.py`（pre-commit `ha-max-threshold-aggregation`，FATAL）斷言聚合 `user_threshold` 的 **operator 必為 `max`、禁 `sum`**；**只判 operator、不約束 `by()` 標籤**（Gemini 外審：死卡 `by(tenant)` 會把 `container_cpu{env="prod"}`+`{env="test"}` 拍平丟維度——維度保留與 operator 是兩件事）；real metric 的 `rate/sum` 不受限。9 個 self-test（sum/avg/min flag、max+多維 clean、real-metric 不誤判、token 相鄰空白變體、live dogfood 全 pack 已 max）。
- **threshold-exporter per-tenant cardinality 截斷改為確定性，消除 over-cap 租戶的 alert flapping（[ADR-024](docs/adr/024-version-aware-threshold-via-dimensional-label.md) AC-7，epic [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)；硬化 [#652](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/652) runtime truncation 路徑）**：`pkg/config/resolve.go::ResolveAtWithStats` 的 per-tenant series cap 以 `result[:startIdx+limit]` 截斷,但該 slice 由 Go map 迭代（`Defaults` + tenant `overrides`,**process 內隨機序**）append 而成 → 一個越過 cap 的租戶**每次 scrape 被截掉的子集都不同** → 存活的告警 series 在 Prometheus 忽隱忽現（alert flapping + PagerDuty 重複轟炸,分散式觀測最忌的非確定性）。修正：截斷前對該租戶 segment 以穩定鍵排序 —— **未版本化 / `version="default"` 閾值為 tier 0（優先保護、必留）**,顯式版本化（`{version="v2"}`）為 tier 1、由字典序末位開始丟,故被丟的版本每次 scrape 固定不變（穩定消失 → 觸發 over-limit gauge 而非閃爍）。為 ADR-024 Version-Aware Threshold epic 的 Phase 1 基礎件,且**獨立於 version feature 即修補既有 #652 截斷路徑**。新增 `truncationSortKey` 雙 tier 排序鍵 + `canonicalLabelKey` helper;單元測試 40 次迭代斷言 survivor set 完全一致（flapping repro）、unversioned/default 永遠存活、丟的是 lexicographic tail。
- **3 個 component chart 的 image 預設改由 `Chart.appVersion` 推導，根除 image-tag 漂移(issue [#682](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/682)，supersedes 過渡方案 [#683](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/683))**：threshold-exporter / tenant-api / da-portal 三 chart 過去各自硬寫 image tag —— exporter 預設 `threshold-exporter:dev`(+`pullPolicy: Never`、repo 無 `ghcr.io/vencil/` 前綴)是純本機 dev 值，tenant-api `2.7.0` / da-portal `2.8.0`(及 tier1/tier2 `2.5.0`)則**缺 `v` 前綴 → 對應 image 不存在**，裸 `helm install ./helm/<chart>/` 一律 ImagePull 失敗。改採 canonical Helm pattern：deployment template 用 `{{ .Values.image.tag | default (printf "v%s" .Chart.AppVersion) }}`、`values.yaml` 的 `image.tag` 留空，tag 永遠解析為 `ghcr.io/vencil/<chart>:v<appVersion>` —— **正是 release pipeline L3 digest-verify([#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445))保證可拉取的同一不變式**，單一 SoT、不漂移、裸 install 即可用。tenant-api 推導為 `v2.7.0`(非 `v2.8.0`)是**刻意尊重 chart-version↔appVersion 的合法 decoupling**(CHANGELOG #445 條已記載 binary 仍 2.7.0)，不 bump appVersion。本機 kind-load 開發改帶 `-f environments/local/threshold-exporter.yaml`(已 pin `:dev`+`Never`、不受影響)。da-portal tier1/tier2 的壞 `2.5.0` 一併消除。新測 `tests/helm/test_image_tag_derivation.py`(static 守 template 表達式 + helm-gated render 斷言 `v<appVersion>`，13 case)取代 #683 的 values-prod pin test。文件 `for-platform-engineers.md{,.en}` 三個 install 簡化為裸 `helm install`。**順帶補洞**：`validate_docs_versions` 的 image-tag v-prefix lint（`BARE_TAG_PATTERN`）原本只認 `da-tools`/`threshold-exporter`、**不認 `tenant-api`/`da-portal`**，故 `k8s/04-tenant-api/deployment.yaml` 一個缺 `v` 的 `tenant-api:2.7.0`（image 不存在、`IfNotPresent` 套用會 ImagePull 失敗）長期未抓到。本 PR 擴 pattern 涵蓋 4 個 component image + 加 lint self-test，並修掉新抓到的 3 處（k8s manifest 真 bug + tenant-api README ×2 build 範例補 `v`）。**刻意不加 appVersion currency/staleness lint**：五線 decoupling（appVersion 2.7.0 合法 ≠ 平台協同 tag v2.8.0）讓「是否落後」機器無法判定，硬加會在 tenant-api 此 case false-positive；image 真存在的權威 gate 維持由 release-time #445 digest-verify 負責。
- **tenant-api SSE `/api/v1/events` liveness：heartbeat + per-write deadline，修 slow-client goroutine leak(issue [#143](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/143))**：SSE hub 過去沒有 per-client liveness —— 卡住 / 半開的 client 無限期佔住 serving goroutine。原 issue 提的「idle timeout 到時關線」是錯的設計（單向 SSE 沒有 client read activity 可量、會打健康閒置連線），且與 §5.1 的全域 `WriteTimeout` 互打（30s 後第一次寫入就砍斷長連 SSE）。改採標準 SSE liveness 模式：(1) **豁免全域 WriteTimeout** —— `http.NewResponseController(w).SetWriteDeadline(time.Time{})` 清掉 server 寫入 deadline，讓長連 SSE 活得過 30s；(2) **heartbeat**（`TA_SSE_HEARTBEAT`，預設 25s）週期寫 `: keepalive`，既防 proxy 收閒置連線、又 **load-bearing** —— 保證週期性寫入嘗試讓 per-write deadline 能對「閒置零流量」卡死 client 觸發（goroutine 卡在 `<-ch`、兩 heartbeat 之間無 in-flight 寫入時 deadline dormant）；**`0s`=停用會重開 leak**、且須 < 下游 proxy 最小 idle timeout；(3) **per-write deadline**（`TA_SSE_WRITE_TIMEOUT`，預設 10s）每次寫入前設 deadline，卡死 client 寫入最多 block 這麼久即 error → goroutine return 回收，worst-case 清除 ≈ heartbeat+write-timeout（~35s，前方有 buffering proxy 時此為下限、實際更慢，Gemini 外審注記）；(4) 可選硬上限 `TA_SSE_MAX_LIFETIME`（預設 `0s`=停用）到時送 `{"type":"close"}` 關線。新 metric `tenant_api_sse_clients` gauge（連線數==goroutine 數，穩定 client 數下攀升即 leak 訊號）。三 env 在 `helm/tenant-api` 以 `tenantApi.sse.{heartbeat,writeTimeout,maxLifetime}` 暴露（預設對齊 binary、default upgrade no-op；malformed→`slog.Warn`+fallback），chart `2.9.1`→`2.9.2`。env 命名用 duration 形式（對齊既有 `TA_*_TIMEOUT`，非原 issue 的 `_SEC` 整數）。Gemini 外審確認設計 + 補強 heartbeat↔deadline 依賴文件。測試：stuck-client → goroutine 回 baseline（ClientCount→0，deterministic proxy 取代 flaky NumGoroutine）+ heartbeat 發出 + WriteTimeout 豁免（SetWriteDeadline 零值）+ max-lifetime close + **mutation dogfood**（停掉 per-write deadline → stuck-client test 報 goroutine leak，證明 deadline load-bearing）；`-race` 全綠。`docs/api/tenant-api-hardening.md §5.3`（+ `.en`）標 resolved。
- **tenant-api forge-write 韌性：circuit breaker + mergeable visibility(umbrella [#632](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/632)，closes [#645](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/645) + [#646](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/646)；#615/#616 Gemini review backlog)**：#632 三項中第一項（polling-staleness 409）已由 [#644](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/644)/[#648](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/648) ship，本 PR 收尾剩兩項。**(A) Circuit breaker（#645）**：degraded on-prem GitHub/GitLab 下，forge HTTP client 原本每個請求硬等 `http.Client{Timeout:30s}` 才失敗。新增 `platform.CircuitBreaker`（包 `sony/gobreaker/v2`，zero-transitive-dep —— 選 library 而非 DIY 因 half-open + generation-counter 狀態機正是易寫錯的並發碼，且符合專案 lean-dep/hybrid policy；repo 的 sibling rate-limiter 雖 DIY 但那只是 trivial sliding window，不轉移）。每 provider 一個 breaker 包在 github/gitlab client 的 `do()`/`doRequest()` chokepoint：連續 5 次 forge degradation（5xx/network/timeout）跳閘 open → 後續請求即時回 `platform.ErrCircuitOpen`（不打 forge），handler 映射 HTTP **503 `FORGE_UNAVAILABLE`**（sanitized retry-hint，不洩內部字串）；60s 後 half-open 單探測恢復。**關鍵語意**：`IsSuccessful` 讓 403/404/409/422 等 deterministic client outcome **不跳閘**（否則一個 token 壞掉的租戶會害全體開斷路）。**(B) Mergeable visibility（#646）**：`PRInfo` 加 tri-state `Mergeable`；GitLab 從 list-MR 的 `detailed_merge_status` 免費擷取（GitHub list-PRs API 不回 mergeable → 留 `unknown`，**刻意不加 per-PR GET** 省 rate limit，符合 issue「narrow surface」框架）。tracker `Sync` 對 conflict MR 發 `slog.Warn` + `tenant_api_forge_pr_conflicts{provider}` gauge。**誠實標註**：tenant-api PR conflict 近乎不可能（ClaimTenant dedup 確保每租戶單一 pending PR、各動唯一 `{tenantID}.yaml`；shared `_groups`/`_views` 是 direct-commit 非 PR）—— 純 defense-in-depth observability，捕 out-of-band edit（手動 PR commit、base force-push）。新 metric `tenant_api_forge_circuit_state{provider}`（0=closed/1=half-open/2=open）。Gemini review（PR #625）促成 breaker library 選型 pros/cons + DIY 否決。測試：breaker state machine（closed→open→half-open）+ `isForgeDegradation` table（4xx no-trip / 5xx+network trip）+ github client **integration test**（真 httptest 連續 503 → `ErrCircuitOpen` 且 open 後不再打 server）+ mutation dogfood（`IsSuccessful` 改 `err==nil` → 4xx-no-trip test 報「circuit open, want ErrForbidden」證明合約 load-bearing）+ GitLab `detailed_merge_status` 對照 + tracker conflict-count clear-to-zero。
- **threshold-exporter 補上 runtime per-tenant cardinality truncation 可觀測性(issue [#652](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/652)，#631 follow-up、評估 H3 時抓到的真正 silent-failure gap)**：#631 H3 提的「runtime per-tenant series cap」追到 `pkg/config/resolve.go:189` 後發現 v1.5.0 早就 ship（`DefaultMaxMetricsPerTenant = 500`，per-tenant tunable，slice-segment 隔離正確；da-guard 鏡像同 limit 做 admission gate）—— 但 `internal/guard/cardinality.go:105` 自己的 comment 寫著「runtime would **silently** truncate the excess」：截斷只記 `log.Printf("ERROR: ...")`，沒 Prometheus signal、沒 alert，一次 `_defaults.yaml` 加 600 個 metric key 會讓**每個 dependent tenant 都被截到 500**，operator 只能從下游 dashboard staleness 反推、跟 #631 H2 phantom-state / #643 silent parse failure 是同類別 silent failure。修法：新增 **Gauge** `da_tenant_metrics_over_limit{tenant} = max(0, count - effective_limit)`（state-coded magnitude；compliant tenant 顯示 0，dropped-below-cap 立即歸 0 —— `Resolve` 在 per-scrape `Collect()` path，用 Counter 會 scrape-frequency-couple、stuck 1h × 30s scrape → 12,000/h 假數據，state-coded gauge 完全免疫此噪音也免疫 `increase()` 浮點外插）；`ResolveAtWithStats(time.Time) ([]ResolvedThreshold, ResolveStats)` 新方法把 per-tenant overflow magnitude 從 `Resolve` 內部漏出（原 `ResolveAt` delegate 過去保 API 相容、所有 caller 不變），collector `Collect` 改呼叫 stats 變體 + `PublishTenantMetricsOverLimit` Reset+Set 一次 pass（vanished tenant 被 Reset 自動 evict、dropped-below-cap tenant Set(0) 清空舊值）。兩條 alert 在 `k8s/03-monitoring/configmap-rules-platform.yaml`(對稱 #647 ConfigParseFailure + ConfigDefaultsParseFailure 模式)：**`TenantMetricsOverLimit`**(warning, `for: 5m`, `> 0` 嚴格不含 0 才精準切 compliant/over-limit)、**`DefaultsTruncationStorm`**(critical fire-once sentinel, `count without (tenant) (... > 0) > 50`，threshold 嚴格 > 50 不含邊界，`without (tenant)` 保 K8s scrape-time topology label 讓 alert payload 帶 `instance/pod` —— **#651 學到的 PR 教訓重 apply**：bare `count()` 會把 topology 全拔光、on-call 不知哪顆 exporter pod 在 storm)。Gemini 對抗式 review 抓兩個 reframe：(1) `.Inc()`/`.Add()` Counter 在 per-scrape path 都會 scrape-frequency-couple → 改 Gauge state-coded、順便消滅 `increase()` 外插問題；(2) 補 `DefaultsTruncationStorm` 千租戶規模災難哨兵 + per-tenant alert annotation 提示「sentinel 同時觸發 → 先查 defaults」雙向 cross-ref。Go test 覆蓋 5 case `ResolveAtWithStats` magnitude + clear-to-zero transition + 2 case collector integration（adversarial self-review 抓到的：collector 原先呼叫 package-level `PublishTenantMetricsOverLimit` helper → 走 global `getConfigMetrics()`、繞過 `ConfigManager.SetMetrics` 注入的 fresh instance、破壞既有 `m.getMetrics()` 模式的 test-isolation 合約。修法改呼叫 `c.manager.getMetrics().PublishTenantMetricsOverLimit(...)` 跟同檔其他 `IncReloadTrigger`/`IncParseFailure` 一致；mutation 回滾改回 helper 後新 integration test 報「gauge = 0, want 100」明確證明合約 load-bearing。第二個 test 覆蓋 tenant 從 config 刪除時 Reset() 應 evict 的 invariant）；promtool dogfood 4 case + 3 mutation 全帶 K8s-realistic labels(`instance/job/namespace/pod`)：mutation A `> 0` → `>= 0` compliant case false-fire / mutation B `count without (tenant)` → 裸 `count()` storm case 丟 topology labels / mutation C `> 50` → `>= 50` 50-tenant 邊界 false-fire，restore 後 4 case 全 SUCCESS。`platform-data.json` 隨 `make platform-data` 重生(總 111 alert，badge 109→111)。延伸：long-horizon SLI counter（reload-time `.Inc()`、`> 0.5` extrapolation floor 沿用 #647）作為獨立 follow-up，與 paging 用的 gauge 分流不污染。
- **threshold-exporter 補上 `ConfigReloadStuck` phantom-state alert(issue [#631](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/631) H2 後半，補完 [#643](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/643)/[#647](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/647) 的 parse-failure 對偶)**：#643/#647 shipped `ConfigParseFailure` + `ConfigDefaultsParseFailure` 捕 **bad YAML**(parse 失敗 logged + counter+1)，但 orthogonal 的「phantom state」未蓋 —— fsnotify-driven trigger 開了(檔案變更被看到)、`diffAndReload` pipeline 卻 wedge → 後續每次 scrape 都吐**舊** config：Git 說 X、Prometheus 仍 emit Y。新 alert `ConfigReloadStuck`(critical, `for: 2m`)落在 `k8s/03-monitoring/configmap-rules-platform.yaml`，gate `sum without (reason) (increase(da_config_reload_trigger_total[10m])) > 0` 限制只在「該有 reload activity」時觸發(否則閒置 exporter 會 false-page，gauge 設計上會隨 idle 老化；`without (reason)` 兩層 dogfood 才正確：第一層 promtool 抓到 bare `increase()` 帶 `{reason}` label、與 RHS 不 match → empty vector，改 `sum()`；Gemini 對抗式 review 抓到 lab-clean test fixture 隱藏了真實 prod 場景—— `time() - gauge` 是 scalar-minus-vector → 繼承 gauge 的 K8s scrape-time topology label (`instance`/`job`/`namespace`/`pod`)、而純 `sum()` 把 LHS 全拔光成 `{}` → AND 仍是 empty、alert **在 K8s prod 仍永不觸發**。`sum without (reason)` 只拔 `reason`、保留 topology label，AND 才 match 且 alert payload 帶 `instance`/`pod` 讓 on-call 知道哪顆 pod 卡住；K8s-realistic 4-case re-dogfood 全綠)，主條件 `(time() - da_config_last_reload_complete_unixtime_seconds) > 600` —— 後者的 `Help` 文字明寫此 pattern「Production use: alert on time() - <gauge> > N for stuck-reloader detection」(`config_metrics.go`)。10m 門檻遠高於 worst-case reload p99(~30s histogram top bucket) + debounce-coalesced burst 緩衝；`for: 2m` 吸收單次 scrape miss，counter+gauge 已是慢動量、無需更長 hold-down。`platform-data.json` 隨 `make platform-data` 重生(總 109 alert，badge 同步 108→109)。**注**：#631 H3「runtime per-tenant series cap」原作者已標 defer，不在本 PR scope。
- **tenant-api PR-mode polling-staleness false 409(issue [#644](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/644)，#615/#616 韌性 backlog)**：`gh.Tracker` / `gitlab.Tracker`(都是 `*platform.PollingTracker` 的 type alias)每 ~30s 同步 forge 開啟 PR 狀態。merge 後 byTenant 快取會殘留「open」到下次同步 → 同租戶下一次寫入回**假 409**(`pending_pr_exists`)。修正:`platform.Tracker` 介面新增 `RefreshNow(ctx)`(`PollingTracker` 以 goroutine + `select` ctx-bounded 包現有 `Sync()`,ctx 過期不取消背景 Sync,只停止等待 → handler 短延遲 fall-through;**in-flight dedup**:N 個同時 410 觸發只會 collapse 成 1 個底層 Sync→ListOpenPRs,避 thundering-herd 撞 forge secondary rate limit — Gemini review 抓);`tenant_put.go` 的 409 路徑:若 `ClaimTenant` 失敗**且** `HasPendingPR=true`(快取陳舊訊號,**而非** in-flight claim 的 `HasPendingPR=false`),以 **2s `context.Background()`-derived ctx**(reviewer 抓的關鍵:用 `r.Context()` 派生,client 在進此分支前若已中斷則 ctx 已 Done → `RefreshNow` 立刻跳過 Sync → 仍回陳舊 409,fix 失效;detached background 確保 live-client 場景一定刷新)呼叫 `RefreshNow` + retry `ClaimTenant` **一次**;仍 false 才 409。2s 邊界是「degraded forge 不該延長 409 延遲」的關鍵(#615 backlog 條件;待 #645 circuit-breaker 落地後可再緊)。`tenant_batch.go` 不需動(consolidated batch flow 無 per-tenant ClaimTenant 路徑)。測試:`tracker_test.go` +2(happy-path 蓋快取 + blocking-lister 證明 ctx-bounded);`pr_test.go` +3(refresh 清陳舊 → 通過 / refresh 確認真實 pending → 409 + refreshCalls=1 / in-flight only → 409 + refreshCalls=0)。既有的 PendingPRConflict / PendingMRConflict 兩條測試 mock server 由 `[]` 改回傳該 PR(refresh 確認 still open → 409 維持原意)。
- **threshold-exporter `da_config_parse_failure_total` 補上 alert(issue [#643](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/643)，#615/#616 韌性 backlog)**：metric 早就存在(`config_metrics.go`,標籤 `file_basename`,Help 寫明「Alert: >5/h for any single basename = page ops」),但 platform rule pack 從未 ship 對應 alert → 壞 YAML 只在 exporter ERROR log,**該租戶靜默失去 rule 評估、operator 無從得知**。`k8s/03-monitoring/configmap-rules-platform.yaml` 新增兩條 alert,對應 exporter 自己的 Help + RCA 註解描述的兩種失敗模式:**`ConfigParseFailure`**(warning,`increase(...{file_basename!="_defaults.yaml"}[1h]) > 5`,`for: 5m`)按 metric Help 守一般 per-file 失敗;**`ConfigDefaultsParseFailure`**(critical,fire-once sentinel,`increase(...{file_basename="_defaults.yaml"}[5m]) > 0`)單一 `_defaults.yaml` 失敗即靜默 drop **整條 defaults chain** → 所有依賴 tenant 全失去 defaults 衍生 rule,blast radius 廣得多。`platform-data.json` 隨 `make platform-data` 重生(總 108 alert)。
- **gitops PR-mode 本地 feature branch ref 慢洩漏（issue [#641](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/641)，#638 review deferred 項）**：`WritePR`/`WritePRBatch` happy path 留下的 `tenant-api/<tenant>/<ts>` 本地 ref **從不刪**（只有 rollback 路徑會 `branch -D`）。tenant-api PR-mode 跑單一長存 replica，數週/數月後本地 clone 累積上千個 stale loose ref（非 brick,但 git GC / ref 列舉變慢、磁碟膨脹）。修正：push **成功**時,於 Step 7 切回 base 之後再 `branch -D <feature>` —— commit 已安全在 origin、PR 也從 `origin/<branch>` 開,本地 ref 不再需要。push **失敗**則保留（本地是 commit 唯一副本,行為同舊版）。回歸測試以本機 `git init --bare` 假 origin 驗 push-success-then-delete + 無 remote 驗 persist-on-failure。
- **gitops Writer 寫入路徑兩處硬化：SIGKILL 殘鎖自癒 + de-relativize checkout（issue [#638](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/638)，#630/PR #637 的 Gemini 對抗式 review follow-up，P1）**：(1) **跨租戶分支污染**——`WritePR`/`WritePRBatch` 原本從「當前 HEAD」`checkout -b` 並靠相對的 `checkout -` 切回；一旦工作區被前次寫入留在某 feature branch，下一個租戶就會**從別人的 feature branch 分叉**，PR diff 靜默夾帶他人未推送的設定（資料隔離破口）。修正：每次 PR 寫入**開頭以 ironclad `reset --hard HEAD` + `checkout -f <base>` 洗白**再 `-b`、所有切回改用同一個 clean checkout（不再用相對 `-`），讓污染**不可能發生**（任何 stuck 狀態下次自我矯正）。**ironclad 是關鍵**（兩位 reviewer 各自抓到）：plain `checkout <base>` 遇到 dirty tree（寫檔成功但 commit 未完成被 SIGKILL）會被擋 → wedge 住後續每個 PR 寫入,PVC-backed conf.d 連 pod 重啟都解不開（death-loop）；`reset --hard`+`-f` 才能真的自我矯正。base 由新 flag/env `TA_GIT_BASE_BRANCH`（預設 `main`、forge-neutral、`SetBaseBranch` 注入）決定，base 不可達即 abort。(2) **SIGKILL 殘鎖**——#630 的逾時 SIGKILL 會讓被殺的本地 `git add`/`commit` 留下 `.git/index.lock`（及 `HEAD.lock`、`refs/**/*.lock`、`packed-refs.lock`、`config.lock`）；因所有寫入共用 `sync.Mutex`，一個殘鎖會讓**後續每一個租戶寫入**都 `index.lock: File exists` 直到人工介入。修正：`gitErr` 的 deadline 分支 best-effort 清掉這些鎖（安全性僅來自 mutex 序列化 + conf.d 由單一 replica 獨占,當下無並發 git 持鎖）。回歸測試以本機真 git（`t.TempDir`）驗證 —— stuck-branch 不污染、**dirty-tree 仍能恢復**、abort-on-missing-base、殘鎖清除後可再 commit、逾時 self-heal。`gitDir` 為 operator config（非 request input）。
- **gitops Writer 的 git CLI 無逾時 → 卡住的 `git push` 凍結全租戶寫入（issue [#630](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/630)）**：`internal/gitops/writer.go` 的所有 git 子程序原用裸 `exec.Command`（零 `CommandContext`），而 `Write`/`WritePR`/`WritePRBatch` 全程持寫入端 `sync.Mutex`。在 degraded on-prem forge／網路瞬斷下，卡住的 `git push` 會無限期持鎖 → **每一個租戶（含 `_groups`／`_views`／federation）的寫入全部阻塞**直到 pod 重啟（既有 `http.Client{Timeout:30s}` 只蓋 REST forge client、不含 git CLI）。修正：所有 git 呼叫改走統一 `gitCmd` helper（`exec.CommandContext` + per-command deadline），逾時即 SIGKILL 子程序、回 loud `timed out — write lock released` 錯誤並釋放 mutex（把無聲的全域寫入凍結變成一次明確失敗）。預設 60s，可由 `TENANT_API_GIT_TIMEOUT`（Go duration，如 `90s`，給較慢的 on-prem GitLab）覆寫；非法／0／負值 clamp 回預設以免關掉這道安全網。pre-existing（非 #615 引入），由 [#625](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/625) Gemini 對抗式 review 抓出、列為 [#616](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/616) resilience backlog 最高 ROI 項。
- **da-portal image healthcheck 永遠 `unhealthy`（baked `localhost` 解析到 IPv6，由 try-local [#465](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/465) nightly 首驗抓到）**：image 內 baked 的 `HEALTHCHECK wget http://localhost/healthz`，在 dual-stack 容器裡 `localhost` 先解析到 `::1`（IPv6），但 nginx 只聽 IPv4 `:80` → `docker run` / docker-compose 下容器永遠停在 `unhealthy`（K8s 不受影響——它走自己的 readinessProbe、不看 docker-native HEALTHCHECK）。**root fix**：`components/da-portal/Dockerfile` 的 HEALTHCHECK 改用顯式 `127.0.0.1`，保持 nginx IPv4-only、**不**加 `listen [::]:80;`（後者在 IPv6 停用的 host 會讓 nginx 啟動失敗 `Address family not supported by protocol`）。try-local 因 pin published v2.8.0 image（仍含此 bug），`docker-compose.yaml` 端同步保留等效 healthcheck override，待含此修補的新 portal image 發版、`PORTAL_TAG` bump 後即可移除。
- **Platform 日誌彙整 runtime smoke-test 兩個 chart bug + 兩個預設值缺口（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 1 follow-up）**：把 `helm/vector` + `helm/victorialogs` 真的部到 kind cluster 跑 runbook §2 五條 LogsQL，抓到兩個 chart-only review 沒蓋到的 runtime bug 與兩個預設值不對齊問題。**Bug 1 (vector)**：configmap.yaml 的 VRL `. = merge(., object!(parsed), deep: true) ?? .` 在 Vector 0.55+ 噴 `unnecessary error coalescing operation` —— `merge(object, object)` 是 infallible，`??` 是多餘的（VRL 編譯器把它當錯誤而非 warning，pod crashloop）。修：拿掉 `?? .`。**Bug 2 (vector)**：daemonset.yaml 把 hostPath 掛在 `/vector-data-dir`，但 Vector 預設 `data_dir` 是 `/var/lib/vector`（落在容器 root），跟 `containerSecurityContext.readOnlyRootFilesystem: true` 衝突 → `kubernetes_logs` 起不來「Could not create subdirectory ... Read-only file system」。修：configmap.yaml 顯式設 `data_dir: /vector-data-dir` 對齊掛載點。chart version `0.1.1`→`0.1.2`。**缺口 1 (victorialogs)**：`persistence.size` 預設 10Gi 跟 runbook §5 capacity 公式（federation audit ~50 RPS × 1 KB × 30d × 1.3 / ~10× 壓縮 ≈ 13 GB on disk）對不上，給 ~2× headroom 改 30Gi（runbook 已寫了該數字）。chart version `0.1.0`→`0.1.1`。**缺口 2 (vector)**：`metrics.enabled=true` 時沒有對應 Service，operator 想開 Prometheus scrape 還得手寫；補 `templates/service.yaml`（headless 因為每顆 Vector 是獨立 pod-IP scrape target，非 routing 後端），含 `prometheus.io/scrape` annotation。chart version 同上一次 bump。**驗證紀錄**：兩個 chart 在 kind cluster `helm install` → `log-generator` test pod 同步噴 JSON 與 plain-text 兩種行 → Vector tail + VRL demux → VictoriaLogs `_stream` 出現 `log_type=federation_audit` 與 `log_type=gateway_operational` 兩條 stream（schema 完全對齊 #539 §3 表：`app`/`k8s_namespace`/`log_type`/`tenant_id`/`status` 進 stream；`pod_name`/`token_id`/`query`/`path`/`method`/`ts`/`duration_ms` 留 data field）→ Grafana datasource provisioning + plugin 自動裝（`victoriametrics-logs-datasource` v0.27.1 從 grafana.com 抓）→ 透過 Grafana proxy API 查 `log_type:federation_audit` 取回 row。AC4 + AC5 端到端證實。

- **Federation 標籤名地雷：proxy 注入 `tenant_id`、平台 data layer 用 `tenant`（ADR-020 IV-2.0，issue [#505](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/505)）**：IV-2.0 前置 audit 盤點 data-layer 租戶 label 現況時抓到一個會讓 federation 全盤靜默失效的 mismatch —— `helm/federation-proxy`（IV-2a #506）以 `prom-label-proxy -label=tenant_id` 啟動、對每個 PromQL selector 強制注入 `{tenant_id="<X>"}`，但平台 data layer 既有的租戶 label 名是 **`tenant`**（Prometheus relabel `target_label: tenant`、threshold-exporter、tenant-scoped rule pack 一律 `on(tenant)`；`tenant_id` 在 `k8s/` data-layer 設定裡一次都沒出現）。注入名不符 → 配不到任何 series → **每一個 federated 租戶查詢回 empty vector**，範圍 100%。修正：`helm/federation-proxy` 的 `tenant.label` 預設由 `tenant_id` 改 `tenant`（chart README 同步、chart version `0.1.0`→`0.1.1`）；`docs/adr/020-tenant-federation.md` 把 prose 中「proxy 注入到 metric 的 label」一律對齊 `tenant`（JWT claim 仍名 `tenant_id` —— claim 名與 metric label 名為獨立命名空間，互不要求一致）。新增前置 audit 文件 [`federation-label-enrichment-audit.md`](docs/internal/federation-label-enrichment-audit.md)：metric family 現況盤點表 + federation whitelist 的 eligible / ineligible 初始清單（IV-2e #510 的輸入）+ follow-up（cAdvisor `container_*` 缺 scrape-time `tenant`、admission validator）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §前提約束。
- **Federation token 每租戶 16-上限的 TOCTOU 競態（issue [#527](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/527)）**：`Manager.Issue` 原以「`store.list()` 數一次 → 比對 `>= maxTokensPerTenant` → `store.put()`」的 check-then-act 把關每租戶 token 上限,但 list 與 put 是兩次獨立 store 往返。多 replica 併發簽發同一租戶時,各 replica 都 `list()` 看到 < 16 → 都 `put()` append,16 上限被擊穿、Sybil 防線失效。修正把上限檢查**下推進 store 的寫入交易**:`configMapStore.put` 在 `RetryOnConflict` 閉包內、對當次載入的最新文件清點該租戶 live record,達上限即回 `ErrTokenLimitReached`(閉包每次 retry 都對新狀態重檢,故 check 與 append 是單一 atomic compare-and-swap);in-memory `store.put` 在同一把 mutex 下做等價檢查。`Manager.Issue` 移除前置 list 檢查,改由 `put` 單點權威把關。
- **Federation gateway 在 prom-label-proxy 模式拒絕 `remote_read`（issue [#529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/529)）**：`helm/federation-gateway` 原以單一 `prefix: "/"` route 轉發所有路徑。`prom-label-proxy` 模式下,upstream 的 prom-label-proxy 只對文字查詢 API（`/api/v1/query[_range]`、`/series`、`/labels`、`/federate` 等）強制注入 tenant label —— Prometheus `remote_read`（`/api/v1/read`,Snappy-framed protobuf body）不在其列、無法被 label-scope。新增條件式 Envoy route:`prom-label-proxy` 模式對 `/api/v1/read` 直接回 `direct_response` 403,不再把 Layer 3 無法做租戶隔離的請求轉下去。`vm-cluster` 模式不受影響 —— `revoked_check.lua` 會把路徑改寫進租戶的 `/select/<id>/` accountID 空間,`remote_read` 連同隔離一併成立。gateway chart README 新增「Supported read APIs」段說明各模式可用的讀取 API;chart version `0.1.0`→`0.1.1`。`envoy --mode validate` 通過。
- **Federation gateway `/api/v1/read` 403 guard 的 non-canonical path 繞過（ADR-020 IV-2b 加固,hardens issue [#529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/529)）**：#529 為 `prom-label-proxy` 模式加的 `/api/v1/read` 403 `direct_response` route 用 Envoy **exact `path:` match** —— `POST /api/v1/read/`（尾斜線）/ `POST /api/v1//read`（雙斜線）都不命中該 route,會 fall through 到 catch-all `prefix: "/"` 被轉給無法對 Snappy-framed remote_read body 做 tenant label-scope 的 Layer 3 prom-label-proxy,即一條跨租戶資料外洩路徑。修正兩處:(1) `HttpConnectionManager` 加 `merge_slashes` + `normalize_path`,在 routing 前把路徑正規化（`/api/v1//read` → `/api/v1/read`、RFC 3986 dot-segment);(2) 403 route 的 match 由 exact `path:` 改為 `path_separated_prefix: "/api/v1/read"` —— 涵蓋 `/api/v1/read` 與其所有 sub-path、且在 path-segment 邊界比對,故不會誤擋 `/api/v1/readiness` 類 sibling。此 bypass 由 #540（IV-2f audit-log）的 Gemini adversarial review 發現,刻意拆為獨立 security fix（base rebase 至 #540 merge 後的 main）。chart version `0.2.0`→`0.2.1`;`envoy --mode validate` 通過。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 3。
- **Federation gateway `/api/v1/read` 403 guard 的 escaped-slash（`%2F`）殘留繞過（ADR-020 IV-2b 加固,hardens [#529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/529) / [#542](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/542)）**：#542 為 `/api/v1/read` 403 route 補的 `merge_slashes` + `normalize_path` 收掉了雙斜線與 RFC 3986 dot-segment,且 `normalize_path` 依 RFC 3986 §6.2.2.2 會 percent-decode **unreserved** 八位元組（`%72`→`r`、`%2e`→`.`)—— envoy v1.38 實測 `%72ead` / `%2e` 類變體早已命中 403。但同條 RFC **刻意不解碼 reserved 字元**:百分號編碼的斜線 `%2F` 維持編碼,`/api/v1%2Fread` 因此不命中 `path_separated_prefix: /api/v1/read`、fall through 到 catch-all `prefix: "/"` 被轉給 upstream —— #542 宣稱「no non-canonical variant can slip past the guard」未收掉的最後一個變體。修正:`prom-label-proxy` 模式的 `HttpConnectionManager` 補 `path_with_escaped_slashes_action: UNESCAPE_AND_FORWARD`,在 routing 前把 `%2F`(與 `%5C`)解碼,路徑完全正規化後 403 guard 即無縫;`path_separated_prefix` 的 path-segment 邊界比對不受影響 —— `/api/v1%2Freadiness` 解碼為 `/api/v1/readiness` 仍是 sibling、不被誤擋(envoy v1.38 實測 7 變體確認)。**mode-scoped 到 `prom-label-proxy`**:該 403 guard 只存在於此模式,`vm-cluster` 由 `revoked_check.lua` 自行改寫路徑、不套此設定。chart version `0.2.1`→`0.2.2`;`helm template | envoy --mode validate` 通過。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 3。
- **Bench-gate（Tier 1 + Tier 2）regression detector 對非確定性 `MB-sys` / `MB-heap-after-gc` false-RED → 改 scope 到確定性 metric（issue [#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)）**：兩條 bench-gate workflow（`bench-gate-pr.yaml` Tier 1 α=0.01／5%、`bench-gate-release.yaml` Tier 2 α=0.05／10%）的 regression 偵測都是「grep 整份 benchstat 輸出抓任何 `+N% (p<α)` 列」，**未依 metric column 限定範圍**。bench 透過 `b.ReportMetric` 發兩個 process-level 非確定性指標 —— `MB-sys`（`runtime.MemStats.Sys`）與 `MB-heap-after-gc`（`config_hierarchy_bench_test.go`），是 GC/runtime high-water 讀值、非 per-op work：跨 process 隨機漂移（同一份 code 連跑兩次會 sign-flip）、且 `-count` run 內 pseudo-replication 使 within-run variance≈0 → 任何 between-process offset 都被讀成「顯著」（p=0.000）→ 真實 Go PR 被 false-RED（[#502](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/502) spurious RED 的根因；[#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459)／[#606](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/606) 獨立判定 `sys_bytes` 高水位爬升是 Go GC pacing 而非 code leak，互相佐證）。修正：兩條 workflow 的 benchstat 調用加 `-filter '.unit:(sec/op OR B/op OR allocs/op)'`，把偵測**與顯示**都限縮到確定性 per-op metric（benchstat 把 `ns/op` 正規化為 `sec/op`）；此 allowlist 自動排除 `MB-sys`／`MB-heap-after-gc`／`goroutines`／`affected-tenants` 及任何未來新增的 custom metric，grep/awk 偵測邏輯與 INCONCLUSIVE 防線**完全不動**。**MB-sys/heap 保留發出**（仍進 nightly `bench-record` artifact + release-attached baseline 供資源趨勢 informational 檢視），僅從兩條 gate 排除。控制實驗（Dev Container, real benchstat@latest, 真實 metric 欄位格式）：MB-sys-only +25% drift → regression=false（不再 RED）、注入 B/op +20% canary → regression=true（仍 RED）。**時效**：須在 v2.9.0 打 tag 前 merge（[#427](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/427)），否則 v2.9.0 首次 Tier 2 release 演練會被同一 MB-sys FP 污染。
- **Bench-gate（Tier 1 + Tier 2）對 benchstat 輸出格式漂移的 silent-pass 加固（PR [#611](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/611) review 期間發現，延伸 [#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)）**：#608 加的 `-filter` 讓 regression 偵測**雙重依賴** benchstat 的輸出格式 —— grep `+N% (p=...)` 列的 row 格式，加上 `-filter` 的 unit 名（`sec/op`／`B/op`／`allocs/op`）。benchstat 以 `@latest` 每次 CI 重裝，未來某版若改 row 格式或 rename 某個 unit，兩條 grep 會**全部 miss → regression=false → GREEN gate 靜默放行所有真實 regression**，且**無需任何 repo 變更**即可觸發（單純某次 benchstat 發版）；既有 `^cpu:` INCONCLUSIVE 防線抓不到（cpu header 不隨輸出格式改變消失）。修正：兩條 workflow 的 Compare step 在 `tee benchstat.txt` 後加 **fail-loud shape assertion** —— 要求 filtered 比較輸出至少含 1 個 metric-section header（`sec/op`／`B/op`／`allocs/op`）**且**至少 1 條帶 `± N%` 的 per-bench result row，否則 `::error::`（明示「pin benchstat 版本」）+ `exit 1`；偵測邏輯與 INCONCLUSIVE 防線不動，兩 tier 斷言完全相同。控制實驗（Dev Container, real benchstat@latest）：真實輸出 pass、empty／garbled／全-unit-rename／`± N%`-移除 四種 drift 全數 exit 1。
- **Federation 稽核 metric `tenant_federation_requests_total` 在生產環境從未被產出（ADR-020 IV-2f 修正,由 IV-2j E2E [#516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/516) 首跑抓到）**：`helm/federation-gateway` 的 mtail 程式 `files/federation-audit.mtail` 以單一 regex `"tenant_id":"…".*"status":…` 從 Envoy JSON access log 抽 `tenant_id` 與 `status`,但該 regex 假設 `tenant_id` 在 `status` **之前**。Envoy 的 `json_format` access log 不論 config 內欄位順序,輸出一律把 key **依字母排序**(`status` < `tenant_id`),故 `status` 永遠在前、該 regex 永不命中。後果:mtail 雖逐行讀入 access log,`tenant_federation_requests_total{tenant,status}` 一個 sample 都不產出 —— IV-2f 的 federation 稽核 metric、`FederationRejectionRateAnomaly` / `FederationGatewayBackendErrors` alert、`federation-audit` 儀表板自 #540 merge 起即靜默全失效(audit log 本身的 JSON 欄位完整、不受影響)。修正:改用 mtail nested pattern 分別抓兩個 key —— `/[{,]"tenant_id":…/ { /[{,]"status":…/ { … } }`,與 JSON key 順序完全解耦;`[{,]` anchor 確保只命中真正的 top-level JSON key,不會誤中 `query` 值內的 `status` 子字串。本 bug 即由 IV-2j E2E 的 S5(Sybil 限流 → `rate_limited`)/ S7(storage cap → `bad_request`)情境首跑抓到 —— E2E 直接斷言這條 metric。chart version `0.2.2`→`0.2.3`。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Audit log + anomaly metric。

### DX

- **退役 `generate_cheat_sheet.py` — cheat-sheet 正名為手工維護 SoT（PR [#780](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/780) 衍生）**：`docs/cheat-sheet.md`/`.en.md` 早已 repo-wide 偏離 generator 輸出（描述 / 排序 / 段落皆異），且在 dev-container 內 regen 驗出 generator 有三個結構性 bug：(1) 描述抽取 regex 斷在第一個 `.` → `v2.8.0` 等含句點字串被截成「…（v2」、甚至斷在反引號；(2) **只讀 `cli-reference.md`（ZH）、從不讀 `.en` 源** → regen `cheat-sheet.en.md` 整表變中文，連帶炸 bilingual CJK-ratio 檢查；(3) 刪掉手加的「快速提示」子段與 ZH `## 相關資源`（後者正是用來修 ZH/EN h2 count parity）—— 即「re-sync 到 generator」是嚴格負向操作。**無任何 active CI gate 對 cheat-sheet 做 regen+compare**：`validate_all.py` 的 `cheatsheet` drift entry 從未被任何 `--only`（`docs-ci.yaml` drift job / `make lint-docs`）觸發，但 `FIX_COMMANDS["cheatsheet"]` 仍掛著 → `validate_all --fix` 會用截斷垃圾**靜默覆蓋** curated 雙語檔（地雷）。**修法（full delete）**：刪 `generate_cheat_sheet.py` + 清 `validate_all.py`（`TOOLS` / `FIX_COMMANDS` / `WATCH_TRIGGERS` 三處 `cheatsheet`）、`Makefile` `generate-cheat-sheet` target、`bump_docs.py` 已失效的 cheat-sheet version-fallback rule（現行檔早改讀 `_lib_versions`、該 regex 永遠 0-match）、`_lib_versions.py` docstring；tool-map 雙語重生（178 tools，−1 列）。cheat-sheet 唯一閘門維持 `check_cli_coverage.py`（命令 presence + 雙語命令集 parity，pre-commit hook）。
- **threshold rule-pack contract test 補 orphan-record gate（[#734](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/734)，#731 M3 殘留）**：#731 的 `rulepack_contract_test.go` 只驗 threshold record 的 selector 與**自身名稱**自洽——但 typo'd record 名（如 `tenant:alert_threshold:rediss_memory_used_bytes`，`rediss`）也自洽通過，而無租戶會寫 `rediss_*` → recording rule 永遠空集合（#731 症狀、不同成因）。新增 `TestThresholdRecordsAreConsumed`：斷言每個 `tenant[_version]:alert_threshold:<K>` record **必被同 pack 至少一個 expr 消費**（alert 比對它，或 ADR-024 version-aware 的 `:core` recording rule 引用它）。typo record 是孤兒（手寫 alert/core 只引用真實 key、不會引用 typo）→ 直接 fail。**比票上原議的兩方案都優**：方案 a（metric-dictionary 交叉驗證）的 dictionary 不完整、是 migrate 啟發式非 authoritative；方案 b（每 record 須有 promtool fixture 行）會誤殺——實測 63 records 中 fixture 只直接 named 17 個（其餘經 alert-level test 間接覆蓋），coverage gate 會 false-fail ~46 個合法 record。orphan gate 零誤殺（version-aware records 被 `:core` 消費仍判 CONSUMED）、含反 undershoot floor（< 50 records 即 fail，防 green-over-empty）。雙控制組驗證：clean main PASS（63 records 全 consumed）/ 注入 typo record FAIL（精確診斷）。
- **SSOT 語言評估報告移出 repo → closed issue #145（`feedback_decision_docs_belong_in_issues_not_repo` 原則落地）**：依「決策/評估報告住 GitHub issue 結案紀錄、repo 只留 operative 殘渣」原則，把 `docs/internal/ssot-language-evaluation.md`（status: superseded）+ `ssot-migration-pilot-report.md`（execution cancelled）全文歸檔到 closed issue [#145](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/145#issuecomment-4587136920) 並自 repo 移除。operative 殘渣（ZH-primary policy lock + 3 條 re-eval trigger）原已完整 inline 於 CLAUDE.md「語言策略」段 + `dev-rules.md` §9b，無 load-bearing 資訊流失；引用點改指 #145（CLAUDE.md / dev-rules / `migrate_ssot_language.py` docstring / testing-playbook §LL §12a / 移除 `.docorphan-ignore` 白名單）；`dx-tooling-backlog.md`「雙語 SSOT 切換（EN-first）」整段移除（不會再做的項目不留 superseded banner，僅保留待辦項）。**保留**季度稽核 trail `audit-reports/rules-drift-2026-05.md`（其 SOP 明定為 in-repo 趨勢留存、由 `audit_rules_drift.py` 重生）。
- **Codename gate Layer 2 — Phase B：升 `--ci` blocking + 進 CI（[#710](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/710) Phase B，#469 epic）**：把 Layer 2 gate 從 warn-mode manual hook 升為**阻擋式**。`codename-gate-check` 改 auto-stage + entry 帶 `--ci`，並在 `ci.yml` 加 `pre-commit run codename-gate-check --all-files`（與 Layer 1 `codename-leak-check` 同步、共用 step 的 `PR_BODY` bypass env）。`--ci`（非 `--strict`）只硬擋**確定性 internal codename leak**（從 `docs/glossary.md`「內部代號」表編譯的模板）——Layer 1 硬編 PATTERNS 的可維護後繼，現有 tree 為 0、零 FP。**刻意不升 `--strict`**：未登錄 shape token 維持 discovery-only（其 backlog 是 #710 持續 soak，跑無旗標的 `check_codename_gate.py` 取完整報告）；`--strict`（未登錄也擋）依 #710 Phase C 延後。doc-as-code：CLAUDE.md hook 計數 SOT 同步、`lint-policy.md`（(b)-class 表新增 `check_codename_gate.py` + §4 註冊 bypass lint-name `codename-gate`）、`hook-vs-skill-coverage.md`（安全 row 補 `codename-gate-check`）。經 vibe-subagent-review 三方對抗式 review 補強（#712 加 `main()` 出口碼 negative-control 證明 blocking 真的擋；本 PR 補 doc-sync）。DX fast-path（外審 perf 建議，本 PR 一併落地）：`--ci` 非 `--strict` 時硬擋只看 internal scan、不需 shape discovery，故 `scan_line(discover=False)` 跳過廣泛 shape regex（internal 偵測照跑）；commit 路徑 ~1.35s→~1.0s，避免開發者養成 `--no-verify` 習慣。剩餘 ~1s 為 interpreter 啟動 + 157 檔 I/O 地板。warn/manual 與 `--strict` 仍跑完整 discovery。
- **Codename gate Layer 2 — soak 首輪 triage：noise 過濾 + glossary seed（[#710](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/710)，#469 follow-up）**：Layer 2 gate warn-mode soak 的第一輪 triage。對 shape 發現的未登錄 token backlog 做分類，落實兩類**確定性**降噪（守 #469 反統計-NLP 原則）+ seed 真實對外術語。**(1) 列舉標籤過濾**：two-word-cap 第二字為單一字母（`Tier A` / `Option B` / `Path A`）視為文件結構列舉、非代號 → 跳過；`Track A` / `Wave N` 等真代號不受影響（由 internal-pattern 掃描先行捕捉）。**(2) HTTP header 片段**：`X-Forwarded` / `X-Request` 等加入 safe allowlist。**(3) glossary seed**：把 12 個高頻真術語登錄進 A–Z 字典雙語對偶（Prometheus Operator / Operator CRD / Custom Rule / Multi-Tenant / Domain Expert / Platform Engineer / Domain Policies / Routing Profile / Profile Builder / Blast Radius / Grafana Dashboard / Staged Adoption）。效果：未登錄 token 自 #707 merge 基線 6712 hits / 2235 distinct → **5664 hits / 2139 distinct**（−15.6% hits）；approved 詞 61→87；internal leaks 維持 0。filter regression test（列舉跳過、列舉不吞 `Track A` 真代號、X-header safe 含數字片段 `X-B3`、列舉對標點黏附 robust），加 6 個 `main()` 出口碼 negative-control（`--ci` 命中 internal leak → exit 1 / clean → 0 / bypass tag 翻 0 / `--ci --strict` 擋未登錄 / 空 internal → exit 2），共 49 個 self-test。X-header safe pattern 經外審放寬為 `^X-[A-Za-z0-9-]+$`（涵蓋 `X-B3`/`X-Amzn` 等含數字/縮寫片段）。soak 為**漸進迭代**，剩餘長尾續 soak；促進 `--ci`/`--strict` 仍依 #710 規劃。
- **Glossary-driven codename gate — Layer 2 self-healing 機制（[#469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/469)，接續 Layer 1 #462/#468）**：Layer 1 `check_codename_leak.py` 把已知代號 regex 硬編在程式碼裡，每個 v2.9.0+ 新代號家族都要等人發現才補 regex（whack-a-mole）。Layer 2 把 SSOT 搬進 `docs/glossary.md` 新增的「## 內部代號 — 禁止用於對外文件」段（雙語對偶 `glossary.en.md`「Explicitly Internal — Do Not Use in Customer Docs」）——以**模板語法**（`{N}`/`{X}`/`{x}`/`{AE}`）一列登錄一個代號家族，毋須改 lint 程式碼；glossary-PR diff 即人工守門點。新 `scripts/tools/lint/check_codename_gate.py` 跑兩個互補偵測器、複用 Layer 1 的對外文件 scope/allowlist/code-comment skip：**(1) internal-pattern 掃描**（deterministic、零 FP，從 glossary 模板編譯 regex 直接比對 → 確認外洩；Layer 1 硬編 PATTERNS 的可維護後繼）；**(2) shape 掃描**（broad regex 抓「長得像代號」的 token，分類為 internal/已核可/內建 safe/未登錄）。Approved 詞自字母表 `**Term**` 條目解析（含全形 `（）` 括號處理）。**TRK 政策**：跟齊 Layer 1——`TRK-NNN` 是 ADR-019 公開 tracking namespace（ADR 中公開引用）、列為 safe 不 fail。**Rollout**（#469 step 3）：先以 **warn-mode manual hook**（`codename-gate-check`，`stages:[manual]`，不帶 `--ci`/`--strict`）出貨，soak 1–2 週把高頻未登錄 token seed 進 glossary、量 FP 率，再升 `--ci`（internal leak 硬擋）與 `--strict`。現有 tree internal-leak = 0。35 個 self-test（`tests/lint/test_check_codename_gate.py`，含 ZH↔EN internal 表 parity guard、family-extension 發現、re.escape 注入防護、所有格邊界、determiner-skip regression）。doc-as-code：CLAUDE.md hook 計數同步、tool-map 雙語、PR template 改泛用 glossary 登錄提示。
- **rule-pack configmap generator + 三副本語意 drift guard（ADR-024 [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) PR3-pre-2 工具層）**：rule pack 存在 3 個手動同步副本（`rule-packs/` source / `k8s/03-monitoring/configmap-rules-*.yaml` live 部署 / `operator-manifests/` 樣板），PR3-pre 實證手動同步致 ~2 個月雙向 drift。新增 `generate_rulepack_configmaps.py`——從 canonical `rule-packs/` 生成 configmap（按 record/alert 拆 `<pack>-recording.yml` + `<pack>-alert.yml` data key，metadata 對齊 projected-volume 掛載），使 configmap 降為 build artifact（`make rulepack-configmaps`）。`--check` 以**語意比對**（非 byte-diff，免疫序列化差異）驗副本與源一致。另 `check_rulepack_sync.py` 三副本語意 drift guard，`_norm_expr` 含 PromQL token-minify（`by(t)`==`by (t)`）+ `#` 行註解濾除（Gemini 外審：避免註解壓平後吞 token 的 false drift）。10 self-test。本層**零行為變更**（configmap 實際重生為後續 owner-review-gated 步驟，因 enrichment inner-join 會把 11 pack 告警 gate 在 `tenant_metadata_info` 存在；routing 影響經 sub-agent 分析為 LOW）。
- **`pr-preflight` 新增 commit-scope 檢查，從源頭根除 first-CI-red deadlock（ADR-024 epic #423 開發中抓到、`feedback_first_ci_red_push_deadlock` codify）**：`pr_preflight.py` 主流程加 `check_commit_scope_range`——驗 `origin/main..HEAD` 每個 commit header 是否通過 `.commitlintrc.yaml` 的 type/scope enum。**根因**：commitlint 在 CI 驗的是 **PR 標題**（`gh pr create --fill` 下標題 == commit subject），而本地 commit-msg hook 只在 host `git commit` 觸發——**在 dev container 內 commit 會整個跳過 pre-commit**，錯 scope（如 `fix(threshold-exporter)`，enum 只允許 `exporter`）一路滑到 PR 首次 CI 才爆；PR 一旦帶紅的 hard-required check，preflight-marker pre-push gate 就無法滿足、只能靠 owner bypass-push（#689 親身踩到）。**修法**：bad scope → preflight FAIL → 不寫 marker → `require_preflight_pass.sh` 擋 push → bad commit 在建 PR 前就被攔，amend 修好再 push 即可，零新 hook（複用既有 marker gate）。搭配紀律：PR 一律 `gh pr create --fill` 讓標題繼承已驗證的 commit subject。5 個 self-test（valid pass / `threshold-exporter` fail / 多 commit 一壞即 fail / 無 commit skip / git 失敗 warn）。
- **component 文件圖連結性 — QUICKSTART↔README 雙向 + 角色指南受眾主幹（[#633](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/633)，follow-up #449/#466）**：把 #466 的 4 份 QUICKSTART「織進」既有文件圖（原本各自孤島、雙向斷）。每元件 **QUICKSTART ↔ README 雙向互指** + README 頂部「接待員」blockquote 導流（GitHub landing-page 雙重身分）；3 個角色指南（`for-*` + `.en`）加「你的上手路徑」小段（Diátaxis 正序：**try-local（大圖像）→ component QUICKSTART（單元件任務）→ helm（部署）**）成受眾主幹；QUICKSTART 加**包容式**「主要服務對象」上指角色指南（不排他，角色會流動）；try-local 產品表每列 → 對應 QUICKSTART。**去脆化漂移數字**（da-portal QUICKSTART 不再寫死「38」與 README「43」打架、da-tools「70+」軟化指向 README，counts 統一由 README 權威清單承載）。雙語對偶同步。設計：連結性審計（全 8 檔 inventory）+ Diátaxis 文件分類 + vibe-brainstorm 三案 A/B/C + Gemini 外審三點（Diátaxis 正序 / 包容措辭 / README 接待員）。
- **try-local onboarding UX polish — persona 內聯標記 + 視覺心智模型 + 入口閉環（[#626](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/626)，follow-up #449）**：採用漏斗評估（vibe-brainstorm + 兩輪 Gemini 外審）後的 doc-only polish。`docs/index.md`（+ `.en`）加「一鍵試用」入口（補發佈站首頁缺口）+ persona→產品→Git/CI 的 Mermaid 心智模型 + role-handoff 敘事；root README 4-產品表加內聯 `[給 <persona>]` 標籤（`<br>` 避 banner blindness、保 mobile）；`hands-on-lab` ↔ try-local 雙向 cross-ref + 誠實 TTV 標籤（try-local `<1 min · Docker only` / lab `30–45 min · CLI`），**明確不淘汰** hands-on-lab；try-local Next Step 補齊 Tenant / Domain Expert / Platform Engineer 三 persona 回程。雙語對偶同步。
- **兩條開發紀律 codify（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) retrospective）**：epic 評估抓到兩個跨任務通用的改善點，用最低成本釘進 SSOT（**不開新 epic、不 re-bloat tier-1 CLAUDE.md**）。**(1) dev-rules §P4「實證驅動」**：PR 宣稱的數字（token/行數/coverage/節省）須附可重現量測指令、新機制 PR body 須含「怎麼證明有效」——無量測 = 杜撰（#570 燒過 110 行/~1000 token 全估值被打臉）。為守 dev-rules 520 行 cap，順手 condense 既有 §P3 的 SOP 敘述、淨維持 518 行（**自身就示範了「不盲加」**）。**(2) `quarterly-audit-sop.md` skill 汰除鐵律**：本地 `vibe-*` skill 連續 2 季在其領域 0 觸發 = dead weight，季度 audit 強制刪除（治 epic 交付但 0-1 觸發的 Gap A，對齊 `feedback_speculative_drift_prefer_remove`）。

- **CLAUDE.md 瘦身 + epic #570 收尾核算（TRK-310）**：epic 因加 always-on 高頻地雷 + 3 skill + pointer 增長：起點 **110 行 / 7,706 字** → peak **133 行 / 9,804 字**。本項把既有 verbose 段下放/收 pointer（測試 Seam table → test-map.md、優先級宣告 + 環境層 bullets → inline、起手式 blockquote 合併），收回 **109 行 / 9,169 字**，5 條高頻地雷 + dev-rules Top 4 + 計數短語零損。**誠實核算（修正初版基於行數的「net-negative」誤述）**：行數 109 < 起點 110，但**字元/token 仍 +19%（7,706→9,169）—— line count 是誤導 proxy，token 才準**；slim 實際效果 = peak −6.5% 字元。epic 整體 tier-1 token net **取決於 plugin prune**（~900-1000 tok/turn，**估值、未在本 repo 量測**）是否 > CLAUDE.md 的 +~430 token，**非來自 CLAUDE.md 變小**。瘦身**安全性經 recall test 實證**：乾淨 subagent 冷讀，5 條 ⛔ 高頻地雷 + dev-rules Top 4 **100% 可抽出 → 壓縮沒埋死線**（方法 codified 進 [`quarterly-audit-sop.md`](docs/internal/quarterly-audit-sop.md)）。epic #570 至此全數收尾。

- **Upstream skill-system FR tracker（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-309）**：新增 `docs/internal/skill-system-feature-requests.md`，收集 6 個**需上游（Anthropic / Cowork）做、Vibe 無法單方解決**的 skill-system 改善（project-scoped allowlist / keyword-gated lazy router / description style-guide+lint / anti-trigger metadata 標準化 / SKILL.md section anchor / usage telemetry CLI）+ 量化背景（~80 skill 描述 ≈ 4000 token/turn 全載）+ 糾錯 / 中長期發想附錄。來源：2026-05-21 superpowers / skill-system 評估；Vibe 內部能做的已落地（epic #570），本表是剩下的 upstream gap。不入 CLAUDE.md / doc-map catalog（internal SSOT，per CLAUDE.md internal-exempt 政策）；由 `hook-vs-skill-coverage.md` §關聯 cross-ref 避免 orphan。

- **兩個本地 skill：`vibe-release` + `vibe-brainstorm`（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-306 + TRK-308）**：本地 skill 四 → 六。**`vibe-release`（TRK-306）**：五線版號 release 收尾 SOP，consume `feedback_release_wrapup_discipline` 的三條紀律（pre-tag audit / CHANGELOG distill + project-face refresh / roadmap milestone-link）+ release-type 分流（GA / interim DX / hotfix）；延伸 [#474](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/474) Layer 3 的 inline checklist 為系統化流程（Layer 1/2 的 docker build + Trivy 已被 #474 機械化進 `make pre-tag`）。**`vibe-brainstorm`（TRK-308）**：設計階段 Socratic ideation，借 superpowers `brainstorming` pattern + 從 ADR-020 federation epic 實際流程（四輪 strategic discussion + 兩輪外審）萃取的五個 Vibe 設計提問（reuse-over-build / MVP-vs-Future-Work / explicit trade-off / defer-with-trigger / blast-radius）；anti-trigger SKIP code-level（→ `engineering:debug` / `vibe-subagent-review`）。TRK-308 原 deferred-to-post-ADR-020，#380 closed 後解鎖。doc-as-code：CLAUDE.md「Skill 體系」四→六 + `hook-vs-skill-coverage.md` §5 同步。

- **Trigger-asymmetry safety net — PR-time component Docker build + Trivy gate（[#474](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/474)）**：堵「`release.yaml` 在 tag push 才 build image，build break 在最糟時機才爆」的 dormant-bug pattern（v2.8.0 release 30 分內中 2 次：[#472](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/472) 移檔 COPY、[#473](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/473) 缺 pkg COPY + CVE）。三層：**(L1)** 新 `.github/workflows/component-docker-build.yaml` — PR 觸碰 component build 輸入路徑時，matrix `docker build --load`（**hard gate**）+ Trivy（**informational**，exit-code 0）；forked PR 可跑（local load 不需 secret）。涵蓋 **全 4 個 production Dockerfile**：threshold-exporter / da-portal / tenant-api 自包含；**da-tools 以 stub 納入**（空 `tools/` + `touch` 預編 Go binary —— 因 Dockerfile 只 COPY+chmod 不執行 binary，stub 即可驗 COPY 路徑 / 語法的 #472/#473 類，不需搬 Go cross-compile 進 PR）。da-portal build 前自動 `mkdir -p docs/assets/vendor`（offline-mode COPY 來源，runtime CDN fallback）。self-review 時 4 個本地逐一 build 驗過。**(L2)** `make pre-tag` 新增 `docker-build-all`（hard gate）+ `trivy-scan-all`（informational）—— 把 release-time 才做的 image build 提前到 pre-tag。**(L3)** `github-release-playbook.md` Step 2.5 加 release wrap-up agent discipline + trigger-asymmetry workflow audit 表。**設計取捨**：Trivy 在 L1/L2 皆 informational（非 #474 原文的 exit-code 1）—— 採 [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) 論點（PR-time 阻擋 CVE 會讓新 upstream CVE 無預警卡不相關 PR）；build 仍 hard gate。解鎖 TRK-306 `vibe-release` skill。

- **季度 rule-corpus drift 稽核 — `audit_rules_drift.py`（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-307）**：新增 `scripts/ops/audit_rules_drift.py` + `make audit-rules` + [`quarterly-audit-sop.md`](docs/internal/quarterly-audit-sop.md)。掃 dev-rules / pre-commit hooks / vibe skills / memory feedback 卡，產 drift report（`docs/internal/audit-reports/rules-drift-YYYY-MM.md`）：count reconciliation（YAML-parse hook 切分 vs CLAUDE.md 宣告）、hook↔dev-rule 覆蓋缺口、重複候選（difflib ≥0.60）、feedback orphan / broken-ref、stale 卡。MANUAL 季度工具（不入 CI；只產 report 不自動修改）。與 `consolidate-memory` 互補（後者只掃 `~/.claude` memory）。**首跑即抓出自埋誤差**：更正 [#582](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/582) `hook-vs-skill-coverage.md` 把 hook 切分誤記為「50 auto + 14 manual」並反指 CLAUDE.md drift——那是 grep `stages:\s*\[manual\]` 配到 `jsx-babel-check-strict-linecount` 註解行（該 hook 是 auto，註解明寫 NOT manual）；YAML parse 確認真值 **51 auto + 13 manual + 3 pre-push**，CLAUDE.md 一直正確。

- **`vibe-subagent-review` skill — IaC-aware blast-radius review（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-305）**：新增第 4 個本地 skill（`.claude/skills/vibe-subagent-review/`）。借 superpowers `subagent-driven-development` 的兩階段 review pattern，但對 Vibe 過半的 IaC 工作（Helm values / `.gotmpl` / Prometheus rules / VRL transforms）改採**副檔名路由**：`.go`/`.py` 走 spec→quality；`values.yaml`/template 走 **blast-radius**（selector/RBAC/NetworkPolicy/ConfigMap 連動）；`.vrl` 走 **schema cascade**（下游 SIEM payload）；Prometheus rules 走 **cardinality+severity**（dedup/Sentinel/四層路由）。定位為 [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) 機械 SAST 的**互補**——顧機械 lint 抓不到的跨檔語義 cascade。CLAUDE.md「Skill 體系」三→四 + `hook-vs-skill-coverage.md` §5/§7 同步（IaC cross-file cascade 漏接由本 skill 補語義層）。

- **Hook / Skill 邊界稽核矩陣（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-304）**：新增 `docs/internal/hook-vs-skill-coverage.md` 盤點全部 67 個 pre-commit/push 品質閘門（實測 50 auto + 14 manual + 3 pre-push）+ 2 PreToolUse session-guard + 3 本地 skill + engineering:* 重疊的 **owner 分類**：🔧 hook-enforced（機械自動擋，AI 不必重做）/ 🧠 skill-advised（AI 須自覺）/ 👁️ reviewer-only（純人工，最易漏）。標出 overlap（trailer 規則 4 層 / sed -i 5 層）、conflict（由 TRK-301 優先級仲裁）、🕳️ 漏接（推銷語言 / 架構圖 drift / IaC cross-file cascade / SAST 1·3·7 — 無機械防線）。`CLAUDE.md` §Pre-commit 品質閘門 加一行 pointer。動機：[#515](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/515) / [#522](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/522) / [#543](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/543) trailer 連燒 3 次暴露 AI 不清楚 hook↔skill 職責邊界。

- **CLAUDE.md AI-context 強化（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-301~303）**：三項 AI agent context 收斂 —（1）**TRK-301** 新增「Skill 優先級宣告」段，明訂 `vibe-*` 本地 skill 在衝突時 supersede 環境層 `engineering:*` / session-bootstrap generic skill；（2）**TRK-302** 新增「⛔ 高頻地雷（always-on）」段，把被燒過 ≥2 次的 5 條 feedback（繁中回應 / commit trailer block 格式 / worktree edit path / `git add` 括號 glob / commit 前觸發 `vibe-dev-rules`）從 lazy-load 升為 always-on；（3）**TRK-303** `dev-rules.md` §4 doc-as-code 補架構圖（`architecture-and-design.md` Mermaid / C4）為 schema 等級需同步項，並對應 adversarial self-review 第 6 lens。純 AI-context 文件，無 code / 行為變更。

- **CI workflow 升級 `actions/checkout@v4`→`@v6`（Node 20 deprecation 清理）**：GitHub 將於 2026-09 自 runner 移除 Node 20，`actions/checkout@v4` 跑在 Node 20、每次 job 噴 deprecation warning。把僅存的 4 個仍用 `@v4` 的 job（`docs-ci.yaml` I-4 Runbook Smoke Test、`planning-status-sync.yaml`、`secret-scan.yml`、`self-review-pass2.yaml`）對齊 repo 其餘 43 處早已採用的 `@v6`；兩處引用 `@v4` 版號的 `CRITICAL: ... fetch-depth: 0` 註解一併更新。純 CI 基礎設施版本對齊，無行為變更。`azure/setup-helm@v4` 維持不動（尚無更新的 major 版本）。

- **`pr_preflight.py`：軟性 CI check 不再卡死 push + 本地擋下壞掉的 Self-Review-Pass-2 trailer（hardens [#543](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/543)）**：兩個機制性修補,堵 #543 暴露的死結。**(1) `check_ci_status` 區分硬／軟失敗**：`continue-on-error: true` 的 workflow job(如 `Validate Self-Review-Pass-2 trailer`)失敗時 `gh pr checks` 仍回 `fail`,但它**不擋 merge**。原本 `check_ci_status` 把任何 `fail` 一律判 FAIL → preflight BLOCKED → 不寫 `.preflight-ok` marker → pre-push gate 擋住「修那個軟性檢查」的 push,而唯一逃生門 `GIT_PREFLIGHT_BYPASS` 又被 agent 安全層 hard-block —— 純化妝品的軟紅燈升級成死結。新增 `_soft_fail_check_names()`:掃 `.github/workflows/*.{yml,yaml}`、收集 `continue-on-error: true` 的 job 名(資料驅動,未來的軟性 workflow 自動納入、不靠 allowlist;workflow 解析失敗則該 check 保守歸 HARD)。CI 全綠或只剩軟性紅燈 → **WARN**;有硬性失敗才 FAIL,且 headline 計數只算硬性失敗。與既有 `check_pr_mergeable` FAIL→WARN 同一精神。**(2) commit-msg hook 擋下壞 trailer**：`check_commit_msg_file` 新增 `validate_pass2_trailer_placement()` —— 訊息若帶 `Self-Review-Pass-2:` 行,用 git 原生 `git interpret-trailers --parse` 驗它真的被認成 trailer。git 只把**連續的底部段落**當 trailer block:中間夾一個空行(#543)或一行非 `Key: value`(#515／#522)就會把上方的行整段甩出、`Self-Review-Pass-2` 不再是 trailer。CI gate `Validate Self-Review-Pass-2 trailer` 是 soft-fail、會靜默放行 —— 改在本地 commit-msg 階段 ERROR 擋下,壞 trailer 根本 push 不出去。Regression tests：`tests/dx/test_pr_preflight_checks.py::TestSoftFailCheckNames` + `TestCheckCIStatus`(soft／hard 分流)+ `tests/dx/test_preflight_msg_validator.py`(trailer placement)。

- **`pr_preflight.py` 的 PR-mergeable 衝突檢查 FAIL → WARN**：`check_pr_mergeable` 原本在 GitHub 回報 `mergeable=CONFLICTING` 時判 **FAIL**,連帶不寫 `.preflight-ok` marker、pre-push gate 擋下 push。但 GitHub 的 mergeable 是**已 push 的 PR head** 視角:衝突在本地已用 rebase / merge 解掉、但還沒 push 時,GitHub 仍回 CONFLICTING —— 形成「修衝突的 push 被『有衝突』擋住」的雞生蛋死結。改判 **WARN**:與同樣 pre-push-unresolvable 的 `BLOCKED`(待 review approval)一致;真正的本地衝突仍由 `check_conflict()` 的 merge dry-run 權威把關,`check_pr_mergeable` 純資訊性。Regression test：`tests/dx/test_pr_preflight_checks.py::TestCheckPRMergeable::test_conflicting_warns`。

- **`bump_docs.py` inline-version 規則跳過 CHANGELOG 已發佈段落**（PR [#503](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/503) 根因修正）：inline-version-text 規則（掃 doc 內文 `於 vX.Y.Z` 形式的行內版號）原會掃進 CHANGELOG.md 已發佈的 `## [vX.Y.Z]` 段落，把記錄「某檔在 v2.8.0 被刪除」這類**歷史事實版號**誤判為 drift、flip 成當前版號。新增 `skip_released_changelog` rule flag + `_split_at_released_changelog()` helper：第一個 `## [vX.Y.Z]` heading 以下視為凍結歷史、排除於掃描之外，`## [Unreleased]` 以上的 in-flight 內容仍照常處理；`--check` 與 `--what-if` 兩條路徑一致套用。PR #503 當時把 `於` 改 `在` 閃避 regex 的迂迴改字不再是必要 workaround。Regression test：`tests/dx/test_bump_docs.py::TestSkipReleasedChangelog`。

- **doc-map / tool-map 產生器版號改讀 SSOT**：`generate_doc_map.py` 與 `generate_tool_map.py` 原本把 frontmatter `version:` 字串硬編在 Python source 的 frontmatter list literal 內 — `bump_docs.py --check` 偵測不到（版號藏在 runtime 產生的字串裡、非 checked-in frontmatter 欄位），每次平台發版得手動補丁兩支檔案（v2.8.0→v2.8.1 release commit 即中招，PR [#503](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/503)）。新增共用 helper `scripts/tools/_lib_versions.py`，三支 dx 文件產生器（doc-map / tool-map / cheat-sheet）改從 CLAUDE.md `## 專案概覽` lead-in 行讀平台版號 — anchor 與 `bump_docs.py` 的 platform write rule 一致，`bump_docs.py --platform` 一跑即自動傳遞，不再需要手動補丁。順帶修正 `generate_cheat_sheet.py` 自 v2.6.0 CLAUDE.md 改版面後即失效、一路 fallback 的舊 regex。doc-map 重新產生零 diff；tool-map 的 Shared Libraries 段落自動新增 `_lib_versions.py` 一列。

- **tenant-api forge client 正確性修正三則**（[#615](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/615)）：① **GitHub PR 分頁**——`ListOpenPRs` 原單頁抓取（`per_page=100` 無迴圈），open PR >100 筆時漏看 → dedup 判斷失準、對同一租戶重複開 PR；改為 `Link` header（`rel="next"`）驅動翻頁，對齊既有 GitLab 行為。② **create-time 403 graceful handling**——read-scoped token 過得了 `ValidateToken`（打 `/user`），卻在開 PR/MR 時才 403；新增 `platform.APIError` + `platform.ErrForbidden`，兩 forge client 的 403 譯為 **clean HTTP 403**（`code: FORBIDDEN`，不外洩上游 response body、不再回 500），讓 da-portal 能精準觸發權限錯誤 UI。③ **同租戶並發寫 TOCTOU**——`HasPendingPR` 檢查與 `RegisterPR` 之間有空窗，兩並發請求可同時開單；改以 tracker 的**同步原子** `ClaimTenant` / `ReleaseClaim` 去重（check-and-set，不依賴 async poll cadence，避免類比 #527 的 poll 空窗 race）。**設計決策**：dedup 維持單副本約束（in-memory tracker + claim 不跨 pod）——PR 寫回模式須 `replicaCount=1`，已於 `helm/tenant-api/values.yaml` 與 `internal/platform/tracker.go`（`ClaimTenant` doc）明文記錄。測試：httptest 驗 >100 分頁不漏 + create-time 403 clean error；並發 single-winner 測試驗只開一張 PR。

### Changed

- **mkdocs build：移除 `docs/rule-packs` symlink，改用 build-time hook**：root `rule-packs/*.md`（Rule Packs / Alert 速查兩頁）改由 mkdocs hook（`scripts/mkdocs/rule_packs_bridge.py`，`on_pre_build` 複製進 `docs/rule-packs/`、`on_post_build` 清除，gitignored）surfacing 進站，取代原 `docs/rule-packs -> ../rule-packs` 的 git symlink。根因：Windows `core.symlinks=false` 把 symlink 材料化成 13-byte 文字檔、`mkdocs build` 走不進去 → 本機 strict build 噴 ~32 條 `rule-packs/* not found` 假警示（CI/Linux 正常），逼迫 `MKDOCS_STRICT_BYPASS=1` 推送。改 hook 後 local／docs-ci／`mkdocs gh-deploy` 三路徑一致、跨平台不再 false-fail；`rule-packs/` 仍留在 repo root（da-tools 與產生器消費）。

---

## [v2.8.1] — secret-scan 四層防線 + Planning SSOT + DX 工具鏈收斂 (2026-05-16)

### Security

- **Secret-scan multi-layer doc-as-code sync — #445 完成**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC iv / Chunk 5 of 5，收尾）：補齊 L0/L1/L2/L3 secret-scan 多層防線的文件層。`docs/internal/dev-rules.md` 新增 **§安全紀律（Secret Hygiene）** — 四層防線指引 + 三條規範（`git commit --no-verify` 嚴禁繞過 secret scan、leak 發生即進 SOP、`--no-verify` 違規記入 post-mortem）；dev-rules.md size cap 500→520（§安全紀律 為實質新內容，`--no-verify` ban 是無法 code-enforce 的純文字規則 — 走 `check_devrules_size.py` 既訂的「改門檻需 CHANGELOG + PR-body 理由」程序）。`CLAUDE.md` 架構速查段加一行 Secret-scan 四層防線速覽。`docs/internal/github-release-playbook.md` §版號驗證 加註 release-time L3 digest verification step（`release.yaml` 自動跑，#445 AC iii）。`secret-leak-remediation-sop.md` 的 forward-reference 收斂（`dev-rules.md §安全紀律` + `secret-scan.yml` 兩個 target 現都已存在）。**doc-map 修正**：AC iv body 原列「doc-map.md 新增 secret-scan.yml + SOP 條目」，但 `generate_doc_map.py` 只索引 `docs/`（`SKIP_DIRS` 含 `internal/`）、`.github/workflows/` 完全不索引 — 故兩者皆不入 doc-map，不強加錯誤條目。**#445 五個 AC 全數完成**：AC v Remediation SOP（PR #492+#493）／ AC iii release digest verification（#494）／ AC i L1 pre-commit hook（#495）／ AC ii L2 server-side workflow（#496）／ AC iv 本 PR。

- **L2 server-side secret-scan workflow (trufflehog → SARIF → Code Scanning)**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC ii / Chunk 4 of 5）：新 `.github/workflows/secret-scan.yml` — L0/L1/L2/L3 多層的 **L2 不可繞 server-side gate**（L1 pre-commit hook 可被 `--no-verify` 繞過，L2 在 GitHub 基礎設施上跑、與 contributor 本地工具無關）。**兩種 scan mode**：`pull_request` → diff-only scan（`trufflehog git --since-commit <merge-base>`，目標 ≤ 1 min）；`schedule`（nightly 02:00 UTC）+ `workflow_dispatch` → full git-history scan。**Diff scan 起點是 merge-base 不是 `base.sha`**（Gemini review of #496 — Git Ancestry Trap）：`pull_request.base.sha` 是 base 分支當下的 tip，若 main 在 PR 分支切出後又前進，該 SHA 不在 PR 分支 ancestry 內，trufflehog `--since-commit` 無法 bound walk 會 fallback 掃整段 history（破壞 ≤1min + 翻出無關舊 finding）；改用 `git merge-base HEAD origin/<base>` 算真正 fork 點。**Verified/unverified policy** 由新工具 `scripts/tools/lint/trufflehog_to_sarif.py` 持有（trufflehog 無原生 SARIF 輸出 — 只有 JSON / GitHub-Actions annotation format，故自寫 NDJSON→SARIF 2.1.0 converter）：verified finding（credential 確認活的）→ SARIF `error` level + converter exit 1 → workflow fail → PR merge 被擋 / nightly email maintainer；unverified|unknown → SARIF `warning` + exit 0 → 進 Code Scanning 但不擋 PR。**Forked-PR 處理**：fork 來的 PR 拿 read-only token 無法寫 security-events — diff scan 仍跑，SARIF 改走 `actions/upload-artifact` fallback（nightly full-scan 以 main-repo write token 補進 Security tab）；**刻意不用 `pull_request_target`**（用高權限 token 跑 fork 程式碼 = RCE-to-secrets 自殺）。Workflow `permissions:` 顯式最小化（`contents: read` / `security-events: write` / `pull-requests: read`，不繼承 repo default）；`concurrency` group + `cancel-in-progress` 防 Ghost-Green race；trufflehog 經官方 `install.sh` 以顯式 release tag `v3.95.3` 安裝（round-2 self-review 從 Docker image 改來 — 避免猜 container-image tag 字串 `v3.95.3` vs `3.95.3` 的 fabricated-pin 風險）。`GIT_LFS_SKIP_SMUDGE=1`：trufflehog `git` mode 內部會 `git clone` repo，本 repo 用 Git LFS 存 visual-regression PNG，clone checkout 觸發 LFS smudge 在 LFS object 缺失時失敗整個掃描掛掉 — secret scan 不需 PNG 二進位內容，skip smudge 讓 clone checkout 出 pointer files（純文字）即可。SARIF upload 兩個 step 都 `if: always()` + `steps.convert.outcome != 'skipped'` guard — verified finding 讓 converter exit 1 後 SARIF 仍須先上傳才讓 job 失敗傳播，但 trufflehog step 自己失敗時不該再噴 confusing 的 "SARIF not found"。Converter 24 unit tests（NDJSON parse / Git+Filesystem location 萃取 / verified 分類含 fail-safe「只有 literal True 才 block」/ SARIF shape / exit-code policy via fixtures），詳 `tests/lint/test_trufflehog_to_sarif.py`。Python tool count 161。

- **L1 pre-commit secret-scan hook (trufflehog)**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC i / Chunk 3 of 5）：新 pre-commit hook `secrets-scan-staged` 在 `.pre-commit-config.yaml` 註冊，呼叫 `scripts/tools/lint/check_secrets_staged.sh` 對**僅 staged files**（不掃全 repo）跑 `trufflehog filesystem --fail --no-verification`。Offline 模式（不打 trufflehog 自家 verifier API）求快，L2 server-side workflow（#445 AC ii / Chunk 4 未 landed）會做 verified-API 互補檢查。**False-positive 兩條 escape**：(a) `.trufflehogignore` 在 repo root，newline-separated regex paths（同 `.gitignore` 風格）；(b) inline `# trufflehog:ignore` 在那行尾（Python / YAML / shell 通用）。**`git commit --no-verify` 明確禁止**：失敗訊息直接 quote SOP rule #1（ASSUME COMPROMISE. ROTATE FIRST.）+ 點名 L2 仍會在 push 時擋下，--no-verify 只是把同個 leak 帶到本地 clone 多撐幾分鐘。**Trufflehog binary 缺失處理**：soft-skip + loud warning（**不** block commit）— 印出 Linux install.sh / macOS brew / Windows release-archive 3 種安裝 hint 後 `exit 0`（**不**列 `go install` — trufflehog 的 go.mod 含 replace directives，`go install` 會直接 fail；round-3 self-review 親測證實後改為明確標註此路不通）。理由：(a) 對齊 repo 既有 commit-msg hook 慣例「don't block commits on a missing validator」；(b) L1 本就是可被 `--no-verify` 繞過的 best-effort shift-left 層（issue #445 framing），真正不可繞的 gate 是 L2 server-side；(c) 硬擋一行 doc fix 在 binary 安裝上是糟糕 DX，只會逼人用 `--no-verify`（連帶跳過所有 hook）。warning 每次 commit 都印，吵到裝為止。**Exit-code 區分**（round-3 self-review）：trufflehog `--fail` 命中 secret 時 exit 恰為 183；其他 non-zero = 工具自身錯誤（bad path / crash）。Hook 分流兩種訊息 — 183 印 ROTATE-FIRST SOP，其他 non-zero 印「scan TOOL errored，非必然 leak，別跑 rotation SOP」，兩者都 fail-closed（block commit）。Hook entry 走 `language: system`（trufflehog binary 由 contributor 安裝），符合 Vibe `.pre-commit-config.yaml` 既有 `repo: local` only 慣例。`.trufflehogignore` 初始空檔（header comment 寫使用規則），patterns 由實際 false positive 增量加入。Pre-commit hook 計數 50 → 51 auto-run（CLAUDE.md 同步更新）。Performance target P95 ≤ 5s（staged files 規模下，offline 模式預期 sub-second）。

- **L3 supply-chain digest verification at release time**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC iii / Chunk 2 of 5）：在 `.github/workflows/release.yaml` 4 個 release job（exporter / da-tools / portal / tenant-api）的 image push 步驟後加 `Verify image digest` step，透過共用 shell helper `scripts/ops/verify_release_digest.sh` 跑 `skopeo inspect` 取 digest，並寫進 GitHub Actions job summary 作審計軌跡。**Two-tag verify semantic**（round-3 self-review 修正）：**永遠** probe `:v${VERSION}`（catches what we just pushed — silent push fail 防護）；當 chart-yaml 給且 `Chart.yaml appVersion ≠ $VERSION` 時 **額外** probe `:v${appVersion}`（catches chart claim with no image）。對 tenant-api（chart 2.8.0 wraps appVersion 2.7.0 的合法 decoupling）會 probe 兩個 tag；exporter / portal 因 `appVersion == version` 只 probe 一個；da-tools 無 chart 也只 probe 一個。**Catches**：(a) 上游 `docker/build-push-action` silent push 失敗；(b) Chart.yaml appVersion claim 對應 image 不存在；(c) GHCR 短暫 outage。**順道補 release-portal 缺漏的 Chart.yaml-vs-tag version-check**。Auth 走 `skopeo login --password-stdin` + `--authfile`（不用 `--creds USER:PASS` 把 token 暴露在 `argv` / `/proc/<pid>/cmdline`）。`GITHUB_TOKEN` 在每個 verify step 顯式以 `env:` 傳入（**GitHub Actions 不會自動 export 給 `run:` script，是 round-3 self-review 抓到的 critical bug — 沒這個 release CI 永遠 fail at exit 3**）。defensive：strip 掉 Chart.yaml appVersion 可能的 leading `v` prefix。skopeo 用 `sudo apt-get install` 在 ubuntu-latest runner 上裝（idempotent）。Helper script 3 個 exit code（1=arg error / 2=image not found or skopeo failed / 3=env misconfig），exit 3 含 `GITHUB_TOKEN` 漏傳的 fix hint。詳：`scripts/ops/verify_release_digest.sh` 內 docstring。

- **Secret Leak Remediation SOP — Gemini external-review amendment**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC v / post-merge follow-up）：post-merge 外部 review 帶回 4 個我自己多面向 review 漏掉的盲區，全採納：(1) **CRITICAL re-infection vector** — Step 4b 從舊 clone `git format-patch` 後直接 `git am` 進新 clone 會把含 leaked secret 的 patch 原封不動帶回，下次 push 重新污染歷史；改加強制 `grep` 檢查步驟 + 3 條 fallback（`git apply --reject` 手挑 hunk / 舊 clone `rebase -i` drop / 接受重做）；(2) **JWT mass-logout 副作用** — Step 2 JWT rotate 會觸發全網 Mass Logout + API-to-API 401 storm，rotate 前要 ping SRE / 客服避免被誤判系統崩潰而 rollback；(3) **Build artifacts poisoning** — Step 3 propagation table 新增 "自動化建置產物" row（leaked commit 觸發的 CI build 已把 secret 烤進 Docker image / 靜態 build cache，需 GHCR 標記為 vulnerable 或刪除 + 從乾淨 commit rebuild）；(4) **GitHub Support SLA expectation** — Step 4c 補非 Enterprise 客服 24-72 小時甚至數日的等待現實，強化「不能依賴 cache invalidation 來止血、Rotate-First 才是根本」。同步擴 反 SOP 表 3 條。Method-level note：上一 PR 的多面向 review 5 lenses 結構正確但**應用深度不足** — Gemini 的 4 點全屬「Operational realism」lens 應該覆蓋但實際走得太抽象、沒具體 walk-through 每個 step 在現實 incident 中可能怎麼 backfire。memory `feedback_adversarial_self_review_for_ops_docs.md` 更新加深-of-application 提醒。

- **Secret Leak Remediation SOP**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC v — Chunk 1 of 5）：新增 `docs/internal/secret-leak-remediation-sop.md`，定義公開 repo secret leak 發生時 contributor 不等 approve 直接 rotate 的 5-step response（Identify → Rotate → Notify affected systems / customers → Clean git history → Post-mortem）。鐵律 ASSUME COMPROMISE. ROTATE FIRST. 列頂；明訂 `git push -f` / BFG / `git filter-repo` 對已 push 到 public 的 secret 是安慰劑（fork / reflog / GHArchive / BigQuery dataset / SaaS cache 都還在）。Provider rotate 入口 cheat sheet（AWS / GCP / Azure / GitHub PAT / Slack / Stripe / 內部 DB / 內部 JWT signing key / 內部 OAuth client）。GitHub Support cache invalidation 工單範本。Decision tree、Triage Ownership（@vencil first + backup contact 待 v2.8.1 closure 前指派；可存取客戶資料 level 並行 Step 3b GDPR/客戶合約通知）、反 SOP「已知會放大傷害」表（先 push 再 rotate、等 approve、`git push --force` 無 lease 等 7 條）。本檔屬 #445 Chunk 1（doc-only standalone，零 CI 風險，day-2 review 明標「the most important AC」）；後續 Chunk 2-5 將補齊 L1 pre-commit hook (AC i)、L2 server-side GHA workflow (AC ii)、L3 release.yaml digest verification (AC iii)、doc-as-code sync (AC iv)。

### DX

- **移除 phantom no-op lint `check-techdebt-drift`**：`check_techdebt_drift.py` 的資料來源 `docs/internal/known-regressions.md` 已在 v2.8.0 被 phantom-delete，該 lint 隨即 graceful 退化為永遠 exit 0 的 no-op（`parse_registry()` 對缺檔回空 dict → `main()` 印「nothing to check」return 0）；其職責由繼任者 `check_planning_status_sync.py`（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2b，ADR-019 Layer 3 — 從 PR commit trailer 驗 planning entry status sync）接手。移除 `scripts/tools/lint/check_techdebt_drift.py` + `tests/lint/test_check_techdebt_drift.py` + `.pre-commit-config.yaml` 的 `check-techdebt-drift` pre-push hook，pre-push hook 計數 4 → 3（`CLAUDE.md` 同步）；`tool-map.md` / `.en.md` 重新產生，`dev-rules.md` §P1 與 `planning-id-mapping.md` §影響的 lint 引用更新。

- **`_lib_compat.try_utf8_stdout()` Phase B sweep — 77 tools migrated**（issue [#489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/489) Phase B）：對 scripts/tools/ 下 77 個 emit emoji 但無 stdout encoding 設定的工具（CLI tier — `def main(...)` 入口、非 library module）以 ast-based sweep 加入 `from _lib_compat import try_utf8_stdout` + `try_utf8_stdout()` 為 main() 第一行。Script 處理三類 edge case：(1) 多行 `from X import (a, b, c)` block — 用 ast `end_lineno` 找閉括號位置，不破壞 import；(2) `def main(): """docstring"""` — 偵測 first body node 是否為 docstring，是的話 `try_utf8_stdout()` 注入到 docstring 之後（保留 docstring 性質）；(3) 既有 `_THIS_DIR = Path(__file__).resolve().parent`（5 個檔案）— 偵測到不 clobber，重用既有變數並 `str(_THIS_DIR)` wrap 讓 `sys.path.insert` 對 Path/str 都通；anchor 點放在既有 `_THIS_DIR =` 之後（避免 NameError）。1 個 library module（`_grar_render.py` — 無 `def main()`，被 `generate_alertmanager_routes.py` import）刻意跳過（library 不該動 stdout）。7241 個 pytest 全綠（3 個 pre-existing flakes 與本次無關，stash-verified）。本次後 `grep -rn '^[^#]*sys\.stdout *= *io' scripts/` 回空 — legacy module-level pattern 完全退役（Phase A 退 `pr_preflight.py` + Phase B 退 77 個從未設定的工具 = 全部）。Phase B 與 Phase A 同 issue 但因 scope 大、commit 巨拆成獨立 PR 易 review。Method-level note：兩輪 sweep script bug 都靠 pytest catch（不是 ast parsing — ast OK 但 runtime semantic 錯）— 提醒「parses cleanly」≠「runs correctly」，full test 必跑。

- **`pr_preflight.py` 採用 `_lib_compat.try_utf8_stdout()`**（issue [#489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/489) Phase A）：移除 module-level `io.TextIOWrapper(sys.stdout.buffer)` 副作用-on-import 舊 pattern，改用 PR #432 引入的 `from _lib_compat import try_utf8_stdout` + `main()` 第一行呼叫。`pr_preflight` 在 Windows hosts 每個 commit 都跑、又會 print emoji，屬於 hot path。本次遷移後 `scripts/` 全 repo 已無 legacy module-level pattern（grep `^[^#]*sys\.stdout *= *io` 回空）。84 個既有 test 全過。其餘 ~79 個 emit emoji 但無 encoding 設定的工具歸 `_lib_compat.py` docstring 既訂政策「proactive on next touch」處理，#489 closed-as-Phase-A-only（scope refined：原估 ~20 個 batch 遷，實測 79 個 + 既有政策不鼓勵 retroactive sweep）。

- **`diag_pr_ci.py` — PR CI 自動排障 CLI**（issue [#446](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/446)）：新工具 `scripts/tools/dx/diag_pr_ci.py` + `make diag-pr ARGS="<PR-N>"` 把 PR 失敗 check 摘要成 markdown / JSON 報告。4-endpoint 串接（`/pulls/{n}` → head sha → `/commits/{sha}/check-runs` paginated → 每個失敗 Actions check 的 `/actions/runs/{id}/jobs` + 全部失敗 check 的 `/check-runs/{id}/annotations`）。**架構選擇** per #446 day-2 review pivot：用 `subprocess.run(["gh", "api", ...])` 取代 `requests`/`urllib`（gh 處理 auth / pagination / rate-limit / retry，contributor 不用管 PAT；scripts/ 內 10 隻打 Prometheus/K8s/OPA 的 requests-using script 無 github.com 既有 pattern 要對齊）。**Prerequisite probe**（3 個 distinct exit codes）：exit 2 = `gh` 缺失或未認證（install / `gh auth login`）、exit 3 = api.github.com 不通（Cowork VM proxy 常見症狀，切 Windows MCP / Dev Container）、exit 1 = 工具內部錯誤、exit 0 = 工具成功（無論 CI 是否紅）。**Output**：`--markdown`（預設，每 check 截 5 條 annotation 避免破 GitHub PR comment 65K 上限）+ `--json`（不截，給 machine consumer）。**Edge cases**：external-app check（CodeCov 等，無 `/actions/runs/<id>/` 在 details_url）跳過 jobs 但仍嘗試 annotations；mid-flow `/jobs` 失敗不 abort，graceful degrade 為 "no job breakdown available"；PR-not-found 改 clean 訊息（不噴 raw gh stderr）。採用現代 stdout 設定 pattern — `from _lib_compat import try_utf8_stdout` + `main()` 第一行呼叫，取代 module-level `io.TextIOWrapper(sys.stdout.buffer)` 副作用-on-import 舊 pattern（per `_lib_compat.py` docstring「Apply this helper proactively when next touching one of those tools」)。34 unit tests 用 side_effect router pattern 把 4 個 endpoint 各 mock 到 fixture file（`tests/dx/fixtures/diag_pr_ci/*.json`，可 `gh api ... > fixture.json` 重錄），涵蓋 gh_api wrapper（含 paginate=True 自動拉長 timeout 30s→90s + explicit timeout override + TimeoutExpired 收斂為 GhApiError rc=124）/ 3-step prereq probe（含 rate-limit `remaining < 10` 早退 + auth-status timeout）/ 4-call sequential ordering / `app.slug == "github-actions"` 確定性判斷取代純 URL regex 啟發式 / 各 formatter（含 whitespace-only annotation message 不 crash + 空 jobs 用 neutral 訊息）/ `--json` 不截斷 / mid-flow error graceful degrade / PR-not-found dispatch。詳：[windows-mcp-playbook §已知陷阱速查 #64](docs/internal/windows-mcp-playbook.md#已知陷阱速查)。

- **Self-Review-Pass-2 trailer CI 軟性閘門**（issue [#454](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/454)）：新 `scripts/tools/dx/pr_preflight.py --check-pass2-trailer-strict` flag 用 git native trailer parser（`--format=%(trailers:key=Self-Review-Pass-2,valueonly=true,unfold=true)`，**不**用 regex — 自動處理 case-insensitive / multi-line folded / 「trailer 必須在 bottom paragraph 前空行」git 規則）掃 `<base>..HEAD` 範圍內所有 commit，任一 commit 含 trailer 即 PASS。Empty range 走 SKIP（exit 0）避免 false-fail on HEAD-on-base / behind-base 情境。新 workflow `.github/workflows/self-review-pass2.yaml` 走 `continue-on-error: true` 軟性失敗（adoption 穩定後切硬性），`actions/checkout@v4 fetch-depth: 0` 同 `planning-status-sync.yaml` trap pattern。PR Template 把 trailer block 從 conditional "Optional" 升格為 expected default 區段。Coordination：本 issue 是 v2.9.0 [#453](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/453) mutmut 的 enabling step — mutmut surviving mutation 出來時可倒推 `git log --format=%(trailers:...)` 找對應 PR 是否 claim 過 pass-2，識別 docstring vs code 偏離 pattern。Day-2 review 校正了 Gemini 原 proposal 的 "squash → check PR body" 前提（repo 的 `squash_merge_commit_message: COMMIT_MESSAGES` 拼接 commit msgs 不用 PR body，所以掃 git log 才對）。8 unit tests cover trailer-present / no-trailer + amend hint / empty-range SKIP / rev-list-fail with fetch-depth hint / log-fail / custom base-ref threading / git-not-on-PATH uniform error / CLI dispatch（`tests/dx/test_pr_preflight_orchestrator.py::TestCheckPass2TrailerStrict`）。

- **Windows MS Store Python stub 防呆**（issue [#436](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/436)）：Windows 11 fresh install 在 `%LOCALAPPDATA%\Microsoft\WindowsApps\` 放的 `python3.exe` App Execution Alias placeholder 通過 `command -v` 但執行回 exit 49（`Python was not found; run without arguments to install from the Microsoft Store`），讓 `scripts/hooks/commit-msg` 每次 commit 被擋 + `scripts/tools/dx/pr_preflight.py::check_scope_drift()` 永遠回報 `❌ Scope drift`。修法兩面：(i) commit-msg shell candidate list 改 `"py -3" "py" python3 python ...` 並加 `--version` probe（stub 失敗即跳下一個 candidate）；(ii) `pr_preflight.py` + `scripts/tools/lint/check_pr_scope_drift.py` 的 subprocess invocation 用 `sys.executable` 取代 bare `"python3"`，由當前 interpreter self-fork 繞過 `CreateProcess` PATH lookup。Regression test 鎖 `cmd[0] == sys.executable`（`tests/lint/test_check_pr_scope_drift.py::TestCheckToolMap::test_uses_sys_executable_not_bare_python3` + `tests/dx/test_pr_preflight_checks.py::TestCheckScopeDrift::test_uses_sys_executable_not_bare_python3`）。Trap codify 至 [windows-mcp-playbook §已知陷阱速查 #63](docs/internal/windows-mcp-playbook.md#已知陷阱速查)。Dev Container / WSL / Linux 不受影響（無 MS Store stub）。

### 文件治理

- **ADR frontmatter migration — #379 chunk 3 wrap-up**：ADR-001 ~ ADR-018（19 個檔案）加 ADR-019 frontmatter spec 欄位（`id` / `tracking_kind: adr` / `status: accepted` / `domain: <subsystem>` / `created_at` 從 `git log --diff-filter=A` 取首 commit 日期 / `updated_at: 2026-05-13`）。Status 從各 ADR 既有 `## 狀態` H2 區塊解析（emoji + bold name），mapping `Accepted/Extended → accepted`、`Proposed → proposed`、`Rejected → abandoned`、`Superseded → superseded`。Domain 來自 hand-authored mapping table（per-ADR judgment）。ADR-019 自身 self-bootstrap（之前是 SSOT spec 但沒套用自身規則）。`docs/internal/planning-index.md` 從 19 → 39 entries（accepted 20 / in-progress 2 / proposed 16 / done 1）。**Note**：`adr-index` 渲染不變，因 chunk 2a `generate_adr_index.py` 用 `## 狀態` 區塊解析、不依賴 frontmatter。`frontend-quality-backlog.md` 為 meta-policy 文件（無 entry）skip migration；`v2.8.0-planning*.md` + `known-regressions.md` phantom-deleted skip。
- **Backlog frontmatter migration — dx-tooling-backlog**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 3）：18 個 `### TRK-001` ~ `TRK-018` entries 加 ADR-019 frontmatter spec yaml 區塊（`id` / `tracking_kind: dx` / `status` / `domain` / `created_at` / `updated_at`）。Default `status: proposed`，TRK-006 / -010 / -011 個別覆蓋為 `in-progress` / `done` 反映實際進度。`docs/internal/planning-index.md` 從 1 entry (ADR-020) 擴張為 19 entries (`in-progress 2 / done 1 / proposed 16`)。順帶修 chunk 2a `SECTION_YAML_RE` 的 multi-line heading bug：`(?P<heading>.+?)` with `re.DOTALL` 能跨多 H2/H3 backtrack 撈出整段 prose 當 title；改 `(?P<heading>[^\n]+?)` 限制單行。新增 regression test `test_heading_does_not_span_multiple_sections` pin 此 contract。Follow-up：其餘 backlog files（`frontend-quality-backlog.md` 等）migration 待後續 PR 處理。
- **Legacy ID code-side sweep**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 1b code）：~150 個非-md 檔案（57 ts / 31 py / 24 jsx / 22 js / 13 go / 4 yaml / 3 tsx / 2 yml / 1 Makefile）內 328 處 `TD-NNN` / `TECH-DEBT-NNN` / `HA-NN` / `REG-NNN` 機械替換為對應 `TRK-NNN` alias（per `planning-id-mapping.md` §編號分區算法）。`check_skip_a11y_justification.py` 的 `RE_JUSTIFICATION` regex 同時擴張為 `(?:TRK-\d+[a-z]?|TD-\d+)`，過渡期兩種形式都接受。Exclude 6 個 lint pattern definition + test fixture 檔（`check_codename_leak.py` / `check_techdebt_drift.py` / `check_skip_a11y_justification.py` source + 3 個對應 test fixtures），這些刻意保留 legacy ID 用以驗 lint 自身。Post-sweep：2320 pytest 全通過（1 個 pre-existing platform-dependent failure 不關此 sweep）+ codename-leak 0 leaks + doc-links valid + tool-map.md regenerated。
- **Planning status sync CI gate**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2b）：新工具 `scripts/tools/lint/check_planning_status_sync.py` 實作 ADR-019 Layer 3 — 從 PR commit 範圍 (`<base>..HEAD`) 用 git native trailer parser (`git log --format='%(trailers:key=Resolves,valueonly=true,unfold=true)'`) 抽取 `Resolves|Closes|Fixes` trailer，對每個 in-scope ID（`TRK-NNN` / `ADR-NNN` / `S#NNN`，legacy `TD-`/`HA-`/`REG-` 不解析）驗：(1) repo 內有對應 planning entry、(2) `status: done`、(3) `pr_ref:` 對齊 current PR number。29 unit tests cover trailer parser（含 RFC-2822 blank-line semantics / lowercase verb / sub-PR letter suffix `TRK-230c` / legacy ID 不 match）+ validate_sync + ID regex 邊界。Default soft-warn（exit 0 + annotations，per ADR-019 Layer 3「黃燈」），`--strict` 升為 hard-fail。CI workflow `.github/workflows/planning-status-sync.yaml` 自動跑（`actions/checkout@v4 fetch-depth: 0` 關鍵 — 否則 `<base>..HEAD` 解不出）。Reuses chunk 2a `generate_planning_index.discover_all()` 的 4-source discovery。Python tool count 157 → 159（吸收 pre-existing +1 drift + 本 PR +1）。
- **Planning index 自動化**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2a）：新工具 `scripts/dx/generate_planning_index.py` 實作 ADR-019 Layer 2 — 從 4 個 source（`docs/**/*.md` top-of-file frontmatter / 嵌入式 yaml block / `flaky-tests.yaml` / code-comment `// TECH-DEBT(id=...)` 註解）發現帶 `tracking_kind:` 的 planning entry，分組（按 status × tracking_kind）渲染到 `docs/internal/planning-index.md` 哨點區塊。每個 entry 連結回 source path + line number。pre-commit drift gate `planning-index-check` + `make planning-index` 本地刷新。Top-level pre-commit hook 計數 49 → 50 auto-stage。
- **Migration Guide hub slim**（issue [#378](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/378) II-1）：`docs/migration-guide.md` 從 808 行瘦身至 357 行 (-56%)，雙語同步。新增 §5-Step 高階流程 作為中心導航（從 toolkit 安裝到 cutover 收斂的 5 步表格 + 各步驟 anchor 連到對應章節 / spoke）；§0-§13 各章節保留 anchor ID（byte-for-byte，舊書籤不失效），body 收斂為 2-4 句摘要 + 連到 `cli-reference.md` / `migration-engine.md` / `shadow-monitoring-sop.md` / `scenarios/incremental-migration-playbook.md` / `scenarios/multi-system-migration-playbook.md` 等既有 spoke。§7 維度標籤（無 clean spoke）以壓縮 inline 形式保留。`### Q:` FAQ 全保留。
- **ADR 索引自動化**（issue [#378](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/378) II-2）：新工具 `scripts/dx/generate_adr_index.py` 從 `docs/adr/` frontmatter + `## 狀態` 區塊自動渲染表至 `docs/architecture-and-design.md` 的 `<!-- ADR_INDEX_START/END -->` 哨點之間。新增 ADR 漏接 hub 索引的問題（ADR-018/019/020 都曾如此）由 pre-commit drift gate `adr-index-check` 機械擋下；本地用 `make adr-index` 重新渲染。Top-level pre-commit hook 計數 48 → 49 auto-stage。

---

## [v2.8.0] — 客戶導入管線 + 千租戶 Scale 驗證 + 自動化收斂 (2026-05-12)

v2.8.0 把 v2.7.0 的 Scale Foundation I（`conf.d/` 階層 + dual-hash 熱重載）推進為**可導入既有 kube-prometheus 客戶**的完整 pipeline：4 條 Go binary 把客戶 PromRule corpus 自動化轉成 Profile-as-Directory-Default `conf.d/` 樹，三條交付路徑（Docker / static binary 6-arch / air-gapped tar）+ supply-chain provenance（cosign keyless + SBOM）滿足全光譜部署環境。Tenant Manager 在 1000+ 租戶規模上以 server-side search + virtualization 維持 p99 < 200 ms。56 個 pre-commit hook（39 auto + 14 manual + 3 pre-push）把開發規範從 reviewer convention 升級為 mechanical net。

### Highlights — 5 條

- **客戶導入自動化** — 4 隻新 Go CLI（`da-parser` / `da-tools profile build` / `da-batchpr` / `da-guard`）把 kube-prometheus 客戶現有 `PrometheusRule` corpus 導入到 `conf.d/` Profile-as-Directory 架構；1000 租戶導入從一週縮到一天。安裝見 [Migration Toolkit Installation](docs/migration-toolkit-installation.md)。
- **Tenant Manager 邁入 1000+ 租戶規模** — UI 直打 `/api/v1/tenants/search`（伺服端 search / pagination，page_size 預設 50 / 上限 500）取 live data；Saved Views frontend 接 v2.5.0 已存在的 backend CRUD；TenantCard 加 Alert Builder / Routing Trace deep link。
- **Defaults 變動可預演** — `/api/v1/tenants/simulate` ephemeral primitive 讓 CI 與 UI 在 commit 前預測 `_defaults.yaml` 變動對 inheritance 的影響（無 disk IO、無 manager state mutation）；新 `da_config_blast_radius_tenants_affected` histogram 量化每 tick 受影響 tenant 分佈。
- **Q2 2026 CVE 集中收斂** — Grafana / Prometheus 3.x / Alertmanager / oauth2-proxy / alpine-git / Python base / Go toolchain 一次 bump，清掉 50+ CVE（13 個 CRITICAL、35+ HIGH），涵蓋 auth bypass、RCE、memory safety、TLS DoS。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100)。
- **API 邊界硬化** — `tenant-api` body-content range validation 在邊界 fail-fast（patch key/value 超長、`_timeout_ms` 超過 1 小時、`_silent_mode` 非 enum 都直接 400 + 完整 violations array）；flat + nested mixed-mode 同 tenant id 重複從 WARN 升級為 hard error。

### 客戶導入管線（5-step chain，新功能）

- **`da-parser`**：PromRule YAML → canonical JSON。dialect 偵測（`prom` / `metricsql` / `ambiguous`）+ VM-only function allowlist（`vm_only_functions.yaml` 走 `go:embed`，CI freshness gate 偵測新版 metricsql 上游函數）+ `StrictPromQLValidator` + provenance header。`prom_portable: bool` 旗標讓客戶遷入 VM 後仍能識別「可回 Prom」的子集 — anti-vendor-lock-in 具體承諾
- **`da-tools profile build`**：cluster 相似 rules → median 演算法決定 cluster 共通閾值 → 寫 `_defaults.yaml`、偏離 tenant 寫 `<id>.yaml` 只含 override；fuzzy matching opt-in 套 duration-equivalence canonicalisation（`[5m]` ≡ `[300s]` ≡ `[300000ms]`）；遵循 [ADR-018](docs/adr/018-profile-as-directory-default.md) Profile-as-Directory-Default
- **`da-batchpr apply` + `refresh`**：Hierarchy-Aware 分塊 — `_defaults.yaml` 變更打 Base Infrastructure PR、tenant PRs 標 `Blocked by:`；`refresh --base-merged` 在 Base merge 後自動 rebase tenant PRs；`refresh --source-rule-ids` 對 parser bug fix 細粒度重生 patch PR
- **`da-guard`**：Schema / Routing / Cardinality / Redundant-override 四層檢查；`.github/workflows/guard-defaults-impact.yml` 自動跑 + sticky PR comment（marker-based update vs create）+ artifact 14d retention
- **Migration Toolkit 三條交付路徑**：(a) Docker pull `ghcr.io/vencil/da-tools`；(b) Static binary linux/darwin/windows × amd64/arm64 共 18 個 archive；(c) Air-gapped tar（`docker save` export）。每條路徑 cosign keyless 簽 + SBOM SPDX/CycloneDX；客戶 `make verify-release` 一鍵驗

### Scale Foundation III — 千租戶生產驗證

- **1000-tenant baseline land**：`make benchmark-report` 17 benches × count=6 跑 nightly cron；mixed-mode flat+hierarchy benches 加入 trend tracking。Cold load 112 ms / steady-state reload 1.3 ms @ 1000 tenants
- **`/api/v1/tenants/simulate` + Ephemeral Graph**：tenant.yaml dry-run preview（不污染 watch loop）；CI gate `TestSimulate_VsResolve_ParityHash` 鎖死「simulate=commit-後 preview」契約
- **5-anchor end-to-end alert fire-through harness**（`tests/e2e-bench/`）：從 `conf.d/` 寫入 → exporter reload → Prometheus alert trigger → Alertmanager dispatch → webhook receiver 的完整鏈；n=30 + bootstrap 95% CI；docker-compose 6-service stack
- **Bench-gate 兩層 CI 治理**（[#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433)）：Tier 1 per-PR single-runner sequential（同 VM 同 CPU by construction → INCONCLUSIVE ~0%）；Tier 2 release-time cumulative drift report（informational）；`override: bench-regress-ok` label 走獨立 `bench-override-audit.yaml` workflow event-scoped 驗證

### Customer-Facing Surface

- **Server-side Search API** `GET /api/v1/tenants/search`：page_size cap 500 + closed-field free-text + RBAC-before-pagination + 30s TTL `tenantSnapshotCache`，p99 < 200 ms @ 1000T
- **Tenant Manager JSX**：API-first 3-layer priority chain（API → platform-data.json → DEMO）+ 429 retry-with-backoff + URL state（`useURLState` + `useDebouncedValue`）+ self-written `useVirtualGrid`（>50 才 virtualize）
- **Master Onboarding Dual Entry**：Import Journey 5 步（parser → profile build → batch-pr → guard inline CLI）vs Wizard Journey 5 步（cicd-setup → deployment → alert-builder → routing-trace → tenant-manager — 全 5/5 真 wizards）
- **TenantCard × Wizard 整合**：footer 三鈕（Alert / Route / Preview）deep link + `?tenant_id=` URL 參數預填 + 獨立 `simulate-preview.jsx` widget（4-state machine + 500ms debounce + AbortController）
- **Smart Views**：`useSavedViews` + `SavedViewsPanel` 接 v2.5.0 backend `/api/v1/views` CRUD；RBAC-aware（Save/Delete hidden when `canWrite=false`）

### 測試與 CI 基礎設施

- **`ConfigManager` test-only setter 注入**：`m.SetMetrics` / `m.SetLogger` / `m.SetClock` 取代 v2.7.x 三類 global-swap（`withIsolatedMetrics` / `log.SetOutput` / WatchLoop `time.Sleep`）；過去因 global state race 必須 serial 的測試現在 `t.Parallel`-eligible，full app pkg `-count=5 -race` 12.1s（previously 20.7s）。AI agent quickref → [`test-map.md` §測試注入 Seam](docs/internal/test-map.md)
- **Policy-as-Code lint**：`check_hardcode_tenant.py`（dev-rules #2 PromQL label selector）/ `check_subprocess_timeout.py`（FATAL）/ `check_jsx_loader_compat.py` / `check_playwright_rtl_drift.py` / `check_codename_leak.py`（[#462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/462) / [#468](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/468)）等。56 hooks 共 39 auto + 14 manual + 3 pre-push
- **Tenant API hardening**：rate limit per-pod + `X-Request-ID` echo + tenant-scoped authz（4 endpoints）+ body-content range validation（go-playground/validator + struct tags + reservedKeyValidators registry）
- **`bump_docs.py` 跨四條 release line 機械 bump**：91+ 文件 frontmatter / helm Chart / Dockerfile / k8s cronjob image tag 一次同步（[#439](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/439)）

### 文件治理

- **Customer-facing docs codename leak sweep**（[#462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/462) / [#468](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/468)）：22 customer-facing 文件、~213 處 codename → feature-name 替換；`check_codename_leak.py` PATTERNS 加 `DEC-X` / `v\d+\.\d+\.\d+-(final\|rc\d*\|alpha\|beta\|preview\d*)`；default scope 從 T0 + component README 擴到 T1（`docs/**` 排除 `internal/`）；新 `PER_FILE_ALLOWLIST` 機制收 ADR-019。Layer 2 self-healing glossary gate → [#469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/469)
- **Planning ID namespace 統一為 TRK**（[#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 1）：依 [ADR-019 Option C refined hybrid](docs/adr/019-planning-ssot.md) 把 `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NNN` 四個分散 namespace 統一為 `TRK-NNN`；新增 [`planning-id-mapping.md`](docs/internal/planning-id-mapping.md) 含完整對映表 + 三段分區編號政策
- **`docs/benchmarks.md` customer-first 重寫**（[#460](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/460)）：488/536 → 209/209 行（−57% / −61%），TL;DR 5 個數字 + v2.2.0 → v2.8.0 evolution + 1000-tenant Scale Gate + e2e fire-through + soak + sizing 七段；toolchain micro-bench detail 移到 [`benchmark-playbook.md §Engineering Reference Benchmarks`](docs/internal/benchmark-playbook.md)
- **ZH-primary SSOT policy lock**：v2.5.0 評估文 §7 原推薦切換 EN SSOT，pilot 工具於 `v2.7.0` 完成；v2.8.0 經內部維運手冊的 4-question audit（含「spec premise validation」一條）後 reverse 原計畫。Pilot 工具保留 dormant，trigger conditions 明確 codify

### Benchmark（1000 tenants, Dev Container, Intel Core 7 240H, Go 1.26.2 linux/amd64, `-benchtime=3s -count=6`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 1.3 ms | Dual-hash steady-state reload（86x 快於 cold） |
| `Simulate_DeepChain` | 5 ms | `/simulate` endpoint per-call |
| 5-anchor e2e fire-through P99 | **4.98 s** | conf.d 寫入 → webhook receiver 全鏈（受 Prometheus 5 s scrape quantization 主導，near-flat 1000→5000 tenants；n=30, bootstrap 95% CI） |

SLO 維持 v2.7.0：cold load 112 ms / 1000 tenants；reload 熱路徑 1.3 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md`](docs/benchmarks.md)。

### ADR 新增（ADR-018 / 019 / 020，3 條）

- **ADR-018** Profile-as-Directory-Default：`_defaults.yaml` 為 cluster 共通閾值的權威位置
- **ADR-019** Planning SSOT：`TRK-NNN` namespace 統一政策（Option C refined hybrid）
- **ADR-020** Tenant Federation outline（v2.9.0+ design seed）

### Breaking changes

- **Mixed-mode duplicate tenant id**：flat + nested 同 tenant id 重複從 WARN 升級為 typed `*DuplicateTenantError` hard error。同 tenant id 出現在兩個位置會直接拒絕 load（state preservation invariant 保證舊 state 不被部分覆寫）
- **`tenant-api` body validation**：超出 range 的 patch 從 silently accept 變成 400 + 完整 violations array（`_timeout_ms` > 1 小時 / `_silent_mode` 非 enum / key|value 超長 等）。客戶 CI 若有 silently-tolerated bad payload 會在升 v2.8.0 後立刻 fail
- **`Open-mode` PUT/DELETE Groups**：tenant-scoped authz 補完後，open-mode 環境（缺 `_rbac.yaml`）下 PUT/DELETE Groups 從可寫變成 403。修補：補 5 行 `_rbac.yaml` `groups: [{name: dev, tenants: ["*"], permissions: [admin]}]`

### Upgrade notes

- 既有客戶：升 v2.8.0 後 mixed-mode duplicate tenant id 不再 silently tolerated — 升前先掃 `da-tools validate-config --strict` 確認沒有重複
- 開啟 Customer Migration Pipeline：見 [Migration Toolkit Installation](docs/migration-toolkit-installation.md)，三條交付路徑任選；客戶 `make verify-release` 驗 cosign + SBOM
- Tenant API hardening：升前讀 [`tenant-api-hardening.md`](docs/api/tenant-api-hardening.md) §3 affected endpoints + §5 known gaps（含 open-mode 升級 checklist）

---
## [v2.7.0] — 千租戶配置架構 + 元件健壯化 (2026-04-19)

v2.7.0 把租戶配置的資料結構升級為可支撐千租戶規模（`conf.d/` 階層 + `_defaults.yaml` 繼承引擎 + dual-hash 熱重載），把 v2.6.0 的 Design Token 定義推進到全面採用，並把測試與 CI 從「能跑」升級為「可規模化」。

### Scale Foundation I — 千租戶配置架構（ADR-016 / ADR-017）

- **`conf.d/<domain>/<region>/<env>/` 階層目錄**：任一層可放 `_defaults.yaml`，`L0 defaults -> L1 domain -> L2 region -> L3 tenant` 四層 deep merge，array replace / null-as-delete 語義明確
- **Dual-hash 熱重載**：`source_hash`（原始檔 SHA-256）+ `merged_hash`（canonical JSON SHA-256）並行追蹤，merged_hash 變才 reload；300ms debounce 吸收 K8s ConfigMap symlink rotation 的連續寫入
- **Mixed-mode**：舊扁平 `tenants/*.yaml` 與新 `conf.d/` 可共存，無強制一次遷移
- **`GET /api/v1/tenants/{id}/effective`**：回傳 merged config + 繼承鏈 + dual hashes，方便 debug 實際生效設定
- **新 CLI**：`da-tools describe-tenant`（含 `--what-if <file>` 模擬 `_defaults.yaml` 變動 -> diff merged_hash）+ `da-tools migrate-conf-d`（扁平 -> 階層自動 `git mv`，預設 `--dry-run`）
- **Schema 新增**：`tenant-config.schema.json` 加入 `definitions/defaultsConfig` + `_metadata.$comment`

### 元件健壯化

- **Design Token 全面遷移**：9 個 Tier 1 JSX 工具完成 Tailwind -> arbitrary value token 改寫（`wizard` / `deployment-wizard` / `alert-timeline` / `dependency-graph` / `config-lint` / `rbac` / `cicd-setup-wizard` / `tenant-manager` / `multi-tenant-comparison`）；剩餘 7 個 px-only 工具延 v2.8.0
- **`[data-theme]` 單軌 dark mode**（ADR-015）：移除 Tailwind `dark:` 雙軌橋接，解決 v2.6.0 誤用陷阱
- **Component Health Snapshot**（ADR-013）：`scan_component_health.py` 五維評分（LOC / Audience / Phase / Writer / Recency）-> Tier 1 = 11 / Tier 2 = 25 / Tier 3 = 3；新增 `token_density` 量化 token 採用進度
- **Colorblind 合規**（ADR-012）：`threshold-heatmap` 結構化 severity（不只靠顏色）
- **TECH-DEBT 類別獨立 budget**：從 REG budget 分出，不佔 REG P2/P3 配額
- **新 lint**：`check_aria_references.py` / `axe_lite_static.py` / `check_design_token_usage.py`

### 測試與基礎設施

- **`tests/` 子目錄分層**：`dx/` / `ops/` / `lint/` / `shared/`，匹配 `scripts/tools/` 的分層
- **1000-tenant synthetic fixture**：`generate_synthetic_tenants.py` 產可重現的千租戶資料，供 B-1 Scale Gate 量測
- **Blast Radius CI bot**：PR 變更自動計算影響的 tenants / rules / thresholds，comment 到 PR
- **Pre-commit**：31 auto + 13 manual-stage；`make pre-tag` 整合 `version-check` + `lint-docs` + `playbook-freshness-ll`

### Benchmark（1000 tenants, Intel Core 7 240H, Go 1.26.1, `-benchtime=3s -count=3`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 2.45 ms | Dual-hash reload noop（45x 快於 cold） |
| `IncrementalLoad + MtimeGuard` | 1.30 ms | 加 mtime 短路（86x 快於 cold） |
| `MergePartialConfigs_1000` | 653 us | 階層 merge 本身 |

SLO：cold load 112 ms / 1000 tenants；reload 熱路徑 1.30 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md §3 v2.8.0 Scale Gate`](docs/benchmarks.md#3-v280-scale-gate-1000-tenant-實測)。

### ADR 新增（ADR-012~017，6 條）

colorblind 結構化 severity / component health + token_density / TECH-DEBT 獨立 budget / token 遷移策略 / 單軌 dark mode / `conf.d/` 階層 / `_defaults.yaml` 繼承引擎 + dual-hash 熱重載。

### Breaking changes

無。`conf.d/` 與繼承引擎為**新增能力**；舊扁平 `tenants/*.yaml` 完全向後相容，Schema 只新增不改動。

### Upgrade notes

- 既有使用者：不需變更
- 想採用 `conf.d/` 分層：見 `docs/scenarios/multi-domain-conf-layout.md` + `incremental-migration-playbook.md`，或 `da-tools migrate-conf-d --dry-run`
- 熱重載：dual-hash 預設啟用，debounce window 300ms 可用 `--scan-debounce` 調整

---

## [v2.6.0] — Operator 遷移路徑 × PR Write-back × 設計系統統一 (2026-04-07)

v2.6.0 的核心是「讓 enterprise 客戶能信賴地在 Operator 環境下運營」：建立完整的 ConfigMap → Operator 遷移工具鏈與對稱文件（ADR-008 addendum），引入 PR-based 非同步寫入支援 GitHub 與 GitLab 雙平台（ADR-011），統一設計系統消除三套平行 CSS 的技術債，並新增 4 個互動工具強化價值傳達。

### K8s Operator 完整遷移路徑

v2.3.0 引入的 Operator 指南是單一文件；v2.6.0 將其擴展為與 ConfigMap 路徑完全對稱的文件體系與工具鏈。

- **ADR-008 addendum**：正式記錄架構邊界宣言——threshold-exporter 不 watch 任何 CRD，CRD → conf.d/ 轉換由外部控制器或 CI 負責。含 Mermaid 邊界圖 + 三問判斷標準（ZH + EN 雙語）
- **`operator-generate` 大幅增強**：AlertmanagerConfig 6 種 receiver 模板（Slack, PagerDuty, Email, Teams, OpsGenie, Webhook），每種自動產出 `secretKeyRef` 引用 K8s Secret（零明文 credential）。新增 `--receiver-template`、`--secret-name`、`--secret-key` 參數
- **三態抑制規則 CRD 化**：Silent / Maintenance mode 自動包含在每個 AlertmanagerConfig 產出（4 條 inhibit rules）
- **Helm `rules.mode` toggle**：threshold-exporter chart 新增 `configmap | operator` 切換 + ServiceMonitor 條件模板，operator section 含 ruleLabels、serviceMonitor、receiverTemplate、secretRef
- **`da-tools migrate-to-operator`**（新增 CLI）：讀取現有 ConfigMap rules → 產出等效 CRD + 6 階段遷移清單（Discovery → Generate → Shadow → Compare → Switch → Cleanup）+ rollback 程序。`validate_tenant_name()` RFC 1123 驗證確保 CRD apply 不失敗
- **Operator Setup Wizard**（新增 JSX）：互動式偵測環境 → 選 CRD 類型 → 產出命令，每步驟含 contextual help + 常見陷阱提示
- **Kustomization.yaml 自動產生**：`operator-generate --kustomize` 產出標準格式，含 commonLabels + sorted resources + namespace
- **`drift_detect.py` Operator 模式**：`--mode operator` 透過 kubectl 取得 PrometheusRule CRD 的 spec.groups SHA-256，與本地 YAML 比對。kubectl timeout 30s + 三種錯誤處理
- **Decision Matrix**：提升到 Getting Started 層級，決策樹 + 10 維度比較表（ZH + EN）
- **文件對稱化**：`prometheus-operator-integration.md` 拆分為 4 組子文件（Prometheus / Alertmanager / GitOps / Shadow Monitoring）各含 ZH + EN 版本 + 2 hub 導航頁 = 10 篇新文件

### PR-based Write-back + 非同步 API

v2.5.0 的 tenant-api 只支援 direct write（API → YAML → git commit）。v2.6.0 新增 PR 模式與非同步批量操作，讓高安全環境能透過 code review 流程管理配置變更。

- **ADR-011**（新增 ADR，ZH + EN 雙語）：定調 PR lifecycle state model（pending / merged / conflicted）、GitHub PAT 權限與 Secret 管理策略、多 PR 合併衝突處理、eventual consistency 語義
- **PR-based write-back**：`_write_mode: direct | pr` 配置切換（`-write-mode` flag + `TA_WRITE_MODE` env）。UI 操作 → 建立 PR → reviewer 核准 → 合併。PR-mode API response 回傳 `pr_url` + `status: "pending_review"`
- **Batch PR 合併**：群組批量操作合併為單一 PR（非 N 個），減少 reviewer 負擔
- **Async batch operations**：`?async=true` query param 啟用非同步模式，回傳 `task_id` + `status: "pending"`。goroutine pool 執行，GET `/tasks/{id}` polling 查詢進度
- **Orphaned task 容錯**：Pod 重啟後 in-memory task state 遺失，GET `/tasks/{id}` 回傳 404 附帶 `pod_may_have_restarted` hint
- **SSE 即時通知**：`GET /api/v1/events` 端點，gitops.Writer 寫入成功後自動推播 `config_change` 事件。採用 Server-Sent Events 實作，零外部依賴
- **tenant-manager.jsx**：Pending PRs 提示 banner（頂部顯示待審核 PR 數量與連結，30s 輪詢）

### Platform Abstraction Layer + GitLab 支援

為使 PR write-back 成為平台無關的能力，抽取 platform interface 並新增 GitLab MR 支援。

- **`internal/platform/platform.go`**（新增）：`PRInfo` struct、`Client` interface（5 methods: CreateBranch / CreatePR / ListOpenPRs / ValidateToken / DeleteBranch）、`Tracker` interface（6 methods）。handler 只依賴 interface，provider 可替換
- **`internal/gitlab/`**（新增套件）：GitLab REST API v4 client，`PRIVATE-TOKEN` header 認證，`url.PathEscape` 支援含 `/` 的 `group/subgroup/project` 路徑。全部 5 個 `platform.Client` 方法 + 6 個 `platform.Tracker` 方法
- **Write mode 路由**：`--write-mode direct | pr | pr-github | pr-gitlab` 四種模式，`pr` 為 `pr-github` alias（向後相容）
- **On-Premise 支援**：GitHub Enterprise Server（`TA_GITHUB_API_URL`）+ 自託管 GitLab（`TA_GITLAB_API_URL`）。`SetBaseURL()` 已納入 `platform.Client` interface
- **Compile-time interface assertions**：`var _ platform.Client = (*Client)(nil)` + `var _ platform.Tracker = (*Tracker)(nil)` 確保型別安全
- **錯誤訊息衛生化**：`doRequest` 在 HTTP 4xx/5xx 時 log 完整 response body（debugging），回傳 caller 的 error 只含 status code（不洩漏 API body 給前端）。GitHub + GitLab 兩端一致
- **GitLab state 正規化**：`normalizeState()` 將 GitLab `opened` 映射為 `open`（與 GitHub 一致）
- **ListOpenPRs pagination**：per_page=100, 10 pages safety limit

### 設計系統統一

v2.5.0 暴露了三套平行 CSS 系統（CSS variables / Tailwind / inline styles）是所有無障礙問題的根源。v2.6.0 建立 design token SSOT 並全面遷移。

- **`docs/assets/design-tokens.css`**（新增）：統一 CSS variable 定義（11 個類別：color, spacing, typography, shadow, radius, transition 等），按 §1-§11 組織，命名規範 `--da-{category}-{element}-{modifier}`
- **Dark mode 三態切換**：`[data-theme="dark"]` attribute 取代 `@media (prefers-color-scheme: dark)`。Portal 加入 Light / Dark / System 三態切換按鈕，狀態存 localStorage（fallback: in-memory + cookie）
- **tenant-manager.jsx 遷移**：消除 454 行 hardcoded inline styles，全面切換至 CSS variables + Tailwind classes
- **focus-visible 全局化**：CSS 層統一實作，不再依賴各 JSX 檔案自行加入
- **index.html 統一**：legacy aliases（`var(--bg)`, `var(--muted)`）全面遷移至 `var(--da-*)` tokens
- **`docs/internal/design-system-guide.md`**（新增）：design token 命名規範、使用方式、`[data-theme]` 切換機制、Light/Dark/System 三態邏輯

### 價值傳達與互動工具

讓潛在使用者與現有客戶能快速量化平台的採用價值。

- **ROI Calculator 增強**：新增 Quick Estimate 模式（單一輸入即出結果）+ 完整三維計算（Rule Maintenance + Alert Storm + Time-to-Market）
- **Migration ROI Calculator**（新增 JSX）：輸入 PromQL 行數 / rules / tenants → coverage estimation + migration effort + break-even analysis
- **Cost Estimator**（新增 JSX, 827 lines）：tenants × packs × scrape interval × retention × HA replicas × deployment mode → Resource Summary + Monthly Cost + ConfigMap vs Operator 比較 + Quick Recommendation
- **Notification Template Editor**（大幅改版, 897 lines）：從 Previewer 升級為 Editor——可編輯 title/body 模板 + template variable autocomplete + validation（unmatched braces, char limits）+ live preview + export YAML/JSON + template gallery（Detailed/Compact/Bilingual presets）
- **architecture-and-design.md** 每個子主題加入 business impact 欄位（ZH + EN，O(M) vs O(N×M) 複雜度對比、Onboard 2hr→5min 等量化指標）
- **release-notes-generator.jsx** 新增 `generateAutoSummary()` 函式，CHANGELOG 角色分流自動摘要（per-role "What's new for you"）

### 測試與品質

- **Playwright axe-core 整合**：`@axe-core/playwright` 自動偵測 WCAG 違規，整合到既有 5 個 smoke tests + 新增 Operator Wizard 12 tests
- **Property-based testing**（新增 22 tests）：Hypothesis 覆蓋 tenant name RFC 1123 validation、SHA-256 hashing、drift detection symmetry、YAML round-trip、kustomization builder。`@settings(max_examples=100)` 確保覆蓋
- **Go `-race` 全通過**：Phase .e 發現並修復 async/taskmanager.go + ws/hub_test.go data race。`Get()` 改為回傳 deep copy snapshot 防止併發讀寫
- **大型 Python 工具重構**：`generate_alertmanager_routes.py`（1,474→1,645 lines，21 helpers extracted，>100 行函式 4→0）+ `init_project.py`（1,404→1,438 lines，6 helpers extracted）
- **aria-live regions**：tenant-manager.jsx 新增 4 個 region（sidebar, PRs banner, batch, tenant grid）+ threshold-heatmap.jsx 新增 3 個 region
- **Batch response summary**：tenant_batch.go + group_batch.go 回傳 `summary` 欄位（"N succeeded, M failed"）
- **version-consistency hook 擴展**：覆蓋 e2e/package.json、JSX 工具版號
- **tool-registry.yaml 對齊**：補齊 3 個缺失條目（rbac-setup-wizard, release-notes-generator, threshold-heatmap）

### 數字

| 項目 | v2.5.0 | v2.6.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 38 | 42 | +4 |
| ADRs | 10 | 11（+ ADR-011）+ ADR-008 addendum | +1 |
| Operator 子文件 | 1 | 10（4 ZH + 4 EN + 2 hub） | +9 |
| Go test packages（`-race` clean） | — | 11 packages, 0 race | NEW |
| Property-based tests (Hypothesis) | 0 | 22 | NEW |
| Helm chart features | — | `rules.mode` toggle + ServiceMonitor | NEW |
| Write-back 模式 | 1（direct） | 4（direct / pr / pr-github / pr-gitlab） | +3 |
| Platform providers | 0 | 2（GitHub + GitLab） | NEW |
| Python 工具 | 91 | 95 | +4 |
| Pre-commit hooks | 19 auto + 9 manual | 19 auto + 10 manual | +1 manual |
| 環境變數（tenant-api） | ~10 | ~18 | +8（Write-back + GitLab） |

### 🐛 Bug Fixes

- `migrate_to_operator.py`：`discover_tenant_configs()` 靜默過濾無效 tenant 名稱 → 改為回報至 `analysis["issues"]` 清單
- `tracker.go`：`RegisterPR()` 同 tenant 可能重複 append → 改為 replace-or-append 邏輯
- `migration-roi-calculator.jsx`：2 個 label 未翻譯
- index.html：light-mode `.journey-phase-badge` + `.card-icon` 殘留 hardcoded hex color → 全部改用 design tokens
- README.md / README.en.md：badge 版號 v2.5.0 → v2.6.0
- troubleshooting.en.md：缺少 Prometheus Operator 章節 → 新增完整診斷+修正步驟+Rollback 程序
- troubleshooting.md：Operator 章節僅有診斷 → 補充三種修正步驟 + Rollback 程序

---

## [v2.5.0] — Multi-Tenant Grouping × Saved Views × E2E Testing (2026-04-06)

v2.5.0 在 v2.4.0 建立的 Tenant API 基礎上，實現租戶分群管理（ADR-010）、Saved Views、Playwright E2E 測試基礎，並新增 4 個互動工具。

### Multi-Tenant Grouping（ADR-010）

- 新增 `conf.d/_groups.yaml` 儲存結構：靜態 `members[]` 成員清單，Git 版本化，可 code review
- Group CRUD API：`GET/PUT/DELETE /api/v1/groups/{id}` + `POST /api/v1/groups/{id}/batch` 批量操作
- Permission-filtered listing：ListGroups 只回傳使用者有權限存取至少一個成員的 group
- 批量操作逐 tenant 驗證寫入權限，部分失敗不影響已成功項目

### Saved Views API

- 新增 `conf.d/_views.yaml`：持久化篩選條件（environment + domain + 自訂 filter 組合）
- CRUD 端點：`GET/PUT/DELETE /api/v1/views/{id}`，支援使用者自建常用視圖
- 與 Portal tenant-manager 整合：一鍵切換預設篩選

### Tenant Metadata 擴展

- 新增可選欄位：`environment`、`region`、`domain`、`db_type`、`tags[]`、`groups[]`
- 全部向後相容——未設定 metadata 的 tenant 不受影響
- Metadata 僅 API/UI 層使用，不影響 Prometheus metric cardinality

### RBAC 增強

- `_rbac.yaml` 新增 `environments[]` 和 `domains[]` 可選過濾欄位
- 支援「特定 group 只能管理 production 環境」等細粒度控制

### 新增互動工具（34 → 38 JSX tools）

- **Deployment Profile Wizard** (`deployment-wizard.jsx`)：互動式 Helm values 產生器
- **RBAC Setup Wizard** (`rbac-setup-wizard.jsx`)：互動式 `_rbac.yaml` 產生
- **Release Notes Generator** (`release-notes-generator.jsx`)：從 CHANGELOG 自動產生角色導向更新摘要
- **Threshold Heatmap** (`threshold-heatmap.jsx`)：跨 tenant 閾值分佈熱力圖 + 離群偵測 + CSV 匯出

### Playwright E2E 測試基礎

- 5 個 critical path spec（38 個 test case）：portal-home、tenant-manager、group-management、auth-flow、batch-operations
- Mock API 隔離（無外部依賴）、GitHub Actions CI 整合
- `tests/e2e/playwright.config.ts` + `.github/workflows/playwright.yml`

### CI/CD 改進

- tenant-api Go 測試納入 CI pipeline（2,115 行測試程式碼）
- Release 流程強化：`make pre-tag` 閘門、`bump_docs.py` 新增 tenant-api 版號線

### 數字

| 項目 | v2.4.0 | v2.5.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 34 | 38 | +4 |
| ADRs | 9 | 10 | +1（ADR-010） |
| Playwright E2E specs | 0 | 5（38 test cases） | NEW |
| API 端點 | ~10 | ~16 | +6（groups + views） |

---

## [v2.4.0] — 防守深化 × 體質精簡 × 租戶管理 API (2026-04-05)

v2.4.0 的核心是「從能用到好管」：將 v2.3.0 release 暴露的手動痛點全面自動化（Phase A），對膨脹的核心檔案進行結構性重構（Phase B/B.5），引入 Tenant Management API 作為管理平面（Phase C），並重整 Playbook 體系（Phase D）。

### Phase A — 防守工具補強

將 v2.3.0 release 過程中手動發現的 6 類問題轉化為 pre-commit hook，auto hooks 從 13 個增至 19 個。

- **`check_build_completeness.py`**：`build.sh` ↔ `COMMAND_MAP` 雙向同步檢查，防止 Docker image 中工具遺漏
- **`check_bilingual_structure.py`**：ZH/EN 文件 heading hierarchy 骨架比對 + README 雙語導航對稱性
- **`check_jsx_i18n.py`**：`TOOL_META` ↔ `CUSTOM_FLOW_MAP` key set 一致性、`window.__t` 雙參數驗證
- **`check_makefile_targets.py`**：每個 `dx/generate_*.py` 和 `dx/sync_*.py` 工具被至少一個 Makefile target 引用
- **`check_metric_dictionary.py`**：`metric-dictionary.yaml` 與 Rule Pack YAML 交叉驗證，偵測 stale/undocumented entries
- **`check_cli_coverage.py` hook 化**：從測試升級為 pre-commit auto hook，cheat-sheet ↔ cli-reference ↔ COMMAND_MAP 三向一致
- **`_lint_helpers.py`**：抽取 `parse_command_map()`、`parse_build_sh_tools()`、`BUILD_EXEMPT` 等共用邏輯，消除 ~80 行重複

### Phase B — Go config.go 分拆 + 程式碼體質改善

- **config.go 拆分**（2,093 行 → 4 檔案）：`config_types.go`（268 行，型別定義）+ `config_parse.go`（277 行，YAML 解析）+ `config_resolve.go`（750 行，ResolveAt + 驗證）+ `config.go`（823 行，ConfigManager + 公開 API）
- 拆分為純結構移動，public API 語意不變，benchmark 差異 -0.3% ~ -5.0%（±5% 以內）
- **config_test.go table-driven 重構**：4,236 → 3,929 行（-7.2%），38 個重複 test function 收斂為 8 個 table-driven test，test function 總數 145 → 115
- Go 全部 145 測試通過，Python 3,657 passed / 44 skipped / 0 failed

### Phase B.5 — 文件與測試瘦身

Phase B 做到了「結構整理」，B.5 補做「內容精簡」。

- **合併 `context-diagram.md` → `architecture-and-design.md`**：~70% 重疊內容消除，淨刪 ~1,165 行，docs/ 檔案數 115 → 113
- **`incremental-migration-playbook.md` 瘦身**：1,165 行 → 575 行（-50.6%），冗長 JSON 範例改為摘要，手動 kubectl 序列改為 `da-tools` 命令
- **三態說明集中化**：`tenant-lifecycle.md` 的 60 行重複三態解釋改為 hyperlink + 3 行速查
- **版號全域修正**：44 處過時版號更新 + 文件計數修正
- 文件總計：docs/ -2,362 行（-6.4%），-2 個檔案

### Phase C — Tenant Management API（ADR-009）

新增 `components/tenant-api/` Go 元件，為 da-portal 加入 Backend API。

**架構決策（ADR-009）**
- API 語言選 Go：與 threshold-exporter 共用 `pkg/config` 解析邏輯，避免 Go↔Python 雙端維護
- 認證用 oauth2-proxy sidecar：API server 零 auth 程式碼，讀 `X-Forwarded-Email` / `X-Forwarded-Groups` header
- 寫回用 commit-on-write：UI 操作 → API → 修改 YAML → git commit（操作者名義），保留完整 audit trail
- RBAC 用 `_rbac.yaml` + `atomic.Value` 熱更新：lock-free 讀取，與 threshold-exporter reload 模式一致
- 不引入資料庫——Git repo 就是 database

**`pkg/config/` 抽取**
- 將 threshold-exporter 的型別與解析邏輯抽入 `components/threshold-exporter/app/pkg/config/`（`types.go` + `parse.go` + `resolve.go`）
- tenant-api 透過 `go.mod replace` directive 直接 import 共用型別

**API 端點**
- `GET /api/v1/tenants` — 租戶列表（支援 group/env 篩選）
- `GET/PUT /api/v1/tenants/{id}` — 單一租戶 CRUD
- `POST /api/v1/tenants/{id}/validate` — 乾跑驗證（不寫入）
- `POST /api/v1/tenants/batch` — 批量操作（`sync.Mutex` 同步，response 預留 `task_id`）
- `GET /api/v1/tenants/{id}/diff` — 預覽變更差異
- Health check / readiness probe / Prometheus metrics

**Portal 降級安全**：API 不可用時，tenant-manager.jsx 自動降級為 platform-data.json 唯讀模式。

**交付物**：Go binary + Docker image（distroless base）+ Helm chart + K8s manifests + 五線版號新增 `tenant-api/v*`

### Phase D — Playbook 重整 + 文件治理

- Playbook 結構化：testing-playbook 五段分層、benchmark-playbook 加入決策樹、windows-mcp-playbook 32 個 pitfall 分類索引
- `bump_docs.py` 自動計數功能：掃描並更新散落各處的工具數量、Rule Pack 數量等
- doc-map.md 自動生成預設包含 ADR

### 數字

| 項目 | v2.3.0 | v2.4.0 | 變化 |
|------|--------|--------|------|
| Pre-commit hooks | 13 auto + 7 manual | 19 auto + 9 manual | +6 auto, +2 manual |
| Go config.go | 2,093 行 × 1 檔 | 4 檔（268 + 277 + 750 + 823） | 結構拆分 |
| config_test.go | 4,236 行 / 145 函式 | 3,929 行 / 115 函式 | -7.2% / table-driven |
| docs/ 行數 | 37,059 | 34,697 | -2,362（-6.4%） |
| Components | 3 | 4（+ tenant-api） | +1 |
| ADRs | 8 | 9（+ ADR-009） | +1 |
| JSX 互動工具 | 29 | 34 | +5 |
| 版號線 | 4 | 5（+ tenant-api/v*） | +1 |
| Python 工具 | 84 | 91 | +7 |

---

## [v2.3.0] — Operator-Native × Management UI × Platform Maturity (2026-04-04)

v2.3.0 聚焦四大主題：Operator-Native 整合、Multi-Instance Management UI、Portal & Doc 成熟度、品質閘門升級。

### Phase .a — Portal & DX Foundation

**Self-Service Portal 模組化**
- `self-service-portal.jsx`（1,376 行）→ 5 個模組：`portal-shared.jsx`（共用常數/函式/元件）+ `YamlValidatorTab.jsx` + `AlertPreviewTab.jsx` + `RoutingTraceTab.jsx` + coordinator
- 新增 `dependencies` frontmatter 機制：jsx-loader.html 支援 YAML frontmatter 中宣告依賴，依序載入 → `loadDependency()` / `loadDependencies()` / `transformImports()`
- `window.__portalShared` 模式：共用模組透過全域變數註冊，tab 模組解構取用

**Template Gallery 外部化**
- 24 個模板 → `docs/assets/template-data.json`（雙語 `{zh, en}` 物件格式 + `category` 欄位）
- `template-gallery.jsx` 改為 `useEffect` fetch 載入，新增 loading/error 狀態
- 檔案大小：806 → 293 行（-64%）

**Portal Hub 五層重組**
- 29 個工具卡片從 2 區（Interactive / Advanced）→ 5 層級：Start Here、Day-to-Day、Explore & Learn、Simulate & Analyze、Platform Operations
- 新增 Quick Access 面板（5 個常用工具快捷連結）
- 每層級附色彩標籤（Onboarding / Core Workflow / Reference / What-If / Engineer）
- Role filter 同時作用於 Quick Access chips
- Tour 步驟更新、Footer 版號同步

**文件模板系統**
- 新增 `docs/internal/doc-template.md`：定義文件標準結構（frontmatter + 必要 section + Related Resources）
- 新增 `scripts/tools/lint/check_doc_template.py`：frontmatter 完整性 + Related Resources 存在性 + 版號一致性

**`_lib_python.py` 模組拆分**
- `_lib_python.py` → 4 個子模組：`_lib_constants.py`（守護值/常數）+ `_lib_io.py`（檔案 I/O）+ `_lib_validation.py`（驗證邏輯）+ `_lib_prometheus.py`（HTTP/Prometheus 查詢）
- 原檔保留為 re-export facade（向後相容，53 行）

**SAST Rule 7**
- 新增 `TestStderrRouting`：AST 掃描 `print("ERROR..."` / `print("Error..."` 確保附帶 `file=sys.stderr`
- 支援 literal string 和 f-string 兩種格式偵測

---

### Phase .b — Operator-Native + Federation

**ADR-008: Operator-Native Integration Path**
- 雙路整合架構決策：既有 ConfigMap 路徑保留，新增 Operator-Native 模式作為 BYO 方案
- 工具鏈適配而非平台重寫原則——threshold-exporter Go 核心語意不變
- 新增 `detectConfigSource()` 函式：逐級檢測 operator env var → git-sync `.git-revision` 文件 → configmap（預設）

**Prometheus Operator 整合指南**
- 新增 `docs/prometheus-operator-integration.md`（雙語 zh + en）：架構圖、CRD 對應、3 個部署場景（all-in-one / mixed / operator-only）
- BYO 文件清理：移除 Prometheus Operator appendices，改為重定向至新指南
- ServiceMonitor / PrometheusRule / AlertmanagerConfig CRD 映射表

**da-tools Operator 工具**
- **`da-tools operator-generate`** — 從 Rule Packs + Tenant 配置產生 PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML
  - 支援 `--namespace` / `--labels` / `--annotations` 自訂，`--output-format yaml | json`
  - 整合於 da-tools entrypoint + build.sh 打包
- **`da-tools operator-check`** — CRD 驗證工具：PrometheusRule 語法 + AlertmanagerConfig 路由合法性 + ServiceMonitor label 一致性
  - 支援 `--kubeconfig` / `--context` 直連 K8s 驗證，亦支援離線 YAML 驗證
  - Registered in CI lint pre-commit hooks

**Config Info Metric（四層感知）**
- 新增 `threshold_exporter_config_info{config_source, git_commit}` info metric
- 三種模式 + 自動偵測：
  - `configmap`（預設）：從 ConfigMap mount path 讀取 config version
  - `git-sync`：讀取 `.git-revision` 共享 volume 文件，提供 git commit SHA
  - `operator`：讀取 env var `CONFIG_SOURCE=operator` + `GIT_COMMIT=<sha>`
- `detectConfigSource()` 呼叫於 reload 時，確保 metric 實時反映部署形態

**Federation Scenario B（邊緣-中央分裂）**
- **`da-tools rule-pack-split`** — Rule Pack 聯邦分裂工具：
  - Part 1（正規化層）：邊緣側 metric value 驗證、單位轉換、異常值濾除 → 產生 Prometheus RecordingRules
  - Parts 2+3（閾值 + 警報層）：中央側聚合、cross-edge 關聯、全域告警決策 → 產生 Alerting Rules
  - 支援 `--operator` CRD 輸出 + `--gitops` 模式（目錄結構）
  - 關鍵特性：無狀態 split（idempotent）、邊緣 auto-healing（快照回滾）
- **`federation-integration.md` §8** — Scenario B 完整文件：三階段部署（邊緣佈建 → 中央策略 → 端對端驗證）、MTTR 優化、成本模型

**Go 單元測試（+12 tests，覆蓋率 87% → 94%）**
- WatchLoop 整合測試：無檔案變動 / 新增檔案 / 更新現有檔案
- `resolveConfigPath()` 三情案例：configmap flag / git-sync flag / 未設定（預設 configmap）
- `detectConfigSource()` 四情案例：configmap（預設）/ git-sync / operator / precedence（operator > git-sync > configmap）
- Config Info metric 收集器三情案例：各模式 value 驗證 + label 正確性
- Fail-Safe Reload E2E：config 不可讀時 fallback 邏輯

---

### Phase .c — Management UI + Intelligence

**Tenant Manager Data Foundation**
- 新增 `scripts/tools/dx/generate_tenant_metadata.py`：從 `conf.d/` 目錄結構推斷租戶 metadata
  - Rule Pack 推斷：根據 YAML 中 metric prefix 比對 Rule Pack 定義
  - 運營模式推斷：`_silent_mode` / `_state_maintenance` 標誌偵測
  - 路由通道推斷：`_routing` 配置解析
- 擴展 `scripts/tools/dx/generate_platform_data.py`：產出的 `platform-data.json` 新增 `tenant_groups` + `tenant_metadata` 結構
- Tenant metadata 版本化：支援 `--output-dir` 自訂輸出路徑，方便 GitOps 集成

**Tenant Manager UI 元件**
- 新增 `docs/interactive/tools/tenant-manager.jsx`（~650 行）：
  - 響應式卡片牆佈局，環境/層級徽章（dev/staging/prod + app/infra/platform）
  - 運營模式指示器：Normal / Silent / Maintenance 視覺標記 + expires 倒數
  - 批量操作：批次維護/靜默模式 YAML 產生器，支援日期範圍選擇
  - 篩選+搜尋：按環境/層級/模式多維度過濾，模糊搜尋租戶名
- 加入 `tool-registry.yaml` + Portal Hub Tier 1 (Day-to-Day 層級)

**閾值推薦 × Portal 智慧**
- 新增 `docs/assets/recommendation-data.json`：15 個核心指標的 P50/P95/P99 預計算資料
  - 資料來源：歷史基線 + 業界最佳實踐
  - 格式：`{metric_name: {p50, p95, p99, source, last_updated}}`
- 擴展 `docs/interactive/tools/AlertPreviewTab.jsx`：
  - Progress bar 上疊加 recommended value marker 視覺指示
  - Confidence badge（high/medium/low）顯示推薦可信度
  - 新增 "Apply Recommended Values" 按鈕，一鍵生成更新 YAML

**OPA/Rego 策略整合**
- 新增 `scripts/tools/ops/policy_opa_bridge.py`（~450 行）：tenant YAML → OPA input JSON 轉換 + 雙模式評估
  - 轉換函式：YAML 欄位 → OPA JSON 輸入格式映射（支援 nested policies）
  - 評估模式：REST API 模式（連接遠端 OPA 伺服器）+ 本地 opa binary 模式
  - 違規輸出格式轉換：OPA violations → da-tools 標準格式（location + description）
- `scripts/policies/examples/` 新增三個 Rego 範例策略：
  - `routing-compliance.rego`：路由規則命名 / receiver type / group_wait 範圍 validation
  - `threshold-bounds.rego`：閾值範圍檢查 / 關鍵指標預留冗餘
  - `naming-convention.rego`：租戶/告警 ID 命名規範 + Prefix 合法性
- 登記為 `da-tools opa-evaluate` 子命令 + CI lint 整合

**Portal i18n Lint 工具**
- 新增 `scripts/tools/lint/check_portal_i18n.py`（~250 行）：掃描 JSX 檔案尋找硬編碼字串
  - AST 解析：偵測 string literal 未用 `window.__t()` 包裝的情況
  - 支援 `--fix-mode`：自動生成修復建議（帶位置資訊）
  - 排除清單：URL / 特殊字元序列 / i18n 函式呼叫內部字串
- 加入 pre-commit manual-stage hooks 為 `check-portal-i18n`

---

### Phase .d — Quality Gate + CI Maturity

**GitHub Actions CI Matrix**
- 新增 `.github/workflows/ci.yml`：Python 3.10/3.13 × Go 1.22/1.26 矩陣（4 × 2 = 8 組合）
- 4 個主 jobs：lint（文件+工具格式）、python-tests（pytest + coverage）、go-tests（threshold-exporter）、lint-docs（SAST + doc 品質）
- pip/Go module 緩存策略、coverage artifacts 產生、失敗時自動 debug log 產出

**Coverage Gate 強制**
- `pyproject.toml` 新增 `fail_under = 85`，CI 強制 `--cov-fail-under=85` 執行
- README.md 新增 CI badge 與 coverage badge（green ≥85%、yellow 80–85%、red <80%）
- Python 工具預期整體覆蓋率 ≥85%

**Python 型別系統加強**
- `_lib_constants.py`、`_lib_io.py`、`_lib_validation.py`、`_lib_prometheus.py` 加入完整型別提示
- 新增 `mypy.ini`：strict mode for all `_lib_*` modules、relaxed mode for test files
- CI lint job 新增 `mypy scripts/tools/_lib_*.py --config-file=scripts/tools/mypy.ini` 步驟

**Integration + Snapshot 測試**
- `tests/test_tool_exit_codes.py`（parametrized）：全部 84+ 工具的 `--help` + invalid args exit code 合約測試
- `tests/test_pipeline_integration.py`：scaffold → validate → routes 完整 pipeline 端對端測試
- `tests/test_snapshot.py`：help output stability snapshot tests，支援 `--snapshot-update` CI 模式

**Pre-commit Hook 驗證確認**
- 確認 13 個 auto-run hooks + 7 個 manual-stage hooks 全部運作，Phase .a–.c 新增項目完全涵蓋
- `make pre-commit-audit` 新增 make 目標印出 hook 清單與觸發規則

---

## [v2.2.0] — 採用管線 + UX 升級 + 運維工具 (2026-03-17)

v2.2.0 聚焦三大主題：降低採用門檻的 Adoption Pipeline、Portal 互動體驗全面升級、配置運維新工具。新增 2 個 CLI 工具、3 個互動工具、Portal 三大 Tab 重構、24 個 Template Gallery 模板、5-tenant 展演腳本與 Hands-on Lab。

### 採用管線（Phase A — Adoption Pipeline）

- **`da-tools init`** — 專案骨架一鍵產生：CI/CD pipeline（GitHub Actions / GitLab CI）、`conf.d/` 目錄（含 `_defaults.yaml` + tenant YAML）、Kustomize overlays、`.pre-commit-config.da.yaml`，支援 `--non-interactive` 自動模式
- **GitOps CI/CD 整合指南** (`docs/scenarios/gitops-ci-integration.md`) — 三階段管線（Validate → Generate → Apply）、ArgoCD / Flux 整合、PR Comment Bot 工作流
- **Kustomize Overlays** — `configMapGenerator` 模式產生 threshold-config ConfigMap

### UX 升級（Phase B — Portal & Templates）

**Self-Service Portal 重構（3 Tab）**
- **Tab 1 (YAML Validation)**: Rule Pack 多選 → metric autocomplete → 動態 sample YAML 產生 → 即時驗證（含 pack-aware metric key 交叉檢查）
- **Tab 2 (Alert Preview)**: Pack-grouped 滑桿、視覺化閾值條、disabled/no-threshold 狀態顯示、severity dedup 說明
- **Tab 3 (Routing Trace)**: Metric+severity 輸入 → Alert origin → Inhibit check → 四層合併 → Domain Policy check → 通知派送 → NOC 副本

**Template Gallery 擴充（6 → 24 模板）**
- 7 場景模板：ecommerce、iot-pipeline、saas-backend、analytics、enterprise-db、event-driven、search-platform
- 13 Quick Start 模板：每個可選 Rule Pack 各一
- 4 特殊模板：maintenance、routing-profile、finance-compliance、minimal
- View mode 切換（All / Scenarios / Quick Start）+ Pack filter chips + Coverage summary

**新增互動工具**
- **CI/CD Setup Wizard** (`cicd-setup-wizard.jsx`) — 5 步精靈產生 `da-tools init` 命令：CI Platform → Deploy Mode → Rule Packs → Tenants → Review & Generate（第 27 個 JSX 工具）
- **Notification Template Previewer** (`notification-previewer.jsx`) — 6 種 receiver 通知預覽（Slack / Email / PagerDuty / Webhook / Teams / Rocket.Chat）+ Dual-Perspective annotation 展示 + Severity Dedup 說明（第 28 個）
- **Platform Health Dashboard** (`platform-health.jsx`) — 平台健康儀表板：元件狀態、租戶概覽、Rule Pack 使用分佈、Reload 事件時間線（第 29 個）

**展演與教學**
- **Demo Showcase** (`scripts/demo-showcase.sh`) — 5-tenant 完整展演腳本（prod-mariadb / prod-redis / prod-kafka / staging-pg / prod-oracle），7 步驟自動執行，支援 `--quick` 模式
- **Hands-on Lab** (`docs/scenarios/hands-on-lab.md`) — 30–45 分鐘 Docker-based 實戰教程，8 個練習覆蓋 init → validate → routes → routing trace → blast radius → three-state → domain policy

### 運維工具（Phase C — Operations）

- **`da-tools config-history`** — 配置快照與歷史追蹤：`snapshot` / `log` / `show` / `diff` 子命令，`.da-history/` 存儲，SHA-256 變更偵測，git-independent 輕量級版本控制

### 漸進式遷移 Playbook

- **`docs/scenarios/incremental-migration-playbook.md`** — 四階段雙軌並行遷移法（Strangler Fig Pattern）：Phase 0 Audit（`onboard` + `blind-spot`）→ Phase 1 Pilot（單一 domain 影子部署）→ Phase 2 Dual-Run（`shadow-verify` 品質比對）→ Phase 3 Cutover（逐 domain 切換）→ Phase 4 Cleanup。每步有 CLI 指令、預期輸出、回退方式
- **`architecture-and-design.md` §2.13** — 新增效能架構說明：Pre-computed Recording Rule vs Runtime Aggregation 的 PromQL 對比，解釋為什麼 tenant 增加不會導致 Prometheus CPU/Memory 暴增

### GitOps Native Mode

- **`da-tools init --config-source git`** — 產生 git-sync sidecar Kustomize overlay，threshold-exporter 直接從 Git 倉庫讀取配置，省去 ConfigMap 中間層。支援 SSH / HTTPS 認證、自訂分支與路徑。git-sync sidecar 寫入 emptyDir shared volume，threshold-exporter 的既有 Directory Scanner + SHA-256 hot-reload 機制無縫復用
- **`da-tools gitops-check`** — GitOps Native Mode 就緒度驗證工具，三個子命令：`repo`（Git 倉庫可達性 + 分支驗證）、`local`（本地 conf.d/ 結構驗證）、`sidecar`（K8s git-sync 部署狀態檢查），支援 `--json` 和 `--ci` 模式
- **Container Image Security Hardening** — 三層防護：base pin + build-time upgrade + attack surface reduction
  - threshold-exporter：`alpine` → `distroless/static-debian12:nonroot`（零 CVE，無 shell/apk/openssl）
  - da-tools：`python:3.13-alpine` → `python:3.13.3-alpine3.22` multi-stage build（修復 CVE-2025-48174, CVE-2025-15467）
  - da-portal：`nginx:1.28-alpine` → `nginx:1.28.0-alpine3.22` + `apk del libavif gd libxml2`（移除未使用 library，消除掃描器 false positive）

### 數字

| 項目 | v2.1.0 | v2.2.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 73 | 77 | +4 |
| da-tools CLI 命令 | 27 | 36 | +9 |
| JSX 互動工具 | 26 (+1 wizard) | 29 | +3 |
| Template Gallery 模板 | 6 | 24 | +18 |
| 場景文件 | 6 | 9 | +3 |
| Makefile targets | — | +1 (`demo-showcase`) | NEW |

---

## [v2.1.0] — 運維自助 + 告警智能化 + 性能優化 + 跨域路由 (2026-03-16)

v2.1.0 自 v2.0.0 起的全量升級。涵蓋 Go Exporter 增量熱載入、告警關聯分析、跨域路由架構 (ADR-006/007)、生態整合 (Backstage Plugin)、5 個新 CLI 工具、3 個互動工具、測試 +75%（1,759 → 3,070）、文件治理與正確性全面校正。

### Go Exporter 核心

**Incremental Hot-Reload (§5.6)**
- per-file SHA-256 index + parsed config cache，WatchLoop 增量重載路徑
- `ConfigManager` 新增 `fileHashes` / `fileConfigs` / `fileMtimes` 欄位
- `scanDirFileHashes()` — mtime guard + 輕量 hash check（mtime 未變直接跳過 I/O）
- `IncrementalLoad()` — 比對 per-file hash → 只重新解析 changed/added files → `mergePartialConfigs()`
- `fullDirLoad()` — 完整載入並初始化 cache（首次載入或 fallback）
- `applyBoundaryRules()` — 提取為獨立函式供共用
- **效能優化**：logConfigStats 取代 Resolve()、mtime guard、incremental merge（tenant 檔變動直接 patch）、byte cache（scan 快取復用，免除重複 I/O）
- 15 個 Go tests + 5 個 benchmarks（含 NoChange / OneFileChanged / ScanHashes / MergePartials）

**程式碼品質**
- 4 處 error print 修正為 `stderr` 輸出
- `parsePromDuration` / `isDisabled` / `clampDuration` 新增單元測試
- Go test 增加 config_test.go（801 行）+ config_bench_test.go（268 行）+ main_test.go（97 行）

### 跨域路由架構（ADR-006 + ADR-007）

**ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)**
- 資料面映射方案：Prometheus Recording Rules 實現 1:N 映射（exporter 零修改）
- `generate_tenant_mapping_rules.py` — 讀取 `_instance_mapping.yaml`，產出 Recording Rules（36 tests）
- `scaffold_tenant.py` 新增 `--topology=1:N`、`--mapping-instance`、`--mapping-filter` 參數（9 tests）
- 範例設定檔 `_instance_mapping.yaml`

**ADR-007: Cross-Domain Routing Profiles**
- 四層合併管線：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`
- `generate_alertmanager_routes.py` 擴展：profile 解析 + `check_domain_policies()` 驗證（21 tests）
- `scaffold_tenant.py` 新增 `--routing-profile` 參數
- 重構 `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` 子函式
- 範例設定檔 `_routing_profiles.yaml`、`_domain_policy.yaml`

**ADR-007 工具生態**
- `explain_route.py` — 路由合併管線除錯器：四層展開、`--show-profile-expansion`、`--json`、da-tools CLI 整合（25 tests）
- `check_routing_profiles.py` — CI lint 工具：未知 profile ref、孤立 profile、格式錯誤 constraints、`--strict` 模式（28 tests + pre-commit hook）

### 新增 CLI 工具

- **`da-tools test-notification`** — 6 種 receiver 連通性測試（webhook/slack/email/teams/pagerduty/rocketchat），Dry-run / CI gate / per-tenant 批次。57 tests，97% 覆蓋率
- **`da-tools threshold-recommend`** — 基於歷史 P50/P95/P99 的閾值推薦引擎，純 Python 統計，信心等級分級。54 tests，96% 覆蓋率
- **`da-tools alert-correlate`** — 告警關聯分析：時間窗口聚類 + 關聯分數 + 根因推斷，支援線上/離線模式。95% 覆蓋率
- **`da-tools drift-detect`** — 跨叢集配置漂移偵測：SHA-256 manifest 比對，pairwise 多目錄分析 + 修復建議。99% 覆蓋率
- **`da-tools explain-route`** — 路由合併管線除錯器（ADR-007），25 tests

### 生態整合

- **Backstage Plugin**：`components/backstage-plugin/` TypeScript/React plugin
  - `DynamicAlertingPage` + `DynamicAlertingEntityContent`
  - `PrometheusClient` API 層：via Backstage proxy 查詢 threshold / silent_mode / ALERTS
  - Entity 整合：`dynamic-alerting.io/tenant` annotation → 自動對應租戶

### 互動工具

- **Multi-Tenant Comparison** (`multi-tenant-comparison.jsx`)：Heatmap 色彩矩陣 + Outlier detection + Divergence Ranking（第 25 個 JSX 工具）
- **Alert Noise Analyzer** (`alert-noise-analyzer.jsx`)：MTTR 計算、震盪偵測、去重空間分析、Top noisy alerts（第 26 個）
- **ROI Calculator** (`roi-calculator.jsx`)：Rule 維護 / Alert Storm / Time-to-Market 三模型成本分析（第 27 個）

### DX Tooling

- **`check_frontmatter_versions.py`** — frontmatter version 全域掃描 + `--fix` 自動修復（29 tests）
- **`coverage_gap_analysis.py`** — per-file 覆蓋率排行報表（22 tests）
- **`check_bilingual_content.py`** — 雙語內容 CJK 比例 lint
- **`check_doc_links.py`** — 跨語言對應檔案驗證
- **`validate_all.py`** 增強：`--notify`（桌面通知）、`--diff-report`（CI 失敗自動 diff）
- **`generate_rule_pack_stats.py --format summary`** — Badge 風格單行輸出
- **Snapshot tests v2** — alert_correlate、drift_detect、bilingual_content 快照測試

### 安全加固

- SAST 規則擴充：6 rules 自動掃描（encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions），189 patterns
- NetworkPolicy 精細化、container security context 強化
- 憑證掃描 + `.env` 防護 + `os.chmod 0o600` 補齊
- **CVE 緩解**：CVE-2025-15467 (openssl CVSS 9.8 pre-auth RCE) + CVE-2025-48174 (libavif buffer overflow)
  - 所有 Dockerfile 加入 `apk --no-cache upgrade` 拉取安全修補
  - `da-tools` base image pin 從 `python:3.13-alpine` → `python:3.13.2-alpine3.21`
- **CI Image Scanning**：release workflow 三個 image 均加入 Trivy 掃描（CRITICAL + HIGH 阻斷）

### 品質閘門

- Pre-commit hooks：12 → **13** 個 auto-run（新增 `routing-profiles-check`）
- `build.sh` 修補：新增遺漏的 `alert_correlate`、`notification_tester`、`threshold_recommend` 打包

### 測試覆蓋率

Python 測試總數從 v2.0.0 的 1,759 提升至 **3,070**（+75%）。v2.1.0 新增工具均達 95%+ 覆蓋率，5 個既有工具從 41–74% 提升至 63–99%。Coverage gate 維持 `fail_under=64`，實際整體覆蓋率高於此基線。

### 數字

| 項目 | v2.0.0 | v2.1.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 62 | 73 | +11 |
| da-tools CLI 命令 | 23 | 27 | +4 |
| JSX 互動工具 | 24 | 26 (+1 wizard) | +3 |
| ADRs | 5 | 7（006/007 Accepted） | +2 |
| Python 測試 | 1,759 | 3,070 | +1,311 |
| Pre-commit hooks | 12 + 5 manual | 13 + 5 manual | +1 |
| Go tests (new files) | — | +3 files (1,166 lines) | NEW |

### Benchmark — Incremental Hot-Reload（Go, `-count=3` median）

| Benchmark | ns/op | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| IncrementalLoad_NoChange_10 | 165,700 | 34,272 | 176 |
| IncrementalLoad_NoChange_1000 | 1,528,000 | 2,027,264 | 13,085 |
| IncrementalLoad_OneFileChanged_10 | 220,600 | 73,280 | 241 |
| IncrementalLoad_OneFileChanged_1000 | 6,908,000 | 6,652,880 | 22,211 |
| ScanDirFileHashes_1000 | 1,206,000 | 1,985,200 | 13,012 |

### 文件治理與正確性

**Root README (zh/en) 增強**
- 開頭改為問題導向定位（規則膨脹 + 變更瓶頸），新增「適用場景」聲明與版本 badge
- 「關鍵設計決策」表新增 ADR 連結欄 + Sentinel 三態控制、四層路由合併兩行
- Quick Start 下方新增「生產部署」指引，文件導覽新增 Day-2 Operations 路徑

**ADR 生命週期更新**
- ADR-006/007：`📋 Proposed` → `✅ Accepted (v2.1.0)`，checklist 改為實作摘要 + 後續方向
- ADR-004：「現況與後續方向」替代舊 Roadmap 段落
- ADR-001/003：新增 v2.1.0 living-doc 狀態行
- ADR-002/004：新增「相關決策」交叉引用（ADR-005/006）

**architecture-and-design.md**
- 新增 §2.12 Routing Profiles 與 Domain Policies（四層合併管線 Mermaid 圖）
- 「本文涵蓋內容」補上三態模式、Dedup、路由系統
- 拆分文件導覽表移除過時 §N 前綴
- ADR-006 工具引用、Rule Pack 數量修正、雙語 annotation 章節翻譯

**benchmarks.md 重構**
- §8（Alertmanager Idle-State）合併至 §10（Under-Load）作為 baseline 比較表
- §13（pytest-benchmark）去重：移除與 §7 重複的 route generation 行
- 傳統方案估算加註推算基礎（per-rule ~0.3ms / ~60KB）
- 自引用修正、相關資源連結格式修正

**docs/README.md (zh/en) 去重**
- 移除與 root README 重複的「工具速查」22 行表 → 精簡為摘要 + 連結
- 移除重複的「快速命令」和「版本與維護」段落

**Component README 修正**
- threshold-exporter：斷裂 §11.1 引用 → 指向 `gitops-deployment.md`
- da-tools：版號表 `v2.0.0` → `v2.1.0`，移除過時措辭
- da-portal：`24 JSX tools` → `26`，image tag `v2.0.0` → `v2.1.0`
- backstage-plugin：移除不存在的 `(§5.13)` 引用

**交叉引用修正**
- ADR-006 (zh/en)：`§2.6` → `§2.3`（Tenant-Namespace 映射模式）
- ADR README (zh/en)：ADR-006/007 badge 更新為 ✅ Accepted

### 🐛 Bug Fixes

- 修復 `entrypoint.py` help text 遺漏 `validate-config` 命令
- 4 處 Python error output 修正為 stderr
- `da-tools build.sh` TOOL_FILES 補齊遺漏工具

---

## [v2.0.0] — Alert Intelligence + Full-Stack DX Overhaul (2026-03-15)

v2.0.0 正式版。自 v1.11.0 起的全量升級：76 個 commits、346 個檔案變更（+73,057 / -12,023）。涵蓋 Go Exporter 增強、Rule Pack 擴展、告警智能化、互動工具生態、文件全面重構、測試工程化、專案結構正規化。

> **版號說明**：v1.12.0 / v1.13.0 / v2.0.0-preview 系列皆為開發中版本（無 Git tag / GitHub Release），統一於 `v2.6.0` 正式釋出。

### 🔧 Go Exporter 增強

**Tenant Profiles（四層繼承）**
- Go schema 新增 `Profiles map[string]map[string]ScheduledValue` 欄位
- `applyProfiles()` fill-in pattern：Load 階段展開 profile 至 tenant overrides（僅填入未設定的 key）
- `_profiles.yaml` boundary enforcement：LoadDir 限制 profiles 只能從該檔載入
- `ValidateTenantKeys()` 擴展：`_profile` 引用不存在的 profile → WARN
- 繼承順序：Global Defaults → Profile → Tenant Override（tenant 永遠勝出）
- 13 個新 Go 測試案例

**Dual-Perspective Annotation**
- `platform_summary` annotation：Alert 同時攜帶 Platform 視角（NOC）和 Tenant 視角 summary
- 與 `_routing_enforced` 整合：NOC 收到 `platform_summary`，tenant 收到原始 `summary`

### 📦 Rule Pack 擴展（13 → 15）

- **JVM Rule Pack** (`rule-pack-jvm.yaml`)：GC pause rate、heap memory usage、thread pool — 7 alert rules（含 composite `JVMPerformanceDegraded`）
- **Nginx Rule Pack** (`rule-pack-nginx.yaml`)：active connections、request rate、connection backlog — 6 alert rules
- Projected Volume 13 → 15 ConfigMap sources，scaffold_tenant / metric-dictionary 同步更新

### 🚀 告警智能化（3 個新工具 + 1 個 Self-Service Portal）

**Alert Quality Scoring (`da-tools alert-quality`)**
- 4 項品質指標：Noise（震盪偵測）、Stale（閒置 14 天）、Resolution Latency（flapping 警告）、Suppression Ratio
- 三級評分（GOOD/WARN/BAD）+ per-tenant 加權分數（0–100）
- 輸出：text / `--json` / `--markdown`，CI gate：`--ci --min-score 60`
- 57 個測試，89.8% 覆蓋率

**Policy-as-Code (`da-tools evaluate-policy`)**
- 宣告式 DSL：10 種運算子（required / forbidden / gte / lte / matches / one_of ...）
- `when` 條件式、萬用字元目標（`*_cpu`）、dot-path 嵌套（`_routing.receiver.type`）
- Duration 比較、tenant 排除、error/warning 雙嚴重度
- CI gate：`--ci` 有 error 違規 exit 1
- 106 個測試，94.0% 覆蓋率

**Cardinality Forecasting (`da-tools cardinality-forecast`)**
- 純 Python 線性回歸（無 numpy）：趨勢分類（growing/stable/declining）+ 風險等級（critical/warning/safe）
- 觸頂天數預測 + 預計日期，可設基數上限（`--limit`）和預警天數（`--warn-days`）
- CI gate：`--ci` 有 critical 風險 exit 1
- 61 個測試，93.5% 覆蓋率

**Tenant Self-Service Portal (`self-service-portal.jsx`)**
- 三分頁 SPA：YAML 驗證（schema + routing guardrails）、告警預覽（滑桿模擬）、路由視覺化（樹狀圖）
- 瀏覽器端執行，零後端依賴，雙語支援（zh/en）

**Self-Hosted Portal (`da-portal` Docker image)**
- `ghcr.io/vencil/da-portal` — nginx:alpine 靜態 image，打包 24 JSX tools + Hub + Guided Flows + vendor JS
- 企業內網 / air-gapped 部署：`docker run -p 8080:80`，免 build step
- Volume mount 客製化：`platform-data.json`、`flows.json`、`nginx.conf`（含 Prometheus reverse proxy placeholder 解決 CORS）
- CI/CD：`portal/v*` tag 觸發 `release.yaml` 自動 build + push GHCR

### 🛠️ DX 自動化工具（+8 個新工具）

**Operations**
- **`shadow_verify.py`**：Shadow Monitoring 三階段驗證（preflight / runtime / convergence）
- **`byo_check.py`**：BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq）
- **`grafana_import.py`**：Grafana Dashboard ConfigMap 匯入（sidecar 掛載 + verify + dry-run）
- **`federation_check.py`**：多叢集 Federation 整合驗證（edge / central / e2e 三模式）

**Scalable Configuration Governance**
- **`assemble_config_dir.py`**：Sharded GitOps 組裝工具 — 多來源 conf.d/ 合併、SHA-256 衝突偵測、assembly manifest
- **`da_assembler.py`**：ThresholdConfig CRD → YAML 輕量 controller（Watch / One-shot / 離線渲染 / Dry-run）
- **ThresholdConfig CRD**（`dynamicalerting.io/v1alpha1`）：namespace-scoped + RBAC + printer columns

**DX 工具迭代**
- `validate_all.py`：`--profile` + `--watch`（CSV timing trend）、`--smart`（git diff → affected-check 自動跳過）
- `bump_docs.py`：`--what-if`（全 238 rules 審計）
- `generate_cheat_sheet.py` / `generate_rule_pack_stats.py`：`--lang zh/en/all` 雙語
- `check_doc_freshness.py`：false-positive 修正 + `--fix`
- `check_translation.py`：cross-dir + lang fix
- `check_includes_sync.py`：`--fix`（自動建立缺失 .en.md stub）

### 🎯 互動工具生態（0 → 24 JSX tools）

**工具矩陣**：23 個位於 `docs/interactive/tools/` + 1 個 `docs/getting-started/wizard.jsx`
- Config：Playground、Lint、Diff、Schema Explorer、Template Gallery
- Rule Pack：Selector、Matrix、Detail、PromQL Tester
- 運維：Alert Simulator/Timeline、Health Dashboard、Capacity Planner、Threshold Calculator
- 學習：Architecture Quiz、Glossary、Dependency Graph、Runbook Viewer、Onboarding Checklist
- 展示：Platform Demo、Migration Simulator、CLI Playground、Self-Service Portal

**基礎設施**
- **tool-registry.yaml**（單一真相源）→ `sync_tool_registry.py`（`make sync-tools`）自動同步 Hub 卡片 + TOOL_META + JSX frontmatter
- **platform-data.json**（共用資料源）：從 Rule Pack YAML 萃取（15 packs, 139R + 99A），JSX 工具 fetch 共用
- **jsx-loader.html**：瀏覽器端 JSX transpiler + `TOOL_META`（related footer）+ `__PLATFORM_DATA` 預載 + Guided Flow 模式
- **tool-consistency-check**（pre-commit）：Registry ↔ Hub ↔ TOOL_META ↔ JSX ↔ MD 一致性驗證

**Guided Flows**
- `flows.json` 多步引導流程（onboarding / tenant-setup / alert-deep-dive），`?flow=onboarding` 啟動
- Cross-step data（`__FLOW_STATE` + sessionStorage）、progress persistence、completion tracking
- Conditional steps + checkpoint validation（`__checkFlowGate()` Next 按鈕閘門）
- Custom flow builder：`?flow=custom&tools=...` Hub 互動式 builder，24 工具全覆蓋
- Flow analytics：進度條、完成率、drop-off 步驟偵測

### 🌐 Bilingual Annotations (i18n)

- **Rule Pack 雙語 annotation**：`summary_zh` / `description_zh` / `platform_summary_zh` — 三個 Pilot Pack（MariaDB, PostgreSQL, Kubernetes）
- **Alertmanager template fallback**：Go `or` function 優先中文、自動 fallback 英文（所有 receiver 類型）
- **CLI i18n**：`detect_cli_lang()` 偵測 `DA_LANG`/`LANG` → argparse help 雙語切換（23 個 CLI 命令）
- **check_bilingual_annotations.py**：Rule Pack 雙語覆蓋率驗證（pre-commit manual stage）

### 📄 文件全面重構

**結構重組**
- architecture-and-design.md 拆分為 6 個專題文件（benchmarks / governance-security / troubleshooting / migration-engine / federation-integration / byo-prometheus-integration）
- 3 個角色入門指南（for-platform-engineers / for-domain-experts / for-tenants）zh/en
- 全面雙語化：33 → 46 對 `.en.md` 文件
- MkDocs Material 站點：CJK 搜尋、tags、i18n 切換、abbreviation tooltips
- Glossary（30+ 術語）+ 5 ADRs + JSON Schema（VS Code 自動補全）

**內容修訂**
- 根 README (zh/en) 重寫：角色導向痛點敘事（Platform / Tenant / Domain / Enterprise）
- architecture-and-design.en.md：補 §2.3 Tenant-Namespace Mapping、修 §3.1（15 packs + `prometheus-rules-*` 命名）、補 Bilingual Annotations
- Benchmarks 重寫：5 輪實測數據統一採集（idle + under-load + routing + alertmanager + reload）
- 6 份文件精簡（avg -23%）：移除過時內容、手動 curl 改為 da-tools CLI 引用
- Scenario CLI 修正：`tenant-lifecycle.md` (zh/en) 修正 4 個不存在的 CLI flags
- Tool-map 重生成：62 個工具完整覆蓋（之前僅 18 個）

**文件 CI 工具鏈（13 tools）**
- `validate_mermaid.py` / `check_doc_links.py` / `check_doc_freshness.py` / `doc_coverage.py`
- `add_frontmatter.py` / `doc_impact.py` / `check_translation.py` / `check_includes_sync.py`
- `sync_glossary_abbr.py` / `sync_schema.py` / `generate_cheat_sheet.py` / `inject_related_docs.py`
- `validate_all.py`：統一驗證入口

### 🔒 Security Audit & Hardening

- **程式碼安全**：ReDoS 防護（regex 長度限制）、URL 注入白名單、SSRF scheme 白名單（http/https only）、Prototype pollution 過濾（`__proto__`/`constructor`）、YAML 100KB 上限、`os.chmod` 補齊
- **文件安全加固**：HTTP→HTTPS 範例、webhook 驗證升為 error、`--web.enable-lifecycle` 安全註解、Grafana 密碼警告、新增「生產環境安全加固」章節

### 🏗️ 專案結構正規化

- **scripts/tools/ 三層子目錄化**：62 個工具分入 `ops/`（30）、`dx/`（18）、`lint/`（13）+ root（1 + 1 lib）
  * Docker flat layout 相容（dual sys.path + build.sh 自動 strip）
- **JSX 工具搬遷**：22 個工具 `docs/` → `docs/interactive/tools/`，registry/flows/loader/hub 路徑同步
- **測試歸位**：`test_assemble_config_dir.py`、`test_da_assembler.py`、`test_flows_e2e.py` 統一搬入 `tests/`
- **generate_tool_map.py 重寫**：自動掃描 ops/dx/lint/root 子目錄

### 🧪 測試工程化（14 輪系統化重構）

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| 測試檔案 | 5 | 40 | +35 |
| 測試數量 | ~790 | 1,759 | +969 |
| Go 測試 | 97 | 110 | +13 |
| Coverage gate | 無 | 64%（`setup.cfg`） | NEW |
| Test markers | 無 | 5（slow/integration/benchmark/regression/snapshot） | NEW |
| Factories | 無 | 12（`factories.py` + `PipelineBuilder`） | NEW |

**關鍵里程碑**：
- Wave 5-6：pytest 遷移、SAST 掃描器（189 rules）、整合測試
- Wave 7-8：property-based tests（Hypothesis）、snapshot tests（18 JSON）、coverage gate
- Wave 9-10：factories 拆分、domain policy、deepdiff structured diff
- Wave 11-12：unittest→pytest batch migration、metric_dictionary fixture
- Wave 13：conftest re-export cleanup、duplicate removal、factory docstrings
- Wave 14-16：parametrize、scaffold snapshots、benchmark baseline、validate_all coverage
- Wave 17：coverage attack — baseline_discovery（31→55%）、backtest_threshold（32→70%）、batch_diagnose（49→71%）
- Wave 18：parametrize sweep — 合併重複測試方法

### 🛡️ 品質閘門

- **Pre-commit hooks**：0 → 12 個 auto-run + 5 個 manual-stage（schema / translation / flow E2E / jsx-babel / i18n coverage）
- **新增 hooks**：`tool-map-check`、`doc-map-check`、`rule-pack-stats-check`、`glossary-check`、`changelog-lint`、`version-consistency`、`includes-sync`、`platform-data-check`、`repo-name-check`、`tool-consistency-check`、`structure-check`、`doc-links-check`
- **Docker CI 修正**：build.sh 自動 strip sys.path hack + 觸發路徑 `**/*.py` + 3 個遺漏工具打包修正
- **Conventional Commits** + `generate_changelog.py` 自動化

### 📦 Dependency Upgrades

- **Prometheus**: v2.53.0 → v3.10.0（PromQL 相容性已驗證，15 個 Rule Pack 無影響）
- **Alertmanager**: v0.27.0 → v0.31.1
- **configmap-reload**: v0.14.0 → v0.15.0
- **Grafana**: 11.1.0 → 12.4.1
- **kube-state-metrics**: v2.10.0 → v2.18.0
- **Go**: 1.22 → 1.26.1（go.mod + Dockerfile + CI）
- **Frontend CDN**: React 18.2.0 → 18.3.1、Babel 7.23.9 → 7.26.4、Lucide 0.383.0 → 0.436.0

### 📊 Numbers

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| Rule Packs | 13 | 15 | +2 |
| Python 工具 | ~20 | 62 | +42 |
| da-tools CLI 命令 | 20 | 23 | +3 |
| JSX 互動工具 | 0 | 24 | +24 |
| 文件（docs/ .md） | ~20 | 68 | +48 |
| 雙語文件對 | 0 | 46 | +46 |
| Python 測試 | ~790 | 1,759 | +969 |
| 測試檔案 | 5 | 40 | +35 |
| Pre-commit hooks | 0 | 12 + 5 manual | +17 |
| Docker images | 2 | 3 | +1 (da-portal) |

### 📈 Benchmark（v2.0.0，15 Rule Packs，Kind 叢集）

**Idle-State（2 tenant，237 rules，43 rule groups）：**

| 指標 | v1.11.0 (13 packs) | v2.0.0 (15 packs) | 變化 |
|------|-------|-------|------|
| Total Rules | 141 | 237 | +96 |
| Rule Groups | 27 | 43 | +16 |
| Eval Time / Cycle | 20.3ms | 23.2ms | +2.9ms |
| p50 per-group | 1.23ms | 0.39ms | 改善 |
| p99 per-group | 6.89ms | 4.89ms | 改善 |
| Prometheus CPU | 0.014 cores | 0.004 cores | — |
| Prometheus Memory | 142.7MB | 112.6MB | — |
| Exporter Heap (×2 HA) | 2.4MB | 2.2MB | — |
| Active Series | ~6,037 | 6,239 | +202 |

**Go Micro-Benchmark（Intel Core 7 240H，`-count=5` median）：**

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 12,209 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 100,400 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 1,951,206 | 3,848,574 | 5,039 |
| ResolveAt_10Tenants_Mixed | 34,048 | 40,052 | 271 |
| ResolveAt_100Tenants_Mixed | 405,797 | 462,636 | 2,622 |
| ResolveAt_1000Tenants_Mixed | 5,337,575 | 5,258,548 | 26,056 |
| ResolveAt_NightWindow_1000 | 5,404,213 | 5,223,925 | 25,056 |
| ResolveSilentModes_1000 | 86,700 | 186,086 | 10 |

**Route Generation Scaling（Python `generate_alertmanager_routes.py`）：**

| Tenants | Wall Time | Routes | Inhibit Rules |
|---------|-----------|--------|---------------|
| 2 | 181ms | 3 | 2 |
| 10 | 196ms | 8 | 10 |
| 50 | 248ms | 41 | 50 |
| 100 | 327ms | 80 | 100 |

---

> **歷史版本 (v0.1.0–v1.11.0)：** 詳見 [`CHANGELOG-archive.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG-archive.md)
