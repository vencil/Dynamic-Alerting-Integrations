package gitops

// #1385: the merge the write gate validates against now expands the profile
// the tenant elects, as /metrics does. The profile is platform-owned: a
// problem in the part of it that reaches the tenant must NEVER block a write
// to the tenant. It is a notice naming the file and the profile. The
// must-fire controls (the same block in the tenant body is refused) are
// TestPlatformLayerControlSameKeyInTheTenantBodyIsRefused's cases, reused
// here by name.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const profileLayerTenant = "tenants:\n  tx:\n    _profile: strict\n"

func TestProfileLayerProblemsNeverBlockTheTenant(t *testing.T) {
	for _, tc := range platformLayerCases {
		if !tc.bodyBlocks || tc.name == "unknown _profile" {
			// A profile body cannot elect a profile (the tenant's
			// `_profile` is always set, so profileFill never fills it),
			// and a deprecated alias in a profile is canonicalized silently
			// (ApplyProfiles' rule).
			continue
		}
		t.Run(tc.name, func(t *testing.T) {
			// `profiles.strict.` nests at the depth of `tenants.tx.`, so
			// the case's block goes in as is.
			dir := seedTreeRepo(t, map[string]string{
				"_defaults.yaml": platformLayerDefaults,
				"_profiles.yaml": "profiles:\n  strict:\n" + tc.block,
				"tx.yaml":        profileLayerTenant,
			})
			w := NewWriter(dir, dir)
			errs, notices, err := w.DryRunValidate("tx", profileLayerTenant)
			if err != nil {
				t.Fatalf("DryRunValidate: %v", err)
			}
			if len(errs) != 0 {
				t.Errorf("a profile problem blocks the tenant's dry-run: %q", errs)
			}
			assertProfileNotice(t, "DryRunValidate", notices, tc.wantNotice)

			body := "tenants:\n  tx:\n    _profile: strict\n    mysql_connections: \"91\"\n"
			notices, err = w.Write(context.Background(), "tx", "op@example.com", body)
			if err != nil {
				t.Fatalf("a profile problem blocks Writer.Write: %v", err)
			}
			assertProfileNotice(t, "Write", notices, tc.wantNotice)
			if got, rerr := os.ReadFile(filepath.Join(dir, "tx.yaml")); rerr != nil || string(got) != body {
				t.Errorf("tx.yaml after the write = %q (%v), want the body", got, rerr)
			}
		})
	}
}

// Before #1385 every `_profile` the tenant wrote was refused as unknown —
// the merge carried no profiles. A defined one is accepted now; one no root
// platform file defines is refused exactly as before.
func TestProfileLayerTenantElectsAProfile(t *testing.T) {
	for _, tc := range []struct {
		name, where, profiles, body string
		wantErr                     string
	}{
		{"defined in _profiles.yaml", "_profiles.yaml", "profiles:\n  strict:\n    mysql_connections: \"55\"\n", profileLayerTenant, ""},
		{"defined in the root carrier", "_defaults.yaml", platformLayerDefaults + "profiles:\n  strict:\n    mysql_connections: \"55\"\n", profileLayerTenant, ""},
		{"defined nowhere", "", "", profileLayerTenant, `WARN: tenant=tx: _profile references unknown profile "strict"`},
		{"only in a nested file", "sub/_profiles.yaml", "profiles:\n  strict:\n    mysql_connections: \"55\"\n", profileLayerTenant, `WARN: tenant=tx: _profile references unknown profile "strict"`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			files := map[string]string{"_defaults.yaml": platformLayerDefaults, "tx.yaml": platformLayerTenant}
			if tc.where != "" {
				files[tc.where] = tc.profiles
			}
			dir := seedTreeRepo(t, files)
			errs, notices, err := NewWriter(dir, dir).DryRunValidate("tx", tc.body)
			if err != nil {
				t.Fatal(err)
			}
			if tc.wantErr == "" {
				if len(errs) != 0 || len(notices) != 0 {
					t.Errorf("errs %q notices %q, want none", errs, notices)
				}
				return
			}
			if len(errs) != 1 || errs[0] != tc.wantErr {
				t.Errorf("errs %q, want [%q]", errs, tc.wantErr)
			}
		})
	}
}

func assertProfileNotice(t *testing.T, where string, notices []string, fragment string) {
	t.Helper()
	for _, n := range notices {
		if strings.Contains(n, `platform file _profiles.yaml, profile "strict"`) && strings.Contains(n, fragment) &&
			strings.Contains(n, "does not block writing it") {
			if strings.Contains(n, os.TempDir()) {
				t.Errorf("%s: notice leaks a server path: %q", where, n)
			}
			return
		}
	}
	t.Errorf("%s: no notice naming _profiles.yaml, profile strict and %q: %q", where, fragment, notices)
}
