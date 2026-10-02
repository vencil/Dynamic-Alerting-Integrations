package main

// components/tenant-api/README.md "## API 參考" is the one list of the HTTP
// endpoints tenant-api serves. Its previous form omitted
// POST /api/v1/federation/accounts/backfill; this test keeps the tables honest
// against the router.
//
// Source side: the production buildRouter with every conditional route
// registered (buildRouterAllRoutes, shared with the org-gate manifests in
// routes_test.go), enumerated by chi.Walk. That is what the server serves, so
// routes without swag annotations (/health, /ready, /metrics, /tasks/{id},
// /events) need no exemption list.
//
// README side: every table row in the section. The first cell is one or more
// backticked HTTP methods separated by spaces; the second is one backticked
// path, optionally followed by a ?query example that is ignored. Any other row
// shape fails — header and separator rows excepted.
//
// Patterns are compared without chi's trailing "/" on r.Route sub-roots
// (chi reports /api/v1/tenants/{id}/; the README writes /api/v1/tenants/{id}).
//
// NOT COVERED: the 權限 and 說明 columns.

import (
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"
)

var (
	readmeMethodRe = regexp.MustCompile("^`(GET|POST|PUT|PATCH|DELETE)`$")
	readmePathRe   = regexp.MustCompile("^`(/[^`?\\s]*)(\\?[^`\\s]*)?`$")
)

func normalizeRoutePattern(p string) string {
	if len(p) > 1 {
		return strings.TrimSuffix(p, "/")
	}
	return p
}

// readmeEndpoints returns "METHOD /path" → README line number.
func readmeEndpoints(t *testing.T) map[string]int {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "README.md"))
	if err != nil {
		t.Fatalf("read README: %v", err)
	}
	lines := strings.Split(string(b), "\n")
	start := -1
	for i, l := range lines {
		if strings.TrimSpace(l) == "## API 參考" {
			start = i + 1
			break
		}
	}
	if start < 0 {
		t.Fatal("README has no \"## API 參考\" section")
	}

	out := map[string]int{}
	for i := start; i < len(lines); i++ {
		l := strings.TrimSpace(lines[i])
		if strings.HasPrefix(l, "## ") {
			break
		}
		if !strings.HasPrefix(l, "|") {
			continue
		}
		cells := strings.Split(strings.Trim(l, "|"), "|")
		first := strings.TrimSpace(cells[0])
		if first == "Method" || strings.HasPrefix(first, "---") {
			continue
		}
		if len(cells) < 2 {
			t.Fatalf("README line %d: expected Method | Path | …, got %q", i+1, l)
		}
		m := readmePathRe.FindStringSubmatch(strings.TrimSpace(cells[1]))
		if m == nil {
			t.Fatalf("README line %d: path cell %q is not one backticked path", i+1, strings.TrimSpace(cells[1]))
		}
		path := m[1]
		methods := strings.Fields(first)
		for _, tok := range methods {
			mm := readmeMethodRe.FindStringSubmatch(tok)
			if mm == nil {
				t.Fatalf("README line %d: method token %q is not a backticked HTTP method", i+1, tok)
			}
			key := mm[1] + " " + path
			if prev, dup := out[key]; dup {
				t.Fatalf("README lists %s twice (lines %d and %d)", key, prev, i+1)
			}
			out[key] = i + 1
		}
	}
	return out
}

func TestEndpointsReadmeParity(t *testing.T) {
	t.Parallel()

	src := map[string]bool{}
	walkErr := chi.Walk(buildRouterAllRoutes(t), func(method, route string, _ http.Handler, _ ...func(http.Handler) http.Handler) error {
		src[method+" "+normalizeRoutePattern(route)] = true
		return nil
	})
	if walkErr != nil {
		t.Fatalf("chi.Walk: %v", walkErr)
	}
	doc := readmeEndpoints(t)
	if len(src) < 10 || len(doc) < 10 {
		t.Fatalf("parsed too little: %d router routes, %d README rows", len(src), len(doc))
	}

	var missing, stale []string
	for k := range src {
		if _, ok := doc[k]; !ok {
			missing = append(missing, k)
		}
	}
	for k, line := range doc {
		if !src[k] {
			stale = append(stale, k+" (README line "+strconv.Itoa(line)+")")
		}
	}
	sort.Strings(missing)
	sort.Strings(stale)
	if len(missing) > 0 {
		t.Errorf("route(s) the server registers but README \"## API 參考\" does not list:\n  %s",
			strings.Join(missing, "\n  "))
	}
	if len(stale) > 0 {
		t.Errorf("README \"## API 參考\" lists route(s) the server does not register:\n  %s",
			strings.Join(stale, "\n  "))
	}
}
