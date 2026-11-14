package handler

// /validate asks the write's own judgement (#3016): the route generator and
// the shared key verdict refuse the preview as they refuse the PUT; what
// PUT would answer 429 / 503 leaves the body not judged, with the reason.

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
)

func validateAs(t *testing.T, d *Deps, tenant, body string) ValidateResponse {
	t.Helper()
	req := newRequestWithChiParam("POST", "/api/v1/tenants/"+tenant+"/validate", "id", tenant, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ValidateTenant(d), d.RBAC, rbac.PermRead, TenantIDFromPath).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	var resp ValidateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestValidateTenant_AsksTheWritesJudgement(t *testing.T) {
	t.Parallel()
	blocking := func(_ int, req testutil.FakeValidateRequest) (int, any) {
		v := testutil.PassVerdict(req, nil)
		v["key_verdict"] = map[string]any{"status": "complete", "tenants": 1, "seconds": 0.1, "truncated": 0,
			"findings": []map[string]any{{"severity": "error", "kind": "unread", "key": "_routng",
				"file": "t-a.yaml", "tenants": []string{"t-a"}, "reason": "unregistered", "blocks": true}}}
		return 200, v
	}
	findings := func(_ int, req testutil.FakeValidateRequest) (int, any) {
		return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("route", "t-a", "t-a.yaml", "t-a route is wrong"))
	}
	busy := func(int, testutil.FakeValidateRequest) (int, any) {
		return 200, testutil.RejectedVerdict("busy")
	}
	tooLarge := func(int, testutil.FakeValidateRequest) (int, any) {
		return 413, map[string]any{"error": "too large"}
	}
	memory := func(int, testutil.FakeValidateRequest) (int, any) {
		return 200, testutil.RejectedVerdict("memory")
	}
	unavailable := func(int, testutil.FakeValidateRequest) (int, any) {
		return 503, map[string]any{"error": "VALIDATOR_UNAVAILABLE", "reason": "tool_fault", "message": "x"}
	}
	cases := []struct {
		name      string
		answer    func(int, testutil.FakeValidateRequest) (int, any)
		valid     bool
		judged    bool
		notJudged string
		warning   string
	}{
		{"pass", nil, true, true, "", ""},
		{"a blocking key verdict", blocking, false, true, "", "_routng"},
		{"generator findings", findings, false, true, "", "t-a route is wrong"},
		{"a non-retryable rejection", tooLarge, false, true, "", "too large"},
		{"busy", busy, true, false, "busy", ""},
		// The container's own failure says nothing about the body.
		{"the container ran out of memory", memory, true, false, "memory", ""},
		{"unavailable", unavailable, true, false, "tool_fault", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f, fake := generatorDirectFixture(t, c.answer)
			before := len(fake.Requests())
			resp := validateAs(t, f.deps, "t-a", tAPut)
			if resp.Valid != c.valid || resp.Judged != c.judged || resp.NotJudged != c.notJudged ||
				(c.warning != "" && !strings.Contains(strings.Join(resp.Warnings, "\n"), c.warning)) {
				t.Fatalf("%+v", resp)
			}
			if n := len(fake.Requests()) - before; n != 1 {
				t.Errorf("generator asked %d time(s), want 1 (no base judgement for a preview)", n)
			}
		})
	}
	t.Run("a refused body is not judged", func(t *testing.T) {
		t.Parallel()
		f, fake := generatorDirectFixture(t, nil)
		resp := validateAs(t, f.deps, "t-a", "tenants:\n  t-a:\n    mysql_connections: [\n")
		if resp.Valid || resp.Judged || resp.NotJudged != notJudgedRefused || len(fake.Requests()) != 0 {
			t.Fatalf("%+v, asked %d", resp, len(fake.Requests()))
		}
	})
}

// A caller that went away is no failure of the judgement: not judged,
// canceled (logged at info, not error).
func TestJudgePreview_ACallerThatWentAway(t *testing.T) {
	t.Parallel()
	f, _ := generatorDirectFixture(t, nil)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := newRequestWithChiParam("POST", "/api/v1/tenants/t-a/validate", "id", "t-a",
		bytes.NewBufferString(tAPut)).WithContext(ctx)
	res, errs, err := previewWriter(f.deps).PreviewPut(context.Background(), "t-a", tAPut, nil)
	if err != nil || len(errs) > 0 {
		t.Fatalf("preview: %v %q", err, errs)
	}
	judged, notJudged, warnings, _ := judgePreview(f.deps, req, "t-a", res)
	if judged || notJudged != notJudgedCanceled || len(warnings) != 0 {
		t.Fatalf("judged %v not_judged %q warnings %q", judged, notJudged, warnings)
	}
}
