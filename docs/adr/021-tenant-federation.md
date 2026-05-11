---
title: "ADR-021: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint"
tags: [adr, federation, multi-tenant, security]
audience: [platform-engineers, contributors]
version: v2.8.0
lang: zh
id: ADR-021
tracking_kind: adr
status: proposed
domain: tenant-api
created_at: 2026-05-11
updated_at: 2026-05-11
---

# ADR-021: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint

> Tenant-user 拉取**自己**的 metrics 子集回 tenant 側 infra 自管 federation。
> 平台**不自寫** federation endpoint，採 vmauth（VM 客戶）/ prom-label-proxy（Prom 客戶）做 label-enforced rewriting。
>
> 與 [ADR-004 (Federation — Central-Exporter-First)](./004-federation-central-exporter-first.md) 是兩件事：ADR-004 是**平台內部**多叢集 federation（中央 exporter 服務邊緣 Prometheus），本 ADR 是**跨平台邊界** federation（tenant 把自己的 metrics 拉回 tenant 自有 Prom/VM 自管）。

## 狀態

🟡 **Proposed**（issue [#380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) IV-1 deliverable，v2.8.0 起草）

> EN mirror：本 ADR 進入 `Accepted` 後再起 EN 翻譯（對齊 ADR-020 雙語策略）。

## 背景

### 客戶需求

進入 v2.8.0 客戶導入準備階段，多個 prospective customer 在 RFP / pre-onboarding 訪談中提出需求：

- 我們已有自己的 Prometheus / VictoriaMetrics infra（NOC / SRE team 自管）
- 想把**屬於我們自己** tenant 的 metrics 子集拉回我們 infra，做：
  - 自己 SRE team 的 long-term retention（平台側預設 retention 可能不滿足合規）
  - 整合到自己既有 Grafana dashboard / oncall workflow
  - 自管告警 evaluation（不依賴平台 Alertmanager）

**注意定位**：這不是「平台幫客戶 federation」（那是 [ADR-004](./004-federation-central-exporter-first.md)），是「**客戶從平台拉自己的 metrics 出去**」。資料流向相反，trust boundary 也不同。

### 既有 federation 架構覆蓋空白

| 場景 | 既有方案 | 缺口 |
|---|---|---|
| 平台內部多叢集 federation | ADR-004 中央 exporter + 邊緣 remote_read | ✅ 覆蓋 |
| 平台自管 alert eval | tenant-api SSE + Alertmanager | ✅ 覆蓋 |
| **Tenant 拉自己 metrics 回 tenant 側自管** | ⛔ **無方案** | 🎯 本 ADR |

### 為什麼這是個棘手的問題

「給 tenant 開一個 read endpoint 把自己 metrics 拉出去」表面簡單，實際有四個交叉約束：

1. **Multi-tenant isolation**（強制）— Tenant A 絕對不能讀到 Tenant B 的資料。Platform 後端 storage（VictoriaMetrics / Mimir / Prom）通常**沒有強制的 label filter**：寫一個自寫 endpoint 容易在 label sanitization 漏一個矩陣維度（cluster / node / namespace 在某些 metric 上不帶 tenant_id），變成 multi-tenant breach。
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
                    ↓ enforce at proxy
┌───────────────────────────────────────────────┐
│ vmauth / prom-label-proxy                     │
│ 強制注入 tenant_id="<X>" 到所有 query         │
│ 拒絕白名單外的 metric_name                    │
└───────────────────────────────────────────────┘
```

**Domain layer**（讓 tenant 內再分 sub-team scope）**留 Future Work**。理由：v2.9.0 customer base 是「單一 SRE/NOC team 拉自己 tenant 全部」，sub-team scope 是更晚的需求；現在做會增加 2-tier → 3-tier schema 複雜度，但無 customer signal。

### Token model

| 屬性 | 設計選擇 | 理由 / trade-off |
|---|---|---|
| **簽發方** | tenant-api `/api/v1/federation/tokens` POST | 與既有 tenant-api auth pipeline 一致；不另起 service |
| **TTL** | 4h（hardcoded MVP，Helm value 可調） | 4h 在「短到撤銷不重要」與「長到 ops 不痛苦」之間平衡 |
| **撤銷機制** | ⚠️ **無 server-side revocation list**（MVP） | Trade-off：避免 token revocation table 的 cache / propagation / TTL 複雜度。換來的代價：token 洩漏後最多曝險 4h。對 v2.9.0 MVP 可接受；compliance 客戶觸發時改設計 |
| **Scope binding** | token 內 embed `tenant_id` claim，proxy 強制 inject | proxy 不能信 query string 帶的 tenant_id |
| **Refresh** | 過期前 tenant 自行重新簽發；無 sliding refresh | 簡化實作；4h 重簽對 self-service tenant 不痛 |

### Blast radius 三件組（必須全部到位）

```yaml
# vmauth / prom-label-proxy 共通 config schema
federation:
  blast_radius:
    max_concurrent_requests_per_token: 4    # 並發查詢上限
    request_timeout_seconds: 30             # 單一 query 超時
    max_series_per_response: 100000         # 單 query 結果 series 上限
```

| 控制項 | 觸發行為 | 防護對象 |
|---|---|---|
| `max_concurrent_requests_per_token` | 超過時 HTTP 429 + audit log | 防 tenant 並發轟炸 |
| `request_timeout_seconds` | 超時 HTTP 504 + audit log | 防 unbounded query 拖垮 storage |
| `max_series_per_response` | 超量直接 truncate + HTTP 413 | 防 high-cardinality scan 把記憶體吃光 |

**三件組缺一不可**：缺並發 cap 等於開門讓 tenant DoS；缺 timeout 等於開門讓 tenant 跑 30 分鐘的 query；缺 series cap 等於開門讓 tenant 拉 `up` 全 fleet。

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

新 metric `tenant_federation_requests_total{tenant_id,status}` + sentinel alert（rejection rate 異常上升 → notify platform ops）。

## 為什麼不用其他方案

### 替代方案 A：Self-built federation endpoint（純自寫）

「在 tenant-api 加 `/api/v1/federation/read` proxy 到後端 storage，自己做 label rewrite。」

**問題**：

1. **Label sanitization 是地雷**：後端 storage 不同 metric 的 label 矩陣不一致（有些 metric 帶 `tenant_id`，有些只帶 `cluster_id` + reverse mapping）。自寫 sanitizer 漏一個矩陣維度 = multi-tenant breach。vmauth / prom-label-proxy 在 production 跑了多年，這類 corner case 已被解掉
2. **Engineering cost**：6+ months 寫一個「比現成開源 proxy 還弱」的東西，無 value-add
3. **Maintenance cost**：自寫的安全 patch 要自己追

**結論**：拒絕。Open-source proxy 是「贏家通吃」場景：自寫只虧不賺。

### 替代方案 B：純 RBAC on remote_read（無 proxy）

「給 tenant 後端 Prom 開 read-only user，靠 storage 層 RBAC 做隔離。」

**問題**：

1. **Prometheus 本身沒 multi-tenant RBAC**：要靠 Cortex / Mimir 這種第三方層做，但既有平台後端不是 Mimir（看 `byo-prometheus-integration.md`，平台**不**強制客戶用特定後端）
2. **VictoriaMetrics 有 vmauth 但本質就是 label-injection proxy** — 等於兜了一圈回到方案 D
3. **PromQL injection**：tenant 可以寫 `count by (tenant_id) (up)` 之類的 query 探測其他 tenant 是否存在；純 RBAC 擋不掉這類 metadata leakage

**結論**：拒絕。沒有 universal 後端 RBAC；prom-label-proxy 本來就是補這缺口的方案。

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

- **Engineering cost**：~56h（一個 v2.9.0 epic）vs 自寫 6+ months
- **安全性**：兩個 proxy 都是 multi-year production-hardened
- **Ecosystem 對齊**：VM 客戶看到 vmauth = 熟悉、Prom 客戶看到 prom-label-proxy = 熟悉，沒新東西要學
- **後端解耦**：proxy 在「客戶 → 後端 storage」中間，後端 storage 換什麼（Prom / VM / Mimir）proxy 都吃

缺點（已接受）：

- 兩個 proxy 兩套 config schema 要維護（platform Helm chart 抽象掉 90%，剩 10% 是兩邊原生差異）
- vmauth 與 prom-label-proxy 的 feature parity 不 100%（series cap 在兩邊都有但語法不同），platform 文件須交代差異

**結論**：採用。本 ADR 主決策。

## 實作計畫

| 階段 | 內容 | effort |
|---|---|---|
| 1. ADR draft + review | 本 ADR ship + Gemini cross-check | 12h（本階段 = IV-1） |
| 2. Proxy 整合 + Helm chart | vmauth / prom-label-proxy 部署 + platform 抽象層 config | 8h |
| 3. Token endpoint | tenant-api `POST/GET/DELETE /api/v1/federation/tokens` + token signing + persistence | 12h |
| 4. Policy schema + validator | 2-tier whitelist/subset schema + admission validator + JSON schema for tenant API | 10h |
| 5. Audit log + anomaly metric | Structured log + sentinel alert + Grafana dashboard fragment | 6h |
| 6. 文件 | `docs/integration/tenant-federation.md` user-facing guide + Helm chart README + sample policy | 8h |
| **IV-2 Total (excluding IV-1)** | — | **~44h** + ADR 12h = **~56h** |

**Effort 估算與 [issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) 一致**。IV-2 拆 sub-issue 在 v2.9.0 epic kickoff 時做，不在本 ADR 範圍。

## 後果（Consequences）

### 正面

- Tenant 拿到 standards-compliant federation 介面（Prom remote_read / VM read API），整合既有 oncall workflow 零摩擦
- Platform engineering cost 從 6+ months 壓到 56h
- Multi-tenant isolation 由 production-hardened proxy 保證，非自寫
- 後端 storage 演進不影響 federation 介面（proxy 抽象層解耦）
- 2-tier policy 讓 platform 與 tenant 各自有 audit-able 控制點

### 負面

- 兩套 proxy 兩套 config 要維護（Helm chart 抽象 + 文件交代差異）
- Token TTL 4h + 無 revocation list = token 洩漏曝險窗 4h，明寫 trade-off
- Domain layer scope 在 MVP 缺席，sub-team 客戶來時要 schema migration
- vmauth / prom-label-proxy 任一上游 break change，platform Helm chart 須同步更新
- Audit log size：每個 federation request 寫一筆 → tenant 高頻拉時 log volume 不小，需 retention policy

### 中性

- 新 tenant-api endpoint 一組（`/api/v1/federation/*`）— 比照既有 `/api/v1/events` / `/api/v1/tenants` 風格
- 文件多一份 `docs/integration/tenant-federation.md` — 客戶 onboarding 多一條閱讀路徑（與 [`federation-integration.md`](../integration/federation-integration.md) ADR-004 平行）

## Future Work

按優先排序：

1. **Server-side revocation list**（觸發條件：compliance 客戶 RFP 顯式要求 / 第一次 token 洩漏事件）。設計時須處理 cache propagation TTL（典型 5min vs 即時 invalidate 二選一）
2. **Domain-layer policy（3-tier）**（觸發條件：tenant 內 sub-team 隔離需求）。schema 從 `{platform_whitelist, tenant_subset}` 擴成 `{platform_whitelist, tenant_subset, domain_scope}`
3. **3-tier permission model**（compliance / audit / operator 角色分離）— 觸發條件：合規客戶（SOX / ISO 27001 / SOC 2）。可能與「server-side revocation」同時 trigger，一起做
4. **Series cap auto-tuning**：依 tenant 歷史 federation 流量 dynamic 調 `max_series_per_response`（避免一刀切過鬆 / 過嚴）
5. **Federation 流量 chargeback**：Audit log 加總成 tenant 月度 federation usage report（多租戶平台 commercial 需求）

## 關聯

- **[ADR-004 (Federation — Central-Exporter-First)](./004-federation-central-exporter-first.md)** — 平台內部多叢集 federation。本 ADR 與之**互補**（platform-internal vs cross-boundary），非取代
- **[ADR-009 (Tenant Manager CRUD API)](./009-tenant-manager-crud-api.md)** — tenant-api 既有 endpoint pattern，本 ADR 的 token endpoint 沿用其 conventions
- **[`docs/integration/federation-integration.md`](../integration/federation-integration.md)** — ADR-004 的 user-facing guide；本 ADR 將有對應 `docs/integration/tenant-federation.md`（IV-2 ship 時）
- **[`docs/integration/victoriametrics-integration.md`](../integration/victoriametrics-integration.md)** — §F1 已預先 cross-link 本 ADR + 註明 v2.9 epic
- **[Issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380)** — IV-1 deliverable（本 ADR）+ IV-2 implementation epic

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [004-federation-central-exporter-first](004-federation-central-exporter-first.md) | ⭐⭐⭐ |
| [009-tenant-manager-crud-api](009-tenant-manager-crud-api.md) | ⭐⭐ |
| [federation-integration](../integration/federation-integration.md) | ⭐⭐ |
| [victoriametrics-integration](../integration/victoriametrics-integration.md) | ⭐⭐ |
| [README](README.md) | ⭐⭐ |
