package routingpolicy

// parity_test.go — this package's half of
// tests/shared/routing_policy_parity_matrix.json (#2280). The Python half
// (tests/shared/test_routing_policy_parity.py) asserts the route generator's
// reader against the same table; neither reads the other's source.

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/receiverspec"
)

type parityTarget struct {
	Ref  string  `json:"ref"`
	Type *string `json:"type"`
}

type parityBatch struct {
	Patch   map[string]string `json:"patch"`
	Verdict string            `json:"verdict"`
}

type parityTenantAPI struct {
	Put   string       `json:"put"`
	Batch *parityBatch `json:"batch"`
}

type parityDiffers struct {
	Reason         string          `json:"reason"`
	Targets        *[]parityTarget `json:"targets"`
	Policy         [][3]string     `json:"policy"`
	RejectedRoutes []string        `json:"rejected_routes"`
}

type parityExpect struct {
	Targets        *[]parityTarget  `json:"targets"`
	Policy         [][3]string      `json:"policy"`
	RejectedRoutes []string         `json:"rejected_routes"`
	UnknownProfile *string          `json:"unknown_profile"`
	TenantAPI      *parityTenantAPI `json:"tenant_api"`
	PythonDiffers  *parityDiffers   `json:"python_differs"`
}

type parityTree struct {
	Name     string                  `json:"name"`
	Files    map[string]string       `json:"files"`
	Platform [][3]string             `json:"platform"`
	Expect   map[string]parityExpect `json:"expect"`
}

type parityMatrix struct {
	Comment       []string     `json:"_comment"`
	BlockingKinds []string     `json:"blocking_kinds"`
	Trees         []parityTree `json:"trees"`
}

func loadParityMatrix(t *testing.T) parityMatrix {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "routing_policy_parity_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m parityMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	return m
}

// tenantBlock is tenants.<id> of the tenant file that declares it — the
// first in name order, at any depth (#2326) — and that file's path; a root
// platform file's entry for it is the overlay, Layers.TenantBlock.
func tenantBlock(t *testing.T, files map[string]string, tenantID string) (map[string]any, string) {
	t.Helper()
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		content := files[name]
		if base := filepath.Base(name); base[0] == '_' || !config.IsScannedPath(name) {
			continue
		}
		var doc struct {
			Tenants map[string]map[string]any `yaml:"tenants"`
		}
		if err := yaml.Unmarshal([]byte(content), &doc); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if b, ok := doc.Tenants[tenantID]; ok {
			return b, name
		}
	}
	t.Fatalf("no tenant file declares tenant %q", tenantID)
	return nil, ""
}

func gotTargets(resolved map[string]any, ok bool) *[]parityTarget {
	if !ok {
		return nil
	}
	out := []parityTarget{}
	for _, tg := range Targets(resolved) {
		pt := parityTarget{Ref: tg.Ref}
		if rt := ReceiverType(tg.Receiver); rt != "" {
			pt.Type = &rt
		}
		out = append(out, pt)
	}
	return &out
}

func gotRejected(resolved map[string]any, ok bool) []string {
	out := []string{}
	if !ok || resolved["routes"] == nil {
		return out
	}
	routes, isList := resolved["routes"].([]any)
	if !isList {
		return []string{"routes"}
	}
	for i, e := range routes {
		if _, bad := RouteEntryProblem(e); bad {
			out = append(out, "routes["+itoa(i)+"]")
		}
	}
	return out
}

func gotPolicy(tenantID string, resolved map[string]any, ok bool, pols []Policy) [][3]string {
	out := [][3]string{}
	if !ok {
		return out
	}
	for _, v := range CheckReceiverTypes(tenantID, resolved, pols) {
		out = append(out, [3]string{v.Domain, v.Target, v.Constraint})
	}
	return out
}

func sortRows(rows [][3]string) [][3]string {
	out := append([][3]string{}, rows...)
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		for k := 0; k < 3; k++ {
			if a[k] != b[k] {
				return a[k] < b[k]
			}
		}
		return false
	})
	return out
}

func jsonEq(t *testing.T, what string, got, want any) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	if !bytes.Equal(g, w) {
		t.Errorf("%s:\n got %s\nwant %s", what, g, w)
	}
}

func TestRoutingPolicyParityMatrix(t *testing.T) {
	t.Parallel()
	m := loadParityMatrix(t)
	for _, tree := range m.Trees {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			if len(tree.Expect) == 0 {
				t.Fatal("tree expects nothing")
			}
			dir := t.TempDir()
			for rel, content := range tree.Files {
				p := filepath.Join(dir, filepath.FromSlash(rel))
				if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			// #2326: the whole tree, as the generator reads it.
			ltree, pols, probs := LoadTree(dir, nil)
			// #2291: routing where the generator never reads it — the
			// defaults carriers come from the exporter's own walk. A tree
			// with a duplicate tenant id is one the exporter rejects outright
			// (the table's duplicate_tenant row), so it has no scope to walk.
			scoped, err := config.ScopeEffective(dir, "")
			var dup *config.DuplicateTenantError
			switch {
			case err == nil:
				probs = append(probs, UnreadRouting(dir, scoped.DefaultsFiles, nil)...)
			case errors.As(err, &dup):
			default:
				t.Fatal(err)
			}
			gotPlatform := [][3]string{}
			for _, p := range probs {
				gotPlatform = append(gotPlatform, [3]string{p.Kind, p.File, p.Field})
			}
			jsonEq(t, "platform", sortRows(gotPlatform), sortRows(append([][3]string{}, tree.Platform...)))
			for tenantID, want := range tree.Expect {
				t.Run(tenantID, func(t *testing.T) {
					// The generator's tenant layer: the tenant file's keys over
					// the root platform overlay (#2291). tenant-api's PUT body
					// carries no overlay, so its model below takes the file alone.
					block, file := tenantBlock(t, tree.Files, tenantID)
					if got := ltree.TenantLevel(tenantID); got != LevelOf(file) {
						t.Errorf("tenant level = %q, want %q (%s)", got, LevelOf(file), file)
					}
					layers := ltree.LayersFor(LevelOf(file))
					resolved, ok, _, unknown := Resolve(tenantID, layers.TenantBlock(tenantID, block), layers)
					jsonEq(t, "targets", gotTargets(resolved, ok), want.Targets)
					jsonEq(t, "rejected_routes", gotRejected(resolved, ok), want.RejectedRoutes)
					jsonEq(t, "policy", sortRows(gotPolicy(tenantID, resolved, ok, pols)), sortRows(want.Policy))
					if (unknown == nil) != (want.UnknownProfile == nil) ||
						(unknown != nil && *unknown != *want.UnknownProfile) {
						t.Errorf("unknown profile = %v, want %v", unknown, want.UnknownProfile)
					}
					if want.TenantAPI != nil {
						checkTenantAPIModel(t, tree.Files, tenantID, block, layers, *want.TenantAPI)
					}
				})
			}
		})
	}
}

// checkTenantAPIModel checks the tenant_api column against this package the
// way DESIGN #2280 has tenant-api use it: policies from `_domain_policy.yaml`
// only; PUT resolves the tenant's own file; a batch patch is laid over the
// on-disk block and judged only when it touches `_routing_profile` /
// `_routing`. It is a consistency check of the table — the handler's own
// assertion lives in tenant-api (internal/handler/routing_policy_parity_test.go).
func checkTenantAPIModel(t *testing.T, files map[string]string, tenantID string, block map[string]any, layers Layers, want parityTenantAPI) {
	t.Helper()
	var pols []Policy
	if src, ok := files["_domain_policy.yaml"]; ok {
		var err error
		if pols, _, err = ParseDomainPolicies([]byte(src)); err != nil {
			t.Fatalf("_domain_policy.yaml: %v", err)
		}
	}
	verdict := func(b map[string]any) bool { // true = refused
		resolved, ok, _, _ := Resolve(tenantID, b, layers)
		return ok && len(CheckReceiverTypes(tenantID, resolved, pols)) > 0
	}
	put := "ok"
	if verdict(block) {
		put = "403"
	} else if writesBadReceiver(block) {
		put = "400"
	}
	if put != want.Put {
		t.Errorf("tenant_api.put model = %s, table says %s", put, want.Put)
	}
	if want.Batch == nil {
		return
	}
	patched := map[string]any{}
	for k, v := range block {
		patched[k] = v
	}
	touches := false
	for k, v := range want.Batch.Patch {
		patched[k] = v
		touches = touches || k == "_routing_profile" || k == "_routing"
	}
	got := "ok"
	if touches && verdict(patched) {
		got = "policy_violation"
	}
	if got != want.Batch.Verdict {
		t.Errorf("tenant_api.batch model = %s, table says %s", got, want.Batch.Verdict)
	}
}

// writesBadReceiver models tenant-api's #2295 PUT check: a receiver the
// tenant block itself writes in `_routing` (main, overrides[i], routes[i];
// absent or null is not written) that pkg/receiverspec reports.
func writesBadReceiver(block map[string]any) bool {
	routing, ok := block["_routing"].(map[string]any)
	if !ok {
		return false
	}
	holders := []map[string]any{routing}
	for _, list := range []string{"overrides", "routes"} {
		entries, _ := routing[list].([]any)
		for _, e := range entries {
			if m, ok := e.(map[string]any); ok {
				holders = append(holders, m)
			}
		}
	}
	for _, h := range holders {
		if recv := h["receiver"]; recv != nil && len(receiverspec.Check(recv)) > 0 {
			return true
		}
	}
	return false
}

func itoa(i int) string {
	b, _ := json.Marshal(i)
	return string(b)
}
