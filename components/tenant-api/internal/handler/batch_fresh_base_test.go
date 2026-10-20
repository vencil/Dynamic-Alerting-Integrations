package handler

// PR mode cuts its branch from the FRESH origin base, while the pod's local
// tree is synced only at startup (#2341). The fixtures here build that
// split; the domain-policy checks they were written for are gone (#2903:
// the route generator judges the branch point), and what is left pins the
// PR-mode batch behaviour that does not depend on them.
//
// Also B2 F2: an op that changes nothing (gitops.ErrMergeNoOp) still returns
// the file's deprecation notices, as a byte-identical patch replay does.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
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
	runGit(t, dir, "remote", "set-url", "origin", bare) // seedGitTree added an empty one
	runGit(t, dir, "push", "origin", "main")
	if len(originFiles) > 0 {
		other := t.TempDir()
		if out, err := exec.Command("git", "clone", bare, other).CombinedOutput(); err != nil {
			t.Fatalf("clone: %v\n%s", err, out)
		}
		for name, content := range originFiles {
			if content == "" { // "" = removed on origin
				runGit(t, other, "rm", "-q", name)
				continue
			}
			if err := os.WriteFile(filepath.Join(other, name), []byte(content), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		runGit(t, other, "add", "-A", ".")
		runGit(t, other, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-m", "moved on")
		runGit(t, other, "push", "origin", "main")
	}
	f := &staleFixture{dir: dir, bare: bare}
	f.deps = &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t),
		Groups: groups.NewDirManager(dir), WriteMode: WriteModePR,
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

// assertRejectedOp: w is the 422 of a rejected batch whose op-th result is
// tenant's refusal with code and a message containing msg.
func assertRejectedOp(t *testing.T, w *httptest.ResponseRecorder, op int, tenant, code, msg string) {
	t.Helper()
	var resp BatchResponse
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if w.Code != http.StatusUnprocessableEntity || resp.Status != batchStatusRejected || op >= len(resp.Results) {
		t.Fatalf("status %d, body %s; want 422 rejected with result %d", w.Code, w.Body.String(), op)
	}
	r := resp.Results[op]
	if r.TenantID != tenant || r.Status != "error" || r.Code != code || !strings.Contains(r.Message, msg) {
		t.Errorf("result %d = %+v; want %s refused with %s, message containing %q", op, r, tenant, code, msg)
	}
	if strings.Contains(r.Message, "operations[") {
		t.Errorf("the per-op message carries an op index: %q", r.Message)
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
	files["t-other.yaml"] = tOther
	return files
}

// An unset of a key the base does not carry beside a patch: the patch is
// written, the unset changes nothing.
func TestBatchTenants_PRMode_FreshBaseSkipsAbsentUnset(t *testing.T) {
	f := newStaleFixture(t, staleTree("    _routing_profile: team-chat\n"), nil)
	w := postTenantBatch(t, f.deps, `[{"tenant_id":"t-off","patch":{"cpu_usage_percent":"90"},"unset":["_routing"]}]`)
	if w.Code != http.StatusOK || !f.prOpened {
		t.Fatalf("status = %d (PR opened %v), want 200 and a PR; body: %s", w.Code, f.prOpened, w.Body.String())
	}
}

// policyListing is a `_domain_policy.yaml` body forbidding slack for tenants
// (a policy file on the tree; tenant-api itself no longer reads it, #2903).
func policyListing(tenants string) string {
	return "domain_policies:\n  finance:\n    tenants: [" + tenants + "]\n" +
		"    constraints:\n      forbidden_receiver_types: [slack]\n"
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
