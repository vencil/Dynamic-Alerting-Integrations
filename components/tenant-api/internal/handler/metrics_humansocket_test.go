package handler

import (
	"net/http/httptest"
	"strings"
	"testing"
)

// renderMetrics runs MetricsHandler and returns the exposition body.
func renderMetrics(t *testing.T) string {
	t.Helper()
	w := httptest.NewRecorder()
	MetricsHandler(w, httptest.NewRequest("GET", "/metrics", nil))
	return w.Body.String()
}

// The human-socket gauge is present ONLY when --human-socket is configured, and
// carries the current up/down value. When NOT configured it must be absent
// entirely (a 0 for a non-existent plane would be a false alarm). NOT parallel:
// mutates the process-global humanSocketConfigured/Up state and restores it.
func TestMetrics_HumanSocketGauge_PresenceAndValue(t *testing.T) {
	prevCfg := humanSocketConfigured.Load()
	prevUp := humanSocketUp.Load()
	t.Cleanup(func() {
		humanSocketConfigured.Store(prevCfg)
		humanSocketUp.Store(prevUp)
	})

	// Not configured → gauge omitted.
	SetHumanSocketConfigured(false)
	if body := renderMetrics(t); strings.Contains(body, "tenant_api_human_socket_up") {
		t.Errorf("gauge present while human socket unconfigured — must be omitted\nbody:\n%s", body)
	}

	// Configured + up → gauge == 1 with HELP/TYPE lines.
	SetHumanSocketConfigured(true)
	SetHumanSocketUp(true)
	body := renderMetrics(t)
	for _, want := range []string{
		"# HELP tenant_api_human_socket_up",
		"# TYPE tenant_api_human_socket_up gauge",
		"tenant_api_human_socket_up 1",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("configured+up metrics missing %q\nbody:\n%s", want, body)
		}
	}

	// Configured + down → gauge == 0.
	SetHumanSocketUp(false)
	if body := renderMetrics(t); !strings.Contains(body, "tenant_api_human_socket_up 0") {
		t.Errorf("configured+down must render gauge 0\nbody:\n%s", body)
	}
}
