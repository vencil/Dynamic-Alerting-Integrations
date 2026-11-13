---
section: Changed
topic: lint
issues: [1560, 3748]
created: 2026-11-11T16:16:42+00:00
---
- **文件的 conf.d 範例也以產生器的憑證判定檢查（lint；[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)、[#3748](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3748)）**：`check_md_yaml_drift` 對宣告 `{.confd}` 或依結構判為 conf.d 範例的區塊（只有 `routing_profiles:` 的區塊也算，以 routing-profiles schema 判）、`{.helm-values}` 裡 chart 會渲染成 conf.d 的 `thresholdConfig`，以及沒有消費者的 yaml 區塊（routing 區塊在任何深度），跑產生器拒收明文憑證的同一個判定；`credential_plaintext` 或 `credential_unjudged` 讓 lint 失敗，訊息只有檔名、行號與 kind，不印值。`{.confd .expect-invalid}` 只能吸收 schema 錯誤，憑證判定一律照報。只判以 YAML 寫成的 conf.d；用 printf、curl、heredoc 或字串寫出的內容不在範圍內，照抄時由產生器拒收。文件範例裡的明文憑證改寫成 Secret 參照（`{secret: {key: <key>}}`；平台層 `_routing_enforced` 用 `{secret: {shared: <別名>, key: <key>}}`）。
