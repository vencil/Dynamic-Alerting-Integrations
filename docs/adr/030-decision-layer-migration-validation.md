---
title: "ADR-030: 決策層遷移驗證 — 製造 Oracle 而非觀測"
tags: [adr, migration, victoriametrics, validation]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
id: ADR-030
tracking_kind: adr
status: accepted
domain: rule-packs
created_at: 2026-07-04
updated_at: 2026-07-19
---
# ADR-030: 決策層遷移驗證 — 製造 Oracle 而非觀測

> 將告警自專有 scheduled-search 系統（如 Splunk）遷移至 VictoriaMetrics 時，**來源規則邏輯**與**來源系統歷史開火紀錄**常皆不可得。
> 本 ADR 定義 Vibe 如何在兩者皆缺下驗證遷移的**決策層**——用**製造** ground truth（fault-waveform 注入）取代觀測，並以 **outcome/catch-rate** 為主軸取代 conversion-parity。
>
> ⚠️ 兩者皆缺下，「轉換前後**等價**」本身無解（無舊規則可差分）→ MVP 改問「新規則**接不接得住已知故障**」的**前向 soundness**；真正的雙邊差分留待 passive shadow（Future Work）。
>
> 承 [ADR-023 (Write-Plane Single-Writer)](./023-write-plane-single-writer-invariant.md) 之外另一條遷移軸；決策層驗證的系統本體 RFC 見 issue [#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948)（設計 SSOT）。與遷移方自管的**資料平面波形驗證（DTW）**分工不變。

## 狀態

🟢 **Accepted**（2026-07-19）。MVP fault-waveform 注入 harness 三 PR 全數合併（[#1039](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1039) 波形編譯 ／ [#1043](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1043) 注入執行 ／ [#1045](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1045) catch-rate 計分 ＋ air-gap 自助 [#1079](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1079)），並產出首份 Oracle/DB2/Linux-on-K8s 參考 catch-rate 報告（vendor-doc 盲寫庫、n=2 獨立作者、含 precision/recall/F1 與 8 項 rule-pack 覆蓋/校準發現）→ 「製造 oracle（非觀測）＋ outcome/catch-rate」的設計方向經實測驗證。內部 fresh-eyes 複審 + 外部三輪對抗 review（2026-07-04 首輪 ／ 2026-07-05 R2、R3）已全數納入。**依 ADR lifecycle（`accepted` = 決策生效、非 rollout 完成，見 Martin Fowler / AWS Prescriptive Guidance），OQ1（合規 carve-out 清單）等客戶項維持列於 [§Open Questions](#open-questions) 作 rollout 前置，不再作為 `accepted` 的前置門檻。** 本 ADR 自此凍結、不再修改（僅修錯字 / 連結）。

> 依語言政策（自 ADR-019 預設 ZH-only），本 ADR 不另製 `.en.md`。

## 背景

### 問題類別（通用）

告警自專有 scheduled-search 系統遷移至 VM 時，遷移方常面對兩個**不可得物**：

1. **來源規則邏輯不可得** — 來源查詢語言（如 SPL）常為智財/敏感，不對外釋出；遷移方只拿得到**轉換後**的 VM 規則 + alert routing（且常是 grouping 後的下游視圖）。
2. **來源歷史開火紀錄不可得** — 來源系統的 triggered-alert 歷史未必匯得出；且來源系統退役中，其開火行為是**折舊資產**（退役即永久消失）。

在此雙缺口下，「驗證轉換前後」這個訴求本身有一個認識論陷阱。

### 為什麼這是棘手問題：Oracle 缺口

要驗證「轉換前後等價」，需同時有 target（新 VM 規則）與 **oracle**（舊系統的決策真相）。當只有 target、無獨立 oracle 時：

- **拿 target 驗 target 是同義反覆**——靜態稽核找得出 target 自身缺陷（MetricsQL 分歧、orphan route、cardinality），但**證明不了它與舊系統決策一致**。
- 追求 exact parity 本身是錯門檻：來源閾值內化了來源系統的取樣傷疤（cron cadence、skipped-search 缺口、search-window 邊界、indexing lag）；把精準 VM 規則對齊舊閾值會 FP 風暴。

**關鍵洞見（本 ADR 的立足點）**：oracle 不是「有或無」的二分，是「**觀測 vs 製造**」。觀測不到舊系統歷史，但可以**製造** ground truth——注入已知故障、驗兩邊是否偵測（承 OXN, arXiv 2510.23970；chaos-engineering 標準做法）。

### 設計討論紀錄

經一輪 vibe-brainstorm 五問發散（reuse / MVP / trade-off / defer-trigger / blast-radius），locked decision 摘要見「決策」節。RFC 系統本體與完整對帳設計見 issue [#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948)。

## 決策

### D1. 主軸：outcome-validation（catch-rate），非 conversion-parity

驗「新規則集是否接住該接的 incident/故障」，而非「新規則是否 1:1 等於舊規則」。承 RFC 北極星：**FN regression = 0（硬門檻）+ FP 有界（軟門檻）+ page-worthy 零未解釋退化**。

- **Trade-off**：換到「可解、有業務意義」的驗證；**犧牲「可證來源/目標一模一樣」**。若遷移方變更管理硬性要求「證明等價」，此路徑不滿足（見 OQ1）。
- **依據**：1:1 threshold 翻譯是業界公認反模式（Google SRE workbook / incident.io / Nobl9）；成熟做法是 incident/SLO-driven。
- **⛔ 合規負空間 carve-out（外審 F2）**：catch-rate 是**正向覆蓋**指標，結構上證明不了「未引入新盲區（negative space）」。故**合規類告警**（audit-log alerting / SOC2 / PCI 等）**排除於 catch-rate MVP 之外**，改強制**人工邏輯雙重審查（dual-control logic review）**當補充證據——這是「來源規則不可得」真正咬到、必須索取規則或人工審的一類。

### D2. Oracle 用「製造」非「觀測」；三-oracle stack，MVP = fault-waveform 注入

| Oracle | 相位 | 蓋什麼 | 限制 |
|---|---|---|---|
| **fault-waveform 注入** | **MVP（built now）** | 已知故障簽章的 catch-rate | 只蓋注得出的已知失效模式 |
| incident-replay | Future Work | 真實 incident（oracle 在 ticketing、非來源系統） | 受 metric retention 限（OQ2）|
| passive shadow soak | Future Work | 未知關聯的 long tail | 只驗剛好發生的 |

- **fault-waveform 注入**：對新 VM 規則集注入「故障會產生的 metric 波形」（如 tablespace 逼滿 / deadlock 尖峰 / CPU 飽和），觀察哪些 alert fire、時序如何。這是**前向 soundness 檢查**（新規則集接不接得住已知故障簽章），直接服務 D1 的 catch-rate 主軸——**不是**「與舊規則差分」（舊規則不可得）。
- **注入型態須含時間軸擾動、非只數值突破**（外審 F1）：Splunk cron-batch vs VM 流式評估的分歧多在**時間軸**（late-arriving data / missed-scrape staleness / 亂序寫入），非數值。波形庫**強制**含 (a) 數值故障波形 + (b) 時間軸擾動（jitter / drop-out / staleness）；**(b) 直接 reuse [#968](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/968) 既有 staleness/scrape-timing 機制**（#968 本為此建）。
- **⛔ 反循環守則（實作鐵則）**：波形庫須源自**真實/文件化的故障簽章**（該 metric 在真故障下的實際形狀），**絕不可**從規則自身閾值反推——否則「造一個會踩閾值的波形 → 規則踩了 → 通過」是同義反覆，對「是否接住真故障」零資訊。**治理（外審 F6）**：光靠自律會敗給「無歷史 + 要交差」的壓力 → 波形參數須由**未參與撰寫/轉換 VM 規則的獨立領域專家（DBA/SME）盲寫**提供（職責分離，同 D5），防隱性逆向工程。
- **⛔ 波形須帶真實底噪，禁「過度潔癖」合成（外審R2-1）**：SME 手寫的合成波形通常太乾淨，純淨波形會系統性高估 catch-rate。具體機制（自外審泛稱「聚合預期外行為」reframe 為可證偽機制）：(a) 貼近閾值的震盪訊號會不斷重置 `for:` 去抖計時器——乾淨 ramp 測過 ≠ 真故障接得住（Vibe 生產規則已燒過同型 spiky-gauge reset-trap，repo 已有 oscillation regression test 前例）；(b) 單 series 波形驗不到聚合維度（`by()` 收斂、多 series fan-out、label churn）。波形庫**強制**含三類變體：底噪疊加、貼閾值震盪、多 series fan-out。
- **SME 盲寫配套：波形擷取問卷範本（外審R2-5）**：領域專家不說「取樣間隔/樣本序列」的語言 → MVP 交付物含一份標準化範本（故障簽章 → 形狀類別 ramp/spike/oscillation/plateau、幅度區間、持續時間、底噪水準、有無前兆、時間軸擾動型態），由範本機械轉譯成注入參數；未填底噪/震盪欄位者退回重填（與 R2-1 綁定、防「太乾淨」從源頭混入）。
- **Trade-off**：製造 oracle 只蓋「注得出的已知失效模式」，蓋不到未知關聯 long tail → 故 **fault-injection 單獨不可當 cutover go-signal**（見 D8）。

### D3. Reuse-over-build：擴充 #968，chaos-mesh 列 defer-with-trigger

fault-waveform 注入 **reuse `vmalert -replay`（[#968](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/968)）既有 harness**（固定 epoch / import / query_range 基建），注入合成波形即可。**不採 chaos-mesh 真故障注入**。

- **Trade-off**：只碰 metric 層、不碰遷移方環境、blast-radius 小、自足；**犧牲 exporter-level 失效覆蓋**（故障時 exporter 是否真 emit 該 metric）。
- **Defer-with-trigger**：chaos-mesh 真故障 → trigger：發現 exporter-emit 為真風險（OQ3）**OR** 取得 staging 環境授權。
- **Temporal-matcher 停損（外審 F7，reframe）**：本案 temporal matching = **視窗內事件集合比對**（有界），**非** AIOps 事件關聯引擎（AIOps 擴張 RFC 已明確排除）。防自建複雜度失控 → sunset-trigger：divergence 分類的 false-diff 率 > 門檻（待定）即停止優化自建、評估現成事件關聯平台。

### D4. 驗證形式化：三個平面切乾淨——決策層 precision/recall、資料層 DTW、內容層 rendering parity

- **決策平面**（有沒有把同一 incident 呼出來）= alert 是事件/點過程 → **temporal-window 下的 precision/recall/F1**（FN=recall 缺口=硬門檻；FP=precision 缺口=軟門檻）。
- **資料平面**（metric 有沒有被忠實重現）= 連續訊號 → **DTW**（遷移方自管，分工不變）。
- **內容平面**（annotation/label 是否正確渲染，外審 F4）= 靜態渲染正確性 → **Annotation/Label parity**。完美 recall 但 annotation 渲染錯（如 100% 顯示成 1%、缺 runbook URL）維運上仍是災難——Vibe 已被咬過（[#975](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/975) ratio-render 100%→1%）、有 in-flight guard（[#978](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/978) renderer↔fixture 耦合 lint）→ 此平面**沿用既有 gate A annotation-templating 驗證**、不新造。
- **Trade-off**：要遷移方接受「DTW 是訊號對齊工具、答不了決策問題」；用對工具的低成本明確贏。

### D5. temporal matching window = 逐 alert-class 協商容差，非全域常數

不挑魔法窗做 pass/fail。每個 mismatch **分類**：晚但接住 / 漏接 / 假警 / 已 re-baseline 掉——再解釋。這 operationalize「**divergence-explanation 而非 parity**」。

- **Trade-off**：犧牲「乾淨客觀 pass/fail」+ 前期逐類分類工，換「反映真實」。OXN 研究亦明確拒絕定死時間窗（「fire 晚 30 秒算不算 FP 是 context-dependent」），佐證方向。
- **⛔ 防 Tolerance Gerrymandering（外審 F5）**：協商容差**極易被 game**——上線壓力下把 5m 放寬到 15m，就能把 FN 洗成「晚但接住」、儀表板強行變綠。故 (a) 每 alert-class 容差有**硬天花板**、由該 alert 的 **SLO/MTTA 導出**（非隨意）；(b) **職責分離**：容差制定者 ≠ 執行遷移的工程師；(c) 容差變更留審計軌跡。**否則 D8.1 的 FN=0 形同虛設**。
- **預設天花板矩陣、例外才協商（外審R2-2）**：數百條規則逐類協商是營運瓶頸，拖久必妥協成寬鬆常數——正是 F5 要防的洗綠路徑。改**兩段式**：先依 severity/SLO 訂**預設容差天花板矩陣**（在看到任何 divergence **之前**鎖定——pre-commitment 本身即防 gerrymandering 的治理手段），僅預設不滿足的 alert-class 才進逐類協商。矩陣數值由 engagement 的 MTTA/SLO 導出（外審示意值不入文）。

### D6. re-baseline 輸入 = saved-search metadata（與規則邏輯可分離）

來源 saved-search 的 `cron_schedule` / `alert.suppress`（throttle）/ `alert.severity` / `dispatch.earliest,latest` 是結構化 REST 匯出，**與 `search`（規則邏輯本體）分離**。用它**預測** FP 風暴（cron cadence=量化、suppress=去重、窗=內化進舊閾值），指引 re-baseline。閾值禁 port、soak re-baseline（承 RFC）。

- **Trade-off**：metadata 匯得出的前提下成立；規則邏輯本體仍不可得，故只能預測 FP 傾向、不能精算。

### D7. Scope 邊界守住（承 RFC #948 分工）

fault-waveform 注入定義為**決策層測試 fixture**（放大版 promtool `input_series`），**不踩資料平面**；波形忠實度仍歸遷移方自管（DTW）。注入**假設** metric 如實 emit——該假設本身的驗證屬資料平面（見 D3 defer / OQ3）。

- **量測點 = pre-inhibition ALERTS，非通知送達（外審 F3）**：catch-rate 算在規則開火層（TSDB `ALERTS`）。Alertmanager 的 group_by/inhibit/silence 若誤配 → 過度收斂/抑制 → **catch-rate 可 100% 但 SRE 收不到 page**。通知送達屬**後抑制 / Toil 層**（RFC 兩層對帳、Future Work；需 shadow-AM webhook 指紋比對）——MVP **不宣稱**驗證通知送達。
- **對帳單位 = 故障事件（episode），非 per-series `ALERTS` 列（外審R2-3）**：來源 scheduled-search 常隱性匯總（一次搜尋＝一個告警事件），VM `ALERTS` 依 time series 展開——同一注入故障可能齊發 N 筆 series-level alert。catch-rate 分子分母以「故障事件 × alert-class」計、不以 series 列計（承 RFC A2 的 episode 對帳單位）；遷移編目時**標註「舊單一告警 → 新多維齊發」規則群**——此為預期 fan-out、divergence 分類記中性，並成為未來維度收斂／關聯分析的優先群體。MVP 報告另須揭露**每 episode 的 series 膨脹係數（fan-out ratio，外審R3-1）**：係數過高即顯性警示——catch-rate 命中 ≠ 通知面安全，`group_by`/inhibit 收斂治理（RFC A5）須在 cutover 前跟上；此指標**只揭露、不當 gate**（守 D7 量測點邊界與「MVP 不驗通知送達」之界）。
- **Trade-off**：positioning 較保守（不主導 cutover 全局）；換 scope 清爽、分工不重談。defer-trigger：若遷移方主動要求 Vibe 主導 cutover gate，才觸發重談邊界。

### D8. Blast-radius 護欄（強制）

新能力最恐怖的失效 = **報「catch-rate 保住、可切換」→ 遷移方退役來源系統（不可逆）→ 真 incident 被漏**（false-green 把人推下懸崖）。護欄四件組：

1. **FN = 0 硬門檻（僅限 injected fault set，非 production 零漏——見 D2 限制）** — replay 漏任一 injected case 即擋 verdict；對**未注入的 long tail 零保證**，勿讀成安全保證。**其有效性依賴 D5 的容差天花板**（否則 miss 可被放寬窗洗成「晚但接住」）。
2. **永不宣稱「等價」** — 只報「這些已知 case 接住 + **這些未驗邊界**」（no-silent-caps；每份報告顯性標出未覆蓋面）。
3. **HARD-BLOCK「可切換」verdict 直到 manufactured-oracle + passive soak 皆過** — fault-injection 只蓋已知故障，不可單獨當 go-signal（承 [#947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/947) governance flag）。**在 passive soak 建成前（Future Work），本 harness 結構上無法單獨產出「可切換」verdict——此為刻意 fail-safe、非缺陷。**
4. **注入/收集管線禁 `&& pass || fail`** — binary / VM-absent 須 **hard-fail**，不可靜默當「catch」。repo 已燒過：jitter-harness 的 `&& P || F` 把 binary-absent 當 fire → 容器清 `/tmp` 後全假 catch。

## 後果

### 正面

- 繞過來源規則邏輯 + 來源歷史兩個不可得物；**this-week 交付物**（首份 catch-rate 報告；前提：首批**真故障簽章**波形庫 + on-demand VM 環境已備——#968 為 on-demand/skip-if-no-VM、非 per-PR）。
- 誠實 under-claim（不宣稱等價）→ 降低 false-green 責任風險。
- reuse **已建 repo 件**（[#968](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/968) replay harness / [#969](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/969) coverage baseline / 三態 Sentinel+inhibit / would-fire promtool），build 主要是**膠水**（注入→收集雙邊 alert→temporal-match→分類→divergence-explanation 報告）。
- 驗證輸出**結構化保存**（injected-fault ↔ firing 紀錄、含 ground-truth 標記）＝新系統最乾淨的「開火歷史」資料集，未來歷史比對可直接當 baseline（外審R2-4；僅為報告格式要求，不因此承諾任何 AIOps／關聯引擎範圍——與 D3 停損一致）。
- ⚠️ **兩層對帳（Coverage/Toil）與 A5 label governance 是 RFC [#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948) 的設計決策、repo 尚未建**（見 #948），屬 Future Work、**非現成可 reuse**——別據此低估 MVP 外的工作量。

### 負面 / 風險

- MVP 只蓋已知故障；long tail 靠 Future Work 承接（passive shadow）。
- temporal-window 逐類分類是前期工（D5 trade-off）。
- 若遷移方硬性要「證明等價」，outcome-validation 有缺口（OQ1）。
- **波形庫是靜態資產、隨遷移方拓撲/label 漂移折舊**（外審R3-2）：label schema 漂移在本設計下 **fail-loud 而非靜默退化**——selector 不再匹配 → injected case 不 fire → D8.1 FN=0 擋下 verdict；但由此而來的波形庫維護成本真實。緩解：波形庫的 label 集**自 label-governance 契約（RFC A5）衍生**、不手寫散落；「注入時動態抓 live topology 疊加」則**否決**——會破壞 harness 的 hermetic determinism（固定 epoch／隔離 VM 是 #968 的根基）。

## Future Work（defer-with-trigger）

- **incident-replay oracle** → trigger：incident 紀錄可得 **AND** metric retention ≥ incident 窗。
- **passive shadow soak** → trigger：來源系統退役重疊期環境授權。
- **chaos-mesh 真故障** → trigger：exporter-emit 為真風險 OR staging 授權（D3）。
- **產品化 harness 為可複用 Vibe 工具** → trigger：**第 2 個同型遷移案**（n=1 不做產品）。
- **re-baseline 自動化** → trigger：metadata 到手 + soak 首見 FP 風暴（粗量：單 soak 窗 > N 條未解釋 FP，N 待定）。

## Open Questions

- **OQ1**：哪些告警屬**合規類**（→ 依 D1 carve-out 排除於 catch-rate MVP、走 dual-control 人工審）？carve-out 原則已決（外審 F2），待遷移方界定清單。
- **OQ2**：metric retention 深度（RFC D1）——決定 incident-replay 可回放窗。未知。
- **OQ3**：exporter-level 失效是否為真風險（決定 D3 chaos-mesh trigger 是否觸發）。
