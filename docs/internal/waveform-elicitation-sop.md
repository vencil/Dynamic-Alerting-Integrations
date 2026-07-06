---
title: "Fault-Waveform 擷取與轉譯 SOP（ADR-030 決策層驗證）"
tags: [internal, sop, migration, validation, rule-packs]
audience: [platform-engineer, maintainers]
version: v2.9.0
lang: zh
---

# Fault-Waveform 擷取與轉譯 SOP

[ADR-030](../adr/030-decision-layer-migration-validation.md) 決策層遷移驗證（製造 oracle）PR-1 的操作手冊：如何從領域專家（SME）擷取故障簽章、轉譯成 waveform pack、通過盲寫治理三閘、物化成注入素材。工具本體為 `scripts/tools/dx/waveform_compile.py`（單工具三模式：`--check` / `--render-readback` / `--compile`），spec 為 `docs/schemas/waveform-pack.schema.json`，SME 填空範本（即 R2-5 問卷本體）為 `scripts/tools/dx/waveform_pack_template.yaml`。

## 為什麼要這套流程（30 秒版）

遷移驗證的 ground truth 用「製造」不用「觀測」（ADR-030 D2）：對新 VM 規則集注入已知故障的 metric 波形、看接不接得住。這件事最大的失效模式是**同義反覆**——如果波形是從規則閾值反推出來的，「規則踩了自己造的閾值波形」對「接不接得住真故障」零資訊。本 SOP 的全部治理設計都在防這一條。

## SME 擷取：雙軌

| 軌 | 適用對象 | 流程 |
|---|---|---|
| **問卷訪談＋轉譯**（主軌） | 會 SQL 不會 YAML 的 DBA/SME | 平台工程師照範本內每個 SME 欄上方的問句訪談，把自然語言回答轉譯成 YAML |
| **直填 YAML**（輔軌） | 願意寫 YAML 的 SME | SME 直接照範本填；平台工程師只補平台欄 |

兩軌的欄位切線相同：**SME 語意欄**（description / source / normal_level / fault_level / onset_duration / hold_duration / typical_wobble / dips_back（+dip_detail）/ agent_keeps_reporting / must_detect）全部可用自然語言回答；**平台欄**（metric / metric_kind / companion_series / shape_class / noise_kind / time_axis / fault_class / labels）由平台工程師或編譯器補。validator 對缺漏的報錯前綴也照這條線分流：SME 欄報「退回 SME」、平台欄報「平台補填」——退回問卷時只拿 SME 欄去問，不要拿平台欄去燒 SME 的耐心。

訪談提示：

- 水位一律要**原生單位**與**真實幅度**（「故障時真的會到多少」），不是警戒線。
- 方向中性：故障水位可高於也可低於正常（命中率下墜與磁碟爬升同樣合法）。
- `agent_keeps_reporting` 是 SME 答得最好的時間軸題：「壞掉的當下，監控數據還在不在？」
- `must_detect` 的問法就是「半夜發生想不想被叫醒」——這是 catch-rate 的 ground truth。

## 盲寫治理三閘（⛔ 全部強制）

1. **轉譯者 ≠ VM 規則轉換者**。把 SME 回答轉譯成 YAML 的平台工程師，不得是撰寫或轉換該批 VM 規則的人。pack 內 `independent_of_rule_conversion: true` 是這條的 attestation（schema 鎖 const true，填 false 直接紅）。
2. **回讀簽核**。轉譯完成後跑 `--render-readback`，把 ASCII 波形圖與中文摘要唸給／拿給 SME 確認「這就是我描述的故障」，確認後才在 pack 標 `readback_signed_off: true`。這一閘防的是轉譯者無意間把自己的預期塞進語意。
3. **簽核前 SME 不看規則與閾值**。訪談與回讀全程，SME 不得接觸 VM 規則、告警閾值、rule pack 文件。獨立性保護的是「不知道規則長什麼樣」，不是「親手打 YAML」。

## 波形包資料治理

- **Engagement 波形包不進 repo**。SME 擷取的真實故障簽章屬 engagement 資料；工具吃外部路徑（`waveform_compile.py --check /path/to/pack.yaml`），repo 只收工具、schema、範本與 self-test seed。
- **Seed 治理**：`source: self-test-seed` 的 pack（`tests/dx/fixtures/waveform/`）只供工具自測——編譯器對它 exit 1，除非帶 `--allow-selftest`；報告器（PR-3）強制把它排除出 catch-rate 分子分母；seed 一律用 `selftest_*` 假 metric 名，不得與任何 rule-pack metric 重名。
- **source enum 是反循環的一部分**：real-incident / vendor-doc / expert-experience 三值記錄簽章的真實根據；沒有「從規則推導」這個選項是刻意的。

## 判定權威與兩物化

`--compile` 每個 pack 產出三個檔：

| 檔 | 物化 | 角色 |
|---|---|---|
| `<id>.promtool.yaml` | (a) promtool fixture 片段（`values:` 記法、`_`＝掉點） | **僅參照**——Prometheus 行為對照、divergence-explanation 輸入 |
| `<id>.vm.txt` | (b) Prometheus import 文字行（絕對 ms 時間戳） | **判定權威**——餵 VM / vmalert-replay，catch-rate 以此為準 |
| `<id>.metadata.json` | 留痕 | 變體種類、expects 繼承、seed、自動調製軌跡 |

兩物化對 dropout／staleness 的判定行為必然不同（取樣語義不同），故 (a) 永不進 catch-rate 判定；jitter 只存在於 (b)——(a) 的 `values:` 記法結構上表達不了 per-sample timestamp，遇 jitter 會在片段頂部帶顯性「不含 jitter」標註。取樣間隔釘死 `STEP=30s`、時間原點 `T0=1700000000`，寫進每份物化的 metadata。

## 變體與 expects 語義（強制、無開關）

編譯器對每個 signature 永遠生成：底噪疊加（boolean 豁免、改 flapping）、震盪（`dips_back: true` 用 SME 的 dip_detail；`false` 仍生成**全深度掉回 normal_level** 的機械震盪探針）、多 series fan-out（label 由 index 派生）。

- 源自 SME 宣告行為的序列**繼承 `must_detect`**＝進 FN=0 分母。
- **純機械探針**（SME 說不會震盪、編譯器仍造的全深度震盪；agent 明明持續回報仍造的 absence）標記 `probe`＝**不擋 verdict、但必列報告**——probe 漏接是「此 alert 對震盪回落／absent 盲區脆弱」的顯性警訊，不是 FN。
- 所有自動調製（boolean 噪音豁免、counter 負速率 clamp、探針生成）都在 metadata `auto_adjustments` 留痕；不可調製即 fail-loud，禁止靜默降格。

## 已知限制（v1；對抗 review disposition）

- **companion_series 分母為常數水位**：比值/join 型的伴隨 series（`role: denominator`）v1 以常態值物化，**不隨主 series 的 `metric_kind` 積分**。多數 infra 比值（利用率＝當前值／上限）分母本就是穩定 gauge，此簡化成立；但若下游規則的分母本身是 counter（需 `rate()`），其比值語義在 v1 會失真。→ defer-with-trigger：首個「counter 分母」比值型 signature 出現時，補分母積分。
- **`--compile` 多 pack 批次非原子**：一次傳多個 pack 時，若某 pack 驗證/物化失敗，前面已成功的 pack 檔案**仍留在 `--out`**、整批回 exit 1。消費 `--out` 前務必先確認 exit code == 0；或一次只編一個 pack（推薦，也利於逐 pack 回讀簽核）。

## PR-2 佔位條款（閾值 fixture 治理）

PR-2 的注入 harness 需要閾值 fixture 時，**一律取出貨 `_defaults.yaml` 的值**、且該 fixture 須在任何 engagement 波形入庫**之前** pre-commit——順序反過來（先看到波形再挑閾值），反循環就從閾值端繞回來了。

## 快速指令

```bash
# 驗證（schema + 治理閘；0=過、1=違規、2=呼叫/環境錯）
python3 scripts/tools/dx/waveform_compile.py --check pack.yaml

# SME 回讀簽核用輸出
python3 scripts/tools/dx/waveform_compile.py --render-readback pack.yaml

# 物化（決定性：同版本＋同 seed＝bitwise 相同）
python3 scripts/tools/dx/waveform_compile.py --compile --out out/ --seed 1 pack.yaml
```

測試在 `tests/dx/test_waveform_compile.py`；promtool round-trip 平時 skipif，CI/容器環境可設 `WAVEFORM_PROMTOOL_REQUIRE=1` 把缺 promtool 變硬性失敗（防 skip-to-green）。
