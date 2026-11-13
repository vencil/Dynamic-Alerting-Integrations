package rbac

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// captureHandler records the identity object it observes on the request
// (ADR-027 §2: the bypass injects a principal, never a header).
func captureHandler(gotEmail, gotGroups *string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if p := RequestPrincipal(r); p != nil {
			*gotEmail = p.Email
			*gotGroups = strings.Join(p.Groups, ",")
		}
		w.WriteHeader(http.StatusOK)
	})
}

// bypassChain is the UDS router's order: ResolveIdentity, then the bypass.
func bypassChain(next http.Handler) http.Handler {
	return ResolveIdentity(HeaderResolver{})(DevBypassMiddleware("dev@local", "demo-admins")(next))
}

func TestDevBypassMiddleware_InjectsWhenIdentityAbsent(t *testing.T) {
	var gotEmail, gotGroups string
	h := bypassChain(captureHandler(&gotEmail, &gotGroups))

	req := httptest.NewRequest("GET", "/api/v1/me", nil) // no X-Forwarded-* headers
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if gotEmail != "dev@local" {
		t.Errorf("injected email = %q, want dev@local", gotEmail)
	}
	if gotGroups != "demo-admins" {
		t.Errorf("injected groups = %q, want demo-admins", gotGroups)
	}
	if got := rr.Header().Get("X-Dev-Auth-Bypass"); got != "active" {
		t.Errorf("tripwire header = %q, want active", got)
	}
	// The bypass sets no header: nothing downstream may read identity from one.
	if got := req.Header.Get("X-Forwarded-Email"); got != "" {
		t.Errorf("bypass set X-Forwarded-Email = %q; it must inject an identity object only", got)
	}
}

// On the TCP listener ResolveIdentity reads nothing, and a request there is
// not given the dev identity by a bypass mounted after it either way —
// the TCP router mounts neither (cmd/server/tcp_router.go). What this pins:
// a TCP-stamped request's headers never become a principal.
// adr-check: ADR-027/9.2a
func TestResolveIdentity_IgnoresHeadersOnTCP(t *testing.T) {
	var gotEmail, gotGroups string
	h := ResolveIdentity(HeaderResolver{})(captureHandler(&gotEmail, &gotGroups))
	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "forged@example.com")
	req = req.WithContext(WithListener(req.Context(), ListenerTCP))
	h.ServeHTTP(httptest.NewRecorder(), req)
	if gotEmail != "" {
		t.Errorf("TCP-stamped request resolved to %q; headers must not be read off the UDS plane", gotEmail)
	}

	gotEmail = ""
	req = httptest.NewRequest("GET", "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "real@example.com")
	req = req.WithContext(WithListener(req.Context(), ListenerUDS))
	h.ServeHTTP(httptest.NewRecorder(), req)
	if gotEmail != "real@example.com" {
		t.Errorf("UDS-stamped request resolved to %q, want real@example.com", gotEmail)
	}
}

// The email and groups headers are the ones the oauth2-proxy config names.
func TestResolveIdentity_ReadsTheConfiguredHeaderNames(t *testing.T) {
	var gotEmail, gotGroups string
	h := ResolveIdentity(HeaderResolver{EmailHeader: "X-Auth-Request-Email", GroupsHeader: "X-Auth-Request-Groups"})(
		captureHandler(&gotEmail, &gotGroups))
	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "conventional@example.com")
	req.Header.Set("X-Auth-Request-Email", "configured@example.com")
	req.Header.Set("X-Auth-Request-Groups", "ops")
	h.ServeHTTP(httptest.NewRecorder(), req)
	if gotEmail != "configured@example.com" || gotGroups != "ops" {
		t.Errorf("resolved %q / %q, want configured@example.com / ops", gotEmail, gotGroups)
	}
}

func TestDevBypassMiddleware_RespectsRealIdentity(t *testing.T) {
	var gotEmail, gotGroups string
	h := bypassChain(captureHandler(&gotEmail, &gotGroups))

	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "real@corp.example")
	req.Header.Set("X-Forwarded-Groups", "platform-admins")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	// A real forwarded identity must never be overridden.
	if gotEmail != "real@corp.example" {
		t.Errorf("email overridden to %q, want real@corp.example", gotEmail)
	}
	if gotGroups != "platform-admins" {
		t.Errorf("groups overridden to %q, want platform-admins", gotGroups)
	}
	// Tripwire still fires on every response while the bypass is mounted.
	if got := rr.Header().Get("X-Dev-Auth-Bypass"); got != "active" {
		t.Errorf("tripwire header = %q, want active (always-on while mounted)", got)
	}
}

func TestInKubernetes(t *testing.T) {
	noEnv := func(string) string { return "" }
	noFile := func(string) bool { return false }

	tests := []struct {
		name   string
		getenv func(string) string
		exists func(string) bool
		want   bool
	}{
		{"neither", noEnv, noFile, false},
		{"service host env", func(k string) string {
			if k == "KUBERNETES_SERVICE_HOST" {
				return "10.0.0.1"
			}
			return ""
		}, noFile, true},
		{"sa token mount", noEnv, func(p string) bool {
			return p == "/var/run/secrets/kubernetes.io"
		}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := InKubernetes(tt.getenv, tt.exists); got != tt.want {
				t.Errorf("InKubernetes = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestDevBypassK8sGuard_PanicsInCluster(t *testing.T) {
	inK8s := func(k string) string {
		if k == "KUBERNETES_SERVICE_HOST" {
			return "10.0.0.1"
		}
		return ""
	}
	noFile := func(string) bool { return false }

	defer func() {
		if r := recover(); r == nil {
			t.Error("DevBypassK8sGuard did not panic inside Kubernetes (poison pill failed)")
		}
	}()
	DevBypassK8sGuard(inK8s, noFile)
}

func TestDevBypassK8sGuard_NoPanicOutsideCluster(t *testing.T) {
	noEnv := func(string) string { return "" }
	noFile := func(string) bool { return false }

	defer func() {
		if r := recover(); r != nil {
			t.Errorf("DevBypassK8sGuard panicked outside Kubernetes: %v", r)
		}
	}()
	DevBypassK8sGuard(noEnv, noFile)
}
