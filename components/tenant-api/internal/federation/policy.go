package federation

// Federation policy — the 2-tier metric allowlist (ADR-020 IV-2e).
//
// Two tiers, deliberately in SEPARATE files:
//
//   - Platform whitelist — _federation_policy.yaml at the config-dir
//     root, maintainer-managed. The set of metrics any tenant is
//     allowed to pull via federation.
//   - Tenant subset — conf.d/_federation/<tenant>.yaml, one file per
//     tenant, tenant-self-managed. The metrics one tenant selected;
//     every entry must be contained in the platform whitelist.
//
// Why one file per tenant and not a single shared document: tenant-api
// is commit-on-write GitOps. A shared subsets file would serialise
// every tenant's self-service edit onto one git object, turning
// concurrent writes into merge conflicts and collapsing per-tenant
// blast-radius isolation. One file per tenant keeps writes independent.
//
// Hot-reload: the platform whitelist is served through the embedded
// configwatcher.Watcher (atomic.Value reads, SHA-256 dedup) — the same
// machinery rbac / policy / groups use. Per-tenant subset files are
// read on demand by the handler, not watched: a subset is only needed
// at its own write time (to check containment) and on a direct GET.

import (
	"fmt"
	"log/slog"
	"path/filepath"
	"regexp"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"gopkg.in/yaml.v3"
)

// WhitelistEntry is one metric the platform allows tenants to federate.
// It is an object (not a bare string) so future per-metric constraints
// — label caps, retention hints — can be added without a schema break.
type WhitelistEntry struct {
	Metric string `yaml:"metric" json:"metric"`
}

// FederationPolicyConfig is the parsed _federation_policy.yaml: the
// platform-wide federation whitelist.
type FederationPolicyConfig struct {
	Whitelist []WhitelistEntry `yaml:"whitelist" json:"whitelist"`
}

// FederationSubset is the parsed conf.d/_federation/<tenant>.yaml: the
// metric subset one tenant selected for federation. Every metric must
// be present in the platform whitelist (the 2-tier containment rule).
type FederationSubset struct {
	Metrics []string `yaml:"metrics" json:"metrics"`
}

// PolicyViolation is a single schema or containment failure, shaped to
// render directly into the API's validation-error envelope.
type PolicyViolation struct {
	Field  string `json:"field"`
	Reason string `json:"reason"`
}

// metricNameRE is the Prometheus metric-name grammar. The `:` is
// allowed because recording-rule outputs (e.g. tenant:cpu:rate5m) are
// legitimate federation targets.
var metricNameRE = regexp.MustCompile(`^[a-zA-Z_:][a-zA-Z0-9_:]*$`)

// PolicyManager holds the hot-reloadable platform whitelist. The
// reload machinery lives in the embedded configwatcher.Watcher; this
// type only adds the whitelist-specific lookup.
type PolicyManager struct {
	*configwatcher.Watcher[FederationPolicyConfig]
}

// NewPolicyManager creates a PolicyManager reading _federation_policy.yaml
// from configDir. A missing file is not an error — configwatcher stores
// an empty whitelist, and an empty whitelist simply means no metric is
// federatable yet.
func NewPolicyManager(configDir string) *PolicyManager {
	path := filepath.Join(configDir, "_federation_policy.yaml")
	w, err := configwatcher.New(path, "federation-policy", parsePolicyConfig, emptyPolicyConfig)
	if err != nil {
		slog.Warn("federation policy: initial load failed", "error", err)
	}
	return &PolicyManager{Watcher: w}
}

// NewPolicyManagerForTest returns a PolicyManager pre-populated with cfg
// and no file path. WatchLoop / Reload become no-ops. For unit tests.
func NewPolicyManagerForTest(cfg *FederationPolicyConfig) *PolicyManager {
	return &PolicyManager{Watcher: configwatcher.NewForTest("federation-policy", cfg)}
}

func emptyPolicyConfig() *FederationPolicyConfig {
	return &FederationPolicyConfig{Whitelist: []WhitelistEntry{}}
}

func parsePolicyConfig(data []byte) (*FederationPolicyConfig, error) {
	var cfg FederationPolicyConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	if cfg.Whitelist == nil {
		cfg.Whitelist = []WhitelistEntry{}
	}
	return &cfg, nil
}

// IsWhitelisted reports whether metric is in the current platform
// whitelist snapshot.
func (m *PolicyManager) IsWhitelisted(metric string) bool {
	for _, e := range m.Get().Whitelist {
		if e.Metric == metric {
			return true
		}
	}
	return false
}

// ParseSubset decodes a per-tenant subset file. An empty document
// yields an empty (non-nil) subset.
func ParseSubset(data []byte) (*FederationSubset, error) {
	var s FederationSubset
	if err := yaml.Unmarshal(data, &s); err != nil {
		return nil, err
	}
	if s.Metrics == nil {
		s.Metrics = []string{}
	}
	return &s, nil
}

// ValidateWhitelist checks a proposed platform whitelist: every entry
// must carry a non-empty, syntactically valid metric name, and no
// metric may appear twice. Returns an empty slice when the whitelist
// is well-formed.
func ValidateWhitelist(cfg *FederationPolicyConfig) []PolicyViolation {
	var v []PolicyViolation
	seen := make(map[string]bool, len(cfg.Whitelist))
	for i, e := range cfg.Whitelist {
		field := fmt.Sprintf("whitelist[%d].metric", i)
		switch {
		case e.Metric == "":
			v = append(v, PolicyViolation{field, "metric name must not be empty"})
		case !metricNameRE.MatchString(e.Metric):
			v = append(v, PolicyViolation{field, fmt.Sprintf("%q is not a valid Prometheus metric name", e.Metric)})
		case seen[e.Metric]:
			v = append(v, PolicyViolation{field, fmt.Sprintf("duplicate whitelist entry %q", e.Metric)})
		default:
			seen[e.Metric] = true
		}
	}
	return v
}

// ValidateSubset checks a tenant's proposed subset against the platform
// whitelist. Every metric must be syntactically valid, unique within
// the subset, and present in the whitelist — the 2-tier containment
// rule: a tenant subset can never exceed the platform whitelist.
// Returns an empty slice when the subset is valid.
func ValidateSubset(subset *FederationSubset, whitelist *FederationPolicyConfig) []PolicyViolation {
	allowed := make(map[string]bool, len(whitelist.Whitelist))
	for _, e := range whitelist.Whitelist {
		allowed[e.Metric] = true
	}
	var v []PolicyViolation
	seen := make(map[string]bool, len(subset.Metrics))
	for i, metric := range subset.Metrics {
		field := fmt.Sprintf("metrics[%d]", i)
		switch {
		case metric == "":
			v = append(v, PolicyViolation{field, "metric name must not be empty"})
		case !metricNameRE.MatchString(metric):
			v = append(v, PolicyViolation{field, fmt.Sprintf("%q is not a valid Prometheus metric name", metric)})
		case seen[metric]:
			v = append(v, PolicyViolation{field, fmt.Sprintf("duplicate metric %q", metric)})
		case !allowed[metric]:
			seen[metric] = true
			v = append(v, PolicyViolation{field, fmt.Sprintf("metric %q is not in the platform federation whitelist", metric)})
		default:
			seen[metric] = true
		}
	}
	return v
}
