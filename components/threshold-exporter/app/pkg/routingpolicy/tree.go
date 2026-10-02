package routingpolicy

// tree.go — the routing plane across conf.d directory levels (#2326,
// ADR-017 / ADR-016 / ADR-007 "Amendment 2026-09-28"). Python twin:
// scripts/tools/ops/_grar_parse._parse_nested_config / _record_profiles /
// _apply_tenant_entries and _grar_merge.resolve_routing_defaults /
// visible_routing_profiles; the parity matrix pins the two.

import (
	"fmt"
	"io"
	"log"
	"path"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// Problem kinds of the hierarchical routing plane. The first three refuse
// the whole tree: the route generator exits 2 on them in every mode (see
// IsBlocking). ProblemDuplicateTenant is refused too, but by the generator's
// #2315 duplicate-tenant check (exit 1, it runs first) and in da-guard by the
// exporter's DuplicateTenantError, so it is not one of IsBlocking's kinds. ProblemDomainPolicyOutOfScope is a domain-policy finding
// (ERROR under the generator's --strict, WARN otherwise; da-guard reports
// every domain-policy finding as an error).
const (
	// ProblemRoutingEnforcedBelowRoot: `_routing_enforced` in a file below
	// the conf.d root. It is read only at the root (ADR-017 (b)).
	ProblemRoutingEnforcedBelowRoot = "routing_enforced_below_root"
	// ProblemRoutingDefaultsNullBelowRoot: `receiver` or `overrides` written
	// as null in a subdirectory level's `_routing_defaults` (ADR-017 (a)):
	// every tenant below without its own would lose it.
	ProblemRoutingDefaultsNullBelowRoot = "routing_defaults_null_below_root"
	// ProblemRoutingProfileDuplicate: one routing-profile name defined in two
	// files anywhere in the tree, `.yaml` beside `.yml` at the root included
	// (ADR-007 (c)). The first definition is kept; the later file is named.
	ProblemRoutingProfileDuplicate = "routing_profile_duplicate"
	// ProblemDuplicateTenant: one tenant id declared by two tenant files
	// (ADR-017 (e)); the later file in name order is named.
	ProblemDuplicateTenant = "duplicate_tenant"
	// ProblemDomainPolicyOutOfScope: a `_domain_policy.yaml` below the root
	// names a tenant declared outside its subtree (ADR-007 (d)). The entry is
	// dropped from the policy — it is not enforced.
	ProblemDomainPolicyOutOfScope = "domain_policy_out_of_scope"
)

// IsBlocking reports whether a Problem kind refuses the whole tree.
func IsBlocking(kind string) bool {
	switch kind {
	case ProblemRoutingEnforcedBelowRoot, ProblemRoutingDefaultsNullBelowRoot,
		ProblemRoutingProfileDuplicate:
		return true
	}
	return false
}

// RootLevel is the conf.d root as a Tree level.
const RootLevel = "."

// Tree is the routing layers of a whole conf.d: the root's Layers plus what
// every subdirectory level contributes. Read-only once LoadTree returns.
type Tree struct {
	// Root is what LoadRoot reads: the root's `_routing_defaults`, its
	// routing profiles and the root platform overlay.
	Root Layers

	// Enforced is the root's `_routing_enforced` block the generator
	// renders from (nil: none, or not enabled) — read for its group_by only
	// (EnforcedGroupByInvalid, #2503).
	Enforced *Enforced

	levelDefaults map[string]map[string]any            // level → `_routing_defaults` (nil: contributes nothing)
	levelProfiles map[string]map[string]map[string]any // level → profile name → body (below the root)
	tenantDirs    map[string]string                    // tenant id → level of its tenant file
}

// ChainLevels is the directory levels BELOW the root on the way to level,
// root-first: "a/b" → ["a", "a/b"]; the root → nil.
func ChainLevels(level string) []string {
	if level == RootLevel || level == "" {
		return nil
	}
	parts := strings.Split(level, "/")
	out := make([]string, len(parts))
	for i := range parts {
		out[i] = strings.Join(parts[:i+1], "/")
	}
	return out
}

// LevelContains reports whether directory other is level itself or lies
// below it.
func LevelContains(level, other string) bool {
	if level == RootLevel || level == "" {
		return true
	}
	return other == level || strings.HasPrefix(other, level+"/")
}

// LevelOf is the Tree level of a root-relative slash path to a file.
func LevelOf(rel string) string {
	return path.Dir(strings.ReplaceAll(rel, "\\", "/"))
}

// TenantLevel is the level of the tenant file that declares tenantID (the
// first one, in name order); RootLevel when no tenant file does.
func (t Tree) TenantLevel(tenantID string) string {
	if d, ok := t.tenantDirs[tenantID]; ok {
		return d
	}
	return RootLevel
}

// LayersFor returns the Layers a tenant at level resolves over (ADR-017
// amendment (a), (c)): `_routing_defaults` of the root, then of every level
// on the way down, each top-level key replacing the one above WHOLE (a null
// included); the profiles of the root and of those levels; the root overlay.
// The result shares its maps' values with t but not the maps themselves.
func (t Tree) LayersFor(level string) Layers {
	out := Layers{Overlay: t.Root.Overlay}
	chain := ChainLevels(level)
	var defaults map[string]any
	if t.Root.Defaults != nil {
		defaults = make(map[string]any, len(t.Root.Defaults))
		for k, v := range t.Root.Defaults {
			defaults[k] = v
		}
	}
	for _, d := range chain {
		rd := t.levelDefaults[d]
		if rd == nil {
			continue
		}
		if defaults == nil {
			defaults = make(map[string]any, len(rd))
		}
		for k, v := range rd {
			defaults[k] = v
		}
	}
	out.Defaults = defaults
	if len(t.Root.Profiles) > 0 || len(chain) > 0 {
		profiles := make(map[string]map[string]any, len(t.Root.Profiles))
		for k, v := range t.Root.Profiles {
			profiles[k] = v
		}
		for _, d := range chain {
			for k, v := range t.levelProfiles[d] {
				profiles[k] = v
			}
		}
		if len(profiles) > 0 || t.Root.Profiles != nil {
			out.Profiles = profiles
		}
	}
	return out
}

var discardTreeLogger = log.New(io.Discard, "", 0)

// LoadTree reads the routing layers and the domain policies of the WHOLE
// conf.d (#2326): the root through LoadRoot, then every file below it through
// the exporter's own walker (config.ScanDirTree — hidden directories pruned,
// one defaults carrier per directory, config.TreeScan.DefaultsCarriers):
//
//   - `_routing_defaults` from each subdirectory's selected defaults carrier
//     (top level of the document), for the tenants at and below it;
//   - `_routing_enforced` below the root: ProblemRoutingEnforcedBelowRoot
//     (the root's, for its group_by only, is Tree.Enforced);
//   - `_routing_profiles.yaml` / `.yml` below the root: profiles visible to
//     that subtree; a name defined twice anywhere: ProblemRoutingProfileDuplicate;
//   - `_domain_policy.yaml` / `.yml` below the root: policies with Scope set,
//     whose `tenants:` entries outside the subtree are dropped and reported
//     (ProblemDomainPolicyOutOfScope) — so CheckReceiverTypes over the
//     returned list never applies a subtree policy outside its subtree, and a
//     tenant meets every policy that applies to it (additive);
//   - a tenant id declared by two tenant files: ProblemDuplicateTenant.
//
// Files are visited in name order (TreeScan.Keys), root files first for
// profiles (LoadRoot), as the Python reader does. skip is LoadRoot's.
func LoadTree(configDir string, skip func(rel string) bool) (Tree, []Policy, []Problem) {
	root, pols, probs, profileOrigin, enforced := loadRoot(configDir, skip)
	t := Tree{
		Root:          root,
		Enforced:      enforced,
		levelDefaults: map[string]map[string]any{},
		levelProfiles: map[string]map[string]map[string]any{},
		tenantDirs:    map[string]string{},
	}
	scan, err := config.ScanDirTree(configDir, nil, nil, discardTreeLogger)
	if err != nil {
		return t, pols, probs // LoadRoot has named an unreadable root
	}

	chosen := map[string]bool{}
	for _, abs := range scan.DefaultsCarriers().ByDir {
		chosen[abs] = true
	}
	selected := map[string]bool{}
	for k, f := range scan.Files {
		if chosen[f.AbsPath] {
			selected[k] = true
		}
	}

	declared := map[string][]string{}
	var ids []string
	for _, k := range scan.Keys {
		f := scan.Files[k]
		if f == nil || (skip != nil && skip(k)) {
			continue
		}
		for _, id := range f.TenantIDs {
			if _, seen := declared[id]; !seen {
				ids = append(ids, id)
			}
			declared[id] = append(declared[id], k)
		}
	}
	sort.Strings(ids)
	for _, id := range ids {
		files := declared[id]
		t.tenantDirs[id] = LevelOf(files[0])
		if len(files) > 1 {
			probs = append(probs, Problem{Kind: ProblemDuplicateTenant, File: files[1], Field: "tenants." + id,
				Message: fmt.Sprintf("duplicate tenant id %q: declared in both %s and %s — threshold-exporter "+
					"rejects a tree that declares one tenant in two files; keep the tenant in one file",
					id, files[0], files[1])})
		}
	}

	policyNodes := map[string]map[string]*yaml.Node{}
	policyOrigin := map[string]map[string]string{}
	var policyLevels []string
	for _, k := range scan.Keys {
		if !strings.Contains(k, "/") || (skip != nil && skip(k)) {
			continue
		}
		f := scan.Files[k]
		if f == nil || f.Data == nil {
			continue
		}
		level, base := LevelOf(k), path.Base(k)
		isPolicy := contains(policyFileNames, base)
		isProfiles := contains(profileFileNames, base)
		// isPolicy: a subtree `_domain_policy.yaml` is a policy document
		// too (#2326), so its `require_critical_escalation` gets the same
		// `!!null` handling as the root one (#2325).
		top, err := parseDoc(f.Data, isPolicy)
		if err != nil {
			switch {
			case isPolicy:
				probs = append(probs, Problem{Kind: ProblemDomainPolicyUnusable, File: k,
					Message: fmt.Sprintf("%s could not be used, so its domain policies are not enforced: %v", k, trimUnusable(err))})
			case isProfiles:
				probs = append(probs, Problem{Kind: ProblemRoutingProfilesUnusable, File: k,
					Message: fmt.Sprintf("%s could not be used, so its routing profiles are not applied: %v", k, trimUnusable(err))})
			}
			continue
		}
		if top == nil {
			continue
		}
		if lookup(top, "_routing_enforced") != nil {
			probs = append(probs, Problem{Kind: ProblemRoutingEnforcedBelowRoot, File: k, Field: "_routing_enforced",
				Message: fmt.Sprintf("%s: _routing_enforced is read only at the conf.d root — a NOC route scoped to "+
					"one subtree is not supported; move it to a root platform file or delete it", k)})
		}
		// #2326 review F3: `_routing_defaults` in a `_` file below the root
		// that is not a defaults carrier (`a/_routing.yaml`). At the root the
		// same file is read, so a move into a subdirectory silently drops it —
		// the #2291 class. An unselected carrier spelling is not reported here:
		// the exporter's multi-carrier warning already names it as ignored
		// whole (and the Python reader skips it the same way).
		if !selected[k] && !f.IsDefaults && strings.HasPrefix(base, "_") && lookup(top, "_routing_defaults") != nil {
			probs = append(probs, Problem{Kind: ProblemRoutingInUnreadLocation, File: k, Field: "_routing_defaults",
				Message: fmt.Sprintf("%s: _routing_defaults is not rendered — below the conf.d root only the directory's "+
					"defaults carrier (_defaults.yaml / _defaults.yml) carries it; move it there", k)})
		}
		if selected[k] {
			if d, present, stripped, bad, err := routingDefaultsFromNode(top); present {
				if err != nil {
					d = nil
				}
				t.levelDefaults[level] = d
				if bad != nil {
					probs = append(probs, routingDefaultsNotMapping(k, bad))
				}
				if stripped {
					probs = append(probs, Problem{Kind: ProblemRoutingDefaultsRoutes, File: k,
						Field: "_routing_defaults.routes",
						Message: fmt.Sprintf("%s: _routing_defaults.routes is not supported (every tenant below would "+
							"inherit the escalation); it is ignored — define routes in a routing profile or the tenant's _routing", k)})
				}
				for _, key := range []string{"receiver", "overrides"} {
					if v, ok := d[key]; ok && v == nil {
						probs = append(probs, Problem{Kind: ProblemRoutingDefaultsNullBelowRoot, File: k,
							Field: "_routing_defaults." + key,
							Message: fmt.Sprintf("%s: _routing_defaults.%s is null — below the conf.d root that takes the %s "+
								"away from every tenant under %s/ that does not set its own; set a value or remove the key",
								k, key, key, level)})
					}
				}
			}
		}
		if isProfiles {
			p, present, err := profilesFromNode(top)
			switch {
			case err != nil:
				probs = append(probs, Problem{Kind: ProblemRoutingProfilesUnusable, File: k,
					Field: "routing_profiles", Message: fmt.Sprintf("%s: %v", k, trimUnusable(err))})
			case present:
				for _, name := range sortedKeys(p) {
					if first, dup := profileOrigin[name]; dup {
						probs = append(probs, duplicateProfile(name, first, k))
						continue
					}
					profileOrigin[name] = k
					if t.levelProfiles[level] == nil {
						t.levelProfiles[level] = map[string]map[string]any{}
					}
					t.levelProfiles[level][name] = p[name]
				}
			}
		}
		if isPolicy {
			nodes, err := policyNodesFrom(top)
			if err != nil {
				probs = append(probs, Problem{Kind: ProblemDomainPolicyUnusable, File: k,
					Field: "domain_policies", Message: fmt.Sprintf("%s: %v", k, trimUnusable(err))})
			}
			if len(nodes) > 0 && policyNodes[level] == nil {
				policyNodes[level] = map[string]*yaml.Node{}
				policyOrigin[level] = map[string]string{}
				policyLevels = append(policyLevels, level)
			}
			for name, n := range nodes {
				policyNodes[level][name] = n
				policyOrigin[level][name] = k
			}
		}
	}

	sort.Strings(policyLevels)
	for _, level := range policyLevels {
		scoped, pprobs := buildPolicies(policyNodes[level], policyOrigin[level])
		probs = append(probs, pprobs...)
		for _, p := range scoped {
			p.Scope = level
			var kept []string
			for _, tid := range p.Tenants {
				if d, known := t.tenantDirs[tid]; known && !LevelContains(level, d) {
					probs = append(probs, Problem{Kind: ProblemDomainPolicyOutOfScope, File: policyOrigin[level][p.Domain],
						Field: "domain_policies." + p.Domain + ".tenants",
						Message: fmt.Sprintf("%s: domain policy %q names tenant %q, which lives outside this policy's "+
							"subtree %s/ — a policy below the conf.d root applies only to its own subtree, so this entry "+
							"is not enforced", policyOrigin[level][p.Domain], p.Domain, tid, level)})
					continue
				}
				kept = append(kept, tid)
			}
			p.Tenants = kept
			pols = append(pols, p)
		}
	}
	return t, pols, probs
}

func duplicateProfile(name, first, later string) Problem {
	return Problem{Kind: ProblemRoutingProfileDuplicate, File: later, Field: "routing_profiles." + name,
		Message: fmt.Sprintf("routing profile %q is defined in both %s and %s — a profile name must be unique "+
			"across the whole conf.d tree; rename or remove one of them", name, first, later)}
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
