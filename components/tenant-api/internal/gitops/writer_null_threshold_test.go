package gitops

// #2518: a threshold written as YAML null is no write. Whether a key is read
// is the shared key verdict's to judge since ADR-038 §11, not the write
// gate's key validation, which is silent about both cases below:
//
//   - a tenant value for a key the ROOT `_defaults.yaml` writes as null
//     (which declares nothing: da-guard root_default_null_undeclared) — the
//     verdict refuses it (error, no_row);
//   - a body `typo_key: null` — no reader reads a null, so the verdict
//     reports nothing and the write is taken: a deliberate relaxation, the
//     key writes no value (the gate used to refuse it as an unknown key).

import (
	"context"
	"testing"
)

func nullGateErrs(t *testing.T, root, body string) []string {
	t.Helper()
	dir := seedTreeRepo(t, map[string]string{
		"_defaults.yaml": root,
		"tx.yaml":        "tenants:\n  tx:\n    mysql_slow_queries: \"5\"\n",
	})
	errs, _, err := previewDryRun(context.Background(), newTestWriter(dir, dir), "tx", body)
	if err != nil {
		t.Fatalf("DryRunValidate: %v", err)
	}
	return errs
}

func TestWriteGate_NullKeysAreTheKeyVerdicts(t *testing.T) {
	for name, tc := range map[string]struct{ root, body string }{
		"body key written as null": {"defaults:\n  mysql_slow_queries: 5\n",
			"tenants:\n  tx:\n    typo_key: null\n"},
		"key the root writes as null": {"defaults:\n  mysql_connections: null\n  mysql_slow_queries: 5\n",
			"tenants:\n  tx:\n    mysql_connections: \"70\"\n"},
	} {
		if errs := nullGateErrs(t, tc.root, tc.body); len(errs) != 0 {
			t.Errorf("%s: errs = %q, want none: whether it is read is the key verdict's", name, errs)
		}
	}
}
