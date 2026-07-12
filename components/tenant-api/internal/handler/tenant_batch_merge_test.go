package handler

// Regression coverage for #1097: a PARTIAL batch patch must merge into the
// tenant's existing keys, never overwrite the whole document. Before the fix,
// buildPatchYAML built a minimal doc from only the patch keys and the write
// path committed it verbatim, silently dropping every un-patched key (other
// thresholds, `_metadata`, `_custom_alerts`) and all comments.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// existingTenantYAML mirrors the try-local seed shape: several threshold keys,
// a nested _metadata map, and human comments.
const existingTenantYAML = `tenants:
  db-a:
    mysql_connections: "50"            # warning threshold
    mysql_connections_critical: "120"  # critical threshold
    mysql_cpu: "40"
    _metadata:
      owner: "platform-db-team"
      tier: "tier-1"
`

// --- pure mergePatchYAML unit tests (no git) ---

func TestMergePatchYAML_PreservesKeysAndComments(t *testing.T) {
	t.Parallel()
	out, err := mergePatchYAML([]byte(existingTenantYAML), "db-a", map[string]string{
		"_silent_mode": "warning",
	})
	if err != nil {
		t.Fatalf("mergePatchYAML: %v", err)
	}
	// The patched key is set...
	if !strings.Contains(out, "_silent_mode") {
		t.Errorf("patched key _silent_mode missing:\n%s", out)
	}
	// ...and every pre-existing key survives.
	for _, key := range []string{"mysql_connections", "mysql_connections_critical", "mysql_cpu", "_metadata", "owner", "tier-1"} {
		if !strings.Contains(out, key) {
			t.Errorf("pre-existing content %q lost after partial patch:\n%s", key, out)
		}
	}
	// ...and a human comment survives (the cardinal rule of the AST merge).
	if !strings.Contains(out, "# warning threshold") {
		t.Errorf("comment '# warning threshold' lost after partial patch:\n%s", out)
	}
	// The merged doc must parse and resolve db-a's original + new keys.
	var cfg struct {
		Tenants map[string]map[string]yaml.Node `yaml:"tenants"`
	}
	if err := yaml.Unmarshal([]byte(out), &cfg); err != nil {
		t.Fatalf("merged doc unparseable: %v\n%s", err, out)
	}
	db := cfg.Tenants["db-a"]
	if _, ok := db["mysql_connections"]; !ok {
		t.Error("mysql_connections missing from parsed merge result")
	}
	if v, ok := db["_silent_mode"]; !ok || v.Value != "warning" {
		t.Errorf("_silent_mode not set to warning in parsed result: %+v", v)
	}
}

func TestMergePatchYAML_NewTenantFallback(t *testing.T) {
	t.Parallel()
	// Empty existing → brand-new tenant → minimal doc (no error).
	out, err := mergePatchYAML(nil, "new-db", map[string]string{"_silent_mode": "critical"})
	if err != nil {
		t.Fatalf("mergePatchYAML(nil): %v", err)
	}
	for _, want := range []string{"tenants:", "new-db:", "_silent_mode", "critical"} {
		if !strings.Contains(out, want) {
			t.Errorf("new-tenant doc missing %q:\n%s", want, out)
		}
	}
}

func TestMergePatchYAML_MalformedExistingErrors(t *testing.T) {
	t.Parallel()
	// A non-empty but unparseable existing file must ERROR, never silently
	// overwrite — overwriting is the exact data loss this fix prevents.
	if _, err := mergePatchYAML([]byte("{{not yaml"), "db-a", map[string]string{"_silent_mode": "warning"}); err == nil {
		t.Error("expected error for unparseable existing file, got nil (would clobber)")
	}
	// Existing file whose tenants.<id> is a scalar, not a mapping → error.
	if _, err := mergePatchYAML([]byte("tenants:\n  db-a: oops\n"), "db-a", map[string]string{"_silent_mode": "warning"}); err == nil {
		t.Error("expected error when tenants.db-a is not a mapping, got nil")
	}
}

func TestMergePatchYAML_ValueStaysQuotedString(t *testing.T) {
	t.Parallel()
	// A numeric-looking string value must round-trip as a STRING (quoted), not
	// leak into the file as a bare int that changes type on the next read.
	out, err := mergePatchYAML([]byte(existingTenantYAML), "db-a", map[string]string{
		"mysql_cpu": "75",
	})
	if err != nil {
		t.Fatalf("mergePatchYAML: %v", err)
	}
	var cfg struct {
		Tenants map[string]map[string]yaml.Node `yaml:"tenants"`
	}
	if err := yaml.Unmarshal([]byte(out), &cfg); err != nil {
		t.Fatalf("unparseable: %v", err)
	}
	cpu := cfg.Tenants["db-a"]["mysql_cpu"]
	if cpu.Value != "75" {
		t.Errorf("mysql_cpu = %q, want \"75\"", cpu.Value)
	}
	if cpu.Tag != "" && cpu.Tag != "!!str" {
		t.Errorf("mysql_cpu tag = %q, want a string tag (value must not become an int)", cpu.Tag)
	}
}

// --- direct commit-on-write path (applyPatch → WriteMerged) ---

// TestApplyPatch_PreservesExistingKeys is the top-level #1097 guard for the
// shared per-tenant write both tenant-batch and group-batch funnel through: a
// partial patch commits a file that keeps the tenant's other keys and comments.
// Exercises the real applyPatch → WriteMerged → mergePatchYAML → commit chain
// (RBAC is enforced one layer up, in executeBatchOps/executeGroupBatchOps, and
// is orthogonal to the merge behavior under test here).
func TestApplyPatch_PreservesExistingKeys(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		// conf.d always ships _defaults.yaml; the whole merged doc is validated,
		// so the tenant's pre-existing metric keys must resolve against it.
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_cpu: 90\n",
		"db-a.yaml":      existingTenantYAML,
	})
	initGitRepo(t, configDir)

	gw := newTestWriter(configDir)
	op := BatchOperation{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "warning"}}
	res := applyPatch(context.Background(), gw, configDir, op, "op@example.com")
	if res.Status != "ok" {
		t.Fatalf("applyPatch status = %q, message = %q; want ok", res.Status, res.Message)
	}

	after, err := os.ReadFile(filepath.Join(configDir, "db-a.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	got := string(after)
	if !strings.Contains(got, "_silent_mode") {
		t.Errorf("patched key missing from committed file:\n%s", got)
	}
	for _, key := range []string{"mysql_connections", "mysql_connections_critical", "mysql_cpu", "_metadata", "platform-db-team"} {
		if !strings.Contains(got, key) {
			t.Errorf("#1097 regression: pre-existing content %q dropped by partial patch:\n%s", key, got)
		}
	}
	if !strings.Contains(got, "# warning threshold") {
		t.Errorf("#1097 regression: comment dropped by partial patch:\n%s", got)
	}
}
