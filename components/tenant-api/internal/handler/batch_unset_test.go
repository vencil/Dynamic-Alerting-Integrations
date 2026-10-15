package handler

// B2 (#2341): a batch op's `unset` removes keys from the tenant block — only
// `_routing`, which turns routing back on for a tenant a disabling
// `_routing` turned off, on /tenants/batch and /groups/{id}/batch, in both
// write modes. tenant-api does not judge the routing that results (#2903:
// the route generator judges the tree with the whole batch applied; these
// fixtures run without one), so the ops below are written as merged.

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
)

// offDisabledOK: t-off on the compliant profile, routing disabled.
const offDisabledOK = "    _routing_profile: domain-ok\n    _routing: disable\n"

const unsetRoutingOp = `{"tenant_id":"t-off","unset":["_routing"]}`

// okStatus is a successful op's status in mode.
func okStatus(mode WriteMode) string {
	if mode == WriteModePR {
		return "included"
	}
	return "ok"
}

var bothModes = []WriteMode{WriteModePR, WriteModeDirect}

// hasRoutingKey: the tenant block in file still carries a `_routing` key.
func hasRoutingKey(file string) bool {
	return strings.Contains(file, "    _routing:")
}

func TestBatchTenants_UnsetRoutingReenables(t *testing.T) {
	for _, mode := range bothModes {
		t.Run(string(mode)+"/disabling string", func(t *testing.T) {
			results, file := runOffBatch(t, mode, offDisabledOK, "["+unsetRoutingOp+"]")
			if got := statuses(results); got != okStatus(mode) {
				t.Fatalf("statuses = %s, want %s: %+v", got, okStatus(mode), results)
			}
			if hasRoutingKey(file) || !strings.Contains(file, "_routing_profile: domain-ok") ||
				!strings.Contains(file, "cpu_usage_percent: '85'") {
				t.Errorf("t-off.yaml must keep its other keys and lose _routing:\n%s", file)
			}
		})
		// Removing a `_routing` MAPPING is allowed (a patch may not overwrite
		// one): the tenant falls back to its profile.
		t.Run(string(mode)+"/mapping", func(t *testing.T) {
			disk := "    _routing_profile: domain-ok\n    _routing:\n" + pdReceiver
			results, file := runOffBatch(t, mode, disk, "["+unsetRoutingOp+"]")
			if got := statuses(results); got != okStatus(mode) {
				t.Fatalf("statuses = %s, want %s: %+v", got, okStatus(mode), results)
			}
			if hasRoutingKey(file) || strings.Contains(file, "main-key") || !strings.Contains(file, "_routing_profile: domain-ok") {
				t.Errorf("t-off.yaml must lose the whole _routing mapping:\n%s", file)
			}
		})
	}
}

// Two ops on one tenant in one request: both write modes merge each over the
// ones before it (direct mode since #2900), and every one lands.
func TestBatchTenants_UnsetRoutingStacked(t *testing.T) {
	cases := []struct {
		name, ops, want string // want: statuses, "ok" standing for okStatus(mode)
		file            func(string) bool
	}{
		// op1 sets a profile; op2 re-enables routing over it.
		{"profile then unset", `[
			{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}},` + unsetRoutingOp + `]`,
			"ok,ok", func(f string) bool {
				return !hasRoutingKey(f) && strings.Contains(f, "team-chat")
			}},
		// op1 re-enables; op2 then points the enabled tenant at a profile.
		{"unset then profile", `[` + unsetRoutingOp + `,
			{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}}]`,
			"ok,ok", func(f string) bool {
				return !hasRoutingKey(f) && strings.Contains(f, "team-chat")
			}},
		// An unrelated op after the re-enable.
		{"unset then unrelated", `[` + unsetRoutingOp + `,
			{"tenant_id":"t-off","patch":{"cpu_usage_percent":"90"}}]`,
			"ok,ok", func(f string) bool {
				return !hasRoutingKey(f) && strings.Contains(f, `cpu_usage_percent: "90"`)
			}},
	}
	for _, mode := range bothModes {
		for _, tc := range cases {
			t.Run(string(mode)+"/"+tc.name, func(t *testing.T) {
				results, file := runOffBatch(t, mode, offDisabledOK, tc.ops)
				want := strings.ReplaceAll(tc.want, "ok", okStatus(mode))
				if got := statuses(results); got != want {
					t.Fatalf("statuses = %s, want %s: %+v", got, want, results)
				}
				if !tc.file(file) {
					t.Errorf("t-off.yaml as it lands:\n%s", file)
				}
			})
		}
	}
}

// #2633 blind review: op1 disables, op2 points the tenant at the violating
// profile. op2 is allowed only because op1 is stacked under it — in PR mode
// that is the prior list (judging op2 over the disk block alone, enabled,
// refuses it).
func TestBatchTenants_DisableStackedUnderProfile(t *testing.T) {
	for _, mode := range bothModes {
		t.Run(string(mode), func(t *testing.T) {
			results, file := runOffBatch(t, mode, "    _routing_profile: domain-ok\n", `[
				{"tenant_id":"t-off","patch":{"_routing":"disable"}},
				{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}}]`)
			want := okStatus(mode) + "," + okStatus(mode)
			if got := statuses(results); got != want {
				t.Fatalf("statuses = %s, want %s: %+v", got, want, results)
			}
			if !strings.Contains(file, "team-chat") || !strings.Contains(file, "_routing: disable") {
				t.Errorf("t-off.yaml must carry both ops:\n%s", file)
			}
		})
	}
}

// assertInvalidBody checks a 400 INVALID_BODY carrying a violation on field
// whose reason contains reason — the only violation unless many is set.
func assertInvalidBody(t *testing.T, w *httptest.ResponseRecorder, field, reason string, many bool) {
	t.Helper()
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body: %s", w.Code, w.Body.String())
	}
	var bad ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &bad); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if bad.Code != CodeInvalidBody || (!many && len(bad.Violations) != 1) {
		t.Fatalf("response = %+v, want %s with one violation on %s", bad, CodeInvalidBody, field)
	}
	for _, v := range bad.Violations {
		if v.Field == field && strings.Contains(v.Reason, reason) {
			return
		}
	}
	t.Errorf("violations = %+v, want one on %s containing %q", bad.Violations, field, reason)
}

type unsetValidationCase struct {
	name, edit, field, reason string // edit: the op's patch/unset members as JSON object members
	many                      bool
}

// unsetValidationCases: field is relative to the op ("unset[0]", "patch").
func unsetValidationCases() []unsetValidationCase {
	tooMany := make([]string, 1001)
	for i := range tooMany {
		tooMany[i] = `"_routing"`
	}
	return []unsetValidationCase{
		{"unknown key", `"unset":["_silent_mode"]`, "unset[0]", `unset can only remove "_routing", "_routing_receiver_type"; got "_silent_mode"`, false},
		{"null element", `"unset":[null]`, "unset[0]", `unset can only remove "_routing", "_routing_receiver_type"; got ""`, false},
		{"repeated", `"unset":["_routing","_routing"]`, "unset[1]", `"_routing" is already listed at`, false},
		{"also in patch", `"patch":{"_routing":"disable"},"unset":["_routing"]`, "unset[0]", "also in patch", false},
		{"too many", `"unset":[` + strings.Join(tooMany, ",") + `]`, "unset", "must not exceed 1000 items", true},
	}
}

func TestBatchTenants_UnsetValidation(t *testing.T) {
	for _, mode := range bothModes {
		for _, tc := range unsetValidationCases() {
			t.Run(string(mode)+"/"+tc.name, func(t *testing.T) {
				configDir := seedGitTree(t, offTree(offDisabledOK))
				d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t), WriteMode: mode}
				if mode == WriteModePR {
					d.PRClient = &mockPlatformClient{providerName: "github"}
					d.PRTracker = &mockPlatformTracker{}
				}
				before := gitHead(t, configDir)
				op := `{"tenant_id":"t-off"`
				if tc.edit != "" {
					op += "," + tc.edit
				}
				w := postTenantBatch(t, d, "["+op+"}]")
				assertInvalidBody(t, w, "operations[0]."+tc.field, tc.reason, tc.many)
				if gitHead(t, configDir) != before {
					t.Errorf("refused batch moved HEAD")
				}
			})
		}
		t.Run(string(mode)+"/unset not an array", func(t *testing.T) {
			configDir := seedGitTree(t, offTree(offDisabledOK))
			d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t), WriteMode: mode}
			w := postTenantBatch(t, d, `[{"tenant_id":"t-off","unset":"_routing"}]`)
			if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "invalid JSON") {
				t.Errorf("status = %d, want 400 invalid JSON; body: %s", w.Code, w.Body.String())
			}
		})
	}
}

func TestGroupBatch_UnsetValidation(t *testing.T) {
	for _, mode := range bothModes {
		for _, tc := range unsetValidationCases() {
			t.Run(string(mode)+"/"+tc.name, func(t *testing.T) {
				f := newGroupBatchFixture(t, mode)
				before := f.mainSHA(t)
				w := f.post(t, "{"+tc.edit+"}")
				assertInvalidBody(t, w, tc.field, tc.reason, tc.many)
				if f.prCalls != 0 || f.mainSHA(t) != before {
					t.Errorf("refused group batch wrote something: PR calls %d", f.prCalls)
				}
			})
		}
		t.Run(string(mode)+"/unset not an array", func(t *testing.T) {
			f := newGroupBatchFixture(t, mode)
			if w := f.post(t, `{"unset":{"_routing":true}}`); w.Code != http.StatusBadRequest ||
				!strings.Contains(w.Body.String(), "invalid JSON") {
				t.Errorf("status = %d, want 400 invalid JSON; body: %s", w.Code, w.Body.String())
			}
		})
	}
}

// unsetGroupTree: g-fin = two disabled members on different profiles.
func unsetGroupTree() map[string]string {
	files := routingPolicyTree()
	files["_domain_policy.yaml"] = "domain_policies:\n  finance:\n    tenants: [t-g-ok, t-g-chat]\n" +
		"    constraints:\n      forbidden_receiver_types: [slack]\n"
	files["t-g-ok.yaml"] = "tenants:\n  t-g-ok:\n    cpu_usage_percent: '85'\n" + offDisabledOK
	files["t-g-chat.yaml"] = "tenants:\n  t-g-chat:\n    cpu_usage_percent: '85'\n" + offDisabledChat
	files["_groups.yaml"] = "groups:\n  g-fin:\n    label: Finance\n    members: [t-g-ok, t-g-chat]\n"
	return files
}

func TestGroupBatch_UnsetRouting(t *testing.T) {
	for _, mode := range bothModes {
		t.Run(string(mode), func(t *testing.T) {
			files := unsetGroupTree()
			f := newGroupBatchFixtureOver(t, mode, files)
			resp := decodeGroupBatch(t, f.post(t, `{"unset":["_routing"]}`))
			got := map[string]BatchResult{}
			for _, r := range resp.Results {
				got[r.TenantID] = r
			}
			for _, id := range []string{"t-g-ok", "t-g-chat"} {
				if r := got[id]; r.Status != okStatus(mode) {
					t.Errorf("%s = %+v, want %s", id, r, okStatus(mode))
				}
			}
			if mode == WriteModeDirect {
				for _, name := range []string{"t-g-ok.yaml", "t-g-chat.yaml"} {
					if b, _ := os.ReadFile(filepath.Join(f.configDir, name)); hasRoutingKey(string(b)) {
						t.Errorf("%s still carries _routing:\n%s", name, b)
					}
				}
			} else if f.head == "" {
				t.Error("no PR opened")
			}
		})
	}
}

func TestMergePatchYAML_Unset(t *testing.T) {
	t.Parallel()
	existing := "# head\ntenants:\n  t-a:\n    # keep me\n    cpu_usage_percent: '85'\n" +
		"    _routing:\n      receiver:\n        type: pagerduty\n        service_key: k\n" +
		"    _routing_profile: domain-ok\n"
	out, err := mergePatchYAML([]byte(existing), "t-a", nil, []string{"_routing"})
	if err != nil {
		t.Fatalf("mergePatchYAML: %v", err)
	}
	want := "# head\ntenants:\n  t-a:\n    # keep me\n    cpu_usage_percent: '85'\n    _routing_profile: domain-ok\n"
	if out != want {
		t.Errorf("unset _routing mapping:\n got %q\nwant %q", out, want)
	}
	// Nothing to remove, nothing to set: gitops.ErrMergeNoOp, so the writer
	// writes nothing — no reformatting commit, and no tenant created.
	noOps := []struct {
		name, existing, tenant string
		patch                  map[string]string
		unset                  []string
	}{
		{"absent key", out, "t-a", nil, []string{"_routing"}},
		{"no file", "", "t-a", nil, []string{"_routing"}},
		{"no section", "tenants:\n  other:\n    cpu_usage_percent: '85'\n", "t-a", nil, []string{"_routing"}},
		{"neither patch nor unset", existing, "t-a", map[string]string{}, nil},
		{"neither, no file", "", "t-a", nil, nil},
	}
	for _, tc := range noOps {
		if got, err := mergePatchYAML([]byte(tc.existing), tc.tenant, tc.patch, tc.unset); !errors.Is(err, gitops.ErrMergeNoOp) {
			t.Errorf("%s: got %q, %v; want gitops.ErrMergeNoOp", tc.name, got, err)
		}
	}
	if _, err := mergePatchYAML([]byte("tenants:\n  t-a: oops\n"), "t-a", nil, []string{"_routing"}); err == nil || errors.Is(err, gitops.ErrMergeNoOp) {
		t.Errorf("a non-mapping section must stay a structural error, got %v", err)
	}
	both, err := mergePatchYAML([]byte(existing), "t-a", map[string]string{"cpu_usage_percent": "90"}, []string{"_routing"})
	if err != nil || strings.Contains(both, "_routing:") || !strings.Contains(both, "cpu_usage_percent: \"90\"") {
		t.Errorf("patch + unset: err %v\n%s", err, both)
	}
}
