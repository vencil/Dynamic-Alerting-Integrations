package gitops

// Receiver shape pre-flight for a whole-document tenant write (#2295).
//
// The receivers a body writes in `_routing` are judged by pkg/receiverspec —
// the Go copy of the receiver contract da-guard uses, pinned to
// tenant-config.schema.json and, through the shared case table, to the Python
// route generator and Alertmanager. It sits in the body-only pre-flight of
// Write / WriteIfUnchanged / WritePR (putPreflight) and of both dry-runs, so
// POST /tenants/{id}/validate and PUT /tenants/{id} give the same verdict on
// the same body.
//
// ⛔ NOT IN validateBodyOnly. That function also judges the MERGED document of
// a batch op (readMergeBodyOnly), which carries the tenant's whole file: a
// patch that only touches a threshold would then be refused over a receiver
// already on disk. Batch is out of #2295's scope; putPreflight is the
// whole-document writers' pre-flight only.

import (
	"fmt"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/receiverspec"
	"gopkg.in/yaml.v3"
)

// ReceiverViolation is one receiver problem: Field is the document path
// (`tenants.<id>._routing.receiver.url`), Reason the receiverspec message.
type ReceiverViolation struct {
	Field  string
	Reason string
}

// ReceiverShapeError is the pre-flight refusal for receiver problems. It
// unwraps to ErrValidation, so every caller that maps ErrValidation to 400
// still does; the PUT handler reads Violations for its INVALID_BODY response.
type ReceiverShapeError struct {
	Violations []ReceiverViolation
}

func (e *ReceiverShapeError) Error() string {
	return fmt.Sprintf("%v: %s", ErrValidation, strings.Join(e.lines(), "; "))
}

// Unwrap makes errors.Is(err, ErrValidation) hold.
func (e *ReceiverShapeError) Unwrap() error { return ErrValidation }

func (e *ReceiverShapeError) lines() []string {
	out := make([]string, len(e.Violations))
	for i, v := range e.Violations {
		out[i] = v.Field + ": " + v.Reason
	}
	return out
}

// putPreflight is the body-only pre-flight of a whole-document write:
// validateBodyOnly, then — once the body is well-formed — its receivers.
func putPreflight(tenantID, yamlContent string) error {
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}
	if v := receiverViolations(tenantID, yamlContent); len(v) > 0 {
		return &ReceiverShapeError{Violations: v}
	}
	return nil
}

// dryRunPreflight is putPreflight for the dry-runs, as their error strings.
func dryRunPreflight(tenantID, yamlContent string) []string {
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return errs
	}
	if v := receiverViolations(tenantID, yamlContent); len(v) > 0 {
		return (&ReceiverShapeError{Violations: v}).lines()
	}
	return nil
}

// receiverViolations returns one violation per receiverspec problem of every
// receiver the tenant block writes in `_routing`: the main `receiver`,
// `overrides[i].receiver` and `routes[i].receiver`.
//
// Only what the body writes is judged. A receiver the tenant inherits from
// `_routing_defaults` or a routing profile is not the author's to fix here
// (da-guard judges the resolved routing). A `receiver` key the body leaves
// out or writes as null is not judged here either. That is a gap, not a
// verdict: an override or a routes entry without a receiver of its own is
// skipped by the route generator and reported by da-guard (the override is
// not routed to the main receiver). Shapes that are not a receiver's (a
// non-map `_routing`, a routes entry that is not a mapping) are left to the
// checks that own them.
func receiverViolations(tenantID, yamlContent string) []ReceiverViolation {
	var doc struct {
		Tenants map[string]map[string]any `yaml:"tenants"`
	}
	if yaml.Unmarshal([]byte(yamlContent), &doc) != nil {
		return nil // validateBodyOnly owns every decode error
	}
	routing, ok := doc.Tenants[tenantID]["_routing"].(map[string]any)
	if !ok {
		return nil
	}
	base := fmt.Sprintf("tenants.%s._routing", tenantID)
	var out []ReceiverViolation
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
			out = append(out, ReceiverViolation{Field: field, Reason: p.Message})
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
