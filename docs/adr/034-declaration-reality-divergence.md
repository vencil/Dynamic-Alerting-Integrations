---
title: "ADR-034: 宣告與現實不一致時的預設行為 — fail-loud 為預設，fail-soft 需列舉"
tags: [adr, config, security, dx, reliability]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-034
tracking_kind: adr
status: proposed
domain: platform
created_at: 2026-08-23
updated_at: 2026-08-23
---

# ADR-034: 宣告與現實不一致時的預設行為

## 狀態

🟡 **Proposed**（2026-08-23）。owner 核可後昇格 Accepted。

> 依語言政策（自 ADR-019 起預設 ZH-only；ADR-024 / ADR-025 為保留 `.en.md` sibling 的例外），本 ADR 不另製 `.en.md`。

## TL;DR

- **一句話**：DAI 有多處在「**宣告**與**現實**不一致」時，選擇相信宣告、且不驗證——結果不是明顯壞掉，而是**看起來正常地做錯事**。
- **本 ADR 決定一條原則加它的例外清單**，不決定個別缺陷的修法（那些各自開票）。
- **原則**：宣告與現實不一致時，預設 **fail-loud**。fail-soft 只在明確列舉的情況允許，且每一項必須說出「退到下一層是**保守的預設**，還是**沉默的零檢查**」。
- 這條判準借自協同產品 db-runbooks 的四層解析（見〈外部來源〉）。它的安全性關鍵不是「有 auto-detect」，而是**每一層都定義了偵測不到時往哪退、且絕不猜**。DAI 缺的正是那條邊界的明文化。

## 背景

### 三個看似無關的發現，同一個根

2026-08-22/23 對 DAI 做了一輪三個互斥 lens 的盤點（設定值解析分層／手工清單漂移／憑證分類）。三份結果表面上分屬三個領域，但把它們並排會看到同一個形狀：

| 領域 | 宣告 | 現實 | 系統的選擇 |
|:--|:--|:--|:--|
| 設定解析 | `--write-mode: pr-guthub` | 這不是合法值 | 相信宣告 → 靜默降級為 direct commit |
| 文件清單 | `test-map.md` 說某腳本是 lint tripwire | 零 runner 引用 | 相信宣告 → 讀者以為有機械防線 |
| 憑證 | `_routing_profile` 的慣例說憑證留平台側 | body 可直接 inline 明文憑證 | 相信慣例 → 明文落 git |

三者都不是「壞掉」——是**壞得像沒壞**。

⚠️ **現行規範完全沒有涵蓋這個形狀**：`docs/internal/dev-rules.md` 的 13 條裡，grep `fail-loud` **零命中**；最接近的 §5 是 SAST 的 7 條安全 review 準則，談的是程式碼寫法，不是「宣告與現實脫鉤時該怎麼辦」。所以這不是「規範沒被遵守」，是**規範沒有這一條**。

### 為什麼現在寫

`_silent_mode` 的三態（dev-rules §3）、Sentinel Alert（§8）、Cardinality Guard 這些既有設計都有明確的「不確定時怎麼辦」。但它們是**逐案**決定的，沒有共同判準——所以新增一個解析點時，作者要自己重新發明一次，而發明的方向取決於當下在想什麼。上面三個發現分屬三個不同時期、三個不同作者，卻犯同一個形狀，就是缺共同判準的證據。

## 決策

### 原則

> **當「被宣告的值」與「現實」不一致時，DAI 的預設行為是 fail-loud。**
>
> fail-soft 只在本 ADR〈允許 fail-soft 的情況〉列舉的類別中允許。新增一個 fail-soft 的解析點時，必須在該處的註解回答一個問題：
>
> **「退到下一層之後，得到的是保守的預設，還是沉默的零檢查？」**
>
> 前者可以 fail-soft；**後者一律 fail-loud**。

### 判準的操作型定義

「保守的預設」= 退下去之後，系統做的事**比宣告要求的少**，且少掉的部分是可觀測的。
「沉默的零檢查」= 退下去之後，某道閘門**整個消失**，而且消失後的外觀與「閘門通過」無法區分。

⚠️ 判斷的是**退下去之後的狀態**，不是退下去的原因。「檔案不存在」與「路徑打錯」的原因不同，但如果兩者退到同一個狀態，判準只看那個狀態。

### 允許 fail-soft 的情況（列舉，非窮舉原則）

1. **布局偵測** — 「conf.d 是 flat 還是 hierarchical」「路徑是檔案還是目錄」這一類。退下去的行為是**保守的**：少讀一些設定，而不是少做一道檢查。
2. **診斷性標籤** — `configSource`（configmap / git-sync / operator）這一類只影響可觀測性、不影響行為的值。
3. **繼承鏈的缺層** — `_defaults.yaml` 某層不存在時跳過該層。這是繼承語意本身的定義，不是偵測失敗。

### 一律 fail-loud 的情況

1. **治理決策** — 「要不要走 PR 審核」「授權檔在哪」這一類。這些的下一層是「閘門消失」，不是「做得少一點」。
2. **信任邊界的宣告** — 憑證來源、身分來源、org 標記。
3. **enforcement 的存在性宣告** — 文件說「這件事有機械防線」時，那個宣告本身要可驗證（見〈連帶決策〉）。

### 連帶決策：宣告有 gate 的文件，自己要有 gate

DAI 已經有一整套 generator + `--check` 把索引類文件釘住（ADR 索引、planning-index、doc-map、tool-map、agent adapters，皆 byte-exact 或 fixed-expectation）。**沒有 gate 的那些，剛好是「宣告哪些東西有 gate」的那幾份。**

⇒ **凡是宣稱某項檢查存在的文件段落，該宣稱必須是可機械驗證的**：要嘛引用一個真實存在的 hook / target / workflow job 名（可被 grep 驗證），要嘛不要宣稱。

這條不追求「所有手工清單都要有 gate」——那個目標在文件量這個級別不現實。它只約束**enforcement 宣稱**這一個子集，因為那是唯一會讓讀者（含 AI agent）**因為相信它而少做事**的一類。

## 適用到現況：三個已量到的案例

⚠️ 以下是**本 ADR 原則的適用示範**，缺陷本身各自開票追蹤，不在本 ADR 修。

### A. `--write-mode` 的 `default:` 分支 → 該改 fail-loud

`cmd/server/wire.go` 的 `switch` 是全 repo 唯一解讀該字串的地方，其 `default:` 直接回 `WriteModeDirect`。合法值定義在 `internal/handler/deps.go`，但沒有任何地方把輸入比對那個集合。

```text
wire.go  case WriteModePR, WriteModePRGitHub:  → PR 模式
wire.go  case WriteModePRGitLab:               → MR 模式
wire.go  default:                              → direct commit
```

⇒ 打錯字（`PR` / `pr-guthub` / 前導空白，此處無 `TrimSpace`）落到 `default:`。一個以為所有變更走 PR 審核的部署，實際上每次 `PUT` 直接 commit 進 base branch，而**啟動 log 那行 INFO 與正常 direct 部署逐字相同**。

判準套用：退下去之後，PR 審核這道閘門**整個消失**，且與「刻意選 direct」無法區分 ⇒ **沉默的零檢查** ⇒ fail-loud。

### B. 布局偵測 → **維持 fail-soft**（本 ADR 明確不改它）

`resolveConfigPath()` 探測 `/etc/threshold-exporter/conf.d` 是否為目錄、`ConfigManager` 以 `os.Stat().IsDir()` 決定 single-file vs directory、hierarchy 旗標由「樹裡是否存在 `_defaults.yaml`」推導。

判準套用：退下去之後讀到的租戶**變少**（可從 metric 觀察），而不是某道檢查消失 ⇒ **保守的預設** ⇒ 允許 fail-soft。

⚠️ 但有一個**不屬於布局偵測**的例外要分開看：階層布局下子目錄的租戶完全不進 metric（[ADR-016 §支援邊界](016-conf-d-directory-hierarchy-mixed-mode.md) 已記錄實測），那是「兩個 reader 的母體不相等」，不是 fail-soft 的結果——它連退都沒退，是壓根沒讀。本 ADR 的原則不涵蓋它，它的處置在 ADR-016。

### C. 憑證：DAI 只有第 1 類與第 3 類，中間是空的

借 db-runbooks 的三分法：

| 類別 | 定義 | DAI 現況 |
|:--|:--|:--|
| 1 · by-reference | 只傳「去哪裡拿」（Secret 名 + key 名），值不經過 API | ✅ 有：部署期憑證（forge PAT、federation 私鑰）全走 K8s Secret → env/volume |
| 2 · by-value 但加密 | 值經過 API，但事先以部署金鑰加密 | ❌ **完全沒有**。無部署金鑰、無加密 payload、無 plan/apply 兩段式 |
| 3 · by-value 明文 | 值以明文進入 API | ⚠️ 有，且最大的一處在**租戶自助寫入平面** |

第 3 類最大的一處是 `PUT /api/v1/tenants/{id}` 的 raw YAML body：`_routing.receiver` 的必填欄位本身就是憑證（Slack `api_url`、PagerDuty `service_key`、webhook `url`）。寫入面的 key 檢查是 **soft-whitelist**——`internal/handler/body_validator.go` 的註解逐字寫著「keys NOT in this map pass through without further checks」，而表裡只有四個 key；`internal/policy/policy.go` 只檢查 receiver 的 **type**，不碰任何憑證欄位的值。

值的落點：git commit（永久）→ Alertmanager ConfigMap（明文，非 Secret）→ `GET /tenants/{id}` 的 `raw_yaml`（有 read 權即可讀回）。

⚠️ **secret-scan 四層防線在這條路徑上的實際涵蓋**（這一點必須精確，否則會高估或低估）：L1（開發者 pre-commit）與 L3（release image digest）與此無關；L0（GitHub push-protection）與 L2（server-side workflow）要等 commit 到達 GitHub 才有作用，而 **direct 模式（出貨預設）的 tenant-api 只 commit 不 push**（`push` 在 `internal/gitops/` 只出現於 PR 模式路徑）。⇒ direct 模式下這四層**沒有東西可掃**；PR 模式會 push，L0 對已知 pattern 仍可能攔下。

而 `conf.d` **沒有任何 secret-shape lint**——`scripts/tools/lint/check_helm_values_secrets.py` 管的是 `helm/**` 與 `k8s/**`，同樣的檢查沒有對稱地套到 `conf.d`。

判準套用：慣例說憑證應該留在平台側 profile（`_routing_profile` 名稱參照），現實是可以直接 inline。「相信慣例、不驗證」⇒ 那道慣例是**沉默的零檢查** ⇒ fail-loud。最小的 fail-loud 形式是把 conf.d 納入既有的 secret-shape lint；完整解是補第 2 類。

## 考慮過的替代方案

### A：不寫原則，逐案修三個缺陷

❌ 三個缺陷分屬三個時期、三個作者、三個領域，卻是同一個形狀——這說明缺的是判準而不是修補。逐案修完，下一個解析點還是會重新發明一次。

### B：一律 fail-loud，不留例外

❌ 布局偵測與繼承鏈缺層若改 fail-loud，會讓「conf.d 只有一個檔案」這種完全合法的部署開不起來。而且那兩類退下去確實是保守的——把它們一起硬化，只會製造為了繞過而寫的組態。

### C：把判準寫進 dev-rules 而不是 ADR

❌ dev-rules 是給貢獻者的操作規範（「不要做 X」），這裡要記錄的是**為什麼**與**邊界在哪**，包含刻意保留 fail-soft 的三類。那是 ADR 的體裁。dev-rules 可以在原則被接受後加一行指過來。

### D：照抄 db-runbooks 的四層解析

❌ 那四層的第三層是「去問活的叢集」，而 DAI 的等價資訊全部走硬編檔名（`internal/confd/confd.go` 是讀寫共用的單一謂詞，那是刻意的合約）。照抄會把「宣告即合約」變成「猜」，製造 tenant 混淆。**借的是判準，不是分層本身。**

## 後果

- **新增解析點時多一個必答問題**（退下去是保守預設還是零檢查），寫在該處註解裡。成本是一句話。
- **三個既有案例各自開票**；本 ADR 只提供判準與已量到的證據。
- **會多出一些啟動期失敗**：A 案例修掉之後，今天靜默降級的部署會變成開不起來。這是刻意的——那些部署本來就沒有在做它以為在做的事。
- ⛔ **不影響布局偵測**：B 案例明確維持現狀，避免這條原則被讀成「所有 fail-soft 都要硬化」。

## 未量到的（誠實標註）

- 三份盤點皆為**靜態閱讀 + grep**。本 ADR 中經實際執行驗證的只有：write-mode 的 `switch` 分支與合法值定義、`body_validator.go` 的 soft-whitelist 註解、`push` 在 `internal/gitops/` 的出現位置、`conf.d` 無 secret-shape lint、`test-map.md` 宣稱的腳本引用數、`CLAUDE.md` 九個設計概念在目標文件的字串命中數。
- **C 案例沒有端到端重現**：沒有實際送一個含 `api_url` 的 `PUT` 進去、再檢查 git 與 ConfigMap。推論鏈由三段程式碼路徑構成，但未實測。
- 其餘盤點結果（`--config-dir` 指到錯目錄、`--rbac` 路徑打錯、`-config-dir` 指到檔案）的觸發條件皆為程式碼推導，**未重現**。它們支持本 ADR 的論點，但不應被引用為已驗證的事實。

## 外部來源

判準借自同 MariaDB domain 的協同產品 db-runbooks——[ADR-033](033-ops-execution-plane-interface.md) 記錄了兩邊的關係與那次協同介面評估的結論。該產品對任務參數採四層解析（呼叫者輸入 → 部署期設定 → 查詢現況 → 硬編 fallback），其安全性關鍵是**每一層都定義了偵測不到時往哪退，且絕不猜**。本 ADR 借的是那條「往哪退」的判準，不是分層結構本身（理由見〈替代方案 D〉）。

## Related

- [ADR-016: conf.d/ 目錄階層 + 混合模式](016-conf-d-directory-hierarchy-mixed-mode.md) — 子目錄租戶不進 metric 的處置在該 ADR，本 ADR 不涵蓋
- [ADR-023: 寫入平面 single-writer 不變式](023-write-plane-single-writer-invariant.md) — 寫入平面的併發保證；本 ADR 的 A 案例在其之上
- [ADR-024: 宣告式 Dimensional 告警引擎（含 Custom Alerts）](024-version-aware-threshold-via-dimensional-label.md) — 把告警機械開放給租戶階層的能力，C 案例的寫入平面由它開啟
- [ADR-033: 與運維執行平面的協同介面](033-ops-execution-plane-interface.md) — 判準的外部來源
