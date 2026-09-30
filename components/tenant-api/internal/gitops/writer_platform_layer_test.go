package gitops

// #2208: the merge the write gate validates against now carries the root
// platform files' per-tenant `tenants:` layer (what /metrics serves). A key
// that layer sets is not the tenant's to fix: it must NEVER block a write to
// the tenant and never be reported as the tenant's error. It is reported as
// a notice that names the platform file instead.
//
// Every class of problem ValidateTenantKeys knows is exercised twice: once
// from the platform file (write goes through, notice names the file) and once
// from the tenant body (write refused — the must-fire control, so a check
// that stopped firing altogether cannot pass as "does not block").

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const platformLayerDefaults = "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 70\n  container_cpu: 90\n"

// platformLayerTenant is the body every platform-side case writes: valid on
// its own.
const platformLayerTenant = "tenants:\n  tx:\n    mysql_connections: \"90\"\n"

var platformLayerCases = []struct {
	name string
	// block is the tenant entry's body, as it appears under `tenants.tx:`
	// in either file (indented four spaces).
	block string
	// wantNotice is a fragment the platform-side notice must carry.
	wantNotice string
	// bodyBlocks: whether the same block in the tenant body is refused
	// (false only for the deprecated alias, which is itself a notice).
	bodyBlocks bool
}{
	{"unknown key", "    mysql_conections_typo: \"5\"\n", "mysql_conections_typo", true},
	{"invalid expires", "    mysql_threads_running:\n      default: \"75\"\n      expires: \"not-a-date\"\n", "not-a-date", true},
	{"dangling _critical", "    nosuch_critical: \"9\"\n", "nosuch_critical", true},
	{"unknown _profile", "    _profile: nope\n", "nope", true},
	{"bad version label", "    mysql_connections{version=\"v2\"}: \"3\"\n", "version", true},
	{"deprecated alias", "    mysql_cpu: \"7\"\n", "mysql_cpu", false},
}

func platformLayerRepo(t *testing.T, platform string) (*Writer, string) {
	t.Helper()
	files := map[string]string{
		"_defaults.yaml": platformLayerDefaults,
		"tx.yaml":        platformLayerTenant,
	}
	if platform != "" {
		files["_platform.yaml"] = platform
	}
	dir := seedTreeRepo(t, files)
	return NewWriter(dir, dir), dir
}

func TestPlatformLayerProblemsNeverBlockTheTenant(t *testing.T) {
	for _, tc := range platformLayerCases {
		t.Run(tc.name, func(t *testing.T) {
			w, dir := platformLayerRepo(t, "tenants:\n  tx:\n"+tc.block)

			errs, notices, err := w.DryRunValidate(context.Background(), "tx", platformLayerTenant)
			if err != nil {
				t.Fatalf("DryRunValidate: %v", err)
			}
			if len(errs) != 0 {
				t.Errorf("a platform-file problem blocks the tenant's dry-run: %q", errs)
			}
			assertPlatformNotice(t, "DryRunValidate", notices, tc.wantNotice)

			// A different, valid write — the platform problem must not block it.
			body := "tenants:\n  tx:\n    mysql_connections: \"91\"\n"
			notices, err = w.Write(context.Background(), "tx", "op@example.com", body)
			if err != nil {
				t.Fatalf("a platform-file problem blocks Writer.Write: %v", err)
			}
			assertPlatformNotice(t, "Write", notices, tc.wantNotice)
			got, rerr := os.ReadFile(filepath.Join(dir, "tx.yaml"))
			if rerr != nil || string(got) != body {
				t.Errorf("tx.yaml after the write = %q (%v), want the body", got, rerr)
			}
		})
	}
}

func TestPlatformLayerControlSameKeyInTheTenantBodyIsRefused(t *testing.T) {
	for _, tc := range platformLayerCases {
		t.Run(tc.name, func(t *testing.T) {
			w, _ := platformLayerRepo(t, "")
			body := "tenants:\n  tx:\n    mysql_connections: \"90\"\n" + tc.block
			errs, notices, err := w.DryRunValidate(context.Background(), "tx", body)
			if err != nil {
				t.Fatalf("DryRunValidate: %v", err)
			}
			if tc.bodyBlocks {
				if len(errs) == 0 {
					t.Fatalf("the must-fire control did not fire: %q in the tenant body is accepted (notices %q)", tc.block, notices)
				}
				if !strings.Contains(strings.Join(errs, "\n"), tc.wantNotice) {
					t.Errorf("errs %q do not name %q", errs, tc.wantNotice)
				}
			} else {
				if len(errs) != 0 {
					t.Fatalf("errs = %q, want the alias to stay advisory", errs)
				}
				if !strings.Contains(strings.Join(notices, "\n"), tc.wantNotice) {
					t.Fatalf("the must-fire control did not fire: no notice for %q in %q", tc.wantNotice, notices)
				}
			}
			for _, n := range append(append([]string{}, errs...), notices...) {
				if strings.Contains(n, "platform file") {
					t.Errorf("a tenant-body problem is attributed to a platform file: %q", n)
				}
			}
		})
	}
}

// TestPlatformLayerKeyTheTenantWritesIsJudgedAsTheTenants: a key the tenant
// body writes is the tenant's, whatever the platform file says about it; a
// platform value the tenant overrides produces no notice at all.
func TestPlatformLayerKeyTheTenantWritesIsJudgedAsTheTenants(t *testing.T) {
	w, _ := platformLayerRepo(t, "tenants:\n  tx:\n    mysql_conections_typo: \"5\"\n")
	body := "tenants:\n  tx:\n    mysql_conections_typo: null\n"
	errs, notices, err := w.DryRunValidate(context.Background(), "tx", body)
	if err != nil {
		t.Fatal(err)
	}
	if len(errs) == 0 {
		t.Errorf("the tenant's own unknown key is accepted because the platform file names it too")
	}
	for _, n := range notices {
		if strings.Contains(n, "platform file") {
			t.Errorf("notice for a platform key the tenant overrides: %q", n)
		}
	}
}

func assertPlatformNotice(t *testing.T, where string, notices []string, fragment string) {
	t.Helper()
	for _, n := range notices {
		if strings.Contains(n, "platform file _platform.yaml") && strings.Contains(n, fragment) &&
			strings.Contains(n, "tenants.tx") {
			if strings.Contains(n, "/") && strings.Contains(n, os.TempDir()) {
				t.Errorf("%s: notice leaks a server path: %q", where, n)
			}
			if strings.Contains(n, "#2208") {
				t.Errorf("%s: notice carries a ticket number: %q", where, n)
			}
			return
		}
	}
	t.Errorf("%s: no notice naming platform file _platform.yaml and %q: %q", where, fragment, notices)
}

// TestPlatformLayerElectingADefinedProfileIsNotFlagged: the platform-side
// `_profile` check knows the profiles the root platform files define (the
// set /metrics expands from), so electing one that exists says nothing.
func TestPlatformLayerElectingADefinedProfileIsNotFlagged(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{
		"_defaults.yaml": platformLayerDefaults,
		"_profiles.yaml": "profiles:\n  std:\n    mysql_connections: 60\n",
		"_platform.yaml": "tenants:\n  tx:\n    _profile: std\n",
		"tx.yaml":        platformLayerTenant,
	})
	errs, notices, err := NewWriter(dir, dir).DryRunValidate(context.Background(), "tx", platformLayerTenant)
	if err != nil {
		t.Fatal(err)
	}
	if len(errs) != 0 || len(notices) != 0 {
		t.Errorf("errs %q, notices %q: want none for a platform-elected profile that exists", errs, notices)
	}
}
