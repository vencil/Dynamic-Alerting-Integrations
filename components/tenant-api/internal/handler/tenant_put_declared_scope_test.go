package handler

// #3062: the write scope of every tenant a PUT body declares besides {id},
// on the metadata axis (environment / domain, --rbac-metadata-write-scope-
// enforce) — the axis whose value lives inside the very file the PUT writes.
//
// Each declared tenant is judged twice, as {id} is: on its metadata as the
// file holds it now (onDisk) and as the body proposes it (proposed). A
// declared section lives in the file {id} lives in, so "on disk" is read
// from THAT file, not from a file named after the declared id (which a
// shared section does not have).
//
// The rows isolate each half: proposed_* keep the disk in scope and move the
// body out, on_disk_* keep the body in scope with the disk out. Dropping
// either judgement turns its rows green-to-red (measured). Both the
// environment and the domain half are covered.
//
// The PR-mode rows (TestPutTenant_DeclaredScopeOnFreshBase) are #2857 for a
// declared tenant: the pod's local tree may lag the base, so the declared
// tenant is judged again on the fresh base the branch is cut from.

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func readFileT(t *testing.T, dir, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// metaDeclOut is the tenant sharing tenant-meta-in.yaml with metaEnfTenantIn.
const metaDeclOut = "tenant-meta-shared"

// metaSection is one tenant's section (no `tenants:` line).
func metaSection(id, env, domain, mode string) string {
	return strings.TrimPrefix(tenantYAMLWithMeta(id, env, domain, mode), "tenants:\n")
}

// metaShared is the shared file: In in scope, then Out.
func metaShared(inMode, outEnv, outDomain, outMode string) string {
	return "tenants:\n" + metaSection(metaEnfTenantIn, metaEnfScopedEnv, metaEnfScopedDomain, inMode) +
		metaSection(metaDeclOut, outEnv, outDomain, outMode)
}

func TestPutTenant_DeclaredTenantMetadataScope(t *testing.T) {
	t.Parallel()
	inOnly := "tenants:\n" + metaSection(metaEnfTenantIn, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode)
	for _, tc := range []struct {
		name string
		// Out on disk, and the body.
		outEnv, outDomain string
		body              string
		want              string // the file after a 200; "" for the 403
	}{
		{
			// Control: both in scope on both halves → the write lands. This
			// row is what a disk read that cannot see Out (an unlabeled Out,
			// which enforce denies) would turn into a false 403.
			name: "both_in_scope", outEnv: metaEnfScopedEnv, outDomain: metaEnfScopedDomain,
			body: metaShared(metaEnfPutMode, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode),
			want: metaShared(metaEnfPutMode, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode),
		},
		{
			name: "proposed_environment_out", outEnv: metaEnfScopedEnv, outDomain: metaEnfScopedDomain,
			body: metaShared(metaEnfPutMode, metaEnfOutsideEnv, metaEnfScopedDomain, metaEnfPutMode),
		},
		{
			name: "proposed_domain_out", outEnv: metaEnfScopedEnv, outDomain: metaEnfScopedDomain,
			body: metaShared(metaEnfPutMode, metaEnfScopedEnv, metaEnfOutsideDomain, metaEnfPutMode),
		},
		{
			name: "on_disk_environment_out", outEnv: metaEnfOutsideEnv, outDomain: metaEnfScopedDomain,
			body: metaShared(metaEnfPutMode, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode),
		},
		{
			name: "on_disk_domain_out", outEnv: metaEnfScopedEnv, outDomain: metaEnfOutsideDomain,
			body: metaShared(metaEnfPutMode, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode),
		},
		{
			// Declaring Out exactly as the file holds it is still declaring
			// it: no "unchanged, so allowed" shortcut.
			name: "unchanged_out_still_denied", outEnv: metaEnfOutsideEnv, outDomain: metaEnfScopedDomain,
			body: metaShared(metaEnfPutMode, metaEnfOutsideEnv, metaEnfScopedDomain, metaEnfSeedMode),
		},
		{
			// Leaving the out-of-scope Out out: the write lands, Out kept.
			name: "omitted_out_kept", outEnv: metaEnfOutsideEnv, outDomain: metaEnfScopedDomain,
			body: inOnly,
			want: metaShared(metaEnfPutMode, metaEnfOutsideEnv, metaEnfScopedDomain, metaEnfSeedMode),
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			file := metaShared(metaEnfSeedMode, tc.outEnv, tc.outDomain, metaEnfSeedMode)
			f := newMetaEnfFixtureWith(t, true, map[string]string{metaEnfTenantIn + ".yaml": file})
			w := f.putTenant(t, metaEnfTenantIn, tc.body, nil)
			got := readFileT(t, f.configDir, metaEnfTenantIn+".yaml")
			if tc.want == "" {
				if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "declares a tenant section you may not write") {
					t.Fatalf("status = %d, want the declared-tenant 403; body=%s", w.Code, w.Body.String())
				}
				if strings.Contains(w.Body.String(), metaDeclOut) {
					t.Errorf("the refusal names Out: %s", w.Body.String())
				}
				if n := f.writes.Load(); n != 0 || got != file {
					t.Errorf("refused PUT wrote: commits=%d file:\n%s", n, got)
				}
				return
			}
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			if got != tc.want {
				t.Errorf("file:\n%s\nwant:\n%s", got, tc.want)
			}
		})
	}
}

// PR mode: db-b shares db-a.yaml. The caller's rule (postStateRBAC) writes
// production only.
const (
	declFreshDB = "    mysql_connections: '80'\n"
	// declFreshLocal / declFreshMoved: db-b production / dev, db-a production.
	declFreshProd = "tenants:\n  db-a:\n    _metadata:\n      environment: production\n" + declFreshDB +
		"  db-b:\n    _metadata:\n      environment: production\n" + declFreshDB
	declFreshDev = "tenants:\n  db-a:\n    _metadata:\n      environment: production\n" + declFreshDB +
		"  db-b:\n    _metadata:\n      environment: dev\n" + declFreshDB
	// The body: both declared, db-b changed and proposed production.
	declFreshBody = "tenants:\n  db-a:\n    _metadata:\n      environment: production\n" + declFreshDB +
		"  db-b:\n    _metadata:\n      environment: production\n    mysql_connections: '90'\n"
)

func TestPutTenant_DeclaredScopeOnFreshBase(t *testing.T) {
	// stale_local_allows: db-b moved to dev on the base, the pod's tree still
	// says production. Judged on the local tree alone the write would go
	// through; the verdict on the fresh base refuses it: 403 with the
	// declared-tenant text, no PR, no branch, origin unmoved.
	t.Run("stale_local_allows/enforce", func(t *testing.T) {
		f := newStaleFixture(t, map[string]string{"_defaults.yaml": freshScopeDefaults, "db-a.yaml": declFreshProd},
			map[string]string{"db-a.yaml": declFreshDev})
		mgr := newRBACManager(t, postStateRBAC)
		mgr.EnableMetadataWriteScopeEnforce()
		f.deps.RBAC = mgr
		before := gitRev(t, f.bare, "main")
		w := putStale(t, f, "db-a", declFreshBody)
		if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "declares a tenant section you may not write") {
			t.Fatalf("status %d, body %s; want the declared-tenant 403", w.Code, w.Body.String())
		}
		if strings.Contains(w.Body.String(), "db-b") {
			t.Errorf("the refusal names db-b: %s", w.Body.String())
		}
		if f.prOpened {
			t.Error("a PR was opened")
		}
		if b := f.tenantBranches(t); b != "" {
			t.Errorf("branch left behind: %s", b)
		}
		if got := gitRev(t, f.bare, "main"); got != before {
			t.Errorf("origin main moved: %s → %s", before, got)
		}
	})
	// In shadow the PR is opened and the base's value is counted once.
	t.Run("stale_local_allows/shadow", func(t *testing.T) {
		f := newStaleFixture(t, map[string]string{"_defaults.yaml": freshScopeDefaults, "db-a.yaml": declFreshProd},
			map[string]string{"db-a.yaml": declFreshDev})
		mgr := newRBACManager(t, postStateRBAC)
		audit := &countingScopeAuditor{n: map[string]int{}}
		mgr.SetScopeAuditor(audit)
		f.deps.RBAC = mgr
		if w := putStale(t, f, "db-a", declFreshBody); w.Code != http.StatusOK || !f.prOpened {
			t.Fatalf("status %d, PR opened %v; want the PR opened in shadow: %s", w.Code, f.prOpened, w.Body.String())
		}
		if got := audit.n["metadata_write"]; got != 1 {
			t.Errorf("would-deny{axis=metadata_write} = %d, want 1 (db-b on the base)", got)
		}
	})
	// stale_local_refuses: the reverse — db-b is dev on the pod's tree and
	// production on the base. The local check refuses early, as it does for
	// {id} (#2857: the local tree may only refuse early, never let through
	// what the base would refuse). The caller retries once the pod has
	// synced; a 403 here is not the base's verdict, only the local one.
	t.Run("stale_local_refuses/enforce", func(t *testing.T) {
		f := newStaleFixture(t, map[string]string{"_defaults.yaml": freshScopeDefaults, "db-a.yaml": declFreshDev},
			map[string]string{"db-a.yaml": declFreshProd})
		mgr := newRBACManager(t, postStateRBAC)
		mgr.EnableMetadataWriteScopeEnforce()
		f.deps.RBAC = mgr
		w := putStale(t, f, "db-a", declFreshBody)
		if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "declares a tenant section you may not write") {
			t.Fatalf("status %d, body %s; want the declared-tenant 403 from the local check", w.Code, w.Body.String())
		}
		if f.prOpened {
			t.Error("a PR was opened")
		}
		if b := f.tenantBranches(t); b != "" {
			t.Errorf("branch left behind: %s", b)
		}
	})
}
