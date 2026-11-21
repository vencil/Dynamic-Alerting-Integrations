---
title: "ADR-030: 告警遷移驗證：注入人造故障，量新規則接住多少"
tags: [adr, migration, victoriametrics, validation]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
id: ADR-030
tracking_kind: adr
status: accepted
domain: rule-packs
created_at: 2026-07-04
updated_at: 2026-11-21
---
# ADR-030: 告警遷移驗證：注入人造故障，量新規則接住多少

> 把告警從 Splunk 這類排程查詢系統搬到 VictoriaMetrics 時，舊系統的規則原文與歷史開火紀錄常常都拿不到。本篇決定在兩者都缺的情況下，怎麼驗證新規則「該叫的時候會叫」。

**決策摘要**：不去證明新規則與舊規則等價（沒有舊規則可比，這題無解），改成對新規則集注入已知故障的指標波形，量它接住多少（catch-rate）。對照答案是我們**製造**的，不是從舊系統**觀測**來的：波形由沒看過新規則的領域專家依真實故障行為寫出，不可從規則閾值反推。漏接任何一個注入的必叫故障，驗證就不通過；但就算全部接住，結論也只涵蓋注入過的故障，這套工具不會、也不能單獨給出「可以切換」的判定。

## 狀態

🟢 **Accepted**（2026-07-19）。本 ADR 自此凍結、不再修改（僅修錯字／連結）。待解問題（例如哪些告警屬合規類）是導入前要和遷移方一起補齊的項目，不是本決策生效的前提。

2026-11-21：依 owner 對全部 ADR 的改寫指示（[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）改寫表達，拿掉內部代號與過程敘事、補上名詞解釋與範例；決策不變。

依語言政策（ADR-019 起預設只寫中文），本篇沒有英文版。

## 名詞

- **來源系統**：要退役的舊告警系統，例如 Splunk 的 scheduled search（排程執行一段查詢、結果符合條件就發告警）。**saved-search** 是存在 Splunk 裡的一筆排程查詢設定，包含查詢本身與排程、去重、嚴重度等欄位。
- **VM**：VictoriaMetrics，遷移的目標時序資料庫。**vmalert** 是它的規則評估元件；`vmalert -replay` 讓 vmalert 對一段已寫入的歷史資料重跑規則，把開火結果寫回資料庫。**vmsingle** 是單機版 VictoriaMetrics。
- **`ALERTS`**：規則開火時寫進時序資料庫的指標，每條開火中的告警一條 series。它記錄的是「規則開火了」，還沒經過 Alertmanager 的分組、抑制與靜音。
- **oracle（對照答案）**：判斷「這時候該不該叫」的獨立依據。驗證新規則需要一份不是從新規則本身推出來的答案。
- **catch-rate**：注入的必叫故障中，新規則接住的比例。**漏報（FN）** 是該叫沒叫，**誤報（FP）** 是不該叫卻叫了。**recall** 是接住的比例（1 減去漏報率），**precision** 是叫出來的告警裡真的該叫的比例，**F1** 是兩者的調和平均。
- **故障波形**：某個指標在某種故障下的時間序列形狀，例如表空間在半小時內從 40% 爬到 96%。**波形庫**是一組這樣的故障描述。
- **SME**：領域專家（subject-matter expert），例如 DBA。
- **episode（故障事件）**：一次注入的故障。同一個故障可能讓多條 series 同時開火，對帳時算一個事件。
- **fan-out**：一個故障同時出現在多條 series 上（例如同一批主機的同一指標）。**fan-out ratio** 是一個故障事件平均帶出幾筆 series 層級的告警。
- **DTW（dynamic time warping）**：比對兩條時間序列形狀是否相似的演算法，容許時間軸上的伸縮。
- **soak**：新規則接上真實流量、與舊系統並行跑一段時間觀察。**passive shadow soak** 指只觀察、不接管告警的並行期。
- **chaos-mesh**：Kubernetes 上的故障注入工具，能讓真的 Pod、網路或磁碟出狀況。
- **`for:`**：告警規則的持續條件，條件要連續成立這麼久才開火；中途條件不成立，計時就歸零。
- **SLO**：服務水準目標。**MTTA**：從問題發生到有人回應的平均時間。

## 背景

告警從排程查詢系統搬到 VM 時，遷移方常有兩樣東西拿不到：

1. **來源規則原文**。查詢語言（例如 Splunk 的 SPL）常被視為智財或敏感資料，不對外提供。遷移方手上只有轉換後的 VM 規則與告警路由，而且路由常是分組之後的下游視圖。
2. **來源系統的歷史開火紀錄**。舊系統的告警歷史不一定匯得出來；系統一旦退役，它的開火行為就永遠消失。

要驗證「轉換前後等價」，需要新規則與一份獨立的對照答案（舊系統的決策）。兩樣都缺的時候：

- **拿新規則驗新規則是循環論證**。靜態檢查找得到新規則自身的缺陷（查詢語法差異、沒有接收者的路由、series 數量爆炸），但證明不了它與舊系統的判斷一致。
- **追求完全一致本身就是錯的目標**。舊閾值是配合舊系統的取樣方式調出來的：排程間隔、跳過的排程、查詢時間窗的邊界、索引延遲，都已內化在數字裡。把取樣更精確的 VM 規則對齊舊閾值，會造成大量誤報。

本篇的立足點：對照答案不是「有或沒有」，而是「觀測來的或製造來的」。舊系統的歷史觀測不到，但可以製造：注入已知故障，看規則有沒有偵測到。這是混沌工程的標準做法，學術討論見 [Validating Alerts in Cloud-Native Observability（arXiv 2510.23970）](https://arxiv.org/abs/2510.23970)。

完整的系統設計與對帳設計記在 issue [#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948)。

## 決策

### 1. 驗證「接不接得住」，不驗「是否等價」

驗證的問題是「新規則集有沒有接住該接的故障」，不是「新規則是否逐條等於舊規則」。目標有三條：注入的必叫故障漏報為 0（硬門檻）、誤報有上限（軟門檻）、會叫醒值班的告警沒有說不出原因的退化。

這是業界的主流做法：逐條翻譯閾值被普遍視為反模式（Google SRE workbook、incident.io、Nobl9 都這樣說），成熟的做法以事件與 SLO 為準。

代價是**無法證明來源與目標一模一樣**。遷移方的變更管理若硬性要求「證明等價」，這條路滿足不了（見下方「待解問題」）。

**合規類告警不在 catch-rate 範圍內。** catch-rate 量的是「接住了哪些」，結構上證明不了「沒有產生新的盲區」。稽核日誌告警、SOC 2、PCI 這類合規告警排除在 catch-rate 之外，改由兩個人各自審查規則邏輯，作為補充證據。這一類正是「拿不到來源規則」真正會出事的地方，必須去索取規則原文或改走人工審查。

### 2. 對照答案用製造的：注入故障波形

三種對照答案，現在只做第一種：

| 對照答案 | 狀態 | 涵蓋什麼 | 限制 |
|---|---|---|---|
| 故障波形注入 | 已建 | 已知故障簽章的 catch-rate | 只涵蓋注得出來的已知故障 |
| 重放真實事件 | 延後 | 真實發生過的事件（答案在工單系統，不在來源系統） | 受指標保存期限制 |
| passive shadow soak | 延後 | 沒預料到的長尾故障 | 只驗得到剛好發生的 |

**故障波形注入**：把故障會產生的指標波形（例如表空間逼滿、deadlock 尖峰、CPU 飽和）寫進一台隔離的 VM，對新規則集重跑，記錄哪些告警開火、何時開火。這檢查的是「新規則集接不接得住已知的故障樣貌」，不是「與舊規則的差異」（舊規則拿不到）。

**波形庫必須同時有數值故障與時間軸擾動。** 排程查詢與 VM 串流評估的差異多半出在時間軸上：資料晚到、抓取漏掉造成的資料過期、亂序寫入。所以波形庫除了數值越界的故障，也要包含時間軸擾動：時間戳抖動（jitter）、漏抓（dropout）、資料停止回報（staleness）。

**波形不可從規則閾值反推。** 波形要來自真實或文件記載的故障行為（那個指標在真故障時實際的形狀）。先看閾值、再造一條會踩過閾值的波形，「規則踩了、所以通過」是循環論證，對「接不接得住真故障」毫無資訊。光靠自律擋不住「沒有歷史資料又要交差」的壓力，所以波形參數由**沒有參與撰寫或轉換 VM 規則的 SME** 在沒看過規則的情況下提供。

**波形要帶真實的雜訊。** SME 手寫的波形通常太乾淨，乾淨波形會系統性地高估 catch-rate，原因有二：貼近閾值來回震盪的訊號會不斷讓 `for:` 計時歸零，乾淨的爬升測得過不代表真故障接得住；單一 series 的波形驗不到多 series 的聚合行為（`by()` 收斂、多 series 同時出現、標籤變動）。所以每個故障一律產生三種變體：疊加日常雜訊、在故障期間掉回正常水位的震盪、多 series 同時出現。

**SME 用問卷描述故障，不必懂取樣。** SME 不講「取樣間隔」「樣本序列」的語言。平台提供標準化的填空範本（`scripts/tools/dx/waveform_pack_template.yaml`），問題包括正常水位、故障水位、惡化多快、持續多久、日常抖動多大、故障中會不會暫時掉回、故障當下監控數據還在不在、半夜發生要不要被叫醒；再由編譯器把回答轉成注入參數。沒填日常抖動的退回重填，從源頭擋住太乾淨的波形。

代價：製造的對照答案只涵蓋注得出來的已知故障，碰不到沒預料到的長尾，所以注入結果**不能單獨當作切換的放行訊號**（見第 8 節）。

### 3. 沿用 vmalert 重放設施，不做真故障注入

故障波形注入沿用 repo 既有的 `vmalert -replay` 測試設施（固定時間原點、寫入、查詢），只多注入合成波形。不用 chaos-mesh 讓真的元件出故障。

代價與交換：只動指標層、不碰遷移方環境、影響範圍小、自給自足；但涵蓋不到 exporter 層的失效（故障時 exporter 到底有沒有送出那個指標）。chaos-mesh 真故障注入延後，觸發條件：確認 exporter 送不出指標是真實風險，**或**取得 staging 環境授權。

**時間比對的複雜度設停損。** 這裡的時間比對是「時間窗內的事件集合比對」，範圍有界，不是 AIOps 那種事件關聯引擎（AIOps 已明確排除在範圍外）。為了防止自建的比對越做越複雜，設一條停止條件：差異分類的誤判率超過門檻（門檻待定）時，停止優化自建的比對，改評估現成的事件關聯平台。

### 4. 三個平面，各用各的方法

| 平面 | 問的問題 | 方法 | 誰負責 |
|---|---|---|---|
| 決策 | 同一個故障有沒有被叫出來 | 時間窗內的 precision／recall／F1（漏報＝recall 缺口＝硬門檻；誤報＝precision 缺口＝軟門檻） | 本篇 |
| 資料 | 指標有沒有被忠實重現 | DTW | 遷移方自管 |
| 內容 | annotation 與標籤有沒有正確渲染 | 沿用既有的告警單元測試 | 既有測試 |

告警是離散的事件，適合用 precision／recall 衡量；指標是連續訊號，適合用 DTW 比對形狀。要遷移方接受「DTW 是訊號對齊工具，回答不了決策問題」，換來的是每個問題都用對的工具。

內容平面不能省：recall 再完美，annotation 渲染錯了（例如比例值 100% 顯示成 1%、少了 runbook 連結），維運上一樣是災難。這一層不另外造，沿用 promtool 與 vmalert-tool（Prometheus 與 VictoriaMetrics 各自的規則單元測試工具）的規則單元測試，它們會比對開火告警的標籤與 annotation。

### 5. 時間容差逐告警類別訂，先鎖預設天花板

不挑一個全域的魔術時間窗做通過／不通過。每個不一致都要分類（晚但接住、漏接、誤報、已重訂基準而消失）並說明原因：目標是「每個差異都說得出原因」，而不是「完全一致」。

代價是失去乾淨的客觀通過／不通過，前期要逐類分類；換來的是反映真實。arXiv 2510.23970 同樣不採固定時間窗：晚 30 秒開火算不算誤報，要看情境。

**防止放寬容差把漏報洗成通過。** 逐類協商的容差很容易被操弄：上線壓力下把 5 分鐘放寬到 15 分鐘，漏報就變成「晚但接住」，儀表板硬生生變綠。所以：

- 每個告警類別的容差有硬天花板，由該告警的 SLO／MTTA 推出來，不是隨意訂；
- 訂容差的人不能是執行遷移的工程師；
- 容差的每次變更都留下審核紀錄。

少了這幾條，漏報為 0 的硬門檻就形同虛設。

**先有預設天花板矩陣，例外才協商。** 幾百條規則逐條協商會變成營運瓶頸，拖久了必然妥協成一個寬鬆的常數，正好是上面要防的路。所以分兩段：先依嚴重度與 SLO 訂好預設的容差天花板矩陣，而且在看到任何差異**之前**就鎖定（先承諾本身就是防操弄的手段）；只有預設值不適用的告警類別才進入逐類協商。矩陣的數值由各案的 MTTA／SLO 推出，本篇不列範例值。

計分器 `scripts/tools/dx/waveform_score.py` 讀的容差檔就是這個形狀：`defaults` 是嚴重度對天花板的矩陣，`overrides` 是逐類例外，每筆都必須帶 `justification` 與 `approved_by`，而且容差不得超過它宣告的嚴重度那一列的天花板，違反時結束碼 2。

### 6. 用 saved-search 的排程設定預測誤報

來源 saved-search 的排程（`cron_schedule`）、去重（`alert.suppress`）、嚴重度（`alert.severity`）、查詢時間窗（`dispatch.earliest`／`dispatch.latest`）是結構化欄位，可以透過 REST 匯出，**與查詢本身（`search` 欄位）分開**。拿這些欄位預測新規則會在哪裡產生大量誤報：排程間隔等於取樣粒度、去重等於抑制重複、時間窗已內化在舊閾值裡。據此決定哪些規則要重訂基準。閾值不照搬舊系統，在 soak 期間重新訂。

代價：前提是這些設定匯得出來；查詢本身仍拿不到，所以只能預測誤報傾向，不能精算。

### 7. 範圍：只量規則開火層

故障波形注入定位為**決策層的測試資料**（相當於加大版的 promtool `input_series`），不碰資料平面；波形是否忠實仍由遷移方用 DTW 自管。注入**假設**指標會如實送出，這個假設本身的驗證屬於資料平面（見第 3 節延後的 chaos-mesh 與下方「待解問題」）。

**量測點是 `ALERTS`，不是通知送達。** catch-rate 算在規則開火層。Alertmanager 的 `group_by`、inhibit、silence 若設錯，告警會被過度合併或抑制，於是可能 catch-rate 100%、值班卻收不到通知。通知送達屬於抑制之後的那一層，延後處理（需要影子 Alertmanager 的 webhook 指紋比對）；這套工具**不宣稱**驗證了通知送達。

**對帳單位是故障事件，不是 `ALERTS` 的每一列。** 排程查詢常隱含彙總（一次查詢就是一個告警事件），VM 的 `ALERTS` 則按 time series 展開：同一個注入故障可能同時帶出 N 筆 series 層級的告警。catch-rate 的分子分母以「故障事件 × 告警類別」計算，不以 series 列計算。遷移盤點時要標出「舊系統一個告警 → 新系統多維同時開火」的規則群：這是預期中的 fan-out，差異分類記為中性，也是日後維度收斂與關聯分析的優先對象。

報告另外揭露每個故障事件的 fan-out ratio。比值過高是明確的警訊：catch-rate 接住不代表通知面安全，`group_by`／inhibit 的收斂治理要在切換前跟上。這個指標只揭露、不當門檻，守住「量測點在開火層、不驗通知送達」的範圍。

代價：定位較保守，不主導整個切換決策；換來範圍清楚、分工不必重談。若遷移方主動要求由本平台主導切換的放行判定，才重談範圍。

### 8. 防止誤判可以切換的護欄

最嚴重的失效是：報告說「catch-rate 守住了，可以切換」→ 遷移方退役來源系統（不可逆）→ 真的故障漏掉。四道護欄，全部強制：

1. **漏報為 0 是硬門檻，但只針對注入的故障**。重放時漏接任何一個注入的必叫故障，就擋下判定。對沒注入的長尾零保證，不要讀成「生產環境零漏報」。這道門檻的效力依賴第 5 節的容差天花板，否則漏接可以被放寬時間窗洗成「晚但接住」。
2. **永不宣稱等價，未驗的範圍要明列**。報告只說「這些已知故障接住了」，同時列出「這些範圍沒有驗」，每份報告都要顯性標出沒涵蓋的部分。
3. **注入與 passive soak 都通過之前，擋住「可以切換」的判定**。故障注入只涵蓋已知故障，不能單獨當放行訊號。passive soak 還沒建，所以這套工具結構上無法單獨給出「可以切換」：這是刻意的保險，不是缺陷。
4. **注入與收集管線不准把「工具壞了」當成「接住了」**。binary 不存在、VM 連不上這類狀況一律硬失敗，不可默默當成「有接住」；用 `cmd && pass || fail` 這種寫法時，工具缺失會被讀成開火，整批結果變成假綠。

### 範例：注入一個磁碟爬滿的故障

輸入一：波形庫節錄（`tests/dx/fixtures/waveform/selftest_disk_used_percent.yaml`，工具自測用的假指標，所以下面的指令都要帶 `--allow-selftest`）：

```yaml
  - description: "自測用：磁碟空間在半小時內從常態爬升到近滿，並持續兩小時"
    normal_level: 42
    fault_level: 96
    onset_duration: 30m
    hold_duration: 2h
    typical_wobble: 1.5
    dips_back: false
    agent_keeps_reporting: true
    must_detect: true
    metric: selftest_disk_used_percent
    time_axis:
      dropout_pattern: "every:10"   # 每 10 個樣本漏抓一個
```

輸入二：候選規則（`tests/dx/fixtures/waveform/rules/selftest_disk.rules.yaml`）`selftest_disk_used_percent > 80`，`for: 2m`，`severity: warning`；容差檔（`tests/dx/fixtures/waveform/tolerances/selftest_tolerances.yaml`）給每個嚴重度一個天花板（工具自測用的示意值，依第 5 節不列在文件裡）。

在有隔離 vmsingle（`-retentionPeriod=100y`）與 vmalert 的環境執行：

```bash
python3 scripts/tools/dx/inject_waveform.py --allow-selftest \
    --rules tests/dx/fixtures/waveform/rules/selftest_disk.rules.yaml \
    --rules-delay-s 30 --seed 1 --out inject.json \
    tests/dx/fixtures/waveform/selftest_disk_used_percent.yaml
python3 scripts/tools/dx/waveform_score.py inject.json \
    --tolerances tests/dx/fixtures/waveform/tolerances/selftest_tolerances.yaml
```

輸出（注入）：一個故障產生六條 series，五條是必叫，震盪變體因為 SME 說不會掉回，是編譯器硬造的探針，只回報、不擋判定：

```text
  [must_detect]   sig0 disk-fill base                      FIRED @+1680s (SelftestDiskUsedHigh)
  [must_detect]   sig0 disk-fill noise                     FIRED @+1680s (SelftestDiskUsedHigh)
  [probe]         sig0 disk-fill oscillation               FIRED @+1680s (SelftestDiskUsedHigh)
  [must_detect]   sig0 disk-fill fanout/f01                FIRED @+1650s (SelftestDiskUsedHigh)
  [must_detect]   sig0 disk-fill fanout/f02                FIRED @+1680s (SelftestDiskUsedHigh)
  [must_detect]   sig0 disk-fill fanout/f03                FIRED @+1650s (SelftestDiskUsedHigh)
  未歸因 alerts: 0
```

輸出（計分，結束碼 0；節錄，省略 `reason:` 與 `scope:` 兩行）：

其中 indeterminate 是「接住與否判不出來」的 case（例如開火的告警被聚合規則拿掉標籤、歸不回是哪個故障），carve-out 是容差檔 `carve_outs` 明列、排除在分母外的故障類別（例如第 1 節的合規類），early-onset 是規則在故障還在爬升時就開火（可能過度敏感，只揭露、不擋判定）。

```text
verdict: PASS  (catch 5/5 = 100.0%, FN=0)
  must_detect 總數 5 = scored 5 + indeterminate 0 + carve-out 0（守恆）；probe/informational 不入分母 1 筆
  ⚠️ early-onset 過敏開火 5 筆（規則在 onset 段就開火、遮蔽過度敏感；只揭露不 gate）
  fan-out ratio: p50=1 p90=1 max=1（只揭露不 gate）
```

把規則閾值改成 `> 99`（高於故障水位 96），同一份波形沒有任何一條開火，計分變成 `verdict: FAIL  (catch 0/5 = 0.0%, FN=5)`，結束碼 1。計分報告最後一行一律附上範圍聲明：判定只涵蓋注入的故障、永不宣稱等價、不能單獨當作切換的放行訊號。

## 後果與已知限制

### 得到的

- 繞過了「拿不到來源規則」與「拿不到來源歷史」兩個限制。
- 不宣稱等價，降低了假綠燈推人退役舊系統的責任風險。
- 注入沿用既有的 vmalert 重放設施（`tests/rulepacks/vm_harness.py`），新寫的部分是串接：注入、收集開火、時間比對、分類、產出報告。
- 驗證結果以結構化格式保存（每個注入故障與它的開火紀錄，標明哪些必叫），等於新系統一份最乾淨的開火歷史，日後做歷史比對可以直接當基準。這只是報告格式的要求，不因此承諾任何 AIOps 或事件關聯的範圍（與第 3 節的停損一致）。

### 要承擔的

- 只涵蓋已知故障，長尾要靠延後的 passive shadow soak 補上。
- 逐告警類別的分類是前期功夫（第 5 節的代價）。
- 遷移方若硬性要求「證明等價」，這條路有缺口。
- 要跑注入，得先備好一台隔離的 vmsingle 與 vmalert；VM 連不上時注入工具直接以結束碼 2 停下。
- **計分器的計數單位與第 7 節的對帳單位還不一致。** 第 7 節決定以「故障事件 × 告警類別」計，但 `waveform_score.py` 目前把每個（故障, 變體）各算一個必叫 case，fan-out 的每條 series 也各算一個，所以上面範例裡一個故障算成 5 個。告警類別也還不是分母的維度。在計分器改過來之前，讀 catch-rate 時要知道它是以變體計。
- 兩層對帳（涵蓋面與值班負擔）與標籤治理契約是 [#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948) 裡的設計，repo 裡還沒有，不是現成可用的元件；估算本篇範圍以外的工作量時別漏掉。
- **波形庫是靜態資產，會隨遷移方的拓撲與標籤變動而過時。** 標籤 schema 變了，結果是大聲失敗而不是悄悄變差：selector 不再匹配 → 注入的故障不開火 → 漏報為 0 的門檻擋下判定。但維護成本是真的。緩解方式：波形庫的標籤集合從標籤治理契約衍生，不手寫散落各處。「注入時動態抓取現場拓撲疊上去」不採用，因為它會破壞注入的可重現性（固定時間原點、隔離的 VM 是重放設施的根基）。

### 待解問題

- **哪些告警屬於合規類**（依第 1 節排除在 catch-rate 之外、改走兩人審查）？排除原則已定，清單待遷移方界定。
- **指標保存多久**？這決定「重放真實事件」能回放多長的時間窗。目前未知。
- **exporter 層失效是不是真實風險**？這決定第 3 節延後的 chaos-mesh 是否觸發。

### 延後的工作與觸發條件

- **重放真實事件**：事件紀錄拿得到，**且**指標保存期涵蓋事件的時間窗。
- **passive shadow soak**：取得來源系統退役前重疊期的環境授權。
- **chaos-mesh 真故障注入**：exporter 送不出指標確認是真實風險，**或**取得 staging 授權（第 3 節）。
- **把這套工具產品化成可重複使用的工具**：出現第二個同類型的遷移案（只有一案不做產品）。
- **自動化重訂基準**：saved-search 設定到手，**且** soak 首次出現大量誤報（粗估：單一 soak 窗內超過 N 條說不出原因的誤報，N 待定）。

## 考慮過的替代方案

- **驗證轉換前後等價**（不採用）：需要舊規則或舊開火紀錄當對照，兩者都拿不到；就算拿得到，對齊舊閾值也會帶進舊系統的取樣偏差（見背景）。
- **只做靜態稽核**（不足以單獨使用）：找得到新規則自身的缺陷，證明不了它接得住故障。
- **用 chaos-mesh 讓真元件出故障**（延後）：能涵蓋 exporter 層，但要碰遷移方環境、影響範圍大，需要 staging 授權（第 3 節）。
- **全域固定的時間比對窗**（不採用）：一個數字套不了所有告警，最後不是太嚴就是被放寬成洗綠的工具（第 5 節）。
- **用 DTW 回答決策問題**（不採用）：DTW 比對訊號形狀，告警是離散事件，形狀相似不等於該叫的都叫了（第 4 節）。
- **注入時疊加現場拓撲**（不採用）：會破壞注入的可重現性（見已知限制）。
- **自建事件關聯引擎**（不採用）：AIOps 範圍已明確排除；時間比對只做有界的時間窗事件集合比對，並設停損（第 3 節）。

## 相關

- 工具：`scripts/tools/dx/waveform_compile.py`（驗證、回讀、產生注入資料）、`scripts/tools/dx/inject_waveform.py`（注入與重放）、`scripts/tools/dx/waveform_score.py`（時間比對與計分）；波形庫格式 [`waveform-pack.schema.json`](../schemas/waveform-pack.schema.json)、容差檔格式 [`waveform-tolerances.schema.json`](../schemas/waveform-tolerances.schema.json)
- [故障波形擷取與轉譯 SOP](../internal/waveform-elicitation-sop.md)：怎麼向 SME 擷取故障、轉成波形庫
- [Air-gap 自助驗證 runbook](../internal/airgap-self-service-runbook.md)：資料不出關的環境怎麼跑
- Rule Pack 參考驗證庫（`tests/rulepacks/reference-validation/README.md`）：用公開文件寫成的 Oracle、DB2、Linux-on-K8s 波形庫與結果
- 設計討論：issue [#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948)
