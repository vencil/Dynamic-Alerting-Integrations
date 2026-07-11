# Air-gap 自助遷移驗證 runbook（決策層 catch-rate，零支援 / 零 egress）

> 適用：受測遷移方環境 **air-gapped 或受限 egress**、且**無法提供我方現場彈性**、
> **資料/內容不得出關**。本 runbook 讓對方領域專家（SME）在其環境內**完全自助**跑
> ADR-030 決策層驗證（fault-waveform 注入 → catch-rate → 三態 verdict），全程資料不出。
> 承 [ADR-030](../adr/030-decision-layer-migration-validation.md)、[waveform 擷取 SOP](waveform-elicitation-sop.md)。

## 0. 核心原理：harness 過去找資料，不是資料出來找 harness

catch-rate 報告本質是**遷移方自己的 go/no-go 決策儀器**，不是我方產物。故：

- **我方從不接觸波形庫**——SME 在 air-gap 內盲寫故障簽章、本地編譯注入、本地判定；
  波形庫、候選規則、觀測資料**一步都不出關**。
- 出關的**至多**是一份 `--redact` 出關安全報告（verdict + 計數 + 比率，零識別項），
  或一紙人類簽署 attestation，或**什麼都不出**（純內部儀器）。三者遞減、都可行。
- 這不是妥協——是「製造 oracle 非觀測」設計的天然對齊：從**不需要**他們的資料。

## 1. 離線 bundle（送進去，過對方受控 ingress）

單一自帶包，內容 + 對方資安可逐行審（純 Python + OSS 靜態 binary，非黑盒）：

| 類 | 內容 |
|---|---|
| 工具 | `scripts/tools/dx/waveform_compile.py`、`inject_waveform.py`、`waveform_score.py` + 依賴 `_waveform_lib.py`、`tests/rulepacks/vm_harness.py`、`_lib_*.py` |
| 引擎 | pinned `victoria-metrics` + `vmutils`（vmalert）**靜態 binary**（單檔、離線）+ SHA-256 checksum |
| schema/範本 | `docs/schemas/waveform-pack.schema.json`、`waveform-tolerances.schema.json`、SME 波形填空範本、容差矩陣範本 |
| 自測 seed | `selftest_*` packs（驗工具鏈本身，不含任何遷移方資料） |
| 文件 | 本 runbook + 擷取 SOP |

**⛔ 交付機制＝engagement 前置決策，不可預設「丟一包 loose wheel」**：vmsingle/vmalert 是
單檔靜態 Go binary（無依賴、`sha256sum -c` 後直接跑）；問題在 **Python 依賴**——`jsonschema`
鏈到 `referencing → rpds-py`（**Rust native 擴充**），loose `.whl` 綁 OS 發行版 / glibc / CPU
架構，**不知道對方隔離機器的確切平台就打包幾乎一定裝失敗**。故：

1. **先做平台探查**（前置）：對方隔離機器的 OS/glibc/arch + 有無容器 runtime（docker/podman）。
2. 依探查結果選交付（**tradeoff 明列，讓 engagement 決定**）：
   - **OCI image tarball**（`docker save` → `docker load`）＝最可靠、消滅環境不一致；但**較不可
     逐行審**（layer/base-image 供應鏈不透明，弱化 §2 的可審性賣點）＋需對方有容器 runtime。
   - **平台匹配的離線 venv**（在**與對方平台一致**的機器上 build 好整個 venv 目錄再打包）＝**保留
     §2 純 Python 可逐行審**；但需先知道平台、且 rpds-py 的 wheel 要 arch 對。
   - loose `pip install --no-index <一堆 .whl>`＝**只在確知平台且 wheel 全 arch 匹配時可用**，
     否則勿用（此前版本誤把它當預設，實戰會炸）。
3. 起隔離 vmsingle（`-retentionPeriod=100y`、`-httpListenAddr=:8428`）。

## 2. no-network 信任聲明（對方資安審查用）

工具**零 phone-home**：`waveform_compile` / `waveform_score` 完全無網路；所有網路呼叫
只在 `vm_harness`、且**只打本地 vmsingle 的 `base_url`**（預設 `localhost:8428`），無任何
硬編外部 URL。對方可自審——**只掃 §1 bundle 的那幾支檔**（勿用 `dx/*.py` 通配，那會掃到
非 bundle 的工具、噴出不相干的外部 URL 而誤判）：

```
grep -nE "urlopen|urllib|http|socket|requests" \
  scripts/tools/dx/waveform_compile.py scripts/tools/dx/waveform_score.py \
  scripts/tools/dx/inject_waveform.py tests/rulepacks/vm_harness.py
```

→ 輸出只剩 `import http.client` / `import urllib.*` 與目標為 `{base_url}/...` 的呼叫，
無任何外部主機。可於無出向網路的隔離網段執行以進一步佐證（工具不會因斷網失敗，除了
連不上本地 vmsingle）。

## 3. SME 自助 author（無我方引導）

範本「每欄上方即訪談問句」——SME 依領域知識填故障簽章（形狀 / normal-fault / onset /
底噪 / 貼閾值震盪）。**回讀簽核閘（載重、取代我方審）**：

```
python3 scripts/tools/dx/waveform_compile.py --render-readback pack.yaml
```

印 ASCII 波形 + 中文摘要——SME **自己確認「這波形是不是我要表達的故障」**。這一步在
air-gap 無我方複核時是唯一防「盲寫走樣」的閘，不可跳。簽核後 `--compile --out D`。

## 4. 跑 + 判讀

```
python3 scripts/tools/dx/inject_waveform.py pack.yaml --rules <候選VM規則路徑> --out inject.json
python3 scripts/tools/dx/waveform_score.py inject.json --tolerances tol.yaml --json
```

（`--out` 落檔供 score 讀；`--json` 只印 stdout、不落檔。）

三態 verdict：**PASS**（injected-set 內零漏接、零遮蔽）／**FAIL**（任一 must_detect 漏接、
exit 1）／**INDETERMINATE**（漏接被聚合規則剝 label 遮蔽、exit 1、需人工覆核）。
另揭露（不改 verdict、供調規則）：`early_onset_fire`（規則過度敏感、故障成形前就叫）、
`flapping_suspected`（fire↔resolve 斷續震盪）、`fan-out ratio`（單故障齊發幾條 series）。

⚠️ **verdict 不是安全保證**：只涵蓋 injected fault set，對未注入 long tail 零保證，單獨
**不得**當 cutover go-signal（見報告 scope disclaimer）。

## 5. ⛔ Escape hatch：範本涵蓋不到的故障形狀

範本的 `shape_class` enum 只有 **4 個基本形狀 `ramp / spike / plateau / step`**（另有
`dips_back` 布林 + `dip_detail` 修飾「掉回」行為，及編譯期自動產生的震盪/flapping 變體——
這些不是 SME 可填的 shape_class）。真實故障若不落在這 4 個基本形狀（如多階段複合、
狀態機式），**不要硬套一個近似形狀**——那會製造一個「能踩閾值但不像真故障」的波形，
catch 了也無意義（同義反覆）。正確做法：

- 該故障類**標為「工具邊界外」**，走**人工邏輯審查**（比照合規類 carve-out 的 dual-control），
  在報告外以人工紀錄，**不**假裝計入 catch-rate。
- 這是誠實限制、非失敗：製造-oracle 只驗「注得出的已知失效模式」；驗不到的顯性列出、
  不靜默漂綠（承 ADR-030 D8「no-silent-caps」）。

## 6. 出關（三選一，遞減）

1. **`--redact` 出關安全報告**（首選，若對方肯放一份極貧資訊物）：
   ```
   python3 scripts/tools/dx/waveform_score.py <報告> --tolerances tol.yaml --redact --out egress.json
   ```
   只含 verdict + 計數 + 比率 + scope disclaimer；**剝除全部識別項**（alertname / metric /
   fault_class / labels 拓撲 / 檔案路徑 / 容差 class 名與 approved_by / per-case 明細 /
   warnings）。allowlist 重建，未來新欄也不外洩。對方資安出關前 eyeball 即可。
   **⚠️ 此報告僅供 Vibe 確認遷移「總體健康度」，不是客戶內部合規/稽核佐證**——approved_by /
   justification 審計軌跡已被剝除，SOC2/內稽會退件。客戶合規單位須在**隔離段內看未加
   `--redact` 的完整 JSON** 核對容差與 carve-out 的簽核軌跡（Gemini #1079 盲區3）。
2. **人類 attestation**（連一行都不放）：對方 cleared 人員簽署——但**不可只簽「PASS / N=K」**
   （空洞簽核：只跑 `selftest_*` seed 或 1 個簽章也能得 PASS，我方拿到無意義的免死金牌）。
   **attestation 範本強制填四個核心分母**——`must_detect_total` / `scored_denominator` /
   `indeterminate` / `carved_out`（這四個都在 `--redact` 報告白名單內、可直接抄），且**事前雙方
   議定各 pack 對關鍵告警類（Oracle/DB2 核心告警等）的預期規模**；唯有實際分母符合預期，
   這份 PASS 才具效力（Gemini #1079 盲區2）。
3. **零出關**：純內部儀器，幫遷移方自己決策；我方 ratify 走別的客戶或 selftest demo。

## 7. 不可化約的限制（誠實記錄）

零支援 + 零 egress 下，**unknown-unknowns 我方抓不到**：工具的 teeth-test 只驗已知失效
類，範本沒預期、teeth 沒覆蓋的新失效會靜默過關。工具能自守已知類（D8.4 fail-loud 三正控
＝它自己的安全網），但驗不掉自己沒想到的類——這是製造-oracle 方法的遞迴限制，無論怎麼
設計都在。故本 runbook 的 §5 escape hatch 與 §4 的「verdict 非安全保證」是**必讀**、非選配。
