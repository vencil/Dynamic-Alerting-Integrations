package rbac

// Candidate-config seams for the P7 what-if dry-run (ADR-027 / LD-6 P7).
// A candidate is an operator-supplied _rbac.yaml that must be evaluated with
// EXACTLY the live pipeline — same strict parse, same validation, same
// ReverseAccessReport semantics — without ever being installed. Both seams
// are additive: no live-path code is touched, so a candidate evaluation can
// never leak into the serving snapshot.

import "github.com/vencil/tenant-api/internal/configwatcher"

// ParseCandidateConfig parses candidate _rbac.yaml bytes through the SAME
// pipeline the live loader uses (parseConfig: strict KnownFields decode +
// detectNullMatchBlocks + validateConfig against declaredClaimKeys). A
// candidate that this function accepts is byte-for-byte the config a
// hot-reload would accept, and one it rejects would be rejected there too —
// the fidelity the dry-run's verdict depends on. declaredClaimKeys is the
// deployment's --identity-claim-headers declaration (ParseClaimHeaders);
// passing anything else would validate the candidate against an identity
// axis the deployment does not run.
func ParseCandidateConfig(data []byte, declaredClaimKeys map[string]string) (*RBACConfig, error) {
	return parseConfig(data, declaredClaimKeys)
}

// CandidateMode carries the live manager's evaluation-mode bits into a
// candidate snapshot so the report reflects what the bytes would do once
// deployed HERE, not under zero-value defaults. Every mode field that
// ReverseAccessReport reads MUST be listed: one left out silently reverts to
// its zero value in candidate reports. That is not hypothetical — the
// enforce bits surface in ReverseFlags with a "runtime" provenance, so an
// enforce-enabled deployment would otherwise emit a candidate side claiming
// SHADOW while the baseline claims ENFORCE, a false "not comparable" signal
// against the flags-parity contract (P7 dry-run diff, definition 11).
type CandidateMode struct {
	// FailClosedOnEmpty gates the empty-config verdict (fail_closed_empty vs
	// open_read); a candidate under the wrong bit reports the opposite verdict.
	FailClosedOnEmpty bool
	// MetadataScopeEnforce / OrgScopeEnforce are the per-axis fail-mode flags
	// echoed verbatim (source: runtime) in ReverseFlags.
	MetadataScopeEnforce bool
	OrgScopeEnforce      bool
}

// EvaluationMode snapshots the live manager's mode bits for NewCandidate. It
// is the ONLY supported way to build a fidelity-preserving candidate: cloning
// the whole mode struct (not a hand-picked subset) is what keeps a future mode
// flag from silently defaulting to zero in dry-run reports.
func (m *Manager) EvaluationMode() CandidateMode {
	return CandidateMode{
		FailClosedOnEmpty:    m.failClosedOnEmpty,
		MetadataScopeEnforce: m.metadataScopeEnforce,
		OrgScopeEnforce:      m.orgScopeEnforce,
	}
}

// NewCandidate returns a Manager over an in-memory candidate snapshot for
// report generation. No file path is configured: WatchLoop and Reload are
// no-ops, LastHash is empty (a candidate report's rbac anchor is always
// AnchorUnanchored), and the snapshot never enters any live serving state.
//
// mode must be the LIVE manager's EvaluationMode(): the empty-config verdict
// and the enforce flags are mode-dependent, and a candidate evaluated under
// the wrong mode would misreport what the same bytes produce once deployed.
func NewCandidate(cfg *RBACConfig, mode CandidateMode) *Manager {
	return &Manager{
		Watcher:              configwatcher.NewForTest("rbac-candidate", cfg),
		failClosedOnEmpty:    mode.FailClosedOnEmpty,
		metadataScopeEnforce: mode.MetadataScopeEnforce,
		orgScopeEnforce:      mode.OrgScopeEnforce,
	}
}
