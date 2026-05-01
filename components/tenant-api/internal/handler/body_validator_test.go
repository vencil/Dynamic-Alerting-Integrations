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
		name       string
		input      string
		wantReason string // "" = expect pass
	}{
		{"warning lower", "warning", ""},
		{"critical lower", "critical", ""},
		{"all lower", "all", ""},
		{"disable lower", "disable", ""},
		// Case insensitive (production resolve already lower-cases, mirror that).
		{"WARNING upper", "WARNING", ""},
		{"Critical mixed", "Critical", ""},
		// Unknowns rejected with offending value in message.
		{"off (rejected — production uses 'disable' not 'off')", "off",
			`must be one of {warning, critical, all, disable}; got "off"`},
		{"empty string", "",
			`must be one of {warning, critical, all, disable}; got ""`},
		{"nonsense", "purple-elephant",
			`must be one of {warning, critical, all, disable}; got "purple-elephant"`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := validateSilentMode(tt.input)
			if got != tt.wantReason {
				t.Errorf("validateSilentMode(%q) = %q, want %q", tt.input, got, tt.wantReason)
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

func TestValidateNonEmptyString256(t *testing.T) {
	if r := validateNonEmptyString256(""); r != "must not be empty" {
		t.Errorf("empty string: got %q, want 'must not be empty'", r)
	}
	if r := validateNonEmptyString256("ok"); r != "" {
		t.Errorf("'ok': got %q, want pass", r)
	}
	long := strings.Repeat("x", 257)
	if r := validateNonEmptyString256(long); !strings.Contains(r, "256") {
		t.Errorf("257-char string: reason %q should mention 256", r)
	}
	exact := strings.Repeat("x", 256)
	if r := validateNonEmptyString256(exact); r != "" {
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
		"_silent_mode": "purple-elephant",
		"_timeout_ms":  "99999999999", // exceeds 1h cap
		"_quench_min":  "abc",         // not an integer
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
		`operations[0].patch["_quench_min"]`,
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

	h := BatchTenants(gw, configDir, rbacMgr, nil, nil, WriteModeDirect, nil, nil)
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

	h := BatchTenants(gw, configDir, rbacMgr, nil, nil, WriteModeDirect, nil, nil)
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
