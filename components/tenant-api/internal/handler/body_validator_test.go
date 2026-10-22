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
// validatePatchMap — keys judged by the registry, values by the write gate
// ─────────────────────────────────────────────────────────────────

func TestValidatePatchMap_ValidPassthrough(t *testing.T) {
	t.Parallel()
	patch := map[string]string{
		"_silent_mode":    "warning",
		"max_connections": "100", // non-reserved, no registry rule, passes through
		"redis_memory":    "disable",
	}
	if v := validatePatchMap(patch, "operations[0].patch"); len(v) != 0 {
		t.Errorf("expected no violations, got %d: %+v", len(v), v)
	}
}

// #3227: a reserved key's VALUE is not judged here — the write gate judges
// it with the exporter's reader, per op (TestBatchTenants_SilentModeValue_*).
// So every value passes this layer, the ones the reader takes and the ones it
// ignores alike; the old copy of the reader's word list refused `off`, which
// the reader takes.
func TestValidatePatchMap_ReservedValuesAreNotJudgedHere(t *testing.T) {
	t.Parallel()
	for _, val := range []string{"warning", "off", "disabled", "false", " warning ", "purple-elephant", "",
		"target: warning\nexpires: 2099-12-31T00:00:00Z\nreason: planned migration"} {
		if v := validatePatchMap(map[string]string{"_silent_mode": val}, "p"); len(v) != 0 {
			t.Errorf("_silent_mode %q: %+v", val, v)
		}
	}
	// `_profile` / `_routing_profile`: the generic 1024 cap only (the 256 cap
	// is gone); empty clears the reference.
	for _, val := range []string{"", "standard-db", strings.Repeat("p", 257), strings.Repeat("p", maxPatchValueLen)} {
		for _, k := range []string{"_profile", "_routing_profile"} {
			if v := validatePatchMap(map[string]string{k: val}, "p"); len(v) != 0 {
				t.Errorf("%s len %d: %+v", k, len(val), v)
			}
		}
	}
}

// A `_` key the registry does not list is read by nothing: refused, with the
// same reason whatever the key (`_timeout_ms` was a hand-kept rule here).
func TestValidatePatchMap_UnregisteredUnderscoreKey(t *testing.T) {
	t.Parallel()
	for _, k := range []string{"_timeout_ms", "_silent_mod", "_some_future_reserved_key"} {
		v := validatePatchMap(map[string]string{k: "1"}, "operations[0].patch")
		if len(v) != 1 || v[0].Field != `operations[0].patch["`+k+`"]` ||
			v[0].Reason != "unknown reserved key (typo?); nothing reads it" {
			t.Errorf("%s: %+v", k, v)
		}
	}
}

// A mapping or a list cannot be set from a string.
func TestValidatePatchMap_NotPatchableAsString(t *testing.T) {
	t.Parallel()
	for _, k := range []string{"_metadata", "_custom_alerts", "_namespaces"} {
		v := validatePatchMap(map[string]string{k: "x"}, "p")
		if len(v) != 1 || !strings.HasPrefix(v[0].Reason, k+" is a ") {
			t.Errorf("%s: %+v", k, v)
		}
	}
}

func TestValidatePatchMap_MultipleViolations_AllReported(t *testing.T) {
	t.Parallel()
	// Per #134 spec: report ALL violations, not first-only — matches
	// PR-2 forbidden-member listing UX (one round-trip to fix everything).
	patch := map[string]string{
		"_timeout_ms":     "99999999999", // unregistered
		"_routing_profle": "p",           // unknown routing key
		"_metadata":       "env: prod",   // a mapping
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
		`operations[0].patch["_timeout_ms"]`,
		`operations[0].patch["_routing_profle"]`,
		`operations[0].patch["_metadata"]`,
	}
	for _, want := range wantFields {
		if !gotFields[want] {
			t.Errorf("missing violation for field %q; got: %v", want, gotFields)
		}
	}
}

func TestValidatePatchMap_OversizedKey(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
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

// ─────────────────────────────────────────────────────────────────
// ValidateStructTags — struct-level rules
// ─────────────────────────────────────────────────────────────────

func TestValidateStructTags_BatchRequest_RequiredOperations(t *testing.T) {
	t.Parallel()
	// Empty operations list rejected by struct tag (validate:"min=1").
	// Note: handler also has its own len(req.Operations)==0 check; this
	// test exercises the validator-level rule.
	req := BatchRequest{Operations: []BatchOperation{}}
	v := ValidateStructTags(&req)
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
	t.Parallel()
	req := PutGroupRequest{
		Label:       strings.Repeat("L", 257),
		Description: "ok",
	}
	v := ValidateStructTags(&req)
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
	t.Parallel()
	req := PutGroupRequest{
		Label:       "ok",
		Description: strings.Repeat("d", 4097),
	}
	v := ValidateStructTags(&req)
	if len(v) == 0 {
		t.Fatal("expected violation for description > 4096 chars")
	}
	if v[0].Field != "description" {
		t.Errorf("expected field 'description', got %q", v[0].Field)
	}
}

func TestValidateStructTags_PutGroupRequest_EmptyLabel(t *testing.T) {
	t.Parallel()
	req := PutGroupRequest{Label: ""}
	v := ValidateStructTags(&req)
	if len(v) == 0 {
		t.Fatal("expected violation for empty label")
	}
}

func TestValidateStructTags_ValidPutGroupRequest_NoViolations(t *testing.T) {
	t.Parallel()
	req := PutGroupRequest{
		Label:       "ok",
		Description: "fine",
		Members:     []string{"db-a", "db-b"},
	}
	if v := ValidateStructTags(&req); len(v) != 0 {
		t.Errorf("expected no violations, got: %+v", v)
	}
}

// ─────────────────────────────────────────────────────────────────
// validateFilterMap — generic length cap only (no registry)
// ─────────────────────────────────────────────────────────────────

func TestValidateFilterMap_ValidPassthrough(t *testing.T) {
	t.Parallel()
	filters := map[string]string{
		"severity": "critical",
		"team":     "platform",
	}
	if v := validateFilterMap(filters, "filters"); len(v) != 0 {
		t.Errorf("expected no violations, got: %+v", v)
	}
}

func TestValidateFilterMap_OversizedValue(t *testing.T) {
	t.Parallel()
	filters := map[string]string{"x": strings.Repeat("v", maxFilterValueLen+1)}
	v := validateFilterMap(filters, "filters")
	if len(v) != 1 {
		t.Fatalf("expected 1 violation, got %d", len(v))
	}
}

// ─────────────────────────────────────────────────────────────────
// WriteValidationErrors — JSON shape contract
// ─────────────────────────────────────────────────────────────────

func TestWriteValidationErrors_JSONShape(t *testing.T) {
	t.Parallel()
	w := httptest.NewRecorder()
	violations := []Violation{
		{Field: "operations[0].patch[\"_timeout_ms\"]", Reason: "unknown reserved key (typo?); nothing reads it"},
		{Field: "operations[0].patch[\"_metadata\"]", Reason: "_metadata is a mapping ..."},
	}
	WriteValidationErrors(w, nil, violations)

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
	t.Parallel()
	// Fixture: one op with an unregistered `_` key (#3227: refused by the
	// registry, no longer by a hand-kept integer rule)
	body := bytes.NewBufferString(`{
		"operations": [
			{"tenant_id": "db-a", "patch": {"_timeout_ms": "99999999999"}}
		]
	}`)

	configDir := t.TempDir()
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	h := BatchTenants(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})
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
	if first["reason"] != "unknown reserved key (typo?); nothing reads it" {
		t.Errorf("reason = %v", first["reason"])
	}
}

func TestBatchTenants_BodyValidation_ReportsAllViolations(t *testing.T) {
	t.Parallel()
	// Two ops, both with keys a patch cannot set — response must list BOTH
	body := bytes.NewBufferString(`{
		"operations": [
			{"tenant_id": "db-a", "patch": {"_namespaces": "ns-a"}},
			{"tenant_id": "db-b", "patch": {"_timeout_ms": "abc"}}
		]
	}`)

	configDir := t.TempDir()
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	h := BatchTenants(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})
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
