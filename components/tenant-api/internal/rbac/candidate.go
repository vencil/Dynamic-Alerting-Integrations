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
// deployment's oauth2-proxy config (proxyconfig.Load);
// passing anything else would validate the candidate against an identity
// axis the deployment does not run.
func ParseCandidateConfig(data []byte, declaredClaimKeys map[string]string) (*RBACConfig, error) {
	return parseConfig(data, declaredClaimKeys)
}

// NewCandidate returns a Manager over an in-memory candidate snapshot for
// report generation. No file path is configured: WatchLoop and Reload are
// no-ops, LastHash is empty (a candidate report's rbac anchor is always
// AnchorUnanchored), and the snapshot never enters any live serving state.
//
// There are no evaluation-mode bits to carry over from the live manager: every
// scope axis is enforced and an empty rule set denies everything (ADR-027 §7),
// so the same bytes evaluate the same way in any deployment.
func NewCandidate(cfg *RBACConfig) *Manager {
	return &Manager{Watcher: configwatcher.NewForTest("rbac-candidate", cfg)}
}
