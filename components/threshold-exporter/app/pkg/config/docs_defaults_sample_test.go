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
//      tell readers to run. Measured: it returns PASS/exit 0 on a sample the
//      loader cannot decode at all, and the byte-identical verdict on the
//      corrected one. Zero discriminating power for this class.
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
//   - The exporter's own scanner IS loud: parsePartialConfig (flat_scanner.go)
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
// Either way the operator's own feedback loop stays green: scripts/tools/ops/
// validate_config.py returns PASS/exit 0 on a sample that cannot load.
//
// Scope boundary: this pins the LOADER contract only. Whether a key is one the
// threshold-registry knows (a dead-series risk) is the reachability class
// tracked by #1189/TRK-337, and whether `_routing_*` blocks satisfy the
// Alertmanager generator is _grar_parse.py's contract — neither is asserted
// here. What IS covered is that re-nesting a `_routing_*` key back under
// `defaults:` breaks decode, which is exactly how it shipped broken.

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// docYAMLBlock is one fenced ```yaml block lifted out of a Markdown file.
type docYAMLBlock struct {
	file string // repo-relative, forward slashes
	line int    // 1-indexed line of the opening fence
	body string
}

// extractYAMLBlocks pulls fenced ```yaml / ```yml blocks out of Markdown.
// Deliberately literal about the fence: docs also carry ```bash and directory
// trees, and only blocks the author tagged as YAML are in scope.
func extractYAMLBlocks(file, content string) []docYAMLBlock {
	var out []docYAMLBlock
	var buf []string
	inYAML, start := false, 0

	for i, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(strings.TrimSuffix(line, "\r"))
		switch {
		case !inYAML && (trimmed == "```yaml" || trimmed == "```yml"):
			inYAML, start, buf = true, i+1, nil
		case inYAML && trimmed == "```":
			inYAML = false
			if len(buf) > 0 {
				out = append(out, docYAMLBlock{file: file, line: start, body: strings.Join(buf, "\n")})
			}
		case inYAML:
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

// isPlatformDefaultsSample reports whether a block claims to be a
// `_defaults.yaml`. Two independent selectors, unioned on purpose:
//
//   - the header comment names the file (catches the `_routing_defaults:` /
//     `_routing_enforced:` samples, which carry no `defaults:` key at all), and
//   - the document has a top-level `defaults:` mapping (catches a sample whose
//     author forgot the header comment).
//
// A block that does not parse as a YAML mapping is not a config sample at all
// (prose, a directory tree in a mislabeled fence) and is left alone — this test
// pins the platform-defaults contract, not Markdown fence hygiene.
//
// ⚠️ Known blind spot, stated rather than papered over: a sample that is BOTH
// malformed AND carries no `_defaults.yaml` header comment fails the generic
// parse here and is therefore skipped, not reported. The header selector is what
// keeps that hole small — every sample in the platform-engineer guide names its
// file. Widening this to "every unparseable ```yaml block is an error" is a
// separate call: docs/ currently has 17 such blocks, nearly all directory-tree
// diagrams in mislabeled fences, so it would land as noise, not signal.
func isPlatformDefaultsSample(body string) bool {
	if firstLine, _, _ := strings.Cut(body, "\n"); strings.Contains(firstLine, "_defaults.yaml") {
		return true
	}
	var generic map[string]any
	if err := yaml.Unmarshal([]byte(body), &generic); err != nil {
		return false
	}
	_, ok := generic["defaults"].(map[string]any)
	return ok
}

// collectDocDefaultsSamples walks docs/ and returns every `_defaults.yaml`
// sample block. Fail-closed like its siblings in this package: a repo root it
// cannot find is a broken test, not a reason to pass quietly.
func collectDocDefaultsSamples(t *testing.T) []docYAMLBlock {
	t.Helper()
	root := findRepoRootForRegistry(t)
	docsDir := filepath.Join(root, "docs")
	if fi, err := os.Stat(docsDir); err != nil || !fi.IsDir() {
		t.Fatalf("docs/ not found under repo root %s — this gate must not pass by skipping", root)
	}

	var samples []docYAMLBlock
	err := filepath.WalkDir(docsDir, func(path string, d os.DirEntry, werr error) error {
		if werr != nil {
			return werr
		}
		// docs/adr/** IS in scope. It was excluded when this gate landed
		// (#1327), on the reasoning that an ADR is a historical decision
		// record whose snippets illustrate a proposed MODEL, so editing one to
		// satisfy today's loader would rewrite the record. That exclusion was
		// reversed deliberately, for two reasons:
		//
		//   - The concrete case that motivated it is gone. #1327's own comment
		//     named ADR-017's L0 sample (`_routing:` nested inside `defaults:`)
		//     as a real defect it was choosing not to silence; #1330 fixed it.
		//     Nothing in docs/adr/ is now knowingly broken, so keeping the
		//     exclusion buys no protection from churn — it only hides the next
		//     one.
		//   - The premise does not survive contact with what these samples are.
		//     ADR-017's block is not a sketch of a rejected alternative; it is
		//     labelled `# L0 _defaults.yaml` and shows the shipping inheritance
		//     model. A reader copies it exactly as they would copy the
		//     platform-engineer guide. "Historical record" protects the
		//     DECISION and its rationale, not a config sample that silently
		//     stopped matching the loader.
		//
		// Sibling doc lints (_version_patterns.DOC_MAP_SKIP_DIRS,
		// check_doc_freshness.py) still skip adr/ — they police freshness
		// metadata, where "don't churn the record" genuinely applies. This gate
		// asserts loadability, which is timeless: a sample that cannot decode
		// was never a faithful illustration of the model on any date.
		//
		// ⚠️ The cost this scope does carry, stated plainly: the repo has an
		// `#### ❌ 錯誤` convention for deliberate counter-examples, and a
		// counter-example that IS a `_defaults.yaml` would be failed here on
		// purpose-built wrongness. No such sample exists today — the existing
		// ❌ blocks are recording-rule sequences, which isPlatformDefaultsSample
		// does not select — and the house style for this file is "show the
		// correct shape, put the trap in a comment" (see the platform-engineer
		// guide). If one is ever wanted, express it in prose or an untagged
		// fence rather than re-narrowing this walk.
		if d.IsDir() {
			return nil
		}
		if !strings.HasSuffix(d.Name(), ".md") {
			return nil
		}
		raw, rerr := os.ReadFile(path)
		if rerr != nil {
			return rerr
		}
		rel, rerr := filepath.Rel(root, path)
		if rerr != nil {
			rel = path
		}
		rel = filepath.ToSlash(rel)
		for _, b := range extractYAMLBlocks(rel, string(raw)) {
			for _, s := range splitConcatenatedFiles(b.body) {
				if isPlatformDefaultsSample(s.text) {
					samples = append(samples, docYAMLBlock{
						file: b.file,
						// b.line is the opening fence; body line 0 sits one line
						// below it, so +1 makes offset=0 point at the segment's
						// first real line instead of the ```yaml fence.
						line: b.line + s.offset + 1,
						body: s.text,
					})
				}
			}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk docs/: %v", err)
	}
	if len(samples) == 0 {
		t.Fatal("found zero _defaults.yaml samples in docs/ — the extractor is broken, " +
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
