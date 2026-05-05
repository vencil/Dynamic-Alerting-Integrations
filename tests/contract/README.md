---
title: "Contract tests (schemathesis)"
purpose: |
  TECH-DEBT-022: schemathesis runs against tenant-api's swag-generated
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

預設只跑 `response_schema_conformance`：每個 GET endpoint 的回應 body 必須符合 spec 宣告的 schema。

**只測 GET**：寫入路徑會改 state，會留垃圾。寫入路徑由 `internal/handler/*_test.go` 的 unit test 覆蓋。

**只測 conformance**：`status_code_conformance` 和 `content_type_conformance` 暫時關閉，因為 spec 還沒完整宣告所有 4xx/5xx 回應。等 spec 補齊後再打開（→ TODO 追蹤 issue）。

## 排錯

**`401 Unauthorized` 大量出現**：tenant-api 走 `X-Forwarded-Email` proxy auth。runner 已經自動帶 `-H "X-Forwarded-Email: schemathesis@example.com"`。如果還是 401，看 `internal/handler/middleware.go` 的 auth 邏輯有沒有改規則。

**`Response violates schema`**：真實 spec 漂移。修法：
1. 先看 schemathesis 報告的具體 endpoint
2. 用 `curl -H "X-Forwarded-Email: a@b.com" http://localhost:8080/api/v1/<path>` 看實際回應
3. 對照 `components/tenant-api/docs/swagger.json` 的對應 path
4. 決定要修 handler 回應 or 修 swag 註解（通常修註解）然後 `make api-docs`

**Server 啟不起來**：腳本 timeout 15s 內 `/health` 沒 200。看 `runner.py` 印出的 stdout/stderr。常見原因：port conflict（runner 已隨機選 port，照理不會碰到）/ config 路徑壞 / build 失敗。

**Build VCS error**：dev container 內 `.git` 是檔案指向 Windows path——已經帶 `-buildvcs=false` 規避。

## CI 整合

**目前 CI 不跑 contract test**。先讓 spec 漂移清乾淨（TD-022 follow-up）再加 CI step。完整契約：
- 條件式 trigger：只在 `components/tenant-api/docs/swagger.json` 或 `internal/handler/**.go` 變動時跑
- 與 `go-tests` job 平行（不阻塞 PR merge 直到 spec clean）
- 失敗自動 attach JUnit XML report 為 artifact

當 spec 完全 conformance pass 後，這份 README 會更新。

## 相關文件

- [TECH-DEBT-022 issue](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/231)
- [tenant-api swag annotations](../../components/tenant-api/internal/handler/) — 編 spec 從這裡改
- [Makefile `api-docs` target](../../Makefile) — 重新產 swagger.json
