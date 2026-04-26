package guard

// Routing schema guardrails — PR-2 of the C-12 Dangling Defaults
// Guard family.
//
// SCOPE REDIRECT FROM PLANNING SPEC
// ----------------------------------
// Planning row §C-12 layer (ii) describes "ADR-017/018 Routing
// Guardrails — routing tree cycle detection + orphaned route
// detection". That description assumes an Alertmanager-style
// graph where receivers can reference routes and back-edges are
// possible.
//
// The actual `_routing` model in this codebase is a *flat
// per-tenant block*: one receiver + an optional `overrides[]`
// list of sub-routes. Routes never reference receivers by name,
// receivers never trigger routes, sub-routes don't reference
// parent routes. **Cycles are structurally impossible.**
// "Orphaned route" is also degenerate — there's nothing for a
// route to be orphaned from.
//
// Implementing a graph cycle detector against a model that can't
// cycle would be theatre. PR-2 instead ships the five checks
// that catch real bugs in the model that exists:
//
//   1. Unknown receiver type (error)
//      receiver.type not in {webhook, email, slack, teams,
//      rocketchat, pagerduty}. The Alertmanager pipeline rejects
//      these silently per existing code (config_resolve.go), so
//      catching them at the guard layer surfaces the issue
//      before merge.
//
//   2. Missing required receiver fields (error)
//      Each receiver type has type-specific required fields per
//      scripts/tools/_lib_constants.py::RECEIVER_TYPES (the SSOT
//      shared with the Python tooling). e.g. webhook needs `url`,
//      slack needs `api_url`, email needs `to` + `smarthost`,
//      pagerduty needs `service_key`. Same checks for receivers
//      embedded in overrides.
//
//   3. Empty override matcher (error)
//      An override with no matcher field (no `alertname`,
//      `metric_group`, etc.) would shadow ALL alerts for that
//      tenant — almost certainly a bug. Block merge.
//
//   4. Duplicate override matcher (warning)
//      Two overrides with identical matchers — the first wins
//      and the second is dead code. Warning so the author can
//      remove the dead override.
//
//   5. Redundant override receiver (warning)
//      An override whose receiver is structurally identical to
//      the main tenant receiver has no effect. Warning.
//
// Why these and not more:
//   - Field-by-field receiver validation against type-specific
//     constraints (URL allowlist, timing bounds, etc.) is
//     better placed in the existing config_resolve.go layer
//     where it already lives — duplicating here would drift.
//   - Cross-referencing override matchers against actual alert
//     rule names needs rule discovery (which alerts exist
//     globally), out of scope for the per-tenant guard.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
)

// receiverTypeSpec mirrors the relevant subset of the Python
// _lib_constants.py::RECEIVER_TYPES for the receiver completeness
// check. We don't pull the optional/metadata fields — only required.
//
// Source of truth lives in scripts/tools/_lib_constants.py.
// TestReceiverTypeSpecs_KeysMatchExpected is a Go-side sentinel
// that catches accidental edits to THIS file; it does NOT parse
// the Python source and does NOT detect upstream additions. If
// _lib_constants.py adds a new receiver type, valid configs using
// it will surface as "unknown receiver type" errors here until
// someone updates this list. A real SSOT freshness gate (parsing
// the Python constants at CI time) belongs in a shared lint
// task — deferred until C-8 PR-2 lands its similar gate for
// vm_only_functions.yaml so we can pick a single pattern.
//
// CAUTION when editing: keep this in lock-step with
// _lib_constants.py. A new receiver type added on the Python side
// without updating here would surface as "unknown receiver type"
// errors against valid configs (false positive blocking merges).
var receiverTypeSpecs = map[string][]string{
	"webhook":    {"url"},
	"email":      {"to", "smarthost"},
	"slack":      {"api_url"},
	"teams":      {"webhook_url"},
	"rocketchat": {"url"},
	"pagerduty":  {"service_key"},
}

// matcherKeys is the (non-exhaustive) set of override-block keys
// the routing pipeline treats as matchers. Used by the empty-
// matcher check + duplicate-matcher check to canonicalise the
// override's "intent".
//
// Per-rule routing overrides today recognise these matchers in
// generate_alertmanager_routes.py::expand_routing_overrides; we
// mirror the names here. Anything not in this set inside an
// override entry is treated as receiver/timing config rather
// than a matcher.
var matcherKeys = map[string]struct{}{
	"alertname":    {},
	"metric_group": {},
	"severity":     {},
	"component":    {},
	"db_type":      {},
	"environment":  {},
}

// checkRoutingGuardrails runs the five PR-2 routing checks. Returns
// findings; run.go handles the global sort.
//
// No-op when input.RoutingByTenant is empty — absent routing is a
// valid configuration (some tenants intentionally disable
// alerting), and silence here matches that intent.
func checkRoutingGuardrails(input CheckInput) []Finding {
	if len(input.RoutingByTenant) == 0 {
		return nil
	}
	tenants := make([]string, 0, len(input.RoutingByTenant))
	for t := range input.RoutingByTenant {
		tenants = append(tenants, t)
	}
	sort.Strings(tenants)

	var out []Finding
	for _, tenantID := range tenants {
		routing := input.RoutingByTenant[tenantID]
		if routing == nil {
			continue
		}
		out = append(out, checkOneTenantRouting(tenantID, routing)...)
	}
	return out
}

// checkOneTenantRouting applies all five checks to one tenant's
// `_routing` dict. Helper extracted so unit tests can exercise a
// single tenant without building the full RoutingByTenant map.
func checkOneTenantRouting(tenantID string, routing map[string]any) []Finding {
	var out []Finding

	mainReceiver, _ := routing["receiver"].(map[string]any)
	mainSig := receiverSignature(mainReceiver)

	// Checks 1 + 2 against the main receiver. nil main receiver is
	// handled here (not earlier) so the finding still references
	// the right `receiver` field path.
	out = append(out, checkReceiverShape(tenantID, "receiver", mainReceiver)...)

	// Checks 3 + 4 + 5 walk the overrides list. Missing overrides
	// list is fine — most tenants have only the main receiver.
	overrides, _ := routing["overrides"].([]any)
	if len(overrides) == 0 {
		return out
	}

	// Stable order: preserve the YAML list order but track
	// duplicate signatures by canonical hash.
	seenMatcher := make(map[string]int) // matcher hash → first index
	for i, raw := range overrides {
		fieldPath := fmt.Sprintf("overrides[%d]", i)
		ov, ok := raw.(map[string]any)
		if !ok {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingMissingReceiverField,
				TenantID: tenantID,
				Field:    fieldPath,
				Message: fmt.Sprintf(
					"tenant %q: %s is not an object (got %T); expected a map with matcher keys + receiver",
					tenantID, fieldPath, raw),
			})
			continue
		}

		// Check 3: empty matcher.
		matcherFingerprint := canonicalMatcher(ov)
		if matcherFingerprint == "" {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingEmptyOverrideMatcher,
				TenantID: tenantID,
				Field:    fieldPath,
				Message: fmt.Sprintf(
					"tenant %q: %s has no matcher fields (alertname, metric_group, severity, component, db_type, environment); an empty matcher would shadow ALL alerts for this tenant",
					tenantID, fieldPath),
			})
			// Skip duplicate check for an empty-matcher override —
			// "two empties match the same alert universe" is
			// already implied by the empty-matcher error.
		} else {
			// Check 4: duplicate matcher across overrides.
			if first, dup := seenMatcher[matcherFingerprint]; dup {
				out = append(out, Finding{
					Severity: SeverityWarn,
					Kind:     FindingDuplicateOverrideMatcher,
					TenantID: tenantID,
					Field:    fieldPath,
					Message: fmt.Sprintf(
						"tenant %q: %s shares the same matcher with overrides[%d]; the first override wins and this one is dead code",
						tenantID, fieldPath, first),
				})
			} else {
				seenMatcher[matcherFingerprint] = i
			}
		}

		// Checks 1 + 2 against the override's receiver.
		ovReceiver, _ := ov["receiver"].(map[string]any)
		out = append(out, checkReceiverShape(tenantID, fieldPath+".receiver", ovReceiver)...)

		// Check 5: redundant override receiver vs main.
		if mainSig != "" && receiverSignature(ovReceiver) == mainSig {
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingRedundantOverrideReceiver,
				TenantID: tenantID,
				Field:    fieldPath + ".receiver",
				Message: fmt.Sprintf(
					"tenant %q: %s.receiver is structurally identical to the main receiver; the override has no routing effect",
					tenantID, fieldPath),
			})
		}
	}
	return out
}

// checkReceiverShape applies checks 1 + 2 to one receiver dict. A
// nil/missing receiver is its own error; a bad type is one error;
// missing required fields are one error each.
func checkReceiverShape(tenantID, fieldPath string, receiver map[string]any) []Finding {
	if receiver == nil {
		return []Finding{{
			Severity: SeverityError,
			Kind:     FindingMissingReceiverField,
			TenantID: tenantID,
			Field:    fieldPath,
			Message: fmt.Sprintf(
				"tenant %q: %s is missing or not an object; routing requires a receiver dict with `type`",
				tenantID, fieldPath),
		}}
	}

	rtype, _ := receiver["type"].(string)
	if rtype == "" {
		return []Finding{{
			Severity: SeverityError,
			Kind:     FindingMissingReceiverField,
			TenantID: tenantID,
			Field:    fieldPath + ".type",
			Message: fmt.Sprintf(
				"tenant %q: %s.type is missing or empty; receivers must declare a type",
				tenantID, fieldPath),
		}}
	}

	required, known := receiverTypeSpecs[rtype]
	if !known {
		return []Finding{{
			Severity: SeverityError,
			Kind:     FindingUnknownReceiverType,
			TenantID: tenantID,
			Field:    fieldPath + ".type",
			Message: fmt.Sprintf(
				"tenant %q: %s.type=%q is not a supported receiver type (supported: %s)",
				tenantID, fieldPath, rtype, supportedTypeList()),
		}}
	}

	var out []Finding
	for _, field := range required {
		v, ok := receiver[field]
		if !ok {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingMissingReceiverField,
				TenantID: tenantID,
				Field:    fieldPath + "." + field,
				Message: fmt.Sprintf(
					"tenant %q: receiver type %q requires field %q",
					tenantID, rtype, field),
			})
			continue
		}
		// Empty string also counts as missing — mirrors the existing
		// Python validator's `if field not in receiver_obj or not
		// receiver_obj[field]` shape (generate_alertmanager_routes.py).
		if s, isStr := v.(string); isStr && s == "" {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingMissingReceiverField,
				TenantID: tenantID,
				Field:    fieldPath + "." + field,
				Message: fmt.Sprintf(
					"tenant %q: receiver type %q field %q is present but empty string",
					tenantID, rtype, field),
			})
		}
	}
	return out
}

// canonicalMatcher reduces an override entry to a stable fingerprint
// of its matcher keys + values, ignoring receiver / timing config.
// Returns "" when no matcher fields are present (signals empty
// matcher to checkOneTenantRouting).
func canonicalMatcher(ov map[string]any) string {
	subset := make(map[string]any)
	for k := range matcherKeys {
		if v, ok := ov[k]; ok {
			subset[k] = v
		}
	}
	if len(subset) == 0 {
		return ""
	}
	// encoding/json sorts map keys alphabetically — gives a stable
	// canonical form regardless of how the YAML was authored.
	b, err := json.Marshal(subset)
	if err != nil {
		// Defensive: any value we got from a yaml.v3 unmarshal is by
		// construction JSON-marshalable (string keys, scalar leaves).
		// json.Marshal can only fail here if the caller fed us a map
		// with non-string keys (e.g. yaml.v2's map[any]any) — a caller
		// violation, not a config error.
		//
		// LIMITATION: when this fires, two different malformed
		// overrides will both fall back to the same error string and
		// trip the duplicate-matcher check (false positive warning).
		// We accept that over panicking or silently dropping the
		// override — a noisy false positive is the right failure mode
		// for "your input shape is wrong". Caller's responsibility to
		// supply yaml.v3-style maps; CLI wrapper (PR-4) will enforce.
		return fmt.Sprintf("err:%v", err)
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// receiverSignature is the structural fingerprint used by check 5
// (redundant override receiver). Same canonicalisation as
// canonicalMatcher: sort keys via encoding/json, hash. Returns ""
// when receiver is nil so check 5 is skipped (the
// missing-receiver finding from check 1 covers that path).
//
// Same json.Marshal collision limitation as canonicalMatcher: a
// caller-supplied map[any]any value would trip the err-fallback
// path, and two such broken receivers would falsely sig-match. See
// canonicalMatcher's comment for the rationale.
func receiverSignature(receiver map[string]any) string {
	if receiver == nil {
		return ""
	}
	b, err := json.Marshal(receiver)
	if err != nil {
		return fmt.Sprintf("err:%v", err)
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// supportedTypeList returns the receiver types in alphabetical
// order, suitable for embedding in a finding Message.
func supportedTypeList() string {
	keys := make([]string, 0, len(receiverTypeSpecs))
	for k := range receiverTypeSpecs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := ""
	for i, k := range keys {
		if i > 0 {
			out += ", "
		}
		out += k
	}
	return out
}
