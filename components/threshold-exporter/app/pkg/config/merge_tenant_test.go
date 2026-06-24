package config

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestMergeTenantWithRootDefaults_PopulatesDefaults verifies the consolidation
// behind the GET / validate / write-boundary parity (ADR-024 PR4 / #704): a
// tenant-only body merged against a root _defaults.yaml yields a config whose
// Defaults are populated, so ValidateTenantKeys recognises plain metric keys
// instead of flagging them "unknown key not in defaults".
func TestMergeTenantWithRootDefaults_PopulatesDefaults(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"),
		[]byte("defaults:\n  container_cpu: 80\n  mysql_cpu: 80\n"), 0o644); err != nil {
		t.Fatalf("write defaults: %v", err)
	}

	body := []byte("tenants:\n  db-a:\n    container_cpu: \"70\"\n")
	merged := MergeTenantWithRootDefaults(dir, "db-a", body)

	if merged.Defaults["container_cpu"] != 80 {
		t.Errorf("expected container_cpu default 80, got %v", merged.Defaults["container_cpu"])
	}
	if _, ok := merged.Tenants["db-a"]["container_cpu"]; !ok {
		t.Error("tenant override container_cpu should be present in merged.Tenants")
	}
	if warnings := merged.ValidateTenantKeys(); len(warnings) != 0 {
		t.Errorf("tenant-only metric body should validate clean against merged defaults, got: %v", warnings)
	}
}

// TestMergeTenantWithRootDefaults_NoDefaultsFile confirms a missing
// _defaults.yaml is tolerated (empty Defaults), and that ValidateTenantKeys
// then still flags an ordinary metric key — i.e. the merge does not fabricate
// defaults, it only surfaces ones that genuinely exist on disk.
func TestMergeTenantWithRootDefaults_NoDefaultsFile(t *testing.T) {
	t.Parallel()
	dir := t.TempDir() // no _defaults.yaml

	body := []byte("tenants:\n  db-a:\n    container_cpu: \"70\"\n")
	merged := MergeTenantWithRootDefaults(dir, "db-a", body)

	if len(merged.Defaults) != 0 {
		t.Errorf("expected empty Defaults without a _defaults.yaml, got: %v", merged.Defaults)
	}
	if warnings := merged.ValidateTenantKeys(); len(warnings) == 0 {
		t.Error("an unmatched metric key should warn when no defaults are present")
	}
}

// TestCheckTenantRootKeys covers the root-key contract (#705): a tenant body may
// carry ONLY a top-level `tenants` block (tenant-config.schema.json
// additionalProperties:false). Shared by the PUT write boundary + POST /validate.
func TestCheckTenantRootKeys(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		yaml    string
		wantBad bool
		wantSub string // substring expected in the warning when wantBad
	}{
		{"tenants only", "tenants:\n  db-a:\n    container_cpu: \"70\"\n", false, ""},
		{"stray defaults", "defaults:\n  container_cpu: 80\ntenants:\n  db-a:\n    container_cpu: \"70\"\n", true, "defaults"},
		{"stray state_filters", "state_filters:\n  x: {}\ntenants:\n  db-a: {}\n", true, "state_filters"},
		{"stray profiles", "profiles:\n  p: {}\ntenants:\n  db-a: {}\n", true, "profiles"},
		{"typo tenant", "tenant:\n  db-a:\n    container_cpu: \"70\"\n", true, "tenant"},
		{"scalar doc (not a map)", "just-a-string\n", false, ""}, // YAML-validity is the caller's gate
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := CheckTenantRootKeys([]byte(tt.yaml))
			if tt.wantBad {
				if len(got) == 0 {
					t.Fatalf("CheckTenantRootKeys(%q) = no warning, want a root-key violation", tt.yaml)
				}
				if !strings.Contains(got[0], tt.wantSub) {
					t.Errorf("warning %q should name %q", got[0], tt.wantSub)
				}
				return
			}
			if len(got) != 0 {
				t.Errorf("CheckTenantRootKeys(%q) = %v, want no warning", tt.yaml, got)
			}
		})
	}
}

// TestMergeTenantWithRootDefaults_FlatKVFallback pins the flat key-value
// fallback (merge_tenant.go "Fallback: a flat key-value document …"): a body
// with NO top-level `tenants:` wrapper is wrapped under tenantID, preserving the
// historical loadMergedConfig behavior. This path is documented as load-bearing
// but was previously unexercised — a wholesale rewrite could silently drop it
// while the wrapper-form tests stayed green (the false-green trap this suite
// closes).
func TestMergeTenantWithRootDefaults_FlatKVFallback(t *testing.T) {
	t.Parallel()
	dir := t.TempDir() // no _defaults.yaml needed

	body := []byte("container_cpu: \"70\"\nmysql_cpu: \"60\"\n")
	merged := MergeTenantWithRootDefaults(dir, "db-a", body)

	tenant, ok := merged.Tenants["db-a"]
	if !ok {
		t.Fatalf("flat-KV body should be wrapped under tenantID db-a, got tenants: %v", merged.Tenants)
	}
	if got := tenant["container_cpu"].Default; got != "70" {
		t.Errorf("container_cpu = %q, want \"70\"", got)
	}
	if got := tenant["mysql_cpu"].Default; got != "60" {
		t.Errorf("mysql_cpu = %q, want \"60\"", got)
	}
	if len(merged.Tenants) != 1 {
		t.Errorf("only the requested tenant should be present, got: %v", merged.Tenants)
	}
}

// TestMergeTenantWithRootDefaults_BodyShapes characterizes how the parse paths
// (tenants-block, flat-KV fallback, and unparseable/empty) shape the resulting
// Tenants map. It pins two non-obvious behaviors: the `tenants:` block takes
// precedence — when it already contains the requested tenantID the flat-KV
// fallback does NOT fire (no phantom key); and a multi-tenant body merges EVERY
// tenant in the block, not just tenantID.
func TestMergeTenantWithRootDefaults_BodyShapes(t *testing.T) {
	t.Parallel()
	// shape flattens Tenants to tenant -> sorted metric keys, so each case can
	// assert the EXACT merged shape without depending on ScheduledValue internals.
	shape := func(c ThresholdConfig) map[string][]string {
		out := make(map[string][]string, len(c.Tenants))
		for tenant, metrics := range c.Tenants {
			keys := make([]string, 0, len(metrics))
			for k := range metrics {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			out[tenant] = keys
		}
		return out
	}
	tests := []struct {
		name     string
		tenantID string
		body     string
		want     map[string][]string
	}{
		{
			name:     "tenants block, requested id present — no flat-KV phantom",
			tenantID: "db-a",
			body:     "tenants:\n  db-a:\n    container_cpu: \"70\"\n",
			want:     map[string][]string{"db-a": {"container_cpu"}},
		},
		{
			name:     "multi-tenant block merges all tenants",
			tenantID: "db-a",
			body:     "tenants:\n  db-a:\n    container_cpu: \"70\"\n  db-b:\n    mysql_cpu: \"60\"\n",
			want:     map[string][]string{"db-a": {"container_cpu"}, "db-b": {"mysql_cpu"}},
		},
		{
			name:     "empty body merges nothing",
			tenantID: "db-a",
			body:     "",
			want:     map[string][]string{},
		},
		{
			name:     "scalar (non-mapping) body merges nothing",
			tenantID: "db-a",
			body:     "just-a-string\n",
			want:     map[string][]string{},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			merged := MergeTenantWithRootDefaults(t.TempDir(), tt.tenantID, []byte(tt.body))
			if got := shape(merged); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("tenant shape = %v, want %v", got, tt.want)
			}
		})
	}
}

// TestMergeTenantWithRootDefaults_PropagatesStateFilters pins that state_filters
// from the root _defaults.yaml are carried into the merged config (alongside
// Defaults). The original suite asserted only Defaults propagation, leaving
// StateFilters — a second thing the defaults overlay copies — unpinned.
func TestMergeTenantWithRootDefaults_PropagatesStateFilters(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	defaults := "state_filters:\n" +
		"  container_crashloop:\n" +
		"    reasons: [\"CrashLoopBackOff\"]\n" +
		"    severity: critical\n"
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(defaults), 0o644); err != nil {
		t.Fatalf("write defaults: %v", err)
	}

	body := []byte("tenants:\n  db-a:\n    container_cpu: \"70\"\n")
	merged := MergeTenantWithRootDefaults(dir, "db-a", body)

	sf, ok := merged.StateFilters["container_crashloop"]
	if !ok {
		t.Fatalf("state_filters from _defaults.yaml should propagate, got: %v", merged.StateFilters)
	}
	if sf.Severity != "critical" {
		t.Errorf("severity = %q, want \"critical\"", sf.Severity)
	}
	if len(sf.Reasons) != 1 || sf.Reasons[0] != "CrashLoopBackOff" {
		t.Errorf("reasons = %v, want [CrashLoopBackOff]", sf.Reasons)
	}
}

// TestMergeTenantWithRootDefaults_MalformedDefaultsTolerated pins the CURRENT
// tolerance of a corrupt _defaults.yaml in THIS helper: a syntactically invalid
// file is skipped (empty Defaults, no panic, no fabricated values) and does NOT
// block the tenant merge. corrupt != missing, and this helper (the lightweight
// tenant-api GET/validate/PUT boundary) tolerates it silently — but the platform
// is NOT blind to it: the production scanner emits
// da_config_parse_failure_total{file_basename="_defaults.yaml"} and pages via the
// ConfigDefaultsParseFailure critical alert (k8s/03-monitoring/configmap-rules-
// platform.yaml, issue #643). Pinned as this helper's current behavior.
func TestMergeTenantWithRootDefaults_MalformedDefaultsTolerated(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	// Unclosed flow mapping → yaml.Unmarshal returns an error.
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"),
		[]byte("{ this is: not valid yaml\n"), 0o644); err != nil {
		t.Fatalf("write defaults: %v", err)
	}

	body := []byte("tenants:\n  db-a:\n    container_cpu: \"70\"\n")
	merged := MergeTenantWithRootDefaults(dir, "db-a", body)

	if len(merged.Defaults) != 0 {
		t.Errorf("malformed _defaults.yaml should yield empty Defaults, got: %v", merged.Defaults)
	}
	if _, ok := merged.Tenants["db-a"]["container_cpu"]; !ok {
		t.Error("tenant body should still merge despite a malformed _defaults.yaml")
	}
}

// TestMergeParsedTenantWithRootDefaults_MatchesByteVariant pins the #708
// parse-once consolidation: for a tenants-block body (the tenant-api write-path
// shape), the parsed-input MergeParsedTenantWithRootDefaults returns a config
// deep-equal to the byte-input MergeTenantWithRootDefaults on the same body — so
// the write boundary's switch from re-Unmarshalling raw bytes a third time to
// threading the already-decoded ThresholdConfig is behavior-preserving. A
// divergence in the shared merge core (defaults overlay, state_filters
// propagation, multi-tenant merge, ApplyProfiles) fails here.
func TestMergeParsedTenantWithRootDefaults_MatchesByteVariant(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	defaults := "defaults:\n  container_cpu: 80\n  mysql_cpu: 80\n" +
		"state_filters:\n" +
		"  container_crashloop:\n" +
		"    reasons: [\"CrashLoopBackOff\"]\n" +
		"    severity: critical\n"
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(defaults), 0o644); err != nil {
		t.Fatalf("write defaults: %v", err)
	}

	tests := []struct {
		name     string
		tenantID string
		body     string
	}{
		{"single tenant", "db-a", "tenants:\n  db-a:\n    container_cpu: \"70\"\n"},
		{"multi tenant block", "db-a", "tenants:\n  db-a:\n    container_cpu: \"70\"\n  db-b:\n    mysql_cpu: \"60\"\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			var tcfg ThresholdConfig
			if err := yaml.Unmarshal([]byte(tt.body), &tcfg); err != nil {
				t.Fatalf("decode body: %v", err)
			}
			parsed := MergeParsedTenantWithRootDefaults(dir, tcfg)
			bytewise := MergeTenantWithRootDefaults(dir, tt.tenantID, []byte(tt.body))
			if !reflect.DeepEqual(parsed, bytewise) {
				t.Errorf("parsed variant diverged from byte variant:\n parsed = %+v\n bytes  = %+v", parsed, bytewise)
			}
		})
	}
}

// TestMergeParsedTenantWithRootDefaults_OmitsFlatKVFallback documents the one
// intentional difference from the byte entry point: the parsed variant does NOT
// wrap a flat key-value document under tenantID. That fallback serves the GET
// read path's legacy on-disk files; the write boundary that uses this variant
// has already asserted a `tenants.<id>` block is present (gitops.validate's
// structural check), so the fallback is unreachable for it. A flat body decodes
// into a ThresholdConfig with no `tenants:`, so the merged result has no entry
// for the tenant — unlike MergeTenantWithRootDefaults, which synthesises one.
func TestMergeParsedTenantWithRootDefaults_OmitsFlatKVFallback(t *testing.T) {
	t.Parallel()
	dir := t.TempDir() // no _defaults.yaml needed

	flat := "container_cpu: \"70\"\nmysql_cpu: \"60\"\n" // no tenants: wrapper
	var tcfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(flat), &tcfg); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if got := MergeParsedTenantWithRootDefaults(dir, tcfg); len(got.Tenants) != 0 {
		t.Errorf("parsed variant must not synthesise a tenant from a flat-KV body, got tenants: %v", got.Tenants)
	}

	// The byte entry point, by contrast, still applies the flat-KV fallback —
	// pinning that this refactor left the GET read-path behavior untouched.
	if _, ok := MergeTenantWithRootDefaults(dir, "db-a", []byte(flat)).Tenants["db-a"]; !ok {
		t.Error("byte variant should still wrap a flat-KV body under tenantID (GET read-path behavior)")
	}
}
