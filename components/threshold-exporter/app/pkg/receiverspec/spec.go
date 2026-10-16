// Package receiverspec names the receiver types the platform supports
// (#2295). The exporter's routing resolver (pkg/config) looks a receiver's
// `type` up here; it does not judge the receiver.
//
// Judging a receiver — which fields each type requires, which values
// Alertmanager refuses — is the route generator's alone (ADR-036 §9,
// scripts/tools/_lib_validation.py over the RECEIVER_TYPES of
// scripts/tools/_lib_constants.py). The Go copy of those rules (Check) and
// its callers in da-guard and pkg/routingpolicy were deleted with ADR-036
// step 3 (#2905); tenant-api had stopped calling it in step 1 (#2903).
//
// The hub of the contract is docs/schemas/tenant-config.schema.json;
// TestKnown_MatchesSchema pins the type list below to it, reading the schema
// as JSON. The package depends on nothing (import_direction_test.go).
package receiverspec

// types are the supported receiver types: a copy of the `type` const of each
// receiver definition in tenant-config.schema.json. The type must match
// exactly — no case folding or trimming (#2180).
var types = map[string]bool{
	"webhook":    true,
	"email":      true,
	"slack":      true,
	"teams":      true,
	"rocketchat": true,
	"pagerduty":  true,
}

// Known reports whether rtype is a supported receiver type.
func Known(rtype string) bool {
	return types[rtype]
}
