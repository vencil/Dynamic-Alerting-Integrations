---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-24T12:56:39+00:00
---
- **三支會改寫自己 baseline 的 lint 改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`check_cli_contract.py --write-baseline`、`check_percent_render_pinned.py`、`check_vmalert_coverage.py`。baseline 是 FIFO 時，原本 `--ci`、`--generate`、`--write-baseline` 都會卡住不返回；現在是結束碼 `2` 加一行點名該檔（`ERROR: cannot read …` 或 `cannot write …`），不再被包成「could not read the inputs」；共用寫入入口在讀取被拒時，提示改為「internal path, this is a bug or an unreadable workspace」。內容沒變時不重寫（剛好是本行程 stdin 的 baseline 因此仍是結束碼 `0`）；改寫一般以原子取代完成，權限是 git checkout 會給的（`0644`，原本有 owner 執行位元則 `0755`）；無法原子取代時退回就地寫入並印 `WARN`。
