package handler

// #3062: PUT /tenants/{id}, POST /diff and POST /validate refuse a body by
// one sequence (tenant_put_plan.go). This table sends the SAME request to
// the three, on the same tree, in both write modes, and asserts that they
// refuse exactly when PUT does, with PUT's status, code and text (/validate:
// valid false with that text, its convention).
//
// The first two rows are the oracle a preview path of its own opened (round
// 4): a caller who may write In but not Out, body declaring both. PUT's
// answer is the fixed 403 whether or not another conf.d file also declares
// Out; a /diff that judged the duplicate before (or without) Out's write
// scope answered 409 in one case and a 200 preview in the other — whether a
// tenant id the caller may not write is declared elsewhere.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/credmask"
	"github.com/vencil/tenant-api/internal/rbac"
)

const (
	parityIn  = "parity-in"
	parityOut = "parity-out"

	parityRBAC = "groups:\n" +
		"  - name: in-team\n    tenants: [\"" + parityIn + "\"]\n    permissions: [read, write]\n" +
		"  - name: in-readers\n    tenants: [\"" + parityIn + "\"]\n    permissions: [read]\n" +
		"  - name: both-team\n    tenants: [\"" + parityIn + "\", \"" + parityOut + "\"]\n    permissions: [read, write]\n" +
		"  - name: prod-ops\n    tenants: [\"parity-*\"]\n    permissions: [read, write]\n    environments: [production]\n" +
		"  - name: platform\n    tenants: [\"*\"]\n    permissions: [read, write]\n"
)

func paritySec(id, mode string) string {
	return "  " + id + ":\n    _silent_mode: \"" + mode + "\"\n"
}

var (
	parityShared    = "tenants:\n" + paritySec(parityIn, "warning") + paritySec(parityOut, "warning")
	parityElsewhere = map[string]string{parityOut + "-copy.yaml": "tenants:\n" + paritySec(parityOut, "warning")}
	parityBodyIn    = "tenants:\n" + paritySec(parityIn, "critical")
	parityBodyInOut = parityBodyIn + paritySec(parityOut, "critical")
)

// parityCase is one request, sent to the three endpoints.
type parityCase struct {
	name   string
	file   string            // parity-in.yaml
	extra  map[string]string // other conf.d files
	groups string
	// metaEnforce: the metadata write scope is enforced (#1597's row).
	metaEnforce bool
	body        string
	// want is PUT's status in direct mode; refusals are compared in full.
	want int
	// tree: the refusal comes from the steps PUT runs on the tree the
	// write replaces — in PR mode on the fresh base, which /validate does
	// not reach (#1718): it answers valid there (see tenant_put_plan.go).
	tree bool
	// maskedDiff: /diff shows this caller (who may read but not write
	// {id}) the masked preview instead of PUT's 403 (#1560). PUT's 403
	// comes from its ROUTE (the PermWrite middleware), before the handler;
	// /validate's route asks read only, so its refusal is the handler's
	// own write gate on {id} (requireOrgWriteAt's text): refused alike,
	// another text.
	maskedDiff bool
}

func parityCases() []parityCase {
	return []parityCase{
		{name: "unwritable_out_declared_elsewhere", file: parityShared, extra: parityElsewhere,
			groups: "in-team", body: parityBodyInOut, want: http.StatusForbidden},
		{name: "unwritable_out_not_declared_elsewhere", file: parityShared,
			groups: "in-team", body: parityBodyInOut, want: http.StatusForbidden},
		{name: "writable_out_declared_elsewhere", file: parityShared, extra: parityElsewhere,
			groups: "both-team", body: parityBodyInOut, want: http.StatusConflict, tree: true},
		{name: "unspliceable_base_without_2405_bit",
			file:   "tenants:\n" + paritySec(parityIn, "warning") + "  <<: {" + parityOut + ": {_silent_mode: \"warning\"}}\n",
			groups: "both-team", body: parityBodyIn, want: http.StatusBadRequest, tree: true},
		{name: "unspliceable_base_with_2405_bit",
			file:   "tenants:\n" + paritySec(parityIn, "warning") + "  <<: {" + parityOut + ": {_silent_mode: \"warning\"}}\n",
			groups: "platform", body: parityBodyIn, want: http.StatusOK},
		{name: "withheld_caller_anchor_on_tenants",
			file:   "tenants: &t\n" + paritySec(parityIn, "warning") + paritySec(parityOut, "warning"),
			groups: "in-team", body: parityBodyIn, want: http.StatusConflict, tree: true},
		{name: "shown_caller_anchor_on_tenants",
			file:   "tenants: &t\n" + paritySec(parityIn, "warning") + paritySec(parityOut, "warning"),
			groups: "both-team", body: parityBodyIn, want: http.StatusOK},
		{name: "placeholder", file: parityShared, groups: "in-team",
			body: "tenants:\n  " + parityIn + ":\n    _silent_mode: \"" + credmask.Placeholder + "\"\n",
			want: http.StatusBadRequest},
		{name: "metadata_moved_out_of_scope",
			file:   "tenants:\n  " + parityIn + ":\n    _metadata:\n      environment: production\n    _silent_mode: \"warning\"\n",
			groups: "prod-ops", metaEnforce: true,
			body: "tenants:\n  " + parityIn + ":\n    _metadata:\n      environment: dev\n    _silent_mode: \"warning\"\n",
			want: http.StatusForbidden},
		{name: "invalid_body", file: parityShared, groups: "in-team",
			body: "tenants:\n  " + parityIn + ":\n    no_such_key_xyz: \"1\"\n", want: http.StatusBadRequest, tree: true},
		{name: "accepted", file: parityShared, groups: "in-team", body: parityBodyIn, want: http.StatusOK},
		{name: "reader_of_id", file: "tenants:\n" + paritySec(parityIn, "warning"), groups: "in-readers",
			body: parityBodyIn, want: http.StatusForbidden, maskedDiff: true},
	}
}

// parityServe sends c to one endpoint on a fresh copy of its tree.
func parityServe(t *testing.T, c parityCase, pr bool, ep putEndpoint) *httptest.ResponseRecorder {
	t.Helper()
	files := map[string]string{parityIn + ".yaml": c.file}
	for k, v := range c.extra {
		files[k] = v
	}
	dir := setupConfigDir(t, files)
	initGitRepo(t, dir)
	if out, err := exec.Command("git", "-C", dir, "branch", "-M", "main").CombinedOutput(); err != nil {
		t.Fatalf("git branch -M main: %v\n%s", err, out)
	}
	mgr := newRBACManager(t, parityRBAC)
	if c.metaEnforce {
		mgr.EnableMetadataWriteScopeEnforce()
	}
	d := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: mgr, WriteMode: WriteModeDirect}
	if pr {
		d.WriteMode, d.PRClient, d.PRTracker = WriteModePR, &mockPlatformClient{}, &mockPlatformTracker{}
	}
	var h http.HandlerFunc
	method, path, perm := "POST", "/api/v1/tenants/"+parityIn, rbac.PermRead
	switch ep {
	case endpointPut:
		h, method, perm = PutTenant(d), "PUT", rbac.PermWrite
	case endpointDiff:
		h, path = DiffTenant(d), path+"/diff"
	case endpointValidate:
		h, path = ValidateTenant(d), path+"/validate"
	}
	req := newRequestWithChiParam(method, path, "id", parityIn, bytes.NewBufferString(c.body))
	req.Header.Set("Content-Type", "application/yaml")
	req.Header.Set("X-Forwarded-Email", "parity@example.com")
	req.Header.Set("X-Forwarded-Groups", c.groups)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, mgr, perm, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

// parityEnvelope is a refusal's JSON without its request_id.
func parityEnvelope(t *testing.T, w *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &m); err != nil {
		t.Fatalf("decode %d %s: %v", w.Code, w.Body.String(), err)
	}
	delete(m, "request_id")
	return m
}

// parityValidateText is how /validate renders PUT's refusal env: its
// violations one per line, or its text without ErrValidation's prefix (the
// dry-run lists validation errors bare, #2124).
func parityValidateText(env map[string]any) string {
	if vs, ok := env["violations"].([]any); ok && len(vs) > 0 {
		var lines []string
		for _, v := range vs {
			m := v.(map[string]any)
			lines = append(lines, m["field"].(string)+": "+m["reason"].(string))
		}
		return strings.Join(lines, "; ")
	}
	s, _ := env["error"].(string)
	return strings.TrimPrefix(s, "validation failed: ")
}

func TestPutPlan_EndpointsRefuseAsPut(t *testing.T) {
	t.Parallel()
	for _, c := range parityCases() {
		for _, pr := range []bool{false, true} {
			mode := map[bool]string{false: "direct", true: "pr"}[pr]
			t.Run(c.name+"/"+mode, func(t *testing.T) {
				t.Parallel()
				put := parityServe(t, c, pr, endpointPut)
				diff := parityServe(t, c, pr, endpointDiff)
				val := parityServe(t, c, pr, endpointValidate)
				t.Logf("PUT %d %s\n/diff %d %s\n/validate %d %s", put.Code, put.Body.String(),
					diff.Code, diff.Body.String(), val.Code, val.Body.String())

				if put.Code != c.want {
					t.Fatalf("PUT status = %d, want %d", put.Code, c.want)
				}
				var vr ValidateResponse
				if val.Code != http.StatusOK || json.Unmarshal(val.Body.Bytes(), &vr) != nil {
					t.Fatalf("/validate: %d %s", val.Code, val.Body.String())
				}

				if put.Code == http.StatusOK {
					// Accepted: /validate says so; /diff previews it (or, for
					// a caller who may not read the whole file, shows nothing:
					// 422 TENANT_FILE_WITHHELD — not a verdict on the write).
					if !vr.Valid {
						t.Errorf("/validate refused what PUT wrote: %v", vr.Warnings)
					}
					if diff.Code != http.StatusOK &&
						(diff.Code != http.StatusUnprocessableEntity || !strings.Contains(diff.Body.String(), CodeTenantFileWithheld)) {
						t.Errorf("/diff refused what PUT wrote: %d %s", diff.Code, diff.Body.String())
					}
					return
				}

				want := parityEnvelope(t, put)
				// /diff: PUT's refusal, status and body — but the masked
				// preview for a caller who may read {id} and not write it.
				if c.maskedDiff {
					var dr DiffResponse
					if diff.Code != http.StatusOK || json.Unmarshal(diff.Body.Bytes(), &dr) != nil || !dr.Masked {
						t.Errorf("/diff for a reader of {id}: %d %s, want the masked preview", diff.Code, diff.Body.String())
					}
				} else {
					got := parityEnvelope(t, diff)
					if diff.Code != put.Code || !jsonEqual(got, want) {
						t.Errorf("/diff answered %d %v, PUT %d %v", diff.Code, got, put.Code, want)
					}
				}
				// /validate: valid false with PUT's text — except, in PR
				// mode, a refusal PUT takes on the fresh base (#1718).
				if c.maskedDiff {
					if vr.Valid || len(vr.Warnings) != 1 || vr.Warnings[0] != orgWriteDeniedText(parityIn) {
						t.Errorf("/validate for a reader of {id}: valid=%v %q, want the write gate's refusal", vr.Valid, vr.Warnings)
					}
					return
				}
				if pr && c.tree {
					if !vr.Valid {
						t.Errorf("PR-mode /validate judged the serving tree: %v", vr.Warnings)
					}
					return
				}
				if vr.Valid || strings.Join(vr.Warnings, "; ") != parityValidateText(want) {
					t.Errorf("/validate answered valid=%v %q, want PUT's %q", vr.Valid, vr.Warnings, parityValidateText(want))
				}
			})
		}
	}
}

func jsonEqual(a, b map[string]any) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return bytes.Equal(x, y)
}
