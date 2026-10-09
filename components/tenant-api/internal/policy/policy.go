// Package policy implements domain policy enforcement at the API layer.
//
// v2.5.0 Phase C: Domain policies are loaded from _domain_policy.yaml (and
// _domain_policy.yml, #2486; see FileNames) and enforced on write
// operations. Violations that were previously WARN-only (CI validation) now
// return 403 at API time.
//
// Schema (_domain_policy.yaml):
//
//	domain_policies:
//	  finance:
//	    description: "Finance domain compliance requirements"
//	    tenants: [db-a, db-b]
//	    constraints:
//	      allowed_receiver_types: [pagerduty, email, opsgenie]
//	      forbidden_receiver_types: [slack, webhook]
//	      enforce_group_by: [tenant, alertname, severity]
//	      max_repeat_interval: 1h
//	      min_group_wait: 30s
//	      require_critical_escalation: true
//
// Concurrency: reads are lock-free (atomic.Value). Hot-reloaded via SHA-256
// (the underlying configwatcher.Watcher dedups disk reads on each tick).
package policy

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
	"gopkg.in/yaml.v3"
)

// Constraints defines the constraints for a domain policy.
type Constraints struct {
	AllowedReceiverTypes   AllowedTypeList   `yaml:"allowed_receiver_types"`
	ForbiddenReceiverTypes ForbiddenTypeList `yaml:"forbidden_receiver_types"`
	EnforceGroupBy         []string          `yaml:"enforce_group_by"`
	MaxRepeatInterval      string            `yaml:"max_repeat_interval"`
	MinGroupWait           string            `yaml:"min_group_wait"`
	// RequireCriticalEscalation is read as the generator's PyYAML reads it
	// (routingpolicy.DecodePyYAML, #2325): a plain `yes` / `on` or a
	// `!!bool yEs` is true and `no` / `off` false. Only a boolean `true` turns
	// the constraint on (the generator's `is True`); any other value PyYAML
	// reads (`"true"`, `1`, any mapping or list) is logged once per load and
	// left off, and the rest of the file still applies — the generator
	// enforces nothing from it either (only --strict reports it). A
	// `!!null`-tagged scalar (`!!null x`) is None, as for PyYAML: the
	// constraint is off, the rest applies (parseConfig decodes via
	// routingpolicy.UnmarshalPolicy). A value PyYAML refuses — `!!bool y`,
	// `!!int abc`, `!!bool [true]`, `{<<: 1}`, `!!null {}` — makes the
	// generator drop the whole file and da-guard refuse it, and tenant-api
	// refuses the whole file too (hub #2486 PR-7c round 2, B1; #2325 read it
	// as this constraint off): the watcher keeps the file's last good
	// content, or with none the policy is unavailable (Manager.Unavailable).
	RequireCriticalEscalation routingpolicy.PyYAMLValue `yaml:"require_critical_escalation"`
}

// DomainPolicy defines a single domain's compliance constraints.
type DomainPolicy struct {
	Description string      `yaml:"description"`
	Tenants     TenantList  `yaml:"tenants"`
	Constraints Constraints `yaml:"constraints"`
}

// TenantList is a domain policy's `tenants:` read as the route generator
// reads it (#2730 §2): each scalar item is its source TEXT, never decoded —
// `010` is "010", `~` is "~", `!!binary aGk=` is "aGk=", `!!null x` is "x",
// `! "null"` is "null" (_lib_yaml_keys.ExporterKeyLoader,
// raw_text_sequences). A value that is not a sequence, or an item that is
// not a scalar, is an error: the generator enforces no tenant from it, and
// tenant-api refuses the file rather than read it otherwise (stricter, never
// looser). A null value is no tenants (yaml.v3 hands it no Unmarshaler).
type TenantList []string

// UnmarshalYAML implements yaml.Unmarshaler.
func (l *TenantList) UnmarshalYAML(n *yaml.Node) error {
	n = derefNode(n)
	if n.Kind != yaml.SequenceNode {
		return fmt.Errorf("line %d: 'tenants' must be a list of tenant ids", n.Line)
	}
	out := make(TenantList, 0, len(n.Content))
	for _, item := range n.Content {
		d := derefNode(item)
		if d == nil || d.Kind != yaml.ScalarNode {
			return fmt.Errorf("line %d: a 'tenants' entry must be a tenant id (a scalar)", item.Line)
		}
		out = append(out, d.Value)
	}
	*l = out
	return nil
}

// ForbiddenTypeList is `forbidden_receiver_types` read as the route
// generator reads it (#2730 §6): the entries PyYAML builds as a string
// (routingpolicy.ReceiverTypeItem — `! "slack"` is "slack"). An entry PyYAML
// builds as anything else (`! "null"` is None, `yes` True, `7` an int) equals
// only a receiver type that is no string, which the generator refuses on its
// own, so it forbids nothing and is left out. A value that is not a list, an
// entry that is not a scalar, and an entry PyYAML cannot build (`!!int x`,
// `!custom x`: the generator drops the file; or builds as a type not
// modelled, `!!binary`) are errors: the file is refused (hub #2486 PR-7c
// round 2, F1).
type ForbiddenTypeList []string

// UnmarshalYAML implements yaml.Unmarshaler.
func (l *ForbiddenTypeList) UnmarshalYAML(n *yaml.Node) error {
	out, err := receiverTypeEntries(n, false)
	*l = out
	return err
}

// AllowedTypeList is `allowed_receiver_types` read as the generator reads
// it: a string entry is a type it allows. A non-string entry allows no
// receiver type a usable config has, yet a list of only such entries still
// restricts — the generator refuses every type not in the non-empty set — so
// it is kept as NotAReceiverType, never as its source text (hub #2486 PR-7c
// round 2, F1: `!!null webhook` is None to PyYAML, and its text "webhook"
// allowed a webhook receiver the generator refuses). Errors as
// ForbiddenTypeList's.
type AllowedTypeList []string

// NotAReceiverType stands in an AllowedTypeList for an entry PyYAML builds as
// no string: it keeps the list non-empty and equals no receiver type. A
// receiver type is a string yaml.v3 decoded, always valid UTF-8; this is not.
const NotAReceiverType = "\xff<allowed_receiver_types entry that is not a string>"

// UnmarshalYAML implements yaml.Unmarshaler.
func (l *AllowedTypeList) UnmarshalYAML(n *yaml.Node) error {
	out, err := receiverTypeEntries(n, true)
	*l = out
	return err
}

// receiverTypeEntries reads a receiver-type list node: string entries as
// PyYAML reads them, non-string scalar entries kept as NotAReceiverType when
// keepOther, else left out. An entry PyYAML cannot build is an error.
func receiverTypeEntries(n *yaml.Node, keepOther bool) ([]string, error) {
	n = derefNode(n)
	if n.Kind != yaml.SequenceNode {
		return nil, fmt.Errorf("line %d: a receiver-type constraint must be a list", n.Line)
	}
	out := make([]string, 0, len(n.Content))
	for _, item := range n.Content {
		d := derefNode(item)
		if d == nil || d.Kind != yaml.ScalarNode {
			return nil, fmt.Errorf("line %d: a receiver-type entry must be a scalar", item.Line)
		}
		s, ok, err := routingpolicy.ReceiverTypeItem(d)
		switch {
		case err != nil:
			return nil, err
		case ok:
			out = append(out, s)
		case keepOther:
			out = append(out, NotAReceiverType)
		}
	}
	return out, nil
}

func derefNode(n *yaml.Node) *yaml.Node {
	for n != nil && n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	return n
}

// DomainPolicyConfig is the parsed _domain_policy.yaml structure.
type DomainPolicyConfig struct {
	DomainPolicies map[string]DomainPolicy `yaml:"domain_policies"`
}

// Violation represents a single policy violation.
type Violation struct {
	Domain     string `json:"domain"`
	Constraint string `json:"constraint"`
	// Target is the route whose receiver breaks the constraint: `receiver`
	// (the tenant's main route), `overrides[i]` or `routes[i]`. Omitted for
	// the flat batch key `_routing_receiver_type`.
	Target  string `json:"target,omitempty"`
	Message string `json:"message"`
}

// Manager holds the hot-reloadable domain policy config. The
// hot-reload machinery (atomic.Value + SHA-256 dedup + WatchLoop)
// lives in the embedded configwatcher.Watcher; this type only adds
// the policy-specific check methods.
type Manager struct {
	*configwatcher.Watcher[DomainPolicyConfig]

	// files is the watcher's per-file parser (NewManager); nil for a
	// LoadSnapshot / NewForTest Manager, which is never unavailable.
	files *fileWiseParser
	// openOnUnavailable is the --policy-unavailable-open escape hatch
	// (SetOpenOnUnavailable): CheckAvailable then lets a write through an
	// unavailable policy, logging and counting each one (openPasses).
	openOnUnavailable atomic.Bool
	openPasses        atomic.Int64
}

// ErrUnavailable is CheckAvailable's error (wrapped): a policy file is there
// but cannot be used and has no last good content of its own, so the
// policy-reading writes are refused (hub #2486 Q7-2, direct mode).
var ErrUnavailable = errors.New("domain policy unavailable")

// Unavailable reports whether a domain policy file is present but unusable
// with no last good content of its own (hub #2486 Q7-2 甲軸), judged file
// by file: Lstat sees the file, and it cannot be read (a directory, a
// dangling symlink, no permission) or does not parse (every refusal of
// parseConfig, the generator-parity ones included), and it never parsed
// since it last appeared. A file Lstat does not see is no policy (legal);
// a broken file that has parsed before keeps serving that content. nil
// when every present file is usable or served from its last good content,
// and always for a Manager not built by NewManager.
func (m *Manager) Unavailable() error {
	if m == nil || m.files == nil {
		return nil
	}
	bad := m.files.unavailableFiles()
	if len(bad) == 0 {
		return nil
	}
	return fmt.Errorf("%w: %s", ErrUnavailable, strings.Join(bad, "; "))
}

// SetOpenOnUnavailable installs the --policy-unavailable-open escape hatch:
// CheckAvailable then lets a write through an unavailable policy (each one
// logged and counted in OpenPasses) instead of refusing it.
func (m *Manager) SetOpenOnUnavailable(on bool) { m.openOnUnavailable.Store(on) }

// OpenPasses is how many writes CheckAvailable let through an unavailable
// policy under --policy-unavailable-open (tenant_api_policy_unavailable_open_total).
func (m *Manager) OpenPasses() int64 {
	if m == nil {
		return 0
	}
	return m.openPasses.Load()
}

// CheckAvailable is the one gate the direct-mode writes that read the
// domain policy call before judging ONE write — a PUT /tenants/{id}, or one
// routing-touching op of POST /tenants/batch or /groups/{id}/batch (at
// execution time, sync or async): nil when the policy is available, else
// an error wrapping ErrUnavailable (the handler answers 503). Under
// --policy-unavailable-open it returns nil instead and logs and counts the
// write it let through — once per write: a batch counts each op it lets
// through, never the batch itself (RefusesWrites is the batch-level gate).
// what names the write in that log line.
func (m *Manager) CheckAvailable(what string) error {
	err := m.Unavailable()
	if err == nil {
		return nil
	}
	if m.openOnUnavailable.Load() {
		m.openPasses.Add(1)
		slog.Warn("policy: domain policy unavailable, write let through (--policy-unavailable-open)",
			"write", what, "reason", err.Error())
		return nil
	}
	return err
}

// RefusesWrites is CheckAvailable's verdict without letting anything
// through: the ErrUnavailable error when the policy is unavailable and
// --policy-unavailable-open is off, else nil — nothing logged, nothing
// counted. A batch request is gated with it before its ops run; each op
// then goes through CheckAvailable, which counts the op it lets through.
func (m *Manager) RefusesWrites() error {
	err := m.Unavailable()
	if err == nil || m.openOnUnavailable.Load() {
		return nil
	}
	return err
}

// FileNames are the conf.d root files the domain policies are read from, in
// the order they are applied — name order, as the route generator
// (_grar_parse._POLICY_FILENAMES, its root files read in name order) and
// routingpolicy.LoadRoot read them. Both may exist: their domains add up,
// and a domain both declare is the later file's (`.yml`), whole — the
// generator's `dict.update` of `domain_policies`. Neither reader treats the
// pair as an error.
var FileNames = []string{"_domain_policy.yaml", "_domain_policy.yml"}

// policySource is one policy file as read: its name and bytes, or (Err) why
// a file Lstat sees could not be read.
type policySource struct {
	Name string `json:"name"`
	Data []byte `json:"data"`
	Err  string `json:"err,omitempty"`
}

// readSources reads every FileNames file present in configDir, in order. A
// file Lstat does not see is skipped (no policy). One Lstat sees but that
// cannot be read — a directory, a dangling symlink (os.ReadFile reports
// "not exist" for it, which is why Lstat decides presence), no permission —
// is returned with Err set: present, and unusable (hub #2486 Q7-2).
func readSources(configDir string) []policySource {
	var out []policySource
	for _, name := range FileNames {
		path := filepath.Join(configDir, name)
		if _, err := os.Lstat(path); err != nil {
			if os.IsNotExist(err) {
				continue
			}
			out = append(out, policySource{Name: name, Err: fmt.Sprintf("stat %s: %v", path, err)})
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			out = append(out, policySource{Name: name, Err: fmt.Sprintf("read %s: %v", path, err)})
			continue
		}
		out = append(out, policySource{Name: name, Data: data})
	}
	return out
}

// parseSources is LoadSnapshot's parse: each file (parseConfig), laid over
// each other in order, a later file's domain replacing an earlier one's of
// the same name. One file that cannot be read or does not parse fails the
// whole read (fail-closed: PR mode refuses rather than judge on a partial
// policy).
func parseSources(configDir string, srcs []policySource) (*DomainPolicyConfig, error) {
	merged := emptyConfig()
	for _, s := range srcs {
		if s.Err != "" {
			return nil, errors.New(s.Err)
		}
		cfg, err := parseConfig(s.Data)
		if err != nil {
			return nil, fmt.Errorf("parse %s: %w", filepath.Join(configDir, s.Name), err)
		}
		mergeDomains(merged, cfg)
	}
	return merged, nil
}

func mergeDomains(dst, src *DomainPolicyConfig) {
	for name, dp := range src.DomainPolicies {
		dst.DomainPolicies[name] = dp
	}
}

// fileWiseParser is the watcher's parse (#2486 review B1): each file on its
// own. A file that does not parse contributes its OWN last good content —
// none if it never parsed, i.e. it is skipped, as the route generator skips
// it — and the others apply; then the files are merged in FileNames order.
// So a broken `.yml` beside a sound `.yaml` at startup leaves the `.yaml`
// enforced (whole-read failure left the watcher empty: fail-open), and on a
// hot reload one broken file does not hold back the other's update. A
// removed file forgets its last good content. Called under the watcher's
// load lock, so lastGood needs no lock of its own.
//
// A file that is present but cannot be read (policySource.Err) is a failed
// file like one that does not parse. A failed file with no last good content
// is recorded in unavailable (hub #2486 Q7-2), which Manager.Unavailable
// reads from request goroutines — hence its own lock.
type fileWiseParser struct {
	configDir string
	lastGood  map[string]*DomainPolicyConfig

	mu          sync.Mutex
	unavailable []string // "<path>: <why>", one per failed file with no last good content
}

func (p *fileWiseParser) setUnavailable(files []string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.unavailable = files
}

func (p *fileWiseParser) unavailableFiles() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.unavailable...)
}

// parse returns the merged snapshot and, when a file failed, an error naming
// it beside that snapshot (configwatcher stores it and reports the failure).
func (p *fileWiseParser) parse(srcs []policySource) (*DomainPolicyConfig, error) {
	present := map[string]bool{}
	merged := emptyConfig()
	var failed, unavailable []string
	for _, s := range srcs {
		present[s.Name] = true
		var cfg *DomainPolicyConfig
		var err error
		if s.Err != "" {
			err = errors.New(s.Err)
		} else {
			cfg, err = parseConfig(s.Data)
		}
		if err != nil {
			path := filepath.Join(p.configDir, s.Name)
			slog.Warn("policy: file cannot be read or does not parse; its last good content (none if it never parsed) applies, the other policy file is unaffected",
				"file", path, "error", err)
			failed = append(failed, fmt.Sprintf("%s: %v", path, err))
			cfg = p.lastGood[s.Name]
			if cfg == nil {
				unavailable = append(unavailable, fmt.Sprintf("%s: %v", path, err))
			}
		} else {
			p.lastGood[s.Name] = cfg
		}
		if cfg != nil {
			mergeDomains(merged, cfg)
		}
	}
	for name := range p.lastGood {
		if !present[name] {
			delete(p.lastGood, name)
		}
	}
	p.setUnavailable(unavailable)
	if len(failed) > 0 {
		return merged, fmt.Errorf("%s", strings.Join(failed, "; "))
	}
	return merged, nil
}

// NewManager creates a Manager that reads the domain policies of configDir
// (FileNames) and hot-reloads them: one watcher over both files, so a change
// to either is picked up (their bytes are hashed together), each file parsed
// on its own (fileWiseParser).
func NewManager(configDir string) *Manager {
	fw := &fileWiseParser{configDir: configDir, lastGood: map[string]*DomainPolicyConfig{}}
	read := func() ([]byte, bool, error) {
		srcs := readSources(configDir)
		if len(srcs) == 0 {
			// Neither file: the watcher stores empty without parsing, so
			// the removed files forget their last good content here, and
			// no policy is no unavailable policy.
			clear(fw.lastGood)
			fw.setUnavailable(nil)
			return nil, false, nil
		}
		data, err := json.Marshal(srcs)
		return data, true, err
	}
	parse := func(data []byte) (*DomainPolicyConfig, error) {
		var srcs []policySource
		if err := json.Unmarshal(data, &srcs); err != nil {
			return nil, err
		}
		return fw.parse(srcs)
	}
	desc := filepath.Join(configDir, "_domain_policy.{yaml,yml}")
	w, err := configwatcher.NewWithReader(desc, "policy", read, parse, emptyConfig)
	if err != nil {
		slog.Warn("policy: initial load failed", "error", err)
	}
	return &Manager{Watcher: w, files: fw}
}

// LoadSnapshot reads configDir's domain policies ONCE, with no watcher: the
// same files NewManager watches (FileNames), the same parser and merge
// (parseSources), and the same answer when neither exists (emptyConfig).
// Unlike the watcher, which keeps its last good snapshot when a file breaks,
// an unreadable or unparseable file is an error here — a directory or a
// dangling symlink included (hub #2486 Q7-2: Lstat decides presence): the
// caller (PR mode, judging the fresh base the branch is cut from — batch B2
// #2341, PUT #2486) refuses the write rather than judge it on a policy it
// cannot read.
func LoadSnapshot(configDir string) (*Manager, error) {
	srcs := readSources(configDir)
	cfg, err := parseSources(configDir, srcs)
	if err != nil {
		return nil, err
	}
	// configwatcher.NewForTest is also the production in-memory constructor
	// (see rbac.NewCandidate): no path, no watch loop.
	return &Manager{Watcher: configwatcher.NewForTest("policy", cfg)}, nil
}

// NewForTest returns a Manager pre-populated with cfg and no file
// path. WatchLoop becomes a no-op; only the embedded check methods
// are exercised. Intended for unit tests.
func NewForTest(cfg *DomainPolicyConfig) *Manager {
	return &Manager{Watcher: configwatcher.NewForTest("policy", cfg)}
}

func emptyConfig() *DomainPolicyConfig {
	return &DomainPolicyConfig{DomainPolicies: make(map[string]DomainPolicy)}
}

// parseConfig parses one policy file. `domain_policies:` present and not a
// mapping is an error — the shape check da-guard reports as
// domain_policy_unusable (routingpolicy.DomainPoliciesShapeError, #2659):
// null, `~` and a bare key read as an empty policy before, so a hot reload
// to one of them dropped every constraint while da-guard and the
// generator's --strict called the file unusable. Now the watcher keeps the
// file's last good content and LoadSnapshot (PR mode) refuses. So is a
// require_critical_escalation PyYAML refuses (B1, hub #2486 PR-7c round 2):
// the generator drops the whole file over it.
func parseConfig(data []byte) (*DomainPolicyConfig, error) {
	if err := routingpolicy.DomainPoliciesShapeError(data); err != nil {
		return nil, err
	}
	var cfg DomainPolicyConfig
	// UnmarshalPolicy, not yaml.Unmarshal: a `!!null`-tagged
	// require_critical_escalation is read as PyYAML reads it (#2325).
	if err := routingpolicy.UnmarshalPolicy(data, &cfg); err != nil {
		return nil, err
	}
	if cfg.DomainPolicies == nil {
		cfg.DomainPolicies = make(map[string]DomainPolicy)
	}
	for _, name := range sortedDomains(&cfg) {
		esc := cfg.DomainPolicies[name].Constraints.RequireCriticalEscalation
		if esc.Refused != nil {
			return nil, fmt.Errorf("domain policy %q: require_critical_escalation cannot be read by the route generator "+
				"(%v), which drops this file; set it to true or false (unquoted)", name, esc.Refused)
		}
		v := esc.Value
		if _, isBool := v.(bool); v != nil && !isBool {
			slog.Warn("policy: require_critical_escalation is not a boolean; the constraint is not enforced",
				"domain", name, "value", fmt.Sprint(v))
		}
	}
	return &cfg, nil
}

// CheckWrite validates a flat batch patch against applicable domain policies.
// Returns violations (empty if the write is allowed).
//
// tenantID: the target tenant.
// patch: the key-value pairs being written.
//
// It judges the flat `_routing_receiver_type` key only. A tenant's routing as
// the generator renders it — `_routing_defaults`, the referenced routing
// profile and the tenant's `_routing`, main receiver, overrides and routes —
// is CheckTenantRouting's (#2280); the nested `_routing.receiver.type` key
// this used to read saw the tenant's own main receiver only, and PUT now goes
// through CheckTenantRouting instead.
func (m *Manager) CheckWrite(tenantID string, patch map[string]string) []Violation {
	receiverType, ok := patch["_routing_receiver_type"]
	if !ok {
		return nil
	}
	cfg := m.Get()
	var violations []Violation
	for _, domainName := range sortedDomains(cfg) {
		dp := cfg.DomainPolicies[domainName]
		if !isTenantInPolicy(dp.Tenants, tenantID) {
			continue
		}
		violations = append(violations, checkReceiverType(domainName, dp.Constraints, receiverType, "", "")...)
	}
	return violations
}

// RoutingPolicies is the receiver-type part of the loaded domain policies, in
// the shape pkg/routingpolicy judges.
func (m *Manager) RoutingPolicies() []routingpolicy.Policy {
	cfg := m.Get()
	out := make([]routingpolicy.Policy, 0, len(cfg.DomainPolicies))
	for _, name := range sortedDomains(cfg) {
		dp := cfg.DomainPolicies[name]
		out = append(out, routingpolicy.Policy{
			Domain:                 name,
			Tenants:                dp.Tenants,
			ForbiddenReceiverTypes: dp.Constraints.ForbiddenReceiverTypes,
			AllowedReceiverTypes:   dp.Constraints.AllowedReceiverTypes,
			AllowedListNonEmpty:    len(dp.Constraints.AllowedReceiverTypes) > 0,
			// #2325: only a boolean true (parseConfig logs anything else).
			RequireCriticalEscalation: dp.Constraints.RequireCriticalEscalation.Value == true,
		})
	}
	return out
}

// CheckTenantRouting judges one tenant's routing as the route generator
// renders it (#2280): block is the tenant's config block (its `_routing` and
// `_routing_profile`), resolved over layers (`_routing_defaults` → profile →
// the tenant's `_routing`, `{{tenant}}` substituted), and every receiver the
// result renders — main route, overrides, routes — is judged against the
// receiver-type constraints of each policy that lists the tenant.
// forbidden_receiver_types and allowed_receiver_types are independent tests,
// so one receiver can yield two violations (the generator's --strict
// semantics). A tenant with no resolved routing yields none.
//
// #2325: a policy with `require_critical_escalation: true` also refuses a
// routing whose severity=critical alerts reach no pagerduty receiver
// (routingpolicy.CheckCriticalEscalation; constraint
// require_critical_escalation, Target `receiver`). The non-blocking half —
// a non-pagerduty destination that still catches some critical alerts first
// — is JudgeTenantRouting's advisories.
func (m *Manager) CheckTenantRouting(tenantID string, block map[string]any, layers routingpolicy.Layers) []Violation {
	violations, _ := m.JudgeTenantRouting(tenantID, block, layers)
	return violations
}

// JudgeTenantRouting is CheckTenantRouting plus its advisories: one message
// per `require_critical_escalation` leak (#2325), never blocking. Each names
// the tenant (`tenant=<id>`), so it reads alone in a batch-level list.
func (m *Manager) JudgeTenantRouting(tenantID string, block map[string]any, layers routingpolicy.Layers) ([]Violation, []string) {
	return m.judge(tenantID, block, layers, false)
}

// JudgeTenantBlock is JudgeTenantRouting of the block the route generator
// reads for the tenant (#2486): own — the tenant file's block — laid over
// the root platform files' `tenants.<id>` entries (layers.Overlay) by
// routingpolicy.Layers.TenantBlock, so the tenant's own `_routing` /
// `_routing_profile` win whole and a key the tenant file does not write is
// the platform's (_lib_confd.overlay_platform_tenants). A violation in a
// `_routing` the overlay supplied names the overlay as its source.
func (m *Manager) JudgeTenantBlock(tenantID string, own map[string]any, layers routingpolicy.Layers) ([]Violation, []string) {
	_, ownRouting := own["_routing"]
	_, overlayRouting := layers.Overlay[tenantID]["_routing"]
	return m.judge(tenantID, layers.TenantBlock(tenantID, own), layers, overlayRouting && !ownRouting)
}

// overlaySource describes a `_routing` the root platform overlay supplied.
const overlaySource = "the platform overlay (tenants.<id>._routing in a root platform file)"

// judge is JudgeTenantRouting; routingFromOverlay says the block's
// `_routing`, if any, is the platform overlay's, not the tenant file's.
func (m *Manager) judge(tenantID string, block map[string]any, layers routingpolicy.Layers, routingFromOverlay bool) ([]Violation, []string) {
	pols := m.RoutingPolicies()
	if len(pols) == 0 {
		return nil, nil
	}
	resolved, ok, prov, _ := routingpolicy.Resolve(tenantID, block, layers)
	if !ok {
		return nil, nil
	}
	var out []Violation
	for _, v := range routingpolicy.CheckReceiverTypes(tenantID, resolved, pols) {
		topKey := v.Target
		if i := strings.IndexByte(topKey, '['); i >= 0 {
			topKey = topKey[:i]
		}
		source := routingpolicy.SourceTenant
		if s, found := prov[topKey]; found {
			source = s
		}
		described := routingpolicy.Describe(source)
		if source == routingpolicy.SourceTenant && routingFromOverlay {
			described = overlaySource
		}
		where := fmt.Sprintf("%s (from %s)", v.Target, described)
		out = append(out, violation(v.Domain, v.Constraint, v.ReceiverType, v.Target, where))
	}
	escalation := strings.Join(routingpolicy.EscalationTypes, ", ")
	var advisories []string
	for _, f := range routingpolicy.CheckCriticalEscalation(tenantID, resolved, pols) {
		if !f.Verdict.Compliant() {
			out = append(out, Violation{
				Domain:     f.Domain,
				Constraint: routingpolicy.ConstraintRequireCriticalEscalation,
				Target:     routingpolicy.MainReceiverRef,
				Message: fmt.Sprintf("severity=critical alerts reach no receiver of type %s (main receiver type '%s', "+
					"and no routes entry matches severity=critical with such a receiver), but domain policy '%s' "+
					"requires critical escalation", escalation, routingpolicy.ReceiverType(resolved["receiver"]), f.Domain),
			})
			continue
		}
		for _, l := range f.Verdict.Leaks {
			if l.Ref == routingpolicy.MainReceiverRef {
				advisories = append(advisories, fmt.Sprintf(
					"tenant=%s: domain policy '%s': severity=critical alerts that no sub-route catches go to the main "+
						"receiver (type '%s'), not a receiver of type %s", tenantID, f.Domain, l.ReceiverType, escalation))
				continue
			}
			advisories = append(advisories, fmt.Sprintf(
				"tenant=%s: domain policy '%s': %s (%s) receiver type '%s' catches alerts with %s before any receiver "+
					"of type %s does, so they never reach one", tenantID, f.Domain, l.Ref,
				routingpolicy.FormatLabels(l.Match), l.ReceiverType, routingpolicy.FormatLabels(l.Caught), escalation))
		}
	}
	return out, advisories
}

func sortedDomains(cfg *DomainPolicyConfig) []string {
	names := make([]string, 0, len(cfg.DomainPolicies))
	for name := range cfg.DomainPolicies {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// PolicyForTenant returns the domain name and policy for a tenant, or empty if none applies.
func (m *Manager) PolicyForTenant(tenantID string) (string, *DomainPolicy, bool) {
	cfg := m.Get()
	for name, dp := range cfg.DomainPolicies {
		if isTenantInPolicy(dp.Tenants, tenantID) {
			return name, &dp, true
		}
	}
	return "", nil, false
}

func isTenantInPolicy(tenants []string, tenantID string) bool {
	for _, t := range tenants {
		if t == tenantID {
			return true
		}
	}
	return false
}

// checkReceiverType judges one receiver type against one domain's
// constraints. forbidden_receiver_types and allowed_receiver_types are
// INDEPENDENT tests (#2280, the route generator's semantics): a type on the
// forbidden list that is also missing from the allowed list is two
// violations. target / where name the route in the violation ("" for the
// flat batch key).
func checkReceiverType(domain string, c Constraints, receiverType, target, where string) []Violation {
	var violations []Violation
	for _, forbidden := range c.ForbiddenReceiverTypes {
		if receiverType == forbidden {
			violations = append(violations, violation(domain, "forbidden_receiver_types", receiverType, target, where))
			break
		}
	}
	if len(c.AllowedReceiverTypes) > 0 {
		allowed := false
		for _, a := range c.AllowedReceiverTypes {
			if receiverType == a {
				allowed = true
				break
			}
		}
		if !allowed {
			violations = append(violations, violation(domain, "allowed_receiver_types", receiverType, target, where))
		}
	}
	return violations
}

// violation is one Violation with its operator message.
func violation(domain, constraint, receiverType, target, where string) Violation {
	subject := fmt.Sprintf("receiver type '%s'", receiverType)
	if where != "" {
		subject += " at " + where
	}
	msg := fmt.Sprintf("%s is forbidden by domain policy '%s'", subject, domain)
	if constraint == "allowed_receiver_types" {
		msg = fmt.Sprintf("%s is not in the allowed list for domain policy '%s'", subject, domain)
	}
	return Violation{Domain: domain, Constraint: constraint, Target: target, Message: msg}
}
