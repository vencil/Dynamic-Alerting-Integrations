---
section: Changed
topic: tenant-api
issues: [2903, 2826, 2858]
created: 2026-10-15T04:27:40+00:00
---
- **⚠️ breaking：tenant-api 每筆會改變 conf.d 內容的寫入，commit 前都問產生器容器（ADR-036 §3–§5；[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）**：所有寫入路徑（直寫與 PR、同步與非同步）在寫入鎖下把 base commit 與要寫的完整內容送給 `da-tools generator-service`，寫入後整棵樹通過 `generate-routes --validate --strict` 才 commit；回應的 base、各檔 sha256 與部署端 policy 檔 hash 須與要寫的一致，commit 的位元組與 base 也須是判定過的那份（git 屬性改了內容回 422 `normalized`，判定期間 HEAD 被外部移動回 409）。新旗標 `--generator-socket`（必填，未設拒絕啟動）、`--deploy-policy`。有 finding 回 422 `GENERATOR_FINDINGS`，沒判出結果回 429／422 `GENERATOR_REJECTED`（計入 `tenant_api_generator_rejections_total{reason}`），容器不可用回 503 `VALIDATOR_UNAVAILABLE`／`POLICY_UNAVAILABLE`。**移除** `--policy-unavailable-open`、`TA_POLICY_UNAVAILABLE_OPEN` 與 `tenant_api_policy_unavailable_open_total`：不再有放行模式。`tenant_api_policy_available` 改由容器狀態產生，`/metrics` 附上容器的 `da_generator_*`。chart 的產生器容器改為必備（沒有 git repo 的 conf.d 不再能 render）；預設映像 `tools/v2.9.0` 不含 `generator-service`，要另指含它的映像，下一個 tools 版本一併調升。
