package gitops

// Masked-credential pre-flight for a whole-document tenant write (#1560).
//
// A PUT /tenants/{id} body is refused when a value decodes to
// credmask.Placeholder — the text a caller without write permission is shown
// in place of a credential. The PUT handler runs it (PlaceholderPreflight, in
// both write modes) and so do both dry-runs behind POST
// /tenants/{id}/validate (dryRunPreflight), so the two give the same verdict
// on the same body.
//
// #2903 (ADR-036 §11): the receiver, matcher-value, group_by, `_routing`
// shape and unknown-routing-key checks this pre-flight used to run are gone.
// Routing is judged by the route generator container alone, on the tree the
// write produces (internal/generator).
//
// ⛔ NOT IN validateBodyOnly, AND NOT IN THE WRITER'S write(). Both also serve
// writes that change only another part of the tenant file: a batch op's
// MERGED document (readMergeBodyOnly) and the custom-alerts PUT (write() via
// WriteIfUnchanged) carry the whole file, so a placeholder already on disk
// would refuse a write that never touched it. Only a body the author wrote in
// full — PUT /tenants/{id} — is judged.

import (
	"fmt"
	"strings"

	"github.com/vencil/tenant-api/internal/credmask"
)

// BodyViolation is one value of a PUT body the pre-flight refuses: Field is
// the document path, Reason why.
type BodyViolation struct {
	Field  string
	Reason string
}

// PlaceholderError is PlaceholderPreflight's refusal. It unwraps to
// ErrValidation; the PUT handler reads Violations for its INVALID_BODY
// response.
type PlaceholderError struct {
	Violations []BodyViolation
}

func (e *PlaceholderError) Error() string {
	return fmt.Sprintf("%v: %s", ErrValidation, strings.Join(e.lines(), "; "))
}

// Unwrap makes errors.Is(err, ErrValidation) hold.
func (e *PlaceholderError) Unwrap() error { return ErrValidation }

func (e *PlaceholderError) lines() []string {
	out := make([]string, len(e.Violations))
	for i, v := range e.Violations {
		out[i] = v.Field + ": " + v.Reason
	}
	return out
}

// PlaceholderPreflight refuses a PUT /tenants/{id} body that writes the
// masked-credential placeholder anywhere. A body the Writer's own pre-flight
// would refuse (over the size cap, bad YAML) passes here, so the Writer
// answers it as before.
func PlaceholderPreflight(yamlContent string) error {
	if v := placeholderViolations(yamlContent); len(v) > 0 {
		return &PlaceholderError{Violations: v}
	}
	return nil
}

// dryRunPreflight is the dry-runs' pre-flight: the Writer's validateBodyOnly,
// then — once the body is well-formed — what PlaceholderPreflight judges.
func dryRunPreflight(tenantID, yamlContent string) []string {
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return errs
	}
	if v := placeholderViolations(yamlContent); len(v) > 0 {
		return (&PlaceholderError{Violations: v}).lines()
	}
	return nil
}

// placeholderViolations refuses every value in the body that decodes to
// credmask.Placeholder (#1560) — the text a caller without write permission
// is shown in place of a credential. Written back, it would replace the real
// credential with the placeholder. The callers shown it cannot PUT (the mask
// and the PUT gate share one predicate); this catches a client that read
// under one identity and writes under another.
//
// ⛔ ON THE DECODED VALUE, ANYWHERE IN THE DOCUMENT: a quoted, `!!str`-tagged,
// block-scalar or aliased placeholder decodes to the same string, and a
// body may move a value to a key the mask never touched.
func placeholderViolations(yamlContent string) []BodyViolation {
	if len(CheckTenantDocSize(yamlContent)) > 0 {
		return nil // the Writer's to refuse, unparsed
	}
	var out []BodyViolation
	for _, path := range credmask.PlaceholderPaths([]byte(yamlContent)) {
		out = append(out, BodyViolation{
			Field: path,
			Reason: fmt.Sprintf("is %q, the placeholder shown in place of a credential to callers "+
				"without write permission; send the real value", credmask.Placeholder),
		})
	}
	return out
}
