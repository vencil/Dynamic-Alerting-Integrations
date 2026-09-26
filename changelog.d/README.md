# changelog.d — 還沒發布的變更

`CHANGELOG.md` 的 `## [Unreleased]` 已經凍結（[#2102](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2102)）：往裡面**新增**條目會被 `changelog-format` 擋下。還沒發布的變更，一律在這個目錄各寫一個片段檔，發版時再組裝進 `## [vX.Y.Z]`。

為什麼：每支 PR 都把條目接在同一個檔案、同幾段的末尾，任兩支並行的 PR 就一定在 CHANGELOG 衝突，前一支合進去、其餘都得 rebase。一個變更一個檔案，就不會改到同一行。

## 格式

檔名：`<issue>-<短描述>.md`，例如 `2102-changelog-fragments.md`，kebab-case。沒有 issue 就只用短描述。

```markdown
---
section: Changed
topic: ci
issues: [2102]
created: 2026-09-26T15:40:00+08:00
---
- **一句話說清楚改了什麼（scope；[#2102](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2102)）**：給使用者看的重點，3–6 行。
```

- `section`：`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security` 其中之一。
- `topic`：組裝時的分組鍵（kebab-case），例如 component 或 epic 名：`exporter`、`tenant-api`、`ci`、`confd-family`。同一主題的條目會排在一起。
- `issues`：相關 issue 編號清單；沒有就寫 `[]`，但不能省略這個鍵。
- `created`：建立時間，帶時區（`date -Iseconds`）。同一主題內依它排先後。
- 本文：**恰好一個** column-0 的 `- ` 條目（可以有縮排的續行），上限 1,000 字元。量測數字放 commit 或 issue，不放這裡。

## 規則

- ⛔ **修改還沒發布的功能，要改它原本的片段檔，不要另外新增一份。** 發版的 release note 描述的是「相對上一版的淨變化」，讀者不需要知道它在開發期間被改過幾次。組裝時同一個 issue 出現多份片段會印出提醒。
- 已經發布過的功能再修改，才新增一份片段（通常是 `Changed` 或 `Fixed`）。
- 連到 `docs/` 以外的連結用絕對 GitHub URL（與 `CHANGELOG.md` 同一條規則）。

## 工具

```bash
python3 scripts/tools/dx/generate_changelog.py --fragments   # lint 全部片段（pre-commit 的 changelog-fragments 會自動跑）
python3 scripts/tools/dx/generate_changelog.py --assemble    # 印出組裝後的 markdown（發版前預覽）
```

發版時的流程見 `vibe-release` skill 的「CHANGELOG distill」一步：`--assemble` 的輸出是原料，濃縮成 `## [vX.Y.Z]` 之後，刪掉已組裝的片段檔。
