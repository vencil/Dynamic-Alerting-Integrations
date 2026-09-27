package federation

// #1723 handler mapping for the federation write paths: the whitelist PUT,
// the per-tenant subset PUT, and the two account-registry writes (logs token
// issuance → EnsureAccountID, fleet backfill → Backfill), which reach
// MutateConfigFile through account.Allocator and map errors through
// writeFederationGitError. A worktree left on a PR feature branch must reach
// the client as 503 TREE_NOT_ON_BASE with Retry-After, whatever layer the
// sentinel crosses on the way; the retry, now on base, succeeds.
//
// The stranded state is built as in internal/handler/tree_not_on_base_test.go:
// a branch in the PR namespace whose copies of the files the write reads
// differ from main's, checked out.

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/platform"
)

const fedStrandedBranch = platform.BranchPrefix + "db-z/20260101-000000"

func fedRunGit(t *testing.T, dir string, args ...string) {
	t.Helper()
	if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

func fedRevParse(t *testing.T, dir, ref string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "rev-parse", ref).Output()
	if err != nil {
		t.Fatalf("rev-parse %s: %v", ref, err)
	}
	return strings.TrimSpace(string(out))
}

// fedStrandedRepo commits mainFiles on main and branchFiles on top of them on
// fedStrandedBranch, which it leaves checked out. Returns the dir and the two
// refs as they stand before any request.
func fedStrandedRepo(t *testing.T, mainFiles, branchFiles map[string]string) (string, map[string]string) {
	t.Helper()
	dir := setupConfigDir(t, mainFiles)
	initGitRepo(t, dir)
	fedRunGit(t, dir, "branch", "-M", "main")
	fedRunGit(t, dir, "checkout", "-b", fedStrandedBranch)
	for name, content := range branchFiles {
		p := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	fedRunGit(t, dir, "add", "-A")
	fedRunGit(t, dir, "commit", "-m", "proposal")
	return dir, map[string]string{
		"main":            fedRevParse(t, dir, "main"),
		fedStrandedBranch: fedRevParse(t, dir, fedStrandedBranch),
	}
}

// assertFedTreeNotOnBase503 checks the retryable 503 shape, that the body
// names neither the stranded branch nor the config dir, and that no ref moved.
func assertFedTreeNotOnBase503(t *testing.T, rec *httptest.ResponseRecorder, dir string, refs map[string]string) {
	t.Helper()
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503; body: %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Retry-After") == "" {
		t.Error("503 without Retry-After")
	}
	var env struct {
		Code string `json:"code"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode body: %v; %s", err, rec.Body.String())
	}
	if env.Code != handler.CodeTreeNotOnBase {
		t.Errorf("code = %q, want %q; body: %s", env.Code, handler.CodeTreeNotOnBase, rec.Body.String())
	}
	body := rec.Body.String()
	for _, leak := range []string{"db-z", platform.BranchPrefix, dir} {
		if strings.Contains(body, leak) {
			t.Errorf("body leaks %q: %s", leak, body)
		}
	}
	for ref, before := range refs {
		if got := fedRevParse(t, dir, ref); got != before {
			t.Errorf("%s moved although the write was refused", ref)
		}
	}
	if head := strings.TrimSpace(fedGitOutput(t, dir, "symbolic-ref", "--short", "HEAD")); head != "main" {
		t.Errorf("HEAD = %q after the 503, want main (the return to base)", head)
	}
}

func fedGitOutput(t *testing.T, dir string, args ...string) string {
	t.Helper()
	out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).Output()
	if err != nil {
		t.Fatalf("git %v: %v", args, err)
	}
	return string(out)
}

// assertFedRetryLanded checks the retry got wantStatus and added exactly one
// commit on main.
func assertFedRetryLanded(t *testing.T, rec *httptest.ResponseRecorder, wantStatus int, dir string, refs map[string]string) {
	t.Helper()
	if rec.Code != wantStatus {
		t.Fatalf("retry status = %d, want %d; body: %s", rec.Code, wantStatus, rec.Body.String())
	}
	if got := fedRevParse(t, dir, "main~1"); got != refs["main"] {
		t.Errorf("retry did not add exactly one commit on main")
	}
	if got := fedRevParse(t, dir, fedStrandedBranch); got != refs[fedStrandedBranch] {
		t.Errorf("retry moved the stranded branch")
	}
}

func newStrandedTokenManager(t *testing.T) *token.Manager {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	fed, err := token.NewManagerForTest(key, filepath.Join(t.TempDir(), "fed-store.json"), time.Hour)
	if err != nil {
		t.Fatalf("NewManagerForTest: %v", err)
	}
	return fed
}

func TestFederationWrites_StrandedOnPRBranch_503ThenRetry(t *testing.T) {
	t.Parallel()
	const tenant = "db-a" // scopedAdminRBAC grants admin on this id
	tenantFile := tenant + ".yaml"
	mainFiles := map[string]string{
		tenantFile:  "tenants:\n  " + tenant + ": {}\n",
		"db-b.yaml": "tenants:\n  db-b: {}\n",
	}
	// The branch changes every file these writes read — the tenant, the
	// whitelist, the subset — and adds a tenant, so a backfill prepared on
	// the branch would allocate an id to a tenant base does not have.
	branchFiles := map[string]string{
		tenantFile:                  "tenants:\n  " + tenant + ":\n    mysql_connections: \"1\"\n",
		"_federation_policy.yaml":   "whitelist:\n  - metric: branch_only\n",
		"_federation/" + tenantFile: "metrics:\n  - branch_only\n",
		"db-branch-only.yaml":       "tenants:\n  db-branch-only: {}\n",
	}

	cases := map[string]struct {
		deps       func(t *testing.T, dir string) *handler.Deps
		h          func(d *handler.Deps) http.HandlerFunc
		req        func(t *testing.T) *http.Request
		wantStatus int
	}{
		"PutFederationPolicy": {
			deps: func(t *testing.T, dir string) *handler.Deps {
				return &handler.Deps{
					ConfigDir:        dir,
					Writer:           gitops.NewWriter(dir, ""),
					FederationPolicy: fedpolicy.NewManager(dir),
					RBAC:             newRBACManager(t, platformAdminRBAC),
				}
			},
			h: PutFederationPolicy,
			req: func(t *testing.T) *http.Request {
				return fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"mysql_up"}]}`)
			},
			wantStatus: http.StatusOK,
		},
		"PutTenantFederation": {
			deps: func(t *testing.T, dir string) *handler.Deps {
				return &handler.Deps{
					ConfigDir: dir,
					Writer:    gitops.NewWriter(dir, ""),
					FederationPolicy: fedpolicy.NewManagerForTest(&fedpolicy.Config{
						Whitelist: []fedpolicy.WhitelistEntry{{Metric: "mysql_up"}},
					}),
					RBAC: newRBACManager(t, scopedAdminRBAC),
				}
			},
			h: PutTenantFederation,
			req: func(t *testing.T) *http.Request {
				return fedReq(t, "PUT", "/api/v1/tenants/"+tenant+"/federation", "id", tenant, `{"metrics":["mysql_up"]}`)
			},
			wantStatus: http.StatusOK,
		},
		"CreateFederationToken_logs": {
			deps: func(t *testing.T, dir string) *handler.Deps {
				w := gitops.NewWriter(dir, "")
				return &handler.Deps{
					ConfigDir:  dir,
					Writer:     w,
					RBAC:       newRBACManager(t, platformAdminRBAC),
					Federation: newStrandedTokenManager(t),
					Accounts:   account.NewAllocator(w),
				}
			},
			h: CreateFederationToken,
			req: func(t *testing.T) *http.Request {
				return fedReq(t, "POST", "/api/v1/federation/tokens", "", "", `{"tenant_id":"`+tenant+`","capability":"logs"}`)
			},
			wantStatus: http.StatusCreated,
		},
		"BackfillAccounts": {
			deps: func(t *testing.T, dir string) *handler.Deps {
				w := gitops.NewWriter(dir, "")
				return &handler.Deps{
					ConfigDir:  dir,
					Writer:     w,
					RBAC:       newRBACManager(t, platformAdminRBAC),
					Federation: newStrandedTokenManager(t),
					Accounts:   account.NewAllocator(w),
				}
			},
			h: BackfillAccounts,
			req: func(t *testing.T) *http.Request {
				return fedReq(t, "POST", "/api/v1/federation/accounts/backfill", "", "", "")
			},
			wantStatus: http.StatusOK,
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir, refs := fedStrandedRepo(t, mainFiles, branchFiles)
			d := tc.deps(t, dir)

			assertFedTreeNotOnBase503(t, executeWithRBAC(t, tc.h(d), tc.req(t)), dir, refs)
			// Nothing of the branch's proposal is left in the tree.
			if _, err := os.Stat(filepath.Join(dir, "db-branch-only.yaml")); !os.IsNotExist(err) {
				t.Errorf("branch-only tenant file still in the tree after the 503 (stat err: %v)", err)
			}

			assertFedRetryLanded(t, executeWithRBAC(t, tc.h(d), tc.req(t)), tc.wantStatus, dir, refs)
		})
	}
}
