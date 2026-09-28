package routingpolicy

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// EscalationTypes are the receiver types that count as an escalation target
// for severity="critical" (_grar_validate.ESCALATION_TYPES, #2244). ADR-007's
// finance case asks for "critical → PagerDuty"; another type is not one.
var EscalationTypes = []string{"pagerduty"}

// MainReceiverRef is the Ref of the tenant's main route (Targets[0]).
const MainReceiverRef = "receiver"

// EscalationLeak is one non-escalation destination a severity=critical alert
// can still reach (#2312): a rendered sub-route, or the main receiver last.
type EscalationLeak struct {
	// Ref is Target.Ref: "overrides[<i>]", "routes[<i>]" or "receiver".
	Ref          string
	ReceiverType string
	// Match is the destination's own match (nil for the main receiver).
	Match map[string]string
	// Caught is the label set of the alert that reaches it:
	// Match ∪ {severity: critical} (the tenant label left out).
	Caught map[string]string
}

// EscalationVerdict is the require_critical_escalation verdict on one
// tenant's resolved routing.
type EscalationVerdict struct {
	// Target is the Ref of the first escalation destination; "" ⇔ the
	// tenant is not compliant (and Leaks is then empty).
	Target string
	Leaks  []EscalationLeak
}

// Compliant reports whether severity=critical alerts reach an
// EscalationTypes receiver at all.
func (v EscalationVerdict) Compliant() bool { return v.Target != "" }

// EscalationFinding is the verdict for one domain policy that requires
// critical escalation and lists the tenant.
type EscalationFinding struct {
	Domain  string
	Verdict EscalationVerdict
}

// JudgeCriticalEscalation is _grar_validate.critical_escalation_findings with
// the tenant given, over the same destinations (Targets). ok=false: the main
// receiver is empty, so nothing renders and there is nothing to judge.
//
// Compliant ⇔ the main receiver type is in EscalationTypes, or a rendered
// sub-route whose match has `severity: critical` sends to one (the first
// such sub-route in render order is Target).
//
// When compliant, every destination N whose type is not an escalation type —
// the sub-routes in render order (overrides, then routes), then the main
// receiver with an empty match — is judged with
// C_N = match(N) ∪ {severity: critical}:
//
//   - N's match has a `severity` other than critical → never reached;
//   - N's match has a `tenant` other than tenantID → never reached (the
//     tenant route's own matcher is tenant=<tenantID>);
//   - some EARLIER sub-route P (escalating or not) has match(P) ⊆
//     C_N ∪ {tenant: tenantID} → P takes every critical alert N could get;
//   - otherwise the alert labelled C_N reaches N: a leak.
//
// The same limits as the Python side apply: platform routes rendered ahead
// of the tenant route are not modelled (over-report only), and a sub-route
// whose receiver content the generator rejects is still counted as present.
//
// ⛔ Pinned to the Python judgement by
// tests/shared/routing_policy_parity_matrix.json (`escalation` column): the
// same input must yield the same Target presence and the same leak refs, in
// the same order.
func JudgeCriticalEscalation(tenantID string, resolved map[string]any) (EscalationVerdict, bool) {
	targets := Targets(resolved)
	if len(targets) == 0 {
		return EscalationVerdict{}, false
	}
	main, subs := targets[0], targets[1:]

	var v EscalationVerdict
	for _, s := range subs {
		if s.Match["severity"] == "critical" && contains(EscalationTypes, ReceiverType(s.Receiver)) {
			v.Target = s.Ref
			break
		}
	}
	if v.Target == "" {
		if !contains(EscalationTypes, ReceiverType(main.Receiver)) {
			return v, true
		}
		v.Target = MainReceiverRef
	}

	destinations := append(append([]Target{}, subs...), Target{Ref: MainReceiverRef, Receiver: main.Receiver})
	for pos, n := range destinations {
		rtype := ReceiverType(n.Receiver)
		if contains(EscalationTypes, rtype) {
			continue
		}
		if sev, has := n.Match["severity"]; has && sev != "critical" {
			continue
		}
		if t, has := n.Match["tenant"]; has && t != tenantID {
			continue
		}
		caught := map[string]string{"severity": "critical"}
		for k, val := range n.Match {
			caught[k] = val
		}
		labels := map[string]string{"tenant": tenantID}
		for k, val := range caught {
			labels[k] = val
		}
		if takenEarlier(subs[:pos], labels) { // the main receiver is last: pos == len(subs)
			continue
		}
		v.Leaks = append(v.Leaks, EscalationLeak{Ref: n.Ref, ReceiverType: rtype, Match: n.Match, Caught: caught})
	}
	return v, true
}

// takenEarlier reports whether some earlier sub-route's match is a subset of
// labels — it then takes every alert carrying labels first.
func takenEarlier(earlier []Target, labels map[string]string) bool {
	for _, p := range earlier {
		all := true
		for k, want := range p.Match {
			if got, ok := labels[k]; !ok || got != want {
				all = false
				break
			}
		}
		if all {
			return true
		}
	}
	return false
}

// CheckCriticalEscalation judges tenantID's resolved routing once per policy
// that sets RequireCriticalEscalation and lists the tenant, domains in name
// order (a tenant listed twice is judged twice, as in Python). Call it only
// for a tenant whose routing resolved; a tenant whose main receiver is empty
// yields nothing (the Python check skips it).
func CheckCriticalEscalation(tenantID string, resolved map[string]any, policies []Policy) []EscalationFinding {
	sorted := make([]Policy, len(policies))
	copy(sorted, policies)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].Domain < sorted[j].Domain })

	var out []EscalationFinding
	var verdict EscalationVerdict
	judged, ok := false, false
	for _, p := range sorted {
		if !p.RequireCriticalEscalation {
			continue
		}
		for _, t := range p.Tenants {
			if t != tenantID {
				continue
			}
			if !judged {
				verdict, ok = JudgeCriticalEscalation(tenantID, resolved)
				judged = true
			}
			if ok {
				out = append(out, EscalationFinding{Domain: p.Domain, Verdict: verdict})
			}
		}
	}
	return out
}

// FormatLabels renders a label set as `k=v, k=v` with `severity` first and
// the rest in name order (Go maps keep no YAML order; the Python message
// follows the file's order, so only the set is comparable).
func FormatLabels(labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		if k != "severity" {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	if _, ok := labels["severity"]; ok {
		keys = append([]string{"severity"}, keys...)
	}
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = k + "=" + labels[k]
	}
	return strings.Join(parts, ", ")
}

// PyStr is Python's str() over a decoded YAML scalar, for the override
// matcher value the generator formats into the route (`alertname: 123` →
// "123", `true` → "True", `1.0` → "1.0"). Lists and mappings get Python's
// repr, mapping keys in name order (the decoder keeps no order). Not
// covered: a timestamp — yaml.v3 decodes `2024-01-01` to a time.Time that
// falls through to fmt.Sprint ("2024-01-01 00:00:00 +0000 UTC"), where
// Python's str() of the date gives "2024-01-01".
func PyStr(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case nil:
		return "None"
	case bool:
		if t {
			return "True"
		}
		return "False"
	case int:
		return strconv.Itoa(t)
	case int64:
		return strconv.FormatInt(t, 10)
	case uint64:
		return strconv.FormatUint(t, 10)
	case float64:
		return pyFloat(t)
	}
	return pyRepr(v)
}

func pyRepr(v any) string {
	switch t := v.(type) {
	case string:
		if strings.Contains(t, "'") && !strings.Contains(t, `"`) {
			return `"` + strings.ReplaceAll(t, `\`, `\\`) + `"`
		}
		return "'" + strings.ReplaceAll(strings.ReplaceAll(t, `\`, `\\`), "'", `\'`) + "'"
	case []any:
		parts := make([]string, len(t))
		for i, e := range t {
			parts[i] = pyRepr(e)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case map[string]any, map[any]any:
		m, ok := asStringMap(t)
		if !ok {
			return fmt.Sprint(t)
		}
		keys := make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		parts := make([]string, len(keys))
		for i, k := range keys {
			parts[i] = pyRepr(k) + ": " + pyRepr(m[k])
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case nil, bool, int, int64, uint64, float64:
		return PyStr(t)
	}
	return fmt.Sprint(v)
}

// pyFloat is Python's repr(float): shortest round-trip digits, fixed
// notation for 1e-4 <= |f| < 1e16 (always with a fractional part),
// exponent notation with a signed two-digit-minimum exponent otherwise.
func pyFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "nan"
	case math.IsInf(f, 1):
		return "inf"
	case math.IsInf(f, -1):
		return "-inf"
	}
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	exp := strconv.FormatFloat(f, 'e', -1, 64) // d.ddde±XX
	i := strings.LastIndexByte(exp, 'e')
	e, _ := strconv.Atoi(exp[i+1:])
	if e < -4 || e >= 16 {
		return exp
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.ContainsRune(s, '.') {
		s += ".0"
	}
	return s
}
