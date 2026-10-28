package fedpolicy

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestValidateWhitelist(t *testing.T) {
	tests := []struct {
		name       string
		whitelist  []WhitelistEntry
		wantCount  int
		wantField  string // substring expected in the first violation's Field (when wantCount > 0)
		wantReason string // substring expected in the first violation's Reason
	}{
		{
			name:      "valid — plain and recording-rule names",
			whitelist: []WhitelistEntry{{Metric: "mysql_up"}, {Metric: "tenant:cpu:rate5m"}},
			wantCount: 0,
		},
		{
			name:      "empty whitelist is valid",
			whitelist: []WhitelistEntry{},
			wantCount: 0,
		},
		{
			name:       "empty metric name",
			whitelist:  []WhitelistEntry{{Metric: ""}},
			wantCount:  1,
			wantField:  "whitelist[0].metric",
			wantReason: "must not be empty",
		},
		{
			name:       "invalid metric name — hyphen",
			whitelist:  []WhitelistEntry{{Metric: "bad-name"}},
			wantCount:  1,
			wantField:  "whitelist[0].metric",
			wantReason: "not a valid",
		},
		{
			name:       "duplicate entry",
			whitelist:  []WhitelistEntry{{Metric: "mysql_up"}, {Metric: "mysql_up"}},
			wantCount:  1,
			wantField:  "whitelist[1].metric",
			wantReason: "duplicate",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ValidateWhitelist(&Config{Whitelist: tt.whitelist})
			if len(got) != tt.wantCount {
				t.Fatalf("ValidateWhitelist() = %d violations %+v, want %d", len(got), got, tt.wantCount)
			}
			if tt.wantCount > 0 {
				if got[0].Field != tt.wantField {
					t.Errorf("violation Field = %q, want %q", got[0].Field, tt.wantField)
				}
				if !strings.Contains(got[0].Reason, tt.wantReason) {
					t.Errorf("violation Reason = %q, want substring %q", got[0].Reason, tt.wantReason)
				}
			}
		})
	}
}

func TestValidateSubset(t *testing.T) {
	whitelist := &Config{Whitelist: []WhitelistEntry{
		{Metric: "mysql_up"},
		{Metric: "pg_up"},
		{Metric: "tenant:cpu:rate5m"},
	}}
	tests := []struct {
		name       string
		metrics    []string
		wantCount  int
		wantReason string
	}{
		{
			name:      "subset fully within whitelist — pass",
			metrics:   []string{"mysql_up", "tenant:cpu:rate5m"},
			wantCount: 0,
		},
		{
			name:      "empty subset is valid",
			metrics:   []string{},
			wantCount: 0,
		},
		{
			name:       "metric not in whitelist — containment failure",
			metrics:    []string{"mysql_up", "redis_up"},
			wantCount:  1,
			wantReason: "not in the platform federation whitelist",
		},
		{
			name:       "duplicate metric in subset",
			metrics:    []string{"mysql_up", "mysql_up"},
			wantCount:  1,
			wantReason: "duplicate",
		},
		{
			name:       "empty metric name",
			metrics:    []string{""},
			wantCount:  1,
			wantReason: "must not be empty",
		},
		{
			name:       "invalid metric name",
			metrics:    []string{"bad-name"},
			wantCount:  1,
			wantReason: "not a valid",
		},
		{
			name:       "multiple metrics outside whitelist — one violation each",
			metrics:    []string{"redis_up", "kafka_up"},
			wantCount:  2,
			wantReason: "not in the platform federation whitelist",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ValidateSubset(&Subset{Metrics: tt.metrics}, whitelist)
			if len(got) != tt.wantCount {
				t.Fatalf("ValidateSubset() = %d violations %+v, want %d", len(got), got, tt.wantCount)
			}
			if tt.wantCount > 0 && !contains(got[0].Reason, tt.wantReason) {
				t.Errorf("violation Reason = %q, want substring %q", got[0].Reason, tt.wantReason)
			}
		})
	}
}

func TestValidateSubsetEmptyWhitelistRejectsEverything(t *testing.T) {
	// With no platform whitelist, every subset entry is a containment
	// failure — a tenant cannot federate metrics the platform has not
	// allowed.
	got := ValidateSubset(
		&Subset{Metrics: []string{"mysql_up"}},
		&Config{Whitelist: []WhitelistEntry{}},
	)
	if len(got) != 1 {
		t.Fatalf("ValidateSubset() against empty whitelist = %d violations, want 1", len(got))
	}
}

func TestEffectiveSubset(t *testing.T) {
	whitelist := &Config{Whitelist: []WhitelistEntry{
		{Metric: "mysql_up"},
		{Metric: "pg_up"},
	}}
	tests := []struct {
		name   string
		stored []string
		want   []string
	}{
		{
			name:   "all metrics whitelisted — unchanged",
			stored: []string{"mysql_up", "pg_up"},
			want:   []string{"mysql_up", "pg_up"},
		},
		{
			name:   "stale metric dropped — read-repair",
			stored: []string{"mysql_up", "redis_up", "pg_up"},
			want:   []string{"mysql_up", "pg_up"},
		},
		{
			name:   "every metric stale — empty effective subset",
			stored: []string{"redis_up", "kafka_up"},
			want:   []string{},
		},
		{
			name:   "empty subset stays empty",
			stored: []string{},
			want:   []string{},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := EffectiveSubset(&Subset{Metrics: tt.stored}, whitelist)
			if len(got.Metrics) != len(tt.want) {
				t.Fatalf("EffectiveSubset() = %v, want %v", got.Metrics, tt.want)
			}
			for i := range tt.want {
				if got.Metrics[i] != tt.want[i] {
					t.Errorf("EffectiveSubset()[%d] = %q, want %q", i, got.Metrics[i], tt.want[i])
				}
			}
		})
	}
}

func TestParsePolicyConfig(t *testing.T) {
	doc := []byte("whitelist:\n  - metric: mysql_up\n  - metric: pg_up\n")
	cfg, err := ParsePolicyConfig(doc)
	if err != nil {
		t.Fatalf("ParsePolicyConfig() error = %v", err)
	}
	if len(cfg.Whitelist) != 2 || cfg.Whitelist[0].Metric != "mysql_up" {
		t.Fatalf("ParsePolicyConfig() = %+v, want 2 entries starting with mysql_up", cfg.Whitelist)
	}

	// An empty document yields an empty, non-nil whitelist.
	empty, err := ParsePolicyConfig([]byte(""))
	if err != nil {
		t.Fatalf("ParsePolicyConfig(empty) error = %v", err)
	}
	if empty.Whitelist == nil {
		t.Error("ParsePolicyConfig(empty) Whitelist is nil, want non-nil empty slice")
	}
}

func TestParseSubset(t *testing.T) {
	s, err := ParseSubset([]byte("metrics:\n  - mysql_up\n  - pg_up\n"))
	if err != nil {
		t.Fatalf("ParseSubset() error = %v", err)
	}
	if len(s.Metrics) != 2 {
		t.Fatalf("ParseSubset() = %+v, want 2 metrics", s.Metrics)
	}

	empty, err := ParseSubset([]byte(""))
	if err != nil {
		t.Fatalf("ParseSubset(empty) error = %v", err)
	}
	if empty.Metrics == nil {
		t.Error("ParseSubset(empty) Metrics is nil, want non-nil empty slice")
	}
}

func TestIsWhitelisted(t *testing.T) {
	m := NewManagerForTest(&Config{Whitelist: []WhitelistEntry{
		{Metric: "mysql_up"},
	}})
	if !m.IsWhitelisted("mysql_up") {
		t.Error("IsWhitelisted(mysql_up) = false, want true")
	}
	if m.IsWhitelisted("redis_up") {
		t.Error("IsWhitelisted(redis_up) = true, want false")
	}
}

func contains(s, substr string) bool {
	return substr == "" || (len(s) >= len(substr) && stringIndex(s, substr) >= 0)
}

func stringIndex(s, substr string) int {
	for i := 0; i+len(substr) <= len(s); i++ {
		if s[i:i+len(substr)] == substr {
			return i
		}
	}
	return -1
}

// TestParse_ParseTreeRefusal (ADR-036 §7 step 7, #3206): whitelist and subset
// records yaml.v3 alone reads (the first document; a `!!binary` or `!` value
// as a string) are parse errors — the whitelist watcher keeps its last good
// list, the subset read errors — never the first document's metrics.
func TestParse_ParseTreeRefusal(t *testing.T) {
	for _, c := range []struct{ name, whitelist, subset, kind string }{
		{"second document",
			"whitelist:\n  - metric: m1\n---\nwhitelist:\n  - metric: m2\n",
			"metrics:\n  - m1\n---\nmetrics:\n  - m2\n", "multiple_documents"},
		{"trailing empty document", "whitelist:\n  - metric: m1\n---\n", "metrics:\n  - m1\n---\n", "multiple_documents"},
		{"!!binary value", "whitelist:\n  - metric: !!binary bTE=\n", "metrics:\n  - !!binary bTE=\n", "tag_not_allowed"},
		{"non-specific tag", "whitelist:\n  - metric: ! \"m1\"\n", "metrics:\n  - ! \"m1\"\n", "non_specific_tag"},
	} {
		if cfg, err := ParsePolicyConfig([]byte(c.whitelist)); err == nil || !strings.Contains(err.Error(), "kind "+c.kind) {
			t.Errorf("%s: ParsePolicyConfig = %+v, %v; want a refusal of kind %s", c.name, cfg, err, c.kind)
		}
		if s, err := ParseSubset([]byte(c.subset)); err == nil || !strings.Contains(err.Error(), "kind "+c.kind) {
			t.Errorf("%s: ParseSubset = %+v, %v; want a refusal of kind %s", c.name, s, err, c.kind)
		}
	}
}

// At startup a refused whitelist record is the initial-load failure it
// always was: logged, and the manager starts from the empty whitelist — no
// metric of the refused record's first document is federatable.
func TestNewManager_ParseTreeRefusalStartsEmpty(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_federation_policy.yaml"),
		[]byte("whitelist:\n  - metric: m1\n---\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if NewDirManager(dir).IsWhitelisted("m1") {
		t.Fatal("m1 of a refused whitelist record is federatable")
	}
}
