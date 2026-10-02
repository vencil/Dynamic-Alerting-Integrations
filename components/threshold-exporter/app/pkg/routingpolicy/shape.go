package routingpolicy

// shape.go — routing that cannot be read at all (#2341 R5). Python twin:
// _grar_parse._parse_tenant_overrides (the tenant's `_routing`) and
// _grar_parse._routing_defaults_not_mapping (`_routing_defaults`, root and
// below), with the texts in _grar_validate.routing_not_mapping_text /
// routing_defaults_not_mapping_text. The parity matrix pins the verdicts.

import (
	"fmt"
	"math/big"
	"time"
)

// ProblemRoutingDefaultsNotMapping: a `_routing_defaults` that is neither a
// mapping nor null, as the generator's PyYAML reads it (#2341 R5). The level
// contributes nothing (nil Defaults); the generator records a blocking WARN
// (`--validate` fails) and a --strict ERROR, so it is an error here too.
const ProblemRoutingDefaultsNotMapping = "routing_defaults_not_mapping"

// RoutingNotMapping reports whether a tenant's `_routing` value — as the
// generator's PyYAML reads it (WithPyYAMLRouting) — is neither a mapping
// nor a disabling string (IsDisabled). Call it only when the key is
// present: a tenant that does not write `_routing` is not judged. Such a
// tenant renders NO route at all (Resolve ok=false), not the defaults route.
func RoutingNotMapping(v any) bool {
	switch v.(type) {
	case map[string]any, map[any]any:
		return false
	}
	return !IsDisabled(v)
}

// RoutingNotMappingMessage is why RoutingNotMapping refused v; a boolean
// gets the hint that turns routing off.
func RoutingNotMappingMessage(v any) string {
	msg := "_routing must be a mapping or a disabling string (disable, disabled, off, false), got " + describeShape(v)
	if _, isBool := v.(bool); isBool {
		msg += " — an unquoted true / false / on / off / yes / no is a YAML boolean, not a string; " +
			"to turn routing off write a quoted string ('off') or disable"
	}
	return msg
}

// describeShape names a value's YAML type for RoutingNotMappingMessage and
// the `_routing_defaults` problem (Python: _grar_validate._shape_of; the
// texts are not parity-pinned, only the verdicts).
func describeShape(v any) string {
	switch t := v.(type) {
	case nil:
		return "null"
	case bool:
		return fmt.Sprintf("boolean %v", t)
	case string:
		return fmt.Sprintf("string %q", t)
	case []any:
		return "a list"
	case int, int64, float64, *big.Int:
		return fmt.Sprintf("number %v", t)
	case time.Time:
		return "a timestamp"
	}
	return fmt.Sprintf("%v", v)
}

func routingDefaultsNotMapping(file string, v any) Problem {
	return Problem{Kind: ProblemRoutingDefaultsNotMapping, File: file, Field: "_routing_defaults",
		Message: fmt.Sprintf("%s: _routing_defaults must be a mapping, got %s — this level contributes "+
			"nothing to the routing of the tenants it reaches", file, describeShape(v))}
}
