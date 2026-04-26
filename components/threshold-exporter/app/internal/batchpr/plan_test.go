package batchpr

import (
	"encoding/json"
	"sort"
	"strings"
	"testing"
)

// fixtureProposal returns a 1-proposal PlanInput with the given
// tenants. Most tests start from this and tweak.
func fixtureProposal(tenants ...string) ProposalRef {
	return ProposalRef{
		MemberRuleIDs:   []string{"src#groups[0].rules[0]"},
		MemberTenantIDs: tenants,
		Dialect:         "prom",
		SharedFor:       "5m",
		SharedLabels:    map[string]string{"severity": "warning"},
	}
}

// dirsByDomain builds a TenantDirs map placing each tenant under
// the supplied (domain, region) hint.
func dirsByDomain(spec map[string][2]string) map[string]string {
	out := make(map[string]string, len(spec))
	for tid, d := range spec {
		out[tid] = d[0] + "/" + d[1] + "/" + tid
	}
	return out
}

func TestBuildPlan_ErrorsOnEmptyProposals(t *testing.T) {
	_, err := BuildPlan(PlanInput{})
	if err == nil {
		t.Fatal("err = nil for empty proposals, want error")
	}
}

func TestBuildPlan_ErrorsOnChunkByCountWithoutSize(t *testing.T) {
	_, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{fixtureProposal("t1")},
		ChunkBy:   ChunkByCount,
	})
	if err == nil {
		t.Fatal("err = nil for ChunkByCount without ChunkSize, want error")
	}
}

func TestBuildPlan_BasePRAlwaysFirst(t *testing.T) {
	plan, err := BuildPlan(PlanInput{
		Proposals:  []ProposalRef{fixtureProposal("t1", "t2")},
		TenantDirs: dirsByDomain(map[string][2]string{"t1": {"dom-a", "r1"}, "t2": {"dom-a", "r1"}}),
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if len(plan.Items) < 2 {
		t.Fatalf("got %d items, want ≥ 2 (base + ≥1 tenant)", len(plan.Items))
	}
	if plan.Items[0].Kind != PlanItemBase {
		t.Errorf("first item kind = %q, want %q", plan.Items[0].Kind, PlanItemBase)
	}
	for i, it := range plan.Items[1:] {
		if it.Kind != PlanItemTenant {
			t.Errorf("item %d kind = %q, want %q", i+1, it.Kind, PlanItemTenant)
		}
		if it.BlockedBy != baseBlockedByMarker {
			t.Errorf("tenant item %d BlockedBy = %q, want %q", i+1, it.BlockedBy, baseBlockedByMarker)
		}
	}
}

func TestBuildPlan_ChunkByDomain_GroupsByFirstSegment(t *testing.T) {
	prop := ProposalRef{
		MemberTenantIDs: []string{"t-a1", "t-a2", "t-b1"},
		Dialect:         "prom",
	}
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{prop},
		TenantDirs: map[string]string{
			"t-a1": "dom-a/r1/t-a1",
			"t-a2": "dom-a/r1/t-a2",
			"t-b1": "dom-b/r1/t-b1",
		},
		ChunkBy: ChunkByDomain,
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	// 1 base + 2 tenant chunks (one per domain).
	if got, want := len(plan.Items), 3; got != want {
		t.Fatalf("got %d items, want %d", got, want)
	}
	tenantItems := plan.Items[1:]
	if tenantItems[0].ChunkKey != "dom-a" || tenantItems[1].ChunkKey != "dom-b" {
		t.Errorf("chunk keys = %q, %q; want dom-a, dom-b (sorted)",
			tenantItems[0].ChunkKey, tenantItems[1].ChunkKey)
	}
}

func TestBuildPlan_ChunkByRegion_GroupsByFirstTwoSegments(t *testing.T) {
	prop := ProposalRef{
		MemberTenantIDs: []string{"t1", "t2", "t3"},
		Dialect:         "prom",
	}
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{prop},
		TenantDirs: map[string]string{
			"t1": "dom-a/r1/t1",
			"t2": "dom-a/r2/t2",
			"t3": "dom-a/r1/t3",
		},
		ChunkBy: ChunkByRegion,
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	// dom-a/r1 (t1+t3), dom-a/r2 (t2)
	if got := plan.Summary.TenantPRCount; got != 2 {
		t.Errorf("TenantPRCount = %d, want 2", got)
	}
	tenantItems := plan.Items[1:]
	if tenantItems[0].ChunkKey != "dom-a/r1" {
		t.Errorf("first chunk key = %q, want dom-a/r1", tenantItems[0].ChunkKey)
	}
}

func TestBuildPlan_ChunkByCount_FixedSize(t *testing.T) {
	tenants := []string{"t1", "t2", "t3", "t4", "t5"}
	dirs := make(map[string]string)
	for _, t := range tenants {
		dirs[t] = "any/dir/" + t
	}
	prop := ProposalRef{MemberTenantIDs: tenants, Dialect: "prom"}
	plan, err := BuildPlan(PlanInput{
		Proposals:  []ProposalRef{prop},
		TenantDirs: dirs,
		ChunkBy:    ChunkByCount,
		ChunkSize:  2,
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	// 5 tenants / 2 = 3 chunks (2, 2, 1).
	if got := plan.Summary.TenantPRCount; got != 3 {
		t.Errorf("TenantPRCount = %d, want 3", got)
	}
	tenantItems := plan.Items[1:]
	chunkSizes := []int{len(tenantItems[0].TenantIDs), len(tenantItems[1].TenantIDs), len(tenantItems[2].TenantIDs)}
	wantSizes := []int{2, 2, 1}
	for i, want := range wantSizes {
		if chunkSizes[i] != want {
			t.Errorf("chunk %d size = %d, want %d", i, chunkSizes[i], want)
		}
	}
}

func TestBuildPlan_SoftCapSplitsOversizedDomain(t *testing.T) {
	// Stress: 7 tenants all in same domain, ChunkSize=3 → expect 3
	// chunks (3+3+1) prefixed `dom-x/part-NN`.
	tenants := []string{"a", "b", "c", "d", "e", "f", "g"}
	dirs := make(map[string]string)
	for _, t := range tenants {
		dirs[t] = "dom-x/r1/" + t
	}
	prop := ProposalRef{MemberTenantIDs: tenants, Dialect: "prom"}
	plan, err := BuildPlan(PlanInput{
		Proposals:  []ProposalRef{prop},
		TenantDirs: dirs,
		ChunkBy:    ChunkByDomain,
		ChunkSize:  3,
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if got := plan.Summary.TenantPRCount; got != 3 {
		t.Errorf("TenantPRCount = %d, want 3 (7 tenants split into 3+3+1 with cap 3)", got)
	}
	tenantItems := plan.Items[1:]
	for i, item := range tenantItems {
		if !strings.HasPrefix(item.ChunkKey, "dom-x/part-") {
			t.Errorf("chunk %d key = %q, want dom-x/part-NN prefix", i, item.ChunkKey)
		}
	}
}

func TestBuildPlan_MissingTenantDirSurfacesAsWarning(t *testing.T) {
	prop := ProposalRef{
		MemberTenantIDs: []string{"known", "missing"},
		Dialect:         "prom",
	}
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{prop},
		TenantDirs: map[string]string{
			"known": "dom-a/r1/known",
			// "missing" deliberately not in the map
		},
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if len(plan.Warnings) != 1 {
		t.Fatalf("got %d warnings, want 1", len(plan.Warnings))
	}
	if !strings.Contains(plan.Warnings[0], "missing") {
		t.Errorf("warning = %q, want mention of `missing`", plan.Warnings[0])
	}
	// `known` still gets a tenant PR; `missing` is skipped.
	if got := plan.Summary.TotalTenants; got != 1 {
		t.Errorf("TotalTenants = %d, want 1 (missing is skipped)", got)
	}
}

func TestBuildPlan_DefaultsToChunkByDomain(t *testing.T) {
	prop := ProposalRef{
		MemberTenantIDs: []string{"t1", "t2"},
		Dialect:         "prom",
	}
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{prop},
		TenantDirs: map[string]string{
			"t1": "dom-x/r1/t1",
			"t2": "dom-x/r1/t2",
		},
		// ChunkBy left zero — should default to ChunkByDomain.
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if plan.Summary.ChunkBy != ChunkByDomain {
		t.Errorf("Summary.ChunkBy = %q, want %q (default)", plan.Summary.ChunkBy, ChunkByDomain)
	}
	if plan.Summary.EffectiveChunkSize != defaultChunkSize {
		t.Errorf("EffectiveChunkSize = %d, want %d (default)", plan.Summary.EffectiveChunkSize, defaultChunkSize)
	}
}

func TestBuildPlan_TitlesNumberedConsistently(t *testing.T) {
	tenants := []string{"a", "b", "c", "d"}
	dirs := make(map[string]string)
	dirs["a"] = "dom-x/r1/a"
	dirs["b"] = "dom-x/r1/b"
	dirs["c"] = "dom-y/r1/c"
	dirs["d"] = "dom-y/r1/d"
	prop := ProposalRef{MemberTenantIDs: tenants, Dialect: "prom"}
	plan, err := BuildPlan(PlanInput{
		Proposals:  []ProposalRef{prop},
		TenantDirs: dirs,
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	tenantItems := plan.Items[1:]
	wantTitles := []string{
		"[chunk 1/2] Import PromRules to dom-x",
		"[chunk 2/2] Import PromRules to dom-y",
	}
	for i, want := range wantTitles {
		if tenantItems[i].Title != want {
			t.Errorf("item %d title = %q, want %q", i+1, tenantItems[i].Title, want)
		}
	}
}

func TestBuildPlan_BasePRTitleIncludesDialectMix(t *testing.T) {
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{
			{MemberTenantIDs: []string{"t1"}, Dialect: "prom"},
			{MemberTenantIDs: []string{"t2"}, Dialect: "metricsql"},
		},
		TenantDirs: map[string]string{
			"t1": "dom/r/t1",
			"t2": "dom/r/t2",
		},
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if !strings.Contains(plan.Items[0].Title, "metricsql+prom") {
		t.Errorf("Base title = %q, want sorted dialect mix `metricsql+prom`", plan.Items[0].Title)
	}
}

func TestBuildPlan_DeterministicOutput(t *testing.T) {
	// Same input two runs → byte-identical JSON. Catches any leaked
	// map iteration order in the planner.
	in := PlanInput{
		Proposals: []ProposalRef{
			{MemberTenantIDs: []string{"t-c", "t-a", "t-b"}, Dialect: "prom",
				SharedLabels: map[string]string{"severity": "warning", "team": "x"}},
		},
		TenantDirs: map[string]string{
			"t-a": "dom-a/r1/t-a",
			"t-b": "dom-b/r1/t-b",
			"t-c": "dom-a/r1/t-c",
		},
	}
	r1, err := BuildPlan(in)
	if err != nil {
		t.Fatalf("run 1: %v", err)
	}
	r2, err := BuildPlan(in)
	if err != nil {
		t.Fatalf("run 2: %v", err)
	}
	j1, _ := json.Marshal(r1)
	j2, _ := json.Marshal(r2)
	if string(j1) != string(j2) {
		t.Errorf("non-deterministic plan output:\nrun 1: %s\nrun 2: %s", j1, j2)
	}
	// Sanity checks on sortedness of the first run.
	for i, item := range r1.Items {
		if item.Kind != PlanItemTenant {
			continue
		}
		if !sort.StringsAreSorted(item.TenantIDs) {
			t.Errorf("item %d TenantIDs not sorted: %v", i, item.TenantIDs)
		}
	}
}

func TestBuildPlan_SourceProposalIndicesMapBackToInput(t *testing.T) {
	prop1 := ProposalRef{MemberTenantIDs: []string{"t1"}, Dialect: "prom"}
	prop2 := ProposalRef{MemberTenantIDs: []string{"t2"}, Dialect: "metricsql"}
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{prop1, prop2},
		TenantDirs: map[string]string{
			"t1": "dom-x/r1/t1",
			"t2": "dom-y/r1/t2",
		},
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	// Base PR references both proposals.
	base := plan.Items[0]
	if got := base.SourceProposalIndices; len(got) != 2 || got[0] != 0 || got[1] != 1 {
		t.Errorf("Base SourceProposalIndices = %v, want [0 1]", got)
	}
	// Each tenant chunk references only the proposal that contains
	// its tenant.
	for _, item := range plan.Items[1:] {
		switch item.ChunkKey {
		case "dom-x":
			if len(item.SourceProposalIndices) != 1 || item.SourceProposalIndices[0] != 0 {
				t.Errorf("dom-x chunk indices = %v, want [0]", item.SourceProposalIndices)
			}
		case "dom-y":
			if len(item.SourceProposalIndices) != 1 || item.SourceProposalIndices[0] != 1 {
				t.Errorf("dom-y chunk indices = %v, want [1]", item.SourceProposalIndices)
			}
		}
	}
}

func TestBuildPlan_UnassignedDirGoesToBucket(t *testing.T) {
	// Tenants placed at a TenantDirs path with no slashes still
	// need to bucket somewhere — `<unassigned>` is the safe label
	// so the chunk surfaces in the plan rather than vanishing.
	prop := ProposalRef{MemberTenantIDs: []string{"t1"}, Dialect: "prom"}
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{prop},
		TenantDirs: map[string]string{
			"t1": "", // intentionally empty path
		},
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if len(plan.Items) < 2 || plan.Items[1].ChunkKey != "<unassigned>" {
		t.Errorf("expected `<unassigned>` chunk, got items %v", plan.Items)
	}
}
