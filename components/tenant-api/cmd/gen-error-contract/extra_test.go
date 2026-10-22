package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/handler"
)

// copyDocs is a temporary copy of the module's docs/ (swag's output).
func copyDocs(t *testing.T) string {
	t.Helper()
	wd, _ := os.Getwd()
	mod, err := moduleRoot(wd)
	if err != nil {
		t.Fatal(err)
	}
	dst := t.TempDir()
	for _, n := range []string{swaggerJSON, swaggerGo, swaggerYAML} {
		b, err := os.ReadFile(filepath.Join(mod, docsRel, n))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dst, n), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dst
}

// realExtra is the Extra fields this module's handlers set.
func realExtra(t *testing.T) []extraUse {
	t.Helper()
	wd, _ := os.Getwd()
	mod, err := moduleRoot(wd)
	if err != nil {
		t.Fatal(err)
	}
	uses, err := scanExtra(mod)
	if err != nil {
		t.Fatal(err)
	}
	return uses
}

// TestScanExtra_ReadsEveryShape: the scan reads the envelope literals with a
// code (a map literal and a variable with an index assignment), the literal
// handed to writeKindAnswer (its kind from kindSpecOf) and renderKind's
// clauses (a returned literal, a variable, a conditional index assignment) —
// and the real tree agrees with internal/errcode's declarations.
func TestScanExtra_ReadsEveryShape(t *testing.T) {
	t.Parallel()
	uses := realExtra(t)
	got := map[string]bool{}
	for _, u := range uses {
		got[string(u.Code)+string(u.Kind)+" "+u.Field+" "+strings.SplitN(u.Site, ":", 2)[0]] = true
	}
	for _, want := range []string{
		"PENDING_PR_EXISTS pr_number internal/handler/tenant_put.go",
		"TASK_NOT_FOUND hint internal/handler/task.go",
		"TENANT_CONFIG_NOT_LOADABLE parse_tree_kind internal/handler/tenant_list.go",
		"INVALID_BODY admission internal/handler/federation/policy.go",
		"precondition_failed current_source_hash internal/handler/tenant_custom_alerts.go",
		"generator_findings unrelated_findings_withheld internal/handler/error_render.go",
		"tenant_file_not_regular tenant_id internal/handler/error_render.go",
		"generator_rejected_retryable reason internal/handler/error_render.go",
		"batch_committed commit internal/handler/error_render.go",
	} {
		if !got[want] {
			t.Errorf("the Extra scan lacks %q", want)
		}
	}
	if err := checkExtra(errcode.Specs(), handler.KindSpecs(), uses, handler.EnvelopeFieldNames()); err != nil {
		t.Fatal(err)
	}
}

const plantedHead = "package handler\n\nimport (\n\t\"net/http\"\n\n\t\"github.com/vencil/tenant-api/internal/errcode\"\n\t\"github.com/vencil/tenant-api/internal/errkind\"\n)\n\n"

// TestScanExtra_FailsClosed: an Extra the scan cannot read stops it with its
// place — never a field that slips past the declarations.
func TestScanExtra_FailsClosed(t *testing.T) {
	t.Parallel()
	for name, body := range map[string]string{
		"key variable":     `WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{Code: errcode.Conflict, Extra: map[string]any{k: 1}})`,
		"index variable":   `e := map[string]any{}; e[k] = 1; WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{Code: errcode.Conflict, Extra: e})`,
		"from a call":      `e := extraOf(); WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{Code: errcode.Conflict, Extra: e})`,
		"set afterwards":   `env := ErrorResponse{Code: errcode.Conflict}; env.Extra = map[string]any{"x": 1}; _ = env`,
		"indexed after":    `env := ErrorResponse{Code: errcode.Conflict, Extra: map[string]any{}}; env.Extra["x"] = 1; _ = env`,
		"no code":          `_ = ErrorResponse{Error: "x", Extra: map[string]any{"message": "m"}}`,
		"spec not looked":  `writeKindAnswer(w, r, KindSpec{}, 0, ErrorResponse{Extra: map[string]any{"message": "m"}})`,
		"another map type": `WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{Code: errcode.Conflict, Extra: extraOf()})`,
	} {
		src := plantedHead + "var _ = errkind.Conflict\n\nfunc extraOf() map[string]any { return nil }\n\n" +
			"func planted(w http.ResponseWriter, r *http.Request, k string) {\n\t" + body + "\n}\n"
		_, err := scanExtra(plantedModule(t, src))
		if err == nil || !strings.Contains(err.Error(), "internal/handler/zz_planted.go:") {
			t.Errorf("%s: scan error %v, want one naming the planted file", name, err)
		}
	}
	// The control: the writeKindAnswer shape is read, with its kind.
	uses, err := scanExtra(plantedModule(t, plantedHead+"func planted(w http.ResponseWriter, r *http.Request) {\n\t"+
		"spec := kindSpecOf(errkind.Conflict)\n\twriteKindAnswer(w, r, spec, 0, ErrorResponse{Extra: map[string]any{\"committed\": false}})\n}\n"))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.ContainsFunc(uses, func(u extraUse) bool {
		return u.Kind == "conflict" && u.Field == "committed" && strings.HasPrefix(u.Site, "internal/handler/zz_planted.go:")
	}) {
		t.Error("the planted writeKindAnswer Extra was not read")
	}
}

// TestCheckExtra_RefusesUndeclared is the intentional break of the
// declarations: a handler that sets a field its code does not declare — in
// an envelope literal, or in a renderKind clause — and a declared field no
// handler sets each stop the command, naming the place or the code.
func TestCheckExtra_RefusesUndeclared(t *testing.T) {
	t.Parallel()
	specs, kinds, fixed := errcode.Specs(), handler.KindSpecs(), handler.EnvelopeFieldNames()

	planted := plantedHead + "var _ = errkind.Conflict\n\nfunc planted(w http.ResponseWriter, r *http.Request) {\n\t" +
		`WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{Code: errcode.PendingPR, Extra: map[string]any{"pr_number": 1, "tenant_id": "x"}})` + "\n}\n"
	uses, err := scanExtra(plantedModule(t, planted))
	if err != nil {
		t.Fatal(err)
	}
	if err := checkExtra(specs, kinds, uses, fixed); err == nil ||
		!strings.Contains(err.Error(), "internal/handler/zz_planted.go:") || !strings.Contains(err.Error(), `"tenant_id" on code PENDING_PR_EXISTS`) {
		t.Errorf("an undeclared field in a literal: %v", err)
	}

	// A renderKind clause: the kind's code is the kind table's.
	if err := checkExtra(specs, kinds, append(realExtra(t), extraUse{Kind: "batch_committed", Field: "tenant_id", Site: "error_render.go:1"}), fixed); err == nil ||
		!strings.Contains(err.Error(), "error_render.go:1") || !strings.Contains(err.Error(), "kind batch_committed") {
		t.Errorf("an undeclared field in a renderKind clause: %v", err)
	}

	// A declared field nothing sets.
	var drop []extraUse
	for _, u := range realExtra(t) {
		if u.Field != "hint" {
			drop = append(drop, u)
		}
	}
	if err := checkExtra(specs, kinds, drop, fixed); err == nil || !strings.Contains(err.Error(), `code TASK_NOT_FOUND declares Extra "hint"`) {
		t.Errorf("a declared field nothing sets: %v", err)
	}
}

// TestCheckExtra_RefusesAFixedName is the intentional break of a declaration
// named like a fixed envelope field (it would replace that field).
func TestCheckExtra_RefusesAFixedName(t *testing.T) {
	t.Parallel()
	specs := errcode.Specs()
	for i := range specs {
		if specs[i].Code == errcode.PendingPR {
			specs[i].Extra = append(specs[i].Extra, "code")
		}
	}
	uses := append(realExtra(t), extraUse{Code: errcode.PendingPR, Field: "code", Site: "x.go:1"})
	if err := checkExtra(specs, handler.KindSpecs(), uses, handler.EnvelopeFieldNames()); err == nil ||
		!strings.Contains(err.Error(), `code PENDING_PR_EXISTS declares Extra "code", a fixed envelope field`) {
		t.Errorf("a declared fixed name: %v", err)
	}
}

// TestBuild_Extra: each code's entry lists its declared fields with their
// types, sorted by name; a code with none has an empty list (never null).
func TestBuild_Extra(t *testing.T) {
	t.Parallel()
	c, err := build(errcode.Specs(), handler.KindSpecs(), realDirect(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range c.Codes {
		if e.Extra == nil {
			t.Errorf("%s: extra is nil", e.Code)
		}
		switch e.Code {
		case errcode.PendingPR:
			want := []extraEntry{{"existing_pr_url", errcode.ExtraString, ""}, {"message", errcode.ExtraString, ""}, {"pr_number", errcode.ExtraInteger, ""}}
			if !slices.Equal(e.Extra, want) {
				t.Errorf("PENDING_PR_EXISTS extra = %v, want %v", e.Extra, want)
			}
		case errcode.GeneratorFindings:
			if !slices.Contains(e.Extra, extraEntry{"findings", errcode.ExtraArray, errcode.ExtraObject}) {
				t.Errorf("GENERATOR_FINDINGS extra = %v", e.Extra)
			}
		case errcode.Internal:
			if len(e.Extra) != 0 {
				t.Errorf("INTERNAL_ERROR extra = %v", e.Extra)
			}
		}
	}
	if _, err := build([]errcode.Spec{{Code: "X", Remedy: errcode.RemedyOperator, Extra: []string{"nope"}}}, nil, nil); err == nil {
		t.Error("a code carrying an undeclared field was accepted")
	}
}

// TestPatchedDocs: the committed docs are their own patch (idempotent); the
// patch adds every declared field to ErrorResponse and nothing else; docs
// with an ErrorResponse that is not the envelope's, or without one, and a Ref
// to a definition the spec lacks are refused.
func TestPatchedDocs(t *testing.T) {
	t.Parallel()
	docs := copyDocs(t)
	j, _ := os.ReadFile(filepath.Join(docs, swaggerJSON))
	g, _ := os.ReadFile(filepath.Join(docs, swaggerGo))
	y, _ := os.ReadFile(filepath.Join(docs, swaggerYAML))
	fields, specs, fixed := errcode.ExtraFields(), errcode.Specs(), handler.EnvelopeFieldNames()
	got, err := patchedDocs(j, g, fields, specs, fixed)
	if err != nil {
		t.Fatal(err)
	}
	for n, want := range map[string][]byte{swaggerJSON: j, swaggerGo: g, swaggerYAML: y} {
		if !bytes.Equal(got[n], want) {
			t.Errorf("%s is not its own patch: run `make api-docs`", n)
		}
	}
	var spec struct {
		Definitions map[string]struct {
			Properties map[string]json.RawMessage `json:"properties"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(got[swaggerJSON], &spec); err != nil {
		t.Fatal(err)
	}
	var props []string
	for p := range spec.Definitions["ErrorResponse"].Properties {
		props = append(props, p)
	}
	want := slices.Clone(fixed)
	for _, f := range fields {
		want = append(want, f.Name)
	}
	slices.Sort(props)
	slices.Sort(want)
	if !slices.Equal(props, want) {
		t.Errorf("ErrorResponse properties %v, want the fixed fields and the declared ones %v", props, want)
	}
	var tenant struct {
		Description string `json:"description"`
		Type        string `json:"type"`
	}
	_ = json.Unmarshal(spec.Definitions["ErrorResponse"].Properties["tenant_id"], &tenant)
	if tenant.Type != "string" || !strings.HasSuffix(tenant.Description, "Codes that carry it: FORBIDDEN, TENANT_CONFIG_NOT_LOADABLE, TENANT_GIT_MANAGED.") {
		t.Errorf("tenant_id property = %+v", tenant)
	}

	if _, err := patchedDocs(bytes.Replace(j, []byte(`"retry_after_s": {`), []byte(`"retry_after_x": {`), 1), g, fields, specs, fixed); err == nil ||
		!strings.Contains(err.Error(), "fixed fields") {
		t.Errorf("an ErrorResponse that is not the envelope's: %v", err)
	}
	if _, err := patchedDocs(j, bytes.Replace(g, []byte(errorResponseMarker), []byte("\n"), 1), fields, specs, fixed); err == nil {
		t.Error("docs.go without the ErrorResponse definition was accepted")
	}
	bad := append(slices.Clone(fields), errcode.ExtraField{Name: "zz", Type: errcode.ExtraObject, Ref: "NoSuchDefinition", Doc: "x"})
	if _, err := patchedDocs(j, g, bad, specs, fixed); err == nil || !strings.Contains(err.Error(), "NoSuchDefinition") {
		t.Errorf("a Ref the spec lacks: %v", err)
	}
}
