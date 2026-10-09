package handler

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/policy"
)

// NOT t.Parallel: installs the package-level policy manager /metrics reads
// (the golden test renders it unset).
func TestPolicyAvailabilityMetrics(t *testing.T) {
	t.Cleanup(func() { SetPolicyAvailabilitySource(nil) })
	render := func() string {
		var b bytes.Buffer
		writePolicyAvailabilityMetrics(&b)
		return b.String()
	}
	if got := render(); !strings.Contains(got, "\ntenant_api_policy_available 1\n") ||
		!strings.Contains(got, "\ntenant_api_policy_unavailable_open_total 0\n") {
		t.Fatalf("no manager installed: %s", got)
	}
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "_domain_policy.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	m := policy.NewManager(dir)
	SetPolicyAvailabilitySource(m)
	if got := render(); !strings.Contains(got, "\ntenant_api_policy_available 0\n") {
		t.Errorf("a directory where the policy file goes: %s", got)
	}
	m.SetOpenOnUnavailable(true)
	_ = m.CheckAvailable("test")
	if got := render(); !strings.Contains(got, "\ntenant_api_policy_unavailable_open_total 1\n") {
		t.Errorf("one write let through: %s", got)
	}
}

// The per-op gate executeBatchOps runs at execution time (an async batch
// runs after the request's own check): only an op the policy judges is
// refused, with POLICY_UNAVAILABLE.
func TestBatchOpPolicyUnavailable(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "_domain_policy.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	m := policy.NewManager(dir)
	for _, tc := range []struct {
		op      BatchOperation
		refused bool
	}{
		{BatchOperation{TenantID: "t1", Patch: map[string]string{"_routing_profile": "p"}}, true},
		{BatchOperation{TenantID: "t1", Patch: map[string]string{"_routing_receiver_type": "slack"}}, true},
		{BatchOperation{TenantID: "t1", Unset: []string{"_routing"}}, true},
		{BatchOperation{TenantID: "t1", Patch: map[string]string{"cpu_usage_percent": "90"}}, false},
	} {
		res, refused := batchOpPolicyUnavailable(m, tc.op)
		if refused != tc.refused || (refused && (res.Code != CodePolicyUnavailable || res.Status != "error")) {
			t.Errorf("%+v: refused = %v (%+v), want %v", tc.op, refused, res, tc.refused)
		}
	}
	if _, refused := batchOpPolicyUnavailable(policy.NewManager(t.TempDir()), BatchOperation{TenantID: "t1",
		Patch: map[string]string{"_routing_profile": "p"}}); refused {
		t.Error("no policy file: the op is refused")
	}
}
