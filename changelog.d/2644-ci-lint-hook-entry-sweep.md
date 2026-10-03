---
section: Fixed
topic: ci
issues: [2644]
created: 2026-10-03T08:20:12+00:00
---
- **十二支 pre-commit hook 在 CI 有了執行點（ci；[#2644](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2644)）**：CI Lint job 逐名跑 hook，沒有任何 workflow 跑不帶 id 的 `pre-commit run --all-files`，於是有 64 支 auto stage hook 不在任何 workflow 裡以 hook 形式執行。逐支盤點後，其中十二支既沒有別的 job 跑同一支腳本，在樹上植入該 hook 會擋的違規後，它們的 pytest 也照樣全綠：`file-hygiene`、`session-guard-liveness-check`、`env-bool-parser-guard`、`devrules-size-check`、`doc-k8s-refs`、`commit-scope-doc-drift`、`aria-references-check`、`md-yaml-crd-check`、三支 `forbid-legacy-*`、`skip-a11y-justification-check`。這十二支現在都列進 Lint job 的逐名清單，每支各一行，並以解析 YAML 的參數化斷言釘住。其餘各支的 CI 執行點分屬三種：有獨立 job 跑、有 pytest 對整棵樹做同一判定，或天生只看 staged 內容。
