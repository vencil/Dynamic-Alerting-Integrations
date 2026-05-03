package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ─────────────────────────────────────────────────────────────────
// Reserved-key validators — unit
// ─────────────────────────────────────────────────────────────────

func TestValidateSilentMode(t *testing.T) {
	tests := []struct {
		name           string
		input          string
		wantSubstrings []string // empty = expect pass; otherwise substrings the reason must contain
	}{
		// Scalar enum form — case-insensitive match per production resolve
		{"warning lower", "warning", nil},
		{"critical lower", "critical", nil},
		{"all lower", "all", nil},
		{"disable lower", "disable", nil},
		{"WARNING upper", "WARNING", nil},
		{"Critical mixed", "Critical", nil},
		// Structured YAML form — production heuristic:
		// `strings.Contains(val, "target:")` distinguishes structured vs
		// scalar. Validator must let structured form pass (downstream
		// parses it; this layer only ensures length/format basics).
		{"structured target+expires (multi-line)",
			"target: warning\nexpires: 2099-12-31T00:00:00Z\nreason: planned migration",
			nil},
		{"structured target only", "target: critical", nil},
		{"structured target=disable", "target: disable", nil},
		// Unknowns rejected — only when neither scalar enum NOR structured form
		{"off (production uses 'disable' not 'off')", "off",
			[]string{`got "off"`, `{warning, critical, all, disable}`}},
		{"empty string", "",
			[]string{`got ""`}},
		{"nonsense scalar", "purple-elephant",
			[]string{`got "purple-elephant"`}},
		// Edge case: value containing a `:` that's NOT `target:` — still
		// goes through the enum path (and gets rejected because
		// `key:value` doesn't match any enum value).
		{"unrelated-key:value", "expires: 2025-01-01",
			[]string{`got "expires: 2025-01-01"`}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := validateSilentMode(tt.input)
			if len(tt.wantSubstrings) == 0 {
				if got != "" {
					t.Errorf("validateSilentMode(%q) = %q, want pass", tt.input, got)
				}
				return
			}
			if got == "" {
				t.Fatalf("validateSilentMode(%q) passed unexpectedly", tt.input)
			}
			for _, sub := range tt.wantSubstrings {
				if !strings.Contains(got, sub) {
					t.Errorf("validateSilentMode(%q): reason %q missing substring %q",
						tt.input, got, sub)
				}
			}
		})
	}
}

func TestValidateNonNegativeIntCap(t *testing.T) {
	const cap = int64(3_600_000)
	v := validateNonNegativeIntCap(cap)

	// Pass cases
	for _, ok := range []string{"0", "1", "1000", "3600000"} {
		if got := v(ok); got != "" {
			t.Errorf("validateNonNegativeIntCap(%d)(%q) = %q, want pass", cap, ok, got)
		}
	}

	// Fail cases — need non-empty reason
	failCases := []struct {
		input          string
		wantSubstrings []string
	}{
		{"-1", []string{"non-negative"}},
		{"3600001", []string{"≤ 3600000", "got 3600001"}},
		{"99999999999", []string{"≤ 3600000", "got 99999999999"}}, // issue #134 example
		{"abc", []string{"integer", "abc"}},
		{"3.14", []string{"integer", "3.14"}},
		{"", []string{"integer"}},
	}
	for _, tt := range failCases {
		t.Run(tt.input, func(t *testing.T) {
			got := v(tt.input)
			if got == "" {
				t.Fatalf("validateNonNegativeIntCap(%d)(%q) passed unexpectedly", cap, tt.input)
			}
			for _, sub := range tt.wantSubstrings {
				if !strings.Contains(got, sub) {
					t.Errorf("validateNonNegativeIntCap(%q): reason %q missing substring %q",
						tt.input, got, sub)
				}
			}
		})
	}
}

func TestValidateProfileReference(t *testing.T) {
	// Empty string MUST pass — customer can patch _profile: "" to
	// clear the profile reference (downstream treats empty as "no
	// profile", same semantic as missing key).
	if r := validateProfileReference(""); r != "" {
		t.Errorf("empty string should pass (clears profile reference); got %q", r)
	}
	if r := validateProfileReference("standard-db"); r != "" {
		t.Errorf("'standard-db': got %q, want pass", r)
	}
	long := strings.Repeat("x", 257)
	if r := validateProfileReference(long); !strings.Contains(r, "256") {
		t.Errorf("257-char string: reason %q should mention 256", r)
	}
	exact := strings.Repeat("x", 256)
	if r := validateProfileReference(exact); r != "" {
		t.Errorf("256-char string (boundary): got %q, want pass", r)
	}
}

// ─────────────────────────────────────────────────────────────────
// validatePatchMap — registry + length-cap composition
// ─────────────────────────────────────────────────────────────────

func TestValidatePatchMap_ValidPassthrough(t *testing.T) {
	patch := map[string]string{
		"_silent_mode":     "warning",
		"_timeout_ms":      "30000",
		"max_connections":  "100", // non-reserved, no registry rule, passes through
		"redis_memory":     "disable",
	}
	if v := validatePatchMap(patch, "operations[0].patch"); len(v) != 0 {
		t.Errorf("expected no violations, got %d: %+v", len(v), v)
	}
}

func TestValidatePatchMap_SingleViolation(t *testing.T) {
	patch := map[string]string{
		"_silent_mode": "purple-elephant",
	}
	v := validatePatchMap(patch, "operations[0].patch")
	if len(v) != 1 {
		t.Fatalf("expected 1 violation, got %d: %+v", len(v), v)
	}
	if !strings.Contains(v[0].Field, "_silent_mode") {
		t.Errorf("field %q should reference _silent_mode", v[0].Field)
	}
	if !strings.Contains(v[0].Reason, "{warning, critical, all, disable}") {
		t.Errorf("reason %q should list valid values", v[0].Reason)
	}
}

func TestValidatePatchMap_MultipleViolations_AllReported(t *testing.T) {
	// Per #134 spec: report ALL violations, not first-only — matches
	// PR-2 forbidden-member listing UX (one round-trip to fix everything).
	patch := map[string]string{
		"_silent_mode":     "purple-elephant",          // bad enum
		"_timeout_ms":      "99999999999",              // exceeds 1h cap
		"_routing_profile": strings.Repeat("p", 257),    // exceeds 256 chars
	}
	v := validatePatchMap(patch, "operations[0].patch")
	if len(v) != 3 {
		t.Fatalf("expected 3 violations (one per bad key), got %d: %+v", len(v), v)
	}

	// Build a set of fields that violated, regardless of map iteration order.
	gotFields := map[string]bool{}
	for _, vio := range v {
		gotFields[vio.Field] = true
	}
	wantFields := []string{
		`operations[0].patch["_silent_mode"]`,
		`operations[0].patch["_timeout_ms"]`,
		`operations[0].patch["_routing_profile"]`,
	}
	for _, want := range wantFields {
		if !gotFields[want] {
			t.Errorf("missing violation for field %q; got: %v", want, gotFields)
		}
	}
}

func TestValidatePatchMap_UnknownReservedKey_PassesThrough(t *testing.T) {
	// Soft whitelist: _* keys not in registry pass through (only
	// generic length cap applies). Catches new threshold-exporter
	// keys without requiring tenant-api release.
	patch := map[string]string{
		"_some_future_reserved_key": "any-value",
	}
	if v := validatePatchMap(patch, "operations[0].patch"); len(v) != 0 {
		t.Errorf("unknown _* key should pass through; got %d violations: %+v", len(v), v)
	}
}

func TestValidatePatchMap_OversizedKey(t *testing.T) {
	hugeKey := strings.Repeat("k", maxPatchKeyLen+1)
	patch := map[string]string{hugeKey: "value"}
	v := validatePatchMap(patch, "operations[0].patch")
	if len(v) != 1 {
		t.Fatalf("expected 1 violation for oversized key, got %d", len(v))
	}
	if !strings.Contains(v[0].Reason, "key length must not exceed") {
		t.Errorf("expected 'key length' violation, got %q", v[0].Reason)
	}
}

func TestValidatePatchMap_OversizedValue(t *testing.T) {
	hugeValue := strings.Repeat("v", maxPatchValueLen+1)
	patch := map[string]string{"some_metric": hugeValue}
	v := validatePatchMap(patch, "operations[0].patch")
	if len(v) != 1 {
		t.Fatalf("expected 1 violation for oversized value, got %d", len(v))
	}
	if !strings.Contains(v[0].Reason, "value length must not exceed") {
		t.Errorf("expected 'value length' violation, got %q", v[0].Reason)
	}
}

func TestValidatePatchMap_SilentMode_StructuredFormPassthrough(t *testing.T) {
	// Production accepts BOTH scalar enum and structured YAML form:
	//   "warning"                                         (scalar)
	//   "target: warning\nexpires: 2099-12-31T00:00:00Z\n" (structured)
	// Validator must let the structured form through (downstream parser
	// handles RFC3339 expires + lower-case target enum). Locks the
	// production-mirror heuristic added in self-review.
	patch := map[string]string{
		"_silent_mode": "target: warning\nexpires: 2099-12-31T00:00:00Z\nreason: planned migration",
	}
	if v := validatePatchMap(patch, "operations[0].patch"); len(v) != 0 {
		t.Errorf("structured _silent_mode form should pass; got %d violations: %+v", len(v), v)
	}
}

func TestValidatePatchMap_ProfileReference_EmptyAllowed(t *testing.T) {
	// Empty `_profile` / `_routing_profile` is a documented "clear
	// the profile reference" semantic (downstream treats empty same
	// as missing). Validator must NOT reject it. Locks the empty-
	// allowed behavior of validateProfileReference added in self-review.
	patch := map[string]string{
		"_profile":         "", // clear profile
		"_routing_profile": "", // clear routing profile
	}
	if v := validatePatchMap(patch, "operations[0].patch"); len(v) != 0 {
		t.Errorf("empty profile reference should pass; got %d violations: %+v", len(v), v)
	}
}

func TestValidatePatchMap_BoundaryNumeric_ExactCapPasses(t *testing.T) {
	// Boundary case for #134 acceptance: cap is INCLUSIVE.
	// _timeout_ms cap = 3_600_000 → "3600000" must pass, "3600001" fail.
	patch := map[string]string{"_timeout_ms": "3600000"}
	if v := validatePatchMap(patch, "operations[0].patch"); len(v) != 0 {
		t.Errorf("exact cap value should pass; got %d violations: %+v", len(v), v)
	}

	patch = map[string]string{"_timeout_ms": "3600001"}
	if v := validatePatchMap(patch, "operations[0].patch"); len(v) != 1 {
		t.Errorf("cap+1 value should fail with 1 violation; got %d: %+v", len(v), v)
	}
}

// ─────────────────────────────────────────────────────────────────
// validateStructTags — struct-level rules
// ─────────────────────────────────────────────────────────────────

func TestValidateStructTags_BatchRequest_RequiredOperations(t *testing.T) {
	// Empty operations list rejected by struct tag (validate:"min=1").
	// Note: handler also has its own len(req.Operations)==0 check; this
	// test exercises the validator-level rule.
	req := BatchRequest{Operations: []BatchOperation{}}
	v := validateStructTags(&req)
	if len(v) == 0 {
		t.Fatal("expected violations on empty Operations slice")
	}
	gotOperations := false
	for _, vio := range v {
		if strings.Contains(vio.Field, "operations") {
			gotOperations = true
		}
	}
	if !gotOperations {
		t.Errorf("expected violation referencing 'operations'; got: %+v", v)
	}
}

func TestValidateStructTags_PutGroupRequest_LabelTooLong(t *testing.T) {
	req := PutGroupRequest{
		Label:       strings.Repeat("L", 257),
		Description: "ok",
	}
	v := validateStructTags(&req)
	if len(v) == 0 {
		t.Fatal("expected violation for label > 256 chars")
	}
	if v[0].Field != "label" {
		t.Errorf("expected field 'label', got %q", v[0].Field)
	}
	if !strings.Contains(v[0].Reason, "256") {
		t.Errorf("expected reason mentioning 256, got %q", v[0].Reason)
	}
}

func TestValidateStructTags_PutGroupRequest_DescriptionTooLong(t *testing.T) {
	req := PutGroupRequest{
		Label:       "ok",
		Description: strings.Repeat("d", 4097),
	}
	v := validateStructTags(&req)
	if len(v) == 0 {
		t.Fatal("expected violation for description > 4096 chars")
	}
	if v[0].Field != "description" {
		t.Errorf("expected field 'description', got %q", v[0].Field)
	}
}

func TestValidateStructTags_PutGroupRequest_EmptyLabel(t *testing.T) {
	req := PutGroupRequest{Label: ""}
	v := validateStructTags(&req)
	if len(v) == 0 {
		t.Fatal("expected violation for empty label")
	}
}

func TestValidateStructTags_ValidPutGroupRequest_NoViolations(t *testing.T) {
	req := PutGroupRequest{
		Label:       "ok",
		Description: "fine",
		Members:     []string{"db-a", "db-b"},
	}
	if v := validateStructTags(&req); len(v) != 0 {
		t.Errorf("expected no violations, got: %+v", v)
	}
}

// ─────────────────────────────────────────────────────────────────
// validateFilterMap — generic length cap only (no registry)
// ─────────────────────────────────────────────────────────────────

func TestValidateFilterMap_ValidPassthrough(t *testing.T) {
	filters := map[string]string{
		"severity": "critical",
		"team":     "platform",
	}
	if v := validateFilterMap(filters, "filters"); len(v) != 0 {
		t.Errorf("expected no violations, got: %+v", v)
	}
}

func TestValidateFilterMap_OversizedValue(t *testing.T) {
	filters := map[string]string{"x": strings.Repeat("v", maxFilterValueLen+1)}
	v := validateFilterMap(filters, "filters")
	if len(v) != 1 {
		t.Fatalf("expected 1 violation, got %d", len(v))
	}
}

// ─────────────────────────────────────────────────────────────────
// writeValidationErrors — JSON shape contract
// ─────────────────────────────────────────────────────────────────

func TestWriteValidationErrors_JSONShape(t *testing.T) {
	w := httptest.NewRecorder()
	violations := []Violation{
		{Field: "operations[0].patch[\"_timeout_ms\"]", Reason: "must be ≤ 3600000"},
		{Field: "operations[0].patch[\"_silent_mode\"]", Reason: "must be one of ..."},
	}
	writeValidationErrors(w, violations)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
	if got := w.Header().Get("Content-Type"); got != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", got)
	}

	var resp map[string]any
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp["error"] != "validation failed" {
		t.Errorf("error = %v, want 'validation failed'", resp["error"])
	}
	if resp["code"] != "INVALID_BODY" {
		t.Errorf("code = %v, want 'INVALID_BODY'", resp["code"])
	}
	gotVios, ok := resp["violations"].([]any)
	if !ok {
		t.Fatalf("violations not an array: %T", resp["violations"])
	}
	if len(gotVios) != 2 {
		t.Errorf("violations count = %d, want 2", len(gotVios))
	}
}

// ─────────────────────────────────────────────────────────────────
// Handler-level integration — BatchTenants happy / sad paths
// ─────────────────────────────────────────────────────────────────

// The full BatchTenants integration requires gitops writer + RBAC
// scaffolding which the existing handler_test.go already has helpers
// for. Here we test the validation path specifically — bad body
// gets 400 + violations BEFORE any per-op work happens.

func TestBatchTenants_BodyValidation_RejectsBadValue(t *testing.T) {
	// Fixture: one op with a bad _timeout_ms value
	body := bytes.NewBufferString(`{
		"operations": [
			{"tenant_id": "db-a", "patch": {"_timeout_ms": "99999999999"}}
		]
	}`)

	configDir := t.TempDir()
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	h := (&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect}).BatchTenants()
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400, body: %s", w.Code, w.Body.String())
	}

	var resp map[string]any
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp["code"] != "INVALID_BODY" {
		t.Errorf("expected code=INVALID_BODY, got %v", resp["code"])
	}
	vios, _ := resp["violations"].([]any)
	if len(vios) == 0 {
		t.Errorf("expected at least 1 violation, got 0")
	}
	first, _ := vios[0].(map[string]any)
	if !strings.Contains(first["field"].(string), "_timeout_ms") {
		t.Errorf("expected violation field referencing _timeout_ms, got %v", first["field"])
	}
}

func TestBatchTenants_BodyValidation_ReportsAllViolations(t *testing.T) {
	// Two ops, both with bad values — response must list BOTH
	body := bytes.NewBufferString(`{
		"operations": [
			{"tenant_id": "db-a", "patch": {"_silent_mode": "purple"}},
			{"tenant_id": "db-b", "patch": {"_timeout_ms": "abc"}}
		]
	}`)

	configDir := t.TempDir()
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	h := (&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect}).BatchTenants()
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", w.Code)
	}

	var resp map[string]any
	_ = json.NewDecoder(w.Body).Decode(&resp)
	vios, _ := resp["violations"].([]any)
	if len(vios) < 2 {
		t.Errorf("expected ≥2 violations (full list, not first-only), got %d: %+v", len(vios), vios)
	}
}
