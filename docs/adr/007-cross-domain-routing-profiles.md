---
title: "ADR-007: 跨域路由設定檔與域策略"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-007
tracking_kind: adr
status: accepted
domain: tenant-api
created_at: 2026-03-16
updated_at: 2026-11-24
---
# ADR-007: 跨域路由設定檔與域策略

> **Language / 語言：** **中文 (Current)** | [English](./007-cross-domain-routing-profiles.en.md)

## 狀態

✅ **Accepted**（v2.1.0）。

2026-09-28 修訂：profile 與 domain policy 以所在子樹為範圍，profile 名稱全樹唯一，policy 疊加判定（見「階層式 conf.d 下的範圍」）。

2026-10-16 修訂：domain policy（含 `require_critical_escalation`）與解析後 routing 的檢查只由路由產生器做，da-guard 與 tenant-api 不再各有一份。

2026-11-24 修訂：改寫為現行行為；拿掉不存在的 CLI 旗標（`--audit`、`--resolve-profiles`、`--check-policies`）。

## 決策

**路由設定分兩層：routing profile 負責重用，domain policy 負責約束。** 不採用三層的 contact profile 模型。

1. **Routing profile**：`_routing_profiles.yaml` 定義具名的路由設定，租戶以 `_routing_profile: <名稱>` 引用。合併順序是 `_routing_defaults` → profile → 租戶自己的 `_routing`，每一步都是頂層逐鍵淺合併。
2. **Domain policy**：`_domain_policy.yaml` 對一組租戶宣告合規約束（允許或禁止的 receiver type、`group_by`、`repeat_interval` 上限、`group_wait` 下限、critical 必須升級到 PagerDuty）。它不注入任何設定，只**驗證**合併後的結果。
3. **`_routing_enforced` 不參與合併**：它另外產成帶 `continue: true` 的路由，排在租戶路由之前，租戶無法覆蓋。
4. **只有路由產生器判 domain policy。** `--validate --strict` 下違規是擋下的 ERROR，不加 `--strict` 是 WARN。

### 範例：一條違反 domain policy 的路由

```yaml
# _routing_profiles.yaml
routing_profiles:
  team-sre-apac:
    receiver:
      type: webhook
      url: "https://alerting.internal/sre-apac"
    group_by: [tenant, alertname, severity]
    repeat_interval: 4h
```

```yaml
# _domain_policy.yaml
domain_policies:
  finance:
    description: "Finance domain"
    tenants: [db-a]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

```yaml
# db-a.yaml
tenants:
  db-a:
    _routing_profile: team-sre-apac
    mysql_connection_usage_percent: "80"
```

`generate_alertmanager_routes.py --config-dir conf.d --validate --strict` 的輸出（結束碼 1；不加 `--strict` 時兩則改為 WARN、結束碼 0）：

```text
Config files: 3 read, 0 skipped
Found 1 tenant(s) with routing config: db-a
Found 1 tenant(s) for severity dedup: db-a
  ERROR: domain_policy 'finance', tenant 'db-a': receiver type 'webhook' is forbidden — fix: domain forbids ['slack', 'webhook']; switch the tenant's receiver.type to a compliant type or amend the domain policy
  ERROR: domain_policy 'finance', tenant 'db-a': repeat_interval '4h' exceeds max '1h' — fix: lower the tenant's repeat_interval to '1h' or less, or raise the policy's max_repeat_interval
Validation: 1 route(s), 1 receiver(s), 2 inhibit rule(s)
FAIL: 2 error(s) found (listed above)
```

租戶 `db-a` 沒寫自己的 `_routing`，它的路由完全來自 profile；policy 判的是合併之後的結果。

## 背景

租戶數量成長之後，路由設定大量重複：多個租戶共用同一個 on-call 團隊、通知頻道與分組策略，卻要在每個租戶的 `_routing` 各寫一份。團隊的 Slack 頻道改名，就得改 N 個檔。不同業務域對路由有不同的合規要求（例如金融域禁止 Slack、必須接 PagerDuty），但沒有機制能強制。

## 決定的細節

### Routing profile

- **欄位與租戶的 `_routing` 相同**：`routing-profiles.schema.json` 直接引用 `tenant-config.schema.json#/definitions/routing`，不另寫一份。
- **淺合併**：租戶 `_routing` 寫了哪個頂層鍵，就整份取代 profile 的同名鍵，`routes` 與 `overrides` 也一樣。要讓某個租戶不用 profile 的 `routes`，寫 `routes: []`。不引用 profile 的租戶行為不變。
- **`routes`（依 label 分流的子路由）**：每條 `match` 只做 label 等值比對（全部相等才命中），不支援 regex 與 `continue`。每條產成租戶主路由底下的一條子路由，排在 `overrides` 之後，所以比對順序是 `overrides` → `routes` → 主路由自己的 receiver，先命中者勝。子路由沒寫的 `group_wait`／`group_interval`／`repeat_interval`／`group_by` 沿用主路由，有寫的走與主路由相同的時間護欄。receiver 名稱是 `tenant-<id>-route-<i>`。不合法的條目（`match` 為空、label 名不合法、值不是字串、含不支援的鍵）以 WARN 略過，`--validate` 因此失敗。`_routing_defaults` 不接受 `routes`。

### Domain policy

- **判的是解析後的 routing**：主 receiver、`overrides`、`routes` 的 receiver type 都判；`forbidden_receiver_types` 與 `allowed_receiver_types` 分開判、可同時觸發。
- **`require_critical_escalation`**：租戶至少要有一條 `severity=critical` 的路徑通往 PagerDuty。合規條件是主 receiver 的 type 為 `pagerduty`，或有一條實際產出的 `routes` 條目以 `match` 比對 `severity: critical` 並送往 `pagerduty`。不合規的 finding 是 `critical_escalation_missing`（`--strict` 下擋下）。
- **critical 外漏的 WARN**：合規之後，凡是還收得到 critical 告警、又不是 PagerDuty 的目的地，各報一則 `critical_escalation_leak` WARN，不影響結束碼。判法：把目的地 N 的 `match` 加上 `severity: critical` 當成它會攔下的告警 `C_N`；N 的 `match` 寫了別的 `severity` 就收不到 critical；N 之前若有任何子路由的 `match` 是 `C_N` 的子集，告警先被攔走，也不報。這個判定只在租戶路由子樹內、只針對等值 matcher 時精確：排在租戶路由之前的平台路由（例如 Watchdog、sentinel）沒納入，可能多報；receiver 內容無效、不會產出的子路由仍被當成存在，可能多報或少報。`tests/shared/routing_policy_parity_matrix.json` 的 `escalation` 欄釘住判定結果。
- **誰判**：只有路由產生器（[ADR-036](036-single-parser-effective-config.md) §9）。da-guard 不判：樹上有 `_domain_policy.yaml` 時列為未判定（結束碼 4），交給產生器。tenant-api 不自己判：每筆寫入由產生器對寫入後的整棵樹判，不合規是 422 `GENERATOR_FINDINGS`（[ADR-036](036-single-parser-effective-config.md) §4），WARN 不擋寫入。因為判的是整棵樹，改了 profile 或 policy 之後，任何經 tenant-api 的寫入都會碰到既有租戶的不合規。CI 以 `generate-routes --validate --strict` 擋下違規。

### 階層式 conf.d 下的範圍

在階層式 conf.d（[ADR-016](016-conf-d-directory-hierarchy-mixed-mode.md)）下，路由產生器與 `check_routing_profiles.py` 在每一層都讀 `_routing_profiles.yaml` 與 `_domain_policy.yaml`（`.yml` 亦可）：

- **profile 對所在子樹可見**：租戶解析 `_routing_profile: X` 時，找自己這一層或祖先層定義的 profile。profile 名稱**整棵樹唯一**：同一個名稱定義在兩個檔案是錯誤（根目錄同時有 `.yaml` 與 `.yml` 撞名也算），產生器結束碼 2。
- **policy 只作用於所在子樹**：子樹 policy 的 `tenants:` 點名子樹外的租戶是錯誤（`--strict` 下 ERROR，否則 WARN），訊息與「到處都找不到這個租戶」分開。不同層級的 policy **疊加判定**：租戶必須滿足每一條適用於它的 policy，所以子樹只能收緊。
- **合併順序不變**：`_routing_defaults` 改由各目錄層組成的鏈提供，`_routing_enforced` 只認根目錄；規格見 [ADR-017 決策 9](017-defaults-yaml-inheritance-dual-hash.md#9-路由設定沿目錄逐層繼承)。tenant-api 只服務根目錄的租戶檔；寫入時的 policy 判定由產生器對整棵樹做。

### 相關工具

- `check_routing_profiles.py`（pre-commit）：profile 引用存在、policy 的租戶清單一致。
- `explain_route.py --show-profile-expansion`：列出 profile 展開前後的差異與實際產出的子路由。
- `scaffold_tenant.py --routing-profile`：新租戶直接引用 profile。
- `da-tools opa-evaluate`：以 Rego 定義額外的策略（與 domain policy 並存）。

## 後果與已知限制

- **路由變更從 O(N) 變成 O(1)**：N 個租戶共用一個 profile，改 profile 即全部生效。
- **合規可機器驗證**：平台工程師宣告「金融域不得用 Slack」，由產生器與 CI 執行，不靠人工審查。
- **policy 的 `tenants` 清單要手動維護**，與實際的租戶檔同步；租戶到千級時會帶來合併衝突。以 regex 或前綴比對租戶（`tenant_matchers`）尚未實作，見下方延後項。
- **合併順序要寫清楚**：`_routing_defaults` → profile → 租戶 `_routing`，`_routing_enforced` 在合併之外。

## 考慮過的替代方案

- **三層 contact profile 模型**（contact → routing profile → domain policy）：contact 資訊與 Alertmanager 的 receiver 重疊，多一層要同步；四方合併的覆蓋順序難以預測；目前沒有租戶需要在同一 profile 中混用不同 contact。需求出現時可再往上加一層。
- **租戶群組繼承**：分組概念直覺，但隱式繼承容易意外覆蓋，與既有的 defaults／enforced 機制衝突。
- **直接寫 Alertmanager 原生的 route tree**：零額外抽象，但沒有「具名模板」，要手工重複，也沒有約束驗證。

## 延後的項目

- **profile 繼承另一個 profile**（extends）：未實作。觸發條件：出現需要多層 profile 的團隊結構。
- **`tenant_matchers`**：以 regex 或前綴取代 policy 裡的 `tenants` 陣列（兩者可並存，精確清單優先）。未實作。觸發條件：租戶數量讓手動清單成為負擔。

## 相關

- [ADR-001](001-severity-dedup-via-inhibit.md) —— 嚴重度去重的 inhibit 規則，與路由互補
- [ADR-003](003-sentinel-alert-pattern.md) —— sentinel 告警
- [ADR-006](006-tenant-mapping-topologies.md) —— 租戶映射拓撲
- [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.md)、[ADR-017](017-defaults-yaml-inheritance-dual-hash.md) —— 目錄階層與繼承
- [ADR-036](036-single-parser-effective-config.md) —— 誰判路由
- [Alertmanager route 設定](https://prometheus.io/docs/alerting/latest/configuration/#route)
