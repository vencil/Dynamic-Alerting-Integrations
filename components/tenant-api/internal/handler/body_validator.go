package handler

// Body-content validation for tenant-api request bodies.
//
// Phase B Track C C4 (issue #134, deferred from PR #135). v2.8.0-pre,
// `POST /api/v1/tenants/batch` accepted nonsense values (e.g.
// `{"_timeout_ms":"99999999999"}` or `{"_silent_mode":"purple-elephant"}`)
// → API returned 200 → downstream threshold-exporter / GitOps writer
// rejected days later, after the bad write was already committed to git.
// Customer's fail-fast surface was wrong.
//
// Design:
//   - Hybrid approach. Fixed-shape fields (Label / Description / Members)
//     use go-playground/validator struct tags. Variable-shape fields
//     (`Patch map[string]string`) use a per-key validator registry —
//     struct tags can't express "validation depends on map key".
//   - Both feed the same `Violation` shape. One JSON response format,
//     one client-side parser.
//   - "Soft whitelist" for `Patch` keys: known reserved keys (`_*`)
//     get strict validation; unknown reserved keys pass through
//     (avoids breaking when threshold-exporter introduces new keys
//     before tenant-api's registry catches up). Misconfigs like
//     `_silent_mod` (typo) DO slip through this layer — caught by
//     threshold-exporter's downstream resolve as today. The wins are:
//     (a) wrong-VALUE for known keys is fail-fast (the high-frequency
//     misconfig class); (b) huge values are bounded so no
//     resource-exhaustion vector via tenant-api boundary.
//
// Trade-offs vs. strict whitelist:
//   - Strict whitelist would catch typo'd keys but couples tenant-api
//     release cadence to threshold-exporter (every new key needs a
//     tenant-api release). Soft is operationally simpler.
//   - If/when threshold-exporter exports a stable key registry,
//     `reservedKeyValidators` can become a generated subset of it.

import (
	"errors"
	"fmt"
	"reflect"
	"strconv"
	"strings"

	"github.com/go-playground/validator/v10"
)

// Violation describes one body-validation failure. Exported because
// the JSON response embeds a slice of these.
type Violation struct {
	Field  string `json:"field"`
	Reason string `json:"reason"`
}

// validatorInstance is the singleton *validator.Validate used by all
// handlers. Configured once at package init so each request doesn't
// rebuild the reflection cache.
var validatorInstance = newValidatorInstance()

func newValidatorInstance() *validator.Validate {
	v := validator.New(validator.WithRequiredStructEnabled())
	// Translate JSON tag → struct tag for clearer error field paths.
	// Without this, a violation on `Label` would report as "Label"
	// (Go field name); with it, reports as "label" (JSON field).
	v.RegisterTagNameFunc(func(fld reflect.StructField) string {
		name := strings.SplitN(fld.Tag.Get("json"), ",", 2)[0]
		if name == "" || name == "-" {
			return fld.Name
		}
		return name
	})
	return v
}

// validateStructTags runs struct-tag validation against any request struct
// and converts errors into the canonical Violation slice. Empty result =
// the struct passed all tag rules.
//
// Note: this only handles the fixed-shape fields. Map-shaped fields
// like Patch / Filters need per-key validation via validatePatchMap /
// validateFilterMap.
func validateStructTags(req interface{}) []Violation {
	if err := validatorInstance.Struct(req); err != nil {
		var validationErrs validator.ValidationErrors
		if errors.As(err, &validationErrs) {
			return translateValidatorErrors(validationErrs)
		}
		// InvalidValidationError or similar — caller bug, surface as one violation
		return []Violation{{Field: "<root>", Reason: err.Error()}}
	}
	return nil
}

// translateValidatorErrors maps go-playground/validator FieldError
// instances into our Violation shape, with reason messages tuned for
// the actual rules we use (max=256, len, etc.) rather than the default
// terse "Field validation for 'X' failed on the 'max' tag".
func translateValidatorErrors(errs validator.ValidationErrors) []Violation {
	out := make([]Violation, 0, len(errs))
	for _, fe := range errs {
		field := fe.Namespace()
		// Strip the top-level struct name prefix. validator returns
		// "PutGroupRequest.Label" — clients only care about "label".
		if dot := strings.IndexByte(field, '.'); dot >= 0 {
			field = field[dot+1:]
		}
		out = append(out, Violation{
			Field:  field,
			Reason: humanizeValidatorTag(fe),
		})
	}
	return out
}

// humanizeValidatorTag renders one validator FieldError into a
// human-readable reason string. Keep messages actionable: state the
// constraint violated and (where helpful) the actual offending value.
//
// `min` / `max` / `len` semantics depend on the field kind:
//   - String  → character-count rule
//   - Slice / Map / Array → element-count rule
//
// Reporting "must be at least 1 characters" for an empty Operations
// slice would confuse customers. We use fe.Kind() to pick the right
// noun. Numeric kinds also fall through to "value" wording.
func humanizeValidatorTag(fe validator.FieldError) string {
	switch fe.Tag() {
	case "required":
		return "is required"
	case "min":
		return fmt.Sprintf("must be at least %s %s", fe.Param(), countNounFor(fe.Kind()))
	case "max":
		return fmt.Sprintf("must not exceed %s %s", fe.Param(), countNounFor(fe.Kind()))
	case "len":
		return fmt.Sprintf("must be exactly %s %s", fe.Param(), countNounFor(fe.Kind()))
	default:
		return fmt.Sprintf("failed %q validation (param=%q)", fe.Tag(), fe.Param())
	}
}

// countNounFor returns the user-facing unit name for size-class
// validator rules ("characters" / "items" / "value").
func countNounFor(k reflect.Kind) string {
	switch k {
	case reflect.String:
		return "characters"
	case reflect.Slice, reflect.Array, reflect.Map:
		return "items"
	default:
		// Numeric / bool / unknown — fall back to a neutral noun
		return "value"
	}
}

// ─────────────────────────────────────────────────────────────────
// Patch (map[string]string) validation — per-key registry
// ─────────────────────────────────────────────────────────────────

// Body-level limits for ANY patch key/value. Caps below catch
// resource-exhaustion vectors (e.g. multi-megabyte string pasted
// into a value field) before downstream sees them.
const (
	maxPatchKeyLen   = 256
	maxPatchValueLen = 1024
)

// reservedKeyValidator validates a single _*-prefixed reserved key value.
// Returns "" if value is acceptable, or a non-empty reason string for
// rejection. Each function is independent; key-aware bounds live here.
type reservedKeyValidator func(value string) string

// reservedKeyValidators maps known reserved-key names to their value
// validators. Soft-whitelist semantics: keys NOT in this map pass
// through without further checks (after the generic length cap).
//
// Source-of-truth for valid values: threshold-exporter's resolve /
// metadata logic (e.g. config_resolve.go's `silent_mode` enum
// `{warning, critical, all, disable}`). When extending this map,
// cross-check the SOT or you'll create a doc/code drift class.
//
// Registry purposely small — only entries with confirmed production
// validation rules go in. (Earlier draft included a hallucinated
// `_quench_min` entry; removed in self-review.)
var reservedKeyValidators = map[string]reservedKeyValidator{
	"_silent_mode":     validateSilentMode,
	"_timeout_ms":      validateNonNegativeIntCap(3_600_000), // ≤ 1h
	"_routing_profile": validateProfileReference,
	"_profile":         validateProfileReference,
}

// validateSilentMode mirrors threshold-exporter's `config_resolve.go`
// behavior: the value can be either the scalar enum form
// (`"warning"` / `"critical"` / `"all"` / `"disable"`, case-insensitive)
// OR a structured YAML form (`"target: warning\nexpires: ..."`). The
// production code distinguishes via `strings.Contains(val, "target:")`
// — we use the same heuristic so the boundary doesn't false-positive
// on documented usage. Structured-form parsing details (RFC3339 expires
// validation, etc.) live downstream — we just let it through here.
func validateSilentMode(value string) string {
	if strings.Contains(value, "target:") {
		// Looks like a structured YAML mapping; defer to threshold-
		// exporter's parser. Length cap (1024) already enforced upstream.
		return ""
	}
	switch strings.ToLower(value) {
	case "warning", "critical", "all", "disable":
		return ""
	default:
		return fmt.Sprintf(
			"must be one of {warning, critical, all, disable} (or structured YAML "+
				"with `target: <mode>` form); got %q",
			value,
		)
	}
}

func validateNonNegativeIntCap(maxVal int64) reservedKeyValidator {
	return func(value string) string {
		n, err := strconv.ParseInt(value, 10, 64)
		if err != nil {
			return fmt.Sprintf("must be an integer; got %q (parse error: %v)", value, err)
		}
		if n < 0 {
			return fmt.Sprintf("must be non-negative; got %d", n)
		}
		if n > maxVal {
			return fmt.Sprintf("must be ≤ %d; got %d", maxVal, n)
		}
		return ""
	}
}

// validateProfileReference enforces the length cap on a profile-name
// reference (`_profile` / `_routing_profile`). Empty value is ALLOWED
// — customers may patch the field to "" to clear the profile reference
// (downstream interprets empty same as missing). Only oversize is
// rejected (resource-exhaustion guard).
func validateProfileReference(value string) string {
	if len(value) > 256 {
		return fmt.Sprintf("must not exceed 256 characters; got %d", len(value))
	}
	return ""
}

// validatePatchMap walks every key/value pair in a patch map and
// returns Violations for both generic length-cap failures and
// known-reserved-key value-rule failures. Returns the FULL list,
// not first-only — caller can render once for the whole batch.
//
// fieldPrefix lets callers nest the field path (e.g.
// "operations[0].patch") so the response's `field` value points
// directly to the offending JSONPath segment.
func validatePatchMap(patch map[string]string, fieldPrefix string) []Violation {
	var violations []Violation
	for k, v := range patch {
		if len(k) > maxPatchKeyLen {
			violations = append(violations, Violation{
				Field: fmt.Sprintf("%s[%q]", fieldPrefix, k),
				Reason: fmt.Sprintf("key length must not exceed %d characters; got %d",
					maxPatchKeyLen, len(k)),
			})
			// Don't bother validating the value of a key we already rejected
			continue
		}
		if len(v) > maxPatchValueLen {
			violations = append(violations, Violation{
				Field: fmt.Sprintf("%s[%q]", fieldPrefix, k),
				Reason: fmt.Sprintf("value length must not exceed %d characters; got %d",
					maxPatchValueLen, len(v)),
			})
			continue
		}
		// Reserved-key strict validation (only for keys in the registry).
		if validator, ok := reservedKeyValidators[k]; ok {
			if reason := validator(v); reason != "" {
				violations = append(violations, Violation{
					Field:  fmt.Sprintf("%s[%q]", fieldPrefix, k),
					Reason: reason,
				})
			}
		}
	}
	return violations
}

// ─────────────────────────────────────────────────────────────────
// Filters (map[string]string) validation — generic length cap only
// ─────────────────────────────────────────────────────────────────
//
// View / Group `Filters` are arbitrary metadata strings (e.g.
// `severity:critical`, `team:platform`). No reserved-key registry
// applies — we just ensure no individual filter value exceeds the
// resource-exhaustion cap.

const maxFilterValueLen = 1024

func validateFilterMap(filters map[string]string, fieldPrefix string) []Violation {
	var violations []Violation
	for k, v := range filters {
		if len(k) > maxPatchKeyLen {
			violations = append(violations, Violation{
				Field: fmt.Sprintf("%s[%q]", fieldPrefix, k),
				Reason: fmt.Sprintf("key length must not exceed %d characters; got %d",
					maxPatchKeyLen, len(k)),
			})
			continue
		}
		if len(v) > maxFilterValueLen {
			violations = append(violations, Violation{
				Field: fmt.Sprintf("%s[%q]", fieldPrefix, k),
				Reason: fmt.Sprintf("value length must not exceed %d characters; got %d",
					maxFilterValueLen, len(v)),
			})
		}
	}
	return violations
}

// writeValidationErrors and the canonical error envelope live in
// errors.go (PR-9/11 unification).
