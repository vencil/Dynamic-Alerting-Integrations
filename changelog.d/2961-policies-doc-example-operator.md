---
section: Fixed
topic: docs
issues: [2961]
created: 2026-10-13T23:31:29+00:00
---
- **for-domain-experts 的 `_policies` 範例改用 `operator:`（docs；[#2961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2961)）**：範例原本寫 `check:` 與 `path:`，policy engine 不讀這兩個欄位、沒寫 `operator` 時預設 `required`，照抄的上限規則因此變成「欄位必須存在」，必違反的值 `evaluate-policy` 與 `validate-config` 也回 0。範例改成 engine 實際讀的寫法（路由規則的 `target` 直接寫 `_routing`），並說明 `severity: warning` 不會讓 `--ci` 失敗。照舊範例寫過規則的請把 `check:` 改成 `operator:`。
