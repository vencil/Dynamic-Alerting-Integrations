package gitops

// #2518: a threshold written as YAML null is no write. Whether a key so
// written is read — a body `typo_key: null`, or a tenant value for a key the
// ROOT `_defaults.yaml` writes as null (which declares nothing: da-guard
// root_default_null_undeclared) — is the shared key verdict's to judge since
// ADR-038 §11, not the write gate's key validation: the gate is silent about
// both, as about any key no reader reads.

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
			t.Errorf("%s: errs = %q, want none: the key verdict judges whether it is read", name, errs)
		}
	}
}
