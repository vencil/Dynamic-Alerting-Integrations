package handler

// Domain policy availability (hub #2486 Q7-2 甲軸). In direct write mode a
// `_domain_policy.yaml` / `.yml` that is present but unusable, with no last
// good version, makes the writes the policy judges answer 503
// POLICY_UNAVAILABLE (policy.Manager.CheckAvailable). /ready stays 200 on
// purpose — the chart runs one replica with `strategy: Recreate`, so a
// NotReady pod would take every endpoint down, reads included — so the state
// is exposed here instead:
//
//   - tenant_api_policy_available — 1 when every present policy file is
//     usable or served from its last good version, 0 otherwise. Read live
//     from the manager at scrape time; 1 when no manager is installed.
//   - tenant_api_policy_unavailable_open_total — writes let through an
//     unavailable policy under --policy-unavailable-open (monotonic counter).

import (
	"fmt"
	"io"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/policy"
)

var activePolicyAvailability atomic.Pointer[policy.Manager]

// SetPolicyAvailabilitySource installs the policy manager /metrics reads the
// two families above from. Called once at startup.
func SetPolicyAvailabilitySource(m *policy.Manager) { activePolicyAvailability.Store(m) }

func writePolicyAvailabilityMetrics(w io.Writer) {
	available, passes := 1, int64(0)
	if m := activePolicyAvailability.Load(); m != nil {
		if m.Unavailable() != nil {
			available = 0
		}
		passes = m.OpenPasses()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_policy_available Whether every present domain policy file (_domain_policy.yaml / .yml) is usable or served from its last good version (1), or one is present but unusable with no last good version (0): direct-mode writes the policy judges then answer 503 POLICY_UNAVAILABLE. Alert on == 0.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_policy_available gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_policy_available %d\n", available)
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_policy_unavailable_open_total Writes let through an unavailable domain policy under --policy-unavailable-open (fail-closed disabled). Should stay 0.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_policy_unavailable_open_total counter\n")
	_, _ = fmt.Fprintf(w, "tenant_api_policy_unavailable_open_total %d\n", passes)
}
