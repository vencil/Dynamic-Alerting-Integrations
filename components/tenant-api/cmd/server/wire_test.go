package main

import (
	"strings"
	"testing"
)

// wireMachineAuditor must fail loud — not fall through to "no audit" — when the
// audit is enabled with an empty or whitespace-only audience. This is the G4
// Go-layer guard (ADR-027): it holds even for raw-manifest / bare-binary deploys
// that the Helm `required` gate never sees, and it catches whitespace the Helm
// gate does not. The check runs BEFORE any in-cluster client build, so this test
// needs no cluster.
func TestWireMachineAuditor_EmptyAudienceFailsLoud(t *testing.T) {
	for _, aud := range []string{"", "   ", "\t"} {
		got, err := wireMachineAuditor(machineAuditorFlags{Enabled: true, Audience: aud})
		if err == nil {
			t.Errorf("wireMachineAuditor(enabled, audience=%q) = nil error, want a G4 non-empty-audience error", aud)
		} else if !strings.Contains(err.Error(), "audience") {
			t.Errorf("error = %q, want it to name the audience gate", err.Error())
		}
		if got != nil {
			t.Errorf("audience=%q returned a non-nil auditor alongside the error; must be nil (never wire a no-audience auditor)", aud)
		}
	}
}

// Disabled audit is a clean no-op: (nil, nil) before touching Kubernetes, so a
// deployment that does not opt in needs neither an in-cluster client nor the
// tokenreviews RBAC — and an empty audience is irrelevant when disabled.
func TestWireMachineAuditor_DisabledIsNoop(t *testing.T) {
	got, err := wireMachineAuditor(machineAuditorFlags{Enabled: false, Audience: ""})
	if err != nil {
		t.Errorf("disabled wireMachineAuditor returned error %v, want nil", err)
	}
	if got != nil {
		t.Errorf("disabled wireMachineAuditor returned a non-nil auditor %v, want nil", got)
	}
}
