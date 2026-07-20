---
title: "Engagement 去識別化政策"
tags: [governance, security]
audience: [maintainer]
version: v2.9.0
verified-at-version: v2.9.0
lang: zh
---
# Engagement 去識別化政策

> dev-rules §E 的細節文件。**前提：本 repo 與其 GitHub issues 皆為 PUBLIC。**

## 為什麼是 pre-publication gate（不是事後塗改）

公開寫入實質**不可逆**——GitHub 會被索引、fork、存檔。曾實際發生：issue body 的 revision history 只能靠 GitHub UI 手動逐筆刪除，且不保證清乾淨。⇒ **沒有「先寫再撤」，只有發布前把關。**

## 三層分類

| 層 | 內容 | 去處 |
|---|---|---|
| **A 預設公開** | 技術/產品產出、**我方自身**的覆蓋 gap + 處置、方法論、reference fixtures | repo / issues |
| **B 僅限私有** | engagement 身分、案量、對方技術棧／時程／弱點／設定 | repo 樹**外** |
| **C 僅限通用化** | 動機脈絡——寫成能力式（「面向跨引擎遷移」），**永不帶合取** | repo |

## ⛔ 合取規則（核心曝險）

`{案量, 產品組合, 被退役的來源平台, air-gap 姿態, 時程}` 之中**任兩項不得同時出現在同一公開處**。

單項皆通用；**合取在小市場可能 k=1**（k-anonymity——經典結果：ZIP+生日+性別即可識別多數人）。所以塗黑技術內容沒有用，**要避免的是「合取出現在同一處」**。

**踩線的不是詞彙本身，是「斷言存在一個進行中的、特定的案子」。**

反例（已修）：`the Oracle pack is an active <platform>-to-VM migration target`
正解：`one of the cross-engine migration reference packs`
技術意義一字未減，合取消失。

## 我方 gap vs 對方弱點（計算方式不同）

- **我方**的覆蓋 gap → **應該公開**（公開 postmortem 是建立信任的業界常規），但須**與處置一起公開**（gap + 已開的 issue）。
- **對方**的弱點 → **永不公開**，即使去識別化——因為合取規則會把它接回去。
- ⚠️ 若某項 finding 屬**安全性**而非監控品質，計算翻轉為 CVD（責任揭露）排序：先修或先有計畫，再公開。

## 為什麼刻意不做關鍵字 denylist

實測本 repo：來源平台的 **10+ 處提及全屬無害**（log sink 範例／secret-token 白名單／泛用 multi-region 情境／「如 X」類別舉例），真踩線僅 3 處。反向也成立——把根目錄草稿按關鍵字計數排序，**hits 最高的三個檔案反而完全不敏感**（泛用設計語言），真正敏感的問卷排在後面。

⇒ 封鎖詞彙會產生幾乎全為假陽性的噪音、淹沒真訊號，並讓人關掉 gate。故機械 lint 只做**窄 backstop**，**主控制是發布前的人工語意檢查**（合取規則是語意的，linter 判不了）。

## 私有素材

- **不做版本控管** — 沒有 git history 就代表**刪除是真的刪得掉**（repo 刪了還在 history/fork/archive 裡）。這是真實的安全性質，不是省事。
- **放 repo 樹「外」**（非 repo 內 gitignore）——git 物理上碰不到，不怕 `git add -f` 或誤改 `.gitignore`。
- **保留期**：決策（切換完成／放棄）**+30 天**；兜底 **1 季**。**刪原始、留去識別化衍生物**（anonymize-then-retain：同時拿到資料最小化與複用價值）。到期需 scheduled reminder（repo 外的東西 lint 管不到）。

## Codified

- [`check_engagement_disclosure.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/lint/check_engagement_disclosure.py) — 窄 proximity gate，只對「來源平台 … in-flight 標記」同行命中即 fail。
- 行級 opt-out：`<!-- deid-ok: 理由 -->` / `# deid-ok: 理由`（**須註解錨定且帶理由**——純子字串比對曾造成 fail-open：散文提及 marker 的行會自我豁免）。
