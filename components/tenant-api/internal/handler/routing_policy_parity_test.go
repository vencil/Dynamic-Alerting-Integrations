package handler

// routing_policy_parity_test.go — tenant-api's half of
// tests/shared/routing_policy_parity_matrix.json (#2280), the `tenant_api`
// column, through the real handlers:
//
//   - put: PUT of the tenant's own file (files[<tenant>.yaml]) verbatim over
//     the tree's other files, policy loaded from the tree by
//     policy.NewManager — "403" = refused with POLICY_VIOLATION and nothing
//     written, "ok" = 200 and written.
//   - batch: the patch as one op through executeBatchOps (direct mode) over
//     the whole tree — "policy_violation" = the op is refused for domain
//     policy, "ok" = it is not.
//
// The Python generator and da-guard assert the other columns of the same
// table; none of the readers reads another's source.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/policy"
)

type tenantAPIParityCell struct {
	Put   string `json:"put"`
	Batch *struct {
		Patch   map[string]string `json:"patch"`
		Verdict string            `json:"verdict"`
	} `json:"batch"`
}

type tenantAPIParityTree struct {
	Name     string            `json:"name"`
	Files    map[string]string `json:"files"`
	Platform json.RawMessage   `json:"platform"`
	Expect   map[string]struct {
		Targets        json.RawMessage      `json:"targets"`
		Policy         json.RawMessage      `json:"policy"`
		RejectedRoutes json.RawMessage      `json:"rejected_routes"`
		UnknownProfile json.RawMessage      `json:"unknown_profile"`
		TenantAPI      *tenantAPIParityCell `json:"tenant_api"`
		PythonDiffers  json.RawMessage      `json:"python_differs"`
	} `json:"expect"`
}

func loadTenantAPIParityMatrix(t *testing.T) []tenantAPIParityTree {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..",
		"tests", "shared", "routing_policy_parity_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m struct {
		Comment       []string              `json:"_comment"`
		BlockingKinds json.RawMessage       `json:"blocking_kinds"`
		Trees         []tenantAPIParityTree `json:"trees"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	return m.Trees
}

func TestTenantAPI_RoutingPolicyParityMatrix(t *testing.T) {
	cells, batches := 0, 0
	for _, tree := range loadTenantAPIParityMatrix(t) {
		for tenantID, want := range tree.Expect {
			if want.TenantAPI == nil {
				continue
			}
			cells++
			own := tenantID + ".yaml"
			body, ok := tree.Files[own]
			if !ok {
				t.Fatalf("%s/%s: the table's PUT body is files[%s], absent", tree.Name, tenantID, own)
			}
			t.Run(tree.Name+"/"+tenantID+"/put", func(t *testing.T) {
				others := map[string]string{}
				for k, v := range tree.Files {
					if k != own {
						others[k] = v
					}
				}
				code, resp, configDir := putRoutingTenant(t, others, tenantID, body)
				_, statErr := os.Stat(filepath.Join(configDir, own))
				switch want.TenantAPI.Put {
				case "403":
					if code != http.StatusForbidden || !strings.Contains(resp, CodePolicyViolation) {
						t.Fatalf("status = %d, table says 403 POLICY_VIOLATION; body: %s", code, resp)
					}
					if !os.IsNotExist(statErr) {
						t.Errorf("refused PUT wrote %s (err=%v)", own, statErr)
					}
				case "ok":
					if code != http.StatusOK {
						t.Fatalf("status = %d, table says ok; body: %s", code, resp)
					}
					if statErr != nil {
						t.Errorf("allowed PUT did not write %s: %v", own, statErr)
					}
				default:
					t.Fatalf("unknown put verdict %q", want.TenantAPI.Put)
				}
			})
			if want.TenantAPI.Batch == nil {
				continue
			}
			batches++
			batch := want.TenantAPI.Batch
			t.Run(tree.Name+"/"+tenantID+"/batch", func(t *testing.T) {
				configDir := seedGitTree(t, tree.Files)
				d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
					Policy: policy.NewManager(configDir), WriteMode: WriteModeDirect}
				patch, _ := json.Marshal(batch.Patch)
				resp := runBatch(t, configDir, d, `[{"tenant_id":"`+tenantID+`","patch":`+string(patch)+`}]`)
				if len(resp.Results) != 1 {
					t.Fatalf("results = %+v", resp.Results)
				}
				r := resp.Results[0]
				refused := r.Status == "error" && strings.Contains(r.Message, "domain policy violation")
				switch batch.Verdict {
				case "policy_violation":
					if !refused {
						t.Errorf("result = %+v, table says policy_violation", r)
					}
				case "ok":
					if r.Status != "ok" {
						t.Errorf("result = %+v, table says ok", r)
					}
				default:
					t.Fatalf("unknown batch verdict %q", batch.Verdict)
				}
			})
		}
	}
	if cells == 0 || batches == 0 {
		t.Fatalf("tenant_api column exercised %d PUT and %d batch cells — a vacuous run", cells, batches)
	}
}
