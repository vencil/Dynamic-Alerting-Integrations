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

三個 conformance check 全開：`response_schema_conformance`（回應 body 符合宣告 schema）、`status_code_conformance`（不回未宣告的 status code）、`content_type_conformance`。

**全 method fuzz**：GET/PUT/POST/DELETE 全部下場。寫入落在 throwaway git repo fixture（runner 對 temp config dir `git init` + initial commit——tenant-api 是 commit-on-write，沒 repo 每個寫入都 500），跑完整個 workdir `rmtree`，不留垃圾。RBAC 用 wildcard fixture（`_rbac.yaml` 單一 group、`tenants: ["*"]`、read+write+admin），請求帶 `X-Forwarded-Email` + `X-Forwarded-Groups`；rate limiter 以 `TA_RATE_LIMIT_PER_MIN=0` 關閉（同一 caller 的 fuzz 流量會踩預設 100/min）。

**排除的 operations**：
- `/federation/tokens*` + `/federation/accounts/backfill`（4 ops）——路由只在 `--federation-key` 設定時註冊，且 token store 需要 in-cluster Kubernetes ConfigMap，本機 fixture 起不來。reopen 條件：federation record store 有 file-backed / fake seam 後補測；現由 Go handler test（stub store）覆蓋。
- `GET /prs`（1 op）——路由只在 PR write-mode（需 forge token）註冊，fixture 跑 `write-mode=direct` 會 404。reopen 條件：fixture 長出 stub forge backend。

**4xx/5xx 宣告紀律**：error 回應已全面宣告為 `handler.ErrorResponse`（統一 error envelope，PR-9）；新 handler 回了未宣告的 status code 會被 `status_code_conformance` 擋下——記得補 `@Failure` 標註 + `make api-docs`。schema 已把 `error`/`code` 收成 **required**（`binding:"required"`），所以 `response_schema_conformance` 現在會真正咬住手寫裸 `{"error": ...}` map 的落差——新 error path 一律走 `WriteJSONError` / `WriteErrorEnvelope` 家族（`codeFromStatus` 未映射的 status 會產出無 `code` 的回應 → 契約測試紅，這是刻意的 tripwire）。原本記錄在此的三處 legacy 裸 map emitter（`access.go` 400 / `me.go` 401 / rbac middleware 的 `writeError`+`writeForbidden` 401/403）已全部遷移統一 envelope（message/status 不變，加 `code`+`request_id`；rbac 屬 domain 層不能 import handler，envelope 形狀以套件內 struct 鏡射、由 `internal/handler/access_test.go` 走真 middleware 釘住對齊）。

**已知未涵蓋**（誠實記錄，非 gate 盲區造成的假綠）：

- RBAC middleware 的 401/403 與 rate limiter 的 429 是跨切面、每個 op 都可能回，但 fixture 用 wildcard RBAC + 關 rate limiter（否則 fuzz 打不進 handler 邏輯），spec 逐 op 宣告與否 fuzz 都觀測不到（middleware 的 wire shape 已同上遷移統一 envelope、由 Go 測試釘住，只是 fuzz 觀測不到）；domain policy 403（`violations` 為 `policy.Violation` 形狀）同理（fixture 無 `_domain_policy.yaml`）。
- Windows host 直跑 runner 的兩個邊角（container/CI 無此問題）：`shutil.rmtree(ignore_errors=True)` 遇 git objects 唯讀檔可能留 temp 殘骸；`Popen` 若在啟動時 raise，`log_fh` 不會被 finally 關閉。走 `make contract-test`（dev container）不受影響。

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
