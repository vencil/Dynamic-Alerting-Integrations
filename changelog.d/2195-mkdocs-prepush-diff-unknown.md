---
section: Fixed
topic: dx
issues: [2195, 2552]
created: 2026-09-27T17:30:00+00:00
---
- **mkdocs strict 的 pre-push 守衛不再把量不到的推送當成沒有文件變更（dx；[#2195](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2195)）**：`scripts/ops/pre_push_mkdocs_strict.sh` 判斷推送是否動到文件時，`git diff` 失敗改走「無法判斷」路徑、照樣建站並分開回報，不再吞成空清單；拿掉列舉式 `--diff-filter=ACMRD`，文件的型別變更（例如改成 symlink）也會觸發建站；改用 `-z` 列檔，非 ASCII 檔名的文件不再因 git 加引號而漏判。pre-commit 只給出推送目標、給不出被推的 commit 時（第一次推到空 remote），改為拒絕推送，不再當成沒有東西可推而放行（[#2552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2552)）。
