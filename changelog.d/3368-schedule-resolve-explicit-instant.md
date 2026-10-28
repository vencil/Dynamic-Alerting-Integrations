---
section: Changed
topic: exporter
issues: [3368]
created: 2026-10-28T15:12:50+00:00
---
- **`pkg/config` 不再讀牆鐘：排程解析一律由呼叫端給時間點（Go API breaking；[#3368](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3368)）**：移除 `ThresholdConfig` 的 `Resolve()`／`ResolveStateFilters()`／`ResolveSilentModes()`／`ResolveMaintenanceExpiries()`／`ResolveThresholdExpiries()` 與 `TenantMerge.Resolve()`，改用對應的 `…At(now)`；`ResolveEffective`、`ResolveEffectiveFromScan`、`ScopeEffective`、`ScopeEffectiveWith`、`EffectiveTree*` 多一個 `now time.Time` 參數。exporter 的 `/metrics` 整次 scrape 只讀一次時鐘：`maintenance_expired` 事件與其他 series 在同一個時間點解析（先前它另讀一次當下時間，跨過 `expires:` 的那一次 scrape 可能與 state filter 不一致）。
