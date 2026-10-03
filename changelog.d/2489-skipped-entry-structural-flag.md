---
section: Fixed
topic: alertmanager-routing
issues: [2489]
created: 2026-10-03T14:51:30+00:00
---
- **設定值剛好是 `skipping` 時，`generate-routes --validate` 與 `validate-config` 不再誤判為失敗（alertmanager-routing；[#2489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2489)）**：判斷「某個設定項因無法使用而被丟棄」原本是在 WARN 文字裡找 `skipping` 這個字，而 WARN 會帶出使用者的值，所以 `repeat_interval: "skipping"` 這種一般的「改用平台預設值」WARN 會讓兩者回 1（換成 `"banana"` 則是 0）。現在改看該行是不是由「丟棄設定項」的產生處建出來的，不再看文字；輸出的文字不變，真正被丟棄的設定項照舊判為失敗。`explain-route` 的「未生效（產生器略過）」清單有同一個問題（timing 值為 `skipping` 的 override 明明有生效卻被列入），一併修正。
