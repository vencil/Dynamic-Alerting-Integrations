package gitops

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// TestValidate_RootMaxMetricsPerTenantDoesNotGateWrites (#2369): the merge
// core validate() runs now carries the root max_metrics_per_tenant (so GET
// cuts where /metrics cuts). The cap is read-side only: validate() must
// return the same errors and notices under a cap of 1 as under no cap — a
// body with more keys than the cap is not refused, and an unknown key is
// still refused.
func TestValidate_RootMaxMetricsPerTenantDoesNotGateWrites(t *testing.T) {
	t.Parallel()
	const defaults = "defaults:\n  m1_x: 1\n  m2_x: 1\n  m3_x: 1\n"
	bodies := map[string]struct {
		body     string
		wantErrs bool
	}{
		"clean, over cap": {"tenants:\n  tx:\n    m1_x: \"2\"\n    m2_x: \"3\"\n    m3_x: \"4\"\n", false},
		"unknown key":     {"tenants:\n  tx:\n    m1_x: \"2\"\n    not_a_default_x: \"3\"\n", true},
	}
	run := func(t *testing.T, capLine, body string) (errs, notices []string) {
		t.Helper()
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(capLine+defaults), 0o644); err != nil {
			t.Fatal(err)
		}
		return validate(dir, "tx", filepath.Join(dir, "tx.yaml"), body)
	}
	for name, tc := range bodies {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			cErrs, cNotices := run(t, "max_metrics_per_tenant: 1\n", tc.body)
			uErrs, uNotices := run(t, "", tc.body)
			if !reflect.DeepEqual(cErrs, uErrs) || !reflect.DeepEqual(cNotices, uNotices) {
				t.Errorf("cap changed validate()\n capped:   %q / %q\n uncapped: %q / %q", cErrs, cNotices, uErrs, uNotices)
			}
			if got := len(cErrs) > 0; got != tc.wantErrs {
				t.Errorf("errors present = %v, want %v: %q", got, tc.wantErrs, cErrs)
			}
		})
	}
}
