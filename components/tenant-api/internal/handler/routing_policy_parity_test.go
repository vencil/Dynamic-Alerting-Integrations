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
//   - escalation (#2325), on the same PUT: a `violation` cell is refused
//     with constraint require_critical_escalation; an allowed PUT carries
//     exactly the leak refs as warnings (checkEscalationCell).
//
// The Python generator and da-guard assert the other columns of the same
// table; none of the readers reads another's source.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
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
		Escalation     *struct {
			Verdict string   `json:"verdict"`
			Leaks   []string `json:"leaks"`
		} `json:"escalation"`
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

// advisoryRef reads the destination ref back from one #2325 advisory.
var advisoryRef = regexp.MustCompile(
	`^tenant=(\S+): domain policy '[^']*': (?:((?:overrides|routes)\[\d+\]) \(|severity=critical alerts that no sub-route catches go to the main receiver)`)

// checkEscalationCell: a `violation` PUT is refused naming
// require_critical_escalation; an allowed PUT of a compliant tenant carries
// one advisory per leak ref (as a set, repeated per requiring domain) and
// no other; a tenant nothing judges carries none.
func checkEscalationCell(t *testing.T, tenantID string, want *struct {
	Verdict string   `json:"verdict"`
	Leaks   []string `json:"leaks"`
}, put, resp string) {
	t.Helper()
	if want != nil && want.Verdict == "violation" {
		if put != "403" || !strings.Contains(resp, `"constraint":"require_critical_escalation"`) {
			t.Errorf("escalation violation: PUT %s, body %s — want a require_critical_escalation POLICY_VIOLATION", put, resp)
		}
		return
	}
	if put != "ok" {
		return // refused for another constraint: no warnings to read
	}
	var body PutTenantResponse
	if err := json.Unmarshal([]byte(resp), &body); err != nil {
		t.Fatalf("PUT body: %v (%s)", err, resp)
	}
	got := map[string]bool{}
	for _, w := range body.Warnings {
		m := advisoryRef.FindStringSubmatch(w)
		if m == nil {
			continue
		}
		if m[1] != tenantID {
			t.Errorf("advisory names tenant %q, want %q: %s", m[1], tenantID, w)
		}
		ref := m[2]
		if ref == "" {
			ref = "receiver"
		}
		got[ref] = true
	}
	wantSet := map[string]bool{}
	if want != nil {
		for _, ref := range want.Leaks {
			wantSet[ref] = true
		}
	}
	if len(got) != len(wantSet) {
		t.Errorf("escalation advisories name %v, table says leaks %v", got, wantSet)
		return
	}
	for ref := range wantSet {
		if !got[ref] {
			t.Errorf("escalation advisories name %v, table says leaks %v", got, wantSet)
			return
		}
	}
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
				case "400": // #2295: a receiver the body writes breaks the contract
					if code != http.StatusBadRequest || !strings.Contains(resp, CodeInvalidBody) {
						t.Fatalf("status = %d, table says 400 INVALID_BODY; body: %s", code, resp)
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
				// #2325: the escalation cell, where tenant-api reads the
				// policy at all (`_domain_policy.yaml` only).
				if _, reads := tree.Files["_domain_policy.yaml"]; !reads {
					return
				}
				checkEscalationCell(t, tenantID, want.Escalation, want.TenantAPI.Put, resp)
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
