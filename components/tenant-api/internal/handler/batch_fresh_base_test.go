package handler

// B2 F1 (#2341): PR mode cuts its branch from the FRESH origin base, but the
// per-op domain-policy pre-check reads the pod's local tree, which is synced
// only at startup. An op the local tree lets through can break the policy on
// the base — re-enabling routing with unset, or pointing a tenant routing is
// enabled for on the base at a violating profile. The merge closure now
// judges the merged block on the fresh base too; a refusal there refuses the
// whole batch (403 POLICY_VIOLATION, nothing written, no PR, no branch).
//
// Also B2 F2: an op that changes nothing (gitops.ErrMergeNoOp) still returns
// the file's deprecation notices, as a byte-identical patch replay does.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

const tOther = "tenants:\n  t-other:\n    cpu_usage_percent: '85'\n"

// staleFixture is a local conf.d (the pod's tree) with an origin whose main
// has moved on: originFiles were committed and pushed by someone else, and
// the local tree has NOT pulled them.
type staleFixture struct {
	dir, bare string
	deps      *Deps
	prOpened  bool
}

func newStaleFixture(t *testing.T, local, originFiles map[string]string) *staleFixture {
	t.Helper()
	dir := seedGitTree(t, local)
	bare := t.TempDir()
	runGit(t, bare, "init", "--bare", "-b", "main")
	runGit(t, dir, "remote", "add", "origin", bare)
	runGit(t, dir, "push", "origin", "main")
	if len(originFiles) > 0 {
		other := t.TempDir()
		if out, err := exec.Command("git", "clone", bare, other).CombinedOutput(); err != nil {
			t.Fatalf("clone: %v\n%s", err, out)
		}
		for name, content := range originFiles {
			if err := os.WriteFile(filepath.Join(other, name), []byte(content), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		runGit(t, other, "add", ".")
		runGit(t, other, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-m", "moved on")
		runGit(t, other, "push", "origin", "main")
	}
	f := &staleFixture{dir: dir, bare: bare}
	f.deps = &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t),
		Groups: groups.NewManager(dir), Policy: policy.NewManager(dir), WriteMode: WriteModePR,
		PRClient: &mockPlatformClient{providerName: "github",
			createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
				f.prOpened = true
				return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
			}},
		PRTracker: &mockPlatformTracker{}}
	return f
}

// batchBranches lists tenant-api batch branches, local and on origin.
func (f *staleFixture) batchBranches(t *testing.T) string {
	t.Helper()
	var out []string
	for _, dir := range []string{f.dir, f.bare} {
		b, err := exec.Command("git", "-C", dir, "branch", "--format=%(refname:short)").Output()
		if err != nil {
			t.Fatalf("git branch in %s: %v", dir, err)
		}
		for _, line := range strings.Split(string(b), "\n") {
			if strings.Contains(line, "tenant-api/batch/") {
				out = append(out, line)
			}
		}
	}
	return strings.Join(out, ",")
}

func (f *staleFixture) originFile(t *testing.T, name string) string {
	t.Helper()
	b, err := exec.Command("git", "-C", f.bare, "show", "main:"+name).CombinedOutput()
	if err != nil {
		t.Fatalf("git show main:%s: %v\n%s", name, err, b)
	}
	return string(b)
}

// assertFreshBaseRefused: 403 POLICY_VIOLATION naming operations[op] and
// tenant, no PR, no branch left, origin's main unmoved.
func assertFreshBaseRefused(t *testing.T, f *staleFixture, w *httptest.ResponseRecorder, op int, tenant, originMainBefore string) {
	t.Helper()
	var env map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	msg, _ := env["error"].(string)
	if w.Code != http.StatusForbidden || env["code"] != CodePolicyViolation || env["tenant_id"] != tenant ||
		env["operation"] != float64(op) || !strings.Contains(msg, "latest base branch") ||
		!strings.Contains(msg, fmt.Sprintf("operations[%d] (tenant %s)", op, tenant)) || !strings.Contains(msg, "Nothing in this batch was written") {
		t.Errorf("status %d, body %s; want 403 %s naming operations[%d] (%s) on the latest base", w.Code, w.Body.String(), CodePolicyViolation, op, tenant)
	}
	if vs, _ := env["violations"].([]any); len(vs) == 0 {
		t.Errorf("no policy violations listed: %s", w.Body.String())
	}
	if f.prOpened {
		t.Error("a PR was opened")
	}
	if b := f.batchBranches(t); b != "" {
		t.Errorf("batch branch left behind: %s", b)
	}
	if got := gitRev(t, f.bare, "main"); got != originMainBefore {
		t.Errorf("origin main moved: %s → %s", originMainBefore, got)
	}
}

func gitRev(t *testing.T, dir, ref string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "rev-parse", ref).CombinedOutput()
	if err != nil {
		t.Fatalf("git rev-parse %s: %v\n%s", ref, err, out)
	}
	return strings.TrimSpace(string(out))
}

// staleTree: t-off and t-other, local copy as given.
func staleTree(tOffDisk string) map[string]string {
	files := offTree(tOffDisk)
	files["_domain_policy.yaml"] = "domain_policies:\n  finance:\n    tenants: [t-off, t-other]\n" +
		"    constraints:\n      forbidden_receiver_types: [slack]\n"
	files["t-other.yaml"] = tOther
	return files
}

func tOffFile(disk string) string { return "tenants:\n  t-off:\n    cpu_usage_percent: '85'\n" + disk }

func TestBatchTenants_PRMode_PolicyJudgedOnFreshBase(t *testing.T) {
	cases := []struct {
		name, localTOff, originTOff, ops string
		op                               int
	}{
		// 1. The blind-review case: locally enabled on a compliant profile;
		// on origin, the violating profile with routing disabled. unset
		// re-enables slack on the branch.
		{"unset re-enables on the base", "    _routing_profile: domain-ok\n", offDisabledChat,
			`[{"tenant_id":"t-off","unset":["_routing"]}]`, 0},
		// 2. The pre-existing bypass: locally disabled, enabled on origin;
		// the violating profile lands enabled.
		{"profile patch on a base that has routing enabled", offDisabledOK, "    _routing_profile: domain-ok\n",
			`[{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}}]`, 0},
		// 3. op1 is compliant everywhere; op2 breaks the policy on the base.
		// op1 must not land either.
		{"unrelated op before it", "    _routing_profile: domain-ok\n", offDisabledChat,
			`[{"tenant_id":"t-other","patch":{"cpu_usage_percent":"90"}},{"tenant_id":"t-off","unset":["_routing"]}]`, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newStaleFixture(t, staleTree(tc.localTOff), map[string]string{"t-off.yaml": tOffFile(tc.originTOff)})
			before := gitRev(t, f.bare, "main")
			localBefore := mustRead(t, filepath.Join(f.dir, "t-off.yaml"))
			w := postTenantBatch(t, f.deps, tc.ops)
			assertFreshBaseRefused(t, f, w, tc.op, "t-off", before)
			if f.originFile(t, "t-other.yaml") != tOther {
				t.Error("op1 landed on origin")
			}
			if mustRead(t, filepath.Join(f.dir, "t-off.yaml")) != localBefore {
				t.Error("the local t-off.yaml changed")
			}
		})
	}
}

// 4. Local and origin agree: the per-op pre-check still refuses per op, the
// rest of the batch still becomes one PR — no whole-batch abort.
func TestBatchTenants_PRMode_InSyncKeepsPerOpErrors(t *testing.T) {
	f := newStaleFixture(t, staleTree("    _routing_profile: domain-ok\n"), nil)
	w := postTenantBatch(t, f.deps, `[{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}},
		{"tenant_id":"t-other","patch":{"cpu_usage_percent":"90"}}]`)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Status != "pending_review" || statuses(resp.Results) != "error,included" || !f.prOpened {
		t.Errorf("response = %+v (PR opened %v), want pending_review with error,included", resp, f.prOpened)
	}
}

// An unset of a key the base does not carry changes no routing, so the
// in-lock check does not judge it either — the patch beside it is not
// refused for the routing already on the base (here already violating).
func TestBatchTenants_PRMode_FreshBaseSkipsAbsentUnset(t *testing.T) {
	f := newStaleFixture(t, staleTree("    _routing_profile: team-chat\n"), nil)
	w := postTenantBatch(t, f.deps, `[{"tenant_id":"t-off","patch":{"cpu_usage_percent":"90"},"unset":["_routing"]}]`)
	if w.Code != http.StatusOK || !f.prOpened {
		t.Fatalf("status = %d (PR opened %v), want 200 and a PR; body: %s", w.Code, f.prOpened, w.Body.String())
	}
}

// 5. The group batch's PR mode runs the same closure.
func TestGroupBatch_PRMode_PolicyJudgedOnFreshBase(t *testing.T) {
	local := staleTree("    _routing_profile: domain-ok\n")
	local["_groups.yaml"] = "groups:\n  g-fin:\n    label: Finance\n    members: [t-other, t-off]\n"
	f := newStaleFixture(t, local, map[string]string{"t-off.yaml": tOffFile(offDisabledChat)})
	before := gitRev(t, f.bare, "main")
	req := newRequestWithChiParam("POST", "/api/v1/groups/g-fin/batch", "id", "g-fin", bytes.NewBufferString(`{"unset":["_routing"]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	f.deps.RBAC.Middleware(rbac.PermRead, nil)(GroupBatch(f.deps)).ServeHTTP(w, req)
	assertFreshBaseRefused(t, f, w, 1, "t-off", before)
}

// F2: a no-op op still carries the file's deprecation notices.
func TestBatchTenants_NoOpKeepsDeprecationNotices(t *testing.T) {
	files := map[string]string{
		"_defaults.yaml": deprecationDefaultsYAML,
		"t-dep.yaml":     "tenants:\n  t-dep:\n    mysql_cpu: \"70\"\n",
	}
	for _, mode := range bothModes {
		for _, op := range []string{`{"tenant_id":"t-dep","patch":{}}`, `{"tenant_id":"t-dep","unset":["_routing"]}`} {
			t.Run(string(mode)+"/"+op, func(t *testing.T) {
				resp, prOpened, headMoved, _ := noOpRun(t, mode, files, "["+op+"]")
				if prOpened || headMoved {
					t.Fatalf("no-op wrote something: PR %v, HEAD moved %v", prOpened, headMoved)
				}
				warnings := resp.Warnings
				if mode == WriteModeDirect {
					warnings = resp.Results[0].Warnings
				}
				assertHandlerDeprecationNotices(t, warnings)
			})
		}
	}
}
