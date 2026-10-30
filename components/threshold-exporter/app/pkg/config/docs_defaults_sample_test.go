package config

// docs_defaults_sample_test.go — every `_defaults.yaml` sample in docs/ must be
// a file this loader can actually load.
//
// Why this exists, and why it is a GO test rather than another Python lint:
// `_defaults.yaml` is described by three artifacts, and only one of them
// governs what actually happens at runtime.
//
//   1. ThresholdConfig (types.go) — `Defaults map[string]float64`, no custom
//      UnmarshalYAML. THIS is what loads. It rejects quoted values ("80" is a
//      !!str, not a float64) and any nested mapping under `defaults:`.
//   2. docs/schemas/platform-defaults.schema.json — deliberately leaves the
//      values loose ("only the top-level `defaults` key itself is guarded"),
//      so it is structurally incapable of catching either shape.
//   3. scripts/tools/ops/validate_config.py — the command the docs themselves
//      tell readers to run. It used to return PASS/exit 0 on a sample the
//      loader cannot decode at all. Since #2359 (#1414) its `root_defaults`
//      row fails a conf.d-ROOT `_defaults.yaml` the loader would drop or
//      decode to 0 — but that is a Python re-statement of this decoder, and
//      it judges a config TREE, not a markdown sample.
//
// So the only faithful oracle is the loader, which is what this test uses —
// the same move as `mdbook test` for Rust, testable Examples for Go, and the
// Kubernetes website running `kubectl apply --dry-run=server` over its own
// example manifests.
//
// ⛔ Why a decode failure is worth a CI gate rather than a doc-review habit:
// the failure is file-GLOBAL — one bad scalar takes the whole `defaults:` block
// down with it — and how loudly it fails depends on which consumer reads it:
//
//   - The exporter's own scanner IS loud: parsePartialConfig (flat_build.go)
//     emits `metrics.IncParseFailure` plus an ERROR log naming the file.
//   - mergeTenantConfig (merge_tenant.go) still DISCARDS the failed decode and
//     carries on with zero platform defaults, and still exports no metric for
//     it. That is the path a tenant's config is validated against, so a broken
//     root `_defaults.yaml` makes ValidateTenantKeys judge legitimate tenant
//     keys "unknown" and refuse the write. Until #1330 it did this without a
//     word; it now logs an ERROR naming the file, so the two consumers are at
//     least both audible — but only the exporter side is also observable in
//     metrics.
//
// Until #2359 (#1414) the operator's own feedback loop stayed green here too:
// scripts/tools/ops/validate_config.py returned PASS/exit 0 on a root
// `_defaults.yaml` that cannot load. It now fails that row, but it reads a
// config tree, not the markdown samples this file checks, so this test is
// still their oracle.
//
// Scope boundary: this pins the LOADER contract only. Whether a key is one the
// threshold-registry knows (a dead-series risk) is the reachability class
// tracked by #1189/TRK-337, and whether `_routing_*` blocks satisfy the
// Alertmanager generator is _grar_parse.py's contract — neither is asserted
// here. What IS covered is that re-nesting a `_routing_*` key back under
// `defaults:` breaks decode, which is exactly how it shipped broken.

import (
	"regexp"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// docYAMLBlock is one fenced ```yaml block lifted out of a Markdown file.
type docYAMLBlock struct {
	file string // repo-relative, forward slashes
	line int    // 1-indexed line of the opening fence
	info string // what follows ```yaml on the fence line, trimmed ("{.confd}"; "" for none)
	body string
}

// yamlFenceRe matches the opening line of a yaml fence (trimmed): exactly
// ```yaml or ```yml, then nothing, or what follows after a blank or `{` — the
// fence attributes (```yaml {.confd}, #3416). Mirrors FENCE_RE in
// scripts/tools/_lib_doc_yaml_inventory.py; TestDocsYAMLInventoryParity holds
// the two extractors to the same units. Any other spelling of a yaml fence is
// the inventory's error, so docs/ holds none for this extractor to miss.
var yamlFenceRe = regexp.MustCompile("^```(?:yaml|yml)(?:[ \\t{].*)?$")

// anyFenceRe matches the opening line of any fence (CommonMark): three or
// more backticks or tildes, then the info string.
var anyFenceRe = regexp.MustCompile("^(`{3,}|~{3,})(.*)$")

// fenceTrim is what a fence line is compared without: ASCII blanks only, as
// the inventory (FENCE_TRIM) — strings.TrimSpace would also take U+3000 away.
const fenceTrim = " \t\r"

// extractYAMLBlocks pulls fenced ```yaml / ```yml blocks out of Markdown,
// attributes and all: a fence that declares what it is (```yaml {.k8s}) is
// still a yaml fence. Every fence is tracked as CommonMark tracks it (the
// closing fence is the same character, at least as long, with nothing after
// it), so a ``` line inside another fence is that fence's text. Whether its
// attributes are ones the inventory knows is the inventory's to say, not this
// extractor's.
func extractYAMLBlocks(file, content string) []docYAMLBlock {
	var out []docYAMLBlock
	var buf []string
	openChar, openLen, start, info, isYAML := "", 0, 0, "", false

	for i, line := range strings.Split(content, "\n") {
		trimmed := strings.Trim(line, fenceTrim)
		if openChar == "" {
			m := anyFenceRe.FindStringSubmatch(trimmed)
			if m == nil || (m[1][0] == '`' && strings.Contains(m[2], "`")) {
				continue
			}
			openChar, openLen, start, buf = m[1][:1], len(m[1]), i+1, nil
			isYAML = yamlFenceRe.MatchString(trimmed)
			info = ""
			if isYAML {
				rest := strings.TrimPrefix(trimmed, "```")
				if strings.HasPrefix(rest, "yaml") {
					rest = strings.TrimPrefix(rest, "yaml")
				} else {
					rest = strings.TrimPrefix(rest, "yml")
				}
				info = strings.Trim(rest, fenceTrim)
			}
			continue
		}
		if trimmed != "" && strings.Trim(trimmed, openChar) == "" && len(trimmed) >= openLen {
			if isYAML && len(buf) > 0 {
				out = append(out, docYAMLBlock{file: file, line: start, info: info, body: strings.Join(buf, "\n")})
			}
			openChar, isYAML = "", false
			continue
		}
		if isYAML {
			buf = append(buf, line)
		}
	}
	return out
}

// fileHeaderRe matches a comment line that names a YAML file, e.g.
// `# conf.d/_defaults.yaml (central)` or `# L1 finance/_defaults.yaml`.
//
// ⚠️ Anchored at column 0 on purpose, and that anchor is load-bearing. With a
// leading `\s*` this also matches an INDENTED comment that merely mentions a
// filename in passing — `    # 其他項目省略，會用 _defaults.yaml 的預設`, which
// sits inside a `tenants:` block in for-domain-experts.md. Treating that as a
// file header severs a block mid-mapping: the tail is then validated as its own
// document, and the head silently loses the entries below it. A real file header
// in these illustrations always starts at column 0; an indented `#` is prose.
var fileHeaderRe = regexp.MustCompile(`^#.*\.ya?ml\b`)

// splitConcatenatedFiles divides a block that illustrates SEVERAL files in one
// fence (each introduced by a `# path/to/file.yaml` comment) into one segment
// per file. Without this, a perfectly correct three-file illustration fails to
// decode for a bogus reason — `defaults:`/`tenants:` legitimately appear once
// per file, which yaml.v3 reports as a duplicate mapping key.
//
// Splitting rather than skipping is deliberate: it makes the gate check MORE,
// not less. Every segment is still held to the loader contract on its own.
// Each segment carries its own line offset (0-based, relative to the first line
// INSIDE the fence) so a failure points at the offending file, not at the fence.
func splitConcatenatedFiles(body string) []struct {
	offset int
	text   string
} {
	type seg = struct {
		offset int
		text   string
	}
	lines := strings.Split(body, "\n")
	var segments []seg
	var cur []string
	start := 0
	for i, line := range lines {
		if fileHeaderRe.MatchString(line) && len(cur) > 0 {
			segments = append(segments, seg{offset: start, text: strings.Join(cur, "\n")})
			cur, start = nil, i
		}
		cur = append(cur, line)
	}
	if len(cur) > 0 {
		segments = append(segments, seg{offset: start, text: strings.Join(cur, "\n")})
	}
	return segments
}

// collectDocDefaultsSamples returns every `_defaults.yaml` sample in docs/:
// the units of the docs' YAML inventory (scripts/tools/_lib_doc_yaml_inventory.py,
// #3416) that it reads as conf.d and judges a PLATFORM file (a platform key at
// the top: `defaults:`, `state_filters:`, `_routing_defaults:` …). Which unit
// is a `_defaults.yaml` is the inventory's one answer — this test used to
// have its own (a header comment naming the file, or a top-level `defaults:`
// mapping), a fourth judgement beside the drift lint's two and the unit
// lint's. An `.illustrative` unit, a refused one and one with an inventory
// error are not samples; an `.expect-invalid` counter-example is (wrong
// against the schema on purpose, it must still load). Fail-closed like its
// siblings in this package: a repo root it cannot find, an inventory that
// cannot be had, or zero samples is a broken test, not a reason to pass.
//
// docs/adr/** IS in scope. It was excluded when this gate landed (#1327), on
// the reasoning that an ADR is a historical decision record whose snippets
// illustrate a proposed MODEL, so editing one to satisfy today's loader would
// rewrite the record. That exclusion was reversed deliberately, for two
// reasons:
//
//   - The concrete case that motivated it is gone. #1327's own comment named
//     ADR-017's L0 sample (`_routing:` nested inside `defaults:`) as a real
//     defect it was choosing not to silence; #1330 fixed it. Nothing in
//     docs/adr/ is now knowingly broken, so keeping the exclusion buys no
//     protection from churn — it only hides the next one.
//   - The premise does not survive contact with what these samples are.
//     ADR-017's block is not a sketch of a rejected alternative; it is
//     labelled `# L0 _defaults.yaml` and shows the shipping inheritance
//     model. A reader copies it exactly as they would copy the
//     platform-engineer guide. "Historical record" protects the DECISION and
//     its rationale, not a config sample that silently stopped matching the
//     loader.
//
// Sibling doc lints (_version_patterns.DOC_MAP_SKIP_DIRS) still skip adr/ —
// they police metadata, where "don't churn the record" genuinely applies.
// This gate asserts loadability, which is timeless: a sample that cannot
// decode was never a faithful illustration of the model on any date. A
// counter-example that is not meant to load is declared in the doc
// (```yaml {.illustrative reason="…"}), never by narrowing this walk.
func collectDocDefaultsSamples(t *testing.T) []docYAMLBlock {
	t.Helper()
	var samples []docYAMLBlock
	for _, u := range docYAMLInventory(t).Units {
		if u.Consumer == nil || *u.Consumer != "confd" || !u.Read || u.Exemption != nil ||
			len(u.Errors) > 0 || u.ConfdKind == nil || *u.ConfdKind != "platform" {
			continue
		}
		samples = append(samples, docYAMLBlock{file: u.Path, line: u.Line, body: u.Text})
	}
	if len(samples) == 0 {
		t.Fatal("found zero _defaults.yaml samples in docs/ — the inventory is broken, " +
			"or the samples moved; a gate that measures nothing must fail loudly")
	}
	return samples
}

// TestDocsDefaultsGateCoversADRs exit-locks the docs/adr/** scope (see the walk
// comment in collectDocDefaultsSamples).
//
// Without it, re-adding a SkipDir shrinks this gate back with NO other symptom:
// every remaining sample still decodes, so the suite stays green while the ADR
// samples quietly stop being checked. That is the same "gate exists but covers
// less than it claims" shape the original exclusion already cost once — the
// defect it was hiding (ADR-017's `_routing:` nested under `defaults:`) sat
// there until someone went looking.
//
// ⚠️ This asserts SCOPE, not that ADRs must forever carry samples. If the ADRs
// legitimately stop documenting `_defaults.yaml` (superseded, archived, sample
// removed), delete this test in the SAME commit and say so in the message —
// do not narrow the walk to make it pass.
func TestDocsDefaultsGateCoversADRs(t *testing.T) {
	samples := collectDocDefaultsSamples(t)
	adr := 0
	for _, s := range samples {
		if strings.HasPrefix(s.file, "docs/adr/") {
			adr++
		}
	}
	if adr == 0 {
		t.Errorf("no `_defaults.yaml` samples collected from docs/adr/** (total=%d). "+
			"Either the adr/ scope was removed (a SkipDir, a changed walk root) — "+
			"restore it — or the ADRs no longer carry samples, in which case remove "+
			"this test deliberately rather than narrowing the walk.", len(samples))
	}
}

// TestDocsDefaultsSamplesDecode asserts every documented `_defaults.yaml`
// sample decodes under the production loader.
//
// Shipped broken until #1321 follow-up: 8/8 samples failed here, because every
// threshold value was quoted ("80" → `cannot unmarshal !!str into float64`) and
// `_routing_defaults` / `_routing_enforced` were indented under `defaults:`
// instead of being its top-level siblings (`!!map into float64`).
func TestDocsDefaultsSamplesDecode(t *testing.T) {
	for _, s := range collectDocDefaultsSamples(t) {
		var cfg ThresholdConfig
		if err := yaml.Unmarshal([]byte(s.body), &cfg); err != nil {
			t.Errorf("%s:%d — documented _defaults.yaml sample does NOT load:\n    %v\n"+
				"    Values under `defaults:` are bare numbers (Defaults is map[string]float64);\n"+
				"    `_routing_defaults` / `_routing_enforced` / `state_filters` / `optional_overrides`\n"+
				"    are TOP-LEVEL siblings of `defaults:`, never nested inside it.\n"+
				"    A reader who copies this gets zero defaults and no error (merge_tenant.go swallows it).",
				s.file, s.line, err)
		}
	}
}

// TestDocsDefaultsSamplesHaveNoTenantOnlyKeys pins the two shapes that decode
// cleanly and are still wrong — neither is reachable by a YAML or JSON-Schema
// check, only by knowing what resolve.go does with the key.
//
//   - `<metric>_critical` in `defaults:` is NOT the critical tier.
//     resolveBaseRows iterates the defaults map and does not skip the suffix,
//     so it emits user_threshold{metric="X_critical", severity="warning"} — a
//     series no rule pack consumes — while the tenant still has no critical
//     tier. The supported shape is a TENANT override, which resolveCriticalRows
//     turns into {metric="X", severity="critical"}.
//   - a dimensional key (`metric{label="v"}`) in `defaults:` does not give
//     dimensional thresholds a default path. resolveDimensionalRows is
//     tenant-only and never consults the defaults map; put one here and
//     parseMetricKey bakes the label segment into the metric NAME, emitting
//     metric="cpu{env=\"prod\"}". ValidateTenantKeys reports nothing.
func TestDocsDefaultsSamplesHaveNoTenantOnlyKeys(t *testing.T) {
	for _, s := range collectDocDefaultsSamples(t) {
		var cfg ThresholdConfig
		if err := yaml.Unmarshal([]byte(s.body), &cfg); err != nil {
			continue // TestDocsDefaultsSamplesDecode owns this failure
		}
		for key := range cfg.Defaults {
			if strings.HasSuffix(key, criticalSuffix) {
				component, metric := parseMetricKey(key)
				t.Errorf("%s:%d — `defaults:` declares %q, which is NOT the critical tier: "+
					"resolveBaseRows emits {component=%q, metric=%q, severity=\"warning\"} — a series "+
					"no rule pack consumes — and the tenant still has no critical tier. "+
					"The critical tier is a TENANT override on the base key %q.",
					s.file, s.line, key, component, metric, strings.TrimSuffix(key, criticalSuffix))
			}
			if strings.Contains(key, "{") {
				t.Errorf("%s:%d — `defaults:` declares dimensional key %q. Dimensional thresholds are "+
					"tenant-only (resolveDimensionalRows never reads defaults); here the label segment "+
					"is baked into the metric NAME instead.", s.file, s.line, key)
			}
		}
	}
}
