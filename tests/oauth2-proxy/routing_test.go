package oauth2proxy

// ADR-027 §9 item 9, second half: the routing ADR-027 §10 documents is what
// oauth2-proxy's own router does with the §10 config. The test reads the
// YAML block and the routing table out of the ADR itself, so the ADR cannot
// drift from what the library does without this test failing.

import (
	"bufio"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

const adrPath = "docs/adr/027-tenant-api-caller-identity.md"

// adrRoute is one row of the §10 routing table: a request path and the
// upstream id ("api" or "portal") the ADR says it reaches.
type adrRoute struct {
	Path, Upstream string
}

// section10 returns the YAML block and the routing table of ADR-027 §10.
func section10(t *testing.T) (yamlBlock string, routes []adrRoute) {
	t.Helper()
	f, err := os.Open(filepath.Join(repoRoot(t), adrPath))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	var (
		in, inYAML bool
		block      strings.Builder
		backticked = regexp.MustCompile("`([^`]+)`")
	)
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		switch {
		case strings.HasPrefix(line, "### 10."):
			in = true
			continue
		case in && (strings.HasPrefix(line, "## ") || strings.HasPrefix(line, "### ")):
			in = false
		}
		if !in {
			continue
		}
		switch {
		case line == "```yaml" && block.Len() == 0:
			inYAML = true
		case inYAML && line == "```":
			inYAML = false
		case inYAML:
			block.WriteString(line + "\n")
		case strings.HasPrefix(line, "|") && !strings.HasPrefix(line, "| `"):
			// Only the header row and the separator row may skip parsing; any
			// other table row must be a backticked path, or a reshaped row
			// would silently drop out of the routes checked.
			if !strings.HasPrefix(line, "|---") && !strings.Contains(line, "請求路徑") {
				t.Fatalf("ADR §10 table row %q: not a header, separator or `path` row", line)
			}
		case strings.HasPrefix(line, "| `"):
			cells := strings.Split(line, "|")
			if len(cells) < 3 {
				t.Fatalf("ADR §10 table row %q: want two cells", line)
			}
			dest := strings.TrimSpace(cells[2])
			var up string
			switch {
			case strings.HasPrefix(dest, "UDS"):
				up = "api"
			case strings.HasPrefix(dest, "portal"):
				up = "portal"
			default:
				t.Fatalf("ADR §10 table row %q: destination %q is neither UDS nor portal", line, dest)
			}
			for _, m := range backticked.FindAllStringSubmatch(cells[1], -1) {
				routes = append(routes, adrRoute{Path: m[1], Upstream: up})
			}
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	// Vacuity guards: a renamed heading or a reshaped table must fail here,
	// not leave nothing to check.
	if block.Len() == 0 {
		t.Fatalf("%s §10: no ```yaml block found", adrPath)
	}
	if len(routes) < 4 {
		t.Fatalf("%s §10: found %d table paths, want the whole routing table", adrPath, len(routes))
	}
	return block.String(), routes
}

// adr-check: ADR-027/9.9b
func TestADRSection10Routing(t *testing.T) {
	t.Parallel()
	block, routes := section10(t)
	path := filepath.Join(t.TempDir(), "section10.yaml")
	if err := os.WriteFile(path, []byte(block), 0o600); err != nil {
		t.Fatal(err)
	}
	opts, err := loadAlpha(path)
	if err != nil {
		t.Fatalf("oauth2-proxy refuses the ADR §10 config: %v\n%s", err, block)
	}
	ids := map[string]bool{}
	for _, u := range opts.UpstreamServers.Upstreams {
		ids[u.ID] = true
	}
	if !ids["api"] || !ids["portal"] || len(ids) != 2 {
		t.Fatalf("ADR §10 config upstreams = %v, want exactly api and portal", ids)
	}

	rec := &recorder{}
	liveUpstreams(t, &opts.UpstreamServers, rec)
	h := proxyFor(t, opts, nil)
	for _, r := range routes {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, r.Path, nil))
		got := rec.take()
		if len(got) != 1 {
			t.Errorf("GET %s: %d upstream requests (status %d), want 1", r.Path, len(got), w.Code)
			continue
		}
		if got[0].Upstream != r.Upstream {
			t.Errorf("GET %s reached %q, ADR §10 says %q", r.Path, got[0].Upstream, r.Upstream)
		}
		// The table's first row says path and query arrive unchanged; that
		// holds for every row, so assert it for all of them.
		if got[0].URI != r.Path {
			t.Errorf("GET %s: upstream saw %q, want it unchanged", r.Path, got[0].URI)
		}
	}
}
