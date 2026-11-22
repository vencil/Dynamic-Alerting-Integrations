---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-22T19:43:52+00:00
---
- **三支改寫追蹤檔的產生器改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`gen_adr_checks.py`、`generate_rulepack_configmaps.py`、`generate_rulepack_derived.py`。目標是 FIFO 時，前兩支原本會卡住不返回（`gen_adr_checks --check` 也會）；現在是結束碼 `2` 加一行點名該檔。三支都改成所有檔案都判斷完、所有檢查都通過才寫第一個：以前 `gen_adr_checks --write` 在後面的檢查失敗前已改寫前面的 ADR，`generate_rulepack_derived` 遇到後面某個無法改寫的規則包時，前面的已經改了。要改寫的檔若剛好是本行程的 stdin／stdout／stderr 也會被拒絕。改寫後的權限是 git checkout 會給的（`0644`，原本有 owner 執行位元則 `0755`）。
