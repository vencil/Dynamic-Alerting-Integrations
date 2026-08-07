# Session handoff — 平台告警治理 gate（branch `claude/rule-pack-governance-followup-q0fgur`）

> ⛔ 這是 WIP branch 的暫存筆記，**merge 前刪除**（原本要放 `dev/`，但那是 gitignore 的暫存區，檔案不會進 repo 而容器會被回收，所以暫放這裡）。所有 commit subject 都標了「WIP，勿 merge」或屬於同一批未完成工作。

**狀態**：10 個 commit，全部已推（遠端 = 本地 = `a59da668`，以 `git ls-remote` 驗過）。工作區乾淨。**沒有開 PR。**

---

## ⛔ 明天第一件事

**最後一批（`a59da668`）完全沒有經過盲審。** 前六輪盲審，每一輪都在我剛寫好、剛自認驗過的 code 裡找到 High——沒有一次例外。這一批動的是判準的核心邏輯，形狀跟前幾次一樣危險。

我自己看得出的兩個可疑處，優先查：

1. **`_source_pack_alerts()` 用「告警名」做集合比對** — 告警名會撞。兩個不同 pack 若有同名告警，`mine <= pack_alerts[claimed]` 可能誤判成立。
2. **「任一 pack 的父集」那個 fallback**（`any(mine <= alerts for alerts in pack_alerts.values())`）— 一個只含**一條**常見告警名的手寫 artifact，可能被任何剛好含該名的 pack 認領，等於自動取得 generated 豁免。**這條 fallback 是我為了修 N1 誤報加的，很可能開了新洞。**

---

## 這輪做了什麼（六輪盲審 + 修補）

起點是三個 WIP commit（runbook 覆蓋率 gate、slugger 重寫、mkdocs anchor ledger）。之後每一輪都是「盲審 → 修 → 再審」。

**盲審抓到而我沒抓到的（節錄，全部已修）**：

- 我的 anchor ledger 在「債為 0」時印一句**祈使句**叫維護者刪掉整段 filter；照做之後整條 warning 管線死透且轉綠
- 我寫在註解裡的「digest 不新增脆弱性」被一次**良性編輯**否證（改標題＋同批改它自己的 TOC 連結）
- HTML 註解剝除放在 fence 判斷之前 → 一段普通的 markdown 範例讓 checker 從 0 變 30 個假 broken anchor
- 誘餌檔用真值判斷 → 合法的空產物（`groups: []`）讓 gate 要求**租戶**告警加 `alert_source: platform`（RESERVED 值，會灌進 NOC）
- provenance 對整檔做無錨點 substring 搜尋 → 標頭放在 annotation 值裡就生效
- `_PLATFORM_PACK_ENTRIES` 的註解宣稱「lockstep 測試會回讀」——**沒有這種測試**
- `_tracked_yaml_paths` 用 `git ls-files` 沒有 `-z`，而**同一個檔案的兄弟函式早就用了**
- pint 的 tripwire 與測試**互相鎖死**：照 CI 自己的指示 ratchet 必然弄紅測試

**正面證據**（reviewer 給的，值得保留）：slugger 重寫用 `github-slugger@2.0.0` 對全 307 份 md / 5725 個 anchor 比對，**新實作 0 mismatch**（舊的 226 files mismatch / 1290 個漏產）。

---

## RV8（第五輪紅隊）尚未處理的

它審的是 `ee6accef`，之後我推了 `0e61f096` / `86ad46c5` / `a59da668`。**下列需要重新確認是否仍活著**：

| # | 內容 | 我的判斷 |
|---|---|---|
| S6 | `kind: List` 包裝（`kubectl get -o yaml` 的原生形狀）三個 `elif` 都不匹配，doc 無聲落地 | **應該仍活著**，但現在只在部署目錄內 |
| S7 | `except yaml.YAMLError: continue` — Helm template 化的規則永久靜默 | 部分處理（含 `- alert:` 的檔會被 `_rule_shaped_but_unparsed` 抓），但整檔跳過仍靜默 |
| S8 | ConfigMap 的 `binaryData` 完全不看 | **仍活著** |
| S9 | ledger **改名頂替**（把舊的改名並補 runbook，新的接收該名字） | **仍活著**——我擋的是重名，它用的是改名 |
| N4 | `spec.spec.groups` 差一層就靜默（不可部署，但守門員宣稱的不變量不成立） | 仍活著 |
| N5 | 掃描來源從檔案系統換成 git index 是覆蓋面**收窄**（未 tracked 但會被 `kubectl apply -f` 收到的檔案） | 仍活著，嚴重度低 |
| N6 | 非 md runbook：無 anchor 的 `.yaml`、越界行號 `#L999999` 仍放行 | 仍活著 |
| N7 | 空掃描下三支契約 vacuous 綠（suite 整體是紅的，但那三支自己沒有非空底線）。`test_every_platform_alert_has_a_derivable_plane` 最該補 | 仍活著 |

RV8 也提了一條**未能查證、建議實測**的：若有人用 Flux `kustomize-controller` 指向這些目錄（無 `kustomization.yaml` 時會遞迴 autodetect），`_SCAN_SKIP_PARTS` 就從設計極限降級為可部署繞過。

---

## 已知邊界（刻意保留，已寫進 `_is_platform_cm_location` docstring）

- 裸 `groups:` 文件只在 `rule-packs/` 下辨識——這是**目錄判準**，與「內容而非位置」矛盾。保留是因為該形狀與 `tests/rulepacks/` 的 26 個 ADR-025 extract 無法區分。⚠️ **不能單獨拿掉**：一拿掉，那些 extract 的告警名會與平台 ConfigMap 重複，撞上重名斷言。
- PrometheusRule 的 provenance 可由物件名取得，名字不擔保內容（`a59da668` 之後由內容比對兜底，但見上方可疑處 2）。

---

## 有用的作業方式（建議沿用）

1. **盲審 worktree**：`git worktree add --detach /home/user/wt-<n>-q0fgur <branch>`，三個 reviewer 各一個、各給不同 lens（演算法正確性 / red-team / CI script fail-open）。⚠️ **不要在 reviewer 跑完前清掉 worktree**——我清太早，害三個 reviewer 中途要改用 `git show <sha>:<path>`。
2. **prompt 必寫**：「diff 內新增的註解 / docstring / CHANGELOG 全部是**被審對象**，不是證據」。這句話是這輪最有效的一句。
3. **反事實紀律**：注入攻擊 → 確認舊碼放行 → 修 → 確認轉紅 → 還原 → 確認不誤報。四步缺一不可，我漏掉「確認不誤報」時就出了 N1/N2 那種誤報。
4. ⚠️ **清 `__pycache__`**：等長的程式碼編輯（如 `>=` 改 `==`）會讓 `(mtime, size)` 判定未變更而重用舊 bytecode，反事實會拿到**假綠**。用 `find . -name __pycache__ -exec rm -rf {} +` 加 `PYTHONDONTWRITEBYTECODE=1 python3 -B`。這是 reviewer 發現並回報的。

---

## 環境

- 沙箱缺 `hypothesis` / `_cffi_backend` → `tests/` 全域有 4 個 collection error，與本變更無關。跑 `tests/ops tests/lint tests/rulepacks tests/helm` 即可（6498 passed / 231 skipped）。
- commit 簽章伺服器偶發 503 → 重試即可（我遇過一次，第 2 次成功）。
- commit scope enum 不含 `gates`，用 `ops`。
