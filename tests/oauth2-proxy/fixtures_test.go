package oauth2proxy

// What oauth2-proxy itself does with the files tenant-api refuses
// (components/tenant-api/internal/proxyconfig/testdata, ADR-027 §9 item 6).
// tenant-api refuses some of them only because oauth2-proxy reads them
// differently from what they appear to say; this pins that the proxy really
// does, so each refusal guards against a real divergence and not an
// imagined one.

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/apis/options"
	"github.com/oauth2-proxy/oauth2-proxy/v7/pkg/apis/sessions"
)

func fixture(t *testing.T, name string) string {
	t.Helper()
	return filepath.Join(repoRoot(t), "components", "tenant-api", "internal", "proxyconfig", "testdata", name)
}

// injected runs one request through the file's header injection (with a
// client copy of every header the test names) and returns what the upstream
// receives.
func injected(t *testing.T, opts *options.Options, clientHeaders map[string]string) http.Header {
	t.Helper()
	opts.UpstreamServers = options.UpstreamConfig{Upstreams: []options.Upstream{{ID: "u", Path: "/", URI: "http://unused"}}}
	rec := &recorder{}
	liveUpstreams(t, &opts.UpstreamServers, rec)
	h := proxyFor(t, opts, &sessions.SessionState{Email: "alice@example.com", Groups: []string{"ops"}})
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	for k, v := range clientHeaders {
		req.Header.Set(k, v)
	}
	h.ServeHTTP(httptest.NewRecorder(), req)
	got := rec.take()
	if len(got) != 1 {
		t.Fatalf("upstream got %d requests", len(got))
	}
	return got[0].Header
}

func TestFixtures_TheChartShapeLoads(t *testing.T) {
	t.Parallel()
	if _, err := loadAlpha(fixture(t, "valid.yaml")); err != nil {
		t.Fatalf("oauth2-proxy refuses the file tenant-api accepts: %v", err)
	}
}

// A field name in a non-standard spelling is read by oauth2-proxy (it matches
// names ignoring case), so the headers ARE injected: a reader matching names
// exactly would see no injectRequestHeaders at all.
func TestFixtures_NonStandardSpellingIsReadByTheProxy(t *testing.T) {
	t.Parallel()
	opts, err := loadAlpha(fixture(t, "nonstandard-field.yaml"))
	if err != nil {
		t.Fatalf("LoadYAML: %v", err)
	}
	if n := len(opts.InjectRequestHeaders); n != 2 {
		t.Fatalf("oauth2-proxy read %d injected headers from InjectRequestHeaders, want 2", n)
	}
	opts, err = loadAlpha(fixture(t, "nonstandard-nested-field.yaml"))
	if err != nil {
		t.Fatalf("LoadYAML: %v", err)
	}
	hdr := injected(t, opts, map[string]string{"X-Forwarded-Groups": "platform-admins"})
	if got := hdr.Values("X-Forwarded-Groups"); len(got) != 1 || got[0] != "platform-admins,ops" {
		t.Errorf("PreserveRequestValue (non-standard case) kept the client value? groups = %q, want the client's value kept", got)
	}
}

// An environment reference is expanded before parsing: the claim the file
// seems to name is not the claim the proxy injects.
func TestFixtures_EnvReferenceIsExpandedByTheProxy(t *testing.T) {
	t.Setenv("GROUPS_CLAIM", "email")
	opts, err := loadAlpha(fixture(t, "env-reference.yaml"))
	if err != nil {
		t.Fatalf("LoadYAML: %v", err)
	}
	hdr := injected(t, opts, nil)
	if got := hdr.Get("X-Forwarded-Groups"); got != "alice@example.com" {
		t.Errorf("X-Forwarded-Groups = %q, want the email the expanded reference names", got)
	}
}

// preserveRequestValue: true keeps the client's copy beside the injected one.
func TestFixtures_PreserveRequestValueKeepsTheClientCopy(t *testing.T) {
	t.Parallel()
	opts, err := loadAlpha(fixture(t, "preserve-request-value.yaml"))
	if err != nil {
		t.Fatalf("LoadYAML: %v", err)
	}
	hdr := injected(t, opts, map[string]string{"X-Forwarded-Email": "mallory@example.com"})
	if got := hdr.Get("X-Forwarded-Email"); got != "mallory@example.com,alice@example.com" {
		t.Errorf("X-Forwarded-Email = %q, want the client copy first", got)
	}
}
