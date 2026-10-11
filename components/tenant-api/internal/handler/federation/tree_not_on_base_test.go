package federation

// #2856: the federation whitelist, the per-tenant subsets and the AccountID
// registry are tenant-api's own records in the state store, not files in the
// conf.d git tree. Their writes therefore no longer care which branch the
// tree is on: with the worktree stranded on a PR feature branch (the state
// #1723 refuses tenant writes in), each of these writes succeeds at once and
// leaves git exactly as it found it — no ref moves, HEAD stays put, no file
// in the tree changes. Before #2856 they committed to the local base and
// were never pushed, which is the loss this replaced.
//
// The stranded state is built as in internal/handler/tree_not_on_base_test.go:
// a branch in the PR namespace whose copies of the files differ from main's,
// checked out.

import (
	"crypto/rand"
	"crypto/rsa"
	"net/http"
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

// assertFedGitUntouched checks that no ref moved, HEAD is still the
// stranded branch, and the working tree is clean.
func assertFedGitUntouched(t *testing.T, dir string, refs map[string]string) {
	t.Helper()
	for ref, before := range refs {
		if got := fedRevParse(t, dir, ref); got != before {
			t.Errorf("%s moved: a state write touched git", ref)
		}
	}
	if head := strings.TrimSpace(fedGitOutput(t, dir, "symbolic-ref", "--short", "HEAD")); head != fedStrandedBranch {
		t.Errorf("HEAD = %q, want %q: a state write switched branches", head, fedStrandedBranch)
	}
	if st := strings.TrimSpace(fedGitOutput(t, dir, "status", "--porcelain")); st != "" {
		t.Errorf("working tree changed by a state write:\n%s", st)
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

func TestFederationStateWrites_IgnoreTheGitTree(t *testing.T) {
	t.Parallel()
	const tenant = "db-a" // scopedAdminRBAC grants admin on this id
	tenantFile := tenant + ".yaml"
	mainFiles := map[string]string{
		tenantFile:  "tenants:\n  " + tenant + ": {}\n",
		"db-b.yaml": "tenants:\n  db-b: {}\n",
	}
	// The branch changes the tenant file and carries the old conf.d state
	// files, which nothing reads any more.
	branchFiles := map[string]string{
		tenantFile:                  "tenants:\n  " + tenant + ":\n    mysql_connections: \"1\"\n",
		"_federation_policy.yaml":   "whitelist:\n  - metric: branch_only\n",
		"_federation/" + tenantFile: "metrics:\n  - branch_only\n",
	}

	cases := map[string]struct {
		deps       func(t *testing.T, dir string) *handler.Deps
		h          func(d *handler.Deps) http.HandlerFunc
		req        func(t *testing.T) *http.Request
		wantStatus int
	}{
		"PutFederationPolicy": {
			deps: func(t *testing.T, dir string) *handler.Deps {
				st := newDirState(t, t.TempDir())
				return &handler.Deps{
					ConfigDir:        dir,
					Writer:           gitops.NewWriter(dir, ""),
					State:            st,
					FederationPolicy: fedpolicy.NewManager(st),
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
					State:     newDirState(t, t.TempDir()),
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
				st := newDirState(t, t.TempDir())
				return &handler.Deps{
					ConfigDir:  dir,
					Writer:     gitops.NewWriter(dir, ""),
					State:      st,
					RBAC:       newRBACManager(t, platformAdminRBAC),
					Federation: newStrandedTokenManager(t),
					Accounts:   account.NewAllocator(st, nil),
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
				st := newDirState(t, t.TempDir())
				return &handler.Deps{
					ConfigDir:  dir,
					Writer:     gitops.NewWriter(dir, ""),
					State:      st,
					RBAC:       newRBACManager(t, platformAdminRBAC),
					Federation: newStrandedTokenManager(t),
					Accounts:   account.NewAllocator(st, nil),
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

			rec := executeWithRBAC(t, tc.h(d), tc.req(t))
			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d; body: %s", rec.Code, tc.wantStatus, rec.Body.String())
			}
			assertFedGitUntouched(t, dir, refs)
		})
	}
}
