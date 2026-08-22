# 成對交錯量測的夜跑比值序列（2026-08-16 .. 2026-08-21）

> [ADR-032](../../../adr/032-paired-interleaved-bench-measurement.md) 第一段（量測管線）上線後的頭六夜。
> 主要消費者是 [#1439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1439)（TRK-359）**第二段**的門檻決策與驗收 harness，
> 以及 [#1497](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1497)（單一 benchmark 雙峰）。

## 為什麼這份資料要進 repo

它正在腐爛，而且是兩層腐爛：

1. **本序列的來源是 job log**，六夜逐夜解析而得。GitHub 的 job log 有保留期限，過了就再也重建不回來——與隔壁 [`bench-trend-2026-08/`](../bench-trend-2026-08/README.md) 收絕對序列的理由完全相同。
2. **artifact 只留 90 天**（`bench-record.yaml` 的 `retention-days: 90`），所以 `bench-paired.json` 原件同樣會消失。

⛔ **這裡收的是輸出，不是「重建方法」。**

## ⛔ 單位不同，不可與隔壁那份混用

| | 這份 | [`bench-trend-2026-08/`](../bench-trend-2026-08/README.md) |
|---|---|---|
| 內容 | `(main ÷ 釘死的參考版本 − 1) × 100`，**比值** | 每夜 median **ns/op**，絕對值 |
| 機器項 | 在比值裡相消（同一台 runner、同一個 job 內交錯） | **不相消**，是該序列最大的噪音來源 |
| 用途 | 第二段門檻決策、配對版驗收 harness | 現行監測器的反事實驗證 |

把兩者池化就是把 ADR-032 整個論證抹掉——它存在的理由正是「機器項在比值裡消失、在絕對序列裡不會」。

## 檔案

| 檔 | 內容 |
|---|---|
| `nights.json` | 六夜 × 22 支（20 支 benchmark + 2 支對照）的比值，每夜附 run id / job id / head SHA / CPU 型號 / 當夜 `workload_drift` 狀態 |
| `analyze_paired.py` | **可直接重跑**，重新導出本文引用的每一個數字：`python3 -B analyze_paired.py` |

⚠️ 與隔壁不同的一點：那份的兩支 `.py` 不可重跑（需要未收進 repo 的中間產物）。這一份**可以**——它唯一的輸入就是旁邊的 `nights.json`。

### 腳本會拒絕分析不對的資料

`load()` 把這份**封存快照**的形狀當契約執行，任一不符就 `ValueError` 中止：

| 面向 | 契約 |
|---|---|
| metadata | `schema` = `bench-paired-series/v1`、`unit` = 那句百分比比值、`reference.sha` = `3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0` |
| 形狀 | `nights` 為非空 list、每筆是 object、`night_utc` 皆為字串、唯一且嚴格遞增 |
| 身分 | 日期序列**逐一**等於 `2026-08-16 .. 2026-08-21`（**不是**「六夜」而已） |
| 完整性 | 每夜有 `ratios_pct`，且 key 集合**逐一**等於釘死的那 22 支**名字**（不是「22 支」而已） |

理由不是防呆，是**這份資料特有的失效模式**：換單位、換參考版本、少一夜的讀數——
下面每一張表**還是會正常印出來**，數字看不出任何異狀。錯的數字會正常 render。

⛔ 「身分」那一列是量出來的，不是保守起見：**只驗夜數擋不住**。一份把六夜整體位移一天的
資料（`08-17..08-22`）夜數對、唯一、遞增，全部通過，但後四夜窗的 `08-18..21` 仍全在，
於是腳本照樣印出「LAST four nights」——而實際最後四夜是 `08-19..22`。標籤說謊，零警告。
同理「重複一夜」能把已拍板的 5%/2 判定從 **1 fire 變 3 fires**，憑空生出兩張票。

⛔ **同一個錯我在這裡犯了第二次，寫下來。** 這批守衛的第一版把 benchmark 那一列寫成
「數量 == 22」——正是我上一段剛論證過不夠的形狀。實測：把某一支在六夜全部改名，
數量仍是 22、各夜仍一致，守衛全過，然後死在 `statistics.quantiles` 的
`StatisticsError` stack trace。改成釘死**名字**之後才擋得住。下面每一節都以特定名字
索引（`ATTRIBUTED` / `UNSTABLE` / §3 那三支），所以契約是那 22 個**身分**，
數量只是它的後果。

也因為身分被釘死，`FOUR_NIGHT_WINDOW` 現在**直接由 `EXPECTED_DATES[-4:]` 導出**，
「最後四夜」是結構上必然而非另一個手寫常數。原本那個「窗不完整就警告」的退路已移除——
它在唯一宣稱能擋的情境（加第七夜）裡**根本不會觸發**，看起來像守衛卻是死碼。

守衛本身逐道做過 intentional-break（弄壞 → 確認會擋 → 還原），不是「寫了就算」：

```text
未破壞      → EXIT=0                 （還原後輸出與此逐位元相同）
schema      → EXIT=1  unsupported schema 'bench-paired-series/v2'
unit        → EXIT=1  unexpected unit 'median ns/op'
reference   → EXIT=1  pinned reference is '000…0', expected '3fd96b51…'
nights 空   → EXIT=1  nights must be a non-empty list, got list of length 0
非 list     → EXIT=1  nights must be a non-empty list, got dict of length n/a
夜非 object → EXIT=1  every night must be an object
日期非字串  → EXIT=1  every night needs a string night_utc
重複一夜    → EXIT=1  night_utc must be unique and strictly increasing
順序顛倒    → EXIT=1  night_utc must be unique and strictly increasing
整體位移    → EXIT=1  this is a pinned archival snapshot of [...]
加第七夜    → EXIT=1  this is a pinned archival snapshot of [...]
缺 ratios   → EXIT=1  night 2026-08-18 has no ratios_pct mapping
只剩 21 支  → EXIT=1  night 2026-08-16 does not carry the pinned 22 benchmarks (missing […])
各夜不齊    → EXIT=1  night 2026-08-18 does not carry the pinned 22 benchmarks (missing […])
改名一支    → EXIT=1  night 2026-08-16 does not carry the pinned 22 benchmarks (missing […], extra […])
```

⚠️ 上表的「未破壞」與「還原」兩列輸出**逐位元相同**，且與加守衛前的正本輸出也**逐位元相同**
——守衛只擋壞資料，沒有動到任何統計。

**這六種修改前不會紅**（counterfactual，對 `173a4710` 版實跑）：重複一夜、順序顛倒、
整體位移、加第七夜、各夜不齊、日期非字串——全部 `EXIT=0` 靜默通過。另六種（`nights` 空、
非 list、夜非 object、缺 `ratios`、只剩 21 支、改名一支）修改前就會死，但死在 `IndexError` /
`TypeError` / `StatisticsError` 的 stack trace，不是說得出「哪個契約被違反」的錯誤。
**那不算偵測，只是碰巧撞死。**

## Provenance

參考版本固定為 `exporter/v2.9.0` / `3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0`
（[`.github/bench-reference.yaml` @ `55a86d1d`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/55a86d1d5d9c23078c4cce4ed878e038e5e116fe/.github/bench-reference.yaml)
—— 刻意連到**不可變的 revision** 而不是 `main`：那個檔案的內容就是這批資料的意義所在，換版之後 `main` 上的它會指向別的參考版本，而這六夜不會跟著改）。
`analyze_paired.py` 載入時會驗證 `nights.json` 的 `reference.sha` 與這裡一致，不一致就拒絕分析。

| 夜 | run | CPU | `workload_drift` |
|---|---|---|---|
| 08-16 | [31924848480](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/31924848480) | AMD EPYC 7763 | **欄位不存在**（#1455 當天稍晚才 merge） |
| 08-17 | [31992030018](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/31992030018) | Intel Xeon 8573C | 4 檔 |
| 08-18 | [32096162832](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32096162832) | AMD EPYC 9V74 | 4 檔 |
| 08-19 | [32212925057](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32212925057) | AMD EPYC 7763 | 4 檔 |
| 08-20 | [32329013063](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32329013063) | **未記錄** | 4 檔 |
| 08-21 | [32444409563](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/32444409563) | AMD EPYC 9V74 | 4 檔 |

⚠️ **08-20 的 CPU 型號是空的，因為我沒讀到那一行**，不是因為它不存在。`null` 在這裡的意思是「本次未取得」，不是「無此資訊」——兩者混淆正是這條線一路在修的病。要補的話那一行還在該 run 的 log 裡。

## 這六夜說了什麼

以下每個數字都由 `analyze_paired.py` 現場導出。

### 1. 已拍板的判定規則（門檻 5%、連續 2 夜）在真實序列上只發射一次

```text
threshold 5% / 2 nights -> 1 fire(s): MergePartialConfigs_1000@08-17
threshold 3% / 2 nights -> 3 fire(s)
threshold 1% / 2 nights -> 9 fire(s)
```

⇒ 第二段監測器切換後開的**第一張票**，會是 [#1474](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1474) 已經量測過、歸因過、並決定**接受**的那一支。這不是預測，是把規則重播在資料上算出來的。門檻下調的代價同樣是量出來的，不是形容詞。

### 2. 離群集中在一支 benchmark，不是全域噪音

| 樣本集 | n | 中位 | p90 | max | >5% |
|---|---|---|---|---|---|
| 全部 20 支 × 6 夜 | 120 | 0.66% | 3.57% | 25.02% | 10 |
| 扣掉 #1474 已歸因的 2 支 | 108 | 0.60% | 1.65% | **25.02%** | 2 |
| 再扣掉 #1497 那一支 | 102 | 0.57% | 1.43% | **3.25%** | **0** |

其餘 17 支在 102 個 bench-night 裡**從沒超過 3.25%**。

### 3. 對照測試乾淨 ≠ 當夜每一支都可信

```text
2026-08-17  canary -0.12/-0.09   worst 19.34%  IncrementalLoad_1000_OneFileChanged
2026-08-21  canary +0.02/+0.00   worst 25.02%  IncrementalLoad_1000_OneFileChanged
```

六夜中有兩夜是「對照測試乾淨，而某一支擺了 20 個百分點以上」。已拍板的**對照測試閘門**（ADR-032 §待決 5）回答的是「成對量測這一夜有沒有壞掉」，**不是**「這支 bench 今晚穩不穩」——這兩件事在資料上是分開的。

### 4. per-bench 門檻（rustc-perf 形）現在不能用，而且理由有兩層

判準取自 `rust-lang/rustc-perf` 的 `docs/comparison-analysis.md`（實抓原文）：`result > Q3 + IQR × 3`。

**第一層：樣本數不足，而且不足到門檻本身會亂跑。**

```text
IncrementalLoad_1000_OneFileChanged   n=6  thr=+7.46%   leave-one-out +6.33% .. +8.49%
```

⛔ 而同一支只取**後四夜**（08-18..21，即六夜序列的第 3–6 夜）時算出來的門檻是 **+27.94%**。少兩夜、門檻移動 **20 個百分點**——這比 leave-one-out 更能說明「歷史不足時這個門檻不能用」。兩個數字都由 `analyze_paired.py` 同一次執行印出，可逐字對照。

⚠️ 這一段同時是一次更正：本序列進 repo 之前，[#1497](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1497) 與相關討論裡引用過「門檻 +27.94%，所以 +30.38% 照樣穿過去」——那個數字出自四夜窗，六夜下**不成立**（+25.02% 會被 +7.46% 的門檻抓到）。結論的方向不變（per-bench 門檻解決不了那支 benchmark），但當時的**論據**是錯的。

**第二層是結構性的，與樣本數無關。**

```text
MergePartialConfigs_1000   median +7.42%  -> threshold +9.57%
ResolveSilentModes_1000    median +4.05%  -> threshold +12.83%
```

rustc-perf 的歷史是 **commit 對 commit 的差值**（分布中心在 0），而這裡的歷史是**對釘死參考版本的水位**（中心在 +8%）。把水位餵進「歷史分布」，門檻就長在水位之上 ⇒ **這個形式的 per-bench 門檻是一台吸收機**，正是 ADR-032 存在要消滅的東西。

⇒ 若日後採用，必須是「**離散度**決定門檻寬度、**水位**另行判定（走 ACCEPTED 出口）」那一形，不是把水位混進歷史那一形。
**Defer-with-trigger**：配對序列 ≥30 夜（1 筆/夜，自 08-16 起算 ≈ 2026-09-14）且屆時重跑本檔、確認門檻在增加樣本後收斂。

### trigger 到期時怎麼做（先決定，別到時候臨場想）

⚠️ 上面那句 trigger 要的**不是**「拿 30 夜算一個新數字」，是**六夜與 30 夜的門檻要能對比**。
任何讓兩者跑在不同程式碼上的做法，都會把「門檻變了」跟「腳本變了」混在一起——
正是 ADR-032 要消滅的那種混淆。所以做法先釘死：

| | 到期時 | 做法 |
|---|---|---|
| **主路徑** | PR-A（digest 欄位）已落地 | 把本目錄的 `analyze_paired.py` 搬到 `scripts/tools/dx/` 並接 `--dataset <dir>`，新資料集另開 `bench-paired-2026-09/` 只放 `nights.json` + README。六夜與 30 夜走**同一份程式碼**，對比才成立 |
| **退路** | PR-A 尚未落地 | 先另開 `bench-paired-2026-09/`（含腳本副本）取得 30 夜數字，但**必須標註「跨程式碼版本，收斂結論僅供參考」**；搬家延到 PR-A |

⛔ **不採「原地改 `EXPECTED_DATES` 擴充本目錄」**。量過的代價：把常數改成前五夜、
前四夜再跑同一支腳本，輸出分別有 **36%** / **48%** 的行數變動。本檔引用 24 個百分比
與 3 個樣本數（120 / 108 / 102），全部由這六夜算出——擴充等於一次推翻它們，
而其中 `+27.94%` 正是本檔更正紀錄的證物。且 `audit-reports` 樹至今
**4 個 commit 新增資料、0 個修改資料**，原地改也違反既有慣例。

⛔ **參數化時契約不可搬進資料檔**。「讓 `nights.json` 自己宣告 `expected_dates`」
看似自然，實則讓守衛失效——與本檔 `reference.sha` 那條同構：被替換的資料集會
**連同自己的契約一起被替換**，`load()` 拿被竄改的宣告驗被竄改的資料，永遠通過。
契約必須釘在資料**之外**，這正是 PR-A 的 digest 在解的問題，也是主路徑要等它的原因。

## 沒有做的事（別讀成已做）

- **沒有收 `bench-paired.json` 原件**，只收了從 log 解析出的比值。原件在各 run 的 artifact 裡，90 天後消失。
- **沒有收每支的樣本離散度**：夜跑每側每支有 6 個樣本，本序列只留了 median 導出的比值。要做「當夜重複樣本」的分析得回去讀 artifact。
- **08-20 的 CPU 未取得**（見上）。
- 六夜**不足以**支撐任何關於誤報率的宣稱；本檔只陳述這六夜發生了什麼。
