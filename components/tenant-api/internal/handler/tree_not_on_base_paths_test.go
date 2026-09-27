package handler

// #1723 handler mapping, continued: the direct-commit write paths that
// tree_not_on_base_test.go does not drive — custom alerts (WriteIfUnchanged)
// and the special-file handlers that go through MutateConfigFile and
// writeConfigFileError (PutView, DeleteView, DeleteGroup). Each one must reach
// the client as 503 TREE_NOT_ON_BASE with Retry-After: a layer that wrapped the
// sentinel with %v, or into its own error type, would turn it into a 500 / 400
// / 409 and these tests would say which.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
)

// strandedRepoWith is strandedRepo with caller-chosen contents: mainFiles are
// committed on main, branchFiles on top of them on strandedBranch, which is
// left checked out. The return to base succeeds (no index.lock).
func strandedRepoWith(t *testing.T, mainFiles, branchFiles map[string]string) string {
	t.Helper()
	dir := setupConfigDir(t, mainFiles)
	initGitRepo(t, dir)
	runGit(t, dir, "branch", "-M", "main")
	runGit(t, dir, "checkout", "-b", strandedBranch)
	for name, content := range branchFiles {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	runGit(t, dir, "add", "-A")
	runGit(t, dir, "commit", "-m", "proposal")
	return dir
}

// assertNoPathLeak checks the body carries neither the config dir's absolute
// path nor the PR branch namespace (assertRetryable503 checks the tenant id
// inside the branch name).
func assertNoPathLeak(t *testing.T, rec *httptest.ResponseRecorder, dir string) {
	t.Helper()
	body := rec.Body.String()
	if strings.Contains(body, dir) {
		t.Errorf("body leaks the config dir path: %s", body)
	}
	if strings.Contains(body, platform.BranchPrefix) {
		t.Errorf("body names the PR branch namespace: %s", body)
	}
}

// assertRetryLanded checks the retry answered 200 and added exactly one
// commit on main, leaving the stranded branch where it was.
func assertRetryLanded(t *testing.T, rec *httptest.ResponseRecorder, dir string, refs map[string]string) {
	t.Helper()
	if rec.Code != http.StatusOK {
		t.Fatalf("retry status = %d, want 200; body: %s", rec.Code, rec.Body.String())
	}
	if got := revParse(t, dir, "main~1"); got != refs["main"] {
		t.Errorf("retry did not add exactly one commit on main")
	}
	if got := revParse(t, dir, strandedBranch); got != refs[strandedBranch] {
		t.Errorf("retry moved the stranded branch")
	}
}

const strandedViewsBranchYAML = testViewsYAML + `  branch-only:
    label: Branch Only
    created_by: other@example.com
    filters:
      tier: tier-9
`

const strandedGroupsBranchYAML = testGroupsYAML + `  branch-only:
    label: Branch Only
    members:
      - db-a
`

func TestCustomAlerts_StrandedOnPRBranch_503ThenRetry(t *testing.T) {
	t.Parallel()
	branchTenant := "tenants:\n  db-a:\n    mysql_connections: \"55\"\n"
	dir := strandedRepoWith(t,
		map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults},
		map[string]string{"db-a.yaml": branchTenant})
	refs := snapshotRefs(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: gitops.NewWriter(dir, dir), RBAC: newRBACManager(t, caWriteRBAC)}

	// The client read the tenant while the tree was stranded, so its
	// base_hash is the branch copy's — the cheap pre-lock check passes, and
	// the write reaches commitFileChange.
	put := func(hash string) *httptest.ResponseRecorder {
		body := `{"base_hash":"` + hash + `","custom_alerts":[{"recipe":"threshold","name":"q","metric":"m","threshold":"1","window":"5m"}]}`
		req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a/custom-alerts", "id", "db-a",
			bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/json")
		return servePopulatingRBAC(t, PutTenantCustomAlerts(deps), req, "alice@example.com", []string{"dba"})
	}
	rec := put(cfg.ComputeSourceHash([]byte(branchTenant)))
	assertRetryable503(t, rec, CodeTreeNotOnBase, dir, refs)
	assertNoPathLeak(t, rec, dir)

	// The retry re-reads the tenant (now base's copy) and lands on main.
	onBase, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if string(onBase) != caTenantYAML {
		t.Fatalf("tree not back on base after the 503; db-a.yaml:\n%s", onBase)
	}
	assertRetryLanded(t, put(cfg.ComputeSourceHash(onBase)), dir, refs)
}

func TestSpecialFileWrites_StrandedOnPRBranch_503ThenRetry(t *testing.T) {
	t.Parallel()
	cases := map[string]struct {
		mainFiles, branchFiles map[string]string
		do                     func(t *testing.T, dir string) func() *httptest.ResponseRecorder
	}{
		"PutView": {
			mainFiles:   map[string]string{"_views.yaml": testViewsYAML},
			branchFiles: map[string]string{"_views.yaml": strandedViewsBranchYAML},
			do: func(t *testing.T, dir string) func() *httptest.ResponseRecorder {
				h := PutView(&Deps{Views: views.NewManager(dir), Writer: gitops.NewWriter(dir, dir)})
				return func() *httptest.ResponseRecorder {
					req := newRequestWithChiParam("PUT", "/api/v1/views/my-view", "id", "my-view",
						bytes.NewBufferString(`{"label":"My View","filters":{"tier":"tier-2"}}`))
					req.Header.Set("Content-Type", "application/json")
					setRequestIdentity(req, "op@example.com")
					return executeWithRBAC(t, h, req)
				}
			},
		},
		"DeleteView": {
			mainFiles:   map[string]string{"_views.yaml": testViewsYAML},
			branchFiles: map[string]string{"_views.yaml": strandedViewsBranchYAML},
			do: func(t *testing.T, dir string) func() *httptest.ResponseRecorder {
				h := DeleteView(&Deps{Views: views.NewManager(dir), Writer: gitops.NewWriter(dir, dir)})
				return func() *httptest.ResponseRecorder {
					req := newRequestWithChiParam("DELETE", "/api/v1/views/prod-finance", "id", "prod-finance", nil)
					setRequestIdentity(req, "op@example.com")
					return executeWithRBAC(t, h, req)
				}
			},
		},
		"DeleteGroup": {
			mainFiles:   map[string]string{"_groups.yaml": testGroupsYAML},
			branchFiles: map[string]string{"_groups.yaml": strandedGroupsBranchYAML},
			do: func(t *testing.T, dir string) func() *httptest.ResponseRecorder {
				h := DeleteGroup(&Deps{Groups: groups.NewManager(dir), Writer: gitops.NewWriter(dir, dir), RBAC: permissiveRBACManager(t)})
				return func() *httptest.ResponseRecorder {
					req := newRequestWithChiParam("DELETE", "/api/v1/groups/staging-all", "id", "staging-all", nil)
					setRequestIdentity(req, "op@example.com")
					return executeWithRBAC(t, h, req)
				}
			},
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := strandedRepoWith(t, tc.mainFiles, tc.branchFiles)
			refs := snapshotRefs(t, dir)
			do := tc.do(t, dir)

			rec := do()
			assertRetryable503(t, rec, CodeTreeNotOnBase, dir, refs)
			assertNoPathLeak(t, rec, dir)
			for file, content := range tc.mainFiles {
				got, err := os.ReadFile(filepath.Join(dir, file))
				if err != nil {
					t.Fatal(err)
				}
				if string(got) != content {
					t.Fatalf("tree not back on base after the 503; %s:\n%s", file, got)
				}
			}

			assertRetryLanded(t, do(), dir, refs)
		})
	}
}

// A direct PUT that the stranded tree would refuse BEFORE reaching the commit:
// the branch's own reads — which file declares the tenant, the base-hash
// comparison — must not answer for base. The branch check runs as soon as the
// write lock is held, before any of them, so the answer is the retryable 503
// and the retry is judged on base.
func TestPutTenantDirect_StrandedOnPRBranch_BranchReadsDoNotAnswer(t *testing.T) {
	t.Parallel()
	baseTenant := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	cases := map[string]struct {
		branchFiles map[string]string
		baseHash    string
	}{
		// A: on the branch a second file declares the tenant, which read on
		// the branch is 409 TENANT_DECLARED_ELSEWHERE; base has no such file.
		"DeclaredElsewhereOnBranchOnly": {
			branchFiles: map[string]string{"extra.yaml": "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"},
		},
		// B: the client's X-DA-Base-Hash is base's copy; compared against
		// the branch's copy it is a 409 CONFLICT.
		"BaseHashOfBaseFile": {
			branchFiles: map[string]string{"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"},
			baseHash:    cfg.ComputeSourceHash([]byte(baseTenant)),
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := strandedRepoWith(t, map[string]string{"db-a.yaml": baseTenant}, tc.branchFiles)
			refs := snapshotRefs(t, dir)
			h := wrapWithRBACMiddleware(
				PutTenant(&Deps{Writer: gitops.NewWriter(dir, dir), WriteMode: WriteModeDirect}),
				permissiveRBACManager(t), rbac.PermWrite, TenantIDFromPath)
			do := func() *httptest.ResponseRecorder {
				req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
					bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"disable\"\n"))
				if tc.baseHash != "" {
					req.Header.Set(BaseHashHeader, tc.baseHash)
				}
				setRequestIdentity(req, "op@example.com")
				rec := httptest.NewRecorder()
				h.ServeHTTP(rec, req)
				return rec
			}

			rec := do()
			assertRetryable503(t, rec, CodeTreeNotOnBase, dir, refs)
			assertNoPathLeak(t, rec, dir)
			if got := strings.TrimSpace(revParseSymbolic(t, dir)); got != "main" {
				t.Fatalf("HEAD = %q after the 503, want main", got)
			}

			assertRetryLanded(t, do(), dir, refs)
		})
	}
}

func revParseSymbolic(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "symbolic-ref", "--short", "HEAD").Output()
	if err != nil {
		t.Fatalf("symbolic-ref: %v", err)
	}
	return string(out)
}
