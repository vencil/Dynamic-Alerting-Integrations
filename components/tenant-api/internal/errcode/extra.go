package errcode

import "slices"

// The fields an error envelope carries beyond its fixed ones (#3272).
//
// An answer may add fields of its own next to `error` and `code` — the
// pending PR/MR's URL, the tenant a refusal is about, the generator's
// findings. Which ones a code carries, and the JSON type of each, is declared
// here and nowhere else:
//
//   - extraFields names every such field once, with its type. A field has one
//     type whatever code carries it, because it is declared once.
//   - codeExtra lists, per code, the fields its answers may carry.
//
// What reads it:
//
//   - the handler's ErrorResponse.MarshalJSON sends only the fields its code
//     declares and drops (and logs) the rest — so a field written by mistake,
//     a field named like a fixed one (`code`, `error`, …), or any field on an
//     INTERNAL_ERROR (which declares none) never reaches a client;
//   - cmd/gen-error-contract checks that every field a handler sets is
//     declared for its code and every declared field is set somewhere,
//     refuses a declaration named like a fixed envelope field, and writes the
//     declarations into the portal's tenant-api-errors.json and the OpenAPI
//     ErrorResponse (one optional property per field, its description naming
//     the codes that carry it — Swagger 2.0 has no oneOf, so the schema is the
//     union).
//
// A new field: add it to extraFields and to its codes in codeExtra, then
// `make error-contract` (and `make api-docs`).

// ExtraType is the JSON type of an envelope field.
type ExtraType string

// The types.
const (
	ExtraString  ExtraType = "string"
	ExtraInteger ExtraType = "integer"
	ExtraBoolean ExtraType = "boolean"
	ExtraArray   ExtraType = "array"
	ExtraObject  ExtraType = "object"
)

// ExtraTypes lists every ExtraType, in declaration order.
func ExtraTypes() []ExtraType {
	return []ExtraType{ExtraString, ExtraInteger, ExtraBoolean, ExtraArray, ExtraObject}
}

// ExtraField is one envelope field beyond the fixed ones.
type ExtraField struct {
	// Name is the JSON key, a sibling of `error` and `code`.
	Name string
	Type ExtraType
	// Items is the type of an array's elements (Type ExtraArray only).
	Items ExtraType
	// Ref names the OpenAPI definition an object — or an array's object
	// elements — is; "" for an object the spec does not define (it is then
	// an open object).
	Ref string
	// Doc says what the field holds, in one or two sentences: the start of
	// the OpenAPI property's description.
	Doc string
}

// extraFields is every envelope field beyond the fixed ones, each once, by
// name.
var extraFields = []ExtraField{
	{Name: "admission", Type: ExtraArray, Items: ExtraObject,
		Doc: "The federation admission results that refused the whitelist change (metric, state, reason, pii_labels)."},
	{Name: "commit", Type: ExtraString,
		Doc: "The commit the batch was written as."},
	{Name: "committed", Type: ExtraBoolean,
		Doc: "Whether the batch was committed: true on BATCH_COMMITTED (do not resend it), false on a direct-mode batch's CONFLICT."},
	{Name: "config_error", Type: ExtraString,
		Doc: "Why the tenant's conf.d file cannot be loaded (the value GET /api/v1/tenants shows for it)."},
	{Name: "current_source_hash", Type: ExtraString,
		Doc: "The source hash of the tenant file now, to resend with after a refresh. Only for an API-managed tenant file."},
	{Name: "existing_pr_url", Type: ExtraString,
		Doc: "The open PR/MR's URL; empty when the caller may not see it or it is still being created."},
	{Name: "findings", Type: ExtraArray, Items: ExtraObject, Ref: "github_com_vencil_tenant-api_internal_generator.Finding",
		Doc: "The route generator's findings the caller may see, the ones about this write first."},
	{Name: "findings_compared_with_base", Type: ExtraBoolean,
		Doc: "Whether the configuration before the write was judged too; when true every finding carries pre_existing."},
	{Name: "git_managed_reason", Type: ExtraString,
		Doc: "Why the tenant's conf.d file is git-managed (a fixed reason naming no other tenant)."},
	{Name: "hint", Type: ExtraString,
		Doc: "Why the task may be missing (pod_may_have_restarted)."},
	{Name: "message", Type: ExtraString,
		Doc: "Advice for the caller, beside the error text."},
	{Name: "operation", Type: ExtraInteger,
		Doc: "The index of the batch operation the refusal is about."},
	{Name: "parse_tree_kind", Type: ExtraString,
		Doc: "The kind of YAML parse-tree refusal, when that is why the file cannot be loaded."},
	{Name: "pr_number", Type: ExtraInteger,
		Doc: "The open PR/MR's number; 0 when the caller may not see it or it is still being created."},
	{Name: "reason", Type: ExtraString,
		Doc: "Why the route generator gave no verdict (unknown for a reason tenant-api does not know)."},
	{Name: "tenant_id", Type: ExtraString,
		Doc: "The tenant the refusal is about."},
	{Name: "unreadable", Type: ExtraArray, Items: ExtraObject,
		Doc: "The unreadable conf.d entries ({path, reason}); only for a caller with write permission on all tenants."},
	{Name: "unreadable_count", Type: ExtraInteger,
		Doc: "How many conf.d entries cannot be read."},
	{Name: "unrelated_findings_withheld", Type: ExtraInteger,
		Doc: "How many findings unrelated to this write are withheld from the caller; absent when none are."},
}

// codeExtra is, per code, the envelope fields its answers may carry. A code
// not listed carries none (INTERNAL_ERROR among them: its answer is only the
// withheld-text sentence and the request_id).
var codeExtra = map[Code][]string{
	InvalidBody:             {"admission"},
	PendingPR:               {"existing_pr_url", "pr_number", "message"},
	TaskNotFound:            {"hint"},
	Forbidden:               {"tenant_id", "operation"},
	Conflict:                {"current_source_hash", "message", "committed"},
	TenantConfigNotLoadable: {"tenant_id", "config_error", "parse_tree_kind"},
	TenantTreeUnreadable:    {"unreadable_count", "unreadable"},
	TenantStoreFault:        {"unreadable_count", "unreadable"},
	TenantGitManaged:        {"tenant_id", "git_managed_reason"},
	GeneratorFindings:       {"findings", "findings_compared_with_base", "unrelated_findings_withheld"},
	GeneratorRejected:       {"reason"},
	GeneratorBusy:           {"reason"},
	ValidatorUnavailable:    {"reason"},
	PolicyUnavailable:       {"reason"},
	BatchCommitted:          {"committed", "commit"},
}

// ExtraFields lists every envelope field beyond the fixed ones, sorted by
// name. The slice is a copy.
func ExtraFields() []ExtraField {
	return slices.Clone(extraFields)
}

// LookupExtraField is the field named name; ok is false for a name not
// declared.
func LookupExtraField(name string) (ExtraField, bool) {
	for _, f := range extraFields {
		if f.Name == name {
			return f, true
		}
	}
	return ExtraField{}, false
}

// Carries reports whether an answer with code c may carry the envelope field
// name. False for every field when c is not a code (an empty one included).
func Carries(c Code, name string) bool {
	return slices.Contains(codeExtra[c], name)
}
