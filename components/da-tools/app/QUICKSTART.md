# da-tools — The ops CLI that blocks Prometheus-killing configs in CI

> **維運瑞士刀（70+ 子命令）：在 CI 階段攔下會引爆 Prometheus cardinality 的配置，守護監控穩定。**
> *Ops Swiss-army CLI — block the config that would blow up Prometheus, before it merges.*

|  |  |
|---|---|
| **What / 是什麼** | 70+ 子命令的維運 CLI（診斷 / 回測 / 路由生成 / config guard / federation keygen…）。*An ops CLI toolkit.* |
| **Why / 為什麼** | 在 CI **攔截高基數 / 惡意配置**，在它炸掉 Prometheus 前擋下。*Catch cardinality bombs in CI.* |
| **Who / 給誰** | Platform Engineer / SRE / CI owner |
| **Try（≤2 min）** | 見下方——**對一份「過量配置」fixture 跑 da-guard** |
| **→ You'll see** | da-guard **紅字攔截**：指出哪個租戶會超出 cardinality 預算、runtime 會被靜默截斷，**exit 1**。*da-guard red-flags the over-budget tenant and exits 1.* |

**Prerequisite**：Docker 20.10+。

## Try it（從本目錄 `components/da-tools/app/` 執行）

內附 fixture [`examples/cardinality-demo/conf.d/`](examples/cardinality-demo/conf.d/) 是一個**故意過度配置**的租戶（60 個 metric 閾值；每個 key = runtime 一條 series）。對它跑 da-guard，預算設低 50（production 預設 500）：

```sh
docker run --rm -v "$(pwd)/examples:/work" ghcr.io/vencil/da-tools:v2.8.0 \
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
| over-budget-tenant | — | cardinality_exceeded | tenant "over-budget-tenant": predicted metric count 60 exceeds the per-tenant cardinality limit of 50; runtime would silently truncate the excess (config_resolve.go::ResolveAt) |
```

```sh
echo $?      # → 1（errors > 0 即 block merge）
```

這就是 da-guard 的價值：在配置進 Prometheus**之前**，於 CI 算出它會撐爆 per-tenant cardinality 預算、並以非零 exit code 擋下 merge——把一場「半夜被 call 起床」的事故消滅在 PR 階段。

## 各子工具一句定位
- **`da-guard`** — conf.d 安全把關：schema + routing + **cardinality** guard（上面 demo 的就是它）
- **`da-parser`** — PromRule → conf.d 解析匯入
- **`da-batchpr`** — hierarchy-aware 批次 PR
- **`fed-key`** — federation JWT 金鑰簽發（ops-only）
- 全部子命令：`docker run --rm ghcr.io/vencil/da-tools:v2.8.0 --help`

## Next
- ← **Try the full stack**：[`try-local/`](../../../try-local/)
- → **Move to production**：把 `da-guard` 接進 **GitHub Actions / GitLab CI 當 Required Merge Guard**（da-tools 是 CLI 工具箱，**無 Helm chart**）——讓每個 config PR 都先過 cardinality 把關。
