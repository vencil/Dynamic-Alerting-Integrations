---
title: "ADR-020: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint"
tags: [adr, federation, multi-tenant, security]
audience: [platform-engineers, contributors]
version: v2.8.1
lang: zh
id: ADR-020
tracking_kind: adr
status: proposed
domain: tenant-api
created_at: 2026-05-11
updated_at: 2026-05-17
---

# ADR-020: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint

> Tenant-user 拉取**自己**的 metrics 子集回 tenant 側 infra 自管 federation。
> 平台**不自寫** federation endpoint，採 vmauth（VM 客戶）/ prom-label-proxy（Prom 客戶）做 label-enforced rewriting。
>
> 與 [ADR-004 (Federation — Central-Exporter-First)](./004-federation-central-exporter-first.md) 是兩件事：ADR-004 是**平台內部**多叢集 federation（中央 exporter 服務邊緣 Prometheus），本 ADR 是**跨平台邊界** federation（tenant 把自己的 metrics 拉回 tenant 自有 Prom/VM 自管）。

## 狀態

🟡 **Proposed**（issue [#380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) IV-1 deliverable，v2.8.0 起草）

> EN mirror：本 ADR 進入 `Accepted` 後再起 EN 翻譯（對齊 ADR-019 雙語策略）。

## 背景

### 客戶需求

進入 v2.8.0 客戶導入準備階段，多個 prospective customer 在 RFP / pre-onboarding 訪談中提出需求：

- 我們已有自己的 Prometheus / VictoriaMetrics infra（NOC / SRE team 自管）
- 想把**屬於我們自己** tenant 的 metrics 子集拉回我們 infra，做：
  - 自己 SRE team 的 long-term retention（平台側預設 retention 可能不滿足合規）
  - 整合到自己既有 Grafana dashboard / oncall workflow
  - 自管告警 evaluation（不依賴平台 Alertmanager）

**注意定位**：這不是「平台幫客戶 federation」（那是 [ADR-004](./004-federation-central-exporter-first.md)），是「**客戶從平台拉自己的 metrics 出去**」。資料流向相反，trust boundary 也不同——ADR-004 是「平台信任邊緣叢集（同組織內 N 個 cluster）」的 inbound 場景；ADR-020 是「平台對客戶（cross-org）」的 outbound 場景，被取走的資料離開平台控制邊界後就可能被 tenant 自己再轉、再存、再泄。Multi-tenant isolation 與 audit 需求因此更嚴格。

### 既有 federation 架構覆蓋空白

| 場景 | 既有方案 | 缺口 |
|---|---|---|
| 平台內部多叢集 federation | ADR-004 中央 exporter + 邊緣 remote_read | ✅ 覆蓋 |
| 平台自管 alert eval | tenant-api SSE + Alertmanager | ✅ 覆蓋 |
| **Tenant 拉自己 metrics 回 tenant 側自管** | ⛔ **無方案** | 🎯 本 ADR |

### 為什麼這是個棘手的問題

「給 tenant 開一個 read endpoint 把自己 metrics 拉出去」表面簡單，實際有四個交叉約束：

1. **Multi-tenant isolation**（強制）— Tenant A 絕對不能讀到 Tenant B 的資料。Platform 後端 storage（VictoriaMetrics / Mimir / Prom）通常**沒有強制的 label filter**：寫一個自寫 endpoint 容易在 label sanitization 漏一個矩陣維度（cluster / node / namespace 在某些 metric 上不帶 tenant），變成 multi-tenant breach。
2. **Blast radius**（強制）— Tenant 拉自己 100 萬 series 的查詢若沒限制，會把 platform storage 拖垮（影響其他 tenant + 影響平台 alert eval pipeline）。
3. **Auth / Token lifecycle**（強制）— Token 簽發 / 失效 / 替換 / 撤銷的 surface。
4. **平台 engineering cost**（限制）— 自寫 endpoint + 多 tenant rewrite engine 是 6+ month workload；對 v2.9.0 epic 不現實。

### 設計討論紀錄

[Issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) 記錄四輪 strategic discussion + 兩輪 Gemini adversarial review，locked decision 摘要：

- **不自寫 endpoint**：用既有開源 proxy（vmauth / prom-label-proxy）做 label injection
- **MVP 2-tier policy**（Platform whitelist + Tenant subset）— Domain layer 暫 drop 到 Future Work
- **Token TTL 4h + 無 server-side revocation list** — 明寫 trade-off 換實作簡單
- **Blast radius 三件組**：concurrency cap / request timeout / series count cap
- **3-tier permission model**（include compliance / audit 角色分離）→ Future Work，等 compliance 客戶觸發再做

## 決策

### 主決策

**採 vmauth（VictoriaMetrics 客戶）/ prom-label-proxy（Prometheus 客戶）作為 label-enforced read proxy，不自寫 federation endpoint。**

兩個 proxy 都是該 ecosystem 的 first-party / well-known 開源工具（vmauth 是 VictoriaMetrics 官方組件；prom-label-proxy 是 prometheus-community 維護）。Platform 側只負責：

1. **Helm chart** 把 vmauth / prom-label-proxy 拉進部署
2. **tenant-api token endpoint** 簽發 / 列舉 / refresh tenant federation token
3. **Policy schema validation**（platform whitelist + tenant subset 兩層）
4. **Audit log + anomaly metric**（誰拉了什麼、拉多少、是否超 cap）

> **實作修正（IV-2a, [#506](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/506)）**：上方「vmauth（VM 客戶）/ prom-label-proxy（Prom 客戶）」的配對有兩個架構誤解，IV-2a 實作後修正為 **只用 prom-label-proxy**：
> 1. vmauth 是 auth router，**不**解析 PromQL、不注入 label matcher —— 它的多租戶靠把使用者路由到 VictoriaMetrics **cluster** 的 `accountID` 路徑達成，對單機 VM 無隔離能力。
> 2. 更關鍵：vmauth 靠**靜態 `auth.yml`** 的 username / bearer token 路由，無法消化 tenant-api **動態簽發**的 RS256 federation JWT（4h TTL）—— 每次簽發都重寫 auth.yml 不可行。
>
> 故 `helm/federation-proxy` chart 只部署 prom-label-proxy（front Prometheus / Thanos / VictoriaMetrics 單機，VM 相容 Prom query API）。**VictoriaMetrics cluster** 不走本 chart：其 Layer 3 隔離由 API Gateway（IV-2b / #507）直接 URL rewrite 到 `/select/<accountID>/prometheus/` 處理（gateway 已從 verified JWT 取得 `tenant_id`）。詳 `helm/federation-proxy` chart README。

### MVP 範圍（2-tier policy）

```
┌───────────────────────────────────────────────┐
│ Platform whitelist (maintainer-managed)       │
│ 允許 tenant 拉的 metric name + label 範圍上限 │
└───────────────────────────────────────────────┘
                    ↓ intersect
┌───────────────────────────────────────────────┐
│ Tenant subset (tenant-self-managed via API)   │
│ Tenant 從 whitelist 中選自己要拉的子集        │
└───────────────────────────────────────────────┘
                    ↓ inform（非 enforce）
┌───────────────────────────────────────────────┐
│ prom-label-proxy                              │
│ 強制注入 tenant="<X>" 到所有 query            │
└───────────────────────────────────────────────┘
```

> **Enforcement model（IV-2e 實作修正）**：上圖第 3 層原列「拒絕白名單外的 metric_name」是 architectural hallucination —— prom-label-proxy **只做 label 注入、無 metric-name allowlist 能力**，gateway 也無法可靠地用 regex 從 PromQL AST 攔截 metric name。故 **whitelist 在 query path 不被強制執行**。跨租戶隔離 100% 來自 proxy 的 `{tenant="<X>"}` 注入：租戶若查 whitelist 外的 metric，proxy 一樣注入它自己的 tenant label，它只會拿到自己的資料（查自己的 custom metric 因此是 feature，不是漏洞）。**whitelist 的定位是 governance / discovery** —— 決定 UI catalogue、admission validator（IV-2e）的檢查標的、租戶 subset 策展的依據，**不是** hard data-plane security boundary。tenant subset ⊆ whitelist 的不變式同理為治理一致性、非安全邊界：靜態檔案過期時以 read-repair（讀取端取交集）修復，不掃改租戶檔。

**Domain layer**（讓 tenant 內再分 sub-team scope）**留 Future Work**。理由：v2.9.0 customer base 是「單一 SRE/NOC team 拉自己 tenant 全部」，sub-team scope 是更晚的需求；現在做會增加 2-tier → 3-tier schema 複雜度，但無 customer signal。

### 前提約束（Prerequisites — IV-2 blocker，**adversarial review surfaced**）

#### Data-layer Label Enrichment Guarantee

> **IV-2.0 audit 修正（[#505](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/505)）**：本平台 data-layer 既有的租戶 label 名為 **`tenant`**，非 `tenant_id`（Prometheus relabel `target_label: tenant`、threshold-exporter、tenant-scoped rule pack 一律 `on(tenant)`）。本 ADR 原稿以 `tenant_id` 為 label 名是 prose 與實作的落差；federation 一律對齊 `tenant`，`helm/federation-proxy` 的 `-label` 已改 `tenant`。本 ADR prose 凡指「proxy 注入到 metric 的 label」一律已對齊為 `tenant`。JWT claim 仍名 `tenant_id`（claim 名與 metric label 名為獨立命名空間，互不要求一致）—— §Token model 與 token JSON 範例的 `tenant_id` 即 claim，不受此修正影響。盤點詳 [`federation-label-enrichment-audit.md`](../internal/federation-label-enrichment-audit.md)。

所有 platform whitelist 列入的 metric，平台**必須**確保在 ingest / scrape 階段
（Prometheus `scrape_configs.relabel_configs`、VictoriaMetrics `relabel_config`、
或統一 ingestion pipeline）已可靠注入 `tenant` label。

**為什麼這是 prerequisite**：proxy 在 query path 強制把 `{tenant="<X>"}` 注入到所有
selector。如果某 metric 原生不帶 `tenant` label，query 結果就是 empty vector——
tenant 看到「dashboard 空白」，會報修「federation 壞了」，SRE 要從 token 一路查到 scrape
config 才能發現是 data-layer 沒打 label。是個典型的 silent-failure 地雷。

**典型踩坑 metric 來源**（無 `tenant` native label）：

- `container_*`（cAdvisor）
- `node_*`（node-exporter）
- `kube_*`（kube-state-metrics）
- 任何透過 federation 從上游 Prom 抓進來、上游沒打 tenant label 的 metric

**Admission validator（soft gate + force override，**adversarial review surfaced**）**：whitelist 加入新 metric 時，IV-2 admission validator 對「過去 24h 該 metric 在後端 storage 至少有一筆帶 `tenant` label 的 sample」做檢查。**輸出分三種**：

| 觀察結果 | Validator 行為 | 為何 |
|---|---|---|
| 有 sample 且帶 `tenant` label | ✅ Pass | 預期情境 |
| 有 sample 但**無** `tenant` label | ⛔ **Hard block** | True positive failure mode——scrape config 沒打 label，這時讓 metric 進 whitelist 就是埋 empty-vector 地雷 |
| 過去 24h 完全無 sample | ⚠️ **WARN，不 block**——要求 admin 顯式 `--force` 才能通過 | Cold start（新 tenant deploy 新 service）/ sparse metric（`critical_error_count` 週發一次）都是合法情境；hard block 會卡死合法 whitelist 更新 |

`--force` bypass 路徑必須寫進 audit log：「Bypassed label enrichment check by `<user>`: reason=`<cold-start|sparse-metric|other>`」。**為什麼不直接 hard block 全部**：cardinality guard 也是 soft gate 設計（[ADR-017](./017-defaults-yaml-inheritance-dual-hash.md) precedent）——平台級防護要區分「結構性錯誤（hard block）」與「資料時序性缺漏（warn + 人工確認）」，否則 false positive 把合法 ops 鎖死。

### Token model

| 屬性 | 設計選擇 | 理由 / trade-off |
|---|---|---|
| **簽發方** | tenant-api `/api/v1/federation/tokens` POST | 與既有 tenant-api auth pipeline 一致；不另起 service |
| **TTL** | 4h（hardcoded MVP，Helm value 可調） | 4h 在「短到撤銷不重要」與「長到 ops 不痛苦」之間平衡 |
| **撤銷機制** | ⚠️ **無 server-side revocation list**（MVP）。⚠️⚠️ **Compensating control 強制要求**：API Gateway 必須實作嚴格 per-token + per-IP rate limiting（見 §Blast radius Layer 2），確保 4h 曝險窗內外洩 token 即便被併發濫用也無法把後端 storage CPU 打滿 | Trade-off：避免 token revocation table 的 cache / propagation / TTL 複雜度。換來代價：token 洩漏後**最多** 4h 曝險（前提：gateway rate limit 確實到位；缺它則 4h 曝險升級為 4h DoS 樂園）。Gateway rate limit **不是** nice-to-have，是放棄 revocation 的對價 |
| **Scope binding** | token 內 embed `tenant_id` claim，proxy 強制 inject | proxy 不能信 query string 帶的 tenant_id |
| **Refresh** | 過期前 tenant 自行重新簽發；無 sliding refresh | 簡化實作；4h 重簽對 self-service tenant 不痛 |

> **實作（IV-2l, [#518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/518)）**：RS256 簽章金鑰的生成 / 輪替由 `da-tools fed-key` 負責 —— 私鑰吐成 Kubernetes Secret manifest（不落地）、公鑰吐成 JWKS 供 gateway。每把公鑰的 `kid` 為其 **RFC 7638 thumbprint**;tenant-api 簽 token 時對載入金鑰算同一 thumbprint、注入 `kid` header,故 gateway `jwt_authn` 可用 `kid` O(1) 選鑰(輪替期 JWKS 多鑰並存也不會放大壞簽章 flood 的 RSA 成本)。計畫性輪替走 grace-period overlap、私鑰外洩走緊急汰換 —— 標準流程見 [`federation-key-rotation-runbook.md`](../internal/federation-key-rotation-runbook.md)。

### Blast radius：3-layer defense（**proxy 一個人擋不住**——adversarial review surfaced）

> ⚠️ **架構幻覺修正**：本 ADR 早期 draft 把三件組（concurrency / timeout / series cap）全
> 放在 proxy layer，**這是錯的**。`prom-label-proxy` 是極輕量的 label-injection middleware：
> 它解析 PromQL AST 注入 label，然後**把 request 原封不動轉給後端**，不解析 response body
> （所以無法擋 series cap），也不追蹤 per-token state（所以無法做 per-token concurrency）。
> `vmauth` 雖有 per-user concurrency / timeout 原生支援，但 series cap 同樣不在它職責。
>
> 正確架構：blast radius 三件組要**分布在三層**，每層做它最適合的事。

#### Layer 1 — Storage backend（series / sample 上限）

| 後端 | Flag / Config | 防護對象 |
|---|---|---|
| Prometheus | `--query.max-samples`（單 query sample 總數上限） + `--query.timeout`（global query 超時） | 防 OOM-by-query |
| VictoriaMetrics | `-search.maxUniqueTimeseries`（單 query 唯一 series 上限） + `-search.maxSamplesPerQuery` + `-search.maxQueryDuration` | 同上 |

平台必須在 v2.9.0 部署時**強制配置**這些 flag。**Starting default**（與下方 §Default 值 rationale 表一致；IV-2 觀察實際 customer query pattern 後 tuning）：

| Flag | Starting default | Tuning range | Rationale |
|---|---|---|---|
| Prom `--query.max-samples` | 5M | 5M–50M（Prom native 預設 50M）| federation read 比 internal eval 嚴；5M 是「典型 1000 series × 1d @ 30s scrape ≈ 3M」之上的保守起點，IV-2 觀察 false-positive 再放寬 |
| VM `-search.maxUniqueTimeseries` | 100k | 50k–300k（VM native 預設 300k）| 同上邏輯；100k 對應「能撐 cluster-wide dashboard panel 但擋 `count by (instance) (...)` 意外高基查詢」 |
| Prom `--query.timeout` / VM `-search.maxQueryDuration` | 30s | 與 Layer 2 timeout 對齊 | Defense-in-depth：gateway timeout 沒切斷時 storage 自己會超時 |

#### Layer 2 — API Gateway / Ingress（per-token concurrency + per-token rate limit）

`prom-label-proxy` 沒有 per-token concurrency 原生支援 → 必須由前置 **API Gateway**（Nginx /
Envoy / Traefik，依平台 Helm 既有 ingress 選擇）擋。Gateway 從 JWT claim 解出 `token_id`，
用它當 rate-limit key。

`vmauth` 對 VM 客戶可走原生 per-user rate limit（`max_concurrent_requests` / `max_request_duration`
on user config），不一定要 gateway 也行——但**為了統一架構**，建議仍走 gateway layer 集中
管理（Helm chart 可選 mode A: gateway-only / mode B: gateway + vmauth 雙層；後者對 VM 客戶
是 defense-in-depth）。

```nginx
# 概念示意（zone 宣告與 JWT claim 抽取為簡化展示，實際 Helm chart 須補完整）
http {
    # zone 宣告（http context；key 為 JWT token_id，由 njs/auth_request 抽出後寫到 $token_id）
    # rate=30r/m: TSDB read 不是 web API——平均每 2s 一次足夠 Grafana 30s refresh × 多 panel；
    # 故意比 web API 預設的 10r/s 嚴 ~20x，因 PromQL query CPU/mem cost 遠高於無狀態 API
    limit_req_zone  $token_id zone=per_token_rl:10m  rate=30r/m;
    limit_conn_zone $token_id zone=per_token_conn:10m;

    server {
        location /federation/ {
            limit_req zone=per_token_rl burst=10 nodelay;  # 30 req/min sustained, burst 10
            limit_conn per_token_conn 4;                   # 4 concurrent per token
            proxy_read_timeout 30s;
            proxy_send_timeout 30s;
            proxy_pass http://prom-label-proxy/;
        }
    }
}
```

**Rate limit rationale（**adversarial review surfaced**）**：

- `rate=30r/m`（平均每 2s 一次 query）對應 Grafana 預設 dashboard refresh interval（30s）× 多 panel 場景的合理上限。一個 oncall 開 10-panel dashboard 一次 refresh ≈ 20r/m，30r/m 留 headroom 但擋 query-level DoS
- `burst=10`：容忍「打開 dashboard 一次性發 ~10 個 panel query」的初始 burst
- **NOT** `rate=10r/s`（web API 數量級）：PromQL query 是 CPU/memory-expensive 操作；一個外洩 token 在 4h 曝險窗 + 10r/s = 144k 次 query，足以把後端 storage CPU 打滿。Web API 預設值對 TSDB read 而言錯了一個數量級
- **Tuning corridor**：若 customer Grafana dashboard 較 panel-heavy（30+ panels），可調高到 60r/m；若觀察到 abuse，降到 15r/m

**Rate limit key 抽取**：Nginx `js_set`（NJS）/ Envoy `header_to_metadata_filter` 從
`Authorization: Bearer <jwt>` 解出 token claim 寫到變數，用 `token_id` 當 limit key
（**不是用 IP**，否則公司 NAT 後所有 tenant 互相影響）。完整 Helm template 細節
留 IV-2 sub-issue。

> **實作（IV-2b, [#507](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/507)）**：上方 nginx 區塊為概念示意；gateway 正式以 **Envoy** 實作，交付為 `helm/federation-gateway` chart。選 Envoy 的關鍵：最不能錯的 RS256 驗章交給原生 `jwt_authn` filter（純設定、audited、無 alg-confusion footgun）。filter chain cheap-before-expensive：per-IP `local_ratelimit` → `jwt_authn` → Lua（revoked-set 查驗 + tenant header 覆寫）→ per-token / per-tenant `local_ratelimit` → router。per-token + per-tenant 雙層限流以 wildcard descriptor 達成（防單 token 濫用 + 防 round-robin ≤16 token 的 Sybil）。revoked-set 由 Lua filter time-gated 重讀 ConfigMap projected volume。詳 `helm/federation-gateway` chart README。

#### Layer 3 — Proxy（label injection + audit log）

`vmauth` / `prom-label-proxy` 在此層只做兩件事：

1. **Label injection**：強制把 `{tenant="<X>"}` 注入所有 selector（核心安全保證）
2. **Audit log**：記錄被改寫後的 query（給 platform ops 觀察 tenant 行為）

**⚠️ Metadata API 防護承諾（adversarial review surfaced，critical）**：

Proxy 的 enforcement 必須**同時涵蓋 Query API 與 Metadata API**，否則跨租戶拓樸資訊外洩：

| API class | Endpoints | 為何必須涵蓋 |
|---|---|---|
| Query API | `/api/v1/query`, `/api/v1/query_range` | 明顯目標 |
| **Metadata API** | `/api/v1/series`, `/api/v1/labels`, `/api/v1/label/<name>/values` | Grafana variable dropdown（`label_values(pod)` 等）走這些 endpoint。**沒擋 = tenant A 在自家 Grafana variable dropdown 看到 tenant B 的 pod name / instance / hostname → multi-tenant breach** |

> ⚠️ 上表 endpoints 為**最常見**且 Grafana 必用的子集，**非 exhaustive**。Prom HTTP API 還有 `/api/v1/metadata`、`/api/v1/targets`、VM `/api/v1/status/active_queries` 等也可能洩漏 metadata。**IV-2 acceptance criteria 必含「完整 Prom HTTP API surface audit」** 把所有 query/metadata endpoint 逐一驗 enforcement coverage（不只本表列出的 3 個）。

**工具具體注意點**：

- **prom-label-proxy**：必須**顯式啟用** label/metadata API enforcement（具體 flag 名因版本而異，現代版本一般是 `--enable-label-apis` 或近似 flag——IV-2 階段對使用版本確認；早期版本預設 OFF，歷史 bug fix log 有 corner case 漏網紀錄）。Helm chart 必須 hardcode 啟用，**不開放 customer override**
- **vmauth**：對應 user config 的 `src_paths`（or equivalent path-matching mechanism）必須涵蓋 metadata-style URL（VM 的 `/select/<tenant>/prometheus/api/v1/series` 等變體）；不能只列 `/api/v1/query`

**Smoke test 要求（IV-2 acceptance）**：簽 token A，用它連 `/api/v1/series?match[]={__name__=~".+"}` 查所有 series，驗證 response 不含任何 tenant B 的 label value。同樣對 `/api/v1/labels` 與 `/api/v1/label/pod/values` 各驗一次。Smoke test 加入 IV-2 acceptance criteria 並擴張至上述 non-exhaustive note 中列出的其他 endpoint。

#### Default 值 rationale（IV-2 實測可調）

| 控制項 | 預設值 | Where | Rationale |
|---|---|---|---|
| Concurrency / token | 4 | Gateway L7 | 一個 tenant 的 Grafana / 自管 alert eval / oncall 並行 query 通常 ≤ 3；4 留 headroom 擋明顯 abuse |
| Request timeout | 30s | Gateway L7 + storage backend | Grafana 預設 query timeout 在 30s 附近；長跑離線分析應走 batch export 非 federation 即時 path |
| Series per query | 100k | Storage backend | 100k series 是「撐得起 cluster-wide dashboard panel」與「不至於 OOM」的中間值；擋住 `count by (instance) (...)` 類意外高基查詢 |
| Sample per query | 5M | Storage backend | 與 series cap 互補：tenant 可能拉低基數但長時段範圍 |

**三層缺一不可，且職責不可錯位**：proxy 擋 label 注入；storage 擋 query 資源；gateway 擋
token-level abuse。錯放層（例如指望 proxy 擋 series cap）= 實作 IV-2 時工程師會發現做不到，
最後還是得自寫 middleware，**違背本 ADR 不自寫 endpoint 的初衷**。

### Audit log + anomaly metric

每個 federation request 記錄：

```jsonc
{
  "ts": "2026-05-11T10:23:00Z",
  "tenant_id": "db-anonymized-001",
  "token_id_prefix": "ftk_8a3f",
  "query": "rate(http_requests_total[5m])",
  "matched_whitelist_rule": "rate-aggregations-v1",
  "series_returned": 4231,
  "duration_ms": 1843,
  "status": "ok" | "rejected_whitelist" | "rejected_blast_radius" | "auth_failed"
}
```

新 metric `tenant_federation_requests_total{tenant,status}` + sentinel alert（rejection rate 異常上升 → notify platform ops）。

## 為什麼不用其他方案

### 替代方案 A：Self-built federation endpoint（純自寫）

「在 tenant-api 加 `/api/v1/federation/read` proxy 到後端 storage，自己做 label rewrite。」

**問題**：

1. **Label sanitization 是地雷**：後端 storage 不同 metric 的 label 矩陣不一致（有些 metric 帶 `tenant`，有些只帶 `cluster_id` + reverse mapping）。自寫 sanitizer 漏一個矩陣維度 = multi-tenant breach。vmauth / prom-label-proxy 在 production 跑了多年，這類 corner case 已被解掉
2. **Engineering cost**：6+ months 寫一個「比現成開源 proxy 還弱」的東西，無 value-add
3. **Maintenance cost**：自寫的安全 patch 要自己追

**結論**：拒絕。Open-source proxy 是「贏家通吃」場景：自寫只虧不賺。

### 替代方案 B：純 RBAC on remote_read（無 proxy）

「給 tenant 後端 Prom 開 read-only user，靠 storage 層 RBAC 做隔離。」

**對 VM 客戶**：VictoriaMetrics 沒有 native multi-tenant RBAC；vmauth 本身就是補這缺口的 label-injection 機制——這條路其實是 Plan D 的別名，不另算選項。

**對 Prom 客戶**：

1. **Prometheus 本身沒 multi-tenant RBAC**：要靠 Cortex / Mimir 這種第三方層做，但既有平台後端不是 Mimir（看 `byo-prometheus-integration.md`，平台**不**強制客戶用特定後端）
2. **PromQL metadata leakage**：tenant 可以寫 `count by (tenant) (up)` 之類的 query 探測其他 tenant 是否存在；純 RBAC（檔案級權限 / basic auth）擋不掉這類查詢層資料外洩。**proxy 方案如 prom-label-proxy 在 query path 強制注入 `{tenant="<X>"}` matcher 才擋得住**——這正是它存在的原因
3. **Audit 缺口**：純 RBAC 只記「誰連線」，不記「誰拉了什麼 query / 多少 series」；compliance 要求的查詢層稽核做不到

**結論**：拒絕（對 Prom 客戶為實質拒絕，對 VM 客戶為 Plan D 別名）。沒有 universal 後端 RBAC；prom-label-proxy 本來就是補這缺口的方案。

### 替代方案 C：Push-based（remote_write to tenant）

「平台主動 remote_write tenant 自己的 metrics 到 tenant 端 Prom/VM。」

**問題**：

1. **逆向 trust**：平台要主動連到 tenant 內網，network ingress 與 firewall 規則複雜度爆炸
2. **Tenant 控制權**：tenant 沒法選擇拉什麼、何時拉、拉多少；只能照平台 push 的 schedule
3. **Backpressure**：tenant 端 storage 滿了 / 慢了，會反壓回平台 remote_write queue
4. **Multi-tenant ergonomic 倒置**：tenant N 個的話平台要維護 N 條 outbound remote_write，運維極差

**結論**：拒絕。Pull-based 是 Prom ecosystem 的 first-principle，不要違背它。

### 替代方案 D：vmauth / prom-label-proxy（採用）

優點（vs A/B/C）：

- **Engineering cost**：~56h IV-2（adversarial review 後從 44h 上調，見 §實作計畫）vs 自寫 6+ months
- **Label injection 安全性**：兩個 proxy 在 label injection / query rewrite 範疇是 multi-year production-hardened（**注意 scope 限定**：proxy 不負責 series cap / 並發控制——那些靠 storage backend + API gateway，見 §Blast radius 3-layer。但每層用的都是 well-established 工具，不是自寫）
- **Ecosystem 對齊**：VM 客戶看到 vmauth = 熟悉、Prom 客戶看到 prom-label-proxy = 熟悉，沒新東西要學
- **後端解耦**：proxy 在「客戶 → 後端 storage」中間，後端 storage 換什麼（Prom / VM / Mimir）proxy 都吃

缺點（已接受）：

- 兩個 proxy 兩套 config schema 要維護（platform Helm chart 抽象掉 90%，剩 10% 是兩邊原生差異）
- vmauth 與 prom-label-proxy 的 feature parity 不對稱（vmauth 有 per-user concurrency / timeout 原生支援，prom-label-proxy 全靠 API gateway 補；本 ADR 統一走 gateway layer 集中管理）
- **3 component coordination**（proxy + gateway + storage backend）比自寫單 endpoint 多三套變動點，每次 upstream 升級要 multi-layer regression

**結論**：採用。本 ADR 主決策。Multi-component 缺點 acceptable，因為每層都是 well-established 工具，不是自寫——比自寫 endpoint 的單點變成「全平台單點 multi-tenant breach 風險」要好。

## 實作計畫

| 階段 | 內容 | effort |
|---|---|---|
| 1. ADR draft + review | 本 ADR ship + Gemini cross-check | 12h（本階段 = IV-1） |
| 2. Proxy 整合 + Helm chart | vmauth / prom-label-proxy 部署 + platform 抽象層 config | 8h |
| 3. API Gateway rate-limit Helm chart support | Nginx/Envoy templates + JWT token claim extraction（**adversarial review surfaced**——proxy 沒 per-token concurrency 原生支援，必須 gateway 補） | 5h |
| 4. Storage backend tuning | Helm chart 加 `--query.max-samples` / `-search.maxUniqueTimeseries` 等 flag 預設 + tuning guide | 2h |
| 5. Token endpoint | tenant-api `POST/GET/DELETE /api/v1/federation/tokens` + token signing + persistence | 12h |
| 6. Policy schema + admission validator | 2-tier whitelist/subset schema + JSON schema + **`tenant` label-enrichment 驗證**（**adversarial review surfaced**——擋 empty-vector silent failure） | 13h |
| 7. Audit log + anomaly metric | Structured log + sentinel alert + Grafana dashboard fragment | 6h |
| 8. Metadata API smoke test | 簽 token A 驗 `/series`/`/labels`/`/label/<name>/values` 都不洩 tenant B 拓樸（**adversarial review surfaced**） | 2h |
| 9. 文件 | `docs/integration/tenant-federation.md` user-facing guide + Helm chart README + sample policy + 3-layer architecture 文件 | 10h |
| **IV-2 Total (excluding IV-1)** | — | **~58h** + ADR 12h = **~70h** |

**Effort 估算（adversarial review 累計後上修）**：原 [issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) IV-2 body 估 44h，但其架構假設「proxy 一個人擋所有 blast radius」是 architecture hallucination（見 §Blast radius adversarial review note）。**第一輪 adversarial review** 修正為 3-layer defense 後 IV-2 增加 +12h：

- Gateway rate-limit Helm 抽象（+5h，line 3）
- Storage backend tuning section（+2h，line 4）
- Admission validator `tenant` enrichment 驗證（+3h 進 line 6 = 10h → 13h）
- 3-layer 架構文件（+2h 進 line 9 = 8h → 10h）

**第二輪 adversarial review** 又補 +2h（line 8 Metadata API smoke test，覆蓋 `/api/v1/series` 等 endpoints 不洩跨 tenant 拓樸）。Admission validator soft-gate + `--force` 邏輯吸收進 line 6 既有預算。Rate limit 值修正（10r/s → 30r/m）零工時 delta。

合計 +14h，IV-2 從 44h → 58h，全 epic 從 56h → 70h。

IV-2 拆 sub-issue 在 v2.9.0 epic kickoff 時做，不在本 ADR 範圍；最終 effort 以 sub-issue 拆分後實測為準。

## 後果（Consequences）

### 正面

- Tenant 拿到 standards-compliant federation 介面（Prom remote_read / VM read API），整合既有 oncall workflow 零摩擦
- Platform engineering cost 從 6+ months 壓到 70h
- Multi-tenant **label-level** isolation 由 production-hardened proxy 保證；resource isolation 由 storage backend 強制；rate isolation 由 API gateway 強制——三層每層都是 well-established 工具，不是自寫
- 後端 storage 演進不影響 federation 介面（proxy 抽象層解耦）
- 2-tier policy 讓 platform 與 tenant 各自有 audit-able 控制點

### 負面

- 兩套 proxy 兩套 config 要維護（Helm chart 抽象 + 文件交代差異）
- **3-component coordination**（proxy + API gateway + storage backend）——每次 upstream 升級要 multi-layer regression；任一層配錯都會造成 silent failure 或安全破口
- Token TTL 4h + 無 revocation list = token 洩漏曝險窗 4h；**critical dependency on gateway rate limit**——gateway 配錯 → 4h 變 DoS 樂園
- **Data-layer prerequisite**：所有 whitelist 列入的 metric 必須在 ingest/scrape 階段帶 `tenant` label；admission validator 是 IV-2 必需，不是 nice-to-have
- **Metadata API coverage 是非顯性風險**：prom-label-proxy `--enable-label-apis` 是顯式 flag、vmauth `src_paths` 必須涵蓋 metadata URL 變體——Helm chart 設計時容易只想到 `/api/v1/query` 而漏掉 `/series` / `/labels`；smoke test 是這個非顯性風險的安全網
- **Admission validator soft-gate 留人為判斷空間**：`--force` 路徑（針對 cold-start / sparse-metric）依賴 admin 判斷正確性；audit log 是事後追蹤手段，misuse 仍可能埋雷
- Domain layer scope 在 MVP 缺席，sub-team 客戶來時要 schema migration
- vmauth / prom-label-proxy 任一上游 break change，platform Helm chart 須同步更新
- Audit log size：每個 federation request 寫一筆 → tenant 高頻拉時 log volume 不小，需 retention policy

### 中性

- 新 tenant-api endpoint 一組（`/api/v1/federation/*`）— 比照既有 `/api/v1/events` / `/api/v1/tenants` 風格
- 文件多一份 `docs/integration/tenant-federation.md` — 客戶 onboarding 多一條閱讀路徑（與 [`federation-integration.md`](../integration/federation-integration.md) ADR-004 平行）
- 新增 platform Grafana dashboard：3-layer rejection rate（gateway 429 / proxy 4xx / storage 5xx）給 platform ops 觀察 blast radius hit rate

## Future Work

按優先排序：

1. **Server-side revocation list**（觸發條件：compliance 客戶 RFP 顯式要求 / 第一次 token 洩漏事件）。設計時須處理 cache propagation TTL（典型 5min vs 即時 invalidate 二選一）
2. **Domain-layer policy（3-tier）**（觸發條件：tenant 內 sub-team 隔離需求）。schema 從 `{platform_whitelist, tenant_subset}` 擴成 `{platform_whitelist, tenant_subset, domain_scope}`
3. **3-tier permission model**（compliance / audit / operator 角色分離）— 觸發條件：合規客戶（SOX / ISO 27001 / SOC 2）。可能與「server-side revocation」同時 trigger，一起做
4. **Series cap auto-tuning**：依 tenant 歷史 federation 流量 dynamic 調 `max_series_per_response`（避免一刀切過鬆 / 過嚴）
5. **Federation 流量 chargeback**：Audit log 加總成 tenant 月度 federation usage report（多租戶平台 commercial 需求）
6. **Long-lived service-account credentials**（觸發條件：稀疏 pull tenant——每天只拉一次但 4h TTL 強制每次重簽，operationally painful）。設計選擇：(a) 延長 TTL 至 24h-7d；(b) service-account credentials 可換 short-lived federation token（auth 分層）；(c) 強制 token rotation via webhook callback。三者各有 trade-off，IV-2 觀察到實際 sparse-pull pattern 後決定

## 關聯

- **[ADR-004 (Federation — Central-Exporter-First)](./004-federation-central-exporter-first.md)** — 平台內部多叢集 federation。本 ADR 與之**互補**（platform-internal vs cross-boundary），非取代
- **[ADR-009 (Tenant Manager CRUD API)](./009-tenant-manager-crud-api.md)** — tenant-api 既有 endpoint pattern，本 ADR 的 token endpoint 沿用其 conventions
- **[`docs/integration/federation-integration.md`](../integration/federation-integration.md)** — ADR-004 的 user-facing guide；本 ADR 將有對應 `docs/integration/tenant-federation.md`（IV-2 ship 時）
- **[`docs/integration/victoriametrics-integration.md`](../integration/victoriametrics-integration.md)** — §7「已知 gap / Future Work」table 已預先 cross-link 本 ADR + 註明 v2.9 epic
- **[Issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380)** — IV-1 deliverable（本 ADR）+ IV-2 implementation epic

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [004-federation-central-exporter-first](004-federation-central-exporter-first.md) | ⭐⭐⭐ |
| [009-tenant-manager-crud-api](009-tenant-manager-crud-api.md) | ⭐⭐ |
| [federation-integration](../integration/federation-integration.md) | ⭐⭐ |
| [victoriametrics-integration](../integration/victoriametrics-integration.md) | ⭐⭐ |
| [README](README.md) | ⭐⭐ |
