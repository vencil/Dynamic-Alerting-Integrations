package batchpr

import (
	"strings"
	"testing"
)

// fixtureAllocPlan returns a Plan with 1 base + 2 tenant chunks.
// Distinct from apply_test fixture — caller controls tenants per
// chunk so allocation-routing edge cases can be exercised.
func fixtureAllocPlan() *Plan {
	return &Plan{
		Items: []PlanItem{
			{Kind: PlanItemBase, Title: "Base"},
			{Kind: PlanItemTenant, ChunkKey: "db", TenantIDs: []string{"tenant-a", "tenant-b"}},
			{Kind: PlanItemTenant, ChunkKey: "web", TenantIDs: []string{"tenant-c"}},
		},
	}
}

func TestAllocateFiles_HappyPath(t *testing.T) {
	plan := fixtureAllocPlan()
	files := map[string][]byte{
		"db/_defaults.yaml": []byte("base body"),
		"db/PROPOSAL.md":    []byte("proposal md"),
		"db/tenant-a.yaml":  []byte("tenant-a body"),
		"db/tenant-b.yaml":  []byte("tenant-b body"),
		"web/tenant-c.yaml": []byte("tenant-c body"),
	}
	got, warnings := AllocateFiles(plan, files)
	if len(warnings) != 0 {
		t.Errorf("unexpected warnings: %v", warnings)
	}
	// Item 0 (base) should have _defaults.yaml + PROPOSAL.md.
	if _, ok := got[0]["db/_defaults.yaml"]; !ok {
		t.Errorf("base bucket missing _defaults.yaml; got %v", keysOf(got[0]))
	}
	if _, ok := got[0]["db/PROPOSAL.md"]; !ok {
		t.Errorf("base bucket missing PROPOSAL.md; got %v", keysOf(got[0]))
	}
	// Item 1 (db chunk) should have tenant-a + tenant-b.
	if _, ok := got[1]["db/tenant-a.yaml"]; !ok {
		t.Errorf("db chunk missing tenant-a.yaml")
	}
	if _, ok := got[1]["db/tenant-b.yaml"]; !ok {
		t.Errorf("db chunk missing tenant-b.yaml")
	}
	// Item 2 (web chunk) should have tenant-c.
	if _, ok := got[2]["web/tenant-c.yaml"]; !ok {
		t.Errorf("web chunk missing tenant-c.yaml")
	}
}

func TestAllocateFiles_EmptyPlan(t *testing.T) {
	got, warns := AllocateFiles(nil, map[string][]byte{"a": {}})
	if got != nil {
		t.Errorf("expected nil result for nil plan; got %v", got)
	}
	if len(warns) == 0 {
		t.Errorf("expected warning for nil plan")
	}
}

func TestAllocateFiles_EmptyFiles(t *testing.T) {
	got, warns := AllocateFiles(fixtureAllocPlan(), nil)
	if len(got) != 0 {
		t.Errorf("expected empty result for empty files; got %v", got)
	}
	if len(warns) != 0 {
		t.Errorf("expected no warnings for empty files; got %v", warns)
	}
}

func TestAllocateFiles_UnknownTenantWarns(t *testing.T) {
	plan := fixtureAllocPlan()
	_, warns := AllocateFiles(plan, map[string][]byte{
		"db/tenant-zzz.yaml": []byte("body"),
	})
	if len(warns) == 0 {
		t.Errorf("expected warning for unknown tenant")
	}
	matched := false
	for _, w := range warns {
		if strings.Contains(w, "tenant-zzz") && strings.Contains(w, "not in any Plan chunk") {
			matched = true
		}
	}
	if !matched {
		t.Errorf("warning should call out tenant-zzz + plan-chunk reason; got %v", warns)
	}
}

func TestAllocateFiles_UnrecognisedShapeWarns(t *testing.T) {
	plan := fixtureAllocPlan()
	_, warns := AllocateFiles(plan, map[string][]byte{
		"db/random.txt": []byte("not yaml"),
	})
	matched := false
	for _, w := range warns {
		if strings.Contains(w, "unrecognised file shape") {
			matched = true
		}
	}
	if !matched {
		t.Errorf("expected unrecognised-shape warning; got %v", warns)
	}
}

func TestAllocateFiles_NoBasePR(t *testing.T) {
	// Plan with no base PR (only tenant chunks); _defaults.yaml
	// has nowhere to go → warning + skipped.
	plan := &Plan{
		Items: []PlanItem{
			{Kind: PlanItemTenant, TenantIDs: []string{"tenant-a"}},
		},
	}
	_, warns := AllocateFiles(plan, map[string][]byte{
		"_defaults.yaml": []byte("base"),
	})
	if len(warns) == 0 || !strings.Contains(warns[0], "no Base PR") {
		t.Errorf("expected no-Base-PR warning; got %v", warns)
	}
}

func TestAllocateFiles_DuplicateTenantInTwoChunksGoesToFirst(t *testing.T) {
	// Defensive: same tenant ID in two chunks (PR-1 contract
	// violation, but allocator must not crash).
	plan := &Plan{
		Items: []PlanItem{
			{Kind: PlanItemBase, Title: "Base"},
			{Kind: PlanItemTenant, ChunkKey: "first", TenantIDs: []string{"tenant-x"}},
			{Kind: PlanItemTenant, ChunkKey: "second", TenantIDs: []string{"tenant-x"}}, // dup
		},
	}
	got, _ := AllocateFiles(plan, map[string][]byte{
		"x/tenant-x.yaml": []byte("body"),
	})
	if _, ok := got[1]["x/tenant-x.yaml"]; !ok {
		t.Errorf("dup tenant should land in first chunk; bucket 1 keys: %v", keysOf(got[1]))
	}
	if _, ok := got[2]["x/tenant-x.yaml"]; ok {
		t.Errorf("dup tenant should NOT land in second chunk too")
	}
}

// helper
func keysOf(m map[string][]byte) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
