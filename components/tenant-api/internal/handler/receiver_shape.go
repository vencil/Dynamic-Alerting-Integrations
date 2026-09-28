package handler

// Receiver shape check on PUT /api/v1/tenants/{id} (#2295): the receivers a
// tenant writes itself are judged by pkg/receiverspec — the Go copy of the
// receiver contract da-guard uses, pinned to tenant-config.schema.json and,
// through the shared case table, to the Python route generator and
// Alertmanager. Before it, PUT answered 200 and committed receivers the
// route generator then skipped (bogus type, missing required field) or that
// made Alertmanager refuse the whole config (ftp URL, both PagerDuty keys,
// `send_resolved: maybe`, two auth methods in http_config).

import (
	"fmt"

	"github.com/vencil/threshold-exporter/pkg/receiverspec"
)

// tenantReceiverViolations returns one violation per receiverspec problem of
// every receiver the tenant block writes in `_routing`: the main `receiver`,
// `overrides[i].receiver` and `routes[i].receiver`.
//
// Only what the body writes is judged. A receiver the tenant inherits from
// `_routing_defaults` or a routing profile is not the author's to fix here
// (da-guard judges the resolved routing), and a key the body leaves out or
// writes as null is not a receiver the body writes — so an override that
// relies on the main receiver, or a `_routing` that only sets timing, is not
// reported. Shapes that are not a receiver's (a non-map `_routing`, a routes
// entry that is not a mapping) are left to the checks that own them.
func tenantReceiverViolations(tenantID string, block map[string]any) []Violation {
	routing, ok := block["_routing"].(map[string]any)
	if !ok {
		return nil
	}
	base := fmt.Sprintf("tenants.%s._routing", tenantID)
	var out []Violation
	check := func(path string, holder map[string]any) {
		recv, present := holder["receiver"]
		if !present || recv == nil {
			return
		}
		for _, p := range receiverspec.Check(recv) {
			field := path + ".receiver"
			if p.Field != "" {
				field += "." + p.Field
			}
			out = append(out, Violation{Field: field, Reason: p.Message})
		}
	}
	check(base, routing)
	for _, list := range []string{"overrides", "routes"} {
		entries, _ := routing[list].([]any)
		for i, e := range entries {
			if m, ok := e.(map[string]any); ok {
				check(fmt.Sprintf("%s.%s[%d]", base, list, i), m)
			}
		}
	}
	return out
}
