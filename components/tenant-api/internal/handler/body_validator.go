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
//   - Fixed-shape fields (Label / Description / Members) use
//     go-playground/validator struct tags. Variable-shape fields
//     (`Patch map[string]string`) are judged per key — struct tags can't
//     express "validation depends on map key".
//   - Both feed the same `Violation` shape. One JSON response format,
//     one client-side parser.
//   - What a patch KEY may be comes from the exporter's reserved-key
//     registry (reserved_keys.json, ADR-038): a `_` key it does not list
//     is refused (nothing reads it), and a key whose value is a mapping or
//     a list cannot be set from a string (cfg.PatchableAsString). What a
//     reserved VALUE may be is not judged here: the write gate judges it
//     with the exporter's own reader (cfg.CheckReservedValue, #3227), the
//     same for PUT, batch and dry-run — a copy of the reader's rules here
//     is how this file used to drift from it.

import (
	"errors"
	"fmt"
	"reflect"
	"strconv"
	"strings"

	"github.com/go-playground/validator/v10"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// Violation describes one body-validation failure. Exported because
// the JSON response embeds a slice of these.
type Violation struct {
	Field  string `json:"field"`
	Reason string `json:"reason"`
} //@name Violation
// swag @name: referenced from ErrorResponse.violations — pinned to one
// canonical definition for the same cross-package reason as ErrorResponse
// (see errors.go).

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

// ValidateStructTags runs struct-tag validation against any request struct
// and converts errors into the canonical Violation slice. Empty result =
// the struct passed all tag rules.
//
// Note: this only handles the fixed-shape fields. Map-shaped fields
// like Patch / Filters need per-key validation via validatePatchMap /
// validateFilterMap.
func ValidateStructTags(req interface{}) []Violation {
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

// lengthCapViolation enforces the generic key/value length caps shared by
// patch and filter map validation (the key cap is maxPatchKeyLen for both;
// the value cap differs per caller). It returns a Violation and true when
// either cap is exceeded — the caller appends it and skips any further
// per-key checks for that entry (key cap is checked first, so an oversize
// key short-circuits before the value is examined). The Violation field path
// and reason strings are produced HERE so patch and filter validation report
// caps identically (single source — no drift between the two messages).
func lengthCapViolation(fieldPrefix, k, v string, maxValueLen int) (Violation, bool) {
	if len(k) > maxPatchKeyLen {
		return Violation{
			Field: fmt.Sprintf("%s[%q]", fieldPrefix, k),
			Reason: fmt.Sprintf("key length must not exceed %d characters; got %d",
				maxPatchKeyLen, len(k)),
		}, true
	}
	if len(v) > maxValueLen {
		return Violation{
			Field: fmt.Sprintf("%s[%q]", fieldPrefix, k),
			Reason: fmt.Sprintf("value length must not exceed %d characters; got %d",
				maxValueLen, len(v)),
		}, true
	}
	return Violation{}, false
}

// validatePatchMap walks every key/value pair in a patch map and
// returns Violations for generic length-cap failures and for keys a patch
// cannot set: a reserved key whose value is a mapping or a list. (Whether
// any reader reads a key is the shared key verdict's, ADR-038 §11.) Returns the FULL
// list, not first-only — caller can render once for the whole batch.
//
// ⚠️ A reserved key's VALUE is not judged here (#3227): the write gate does,
// with the exporter's reader, per op — a batch op with `_silent_mode:
// purple` fails as that op (422, op code BAD_REQUEST), not as a 400 for the
// whole request.
//
// fieldPrefix lets callers nest the field path (e.g.
// "operations[0].patch") so the response's `field` value points
// directly to the offending JSONPath segment.
func validatePatchMap(patch map[string]string, fieldPrefix string) []Violation {
	var violations []Violation
	for k, v := range patch {
		if viol, capped := lengthCapViolation(fieldPrefix, k, v, maxPatchValueLen); capped {
			violations = append(violations, viol)
			continue
		}
		// Whether any reader reads k (an unregistered `_` key, a `_routing`
		// key the generator does not read) is not judged here: the shared
		// key verdict refuses it (ADR-038 §11).
		//
		// A mapping or a list cannot be written from a string (`_metadata`,
		// #2859; `_custom_alerts`, `_namespaces`).
		if ok, reason := cfg.PatchableAsString(k); !ok {
			violations = append(violations, Violation{
				Field:  fmt.Sprintf("%s[%q]", fieldPrefix, k),
				Reason: reason,
			})
			continue
		}
		// #3140: a threshold written as a non-finite number — the
		// exporter's own predicate, the one PUT's body-only pre-flight runs
		// (validateBodyOnly), here on the one value this patch writes.
		// Refused before anything is written, so every batch path (direct,
		// PR, /groups/{id}/batch) answers it with this 400; the writer's
		// merge paths refuse it again on the merged file.
		if msgs := cfg.NonFiniteThresholdWrites(map[string]cfg.ScheduledValue{k: {Default: v}}); len(msgs) > 0 {
			violations = append(violations, Violation{
				Field:  fmt.Sprintf("%s[%q]", fieldPrefix, k),
				Reason: strings.TrimPrefix(msgs[0], k+": "),
			})
			continue
		}
	}
	return violations
}

// ─────────────────────────────────────────────────────────────────
// Batch `unset` ([]string) validation — B2 (#2341)
// ─────────────────────────────────────────────────────────────────

// unsetAllowedList is the keys a batch op's `unset` may remove
// (cfg.BatchUnsettableKeys: the registry's `batch.unset` keys and every
// retired key), for the violation text. Removing a key is not judged by any
// value rule, so each key the registry admits must be one whose removal the
// write path is known to handle — `_routing`, whose removal resets the tenant
// to `_routing_defaults` + its profile (the route generator judges the
// result, #2903), and a retired key, which nothing reads and which, while it
// is on disk, makes the write gate refuse every other write to the tenant
// (#2983). Admitting `_metadata` or `_profile` would also have to widen
// touchesScopeMetadata, which judges only the patch.
func unsetAllowedList() string {
	keys := cfg.BatchUnsettableKeys()
	for i, k := range keys {
		keys[i] = strconv.Quote(k)
	}
	return strings.Join(keys, ", ")
}

// validateBatchEdit validates one batch op's patch and unset together:
// validatePatchMap on the patch (field `<prefix>.patch[...]`), then each
// unset key (field `<prefix>.unset[i]`) — cfg.BatchUnsettable, not repeated,
// not also in the patch. prefix is "operations[i]" for /tenants/batch and ""
// for /groups/{id}/batch. Returns every violation, like validatePatchMap.
// An op with neither patch nor unset is NOT refused here: /tenants/batch
// keeps its pre-B2 no-op for it, and /groups/{id}/batch keeps its own
// earlier 400.
func validateBatchEdit(patch map[string]string, unset []string, prefix string) []Violation {
	field := func(name string) string {
		if prefix == "" {
			return name
		}
		return prefix + "." + name
	}
	violations := validatePatchMap(patch, field("patch"))
	seen := make(map[string]int, len(unset))
	for i, k := range unset {
		at := fmt.Sprintf("%s[%d]", field("unset"), i)
		if len(k) > maxPatchKeyLen {
			violations = append(violations, Violation{Field: at,
				Reason: fmt.Sprintf("key length must not exceed %d characters; got %d", maxPatchKeyLen, len(k))})
			continue
		}
		if !cfg.BatchUnsettable(k) {
			violations = append(violations, Violation{Field: at,
				Reason: fmt.Sprintf("unset can only remove %s; got %q", unsetAllowedList(), k)})
			continue
		}
		if j, dup := seen[k]; dup {
			violations = append(violations, Violation{Field: at,
				Reason: fmt.Sprintf("%q is already listed at %s[%d]", k, field("unset"), j)})
			continue
		}
		seen[k] = i
		if _, set := patch[k]; set {
			violations = append(violations, Violation{Field: at,
				Reason: fmt.Sprintf("%q is also in patch: a key cannot be both set and removed", k)})
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
		if viol, capped := lengthCapViolation(fieldPrefix, k, v, maxFilterValueLen); capped {
			violations = append(violations, viol)
		}
	}
	return violations
}

// WriteValidationErrors and the canonical error envelope live in
// errors.go (PR-9/11 unification).
