package handler

// #3206 (ADR-036 §4, confedit.Verify): a batch unset that removes the last
// key of a tenant's block leaves the block empty, which the re-encode writes
// `{}`. That mapping is the edit's own — before, the invariant compared its
// style (block, now flow) and refused the write as not_round_trippable. It is
// written now, and the file still loads with the tenant declared and no
// overrides left.

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

func TestBatchTenants_UnsetTheBlocksLastKey(t *testing.T) {
	const want = "tenants:\n  t-off: {}\n"
	for _, mode := range bothModes {
		t.Run(string(mode), func(t *testing.T) {
			files := routingPolicyTree()
			files["t-off.yaml"] = "tenants:\n  t-off:\n    _routing:\n" + pdReceiver
			configDir := seedGitTree(t, files)
			d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t), WriteMode: mode}
			var head string
			if mode == WriteModePR {
				d.PRClient = &mockPlatformClient{
					providerName: "github",
					createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
						head = h
						return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
					},
				}
				d.PRTracker = &mockPlatformTracker{}
			}
			resp := runBatch(t, configDir, d, "["+unsetRoutingOp+"]")
			if got := statuses(resp.Results); got != okStatus(mode) {
				t.Fatalf("statuses = %s, want %s: %+v", got, okStatus(mode), resp.Results)
			}
			var file []byte
			if mode == WriteModePR {
				out, err := exec.Command("git", "-C", configDir, "show", prBranchRef(head)+":t-off.yaml").CombinedOutput()
				if err != nil {
					t.Fatalf("git show: %v\n%s", err, out)
				}
				file = out
			} else {
				var err error
				if file, err = os.ReadFile(filepath.Join(configDir, "t-off.yaml")); err != nil {
					t.Fatal(err)
				}
			}
			if string(file) != want {
				t.Fatalf("t-off.yaml = %q, want %q", file, want)
			}
			c, err := cfg.ParseTenantFile(file)
			if err != nil {
				t.Fatalf("the written file does not load: %v", err)
			}
			overrides, declared := c.Tenants["t-off"]
			if !declared || len(overrides) != 0 {
				t.Fatalf("tenant declared %v with overrides %v; want declared, none", declared, overrides)
			}
		})
	}
}
