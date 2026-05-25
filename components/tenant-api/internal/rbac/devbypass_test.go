package rbac

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// captureHandler records the identity headers it observes on the request.
func captureHandler(gotEmail, gotGroups *string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*gotEmail = r.Header.Get("X-Forwarded-Email")
		*gotGroups = r.Header.Get("X-Forwarded-Groups")
		w.WriteHeader(http.StatusOK)
	})
}

func TestDevBypassMiddleware_InjectsWhenIdentityAbsent(t *testing.T) {
	var gotEmail, gotGroups string
	h := DevBypassMiddleware("dev@local", "demo-admins")(captureHandler(&gotEmail, &gotGroups))

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
}

func TestDevBypassMiddleware_RespectsRealIdentity(t *testing.T) {
	var gotEmail, gotGroups string
	h := DevBypassMiddleware("dev@local", "demo-admins")(captureHandler(&gotEmail, &gotGroups))

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
