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

## 重跑（迴歸基線）

需要 dev-container 內的 `vmsingle`（`:8428`）+ `vmalert-tool`/`vmalert`（見 ADR-030
harness 的 VM 安裝說明）。每個函式庫：

```sh
# 驗證 → 注入 → 評分（--rules-delay-s 30 是 for:-alert 的 ALERTS 可見性所必需）
python3 scripts/tools/dx/waveform_compile.py --check <lib>.yaml
python3 scripts/tools/dx/inject_waveform.py --vm-url http://localhost:8428 \
    --vmalert /tmp/vm/vmalert-prod --rules candidate-<engine>.rules.yaml \
    --rules-delay-s 30 --seed 1 --out /tmp/<lib>-inject.json <lib>.yaml
python3 scripts/tools/dx/waveform_score.py /tmp/<lib>-inject.json --tolerances tolerances.yaml
```

## 結果摘要（首次執行，2026-07-19）

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
