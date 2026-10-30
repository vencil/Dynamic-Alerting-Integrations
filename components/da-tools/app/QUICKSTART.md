# da-tools — The ops CLI that blocks Prometheus-killing configs in CI

> **維運瑞士刀：在 CI 階段攔下會引爆 Prometheus cardinality 的配置，守護監控穩定。**
> *Ops Swiss-army CLI — block the config that would blow up Prometheus, before it merges.*

|  |  |
|---|---|
| **What / 是什麼** | 維運 CLI 工具箱（診斷 / 回測 / 路由生成 / config guard / federation keygen…；完整子命令清單見 [README](../README.md)）。*An ops CLI toolkit.* |
| **Why / 為什麼** | 在 CI **攔截高基數 / 惡意配置**，在它炸掉 Prometheus 前擋下。*Catch cardinality bombs in CI.* |
| **Who / 給誰** | Platform Engineer / SRE / CI owner |
| **Try（≤2 min）** | 見下方——**對一份「過量配置」fixture 跑 da-guard** |
| **→ You'll see** | da-guard **紅字攔截**：指出哪個租戶的列數超出 cardinality 預算，並說明 exporter 自己的上限（`max_metrics_per_tenant`）會不會截斷它，**exit 1**。*da-guard red-flags the over-budget tenant and exits 1.* |

> 🎯 **這個 demo 給誰**：Platform Engineer / SRE / CI owner（把 da-guard 接成 CI 護欄）。其他角色（Domain Expert / Tenant）的上手路徑見 [角色導引](../../../docs/getting-started/README.md)。

**Prerequisite**：Docker 20.10+。

## Try it（從本目錄 `components/da-tools/app/` 執行）

內附 fixture [`examples/cardinality-demo/conf.d/`](examples/cardinality-demo/conf.d/) 是一個**故意過度配置**的租戶（60 個 metric 閾值，都在根 `_defaults.yaml` 的 `optional_overrides:` 宣告過，所以 exporter 每個 key 送一條 series；da-guard 數的就是 exporter 實際會產生的列數，[#3380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3380)）。對它跑 da-guard，預算設低 50（`--cardinality-limit` 只是 da-guard 的預算；exporter 自己的上限是根 `_defaults.yaml` 的 `max_metrics_per_tenant`，未設即 500，所以這 60 列在 runtime 不會被截斷——demo 示範的是把超出預算的變更擋在 CI）：

```sh
docker run --rm -v "$(pwd)/examples:/work:ro" ghcr.io/vencil/da-tools:v2.9.0 \
  guard defaults-impact --config-dir /work/cardinality-demo/conf.d --cardinality-limit 50
```

**你會看到**（實測輸出）：

```markdown
## Dangling Defaults Guard

### Summary

- Tenants in scope: **1**
- Errors: **1**
- Warnings: **0**
- Tenants passing (zero errors): **0**

### Errors (block merge)

| Tenant | Field | Kind | Message |
|--------|-------|------|---------|
| over-budget-tenant | — | cardinality_exceeded | tenant "over-budget-tenant": the exporter would produce 60 user_threshold rows at its busiest minute of the day, which exceeds the per-tenant cardinality limit of 50; the exporter's own max_metrics_per_tenant (500) truncates none of them |
```

⚠️ v2.9.0 映像裡的 da-guard 對這棵樹判定相同（`cardinality_exceeded`、exit 1），但數的是租戶 effective config 的頂層鍵，報 61（`optional_overrides` 也被算成一個鍵），訊息也仍是舊寫法：`predicted metric count 61 exceeds the per-tenant cardinality limit of 50; runtime would silently truncate the excess (config_resolve.go::ResolveAt)`。<!-- image-caveat: v2.9.0 -->

```sh
echo $?      # → 1（errors > 0 即 block merge）
```

這就是 da-guard 的價值：在配置進 Prometheus**之前**，於 CI 算出它會超出 per-tenant cardinality 預算、並以非零 exit code 擋下 merge——把一場「半夜被 call 起床」的事故消滅在 PR 階段。

## 各子工具一句定位
- **`da-guard`** — conf.d 安全把關：schema + **cardinality** guard（上面 demo 的就是它；路由與 domain policy 自 #2904 起交給 `da-tools generate-routes --config-dir <conf.d> --validate --strict`，樹上有這類內容時 exit 4）
- **`da-parser`** — PromRule → conf.d 解析匯入
- **`da-batchpr`** — hierarchy-aware 批次 PR
- **`fed-key`** — federation JWT 金鑰簽發（ops-only）
- 全部子命令：`docker run --rm ghcr.io/vencil/da-tools:v2.9.0 --help`

## Next
- ← **先玩整套**：[`try-local/`](../../../try-local/)
- 📖 **完整子命令 / 客戶旅程分類**：[`README.md`](../README.md)（本元件的 reference）
- → **上 production**：把 `da-guard` 接進 **GitHub Actions / GitLab CI 當 Required Merge Guard**（da-tools 是 CLI 工具箱，**無 Helm chart**）——讓每個 config PR 都先過 cardinality 把關。
