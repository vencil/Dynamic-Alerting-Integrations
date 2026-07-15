---
title: "Contract tests (schemathesis)"
purpose: |
  schemathesis runs against tenant-api's swag-generated
  OpenAPI spec to catch:
    - Response schemas that don't match what the spec declares
    - Endpoints documented in spec but missing in code (or vice versa)
    - Status codes / content types not declared in spec
audience: [contributors, ai-agent]
lang: zh
---

# Contract tests (schemathesis)

## 怎麼跑

```bash
# 完整流程：build tenant-api → 啟 server → schemathesis fuzz → 拆掉
make contract-test

# 加深 fuzz（local 調查時用）
CONTRACT_MAX_EXAMPLES=50 make contract-test
```

需要：
- Dev container（`make dc-up`）或 host 端有 Go 1.26 + Python 3.13 + `pip install schemathesis`
- `components/tenant-api/docs/swagger.json` 是最新的（如不確定先 `make api-docs`）

## 它檢查什麼

預設只跑 `response_schema_conformance`：每個 endpoint 的回應 body 必須符合 spec 宣告的 schema。

**全 method fuzz**：GET/PUT/POST/DELETE 全部下場。寫入落在 throwaway git repo fixture（runner 對 temp config dir `git init` + initial commit——tenant-api 是 commit-on-write，沒 repo 每個寫入都 500），跑完整個 workdir `rmtree`，不留垃圾。RBAC 用 wildcard fixture（`_rbac.yaml` 單一 group、`tenants: ["*"]`、read+write+admin），請求帶 `X-Forwarded-Email` + `X-Forwarded-Groups`；rate limiter 以 `TA_RATE_LIMIT_PER_MIN=0` 關閉（同一 caller 的 fuzz 流量會踩預設 100/min）。

**排除的 operations**：`/federation/tokens*` + `/federation/accounts/backfill`（4 ops）——路由只在 `--federation-key` 設定時註冊，且 token store 需要 in-cluster Kubernetes ConfigMap，本機 fixture 起不來。reopen 條件：federation record store 有 file-backed / fake seam 後補測；現由 Go handler test（stub store）覆蓋。

**只測 conformance**：`status_code_conformance` 和 `content_type_conformance` 暫時關閉，因為 spec 還沒完整宣告所有 4xx/5xx 回應。等 spec 補齊後再打開（→ TODO 追蹤 issue）。

## 排錯

**`401 Unauthorized` 大量出現**：tenant-api 走 `X-Forwarded-Email` proxy auth。runner 已經自動帶 `-H "X-Forwarded-Email: schemathesis@example.com"`。如果還是 401，看 `internal/handler/middleware.go` 的 auth 邏輯有沒有改規則。

**`Response violates schema`**：真實 spec 漂移。修法：
1. 先看 schemathesis 報告的具體 endpoint
2. 用 `curl -H "X-Forwarded-Email: a@b.com" http://localhost:8080/api/v1/<path>` 看實際回應
3. 對照 `components/tenant-api/docs/swagger.json` 的對應 path
4. 決定要修 handler 回應 or 修 swag 註解（通常修註解）然後 `make api-docs`

**Server 啟不起來**：腳本 timeout 15s 內 `/health` 沒 200。看 runner 在非預期退出時印的 server log tail（server 輸出寫到 workdir 的 `tenant-api.log`，不是 pipe——全 method fuzz 的 log 量會塞爆沒人讀的 64KB pipe buffer，讓所有請求 hang 死）。常見原因：port conflict（runner 已隨機選 port，照理不會碰到）/ config 路徑壞 / build 失敗。

**Build VCS error**：dev container 內 `.git` 是檔案指向 Windows path——已經帶 `-buildvcs=false` 規避。

## CI 整合

**已啟用**（spec-drift fix 之後）：在 `Go Tests (1.26)` job 末段，於 swag drift check 之後跑。`CONTRACT_MAX_EXAMPLES=5` 讓單次 CI ~10-20s。

當前 CI step:
```yaml
- name: Install schemathesis
  run: pip install schemathesis
- name: Run schemathesis contract tests
  env:
    CONTRACT_MAX_EXAMPLES: "5"
  run: python3 tests/contract/run_contract_tests.py
```

### 已知 warnings（非 failure）
- **Schema validation mismatch (6 ops)**：spec 裡 path param 沒指定 `pattern`，schemathesis 隨機產生奇怪字串被 \`ValidateTenantID\` 拒絕。改善方法：在 swag 註解加 `format` / `pattern` 約束。
- **Missing valid test data (5 ops)**：schemathesis 隨機產 ID 大多 404。改善方法：在 spec 加 `example` 或在 runner 用 `--data` 鎖固定值。

兩者都是 warning（exit 0），目前不阻擋 CI。後續 follow-up TECH-DEBT 可逐步收斂。

## 相關文件

- [Contract-test tracking issue (#231)](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/231)
- [tenant-api swag annotations](../../components/tenant-api/internal/handler/) — 編 spec 從這裡改
- [Makefile `api-docs` target](../../Makefile) — 重新產 swagger.json
