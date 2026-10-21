package handler

// #3062: PUT /tenants/{id}, POST /diff and POST /validate refuse a body by
// one sequence (tenant_put_plan.go). This table sends the SAME request to
// the three, on the same tree, in both write modes, and asserts that they
// refuse exactly when PUT does, with PUT's status, code and text (/validate:
// valid false with that text, its convention).
//
// #3064: the git_managed_* rows are the API/git-managed line — a tenant
// whose file is not API-shaped is refused by the three alike, with one code,
// for every caller (the masked /diff of a reader of {id} included).
//
// ⛔ ORDER ROWS. planPut's order is part of its answer, so each pair of
// neighbouring steps has a row whose body both refuse (the *_and_* rows):
// swap the two and PUT answers that row with the other refusal, and the
// row turns red. readBody → the proposed scope on {id} is no such pair: the
// second reads what the first returns. The FIFO pairs are in
// tenant_put_plan_order_unix_test.go.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/credmask"
	"github.com/vencil/tenant-api/internal/errcode"
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
		"  - name: in-prod-ops\n    tenants: [\"" + parityIn + "\"]\n    permissions: [read, write]\n    environments: [production]\n" +
		"  - name: platform\n    tenants: [\"*\"]\n    permissions: [read, write]\n"
)

func paritySec(id, mode string) string {
	return "  " + id + ":\n    _silent_mode: \"" + mode + "\"\n"
}

var (
	parityShared    = "tenants:\n" + paritySec(parityIn, "warning") + paritySec(parityOut, "warning")
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
	// wantText is in PUT's refusal (the order rows: which step answered,
	// when both would answer with want).
	wantText string
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
	// maskedRefusal: for a maskedDiff row, the code of the refusal /diff
	// answers instead of the masked preview — a step after the write scope
	// on {id} that refuses for every caller.
	maskedRefusal string
	// maxBody: Deps.MaxBodyBytes (0: the default).
	maxBody int64
	// setup runs on the tree after its git commit (the FIFO rows).
	setup func(t *testing.T, dir string)
}

func parityCases() []parityCase {
	// parityOwn is {id}'s file API-shaped (#3064): it declares {id} alone.
	parityOwn := "tenants:\n" + paritySec(parityIn, "warning")
	return []parityCase{
		// #3064: a git-managed tenant — its file shared, anchored where
		// tenants are declared, or declaring another id — is refused by
		// every write, for every caller, with one code; /diff and /validate
		// answer it as PUT does. In PR mode PUT judges it on the fresh base,
		// which /validate does not reach (tree).
		{name: "git_managed_shared", file: parityShared,
			groups: "in-team", body: parityBodyIn, want: http.StatusConflict, wantText: string(errcode.TenantGitManaged), tree: true},
		{name: "git_managed_shared_writer_of_both", file: parityShared,
			groups: "both-team", body: parityBodyIn, want: http.StatusConflict, wantText: string(errcode.TenantGitManaged), tree: true},
		{name: "git_managed_shared_platform_writer", file: parityShared,
			groups: "platform", body: parityBodyIn, want: http.StatusConflict, wantText: string(errcode.TenantGitManaged), tree: true},
		{name: "git_managed_anchor_on_tenants", file: "tenants: &t\n" + paritySec(parityIn, "warning"),
			groups: "in-team", body: parityBodyIn, want: http.StatusConflict, wantText: "yaml_indirection", tree: true},
		{name: "git_managed_merge_key", file: "tenants:\n" + paritySec(parityIn, "warning") + "  <<: {" + parityOut + ": {_silent_mode: \"warning\"}}\n",
			groups: "platform", body: parityBodyIn, want: http.StatusConflict, wantText: "yaml_indirection", tree: true},
		{name: "git_managed_id_mismatch", file: "tenants:\n" + paritySec(parityOut, "warning"),
			groups: "both-team", body: parityBodyIn, want: http.StatusConflict, wantText: "id_mismatch", tree: true},
		{name: "git_managed_reader_of_id", file: parityShared, groups: "in-readers", body: parityBodyIn,
			want: http.StatusForbidden, maskedDiff: true, maskedRefusal: string(errcode.TenantGitManaged)},
		// The body must be API-shaped (step 2's body-only pre-flight, which
		// /validate runs in PR mode too).
		{name: "body_declares_another_tenant", file: parityOwn, groups: "both-team",
			body: parityBodyInOut, want: http.StatusBadRequest, wantText: "and no other tenant"},
		{name: "placeholder", file: parityShared, groups: "in-team",
			body: "tenants:\n  " + parityIn + ":\n    _silent_mode: \"" + credmask.Placeholder + "\"\n",
			want: http.StatusBadRequest},
		{name: "metadata_moved_out_of_scope",
			file:   "tenants:\n  " + parityIn + ":\n    _metadata:\n      environment: production\n    _silent_mode: \"warning\"\n",
			groups: "prod-ops", metaEnforce: true,
			body: "tenants:\n  " + parityIn + ":\n    _metadata:\n      environment: dev\n    _silent_mode: \"warning\"\n",
			want: http.StatusForbidden},
		{name: "declared_elsewhere", file: parityOwn, extra: map[string]string{"copy.yaml": "tenants:\n" + paritySec(parityIn, "warning")},
			groups: "in-team", body: parityBodyIn, want: http.StatusConflict, wantText: string(errcode.TenantDeclaredElsewhere), tree: true},
		{name: "invalid_body", file: parityOwn, groups: "in-team",
			body: "tenants:\n  " + parityIn + ":\n    no_such_key_xyz: \"1\"\n", want: http.StatusBadRequest, tree: true},
		{name: "accepted", file: parityOwn, groups: "in-team",
			body: parityBodyIn, want: http.StatusOK},
		{name: "accepted_anchor_inside_own_section",
			file:   "tenants:\n  " + parityIn + ":\n    _silent_mode: &m \"warning\"\n    _severity_dedup: *m\n",
			groups: "in-team", body: parityBodyIn, want: http.StatusOK},
		{name: "reader_of_id", file: parityOwn, groups: "in-readers",
			body: parityBodyIn, want: http.StatusForbidden, maskedDiff: true},

		// Order rows (see the file comment): PUT's answer is the first
		// step's; swapped, it would be the second's.
		//
		// The proposed scope on {id} (403, requireOrgWriteProposedAt's
		// text) before the body's shape (400).
		{name: "proposed_scope_and_body_shape",
			file:   "tenants:\n  " + parityIn + ":\n    _metadata:\n      environment: production\n    _silent_mode: \"warning\"\n",
			groups: "in-prod-ops", metaEnforce: true,
			body: "tenants:\n  " + parityIn + ":\n    _metadata:\n      environment: dev\n    _silent_mode: \"warning\"\n" +
				paritySec(parityOut, "critical"),
			want: http.StatusForbidden, wantText: "this write proposes"},
		// The placeholder (400, its violation) before the body's shape
		// (400, step 2's pre-flight).
		{name: "placeholder_and_body_shape", file: parityShared, groups: "both-team",
			body: "tenants:\n  " + parityIn + ":\n    _silent_mode: \"" + credmask.Placeholder + "\"\n" + paritySec(parityOut, "critical"),
			want: http.StatusBadRequest, wantText: "the placeholder shown in place of a credential"},
		// The placeholder (400, its violation) before step 2's body-only
		// pre-flight (400, a root key the body may not carry).
		{name: "placeholder_and_root_key", file: parityShared, groups: "in-team",
			body: "defaults: {}\ntenants:\n  " + parityIn + ":\n    _silent_mode: \"" + credmask.Placeholder + "\"\n",
			want: http.StatusBadRequest, wantText: "the placeholder shown in place of a credential"},
		// The body's shape (400, body only) before the git-managed file
		// (409, the tree).
		{name: "body_shape_and_git_managed", file: parityShared, groups: "both-team",
			body: parityBodyInOut, want: http.StatusBadRequest, wantText: "and no other tenant"},
		// The git-managed file (409) before validation against the tree
		// (400).
		{name: "git_managed_and_invalid_body", file: parityShared, groups: "in-team",
			body: "tenants:\n  " + parityIn + ":\n    no_such_key_xyz: \"1\"\n",
			want: http.StatusConflict, wantText: string(errcode.TenantGitManaged), tree: true},
		// The tenant file's resolution (409, another file declares {id})
		// before the git-managed verdict (409, its own code).
		{name: "declared_elsewhere_and_git_managed", file: parityShared,
			extra:  map[string]string{"copy.yaml": "tenants:\n" + paritySec(parityIn, "warning")},
			groups: "in-team", body: parityBodyIn, want: http.StatusConflict, wantText: string(errcode.TenantDeclaredElsewhere), tree: true},
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
	addBareOrigin(t, dir) // a PR-mode write's push must succeed (#1724)
	if c.setup != nil {
		c.setup(t, dir)
	}
	mgr := newRBACManager(t, parityRBAC)
	if c.metaEnforce {
		mgr.EnableMetadataWriteScopeEnforce()
	}
	d := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: mgr, WriteMode: WriteModeDirect,
		MaxBodyBytes: c.maxBody}
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
	runParityCases(t, parityCases())
}

// runParityCases sends each case to the three endpoints, in both write
// modes, and asserts they answer as PUT does.
func runParityCases(t *testing.T, cases []parityCase) {
	t.Helper()
	for _, c := range cases {
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
				if c.wantText != "" && !strings.Contains(put.Body.String(), c.wantText) {
					t.Fatalf("PUT answered %s, want the refusal %q", put.Body.String(), c.wantText)
				}
				var vr ValidateResponse
				if val.Code != http.StatusOK || json.Unmarshal(val.Body.Bytes(), &vr) != nil {
					t.Fatalf("/validate: %d %s", val.Code, val.Body.String())
				}

				if put.Code == http.StatusOK {
					// Accepted: /validate says so; /diff previews it.
					if !vr.Valid {
						t.Errorf("/validate refused what PUT wrote: %v", vr.Warnings)
					}
					var dr DiffResponse
					if diff.Code != http.StatusOK || json.Unmarshal(diff.Body.Bytes(), &dr) != nil || !dr.HasDiff {
						t.Errorf("/diff of what PUT wrote: %d %s, want the 200 preview", diff.Code, diff.Body.String())
					}
					return
				}

				want := parityEnvelope(t, put)
				// /diff: PUT's refusal, status and body — but the masked
				// preview for a caller who may read {id} and not write it.
				switch {
				case c.maskedDiff && c.maskedRefusal != "":
					if got := parityEnvelope(t, diff)["code"]; got != c.maskedRefusal {
						t.Errorf("/diff for a reader of {id}: %d %s, want code %s", diff.Code, diff.Body.String(), c.maskedRefusal)
					}
				case c.maskedDiff:
					var dr DiffResponse
					if diff.Code != http.StatusOK || json.Unmarshal(diff.Body.Bytes(), &dr) != nil || !dr.Masked {
						t.Errorf("/diff for a reader of {id}: %d %s, want the masked preview", diff.Code, diff.Body.String())
					}
				default:
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
				// #3064: the git-managed verdict carries its code.
				if gm := want["code"] == string(errcode.TenantGitManaged); gm != (vr.Code == string(errcode.TenantGitManaged)) {
					t.Errorf("/validate code = %q, PUT's code %v", vr.Code, want["code"])
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
