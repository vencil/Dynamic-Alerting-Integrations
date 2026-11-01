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
	"strings"
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

// #3267: the refusal of a value the exporter cannot read is not a refusal of
// a null. `k: ""` is a written value the exporter refuses ("unknown value");
// `k: null` writes nothing (#2518) — the write gate's plain decode holds it
// as "" too, so refusedWrites re-judges on the exporter's decode.
func TestWriteGate_RefusedEmptyIsNotNull(t *testing.T) {
	root := "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 5\n"
	for _, body := range []string{
		"tenants:\n  tx:\n    mysql_connections: null\n",
		"tenants:\n  tx:\n    mysql_connections:\n      default: null\n",
	} {
		if errs := nullGateErrs(t, root, body); len(errs) != 0 {
			t.Errorf("%q: errs = %q, want none: a null writes nothing", body, errs)
		}
		if errs := validateBodyOnly("tx", body); len(errs) != 0 {
			t.Errorf("%q: body-only errs = %q, want none", body, errs)
		}
	}
	empty := "tenants:\n  tx:\n    mysql_connections: \"\"\n"
	if errs := nullGateErrs(t, root, empty); len(errs) == 0 || !strings.Contains(strings.Join(errs, "; "), "is not a threshold the exporter reads") {
		t.Errorf("written \"\": errs = %q, want the refusal", errs)
	}
	if errs := validateBodyOnly("tx", empty); len(errs) == 0 {
		t.Error("written \"\": body-only pre-flight took it")
	}
}
