package main

// ADR-027 §9 item 1: the TCP listener registers only /health, /ready and
// /metrics (matched exactly), answers every other path 404, and answers a
// request carrying any identity header 400.

import (
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/proxyconfig"
	"github.com/vencil/tenant-api/internal/rbac"
)

// tcpTestIdentity is a config whose email header is NOT the conventional
// name and which injects one extra claim, so the 400 is shown to cover the
// file's names, the conventional names and the claim header alike.
var tcpTestIdentity = proxyconfig.Identity{
	EmailHeader:  "X-Auth-Request-Email",
	GroupsHeader: "X-Forwarded-Groups",
	ClaimHeaders: map[string]string{"org": "X-Org"},
}

func newTCPRouter(t *testing.T) *chi.Mux {
	t.Helper()
	mgr, err := rbac.NewManager("", nil)
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}
	return buildTCPRouter(&handler.Deps{RBAC: mgr, ConfigDir: t.TempDir()}, tcpTestIdentity)
}

func serveTCP(t *testing.T, r http.Handler, method, path string, hdr map[string]string) int {
	t.Helper()
	req := httptest.NewRequest(method, path, nil)
	req = req.WithContext(rbac.WithListener(req.Context(), rbac.ListenerTCP))
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w.Code
}

// adr-check: ADR-027/9.1a
func TestTCPRouter_RegistersOnlyTheProbeRoutes(t *testing.T) {
	t.Parallel()
	var got []string
	if err := chi.Walk(newTCPRouter(t), func(method, route string, _ http.Handler, _ ...func(http.Handler) http.Handler) error {
		got = append(got, method+" "+route)
		return nil
	}); err != nil {
		t.Fatalf("chi.Walk: %v", err)
	}
	sort.Strings(got)
	want := []string{"GET /health", "GET /metrics", "GET /ready"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("TCP routes = %v, want exactly %v (ADR-027 §3)", got, want)
	}
}

// adr-check: ADR-027/9.1b
func TestTCPRouter_EverythingElseIs404(t *testing.T) {
	t.Parallel()
	r := newTCPRouter(t)
	for _, path := range []string{
		"/", "/api/v1/me", "/api/v1/tenants", "/api/v1/health", "/api/health",
		"/health/", "/ready/", "/metrics/", "/healthz", "/metrics/x", "//health",
	} {
		if code := serveTCP(t, r, http.MethodGet, path, nil); code != http.StatusNotFound {
			t.Errorf("GET %s on TCP = %d, want 404", path, code)
		}
	}
	if code := serveTCP(t, r, http.MethodGet, "/health", nil); code != http.StatusOK {
		t.Errorf("GET /health on TCP = %d, want 200", code)
	}
}

// adr-check: ADR-027/9.1c
func TestTCPRouter_IdentityHeaderIs400(t *testing.T) {
	t.Parallel()
	r := newTCPRouter(t)
	for _, h := range []string{
		"X-Forwarded-Email", "X-Forwarded-Groups", // conventional names
		"X-Auth-Request-Email", // the file's email header
		"X-Org",                // the file's claim header
		"x-forwarded-email",    // any case
	} {
		for _, path := range []string{"/health", "/metrics", "/api/v1/me", "/nope"} {
			if code := serveTCP(t, r, http.MethodGet, path, map[string]string{h: "someone@example.com"}); code != http.StatusBadRequest {
				t.Errorf("GET %s with %s on TCP = %d, want 400", path, h, code)
			}
		}
	}
	// Present but empty still counts: the forwarding hop is the misconfiguration.
	if code := serveTCP(t, r, http.MethodGet, "/health", map[string]string{"X-Forwarded-Email": ""}); code != http.StatusBadRequest {
		t.Errorf("GET /health with an empty X-Forwarded-Email on TCP = %d, want 400", code)
	}
}

func TestIdentityHeaderNames(t *testing.T) {
	t.Parallel()
	got := identityHeaderNames(tcpTestIdentity)
	want := []string{"X-Auth-Request-Email", "X-Forwarded-Email", "X-Forwarded-Groups", "X-Org"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("identityHeaderNames = %v, want %v", got, want)
	}
}

// adr-check: ADR-027/9.3d
func TestCheckIdentityPlaneFlags(t *testing.T) {
	t.Parallel()
	if err := checkIdentityPlaneFlags("/run/ta-human/human.sock", "/etc/oauth2-proxy/oauth2-proxy.yaml"); err != nil {
		t.Errorf("both set: %v", err)
	}
	for _, c := range []struct{ sock, cfg, want string }{
		{"", "/etc/o.yaml", "--human-socket"},
		{"  ", "/etc/o.yaml", "--human-socket"},
		{"/run/s.sock", "", "--oauth2-proxy-config"},
	} {
		err := checkIdentityPlaneFlags(c.sock, c.cfg)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("checkIdentityPlaneFlags(%q, %q) = %v, want an error naming %s", c.sock, c.cfg, err, c.want)
		}
	}
}

func TestCheckDevBypassEmail(t *testing.T) {
	t.Parallel()
	if err := checkDevBypassEmail("dev@local"); err != nil {
		t.Errorf("dev@local: %v", err)
	}
	for _, e := range []string{"", "  "} {
		if err := checkDevBypassEmail(e); err == nil || !strings.Contains(err.Error(), "--dev-bypass-email") {
			t.Errorf("checkDevBypassEmail(%q) = %v, want an error naming --dev-bypass-email", e, err)
		}
	}
}

// /ready and /metrics are bound to their own handlers, not /health's: /ready
// reports readiness, /metrics the Prometheus exposition.
func TestTCPRouter_ReadyAndMetricsAnswerWithTheirOwnHandlers(t *testing.T) {
	t.Parallel()
	r := newTCPRouter(t)
	for path, want := range map[string]string{
		"/health":  `"status":"ok"`,
		"/ready":   `"status":"ready"`,
		"/metrics": "tenant_api_",
	} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req = req.WithContext(rbac.WithListener(req.Context(), rbac.ListenerTCP))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), want) {
			t.Errorf("GET %s on TCP = %d %.120q, want 200 with %q", path, w.Code, w.Body.String(), want)
		}
	}
}

// A probe path with another method is 405 with an Allow header (chi), not 404.
func TestTCPRouter_WrongMethodIs405(t *testing.T) {
	t.Parallel()
	r := newTCPRouter(t)
	req := httptest.NewRequest(http.MethodPost, "/health", nil)
	req = req.WithContext(rbac.WithListener(req.Context(), rbac.ListenerTCP))
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusMethodNotAllowed || !strings.Contains(w.Header().Get("Allow"), http.MethodGet) {
		t.Errorf("POST /health on TCP = %d (Allow %q), want 405 with Allow: GET", w.Code, w.Header().Get("Allow"))
	}
}
