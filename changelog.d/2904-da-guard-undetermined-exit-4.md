---
section: Changed
topic: alertmanager-routing
issues: [2904, 2858]
created: 2026-10-16T05:30:00+00:00
---
- **⚠️ breaking：da-guard 不再判路由與 domain policy，改報「未判定」、exit 4（exporter；[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)，ADR-036 §9）**：`da-guard`／`da-tools guard defaults-impact` 不再產出任何路由類 finding；整棵樹有路由鍵、`_domain_policy`、`_routing_profiles`、`parse-tree` 讀不了的檔或不合法的租戶 id 時，報告列出證據（JSON 一律有 `undetermined`，沒有時為 `[]`）並回 exit 4，優先序 3 > 1 > 4 > 0；沒有這類內容的樹照常回 0。⚠️ 兩種租戶 id 的情況因此由 exit 1 改為 exit 4：不合法的租戶 id（過去是 error 級 finding `invalid_tenant_id`），以及兩個租戶檔宣告同一個 id、而本次 `--scope` 看不到 exporter 拒收的情況（過去是 error 級 finding `duplicate_tenant`；整棵樹執行仍由 exporter 以 exit 2 拒收）；兩者現在是 `undetermined` 的同名 `reason`，判定交給產生器。`--required-fields _routing.*` 改回 exit 2。產生器整份拒收、exporter 卻照讀的檔（如重複鍵）不算 exit 3，列為 `generator_unreadable`（[#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905)）。validate-config 的 profiles 列改取產生器的判定。遷移：在 CI 原本跑 da-guard 的地方加一步 `da-tools generate-routes --validate --strict`，並把 exit 4 視為「路由交給產生器」；`guard-defaults-impact.yml` 已這樣做。
