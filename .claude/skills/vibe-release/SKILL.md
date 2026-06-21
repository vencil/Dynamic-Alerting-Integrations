---
name: vibe-release
description: Vibe 六線版號 release 收尾 SOP — make pre-tag → CHANGELOG distill + project-face refresh → 6-line tag push → gh release ×6。Use when wrapping a Vibe release：user 說「release 收尾 / 進入 phase e / 準備 release」、問「release 準備好了嗎」、branch 名 `chore/v*-release-wrapup`、或動到 `make pre-tag` / 六線 tag push / `gh release create`。延伸 #474 Layer 3 的 inline checklist 為系統化流程。
---

# vibe-release — 六線版號 release 收尾

完整步驟、distribution artifacts、benchmark gate、踩坑見 [`github-release-playbook.md`](../../../docs/internal/github-release-playbook.md)。本 skill 是**收尾 agent 紀律**的濃縮：三條規則（源自 v2.8.0 收尾踩的 2 個 release blocker），加 release-type 分流。

## 何時觸發

release-wrap-up 情境（**非**一般 dev）：「release 收尾 / 進入 phase e / 準備 release」、「release 準備好了嗎」、`chore/v*-release-wrapup` branch、`make pre-tag` / 六線 tag push / `gh release create ×6`。

## Release-type 分流

| 類型 | 範例 | 推哪些 tag |
|---|---|---|
| **GA**（component 有變更） | v2.8.0 | 六線中有 code change 的線：`v*` / `exporter/v*` / `tools/v*` / `portal/v*` / `recipe-preview/v*` / `tenant-api/v*` |
| **Interim DX**（僅平台 / 內部工具） | v2.8.1 | 通常只 `v*`（platform tag）；component binary 不變則不推其 tag |
| **Hotfix** | 假想 v2.8.2 | 受影響 component tag（觸發 build/image）**＋ 平台 `v*` tag**（見下「錨點鐵則」） |

**錨點鐵則**：平台 `v*` tag **不觸發 build，但它是 GitHub Release 的錨點**——release body（含 hotfix 的「what changed」）與**客戶下載 binary 的入口**都掛在平台版號上（playbook Step 3 / line 171）。所以**任何**有 release notes 的發布（含只動單一 component 的 hotfix）都要推平台 `v*` tag；CHANGELOG 是平台版號文件，hotfix 改了它＝平台版號 bump，與「版號不變不推」一致。

**鐵則**：版號*內容*不變的 component **不推**其 component tag（dev-rule #7）。但平台 `v*` 因 release-notes 變動幾乎總是 bump（除非該 component 自帶獨立 release）。

## 收尾流程

### 1. `make pre-tag`（硬性閘門）

含 version-check + lint-docs + playbook-freshness + benchmark-report-warn + **`docker-build-all`（hard gate）+ `trivy-scan-all`（informational）**（#474 Layer 2 已把 5 component image build + CVE scan 收進 pre-tag）。

> **仍是 authoritative-but-incomplete**：pre-tag 是**最低標**，`release.yaml` 才是真 contract。release-only 的步驟（cosign 簽章、helm chart OCI push、digest verification #445 L3）不在 pre-tag——agent 須 audit「pre-tag 涵蓋了什麼 vs release.yaml 實際做什麼」，缺的手動補驗。#474 已把 docker build + Trivy 那段機械化（過去是純 discipline）。

### 2. CHANGELOG distill + **project-face refresh**（Rule 2）

`[Unreleased]` → `## [vX.Y.0]` 時，**同步刷新門面**（CHANGELOG 是版本切片，README / architecture-and-design 是 release 之間客戶/架構師看的門面）：

- **README.md / README.en.md**（廣度，SRE/DevOps evaluator）：version badge + 雙語/tool/doc count badge（`bump_docs.py --sync-counts --dry-run` 對照）+ 新支柱在既有結構提及（不為單次 release 重構）
- **architecture-and-design.md / .en.md**（深度，架構師/貢獻者）：§Roadmap 當前版翻「In Development → Shipped (YYYY-MM-DD)」+ 加 next-version 方向列（**link milestone 不列 issue**）+ 重大新類別加架構圖
- **區分受眾**：README =「前 30 秒 value prop」；arch-and-design =「30 分鐘理解架構」，別兩處鏡像同內容

### 3. Roadmap v.next = **link milestone 不複製 issue list**（Rule 3）

issue triage 每天動、docs 月級更新——靜態 issue list 幾天就說謊。roadmap 拆三段：(1) vX.Y.0 delivered 靜態成就；(2) v.next 方向 + **單一 live milestone link** + 3-5 focus bullet；(3) 長期願景。docs = SSOT for「打算做什麼」，milestone = SSOT for「正在做什麼」，不同抽象層故不漂移。

### 4. 六線 tag push + `gh release create`

依分流推 tag；步驟、artifact、benchmark gate 見 [`github-release-playbook.md`](../../../docs/internal/github-release-playbook.md)。

## 使用法

1. 收尾前對照三條規則跑完才宣告「ready to ship」（每條都在 v2.8.0 炸過：Rule 1 → 30 分 recovery、Rule 2 user 中途加、Rule 3 Gemini 抓）
2. pre-tag 過 ≠ 完成；audit pre-tag vs release.yaml 的 gap
3. 細節一律 deferred 到 [`github-release-playbook.md`](../../../docs/internal/github-release-playbook.md)，本 skill 只管「別漏哪三件事」
