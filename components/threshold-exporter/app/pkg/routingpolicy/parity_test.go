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
	GroupByInvalid [][2]string     `json:"group_by_invalid"` // #2503: the Python value only
}

// parityEscalation is the `escalation` cell (#2325).
type parityEscalation struct {
	Verdict string   `json:"verdict"` // "compliant" | "violation"
	Leaks   []string `json:"leaks"`
}

type parityExpect struct {
	Targets        *[]parityTarget   `json:"targets"`
	Policy         [][3]string       `json:"policy"`
	RejectedRoutes []string          `json:"rejected_routes"`
	NotString      []string          `json:"values_not_string"`
	GroupByInvalid [][2]string       `json:"group_by_invalid"`
	UnknownProfile *string           `json:"unknown_profile"`
	TenantAPI      *parityTenantAPI  `json:"tenant_api"`
	PythonDiffers  *parityDiffers    `json:"python_differs"`
	Escalation     *parityEscalation `json:"escalation"`
	Refused        *string           `json:"refused"` // #2341
}

type parityTree struct {
	Name     string                  `json:"name"`
	Files    map[string]string       `json:"files"`
	Platform [][3]string             `json:"platform"`
	Expect   map[string]parityExpect `json:"expect"`
	// EnforcedGroupBy (#2503): [file, field, kind] per bad group_by element
	// of the rendered `_routing_enforced` route(s).
	EnforcedGroupBy [][3]string `json:"enforced_group_by_invalid"`
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
// platform file's entry for it is the overlay, Layers.TenantBlock. Its
// `_routing` carries the values the generator reads with PyYAML
// (WithPyYAMLRouting, #2295 / #2431), as da-guard and tenant-api read it.
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
			if r, has := b["_routing"]; has {
				b["_routing"] = WithPyYAMLRouting(r, PyYAMLRoutingByTenant([]byte(content))[tenantID])
			}
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

// gotNotString is the `values_not_string` cell (#2431): the fields
// ValuesNotString names in the resolved routing, in name order.
func gotNotString(resolved map[string]any, ok bool) []string {
	out := []string{}
	if !ok {
		return out
	}
	for _, v := range ValuesNotString(resolved) {
		out = append(out, v.Field)
	}
	sort.Strings(out)
	return out
}

// gotGroupBy is the `group_by_invalid` cell (#2503): [field, kind] per
// GroupByInvalid finding of the resolved routing, in its order (main,
// overrides, routes; element index order).
func gotGroupBy(resolved map[string]any, ok bool) [][2]string {
	out := [][2]string{}
	if !ok {
		return out
	}
	for _, p := range GroupByInvalid(resolved) {
		out = append(out, [2]string{p.Field, p.Kind})
	}
	return out
}

// gotRefused is the `refused` cell (#2341): why the tenant renders nothing.
func gotRefused(block map[string]any) *string {
	if r, has := block["_routing"]; has && RoutingNotMapping(r) {
		kind := "routing_not_mapping"
		return &kind
	}
	return nil
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

// gotEscalation is the `escalation` cell: nil when no requiring domain
// judges the tenant, else the one verdict every requiring domain reaches —
// leak refs in the order JudgeCriticalEscalation lists them.
func gotEscalation(t *testing.T, tenantID string, resolved map[string]any, ok bool, pols []Policy) *parityEscalation {
	t.Helper()
	if !ok {
		return nil
	}
	var cells []parityEscalation
	for _, f := range CheckCriticalEscalation(tenantID, resolved, pols) {
		c := parityEscalation{Verdict: "violation", Leaks: []string{}}
		if f.Verdict.Compliant() {
			c.Verdict = "compliant"
			for _, l := range f.Verdict.Leaks {
				c.Leaks = append(c.Leaks, l.Ref)
			}
		}
		cells = append(cells, c)
	}
	if len(cells) == 0 {
		return nil
	}
	for _, c := range cells[1:] {
		jsonEq(t, "escalation per domain", c, cells[0])
	}
	return &cells[0]
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
			// #2503: the enforced route(s)' group_by, over the tenants with a
			// resolved routing (every routed tenant is in expect: the Python
			// half asserts it), in EnforcedGroupByInvalid's order.
			var routed []string
			for tenantID := range tree.Expect {
				block, file := tenantBlock(t, tree.Files, tenantID)
				layers := ltree.LayersFor(LevelOf(file))
				if _, ok, _, _ := Resolve(tenantID, layers.TenantBlock(tenantID, block), layers); ok {
					routed = append(routed, tenantID)
				}
			}
			gotEnforced := [][3]string{}
			for _, p := range EnforcedGroupByInvalid(ltree.Enforced, routed) {
				gotEnforced = append(gotEnforced, [3]string{p.File, p.Path(), p.Kind})
			}
			jsonEq(t, "enforced_group_by_invalid", gotEnforced, append([][3]string{}, tree.EnforcedGroupBy...))
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
					wantNotString := append([]string{}, want.NotString...)
					sort.Strings(wantNotString)
					jsonEq(t, "values_not_string", gotNotString(resolved, ok), wantNotString)
					wantGroupBy := append([][2]string{}, want.GroupByInvalid...)
					jsonEq(t, "group_by_invalid", gotGroupBy(resolved, ok), wantGroupBy)
					jsonEq(t, "policy", sortRows(gotPolicy(tenantID, resolved, ok, pols)), sortRows(want.Policy))
					jsonEq(t, "escalation", gotEscalation(t, tenantID, resolved, ok, pols), want.Escalation)
					jsonEq(t, "refused", gotRefused(layers.TenantBlock(tenantID, block)), want.Refused)
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
		if !ok {
			return false
		}
		if len(CheckReceiverTypes(tenantID, resolved, pols)) > 0 {
			return true
		}
		for _, f := range CheckCriticalEscalation(tenantID, resolved, pols) { // #2325
			if !f.Verdict.Compliant() {
				return true
			}
		}
		return false
	}
	put := "ok"
	r, writesRouting := block["_routing"]
	if verdict(block) {
		put = "403"
	} else if (writesRouting && RoutingNotMapping(r)) || writesBadReceiver(block) || len(ValuesNotString(block["_routing"])) > 0 ||
		len(GroupByInvalidForTenant(tenantID, block["_routing"])) > 0 {
		put = "400" // #2295 receiver contract, #2431 a matcher value / #2503 a group_by element the block writes
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
