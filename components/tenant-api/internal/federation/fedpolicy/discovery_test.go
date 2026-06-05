package fedpolicy

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeLabelValues is a stand-in Prometheus label-values API. It records
// the last request's query parameters so tests can assert the exact
// selector the discoverer force-built, and returns a configurable name
// list (or an oversized blob to exercise the LimitReader cap).
type fakeLabelValues struct {
	data     []string // names to return on success
	status   int      // when non-zero, respond with this HTTP status
	oversize bool     // when true, flood the body past maxResponseBytes
	gotMatch string   // captured match[] selector
	gotLimit string   // captured limit
	gotPath  string   // captured request path
	gotStart string   // captured start
}

func (f *fakeLabelValues) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.gotPath = r.URL.Path
		f.gotMatch = r.URL.Query().Get("match[]")
		f.gotLimit = r.URL.Query().Get("limit")
		f.gotStart = r.URL.Query().Get("start")
		if f.status != 0 {
			w.WriteHeader(f.status)
			return
		}
		if f.oversize {
			// One huge fake metric name — larger than the 1 MiB cap.
			big := strings.Repeat("x", maxResponseBytes+1024)
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": []string{big}})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": f.data})
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestNewMetricDiscoverer_EmptyURLDisables(t *testing.T) {
	t.Parallel()
	if NewMetricDiscoverer("") != nil {
		t.Error(`NewMetricDiscoverer("") should return nil (disabled)`)
	}
	if NewMetricDiscoverer("   ") != nil {
		t.Error("NewMetricDiscoverer(blank) should return nil")
	}
	if NewMetricDiscoverer("http://prometheus:9090") == nil {
		t.Error("NewMetricDiscoverer(url) should return a discoverer")
	}
}

func TestDiscover_HitsLabelValuesAPIWithTenantMatcher(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{data: []string{"http_requests_total", "queue_depth"}}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	names, truncated, err := d.Discover(context.Background(), "db-a", "", 0)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if f.gotPath != "/api/v1/label/__name__/values" {
		t.Errorf("path = %q, want the label-values metadata API", f.gotPath)
	}
	// Cross-tenant isolation: the selector MUST pin tenant="db-a".
	if f.gotMatch != `{tenant="db-a"}` {
		t.Errorf("match[] = %q, want {tenant=\"db-a\"}", f.gotMatch)
	}
	if f.gotStart == "" {
		t.Error("expected a 24h lookback start param to be set")
	}
	// Sorted output.
	if len(names) != 2 || names[0] != "http_requests_total" || names[1] != "queue_depth" {
		t.Errorf("names = %v, want sorted [http_requests_total queue_depth]", names)
	}
	if truncated {
		t.Error("truncated = true, want false (2 names < default limit)")
	}
}

func TestDiscover_PrefixBuildsAnchoredNameMatcher(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{data: []string{"http_requests_total"}}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	if _, _, err := d.Discover(context.Background(), "db-a", "http", 0); err != nil {
		t.Fatalf("Discover: %v", err)
	}
	want := `{tenant="db-a",__name__=~"^http.*"}`
	if f.gotMatch != want {
		t.Errorf("match[] = %q, want %q", f.gotMatch, want)
	}
}

func TestDiscover_MaliciousTenantIDIsEscapedNotInjected(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{data: []string{}}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	// In RBAC open mode this crafted id reaches the discoverer. It must
	// be escaped inside the literal, NOT break out into new matchers.
	evil := `db-a"} or {job=~".+`
	if _, _, err := d.Discover(context.Background(), evil, "", 0); err != nil {
		t.Fatalf("Discover: %v", err)
	}
	// The quote is backslash-escaped, so the whole evil string stays a
	// single tenant value — no `or`, no second matcher escapes the {}.
	want := `{tenant="db-a\"} or {job=~\".+"}`
	if f.gotMatch != want {
		t.Errorf("match[] = %q\n        want %q (injection neutralised)", f.gotMatch, want)
	}
}

func TestDiscover_TruncatedWhenResultHitsLimit(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{data: []string{"a", "b", "c"}}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	names, truncated, err := d.Discover(context.Background(), "db-a", "", 3)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if f.gotLimit != "3" {
		t.Errorf("limit param = %q, want 3", f.gotLimit)
	}
	if !truncated {
		t.Error("truncated = false, want true (result count == limit)")
	}
	if len(names) != 3 {
		t.Errorf("len(names) = %d, want 3", len(names))
	}
}

func TestDiscover_DefaultLimitWhenNonPositive(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{data: []string{"a"}}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	if _, _, err := d.Discover(context.Background(), "db-a", "", -5); err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if f.gotLimit != "200" {
		t.Errorf("limit param = %q, want the default 200", f.gotLimit)
	}
}

func TestDiscover_EmptyDataReturnsNonNilSlice(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{data: nil}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	names, _, err := d.Discover(context.Background(), "db-a", "", 0)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if names == nil {
		t.Error("names should be a non-nil empty slice, not nil (JSON [] not null)")
	}
	if len(names) != 0 {
		t.Errorf("len(names) = %d, want 0", len(names))
	}
}

func TestDiscover_UpstreamNon200IsError(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{status: http.StatusInternalServerError}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	if _, _, err := d.Discover(context.Background(), "db-a", "", 0); err == nil {
		t.Error("Discover should return an error on upstream HTTP 500")
	}
}

func TestDiscover_OversizeBodyHitsLimitReaderCap(t *testing.T) {
	t.Parallel()
	f := &fakeLabelValues{oversize: true}
	srv := f.server(t)

	d := NewMetricDiscoverer(srv.URL)
	_, _, err := d.Discover(context.Background(), "db-a", "", 0)
	if err == nil {
		t.Fatal("Discover should error when the response exceeds the byte cap")
	}
	if !strings.Contains(err.Error(), "byte cap") {
		t.Errorf("error = %q, want a byte-cap message", err.Error())
	}
}
