package config

// merge_tenant_platform_test.go — the tenant-api merge core
// (MergeTenantWithRootDefaults / MergeParsedTenantWithRootDefaults: GET
// /tenants/{id}, POST …/validate and the write gate) reads the ROOT platform
// files' per-tenant `tenants:` layer the way /metrics does (#2208).
//
// The oracle is /metrics itself: LoadDir (ScanDirTree → BuildFlatConfig, the
// flat plane) resolved at one instant. Each tree is compared row for row, so a
// divergence names the row, not just "different".
//
// What these trees pin, beyond "the platform value shows up": the platform
// file cannot create a tenant, a null on either side means what the flat
// merge means, several platform files apply in sort order, and every
// file-selection rule (hidden, `.yml`, case, a file the flat decode rejects,
// an unselected root carrier) is the flat plane's.
//
// ⚠️ ONE RULE IS NOT /metrics': `_metadata`. /metrics (ResolveMetadata) takes
// a platform entry's `_metadata` for the tenant; this core follows the walker
// plane (/effective, overlayTenant) and does not inherit it. That is a
// deliberate divergence, and the `_metadata` tree below pins it against the
// WALKER rule — its threshold rows still match /metrics, its metadata does
// not, and the test asserts both halves. GET serves no metadata field, so the
// divergence is not visible to a client today.
//
// Seams: none — t.TempDir() trees; LoadDir discards its log.

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

var platformMergeNow = time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)

// rowResolver is what both a flat config and a tenant merge answer.
type rowResolver interface {
	ResolveAt(now time.Time) []ResolvedThreshold
}

// resolvedRows renders tenantID's resolved thresholds as sorted strings.
func resolvedRows(c rowResolver, tenantID string) []string {
	var out []string
	for _, r := range c.ResolveAt(platformMergeNow) {
		if r.Tenant != tenantID {
			continue
		}
		var labels []string
		for k, v := range r.CustomLabels {
			labels = append(labels, k+"="+v)
		}
		for k, v := range r.RegexLabels {
			labels = append(labels, k+"=~"+v)
		}
		sort.Strings(labels)
		out = append(out, fmt.Sprintf("%s_%s{%s}=%g/%s", r.Component, r.Metric, strings.Join(labels, ","), r.Value, r.Severity))
	}
	sort.Strings(out)
	return out
}

func writeMergeTree(t testing.TB, dir string, files map[string]string) {
	t.Helper()
	for name, body := range files {
		rootWrite(t, filepath.Join(dir, filepath.FromSlash(name)), body)
	}
}

const platformMergeDefaults = "defaults:\n  mysql_connections: 80\n  redis_memory: 70\n  pg_connections: 100\noptional_overrides: [oracle_wait]\n"

// platformMergeTrees are the oracle trees. Every one has tenant tx in
// tx.yaml (the file GET would read), a root `_defaults.yaml`, and whatever
// the case adds.
func platformMergeTrees() []struct {
	name  string
	files map[string]string
	// wantDiffersFromMain marks the trees where main (no platform layer)
	// served something else — so the table is not a set of controls only.
	wantDiffersFromMain bool
} {
	withDefaults := func(extra map[string]string) map[string]string {
		m := map[string]string{"_defaults.yaml": platformMergeDefaults}
		for k, v := range extra {
			m[k] = v
		}
		return m
	}
	tenant := "tenants:\n  tx:\n    mysql_connections: \"90\"\n"
	return []struct {
		name                string
		files               map[string]string
		wantDiffersFromMain bool
	}{
		{"platform file sets a key the tenant does not write", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"66\"\n    mysql_connections: \"11\"\n",
			"tx.yaml":        tenant}), true},
		{"the carrier's own tenants block", map[string]string{
			"_defaults.yaml": platformMergeDefaults + "tenants:\n  tx:\n    redis_memory: \"65\"\n",
			"tx.yaml":        tenant}, true},
		{"platform _metadata is not inherited (walker rule; /metrics differs)", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: platform-team\n    redis_memory: \"64\"\n",
			"tx.yaml":        tenant}), true},
		{"platform null on a threshold key", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    redis_memory: null\n    pg_connections: \"63\"\n",
			"tx.yaml":        tenant}), true},
		{"tenant null over a platform value", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"62\"\n",
			"tx.yaml":        "tenants:\n  tx:\n    redis_memory: null\n"}), false},
		{"two platform files: the later one wins key by key", withDefaults(map[string]string{
			"_a.yaml": "tenants:\n  tx:\n    redis_memory: \"61\"\n    pg_connections: \"160\"\n",
			"_b.yaml": "tenants:\n  tx:\n    redis_memory: \"60\"\n",
			"tx.yaml": tenant}), true},
		{"a platform file the flat decode rejects contributes nothing", withDefaults(map[string]string{
			"_bad.yaml": "defaults:\n  x: abc\ntenants:\n  tx:\n    redis_memory: \"59\"\n",
			"tx.yaml":   tenant}), false},
		{"a hidden platform file is not read", withDefaults(map[string]string{
			"._platform.yaml": "tenants:\n  tx:\n    redis_memory: \"58\"\n",
			"tx.yaml":         tenant}), false},
		{"a .yml platform file", withDefaults(map[string]string{
			"_platform.yml": "tenants:\n  tx:\n    redis_memory: \"57\"\n",
			"tx.yaml":       tenant}), true},
		{"an upper-case platform file", withDefaults(map[string]string{
			"_PLATFORM.YAML": "tenants:\n  tx:\n    redis_memory: \"56\"\n",
			"tx.yaml":        tenant}), true},
		{"an unselected root carrier supplies nothing", withDefaults(map[string]string{
			"_defaults.yml": "tenants:\n  tx:\n    redis_memory: \"55\"\n",
			"tx.yaml":       tenant}), false},
		{"a nested platform file supplies nothing", withDefaults(map[string]string{
			"sub/_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"54\"\n",
			"tx.yaml":            tenant}), false},
		{"platform names a tenant nobody declares", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  ty:\n    redis_memory: \"53\"\n  tx:\n    pg_connections: \"153\"\n",
			"tx.yaml":        tenant}), true},
		{"scheduled, critical, dimensional and declared keys", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n" +
				"    redis_memory:\n      default: \"52\"\n      overrides:\n        - window: \"00:00-23:59\"\n          value: \"51\"\n" +
				"    pg_connections_critical: \"190\"\n" +
				"    mysql_connections{db=\"orders\"}: \"33\"\n" +
				"    oracle_wait: \"7\"\n",
			"tx.yaml": tenant}), true},
		{"a number's source text is kept (0x1F, 1_000)", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    redis_memory: 0x1F\n    pg_connections: 1_000\n",
			"tx.yaml":        tenant}), true},
		{"disable from the platform layer", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    redis_memory: disable\n",
			"tx.yaml":        tenant}), true},
		{"tenant writes every key the platform sets", withDefaults(map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    mysql_connections: \"12\"\n",
			"tx.yaml":        tenant}), false},
	}
}

// mainMergeRows is what main's core served: the root carrier and the tenant
// body, no platform layer. Computed here from the carrier alone so the
// "differs from main" column is measured, not asserted from memory.
func mainMergeRows(t *testing.T, dir, tenantID string, body []byte) []string {
	t.Helper()
	carrierOnly := t.TempDir()
	if data, err := os.ReadFile(filepath.Join(dir, "_defaults.yaml")); err == nil {
		// Strip a `tenants:` block from the copy: main never read it.
		c, perr := ParseConfigFile(data)
		if perr != nil {
			t.Fatal(perr)
		}
		var b strings.Builder
		b.WriteString("defaults:\n")
		for k, v := range c.Defaults {
			fmt.Fprintf(&b, "  %s: %v\n", k, v)
		}
		if len(c.OptionalOverrides) > 0 {
			fmt.Fprintf(&b, "optional_overrides: [%s]\n", strings.Join(c.OptionalOverrides, ", "))
		}
		rootWrite(t, filepath.Join(carrierOnly, "_defaults.yaml"), b.String())
	}
	m := MergeTenantWithRootDefaults(carrierOnly, tenantID, body)
	return resolvedRows(&m, tenantID)
}

func TestMergeTenantPlatformLayerMatchesMetrics(t *testing.T) {
	t.Parallel()
	for _, tc := range platformMergeTrees() {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeMergeTree(t, dir, tc.files)
			oracle, _, err := LoadDir(dir, nil)
			if err != nil {
				t.Fatalf("LoadDir: %v", err)
			}
			want := resolvedRows(oracle, "tx")
			body, err := os.ReadFile(filepath.Join(dir, "tx.yaml"))
			if err != nil {
				t.Fatal(err)
			}

			byteMerge := MergeTenantWithRootDefaults(dir, "tx", body)
			if got := resolvedRows(&byteMerge, "tx"); strings.Join(got, "\n") != strings.Join(want, "\n") {
				t.Errorf("GET merge rows differ from /metrics\n got: %v\nwant: %v", got, want)
			}
			parsed, err := ParseConfigFile(body)
			if err != nil {
				t.Fatal(err)
			}
			parsedMerge := MergeParsedTenantWithRootDefaults(dir, parsed)
			if got := resolvedRows(&parsedMerge, "tx"); strings.Join(got, "\n") != strings.Join(want, "\n") {
				t.Errorf("write-gate merge rows differ from /metrics\n got: %v\nwant: %v", got, want)
			}

			// A platform file cannot create a tenant: the merge holds exactly
			// the tenants the body declares.
			for tid := range byteMerge.Tenants {
				if tid != "tx" {
					t.Errorf("merge created tenant %q the body does not declare", tid)
				}
			}
			// `_metadata`: the WALKER rule (not inherited), not /metrics'.
			// The oracle side is asserted too, so the divergence is measured
			// rather than assumed: if /metrics stopped inheriting it, the
			// comment above and this pin would be stale.
			if _, ok := byteMerge.Tenants["tx"]["_metadata"]; ok {
				t.Errorf("platform _metadata was inherited into the tenant: %v", byteMerge.Tenants["tx"]["_metadata"])
			}
			if strings.Contains(tc.files["_platform.yaml"], "_metadata") {
				metricsOwner := ""
				for _, md := range oracle.ResolveMetadata() {
					if md.Tenant == "tx" {
						metricsOwner = md.Owner
					}
				}
				if metricsOwner != "platform-team" {
					t.Errorf("/metrics' ResolveMetadata owner for tx = %q: the documented divergence (/metrics inherits platform _metadata) no longer holds", metricsOwner)
				}
			}

			mainRows := mainMergeRows(t, dir, "tx", body)
			if differs := strings.Join(mainRows, "\n") != strings.Join(want, "\n"); differs != tc.wantDiffersFromMain {
				t.Errorf("main's rows differ from /metrics = %v, the table says %v (main: %v)", differs, tc.wantDiffersFromMain, mainRows)
			}
		})
	}
}

func containsRow(rows []string, want string) bool {
	for _, r := range rows {
		if r == want {
			return true
		}
	}
	return false
}

// TestRootPlatformScanSelectsWhatTheFullScanSelects: the single root walk
// the merge core runs (scanRootPlatform) picks the same root carrier and the
// same root platform files, in the same order, as a full ScanDirTree.
func TestRootPlatformScanSelectsWhatTheFullScanSelects(t *testing.T) {
	t.Parallel()
	trees := map[string]map[string]string{
		"carrier spellings and platform files": {
			"_DEFAULTS.YML": "defaults:\n  a: 1\n", "_defaults.yaml": "defaults:\n  a: 2\n",
			"_b.yaml": "tenants: {}\n", "_A.YML": "tenants: {}\n", "._hidden.yaml": "tenants: {}\n",
			"_notyaml.txt": "x", "t.yaml": "tenants:\n  t: {}\n",
		},
		"nested platform files and carriers are not root ones": {
			"sub/_defaults.yaml": "defaults:\n  a: 7\n", "sub/_p.yaml": "tenants: {}\n",
			"sub/t.yaml": "tenants:\n  t: {}\n", "_p.yaml": "tenants: {}\n",
		},
		"no platform file": {"t.yaml": "tenants:\n  t: {}\n"},
	}
	for name, files := range trees {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			writeMergeTree(t, root, files)
			full, err := ScanDirTree(root, nil, nil, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatal(err)
			}
			only, err := scanRootPlatform(root)
			if err != nil {
				t.Fatal(err)
			}
			if a, b := selectedRootCarrierKey(full), selectedRootCarrierKey(only); a != b {
				t.Errorf("carrier: root-platform walk %q, full walk %q", b, a)
			}
			if a, b := rootPlatformKeys(full), rootPlatformKeys(only); strings.Join(a, ",") != strings.Join(b, ",") {
				t.Errorf("platform files: root-platform walk %v, full walk %v", b, a)
			}
			for k := range only.Files {
				if strings.Contains(k, "/") || !strings.HasPrefix(k, "_") {
					t.Errorf("root-platform walk kept %q — it keeps root `_` files only", k)
				}
			}
		})
	}
}

// TestMergeTenantLegacyKeyShadowedByPlatformCanonical: the tenant writes the
// LEGACY spelling (mysql_cpu), a platform file the CANONICAL one
// (mysql_threads_running). Resolve's canonical-wins dedup serves the
// platform's value on both /metrics and GET — displayed consistently, and not
// changed here — so the tenant's own value is silently ignored. The tenant
// must hear that, and must NOT be told "the old name still resolves".
func TestMergeTenantLegacyKeyShadowedByPlatformCanonical(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: 70\n",
		"_p.yaml":        "tenants:\n  tx:\n    mysql_threads_running: \"11\"\n",
	}
	run := func(t *testing.T, tenantBody string) (rows []string, oracleRows []string, kv KeyValidation) {
		t.Helper()
		dir := t.TempDir()
		writeMergeTree(t, dir, files)
		writeMergeTree(t, dir, map[string]string{"tx.yaml": tenantBody})
		oracle, _, err := LoadDir(dir, nil)
		if err != nil {
			t.Fatal(err)
		}
		m := MergeTenantWithRootDefaults(dir, "tx", []byte(tenantBody))
		return resolvedRows(&m, "tx"), resolvedRows(oracle, "tx"), m.ValidateTenantKeys()
	}

	rows, oracleRows, kv := run(t, "tenants:\n  tx:\n    mysql_cpu: \"22\"\n")
	if strings.Join(rows, "\n") != strings.Join(oracleRows, "\n") || !containsRow(rows, "mysql_threads_running{}=11/warning") {
		t.Fatalf("display changed: GET %v, /metrics %v (both must serve the platform's 11)", rows, oracleRows)
	}
	if len(kv.Errors) != 0 {
		t.Errorf("errors %q: the shadowed legacy key must not block the write", kv.Errors)
	}
	var shadow []string
	for _, n := range kv.Notices {
		if strings.Contains(n, "still resolves") {
			t.Errorf("misleading notice for a key whose value is not applied: %q", n)
		}
		if strings.Contains(n, `"mysql_cpu"`) && strings.Contains(n, "platform file _p.yaml") &&
			strings.Contains(n, `"mysql_threads_running"`) && strings.Contains(n, "tenant=tx") {
			shadow = append(shadow, n)
		}
	}
	if len(shadow) != 1 {
		t.Errorf("want exactly one tenant notice naming mysql_cpu, _p.yaml and mysql_threads_running; notices %q", kv.Notices)
	}

	// Control: the tenant writes the canonical name — its value wins, and
	// no shadow notice is due.
	rows, _, kv = run(t, "tenants:\n  tx:\n    mysql_threads_running: \"22\"\n")
	if !containsRow(rows, "mysql_threads_running{}=22/warning") {
		t.Fatalf("control: the tenant's canonical value does not win: %v", rows)
	}
	if len(kv.Errors) != 0 || len(kv.Notices) != 0 {
		t.Errorf("control: errors %q notices %q, want none", kv.Errors, kv.Notices)
	}
}

// TestZeroRootPlatformIsAnEmptyRoot: the zero RootPlatform is a usable value
// — "no carrier, no platform files" — not a panic waiting for a caller that
// did not go through LoadRootPlatform.
func TestZeroRootPlatformIsAnEmptyRoot(t *testing.T) {
	t.Parallel()
	body := []byte("tenants:\n  tx:\n    mysql_connections: \"90\"\n")
	var zero RootPlatform
	got := MergeTenantOverRootPlatform(zero, "tx", body)
	want := MergeTenantWithRootDefaults(t.TempDir(), "tx", body)
	if a, b := resolvedRows(&got, "tx"), resolvedRows(&want, "tx"); strings.Join(a, "\n") != strings.Join(b, "\n") {
		t.Errorf("zero root rows %v, empty root rows %v", a, b)
	}
	if len(got.Defaults) != 0 || len(got.Tenants["tx"]) != 1 {
		t.Errorf("zero root merged to Defaults %v, Tenants %v", got.Defaults, got.Tenants)
	}
	gv, wv := got.ValidateTenantKeys(), want.ValidateTenantKeys()
	if strings.Join(gv.Errors, "|") != strings.Join(wv.Errors, "|") || strings.Join(gv.Notices, "|") != strings.Join(wv.Notices, "|") {
		t.Errorf("zero root validation %+v, empty root %+v", gv, wv)
	}
}

// TestMergeTenantPlatformLayerSeesAnEdit: the decode cache is keyed by the
// bytes' hash, so an edited platform file is decoded again, not served stale.
func TestMergeTenantPlatformLayerSeesAnEdit(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	body := []byte("tenants:\n  tx: {}\n")
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": platformMergeDefaults,
		"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"41\"\n",
	})
	value := func() string {
		m := MergeTenantWithRootDefaults(dir, "tx", body)
		return m.Tenants["tx"]["redis_memory"].Default
	}
	if got := value(); got != "41" {
		t.Fatalf("redis_memory = %q, want 41", got)
	}
	writeMergeTree(t, dir, map[string]string{"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"42\"\n"})
	if got := value(); got != "42" {
		t.Errorf("after the edit redis_memory = %q, want 42 (stale decode served)", got)
	}
}

// TestMergeTenantPlatformLayerConcurrent: GET runs concurrently; the shared
// decode cache and the shared decoded values must survive it (run with
// -race). Each merge must also get maps of its own.
func TestMergeTenantPlatformLayerConcurrent(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": platformMergeDefaults,
		"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"43\"\n    pg_connections: \"143\"\n",
	})
	body := []byte("tenants:\n  tx:\n    mysql_connections: \"90\"\n")
	var wg sync.WaitGroup
	errs := make(chan string, 64)
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				m := MergeTenantWithRootDefaults(dir, "tx", body)
				if m.Tenants["tx"]["redis_memory"].Default != "43" {
					errs <- fmt.Sprintf("goroutine %d: redis_memory = %q", i, m.Tenants["tx"]["redis_memory"].Default)
					return
				}
				// Writing into this merge's maps must not reach the cache.
				m.Tenants["tx"]["redis_memory"] = ScheduledValue{Default: fmt.Sprint(i)}
				_ = m.ValidateTenantKeys()
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		t.Error(e)
	}
}

func TestPlatformParseCacheIsBounded(t *testing.T) {
	t.Parallel()
	c := &platformParseCache{max: 3}
	for i := 0; i < 10; i++ {
		c.get(fmt.Sprintf("h%d", i), []byte(fmt.Sprintf("defaults:\n  a: %d\n", i)))
	}
	if len(c.entries) != 3 || len(c.order) != 3 {
		t.Fatalf("cache holds %d entries (%d ordered), want 3", len(c.entries), len(c.order))
	}
	if _, ok := c.entries["h9"]; !ok {
		t.Errorf("the newest entry was evicted: %v", c.order)
	}
}
