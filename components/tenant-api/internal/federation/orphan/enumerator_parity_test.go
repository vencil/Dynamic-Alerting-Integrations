package orphan_test

// Cross-caller parity for #1680: all five conf.d enumerators agree on the
// tenant ID SET, whatever state a tenant's file is in.
//
// Before #1680 only handler.loadAllTenants read file content, and it silently
// dropped a tenant whose file was broken — so GET /api/v1/tenants answered
// 200 [] while account.ListTenantIDs (startup guard), both orphan-detector
// scans and the write-plane resolver all still counted the tenant. The
// callers now share one enumeration loop (confd.ListTenantFiles); the list
// handler reports a broken file as a degraded row instead of dropping it.
// This test is the regression guard for that disagreement, which is the
// issue's actual complaint — the classification detail is pinned in confd.
//
// It lives in an external test package so it can import handler (which
// imports orphan); the two unexported orphan scans are reached through
// export_test.go.

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"sort"
	"testing"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/orphan"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
)

const parityTenant = "acme"

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func uniqSorted(in []string) []string {
	set := map[string]struct{}{}
	for _, s := range in {
		set[s] = struct{}{}
	}
	return sortedKeys(set)
}

// listViaHTTP runs GET /api/v1/tenants through the real handler in open mode.
func listViaHTTP(t *testing.T, configDir string) []string {
	t.Helper()
	mgr, err := rbac.NewManager("", nil)
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}
	w := httptest.NewRecorder()
	handler.ListTenants(&handler.Deps{ConfigDir: configDir, RBAC: mgr})(w,
		httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("GET /api/v1/tenants status = %d, body=%s", w.Code, w.Body.String())
	}
	var rows []handler.TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &rows); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	ids := make([]string, 0, len(rows))
	for _, r := range rows {
		ids = append(ids, r.ID)
	}
	return uniqSorted(ids)
}

// resolvedSet is the write-plane resolver's (matchTenantFiles') answer: the
// id resolves to a file, or is ambiguous — either way the plane counts it.
func resolvedSet(t *testing.T, dir string) []string {
	t.Helper()
	_, err := confd.ResolveTenantFile(dir, parityTenant)
	switch {
	case err == nil, errors.Is(err, confd.ErrAmbiguousTenantFile):
		return []string{parityTenant}
	case errors.Is(err, confd.ErrTenantFileNotFound):
		return []string{}
	default:
		t.Fatalf("ResolveTenantFile(%q): %v", dir, err)
		return nil
	}
}

func TestConfdEnumeratorsAgreeOnTenantIDSet(t *testing.T) {
	t.Parallel()
	for _, state := range testutil.ConfdFileStates {
		t.Run(string(state), func(t *testing.T) {
			t.Parallel()
			configDir := t.TempDir()
			if !testutil.BuildConfdEntry(t, configDir, parityTenant, state) {
				t.Skipf("state %q cannot be built in this process (euid 0 ignores permission bits)", state)
			}
			subsetDir := confd.FederationSubsetDir(configDir)
			if err := os.Mkdir(subsetDir, 0o755); err != nil {
				t.Fatalf("mkdir: %v", err)
			}
			testutil.BuildConfdEntry(t, subsetDir, parityTenant, state)

			want := []string{parityTenant}
			if state == testutil.ConfdRealDir {
				want = []string{} // a directory is not a tenant file on any plane
			}

			accountIDs, err := account.ListTenantIDs(configDir)
			if err != nil {
				t.Fatalf("account.ListTenantIDs: %v", err)
			}
			known, err := orphan.ScanKnownTenants(configDir)
			if err != nil {
				t.Fatalf("scanKnownTenants: %v", err)
			}
			subsets, err := orphan.ScanSubsetTenants(configDir)
			if err != nil {
				t.Fatalf("scanSubsetTenants: %v", err)
			}

			got := map[string][]string{
				"handler GET /api/v1/tenants":           listViaHTTP(t, configDir),
				"account.ListTenantIDs":                 uniqSorted(accountIDs),
				"orphan.scanKnownTenants":               sortedKeys(known),
				"orphan.scanSubsetTenants":              uniqSorted(subsets),
				"confd.ResolveTenantFile (conf.d)":      resolvedSet(t, configDir),
				"confd.ResolveTenantFile (_federation)": resolvedSet(t, subsetDir),
			}
			for caller, ids := range got {
				if !reflect.DeepEqual(ids, want) {
					t.Errorf("%s = %v, want %v", caller, ids, want)
				}
			}
		})
	}
}
