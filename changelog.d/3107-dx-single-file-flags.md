---
section: Changed
topic: dx
issues: [3107]
created: 2026-11-17T22:30:00+00:00
---
- **`generate_changelog.py -o`、`waveform_score.py --out`、`waveform_compile.py --out` 改走 operator 輸出（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：kernel 開檔，權限 `0600` 只設在自己擁有的一般檔；遷移後所有工具都不再經舊寫入名稱寫輸出旗標。⚠️ 行為變更：(1) `generate_changelog.py -o -` 由寫出名為 `-` 的檔改為印到 stdout；(2) `waveform_score.py --out` 給 `-` 或本次 stdout 重導的檔時 `0` → `2`（stdout 已印報告；`--json` 印同一份 JSON）；(3) `waveform_compile.py --out -`（任何模式，`--check` 也是）由建出名為 `-` 的目錄或 `0` 改為 `2`；(4) 兩個單檔旗標的空值 `0` → `2`；(5) 非 root 寫別人的可寫檔 `2` → `0`、權限不動；symlink 的目標不再被改成 `0600`；沒有 reader 的 FIFO 由卡住改為 `2`；寫入途中出錯留空檔。
