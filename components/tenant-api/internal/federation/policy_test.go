package federation

import (
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
			got := ValidateWhitelist(&FederationPolicyConfig{Whitelist: tt.whitelist})
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
	whitelist := &FederationPolicyConfig{Whitelist: []WhitelistEntry{
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
			got := ValidateSubset(&FederationSubset{Metrics: tt.metrics}, whitelist)
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
		&FederationSubset{Metrics: []string{"mysql_up"}},
		&FederationPolicyConfig{Whitelist: []WhitelistEntry{}},
	)
	if len(got) != 1 {
		t.Fatalf("ValidateSubset() against empty whitelist = %d violations, want 1", len(got))
	}
}

func TestEffectiveSubset(t *testing.T) {
	whitelist := &FederationPolicyConfig{Whitelist: []WhitelistEntry{
		{Metric: "mysql_up"},
		{Metric: "pg_up"},
	}}
	tests := []struct {
		name  string
		stored []string
		want  []string
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
			got := EffectiveSubset(&FederationSubset{Metrics: tt.stored}, whitelist)
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
	cfg, err := parsePolicyConfig(doc)
	if err != nil {
		t.Fatalf("parsePolicyConfig() error = %v", err)
	}
	if len(cfg.Whitelist) != 2 || cfg.Whitelist[0].Metric != "mysql_up" {
		t.Fatalf("parsePolicyConfig() = %+v, want 2 entries starting with mysql_up", cfg.Whitelist)
	}

	// An empty document yields an empty, non-nil whitelist.
	empty, err := parsePolicyConfig([]byte(""))
	if err != nil {
		t.Fatalf("parsePolicyConfig(empty) error = %v", err)
	}
	if empty.Whitelist == nil {
		t.Error("parsePolicyConfig(empty) Whitelist is nil, want non-nil empty slice")
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
	m := NewPolicyManagerForTest(&FederationPolicyConfig{Whitelist: []WhitelistEntry{
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
