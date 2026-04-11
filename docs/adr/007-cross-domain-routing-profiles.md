---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: zh
---

# ADR-007: 跨域路由設定檔與域策略

## 狀態

✅ **Accepted** (v2.1.0) — 四層路由 + 域策略驗證已完成，Profile 繼承鏈為 v2.7.0+ 候選

## 背景

隨著平台管理的租戶數量成長，路由配置出現大量重複。多個租戶共享相同的 on-call 團隊、通知頻道、和群組策略，但每個租戶的 `_routing` 區塊都需要獨立配置。

### 問題陳述

1. **配置重複**：10 個租戶歸同一團隊管理時，相同的 `receiver`, `group_by`, `group_wait` 等設定重複 10 次
2. **變更放大**：團隊 Slack 頻道更名需修改 N 個租戶的配置
3. **域約束缺失**：不同業務域（金融域、電商域）對路由有不同的合規要求（如金融域禁止 Slack 通知、必須 PagerDuty），但缺乏強制機制
4. **繼承衝突**：多層配置合併 (`_routing_defaults` → tenant `_routing`) 在跨域場景下語意不清

需要一個機制讓路由配置可重用、可約束、且可跨域共享。

## 決策

**採用兩層架構：Routing Profiles（重用）+ Domain Policies（約束），而非三層的 Contact Profile 模型。**

### 第一層：Routing Profiles（路由設定檔）

在 `_routing_profiles.yaml` 中定義命名路由配置，租戶透過 `_routing_profile` 引用：

```yaml
# _routing_profiles.yaml — 位於 config-dir
routing_profiles:
  team-sre-apac:
    receiver: slack-sre-apac
    group_by: [tenant, alertname, severity]
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    routes:
      - match: { severity: critical }
        receiver: pagerduty-sre-apac
        repeat_interval: 15m

  team-dba-global:
    receiver: slack-dba
    group_by: [tenant, alertname, db_type]
    group_wait: 1m
    group_interval: 10m
    repeat_interval: 8h
```

```yaml
# db-a.yaml — 租戶配置
db-a:
  _routing_profile: team-sre-apac   # 引用 profile，無需重複
  cpu_usage_percent: "80"
  memory_usage_percent: "85"
```

**合併語意**：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`（NOC 覆蓋，不可變）。後者覆蓋前者，但 `_routing_enforced` 永遠最終覆蓋。

### 第二層：Domain Policies（域策略）

在 `_domain_policy.yaml` 中定義業務域的合規約束。Domain Policies 是**驗證規則**，不是繼承層級：

```yaml
# _domain_policy.yaml — 位於 config-dir
domain_policies:
  finance:
    description: "金融域合規要求"
    tenants: [db-a, db-b, db-e]
    constraints:
      allowed_receiver_types: [pagerduty, email, opsgenie]
      forbidden_receiver_types: [slack, webhook]
      enforce_group_by: [tenant, alertname, severity]
      max_repeat_interval: 1h
      min_group_wait: 30s
      require_critical_escalation: true

  ecommerce:
    description: "電商域標準"
    tenants: [db-c, db-d]
    constraints:
      allowed_receiver_types: [slack, pagerduty, email]
      max_repeat_interval: 12h
```

**驗證時機**：`generate_alertmanager_routes.py` 產生最終路由時，逐條檢查 Domain Policy constraints。違反約束時：
- `--strict` 模式：報錯終止
- 預設模式：發出 WARNING 並標記

### 為何拒絕三層 Contact Profile 模型

Gemini 分析中提出的三層模型（Contact Profile → Routing Profile → Domain Policy）存在過度工程化的風險：

- **Contact Profile 與 Alertmanager Receiver 重疊**：contact 資訊（Slack channel、PagerDuty key）已在 Alertmanager `receivers` 中定義，額外抽象增加同步成本
- **三層合併語意複雜**：四方合併（defaults → contact → profile → tenant）的覆蓋順序難以預測，調試成本高
- **YAGNI**：目前沒有租戶需要在同一 profile 中混用不同 contact，需求出現時可向上擴展

## 基本原理

### Routing Profiles 的價值

**配置收斂**：10 個租戶共用同一 profile，路由變更從 O(N) 降為 O(1)。

**語意明確**：Profile 是「完整的路由模板」，不是部分片段。合併順序清晰：defaults → profile → tenant override → enforced。

**向後相容**：不引用 `_routing_profile` 的租戶行為完全不變，profile 機制是 opt-in。

### Domain Policies 的設計哲學

**約束不是繼承**：Domain Policy 不向租戶「注入」配置，而是「驗證」最終合併結果。這避免了多重繼承的菱形問題。

**聲明式合規**：平台工程師可宣告「金融域租戶不得使用 Slack」，由工具鏈自動執行，而非依賴人工審查。

**可審計**：`generate_alertmanager_routes.py --audit` 輸出完整的策略符合性報告。

## 後果

### 正面影響

✅ 路由配置重複率大幅降低，N 個租戶共用 profile 後只需維護一份
✅ 團隊路由變更為原子操作（修改 profile → 所有引用租戶自動生效）
✅ 域策略提供機器可驗證的合規約束，CI 可自動攔截違規
✅ 現有租戶完全向後相容，profile 和 policy 均為 opt-in
✅ 與 `_routing_enforced`（NOC 覆蓋）機制無衝突

### 負面影響

⚠️ `generate_alertmanager_routes.py` 需擴展解析 `_routing_profiles.yaml` 和 `_domain_policy.yaml`
⚠️ 合併順序 (defaults → profile → tenant → enforced) 需充分文件化，避免混淆
⚠️ Domain Policy 的 `tenants` 清單需與實際 tenant YAML 同步維護

### 運維考量

- `generate_alertmanager_routes.py` 新增 `--resolve-profiles` 和 `--check-policies` 子命令
- CI hook：`check_routing_profiles.py` 驗證 profile 引用存在、policy tenant 清單一致
- 告警路由的除錯工具 `explain_route.py` 需顯示 profile 展開前後的差異
- 建議 profile 命名規範：`team-{team}-{region}` 或 `domain-{domain}-{tier}`

### 未來擴展性

當租戶數量達到千級規模時，硬編碼 `tenants` 陣列會產生嚴重的 Merge Conflict 和維護負擔。實作 `generate_alertmanager_routes.py` 時，可考慮支援 `tenant_matchers`（regex / prefix 匹配）作為 `tenants` 的替代語法：

```yaml
domain_policies:
  finance:
    tenant_matchers:        # 與 tenants 二擇一
      - "^finance-db-.*"   # regex：自動套用給所有 finance-db 開頭的租戶
      - "payment-gateway"   # 精確匹配仍可用
    constraints:
      forbidden_receiver_types: [slack, webhook]
```

此擴展與 v1 的 `tenants` 陣列向後相容（兩者可共存，`tenants` 精確匹配優先），實作時機可依需求決定。

## 替代方案考量

### 方案 A：三層 Contact Profile 模型 (已拒絕)
- 優點：更細粒度的聯絡人管理
- 缺點：與 Alertmanager receiver 概念重疊、三層合併語意複雜、YAGNI

### 方案 B：Tenant Group 繼承 (已考量)
- 優點：直覺的分組概念
- 缺點：隱式繼承易產生意外覆蓋、與現行 defaults/enforced 機制衝突

### Approach C: Native Alertmanager Route Tree (Considered)
- 優點：零額外抽象
- 缺點：Alertmanager route tree 不支援「命名模板」，需手工重複；無約束驗證能力

## 設計細節

### 合併流水線

```
┌──────────────────┐
│ _routing_defaults │  ← 全域預設值
└────────┬─────────┘
         ▼
┌──────────────────────┐
│ routing_profiles[ref] │  ← 團隊/域共享的命名配置
└────────┬─────────────┘
         ▼
┌──────────────────┐
│ tenant _routing   │  ← 租戶級覆寫（可選）
└────────┬─────────┘
         ▼
┌──────────────────────┐
│ domain_policies       │  ← 驗證約束（不修改值，僅報錯/警告）
└────────┬─────────────┘
         ▼
┌──────────────────────┐
│ _routing_enforced     │  ← NOC 不可變覆蓋
└──────────────────────┘
```

### Profile 引用解析

```python
# generate_alertmanager_routes.py 的擴展邏輯（虛擬碼）
def resolve_tenant_routing(tenant_cfg, profiles, defaults, enforced):
    base = copy(defaults)

    # 若引用了 profile，先合併 profile
    if '_routing_profile' in tenant_cfg:
        profile = profiles[tenant_cfg['_routing_profile']]
        base = deep_merge(base, profile)

    # 再合併租戶級覆寫
    if '_routing' in tenant_cfg:
        base = deep_merge(base, tenant_cfg['_routing'])

    # 最後套用 enforced（不可覆蓋）
    base = deep_merge(base, enforced)

    return base
```

### Policy 驗證邏輯

```python
def check_domain_policies(resolved_routing, tenant_id, policies):
    violations = []
    for policy_name, policy in policies.items():
        if tenant_id not in policy['tenants']:
            continue
        constraints = policy['constraints']

        if 'forbidden_receiver_types' in constraints:
            for recv_type in extract_receiver_types(resolved_routing):
                if recv_type in constraints['forbidden_receiver_types']:
                    violations.append(f"{policy_name}: {recv_type} forbidden")

        if 'max_repeat_interval' in constraints:
            if resolved_routing.get('repeat_interval') > parse_duration(constraints['max_repeat_interval']):
                violations.append(f"{policy_name}: repeat_interval exceeds max")

    return violations
```

## v2.1.0 Implementation Summary

- `generate_alertmanager_routes.py` — 四層合併（defaults → profile → tenant → enforced）+ `check_domain_policies()` 驗證（21 tests）
- `check_routing_profiles.py` — Profile/Policy lint 工具（28 tests + pre-commit hook 自動執行）
- `explain_route.py` — Routing 偵錯工具，支援 `--show-profile-expansion` trace 模式（25 tests + da-tools CLI 整合）
- `scaffold_tenant.py --routing-profile` — Onboarding 整合，新 tenant 可直接引用 profile（9 tests）
- `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` 子函式重構
- 範例配置 `conf.d/examples/_routing_profiles.yaml`、`conf.d/examples/_domain_policy.yaml`
- JSON Schema：`routing-profiles.schema.json`、`domain-policy.schema.json`
- Go/Python 雙端 `_routing_profile` reserved key 同步
- Self-Service Portal：routing profile 驗證 + 範例切換 UI

## 演進狀態

- **v2.1.0**（已完成）：四層合併管線、check_routing_profiles lint、explain_route 偵錯工具
- **v2.3.0**（已完成）：OPA 整合——`da-tools opa-evaluate` 支援 Rego 定義域策略（routing-compliance / threshold-bounds / naming-convention 三個範例策略）
- **v2.5.0**（已完成）：Domain Policy 從 CI-time validation 前移到 API-time enforcement（tenant-api 403 回應）
- **v2.6.0**（已完成）：`generate_alertmanager_routes.py` 重構（21 helpers extracted），`_build_receiver_config()` 改為 strategy pattern

**殘留**：
- Profile 繼承鏈（profile extends another profile）— 排入 v2.7.0+ 候選
- `tenant_matchers`（regex / prefix）替代硬編碼 `tenants` 陣列 — 排入 v2.7.0+ 候選

## 相關決策

- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) — inhibit rules 與 routing 互補
- [ADR-003: Sentinel Alert 模式](./003-sentinel-alert-pattern.md) — sentinel 告警影響 routing 行為
- [ADR-006: 租戶映射拓撲](./006-tenant-mapping-topologies.md) — 1:N 映射後的租戶仍適用 routing profiles

## 參考資料

- [`docs/architecture-and-design.md`](../architecture-and-design.md) §2.9 — Routing Guardrails
- [`docs/architecture-and-design.md`](../architecture-and-design.md) §2.11 — Dual-Perspective routing
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — 路由產生器（將擴展）
- [Alertmanager Route Configuration](https://prometheus.io/docs/alerting/latest/configuration/#route) — 官方文件

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐ |
| [004-federation-central-exporter-first](004-federation-central-exporter-first.md) | ⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐ |
| [006-tenant-mapping-topologies](006-tenant-mapping-topologies.md) | ⭐⭐⭐ |
| [007-cross-domain-routing-profiles](007-cross-domain-routing-profiles.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](../architecture-and-design.md) | ⭐⭐⭐ |
| ["架構與設計 — 附錄 A"](../architecture-and-design.md#附錄角色與工具速查) | ⭐⭐ |
