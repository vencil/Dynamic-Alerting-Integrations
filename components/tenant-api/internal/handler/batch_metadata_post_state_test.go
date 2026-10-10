package handler

// #2830: once tenant-api reads `_metadata` as threshold-exporter does, two
// scalar keys a batch op may patch decide a tenant's environment/domain —
// `_profile` (its profile's `_metadata`) and `_metadata` in its string form.
// The batch write gate therefore authorizes such an op against the metadata
// the tenant will have once it is written (gateBatchOpPostState), as PUT does
// for its body (RequireOrgWriteProposed): under
// --rbac-metadata-write-scope-enforce a production-scoped caller cannot move
// a tenant to dev by batch, in either write mode; in shadow nothing changes.

import (
	"encoding/json"
	"os"
	"path/filepath"
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

// postStateBatch runs ops against a fresh postStateTree in mode and returns
// the per-op results and whether anything was written (a PR opened in PR
// mode, HEAD moved in direct mode).
func postStateBatch(t *testing.T, mode WriteMode, enforce bool, ops string) (results []BatchResult, wrote bool) {
	t.Helper()
	dir := seedGitTree(t, postStateTree())
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
		for name, patch := range map[string]string{
			"_profile":        `{"_profile":"devprof"}`,
			"string_metadata": `{"_metadata":"environment: dev\n"}`,
		} {
			t.Run(string(mode)+"/"+name, func(t *testing.T) {
				results, wrote := postStateBatch(t, mode, true, `[{"tenant_id":"db-a","patch":`+patch+`}]`)
				if len(results) != 1 || results[0].Status != "error" {
					t.Errorf("results = %+v, want one refused op", results)
				}
				if wrote {
					t.Error("a refused op was written")
				}
			})
		}
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

// An op that keeps the tenant in the caller's environment goes through, and
// in PR mode each op is judged with the same tenant's earlier ops stacked
// under it: the second op alone would move db-a to dev, but the first one's
// string `_metadata` wins over any profile, so the result stays production.
func TestBatchInScopeMetadataEditsWritableUnderEnforce(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			results, wrote := postStateBatch(t, mode, true, `[
				{"tenant_id":"db-a","patch":{"_profile":"prodalt"}},
				{"tenant_id":"db-a","patch":{"_metadata":"environment: production\n"}},
				{"tenant_id":"db-a","patch":{"_profile":"devprof"}}]`)
			if len(results) != 3 || !wrote {
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
		[]byte("tenants:\n  db-a:\n    _profile: devprof\n    _metadata: \"environment: production\\n\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	op := BatchOperation{TenantID: "db-a", Patch: map[string]string{"_profile": "prodalt"}}
	if env, domain := postStateScopeMeta(dir, nil, op)("db-a"); env != "production" || domain != "" {
		t.Errorf("post-state = (%q, %q), want (production, \"\"): the tenant's own string _metadata wins over the profile", env, domain)
	}
}
