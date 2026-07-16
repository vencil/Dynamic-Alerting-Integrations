package rbac

// Candidate-config seam tests (ADR-027 / LD-6 P7): the dry-run's value rests
// on two invariants — a candidate report is indistinguishable from a
// path-loaded report except for the anchor (fidelity), and the empty-config
// verdict follows the LIVE fail-closed bit, never a zero value (tripwire).
// The parse table pins that ParseCandidateConfig keeps the full live
// pipeline (strict decode / null-match detection / claim-key validation).

import (
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// candidateFidelityYAML exercises a legacy rule, a match-block rule and an
// org-scoped rule so the fidelity comparison covers every grant shape.
const candidateFidelityYAML = `groups:
  - name: platform-admins
    tenants: ["*"]
    permissions: [admin]
  - name: org-ops
    match:
      groups: [ops]
      claims:
        org: [ORG-1, ORG-2]
    tenants: ["db-*"]
    permissions: [read, write]
    org-scope: org
`

var candidateClaimHeaders = map[string]string{"org": "X-Auth-Request-Org"}

// Fidelity: the same bytes evaluated via NewCandidate must produce a
// ReverseReport deep-equal to the path-loaded manager's — the rbac anchor
// (unanchored by design for a candidate) and the wall-clock GeneratedAt are
// the only permitted divergences.
func TestNewCandidate_FidelityWithPathLoad(t *testing.T) {
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", candidateFidelityYAML)
	live, err := NewManager(rbacFile, candidateClaimHeaders)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	cfg, err := ParseCandidateConfig([]byte(candidateFidelityYAML), candidateClaimHeaders)
	if err != nil {
		t.Fatalf("ParseCandidateConfig: %v", err)
	}
	cand := NewCandidate(cfg, live.EvaluationMode())

	orgs := []string{"ORG-1", "ORG-9"}
	opts := ReverseReportOptions{IncludeOrgValues: true}
	liveRep := live.ReverseAccessReport("db-team-1", orgs, true, "torgs-h", opts)
	candRep := cand.ReverseAccessReport("db-team-1", orgs, true, "torgs-h", opts)

	if got := candRep.ConfigAnchor.RBACSHA256.Value; got != AnchorUnanchored {
		t.Errorf("candidate rbac anchor = %q, want %q", got, AnchorUnanchored)
	}
	if got := liveRep.ConfigAnchor.RBACSHA256.Value; got == AnchorUnanchored || got == "" {
		t.Errorf("live rbac anchor = %q, want a real file hash", got)
	}

	// Neutralize the two permitted divergences, then require deep equality.
	liveRep.ConfigAnchor, candRep.ConfigAnchor = ReverseConfigAnchor{}, ReverseConfigAnchor{}
	liveRep.GeneratedAt, candRep.GeneratedAt = "", ""
	if !reflect.DeepEqual(liveRep, candRep) {
		t.Errorf("candidate report diverges from path-loaded report:\nlive: %+v\ncand: %+v", liveRep, candRep)
	}
}

// Fidelity under enforce: an enforce-enabled deployment must produce a
// candidate report whose runtime enforce flags match the live side. A
// constructor that cloned only failClosedOnEmpty (the original bug) would emit
// candidate flags of false while the baseline reads true — a spurious "not
// comparable" signal against the flags-parity contract. Pins that
// EvaluationMode carries the enforce bits.
func TestNewCandidate_FidelityUnderEnforce(t *testing.T) {
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", candidateFidelityYAML)
	live, err := NewManager(rbacFile, candidateClaimHeaders)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	live.EnableMetadataScopeEnforce()
	live.EnableOrgScopeEnforce()

	cfg, err := ParseCandidateConfig([]byte(candidateFidelityYAML), candidateClaimHeaders)
	if err != nil {
		t.Fatalf("ParseCandidateConfig: %v", err)
	}
	cand := NewCandidate(cfg, live.EvaluationMode())

	rep := cand.ReverseAccessReport("db-team-1", []string{"ORG-1"}, true, "torgs-h", ReverseReportOptions{})
	if !rep.Flags.OrgScopeEnforce.Value {
		t.Errorf("candidate org_scope_enforce = false, want true (live enforce bit not cloned)")
	}
	if !rep.Flags.MetadataScopeEnforce.Value {
		t.Errorf("candidate metadata_scope_enforce = false, want true (live enforce bit not cloned)")
	}
}

// Tripwire: an empty candidate under a fail-closed deployment must report
// fail_closed_empty — a constructor that dropped the bit (e.g. wrapping
// NewForTest's zero value) would report open_read, inverting the verdict an
// operator is about to act on. Both wire states are pinned.
func TestNewCandidate_EmptyConfigFollowsFailClosedBit(t *testing.T) {
	opts := ReverseReportOptions{}

	rep := NewCandidate(&RBACConfig{}, CandidateMode{FailClosedOnEmpty: true}).ReverseAccessReport("db-x", nil, false, "", opts)
	if rep.Mode != ReverseModeFailClosedEmpty {
		t.Errorf("fail-closed candidate mode = %q, want %q", rep.Mode, ReverseModeFailClosedEmpty)
	}
	if rep.Verdict != ReverseVerdictFailClosedEmpty {
		t.Errorf("fail-closed candidate verdict = %q, want %q", rep.Verdict, ReverseVerdictFailClosedEmpty)
	}

	rep = NewCandidate(&RBACConfig{}, CandidateMode{}).ReverseAccessReport("db-x", nil, false, "", opts)
	if rep.Mode != ReverseModeOpenRead {
		t.Errorf("open candidate mode = %q, want %q", rep.Mode, ReverseModeOpenRead)
	}
	if rep.Verdict != ReverseVerdictOpenRead {
		t.Errorf("open candidate verdict = %q, want %q", rep.Verdict, ReverseVerdictOpenRead)
	}
}

// ParseCandidateConfig must keep the FULL live parse pipeline — each case
// targets one stage (strict KnownFields / detectNullMatchBlocks /
// validateConfig's declared-claim-key check / the io.EOF empty-file branch).
func TestParseCandidateConfig_KeepsLivePipeline(t *testing.T) {
	cases := []struct {
		name         string
		yaml         string
		claimHeaders map[string]string
		wantErr      string // "" = must parse
	}{
		{
			name:         "unknown field rejected (strict decode)",
			yaml:         "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    mach:\n      groups: [ops]\n",
			claimHeaders: nil,
			wantErr:      "mach",
		},
		{
			name:         "null match block rejected",
			yaml:         "groups:\n  - name: ops\n    match:\n    tenants: [\"*\"]\n    permissions: [read]\n",
			claimHeaders: nil,
			wantErr:      "match",
		},
		{
			name:         "undeclared org-scope key rejected",
			yaml:         "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: org\n",
			claimHeaders: nil,
			wantErr:      "not declared in --identity-claim-headers",
		},
		{
			name:         "declared org-scope key accepted",
			yaml:         "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: org\n",
			claimHeaders: candidateClaimHeaders,
			wantErr:      "",
		},
		{
			name:         "empty input parses to the empty config",
			yaml:         "",
			claimHeaders: nil,
			wantErr:      "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg, err := ParseCandidateConfig([]byte(tc.yaml), tc.claimHeaders)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("ParseCandidateConfig returned error: %v", err)
				}
				if cfg == nil {
					t.Fatal("ParseCandidateConfig returned nil config without error")
				}
				return
			}
			if err == nil {
				t.Fatalf("ParseCandidateConfig = %+v, want error containing %q", cfg, tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("error = %q, want it to contain %q", err, tc.wantErr)
			}
		})
	}
}
