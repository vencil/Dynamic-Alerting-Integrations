---
title: "ADR-034: 合法值不得同時當作無法辨識時的 fallback"
tags: [adr, config, reliability, dx]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-034
tracking_kind: adr
status: accepted
domain: platform
created_at: 2026-08-23
updated_at: 2026-08-30
---

# ADR-034: 合法值不得同時當作無法辨識時的 fallback

## 狀態

✅ **Accepted**（2026-08-23 起草，2026-08-29 由 owner 核可）。本規則自核可起生效。

> 依語言政策（自 ADR-019 起預設 ZH-only；ADR-024 / ADR-025 為保留 `.en.md` sibling 的例外），本 ADR 不另製 `.en.md`。

## 摘要

- **問題**：當一個設定的合法值**同時**被拿來當「認不得就用這個」的 fallback，打錯字的輸入就與那個合法值**完全不可區分**。
- 如果那個設定決定了某道檢查跑不跑，後果是**檢查被關掉，而啟動當下沒有可辨別的訊號**。
- **決定**：這類設定必須先對照合法值集合驗證，驗不過就報錯，不得落到 fallback。
- 本 ADR 本身**不修任何東西**，兩個已量到的案例各自開票，兩張都已修：`envBool`（[#1599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1599)，**已修於 [#1624](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1624)**）與 `--write-mode`（[#1559](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1559)，**已修於本規則昇格 `accepted` 之後**）。前者同時觸發了本 ADR 自訂的修訂條件，機械檢查已隨該輪補上——見末節。⚠️ 那支檢查抓不到案例一：它掃的是 Go 生產碼裡自製的 truthy-string 解析器，而 `--write-mode` 的缺陷是「一個 `switch` 沒有對照合法值集合」，形狀不同。**兩個已知實例都修完了，不代表這條規則現在有機械保證**。

## 問題

tenant-api 用 `--write-mode` 決定寫入行為。合法值有四個：

```text
direct       直接 commit
pr           開 PR 讓人審核
pr-github    同上
pr-gitlab    開 MR
```

實際解讀這個字串的地方只有一個 switch（`components/tenant-api/cmd/server/wire.go`），而它只有**三條**臂：

```text
case "pr", "pr-github":   → 開 PR
case "pr-gitlab":         → 開 MR
default:                  → 直接 commit
```

第四個合法值 `direct` 沒有自己的 case——**它就是 `default`**。合法值集合定義在 `internal/handler/deps.go`，但沒有任何一行程式把輸入拿去跟它比對。

於是 `pr-guthub`（打錯字）、`PR`（大小寫）、`" pr"`（從 flag 定義到這個 switch，全程沒有任何一段做去空白處理）全部落到 `default`，得到的結果與「使用者刻意選了 `direct`」**是同一段程式碼執行出來的**——包含那行啟動日誌 `slog.Info("direct write mode (commit-on-write)")`。

⇒ 一個以為「所有變更都要經過審核」的部署，可能每次寫入都直接 commit。**啟動當下沒有可辨別的訊號**：日誌是同一行、沒有錯誤。（⚠️ 是否另有指標可區分，本輪沒有掃過。）

⚠️ 不是完全無跡可循——時間拉長後「PR 不再出現」本身是線索。但那需要有人注意到**某件事沒有發生**，而這正是最不可靠的一種偵測。

## 決定

### 規則

> **決定「某道檢查是否執行」的列舉型設定值，不得把任何一個合法值同時當作無法辨識時的 fallback。**
>
> 必須先對照合法值集合驗證；驗不過就停下來報錯，不要落到任何一個合法值上。

### 適用條件（兩個都要成立）

1. **列舉型** — 這個設定有一份明確、有限的合法值清單可以比對。
2. **決定某道檢查跑不跑** — 定義如下。

**「檢查」的判斷方式**：這個值選錯，會不會導致**某段本應執行的驗證／審核／授權／稽核動作不被執行**？會 ⇒ 屬於本規則。

「審核」在這裡明確包含**人工**審核——本文的主案例 `--write-mode` 決定的正是「要不要有人看過再寫入」，如果判準只寫機器執行的驗證與授權，讀者會依條件 2 把主案例判成不適用。動作由人執行還是由程式執行，不改變「它整段沒有發生」這件事。

邊界例子（都是列舉型，差別只在條件 2）：

| 設定 | 屬於嗎 | 理由 |
|:--|:--|:--|
| 走不走人工審核 | ✅ | 審核這個動作整段不執行 |
| 要不要啟用稽核日誌 | ✅ | 稽核紀錄整段不產生 |
| `--log-level` 打錯退成 `info` | ❌ | 沒有任何驗證／授權／稽核動作被跳過，只是輸出詳細度變了 |
| 顯示用時區打錯退成 UTC | ❌ | 同上，且與檢查無關 |

### 明確不適用

- **不是列舉型的值**（檔案路徑、端點位址、來源標記）。沒有合法集合可以比對，那是另一個問題，本 ADR 不主張。
- **不決定檢查跑不跑的值**（重試次數、顯示用時區、判斷檔案佈局）。寫錯的後果不是「一道檢查消失」。

### 為什麼限縮在這個交集

**列舉型**決定了規則有沒有可執行的動作——沒有集合，「對照集合驗證」這個動作不存在。

**決定檢查跑不跑**決定了後果的嚴重度。同樣是靜默 fallback，`--retry-count` 打錯改變的是重試次數——那是程度差異，而且重試行為本身會留下痕跡（請求次數、耗時）；`--write-mode` 打錯是整道人工審核**整段不執行**，而不執行後的樣子與「刻意不要審核」一模一樣。

⚠️ 這條規則**不主張**其他靜默 fallback 都是好的。它只主張：在這個交集裡，靜默 fallback 一定是錯的。交集之外，本 ADR 沒有意見。

## 案例一：`--write-mode`（已修）

即上一節。逐條套規則：

| 條件 | 判定 |
|:--|:--|
| 列舉型？ | 是——四個值定義在 `internal/handler/deps.go` |
| 決定檢查跑不跑？ | 是——決定寫入要不要經過人工審核 |
| 有合法值當 fallback 嗎？ | 有——`direct` 就是 `default` |
| **結論** | **適用，必須驗證** |

**最小修法**：解讀前先比對合法值集合，不在集合內就啟動失敗。追蹤於 [#1559](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1559)，七個輸入的實測輸出在該票。

⚠️ **終態必須是報錯**——「先出一版只警告」等於仍舊落到合法值上再多印一行 log，而本文的論證正是那行 log 不可靠。過渡要怎麼排（要不要先警告一版、警告多久）是那張票的 rollout 決定，但不改變終態。

**已於本規則生效後修正**（`parseWriteMode` + `wirePRBackend`）。三件值得記下的，因為它們是「套用本規則」在真實程式碼上的具體形狀：

- **`default:` 分支不再是任何合法值的家。** `direct` 升為具名 `case`，`default:` 改成 `log.Fatalf("BUG: ...")`。這一步不是為了擋使用者輸入（那由 `parseWriteMode` 擋掉了），而是擋**下一次的自己**：往合法值集合加第五個模式卻忘了配線，會 fail-loud，而不是無聲落回 direct commit-on-write——那正是本 ADR 講的同一個形狀，只是往上挪了一層。
- **空白 trim，大小寫不 trim。** 兩者看似對稱，判準卻不同：trim 空白不會把無法辨識的值變成合法值（`" pr-guthub "` 照樣被拒），這條界線是案例二的 doc comment 先寫下的，這裡沿用；折疊大小寫則會（`PR` → `pr`），而且沒有標準庫的既有契約可繼承——那是程式在猜意圖。⇒ `PR` / `DIRECT` 一律拒絕。
- ⚠️ **這件事有代價，且它不在原票的預期內**：一個今天寫 `TA_WRITE_MODE=DIRECT` 的部署，現在拿到的**就是**它想要的 direct，修法後會直接開不起來。它不是「帶著打錯字在跑」——原票的 rollout 段只設想了後者。

**rollout**：repo 內量到的面積是零，所以沒有出警告過渡版。逐項見 §證據與限制。

## 案例二：`envBool`（已修，並觸發本 ADR 的修訂條件）

`components/tenant-api/cmd/server/main.go` 的 `envBool` 是一支自己寫的 6 行 switch，接受 `true`/`1`/`yes`/`on`，**其餘一律回 `false`，不報錯也不記錄**。它被當成五個 `flag.Bool` 的預設值用。

| 條件 | 判定 |
|:--|:--|
| 列舉型？ | 是——布林的合法字面值集合有限且明確，`strconv.ParseBool` 就是它的定義（`1`/`t`/`T`/`TRUE`/`true`/`True`/`0`/`f`/`F`/`FALSE`/`false`/`False`） |
| 決定檢查跑不跑？ | 是——五個消費端裡**三個**是：`TA_RBAC_METADATA_SCOPE_ENFORCE` 與 `TA_RBAC_ORG_SCOPE_ENFORCE` 決定 RBAC scope 走 SHADOW 還是 fail-closed，`TA_MACHINE_IDENTITY_AUDIT` 決定機器身分稽核跑不跑 |
| 有合法值當 fallback 嗎？ | 有——`false` 既是合法輸入，又是所有無法辨識輸入的落點 |
| **結論** | **適用，必須驗證** |

**它比案例一更尖銳的地方**：同一個打錯字走命令列會**當場 parse error 停下來**（`flag.Bool` 用的就是 `strconv.ParseBool`），走環境變數則靜默降級。差別只在中間多了那支自製解析器——所以這不是「還沒做驗證」，是**繞過了標準庫已經做好的驗證**。

⚠️ **適用範圍與修法範圍不重合，這裡說清楚**：五個消費端裡另外兩個（`TA_RBAC_EMPTY_OPEN` 是 MED-8 的 rollback 逃生門、`TA_DEV_BYPASS_AUTH` 是 local-dev 身分注入）嚴格說不完全落在本規則的交集內，而且它們打錯字的方向是**較安全**的（維持 fail-closed）。修法仍一次涵蓋五個，理由是它們共用同一支解析器——留一半不改，等於留下本規則要禁的東西的縮小版，下一個消費端接上去時分歧會再出現。**這是修法者的判斷，不是本規則的主張。**

**修法**（[#1624](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1624)）：未設或只有空白維持該 flag 自己的預設；其餘一律交給 `strconv.ParseBool`；解析不了就 `log.Fatalf`。

**修的時候量到三件 [#1599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1599) 沒寫、而且會改變修法形狀的事**（go1.24.7 三路對照探針，22 個輸入）：

1. **裸 `ParseBool` 會炸掉所有沒設 env var 的部署**——空字串在 `ParseBool` 下是 parse error。照票面建議案字面實作，五個 flag 會全部在啟動時 fatal。必須先判空。
2. **`yes` / `on` 過去是合法的 `true`，改後啟動失敗**——這不是「打錯字才失敗」，是現行合法輸入斷裂，需要 release note 而不只是修 bug 的說明。
3. **`t` / `T` 是行為反轉而非報錯**——過去被靜默當成 `false`，改後是 `true`。這是五種輸入裡唯一朝「更開」翻的方向。

⇒ 這三件也回頭印證了本 ADR §後果 那句「**會多出啟動期失敗**……目前沒有盤點過實際有幾個這種部署」：⑵ 與 ⑶ 說明未盤點的不只是「帶著打錯字的部署」，還有**帶著過去合法、現在不合法的值**的部署。⚠️ 該格仍未盤點——只量到 repo 內：官方 helm chart 走 `args:` bare flag、`env:` 區段不含這五個 `TA_*`，所以 **k8s 部署路徑根本不經過 `envBool`**；repo 內也找不到任何以 `yes`/`on` 為值的部署設定。repo 外的現場仍是未知數。

## 考慮過的其他做法

**只修這一個地方，不立規則。**
❌ 規範裡已經有四處在各自的場景處理同一種精神——「不得靜默忽略旗標」「新工具無法靜默逃脫 gate」「no-op 即 fail-open 直接紅」（`docs/internal/dev-rules.md`）。四處都在防同一件事，卻沒有一條可以套到新情況上——本 ADR 認為那是缺通則，但也可能只是四件本來就不同的事各自處理。判斷理由是它們的失敗形狀相同：某件事沒有發生，而且沒有訊號。

**立一條更廣的原則：所有靜默 fallback 都要改成報錯。**
❌ 起草時試過，被審出兩個洞：判斷檔案佈局那類值套進去會得到「該報錯」，但那不是想要的結果；而「憑證可以明文寫進設定」那類缺陷根本沒有「被寫錯的值」，套不進來。⇒ 廣義版判不動它自己舉的案例，因此收窄成現在的交集。

**寫進 dev-rules 而不是 ADR。**
❌ dev-rules 記的是「不要做 X」，這裡要記的是**適用邊界**與**為什麼**。規則被接受後，dev-rules 可以加一行指過來。

## 後果

- 新增一個「決定檢查跑不跑」的列舉型設定時，要多寫一段驗證。
- **會多出啟動期失敗**：案例修掉之後，**如果**現存部署裡有寫錯的值，那些部署會開不起來。這是刻意的——它們本來就沒有在做自己以為在做的事。⚠️ 目前沒有盤點過實際有幾個這種部署。
- ⛔ **不涵蓋**非列舉型的值，以及不決定檢查跑不跑的值。
- 案例另外開票；本 ADR 只提供判準與證據。**案例二已於 2026-08-29 修掉**（[#1624](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1624)），所以上一項對它已不是假設：帶著 `yes`/`on` 或打錯字的部署，升級後會開不起來。
- **多出一個窄的機械檢查**（見末節）：導入時基線零命中，只擋已知的兩種單行形狀，**不改變「本規則主要仍靠 code review」這個事實**。

## 機械執行機制：補上了一個窄的 tripwire，不是完整保證

**起草時沒有。** 本 ADR 原本只靠 code review，並自陳這是弱點——尤其這條規則的立論本身就是「人不會注意到某件事沒有發生」。

**修訂觸發條件已於 2026-08-29 觸發並履行。** 條件原文是「下一次出現**同型缺陷**（合法值兼任 fallback、且該值決定檢查跑不跑）的票，就回頭補一個機械檢查並修訂本 ADR」——[#1599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1599) 命中（見案例二），於是依約補上：

`scripts/tools/lint/check_env_bool_parsers.py`（pre-commit hook `env-bool-parser-guard`；依 [`lint-policy.md`](../internal/lint-policy.md) 屬 **(b) class**：diff-only 掃描、auto-stage、hard block、PR body bypass tag）。它禁止 Go **生產**程式碼出現自製的「truthy 字串轉 bool」解析器，比對兩種**單行**形狀：

- 一個 `case` 臂列出**兩個以上**布林字面值（`case "true", "1", "yes", "on":`）。合法的列舉 switch（`case "pr", "pr-github":`）不會命中，因為那些字面值不是布林字面值；單一個 `case "true":` 也不命中——一個字面值不足以區分 truthy 解析器與一般列舉，這是刻意選的精確度／召回率取捨
- 同一行既讀 env var 又拿它跟布林字面值比較（`strings.ToLower(os.Getenv(k)) == "true"`）

⛔ **它是窄的，而且窄是刻意的。** 它抓的是**已知形狀**，不是「證明不存在自製解析器」：跨行寫成別的樣子、改用 Python、或把字面值換個拼法，它都抓不到；`_test.go` 也在範圍外（測試會合法地拿 env var 比對字串當 subprocess 哨兵）。⇒ **本節標題因此從「這條規則沒有機械執行機制」改成「補上了一個窄的 tripwire」，不是改成「已解決」。**

⚠️ **導入時的基線是零命中**——[#1624](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1624) 修掉了 repo 裡唯一一個。所以它現在是純粹的防迴歸 tripwire，抓不到任何現存的東西。這正是「一個靜默找不到東西可檢查的 shape gate，比沒有 gate 更糟」的形狀，因此它的 self-test 帶**反空轉見證**：把 #1624 之前 `envBool` 的**真實原始碼**餵進去，必須命中；抓不到就轉紅。

⚠️ 觸發條件刻意不寫成「如果發現 review 抓不住就修訂」——那個條件不會產生任何訊號，修訂永遠不觸發。

⚠️ **原本那個「靠人開票」的觸發條件並沒有被這支 lint 取代，它的極限依然成立**：它需要有人開票、有人認出是同型、有人在比對票流，而**本 ADR 沒有指定 label 也沒有指定 owner**。所以它是「可觀測的」，不是「保證會被觀測到」。新增的 lint 只縮小了其中**一種形狀**的漏接面，沒有改變這個結構。要補齊，仍需要一個固定 label 與一位具名負責人——那超出本 ADR 能單方面決定的範圍。

## 證據與限制

結論來自 2026-08-22/23 的一輪程式碼盤點。

**跑過指令、看過輸出的**：

- `--write-mode` 的 switch 只有三條臂、第四個合法值 `direct` 即 `default` 分支
- 合法值集合定義在 `internal/handler/deps.go`，且沒有任何地方把輸入與它比對
- `default` 分支印的就是 `slog.Info("direct write mode (commit-on-write)")` 那一行本身——所以與合法 `direct` 的輸出必然相同
- `dev-rules.md` 內「不得靜默 X」語氣的規則共四處，且各自綁定場景
- **（本輪補測）** 直接呼叫 `wirePRBackend` 餵七個輸入（六個錯的 + `direct`），全部回傳 `mode="direct"` 且 `slog` 輸出逐字相同——見 [#1559](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1559)
- **（本輪補測）** `--write-mode` 從 flag 到 `prBackendFlags.Mode` 全程沒有 `TrimSpace`，所以前導／尾隨空白確實會落到 `default`
- **（本輪補測）** 合法的 PR 值在缺 token 時走 `log.Fatalf`——**這個元件已經會在啟動期硬失敗，只有 `default` 那條沒有**

**（2026-08-29 補測，隨案例二與本次昇格）**：

- go1.24.7 三路對照探針（複製當時的 `envBool` + `strconv.ParseBool` + `flag.Bool`，22 個輸入）逐格比對，量出未設值、`yes`/`on`、`t`/`T` 三處差異——三者都會改變修法形狀
- `envBool` 五個消費端的 fail 方向逐一開檔核對，與 [#1599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1599) 的方向表一致
- helm chart 的 tenant-api container 走 `args:` bare flag，其 `env:` 區段不含那五個 `TA_*` ⇒ **官方 k8s 部署路徑不經過 `envBool`**
- 新增的 lint 在導入時對整個 repo 零命中，其 self-test 以 #1624 前 `envBool` 的真實原始碼作反空轉見證

**（2026-08-30 補測，隨案例一修正）**：

- 重跑案例一的實測，這次是在**本機 go1.26.4** 上直接呼叫真實的 `wirePRBackend`（非複製邏輯），十個輸入（原票七個 + `Direct` / `DIRECT` / `pr-gitlab-x`）**全部**回傳 `mode="direct"`，`slog` 輸出逐字相同——起草時記的形狀成立
- ⭐ 其中 `Direct` / `DIRECT` 是原票沒列的一類：它們的使用者**要的就是 direct、而且拿到了**，修法後會從「正常運作」變成「開不起來」。原票 rollout 段只設想了「帶著打錯字在跑」的部署
- **rollout 面積盤點**：helm chart 的 tenant-api container 既不傳 `--write-mode`，`env:` 區段也不含 `TA_WRITE_MODE`，且全 chart 無 `extraEnv` / `extraArgs` 逃生門（`grep` rc=1）⇒ **官方 helm 部署路徑沒有任何方式能設出一個非法值**。repo 內其餘實際值只有 try-local compose 的 `TA_WRITE_MODE: direct` 與文件範例，全部是合法小寫值 ⇒ **repo 內面積為零，因此不出警告過渡版**
- 修法的 mutation 驗證：把 `parseWriteMode` 還原成 pre-#1559 的靜默 fallback，三個測試轉紅（`rc=1`）——見 [#1559](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1559)

**⚠️ 仍未量到的**：repo 外的現場部署有幾個帶著上述輸入——這與起草時 §後果 列的是同一格，**三輪都沒能量到**。案例一的 rollout 決定因此是建立在「repo 內為零」之上，不是「現場為零」。

**⚠️ 沒有實際重現的**：

- **沒有真的啟動整個服務程序**。上述實測是直接呼叫 `wirePRBackend` 這個函式，涵蓋了「輸入如何被解讀」與「印出什麼日誌」，但沒有跑完整個 `main()` 的啟動流程。案例一的修正沿用同一個邊界：fatal 路徑是用 subprocess 跑該測試自身、斷言 exit code 非零，不是啟動 tenant-api。

## 盤點時另外發現的缺陷（**不是本規則的實例**）

記在這裡的唯一理由是：它是上面「廣義版套不進來」那個論證的證據。**它套不進本規則**——那裡沒有「被寫錯的設定值」，而是檢查涵蓋不足。

`PUT /api/v1/tenants/{id}` 讓租戶送一份 YAML 上來，而告警接收端的必填欄位本身就是憑證。**整檔 PUT 這條路上沒有任何 key 層級的檢查**——它跑的是 YAML 解析、拒絕非 `tenants` 頂層 key、要求 tenant 區段存在、以及 key **名稱**對照 `_defaults.yaml`，四步都不看欄位的值。（`internal/handler/body_validator.go` 那份 reserved-key 驗證器只被批次 patch 端點呼叫。）平台有憑證形狀檢查，但只掃 `helm/**` 與 `k8s/**`，沒有涵蓋租戶設定目錄。

已開票追蹤：[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)，端到端實測（PUT → 200 → 值出現在 `git show HEAD`）已補在該票，本 ADR 不重複。

## Related

- [ADR-023: 寫入平面 single-writer 不變式](023-write-plane-single-writer-invariant.md) — 寫入平面的併發保證
- [ADR-024: 宣告式 Dimensional 告警引擎（含 Custom Alerts）](024-version-aware-threshold-via-dimensional-label.md) — 把告警機制開放到租戶階層
- [ADR-033: 與運維執行平面的協同介面](033-ops-execution-plane-interface.md) — 本規則的思路借自該 ADR 記錄的協同產品：借的是「認不出來時絕不猜」這個態度，不是它的分層結構
