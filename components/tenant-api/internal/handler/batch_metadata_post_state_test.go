package handler

// #2830: once tenant-api reads `_metadata` as threshold-exporter does, a
// scalar key a batch op may patch decides a tenant's environment/domain —
// `_profile` (its profile's `_metadata`). `_metadata` itself a batch cannot
// set (#2859, TestBatchMetadataPatchRefused). The batch write gate therefore authorizes such an op against the metadata
// the tenant will have once it is written (gateBatchOpPostState), as PUT does
// for its body (RequireOrgWriteProposed): under
// --rbac-metadata-write-scope-enforce a production-scoped caller cannot move
// a tenant to dev by batch, in either write mode; in shadow nothing changes.

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
)

const postStateRBAC = `groups:
  - name: admins
    tenants: ["*"]
    environments: ["production"]
    permissions: [read, write]
`

// postStateTree: db-a is production through its profile alone; prodalt is a
// second production profile, devprof a dev one.
func postStateTree() map[string]string {
	return map[string]string{
		"_profiles.yaml": "profiles:\n" +
			"  prodprof:\n    _metadata:\n      environment: production\n" +
			"  prodalt:\n    _metadata:\n      environment: production\n      domain: finance\n" +
			"  devprof:\n    _metadata:\n      environment: dev\n",
		"db-a.yaml": "tenants:\n  db-a:\n    _profile: prodprof\n",
	}
}

// ownProductionMetadata is db-a with its own `_metadata`, which wins over any
// profile's.
const ownProductionMetadata = "tenants:\n  db-a:\n    _profile: prodprof\n    _metadata:\n      environment: production\n"

// postStateBatch runs ops against a fresh postStateTree in mode and returns
// the per-op results and whether anything was written (a PR opened in PR
// mode, HEAD moved in direct mode).
func postStateBatch(t *testing.T, mode WriteMode, enforce bool, ops string) (results []BatchResult, wrote bool) {
	t.Helper()
	return postStateBatchOn(t, postStateTree(), mode, enforce, ops)
}

// postStateBatchOn is postStateBatch over tree.
func postStateBatchOn(t *testing.T, tree map[string]string, mode WriteMode, enforce bool, ops string) (results []BatchResult, wrote bool) {
	t.Helper()
	dir := seedGitTree(t, tree)
	mgr := newRBACManager(t, postStateRBAC)
	if enforce {
		mgr.EnableMetadataWriteScopeEnforce()
	}
	if env, _ := WriteScopeMeta(dir)("db-a"); env != "production" {
		t.Fatalf("precondition: db-a environment on disk = %q, want production", env)
	}
	prCalls := 0
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: mgr, WriteMode: mode}
	if mode == WriteModePR {
		d.PRClient = &mockPlatformClient{providerName: "github",
			createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
				prCalls++
				return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
			}}
		d.PRTracker = &mockPlatformTracker{}
	}
	before := gitHead(t, dir)
	w := postTenantBatch(t, d, ops)
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("status %d, unmarshal: %v; body: %s", w.Code, err, w.Body.String())
	}
	if mode == WriteModePR {
		return resp.Results, prCalls > 0
	}
	return resp.Results, gitHead(t, dir) != before
}

func TestBatchRelabelOutOfScopeRefusedUnderEnforce(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			results, wrote := postStateBatch(t, mode, true, `[{"tenant_id":"db-a","patch":{"_profile":"devprof"}}]`)
			if len(results) != 1 || results[0].Status != "error" ||
				!strings.Contains(results[0].Message, "the tenant metadata this op proposes") {
				t.Errorf("results = %+v, want one op refused by the post-state check", results)
			}
			if wrote {
				t.Error("a refused op was written")
			}
		})
	}
}

// #2859: `_metadata` is a mapping and a patch value a string, so a batch op
// that sets it is refused up front — 400 naming the field, nothing written —
// in shadow too, whatever the value would place the tenant in.
func TestBatchMetadataPatchRefused(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			dir := seedGitTree(t, postStateTree())
			d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: newRBACManager(t, postStateRBAC), WriteMode: mode}
			prOpened := false
			if mode == WriteModePR {
				d.PRClient = &mockPlatformClient{providerName: "github",
					createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
						prOpened = true
						return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
					}}
				d.PRTracker = &mockPlatformTracker{}
			}
			before := gitHead(t, dir)
			w := postTenantBatch(t, d, `[{"tenant_id":"db-a","patch":{"mysql_connections":"90","_metadata":"environment: production\n"}}]`)
			if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), `operations[0].patch[\"_metadata\"]`) ||
				!strings.Contains(w.Body.String(), "batch cannot set it") {
				t.Errorf("status %d, body %s; want 400 naming operations[0].patch[\"_metadata\"]", w.Code, w.Body.String())
			}
			if prOpened || gitHead(t, dir) != before {
				t.Error("a refused batch was written")
			}
		})
	}
}

func TestBatchRelabelStaysWritableInShadow(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			results, wrote := postStateBatch(t, mode, false, `[{"tenant_id":"db-a","patch":{"_profile":"devprof"}}]`)
			if len(results) != 1 || results[0].Status == "error" || !wrote {
				t.Errorf("results = %+v, wrote = %v; want the op written in shadow", results, wrote)
			}
		})
	}
}

// An op that keeps the tenant in the caller's environment goes through: the
// second op would move db-a to dev through its profile alone, but db-a's own
// `_metadata` on disk wins over any profile, so it stays production.
func TestBatchInScopeMetadataEditsWritableUnderEnforce(t *testing.T) {
	tree := postStateTree()
	tree["db-a.yaml"] = ownProductionMetadata
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			results, wrote := postStateBatchOn(t, tree, mode, true, `[
				{"tenant_id":"db-a","patch":{"_profile":"prodalt"}},
				{"tenant_id":"db-a","patch":{"_profile":"devprof"}}]`)
			if len(results) != 2 || !wrote {
				t.Fatalf("results = %+v, wrote = %v", results, wrote)
			}
			for i, r := range results {
				if r.Status == "error" {
					t.Errorf("op %d refused: %+v", i, r)
				}
			}
		})
	}
}

// The post-state read is the tenant's block on disk with the op laid over it,
// read over the root platform layer: a key the op does not touch is kept.
func TestPostStateScopeMetaKeepsUntouchedKeys(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, postStateTree())
	if err := os.WriteFile(filepath.Join(dir, "db-a.yaml"),
		[]byte("tenants:\n  db-a:\n    _profile: devprof\n    _metadata:\n      environment: production\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	op := BatchOperation{TenantID: "db-a", Patch: map[string]string{"_profile": "prodalt"}}
	if env, domain := postStateScopeMeta(dir, nil, op)("db-a"); env != "production" || domain != "" {
		t.Errorf("post-state = (%q, %q), want (production, \"\"): the tenant's own _metadata wins over the profile", env, domain)
	}
}

// PR mode re-judges the metadata write scope on the fresh base the branch is
// cut from: the pod's local tree (synced at pod start) still has db-a's own
// `_metadata`, which wins over any profile, so the per-op check reads
// production; on the base that line is gone, and `_profile: devprof` would
// land db-a in dev. Under enforce the whole batch is refused (403 FORBIDDEN,
// no PR, no branch, origin unmoved); in shadow the PR is opened as before.
func TestBatchPRModeScopeJudgedOnFreshBase(t *testing.T) {
	profiles := postStateTree()["_profiles.yaml"]
	local := map[string]string{"_profiles.yaml": profiles,
		"db-a.yaml": ownProductionMetadata}
	origin := map[string]string{"db-a.yaml": "tenants:\n  db-a:\n    _profile: prodprof\n"}
	const ops = `[{"tenant_id":"db-a","patch":{"_profile":"devprof"}}]`

	t.Run("enforce", func(t *testing.T) {
		f := newStaleFixture(t, local, origin)
		mgr := newRBACManager(t, postStateRBAC)
		mgr.EnableMetadataWriteScopeEnforce()
		f.deps.RBAC = mgr
		if env, _ := WriteScopeMeta(f.dir)("db-a"); env != "production" {
			t.Fatalf("precondition: db-a on the local tree = %q, want production", env)
		}
		before := gitRev(t, f.bare, "main")
		w := postTenantBatch(t, f.deps, ops)
		var env map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &env)
		msg, _ := env["error"].(string)
		if w.Code != http.StatusForbidden || env["code"] != CodeForbidden || env["tenant_id"] != "db-a" ||
			env["operation"] != float64(0) || !strings.Contains(msg, "latest base branch") ||
			!strings.Contains(msg, "Nothing in this batch was written") {
			t.Errorf("status %d, body %s; want 403 %s naming operations[0] (db-a) on the latest base", w.Code, w.Body.String(), CodeForbidden)
		}
		if f.prOpened {
			t.Error("a PR was opened")
		}
		if b := f.batchBranches(t); b != "" {
			t.Errorf("batch branch left behind: %s", b)
		}
		if got := gitRev(t, f.bare, "main"); got != before {
			t.Errorf("origin main moved: %s → %s", before, got)
		}
	})
	t.Run("shadow", func(t *testing.T) {
		f := newStaleFixture(t, local, origin)
		f.deps.RBAC = newRBACManager(t, postStateRBAC)
		if w := postTenantBatch(t, f.deps, ops); w.Code != http.StatusOK || !f.prOpened {
			t.Errorf("status %d, PR opened %v; want the PR opened in shadow: %s", w.Code, f.prOpened, w.Body.String())
		}
	})
}

// countingScopeAuditor counts would-deny observations per axis.
type countingScopeAuditor struct{ n map[string]int }

func (a *countingScopeAuditor) IncWouldDeny(axis string) { a.n[axis]++ }

// No false would-deny in shadow: an op whose profile alone reads dev, on a
// tenant whose own `_metadata` keeps it in production, is counted neither by
// the per-op check nor by the fresh-base check of the post-checkout pass
// (FreshMerge, #2857).
func TestBatchPRModeShadowCountsNoFalseWouldDeny(t *testing.T) {
	tree := postStateTree()
	tree["db-a.yaml"] = ownProductionMetadata
	dir := seedGitTree(t, tree)
	mgr := newRBACManager(t, postStateRBAC)
	audit := &countingScopeAuditor{n: map[string]int{}}
	mgr.SetScopeAuditor(audit)
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: mgr, WriteMode: WriteModePR,
		PRClient: &mockPlatformClient{providerName: "github"}, PRTracker: &mockPlatformTracker{}}
	w := postTenantBatch(t, d, `[
		{"tenant_id":"db-a","patch":{"_profile":"prodalt"}},
		{"tenant_id":"db-a","patch":{"_profile":"devprof"}}]`)
	if w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if got := audit.n["metadata_write"]; got != 0 {
		t.Errorf("would-deny{axis=metadata_write} = %d, want 0: nothing in this batch leaves production", got)
	}
}
