// Package routingpolicy resolves a tenant's routing the way the Python route
// generator does and checks it against the ADR-007 domain policies (#2280).
//
// It is the Go copy of four pieces of scripts/tools/ops (the SSOT):
//
//   - resolution (_grar_parse._merge_tenant_routing +
//     _grar_merge.merge_routing_with_defaults): `_routing_defaults` →
//     routing profile → the tenant's `_routing`, each a SHALLOW top-level
//     merge, then `{{tenant}}` replaced in every string value. The fourth
//     layer, `_routing_enforced` (NOC), takes no part in policy checks and is
//     not modelled here;
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
//     independently, so one receiver can break both.
//
// ⛔ The two languages are pinned to each other through
// tests/shared/routing_policy_parity_matrix.json, never through each other's
// source: tests/shared/test_routing_policy_parity.py asserts the Python side,
// parity_test.go here and cmd/da-guard/routing_policy_parity_test.go the Go
// side. A behaviour change on either side goes into the matrix first.
//
// Consumers: cmd/da-guard (through internal/guard) and tenant-api, which
// cannot import internal/guard. Besides the standard library and yaml.v3 it
// imports only pkg/config — for the walker (config.RootPlatformFiles and
// config.ScanDirTree: the one directory lister, #1911) and
// config.IsDisabled — and never
// net/http (import_direction_test.go, depguard).
package routingpolicy
