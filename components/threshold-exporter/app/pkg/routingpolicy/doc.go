// Package routingpolicy resolves a tenant's routing the way the Python route
// generator does and checks it against the ADR-007 domain policies (#2280).
//
// It is the Go copy of these pieces of scripts/tools/ops (the SSOT):
//
//   - resolution (_grar_parse._merge_tenant_routing +
//     _grar_merge.merge_routing_with_defaults): `_routing_defaults` →
//     routing profile → the tenant's `_routing`, each a SHALLOW top-level
//     merge, then `{{tenant}}` replaced in every string value. The fourth
//     layer, `_routing_enforced` (NOC), takes no part in resolution or in
//     policy checks; of it only the group_by of the route(s) the generator
//     renders is modelled (#2503, _grar_routes.enforced_group_by_problems →
//     Tree.Enforced / EnforcedGroupByInvalid: the block of the last root
//     platform file that enables it, read as PyYAML reads it; judged only
//     with a receiver pkg/receiverspec accepts, the `{{tenant}}` shape per
//     tenant after substitution). Its match, timing and the `--policy`
//     domain allowlist are not read;
//   - the platform files that feed it (_grar_parse._parse_platform_config
//     and, below the root, _parse_nested_config): LoadTree reads the whole
//     tree (#2326, ADR-017 amendment 2026-09-28) — `_routing_defaults` along
//     the tenant's directory chain, profiles and domain policies scoped to
//     the subtree they sit in (Tree.LayersFor, Policy.Scope) — and reports
//     the tree shapes the generator refuses (IsBlocking); LoadRoot is its
//     root half;
//   - the tenant layer (#2291, Layers.TenantBlock): the tenant file's
//     `_routing` / `_routing_profile` over the root platform files'
//     `tenants.<id>` entries (_lib_confd.overlay_platform_tenants) — never
//     the exporter's effective config, whose defaults chain and threshold
//     profile the generator does not read. Routing written there is
//     reported by UnreadRouting;
//   - the sub-routes a resolved routing renders (_grar_validate.
//     list_tenant_subroutes + route_entry_matchers);
//   - the receiver-type half of _grar_validate.check_domain_policies:
//     `forbidden_receiver_types` and `allowed_receiver_types`, judged
//     independently, so one receiver can break both;
//   - the matcher values the generator needs as strings (#2431,
//     _grar_validate.routing_values_not_string → ValuesNotString), read as
//     PyYAML reads them (WithPyYAMLRouting);
//   - the group_by contract (#2503, _grar_validate.group_by_problems /
//     routing_group_by_invalid → GroupByProblems / GroupByInvalid): no
//     non-string, empty or repeated element, no `...` beside other labels;
//   - routing that cannot be read at all (#2341, shape.go): a tenant
//     `_routing` that is neither a mapping nor a disabling string, as PyYAML
//     reads it (RoutingNotMapping — the tenant renders nothing), and a
//     `_routing_defaults` that is neither a mapping nor null
//     (ProblemRoutingDefaultsNotMapping);
//   - `require_critical_escalation` (#2325, _grar_validate.
//     critical_escalation_findings): whether severity=critical alerts reach
//     a pagerduty receiver at all, and which non-pagerduty destinations
//     still catch some of them first (JudgeCriticalEscalation).
//
// ⛔ The two languages are pinned to each other through
// tests/shared/routing_policy_parity_matrix.json, never through each other's
// source: tests/shared/test_routing_policy_parity.py asserts the Python side,
// parity_test.go here and cmd/da-guard/routing_policy_parity_test.go the Go
// side. A behaviour change on either side goes into the matrix first.
//
// Consumers: cmd/da-guard (through internal/guard) and tenant-api, which
// cannot import internal/guard. Besides the standard library, yaml.v3,
// pkg/pyyamlcompat (receivers and matcher values as the generator's PyYAML
// reads them, #2295 / #2431) and pkg/receiverspec (whether an enforced
// route's receiver is rendered, #2503) it
// imports only pkg/config — for the walker (config.RootPlatformFiles and
// config.ScanDirTree: the one directory lister, #1911) and
// config.IsDisabled — and never
// net/http (import_direction_test.go, depguard).
package routingpolicy
