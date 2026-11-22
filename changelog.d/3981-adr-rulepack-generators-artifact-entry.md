---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-22T19:43:52+00:00
---
- **三支改寫追蹤檔的產生器改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`gen_adr_checks.py`、`generate_rulepack_configmaps.py`、`generate_rulepack_derived.py`。目標是 FIFO 時，前兩支原本會卡住不返回（`gen_adr_checks --check` 也會）；現在是結束碼 `2` 加一行點名該檔。三支都改成內容檢查全部通過、所有要改寫的檔都通過寫入前判斷（不是一般檔，或剛好是本行程的 stdin／stdout／stderr，都會被拒絕）才寫第一個：以前 `gen_adr_checks --write` 在後面的檢查失敗前已改寫前面的 ADR，`generate_rulepack_derived` 遇到後面某個內容無法改寫的規則包時，前面的已經改了。寫到一半才失敗的（例如目標是唯讀檔、磁碟滿）仍會留下前面已改寫的檔（[#4250](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4250)）。改寫後的權限是 git checkout 會給的（`0644`，原本有 owner 執行位元則 `0755`）；無法原子取代、退回就地寫入時沿用原權限並印 `WARN`。
