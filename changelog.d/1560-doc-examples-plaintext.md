---
section: Changed
topic: lint
issues: [1560]
created: 2026-11-11T16:16:42+00:00
---
- **文件的 conf.d 範例也以產生器的憑證判定檢查（lint；[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：`check_md_yaml_drift` 對宣告 `{.confd}` 或依結構判為 conf.d 範例的區塊，跑產生器拒收明文憑證的同一個判定；`credential_plaintext` 或 `credential_unjudged` 讓 lint 失敗，訊息只有檔名、行號與 kind，不印值。`{.confd .expect-invalid}` 的反例命中這個判定也算「故意寫錯」。文件範例裡的明文憑證改寫成 Secret 參照（`{secret: {key: <key>}}`；平台層 `_routing_enforced` 用 `{secret: {shared: <別名>, key: <key>}}`）。
