# Session handoff — 平台告警治理 gate（branch `claude/rule-pack-governance-followup-q0fgur`）

> ⛔ 這是 WIP branch 的暫存筆記，**merge 前刪除**（原本要放 `dev/`，但那是 gitignore 的暫存區，
> 檔案不會進 repo 而容器會被回收，所以暫放這裡）。

**狀態**：28 個 commit，HEAD = `bcb20069`，遠端與本地同步，工作區乾淨。**沒有開 PR。**
`tests/ops` + `tests/lint` + `tests/rulepacks` = **6497 passed / 130 skipped**。

---

## ⛔ 明天第一件事

**剩餘工作只剩一個小項**（parity#2 的非空底線，見下）。S1 / S3 / 16 個 mutant 全部做完。
沒有阻塞項，也沒有待你決定的事。

真正該考慮的是**這個 branch 要怎麼收**：28 個 commit、沒有 PR、最早三個 commit 的 subject
還標著「WIP，勿 merge」。merge 前要刪掉這份 handoff。

---

## 這一段做完了什麼

九個 commit，每一個都是「實測 → 修 → mutation 驗證轉紅 → 推」。

| commit | 內容 |
|---|---|
| `bce3c7bd` | 兩個 `>= 350` 非空底線**真的**換成逐檔覆蓋（上一批宣稱做了、實際沒做） |
| `c8c513fe` | `platform_alert_identities()` 兩個 fail-open 缺口（只讀第一個 data key；expr 的 tenant 偵測漏 suffix 形式） |
| `dc4f7a4c` | 掃描器判準整組沒測試——RVL 的 104 mutant 揭露 69 存活 |
| `2975251e` | 更正上一條的存活數（我寫「降到 4」，那是估的） |
| `95a64a36` | `items:` 蓋掉文件自身規則 → 交給 silent-zero 哨兵 |
| `9db73247` | UTF-16 存的 manifest 部署得了但掃描器讀不到 |
| `e90d89bb` | Watchdog 契約豁免改由 route 推導 |
| `1cace594` | 補完 mutation：解析層 15 個守衛全無 fixture |
| `6d20740c` | 這份 handoff |
| `14fd1384` | `_norm_expr` 把正規化套進字串字面量裡（S3；實測全樹 no-op） |
| `bcb20069` | helm chart 宣告的規則一律拒收（S1 選項 iii）＋ 補完剩下的 mutant |

### 花錢買到、值得保留的事實

**kubectl 解碼器的實測行為**（用 `k8s.io/apimachinery` 1.36 寫小程式直接餵，
不是推論；`kubectl apply --dry-run=client` **問不出來**，它在 flatten 之前就要 cluster discovery）：

| 形狀 | `kubectl apply` 實際部署 |
|---|---|
| `kind: ConfigMap` + `data` | 1 個物件，data 保留 |
| `kind: ConfigMap` + `items: []` + `data` | **0 個物件** |
| `items` 非空 + 自己也有 `data` | 只有 items 裡那些 |
| UTF-8 / UTF-8+BOM | 部署 |
| **UTF-16 BE + BOM** | **部署** |
| UTF-16 LE + BOM | `incomplete UTF-16 character`，不部署 |

BE 過 LE 不過，是因為 `yaml.NewYAMLReader` 以 `\n` **位元組**切文件——
big-endian 下切在 pair 邊界，little-endian 下切在 codepoint 中間。

複現用的探針還在 `scratchpad/decoder/` 與 `scratchpad/utf16/`（容器回收後會消失，
但 `go mod tidy` + 20 行就能重建，方法見 `9db73247` 的 commit message）。

---

## 剩餘工作（依建議順序）

### ~~S3~~ — 已完成（`14fd1384`）

缺陷比原本記的更寬：不只 `#`，空白／逗號／括號／比較運算子在字串字面量內也全部被改寫，
三種引號形式皆然。已改為單一 left-to-right alternation（字串三式 ＋ 行註解一起掃），
字串以 NUL 定界索引暫存。全樹 930 條 expr 修前修後逐條相同。

### ~~S1~~ — 已完成（`bcb20069`，選了 (iii)）

`_rules_shaped_at_any_depth()` 遞迴拒收 helm 裡的規則形狀（帶真規則的 `groups:` mapping、
以及 template 會自己包 `groups:` 的裸 rule list），並釘了五個反向對照防誤報。
實測全 repo 目前 0 個 helm YAML 是規則形狀。

### ~~16 個仍存活的 mutant~~ — 已完成（`bcb20069`）

殺掉 9 個（`_generated_pack_names` 四道守衛、`_SCAN_SKIP_PARTS` 空集合、副檔名大小寫、
`python -O` 下的 pack 唯一性 `raise`、`_tracked_yaml_paths` 新加的空集合防護、`A32`）。
順帶發現 `_tracked_yaml_paths` 少了 `_expected_rule_files` 那道空集合防護——git 失敗時
靜默回傳 `[]`，`check_scrape_reachability --ci` 會對一棵從未打開的樹印「0 DEAD」。已補。

剩下 8 個**逐一可證明等價**，不要再花時間：`A85`（control）、`A44`（恆真式）、
`B07`（由已測過的唯一性 `raise` 保證）、`A30` / `A22`、`A04`（`is_file()` 多餘，對目錄
`read_text()` 拋 `IsADirectoryError`，屬既有 `OSError` catch）、`A40`（`check=True` 被
空集合防護涵蓋）。

### 1. 唯一剩下的小項

- `TestPlatformReaderParity` 的 parity#2 只有 `assert prod`（≥1），沒有數值底線，
  而它的結論已被 parity#1 + `test_runtime_default_is_the_derived_set_not_a_sample` 蘊含。
  要嘛給它 `_MIN_PLATFORM_ALERTS`，要嘛在 docstring 講明它是推論式。約 15min。
- `test_check_pint.py` 用 regex 讀 `_rule_tree.py` 常數而不 import——模組註解已寫明那是刻意的
  （要跟 `.pint.hcl` 的 include pattern 鎖步）。**判定不必動。**

### 不必再做的

上一版 handoff 的 RV8 清單（S6/S7/S8/S9/N4-N7）**全部已處理或已判定**：
`kind: List` 與 binaryData 已修並有 fixture、ledger 改名頂替已改釘 expr digest、
runbook 越界行號已修、三支契約的非空底線已補。

RVL 那 69 個存活的最終帳：**61 已驗證被殺、8 逐一可證明等價**（清單見上）。

---

## ⛔ 我這一段犯過的三個錯（同一種形狀，請保持警覺）

三次都是**把沒實測的宣稱寫進 commit message / 註解**：

1. `211c7fa5` 宣稱「兩個 350 換成逐檔覆蓋」——實際只拿掉 `>= 30`，兩個 350 一字未動，
   而 `_expected_rule_files` 的 docstring 照著那個宣稱描述了一個沒發生的修復。**外審抓到。**
2. `dc4f7a4c` 宣稱「存活 mutant 從 69 降到 4」——那個 4 是從「我補了幾支測試」反推的。
   實數是 29 殺 / 13 活 / 27 未測。**自己補測時抓到。**
3. `_decode_manifest` 的第一版用 `raw.decode("utf-16-le")`，那**不剝 BOM**
   （只有 `utf-8-sig` 與無 endian 的 `utf-16`/`utf-32` 會），等於把我在同一段註解裡
   描述的 provenance bug 從一種編碼擴散到四種。**我自己新寫的測試抓到，不是想出來的。**

**規則**：任何寫進 commit message 或註解的數字／因果宣稱，都要有一條當場跑過的指令當證據。
「我改了 X 所以 Y 應該變成 Z」不算。

外審也有講錯的（S2 被指認為「可部署的繞過」，實測 kubectl 根本不部署那份文件）——
**兩個方向都要驗**，不要照單全收，也不要因為它錯過一次就打折。

---

## 有用的作業方式（建議沿用）

1. **盲審 worktree**：`git worktree add --detach /home/user/wt-<n>-q0fgur <branch>`，
   三個 reviewer 各一個、各給不同 lens（演算法正確性 / red-team / CI script fail-open）。
   ⚠️ **不要在 reviewer 跑完前清掉 worktree。**
2. **prompt 必寫**：「diff 內新增的註解 / docstring / CHANGELOG 全部是**被審對象**，不是證據」。
   另外必寫「不要對工作區用 `git checkout`」——**我自己犯過一次，毀掉整輪修改。**
3. **反事實四步**：注入 → 確認舊碼放行 → 修 → 確認轉紅 → 還原 → 確認不誤報。
   缺「確認不誤報」就會出誤報（N1/N2、以及這輪 `utf-8-sig` 差點被報成 offender）。
4. ⚠️ **清 `__pycache__`**：等長的程式碼編輯會讓 `(mtime, size)` 判定未變更而重用舊 bytecode，
   反事實會拿到**假綠**。`find . -name __pycache__ -type d -exec rm -rf {} +` +
   `PYTHONDONTWRITEBYTECODE=1 python3 -B`。
5. **mutation 腳本的形狀**（`scratchpad/sweep.py`，值得重建）：
   - **先驗證所有 anchor**（缺漏／多重命中都在動任何一行前中止）。實測攔下一個多重命中
     （`A75` 的字串在 `_alert_names` 與 `_iter_repo_alert_rules` 各出現一次）。
   - 結果**逐筆 flush** 到 log 檔（`buffering=1`），還原放 `finally`。
     上一版就是一筆格式錯誤中途炸掉且沒還原，工作區留著 mutant。
   - 每輪都放兩個 control：一個**語意相同**（必須存活，否則 harness 太敏感）、
     一個**災難性失明**（掃描器回傳空；必須被殺，否則 harness 不可信）。
   - 速度：約 17s/mutant（`tests/ops/test_generate_routes_orchestration.py` +
     `tests/lint/test_check_pint.py`，`-x`）。29 個約 8 分鐘。

---

## 環境

- 沙箱缺 `hypothesis` → `tests/ops/test_property_based.py` collection error，**與本變更無關**
  （`a2503a07` 亦然）。跑 `tests/ops tests/lint --ignore=tests/ops/test_property_based.py`。
- 缺 `pytest-randomly` / `pytest-timeout` / `pytest-xdist` → 順序相依只能手動打散驗。
  **「本機全綠」不等於「CI 全綠」。**
- 沒有 `pre-commit` 執行檔。替代做法：parse `.pre-commit-config.yaml`，挑出 `files:` regex
  命中改動檔的 auto-stage hook，直接跑它們的 `entry`（`pygrep` 語言的要當 regex 跑，
  不是當指令跑）。本輪每次 commit 前都這樣做，15-20 個 hook。
- 沒有 `kubectl` / `helm` / `ruff`。kubectl 可以現抓（`dl.k8s.io`），Go 可用（1.24.7），
  proxy 下 `go mod tidy` 會通。
- commit scope enum 不含 `gates` / `changelog`，用 `ops`。commit-msg hook 會擋。
