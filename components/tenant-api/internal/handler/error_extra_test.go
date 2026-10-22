package handler

// #3272: an error envelope sends only the Extra fields its code declares in
// internal/errcode (extra.go). These tests hold the three things that makes
// true — a field cannot replace a fixed one, an INTERNAL_ERROR leaks none,
// zero values are still sent — and check the golden corpus against the
// declarations at run time. That every field a handler sets is declared is
// checked from the source by cmd/gen-error-contract.

import (
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// TestErrorResponse_ExtraNotAFixedField: the fixed fields read from the struct's json tags
// are the ones the envelope is documented with — so the reader finds them —
// and none of them is declared as an Extra field.
func TestErrorResponse_ExtraNotAFixedField(t *testing.T) {
	t.Parallel()
	fixed := EnvelopeFieldNames()
	if want := []string{"action", "code", "error", "help", "request_id", "retry_after_s", "violations"}; !slices.Equal(fixed, want) {
		t.Fatalf("EnvelopeFieldNames() = %v, want %v", fixed, want)
	}
	for _, f := range errcode.ExtraFields() {
		if slices.Contains(fixed, f.Name) {
			t.Errorf("errcode declares Extra field %q, a fixed envelope field: it would replace it", f.Name)
		}
	}
}

// extraEnvelopeBody is what WriteErrorEnvelope sends for env, decoded.
func extraEnvelopeBody(t *testing.T, env ErrorResponse) map[string]any {
	t.Helper()
	w := httptest.NewRecorder()
	WriteErrorEnvelope(w, requestWithID("PUT", "/x", "req-extra"), http.StatusConflict, env)
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body %q: %v", w.Body.String(), err)
	}
	return body
}

// TestErrorResponse_ExtraCannotReplaceFixedFields: for every code, an Extra
// naming each fixed field changes nothing — the fixed fields keep their
// values, an unset one stays absent.
func TestErrorResponse_ExtraCannotReplaceFixedFields(t *testing.T) {
	t.Parallel()
	extra := map[string]any{}
	for _, f := range EnvelopeFieldNames() {
		extra[f] = "from-extra"
	}
	for _, s := range errcode.Specs() {
		body := extraEnvelopeBody(t, ErrorResponse{Error: "the text", Code: s.Code, Extra: extra})
		want := map[string]any{"error": "the text", "code": string(s.Code), "request_id": "req-extra"}
		if s.Code == errcode.Internal {
			want["error"] = msgInternal
		}
		if !maps.Equal(body, want) {
			t.Errorf("%s: body %v, want %v", s.Code, body, want)
		}
	}
}

// TestErrorResponse_InternalErrorSendsNoExtra: an INTERNAL_ERROR's text is
// withheld (#1700), and so is every Extra field — `message` carrying the
// same text would otherwise send it.
func TestErrorResponse_InternalErrorSendsNoExtra(t *testing.T) {
	t.Parallel()
	secret := "open /srv/conf.d/tu-a.yaml: permission denied"
	body := extraEnvelopeBody(t, ErrorResponse{Error: secret, Code: errcode.Internal, Extra: map[string]any{
		"message": secret, "error": secret, "tenant_id": "tu-a", "hint": secret,
	}})
	if want := map[string]any{"error": msgInternal, "code": string(errcode.Internal), "request_id": "req-extra"}; !maps.Equal(body, want) {
		t.Errorf("body %v, want %v", body, want)
	}
}

// TestErrorResponse_ExtraOnlyDeclared: a field the code does not declare is
// dropped; declared ones are sent with their zero values (an invisible PR/MR
// is `existing_pr_url: ""` and `pr_number: 0`, a direct batch's conflict is
// `committed: false`, a judged write with nothing to show is `findings: []`);
// an envelope with no code sends no Extra.
func TestErrorResponse_ExtraOnlyDeclared(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		code  errcode.Code
		extra map[string]any
		want  map[string]any
	}{
		{errcode.PendingPR,
			map[string]any{"existing_pr_url": "", "pr_number": 0, "message": "m", "tenant_id": "tu-a", "hint": "h"},
			map[string]any{"existing_pr_url": "", "pr_number": 0.0, "message": "m"}},
		{errcode.Conflict,
			map[string]any{"committed": false, "commit": "c0ffee"},
			map[string]any{"committed": false}},
		{errcode.GeneratorFindings,
			map[string]any{"findings": []any{}, "findings_compared_with_base": false, "reason": "busy"},
			map[string]any{"findings": []any{}, "findings_compared_with_base": false}},
		{errcode.NotFound, map[string]any{"tenant_id": "tu-a"}, map[string]any{}},
		{"", map[string]any{"tenant_id": "tu-a", "message": "m"}, map[string]any{}},
	} {
		body := extraEnvelopeBody(t, ErrorResponse{Error: "e", Code: c.code, Extra: c.extra})
		for _, f := range EnvelopeFieldNames() {
			delete(body, f)
		}
		if fmt.Sprint(body) != fmt.Sprint(c.want) {
			t.Errorf("%q: extra sent %v, want %v", c.code, body, c.want)
		}
	}
}

// jsonType is v's JSON type as errcode names it, after a round trip through
// encoding/json.
func jsonType(t *testing.T, v any) errcode.ExtraType {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal %v: %v", v, err)
	}
	var back any
	if err := json.Unmarshal(b, &back); err != nil {
		t.Fatal(err)
	}
	switch x := back.(type) {
	case string:
		return errcode.ExtraString
	case bool:
		return errcode.ExtraBoolean
	case []any:
		return errcode.ExtraArray
	case map[string]any:
		return errcode.ExtraObject
	case float64:
		if x == float64(int64(x)) {
			return errcode.ExtraInteger
		}
	}
	return errcode.ExtraType(fmt.Sprintf("%T", back))
}

// TestErrorGoldenCorpus_ExtraDeclared is the run-time side of the
// declarations: every error of the golden corpus (errorGoldenCorpus), bare
// and wrapped, answered as every kind it is, under every errCtx shape the
// entries use and for both golden callers — every Extra field renderKind
// gives it is declared for the kind's code, with the declared JSON type (an
// array's elements too). A drop at MarshalJSON would hide an undeclared field
// from the golden itself; this reads it before.
func TestErrorGoldenCorpus_ExtraDeclared(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{goldenTenant + ".yaml": goldenTenantFile})
	mgr := newRBACManager(t, goldenRBACYAML)
	d := &Deps{ConfigDir: dir, RBAC: mgr}
	callers := []*http.Request{goldenCaller(t, mgr, "team"), goldenCaller(t, mgr, "platform")}
	ctxs := []errCtx{
		{},
		{tenantID: goldenTenant},
		{tenantID: goldenTenant, preconditionMessage: true},
		{batch: true},
		{fallback: fallbackWrite},
		{fallback: fallbackInternal, prefix: "op: "},
		{provider: "GitHub"},
	}
	seen := map[string]bool{}
	for _, ge := range errorGoldenCorpus(cfg.ComputeSourceHash([]byte(goldenTenantFile))) {
		for _, err := range []error{ge.err, ge.wrapped} {
			if err == nil {
				continue
			}
			for _, spec := range KindSpecs() {
				if spec.Code == "" {
					continue
				}
				if _, ok := firstKind(err, kindsOf(spec.Kind)); !ok && spec.Kind != errkind.Internal && spec.Kind != errkind.BadRequest {
					continue
				}
				for _, ec := range ctxs {
					for _, r := range callers {
						_, extra := renderKind(spec.Kind, r, d, err, ec)
						for k, v := range extra {
							seen[k] = true
							f, ok := errcode.LookupExtraField(k)
							if !errcode.Carries(spec.Code, k) || !ok {
								t.Errorf("%s answered as %s (%s): Extra %q is not declared for %s in internal/errcode",
									ge.name, spec.Kind, spec.Code, k, spec.Code)
								continue
							}
							if got := jsonType(t, v); got != f.Type {
								t.Errorf("%s answered as %s: Extra %q is a JSON %s, declared %s", ge.name, spec.Kind, k, got, f.Type)
							}
							if f.Type == errcode.ExtraArray {
								b, _ := json.Marshal(v)
								var elems []any
								_ = json.Unmarshal(b, &elems)
								for _, e := range elems {
									if got := jsonType(t, e); got != f.Items {
										t.Errorf("%s answered as %s: an element of %q is a JSON %s, declared %s", ge.name, spec.Kind, k, got, f.Items)
									}
								}
							}
						}
					}
				}
			}
		}
	}
	// The corpus reaches the renderKind fields: a corpus that stopped
	// producing them would pass the loop above without checking anything.
	var missing []string
	for _, k := range []string{"tenant_id", "operation", "findings", "findings_compared_with_base",
		"unrelated_findings_withheld", "reason", "unreadable_count", "unreadable", "config_error",
		"git_managed_reason", "current_source_hash", "message", "committed", "commit"} {
		if !seen[k] {
			missing = append(missing, k)
		}
	}
	if len(missing) > 0 {
		t.Errorf("the golden corpus no longer produces Extra %s", strings.Join(missing, ", "))
	}
}
