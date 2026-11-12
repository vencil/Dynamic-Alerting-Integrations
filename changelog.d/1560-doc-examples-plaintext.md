---
section: Changed
topic: lint
issues: [1560, 3748]
created: 2026-11-11T16:16:42+00:00
---
- **文件的 conf.d 範例也以產生器的憑證判定檢查（lint；[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)、[#3748](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3748)）**：`check_md_yaml_drift` 對宣告 `{.confd}` 或依結構判為 conf.d 範例的區塊（只有 `routing_profiles:` 的區塊也算，以 routing-profiles schema 判）、`{.helm-values}` 裡 chart 會渲染成 conf.d 的 `thresholdConfig`，以及沒有消費者的 yaml 區塊（routing 區塊在任何深度），跑產生器拒收明文憑證的同一個判定；`credential_plaintext` 或 `credential_unjudged` 讓 lint 失敗，訊息只有檔名、行號與 kind，不印值。`{.confd .expect-invalid}` 反例裡的明文憑證照常報錯，不算「故意寫錯」。routing 區塊鍵寫在不被判定的地方（bash heredoc、blockquote、縮排程式碼、其他語言的 fence，flow 寫法也算；或被判定區塊裡的字串與註解，以讀到的鍵數比對）也讓 lint 失敗，要求改寫成 `{.confd}` fence 或宣告 `{.illustrative reason="…"}`（其他語言的 fence 也可以）。文件範例裡的明文憑證改寫成 Secret 參照（`{secret: {key: <key>}}`；平台層 `_routing_enforced` 用 `{secret: {shared: <別名>, key: <key>}}`）。
