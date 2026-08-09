# Rule-Pack 告警品質參考驗證（ADR-030）

可重複執行的**參考 fixture**，用來量測 Vibe 出貨的 rule-pack 對人造故障的偵測能力
—— 也就是 [ADR-030](../../../docs/adr/030-decision-layer-migration-validation.md)
「製造故障，而非觀察故障」的 catch-rate harness，套用在 Oracle、DB2 與 Linux-on-K8s 上。

## ⚠️ 這些是什麼（以及不是什麼）

- **Vendor-doc 參考函式庫** —— 故障／良性波形 signature，依據公開的 vendor 文件 +
  DBA／SRE 領域知識撰寫。**公開、可提交進版控、可重複使用。**
- **不是客戶案場的波形。** 真實客戶的故障函式庫永遠不會進入這個 repo
  —— 它們從外部路徑載入，並經過 `waveform_score.py --redact`（氣隙式自助服務，
  [#1079](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1079)）。
  本目錄是其*公開知識*版本的對應物，與 `tests/dx/fixtures/waveform/`
  的玩具型自我測試種子並不相同。
- **盲寫（anti-circularity）。** 每一個值都來自真實的故障行為，絕非從規則門檻反推。
  `oracle-reference-n2` / `db2-reference-n2` 是**第二位獨立作者**（不同模型）——
  能跨作者重現的發現才是穩健的。`negative-*` 全部都是 `must_detect:false` 的良性
  signature（精確度探針）。

## 檔案

| 檔案 | 角色 |
|---|---|
| `oracle-reference.yaml`, `db2-reference.yaml` | 故障函式庫（作者 1） |
| `oracle-reference-n2.yaml`, `db2-reference-n2.yaml` | 故障函式庫（作者 2，獨立撰寫） |
| `k8s-linux-reference.yaml` | Linux-on-K8s 故障函式庫（容器／節點） |
| `negative-oracle.yaml`, `negative-db2.yaml` | 良性函式庫（精確度探針） |
| `candidate-{oracle,db2,k8s}.rules.yaml` | 出貨告警邏輯的 direct-predicate 形式（見各檔標頭） |
| `divergence-ledger.yaml` | candidate ↔ 出貨規則的**宣告式分歧帳本**（sync gate 的輸入，見下節） |
| `tolerances.yaml` | ⚠️ **示意性質**的偵測時間上限 —— 並非由客戶 MTTA 推導而來 |

## ⛔ Candidate rules 不是生產規則的 proxy

`candidate-*.rules.yaml` 是出貨告警**謂詞**（門檻 + `for:`）的 direct-predicate 形式。
之所以不直接跑生產規則，是因為生產側的 `max by(tenant)` 聚合會**剝除注入歸因 label**，
使每一次開火都無法歸因（詳見各 candidate 檔標頭）。代價必須講明白：

> **本 harness 量測的是「告警數學」，不是「營運管線」。**
> recording rule 層（聚合方向、向量匹配、rate/ratio 語意）**完全不在量測範圍內**。
> 有人把生產規則的 `max` 誤寫成 `min`、或改壞 `by()` 造成向量不匹配，
> 本 harness 仍會給出 100% PASS。

**這不是假設性風險——已有實例**：`DB2HighSortOverflow` 的 recording rule 直接相除兩個
**累積 counter**（`db2_sort_overflows / db2_total_sorts`），得到的是生命期累積比例而非
近期速率比，長跑 instance 上會逐漸失去敏感度。該缺陷**正好落在本 harness 的盲區**
（ratio 型告警被 defer、且該 signature 是 `must_detect:false` 的良性探針），
最終是由**閱讀程式碼**而非行為式驗證發現的。追蹤於
[#1181](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1181)。

**補償控制**：生產規則的聚合層由另一組 gate 負責——`rule-pack-{oracle,db2}_test.yaml`
的 promtool fixture 與 vmalert parity gate A **是對真規則（含 recording rule 與聚合）測的**。
兩者互補，缺一不可：本 harness 測「門檻對不對」，那組 gate 測「管線接得對不對」。

## Candidate ↔ 出貨規則同步契約（TRK-339 WS4a-1）

candidate 的 `for:` / severity / raw-metric selector 與出貨 pack **逐條鎖定**，由
`tests/rulepacks/test_reference_validation_sync.py` 強制。刻意分歧（direct-predicate
結構、group interval、未鏡射的出貨 alert、內聯的 record 層 matcher）全部宣告在
[`divergence-ledger.yaml`](divergence-ledger.yaml)——條目本身帶 exit-lock：分歧在
現實中消失或改變時 gate 會咬，必須同步移除／更新條目。兩側都不能靜默漂移
（run#1 就是被靜默漂移咬到：candidate `for:` 自創 5m/6m、零 namespace selector，
而出貨是 30s/60s/3m + `namespace=~"db-.+"`）。

**注入 namespace 紀律**：K8s 波形一律注入 `namespace: db-ref`（落在出貨 selector
／cadvisor scrape keep-list 的 `namespace=~"db-.+"` 面內）；`db-ref` 是參考 fixture
識別子，非真實租戶 id。

**本目錄（tests/）是唯一 canonical 副本**。`dev/waveform-ref/`（未版控 scratch）只
保留 run 產物（inject 報告 JSON、CATCH-RATE-REPORT 等）；規則與波形 fixture 的重複
副本已於 2026-07-24 移除（該批為 prose 翻譯前的英文原稿，語意層經 parse 比對零分岔）。

## Threshold 注入政策（epic F1 決議）

三段量測、各自的門檻來源與身分——**絕不混用**：

| Run | 注入門檻 | 身分 | 用途 |
|---|---|---|---|
| **run#1** | rule-pack 標頭**文件範例值**（Oracle/DB2 不出貨 `_defaults` —— #1175） | **fidelity-corrected 基線，非校準數據** | 保真度修復（WS4a-1）後重跑；只回答「出貨謂詞邏輯接得對不對」 |
| **run#2** | WS3 產出的**候選 defaults** | 候選值驗證 | 加髒語料變體（2–5% dropout + timestamp jitter，外審 F5）；通過才解 #1196-A／#1175 |
| **run#3** | post-migration 實際生效值 | 遷移後重量測 | WS3 遷移落地後的回歸基線 |

⚠️ **波形庫 ≠ spec（防 Goodhart，外審 F5 決議）**：本函式庫是偵測能力的**抽樣
探針**，不是告警行為的規格書。針對「讓某條波形過」去調規則或門檻＝優化到量測
本身，量測即失效；規則變更的正當性只能來自故障語意（vendor 行為、SRE 領域
知識），波形庫只負責事後量測它。

## 重跑（迴歸基線）

需要 `vmsingle`（`:8428`）+ `vmalert`——dev-container 內兩支都已備妥：vmsingle 是
`/tmp/vm/victoria-metrics-prod`、vmalert 是 `/tmp/vm/vmalert-prod`（`inject_waveform.py`
依序探 `$VMALERT` → `PATH` → 後者）。

⛔ **本檔不複製一份安裝指令**——版本與 SHA 只能有一份，手抄第二份正是這個 repo
在消滅的病。可執行的配方在 CI workflow 裡，照抄那裡：

| 要裝什麼 | 唯一可執行來源 |
|---|---|
| `vmalert`（本節用的） | [`nightly-vm-replay.yaml`](../../../.github/workflows/nightly-vm-replay.yaml) 的 `Install vmalert (from the pinned vmutils tarball)` step |
| `vmalert-tool`（⚠️ **本節不需要**，列此僅為指出與上一列 SHA 同源） | [`ci.yml`](../../../.github/workflows/ci.yml) 的 `Install vmalert-tool (VictoriaMetrics MetricsQL unit-test engine)` step（**同一份** vmutils tarball，同一組 SHA-256——下方重跑指令不會用到它） |
| `vmsingle` | `nightly-vm-replay.yaml` 的 `Start pinned VictoriaMetrics (vmsingle, -retentionPeriod=100y)` step（digest-pinned image） |

**版本從哪裡讀**：以上每一處都 `. tests/rulepacks/vm_engine_version` 取 `VM_VERSION`——
那是引擎版本的單一 SSOT（現值見該檔）。但**兩種產物的耦合方式不同，別記成同一件事**：

- **vmutils tarball**：SHA-256 直接與 `VM_VERSION` 耦合——兩支 install step 各自寫死
  `VMUTILS_SHA256`，下載回來的是 `vmutils-linux-amd64-v${VM_VERSION}.tar.gz`，由
  `scripts/ops/_verify_download.sh` 比對；改版號沒改 hash 即 fail。
- **vmsingle image**：與 `VM_VERSION` 耦合的只有 **tag**——`nightly-vm-replay.yaml`
  的 **Guard 1** 斷言 image 字串含 `:v${VM_VERSION}@`。**digest 不由 `VM_VERSION` 決定**
  （同一個 tag 可以被重 pin 到不同 digest，Guard 1 完全看不見），digest 的正確性改由
  **Guard 2** 顧：把該處的 image 字串與 [`vm-anchor-on-pin-change.yml`](../../../.github/workflows/vm-anchor-on-pin-change.yml)
  裡那份**手同步副本**逐 byte 比對，只改一邊就 fail。也就是說 digest 錨的是「兩個
  workflow 跑的是同一批 bytes」，不是「digest 對得上版號」。

bump 程序、以及 dev-container 的 vmsingle 為何來自**另一包** tarball，見
[`backend-compat-baseline.md`](../../../docs/internal/backend-compat-baseline.md)
的「版本 pin 單一 SSOT」條。

⚠️ [ADR-030](../../../docs/adr/030-decision-layer-migration-validation.md) **沒有** VM 安裝
說明（本節原本指向那裡），且該 ADR 已宣告凍結、不再擴充——安裝面的 SSOT 只在上表。

每個函式庫：

```sh
# 驗證 → 注入 → 評分（--rules-delay-s 30 是 for:-alert 的 ALERTS 可見性所必需）
python3 scripts/tools/dx/waveform_compile.py --check <lib>.yaml
python3 scripts/tools/dx/inject_waveform.py --vm-url http://localhost:8428 \
    --vmalert /tmp/vm/vmalert-prod --rules candidate-<engine>.rules.yaml \
    --rules-delay-s 30 --seed 1 --out /tmp/<lib>-inject.json <lib>.yaml
python3 scripts/tools/dx/waveform_score.py /tmp/<lib>-inject.json --tolerances tolerances.yaml
```

## 結果摘要（首次執行，2026-07-19；⚠️ 先於 WS4a-1 保真度修復）

> ⚠️ 下表數字是**保真度修復前**（candidate `for:`/selector 漂移、K8s 波形 ns 在
> 出貨 selector 面之外、#1184/#1188 出貨修正未回鏡）量出來的——僅供歷史對照，
> 不作為基線；基線＝修復後依上節政策重跑的 run#1。

| 指標 | 數值 |
|---|---|
| Recall（Oracle+DB2，作者 1） | 51/67 = **76.1%** |
| Precision（Oracle+DB2） | ≈ **71.8%**（20 個良性案例誤觸發） |
| F1 | ≈ **73.9%** |
| Recall —— Linux-on-K8s | 23/35 = **65.7%** |
| Recall —— 作者 2（n=2） | Oracle 100% · DB2 57.9% |

⚠️ **所使用的門檻是 rule-pack 標頭中的*文件範例*值，不是出貨的作用中預設值**
（Oracle/DB2 的 `_defaults.yaml` 一個都沒帶 —— 見 findings）。Precision 對門檻很敏感：
每一次過度觸發都是一個忙碌但良性的型態超過了偏低的範例門檻。

## Findings → 追蹤中的 issue

- [#1174](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1174) —— Oracle/DB2 覆蓋缺口（hard-parse、lock-wait orphan）
- [#1175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1175) —— ⭐ Oracle/DB2 門檻告警出貨即休眠（沒有 `_defaults` 值）
- [#1176](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1176) —— 文件所載門檻在忙碌工作負載上過度觸發 + deadlock／scale 校準
- [#1177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1177) —— Linux-on-K8s 覆蓋缺口（oomkill-restart、staleness、flapping）

完整報告：[#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948)（ADR-030 RFC SSOT）。
