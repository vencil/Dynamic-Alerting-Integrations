package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"

	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/rbac"
)

// fakeMetricsProm is a stand-in Prometheus label-values API for handler
// tests. It records the match[] selector so cross-tenant isolation can
// be asserted at the HTTP layer, and returns a fixed name list.
func fakeMetricsProm(t *testing.T, capture *string, data []string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if capture != nil {
			*capture = r.URL.Query().Get("match[]")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": data})
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestDiscoverMetrics_DisabledReturns503(t *testing.T) {
	t.Parallel()
	h := DiscoverMetrics(&Deps{MetricDiscoverer: nil})
	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/metrics", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 when discovery disabled; body: %s", w.Code, w.Body.String())
	}
}

func TestDiscoverMetrics_BadQCharsetReturns400(t *testing.T) {
	t.Parallel()
	srv := fakeMetricsProm(t, nil, []string{"m"})
	deps := &Deps{MetricDiscoverer: fedpolicy.NewMetricDiscoverer(srv.URL)}
	h := DiscoverMetrics(deps)

	for _, bad := range []string{`a"b`, `a.*`, `a{b`, `a b`, `a\b`} {
		req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/metrics", "id", "db-a", nil)
		// Encode the bad value so it survives the URL but decodes back to
		// the raw injection attempt the handler must reject.
		req.URL.RawQuery = url.Values{"q": {bad}}.Encode()
		w := httptest.NewRecorder()
		h(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("q=%q: status = %d, want 400 (injection charset)", bad, w.Code)
		}
	}
}

func TestDiscoverMetrics_OverlongQReturns400(t *testing.T) {
	t.Parallel()
	srv := fakeMetricsProm(t, nil, []string{"m"})
	deps := &Deps{MetricDiscoverer: fedpolicy.NewMetricDiscoverer(srv.URL)}
	h := DiscoverMetrics(deps)

	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/metrics", "id", "db-a", nil)
	req.URL.RawQuery = url.Values{"q": {strings.Repeat("a", 257)}}.Encode()
	w := httptest.NewRecorder()
	h(w, req)
	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for an over-long prefix", w.Code)
	}
}

func TestDiscoverMetrics_SuccessAndTenantIsolation(t *testing.T) {
	t.Parallel()
	var gotMatch string
	srv := fakeMetricsProm(t, &gotMatch, []string{"queue_depth", "http_requests_total"})
	deps := &Deps{MetricDiscoverer: fedpolicy.NewMetricDiscoverer(srv.URL)}
	h := DiscoverMetrics(deps)

	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/metrics?q=queue", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	// The selector MUST pin the path tenant — the handler cannot be
	// coaxed into listing another tenant's metrics.
	if !strings.Contains(gotMatch, `tenant="db-a"`) {
		t.Errorf("selector = %q, must pin tenant=\"db-a\"", gotMatch)
	}
	if !strings.Contains(gotMatch, `__name__=~"^queue.*"`) {
		t.Errorf("selector = %q, must carry the prefix matcher", gotMatch)
	}

	var resp DiscoverMetricsResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// Sorted.
	if len(resp.Metrics) != 2 || resp.Metrics[0] != "http_requests_total" {
		t.Errorf("metrics = %v, want sorted", resp.Metrics)
	}
}

func TestDiscoverMetrics_EmptyQListsAll(t *testing.T) {
	t.Parallel()
	var gotMatch string
	srv := fakeMetricsProm(t, &gotMatch, []string{"m"})
	deps := &Deps{MetricDiscoverer: fedpolicy.NewMetricDiscoverer(srv.URL)}
	h := DiscoverMetrics(deps)

	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/metrics", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	// No prefix → bare tenant matcher, no __name__ regex.
	if gotMatch != `{tenant="db-a"}` {
		t.Errorf("selector = %q, want bare {tenant=\"db-a\"} when q empty", gotMatch)
	}
}

// TestFullRouter_DiscoverMetrics_RBACBlocksCrossTenant proves the
// route-middleware layer: a caller whose groups grant read on db-a only
// gets 403 when probing db-b's metrics — the tenant-isolation guarantee
// is enforced before the handler runs.
func TestFullRouter_DiscoverMetrics_RBACBlocksCrossTenant(t *testing.T) {
	t.Parallel()
	srv := fakeMetricsProm(t, nil, []string{"m"})
	deps := &Deps{MetricDiscoverer: fedpolicy.NewMetricDiscoverer(srv.URL)}
	rbacMgr := newRBACManager(t, `groups:
  - name: db-a-team
    tenants: ["db-a"]
    permissions: [read]
`)
	r := chi.NewRouter()
	r.Route("/api/v1/tenants/{id}", func(r chi.Router) {
		r.With(rbacMgr.Middleware(rbac.PermRead, TenantIDFromPath)).
			Get("/metrics", DiscoverMetrics(deps))
	})

	// Authorised: db-a → 200.
	reqOK := httptest.NewRequest("GET", "/api/v1/tenants/db-a/metrics", nil)
	reqOK.Header.Set("X-Forwarded-Email", "alice@example.com")
	reqOK.Header.Set("X-Forwarded-Groups", "db-a-team")
	wOK := httptest.NewRecorder()
	r.ServeHTTP(wOK, reqOK)
	if wOK.Code != http.StatusOK {
		t.Fatalf("db-a status = %d, want 200; body: %s", wOK.Code, wOK.Body.String())
	}

	// Cross-tenant: db-b → 403 (no read on db-b).
	reqNo := httptest.NewRequest("GET", "/api/v1/tenants/db-b/metrics", nil)
	reqNo.Header.Set("X-Forwarded-Email", "alice@example.com")
	reqNo.Header.Set("X-Forwarded-Groups", "db-a-team")
	wNo := httptest.NewRecorder()
	r.ServeHTTP(wNo, reqNo)
	if wNo.Code != http.StatusForbidden {
		t.Fatalf("db-b status = %d, want 403 (RBAC blocks cross-tenant)", wNo.Code)
	}
}
