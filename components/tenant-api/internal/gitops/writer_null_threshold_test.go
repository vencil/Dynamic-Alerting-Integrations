package gitops

// #2518: a threshold written as YAML null is no write. Two pins on the write
// gate's key validation (ValidateTenantKeys over MergeParsedTenantWithRootDefaults):
//
//   - the gate decodes the BODY with a plain yaml.Unmarshal, so a body key
//     written as null still counts as written there and is still validated
//     — `typo_key: null` keeps its unknown-key error (GET, which decodes
//     through ParseConfigFile, drops such a key; the gate stays stricter);
//   - a ROOT `_defaults.yaml` key written as null no longer declares the key,
//     so a tenant body setting a value for it gets the unknown-key error —
//     the same key /metrics does not serve (da-guard:
//     root_default_null_undeclared). The control is the root with a number.

import (
	"context"
	"strings"
	"testing"
)

func nullGateErrs(t *testing.T, root, body string) []string {
	t.Helper()
	dir := seedTreeRepo(t, map[string]string{
		"_defaults.yaml": root,
		"tx.yaml":        "tenants:\n  tx:\n    mysql_slow_queries: \"5\"\n",
	})
	errs, _, err := NewWriter(dir, dir).DryRunValidate(context.Background(), "tx", body)
	if err != nil {
		t.Fatalf("DryRunValidate: %v", err)
	}
	return errs
}

func TestWriteGate_BodyNullKeyIsStillValidated(t *testing.T) {
	errs := nullGateErrs(t, "defaults:\n  mysql_slow_queries: 5\n",
		"tenants:\n  tx:\n    typo_key: null\n")
	if !strings.Contains(strings.Join(errs, "\n"), "typo_key") {
		t.Errorf("errs = %q, want the unknown-key error for typo_key (written as null)", errs)
	}
}

func TestWriteGate_KeyTheRootWritesAsNullIsUndeclared(t *testing.T) {
	body := "tenants:\n  tx:\n    mysql_connections: \"70\"\n"
	errs := nullGateErrs(t, "defaults:\n  mysql_connections: null\n  mysql_slow_queries: 5\n", body)
	if !strings.Contains(strings.Join(errs, "\n"), "mysql_connections") {
		t.Errorf("errs = %q, want mysql_connections refused: the root writes it as null, which declares nothing", errs)
	}
	// Control: a number at the root declares it.
	if errs := nullGateErrs(t, "defaults:\n  mysql_connections: 30\n  mysql_slow_queries: 5\n", body); len(errs) != 0 {
		t.Errorf("errs = %q with the root declaring mysql_connections, want none", errs)
	}
}
