package main

// undetermined_test.go — #2904 (ADR-036 §9 step 2): the default check judges
// no routing or domain policy; a tree holding what only the route generator
// judges is "undetermined", exit 4, below 3 and 1.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// judged is one CLI run's outcome as the JSON report gives it.
type judged struct {
	code         int
	findings     []string // "severity kind tenant field", sorted
	undetermined []undeterminedItem
	hasField     bool // the JSON carried an `undetermined` key
	stdout       string
	stderr       string
}

// runJudged writes files under <tmp>/conf.d and runs da-guard there with
// --format json and args; scope, when non-empty, is a path under conf.d.
func runJudged(t *testing.T, files map[string]string, scope string, args ...string) judged {
	t.Helper()
	tmp := t.TempDir()
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	root := filepath.Join(tmp, "conf.d")
	argv := append([]string{"--config-dir", root, "--format", "json"}, args...)
	if scope != "" {
		argv = append(argv, "--scope", filepath.Join(root, scope))
	}
	code, stdout, stderr := runOnce(t, argv...)
	j := judged{code: code, stdout: stdout, stderr: stderr, findings: []string{}}
	if code == exitCallerErr && stdout == "" {
		return j
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("exit %d, report is not JSON: %v\nstdout=%s\nstderr=%s", code, err, stdout, stderr)
	}
	if raw, ok := doc["undetermined"]; ok {
		j.hasField = true
		if err := json.Unmarshal(raw, &j.undetermined); err != nil {
			t.Fatalf("undetermined: %v in %s", err, raw)
		}
	}
	var rep *struct {
		Findings []jsonFinding `json:"findings"`
	}
	if raw, ok := doc["report"]; ok {
		if err := json.Unmarshal(raw, &rep); err != nil {
			t.Fatalf("report: %v", err)
		}
	}
	if rep != nil {
		for _, f := range rep.Findings {
			j.findings = append(j.findings, strings.Join([]string{f.Severity, f.Kind, f.TenantID, f.Field}, " "))
		}
	}
	sort.Strings(j.findings)
	return j
}

// routingPlaneKinds are the registered kinds of findingkind.PlaneRouting.
func routingPlaneKinds() map[string]bool {
	out := map[string]bool{}
	for _, e := range findingkind.All() {
		if e.Kind.Plane() == findingkind.PlaneRouting {
			out[e.Kind.String()] = true
		}
	}
	return out
}

// findingKind is the kind of a "severity kind tenant field" line.
func findingKind(line string) string {
	parts := strings.SplitN(line, " ", 3)
	if len(parts) < 2 {
		return ""
	}
	return parts[1]
}

// assertRoutingNotJudged holds one run to the #2904 contract, given what the
// pre-#2904 CLI expected of the same tree (oldCode, oldWant as "severity kind
// tenant field"):
//   - no finding of a routing-plane kind;
//   - every other expected finding, and nothing else;
//   - a tree on which the old CLI reported a routing finding is undetermined
//     (the routing did not silently vanish);
//   - exit 3 > 1 > 4 > 0: 3 stays 3; 1 when a remaining finding is an error
//     (or a warning under warnAsError); else 4 exactly when undetermined.
func assertRoutingNotJudged(t *testing.T, oldCode int, oldWant []string, warnAsError bool, got judged) {
	t.Helper()
	routing := routingPlaneKinds()
	var keep []string
	hadRouting := false
	for _, w := range oldWant {
		if routing[findingKind(w)] {
			hadRouting = true
			continue
		}
		keep = append(keep, w)
	}
	if keep == nil {
		keep = []string{}
	}
	sort.Strings(keep)
	for _, f := range got.findings {
		if routing[findingKind(f)] {
			t.Errorf("routing-plane finding %q: the CLI judges no routing (#2904)", f)
		}
	}
	if !reflect.DeepEqual(got.findings, keep) {
		t.Errorf("findings:\n got %q\nwant %q (the pre-#2904 expectation less its routing findings)", got.findings, keep)
	}
	if hadRouting && len(got.undetermined) == 0 {
		t.Errorf("the old CLI reported routing here, but the run is not undetermined (exit %d): routing vanished silently", got.code)
	}
	want := exitOK
	switch {
	case oldCode == exitParseFailed:
		want = exitParseFailed
	case hasError(keep, warnAsError):
		want = exitFindings
	case len(got.undetermined) > 0:
		want = exitUndetermined
	}
	if got.code != want {
		t.Errorf("exit = %d, want %d (undetermined %v, findings %v)\nstderr=%s", got.code, want, got.undetermined, got.findings, got.stderr)
	}
}

func hasError(lines []string, warnAsError bool) bool {
	for _, l := range lines {
		if strings.HasPrefix(l, "error ") || (warnAsError && strings.HasPrefix(l, "warn ")) {
			return true
		}
	}
	return false
}

func reasonsOf(items []undeterminedItem) []string {
	out := []string{}
	for _, it := range items {
		out = append(out, it.File+" "+it.Field+" "+it.Reason)
	}
	return out
}

const (
	udDefaults = "defaults:\n  mysql_connections: 80\n"
	udTenant   = "tenants:\n  tx:\n    mysql_connections: \"50\"\n"
	udRouting  = "    _routing:\n      receiver: {type: webhook, url: \"https://t.example/h\"}\n"
	// udThresholdError: a root carrier without `defaults:` (an error finding).
	udThresholdError = "mysql_connections: 80\n"
	// udDropped: a tenant file the exporter's decode refuses (exit 3).
	udDropped = "tenants:\n  ty:\n    mysql_connections: \"60\"\n    mysql_connections: \"61\"\n"
)

// The exit matrix: routing content × an error finding × a dropped file.
func TestUndetermined_ExitPrecedence(t *testing.T) {
	t.Parallel()
	for _, routing := range []bool{false, true} {
		for _, errFinding := range []bool{false, true} {
			for _, dropped := range []bool{false, true} {
				files := map[string]string{"_defaults.yaml": udDefaults, "tx.yaml": udTenant}
				if routing {
					files["tx.yaml"] = udTenant + udRouting
				}
				if errFinding {
					files["_defaults.yaml"] = udThresholdError
				}
				if dropped {
					files["ty.yaml"] = udDropped
				}
				want := exitOK
				switch {
				case dropped:
					want = exitParseFailed
				case errFinding:
					want = exitFindings
				case routing:
					want = exitUndetermined
				}
				got := runJudged(t, files, "")
				if got.code != want {
					t.Errorf("routing=%v error=%v dropped=%v: exit %d, want %d\nstderr=%s",
						routing, errFinding, dropped, got.code, want, got.stderr)
				}
				// The evidence is listed whatever wins (only routing here; the
				// dropped file is a duplicate key yaml.v3 refuses too, which
				// parse-tree names as well).
				hasRouting := false
				for _, it := range got.undetermined {
					if it.Reason == undeterminedRoutingKey {
						hasRouting = true
					}
				}
				if hasRouting != routing {
					t.Errorf("routing=%v error=%v dropped=%v: undetermined %v", routing, errFinding, dropped, got.undetermined)
				}
				if strings.Contains(got.stderr, "routing and domain policy not judged") != (len(got.undetermined) > 0) {
					t.Errorf("routing=%v error=%v dropped=%v: stderr line ⇔ evidence; stderr=%s", routing, errFinding, dropped, got.stderr)
				}
			}
		}
	}
}

// --warn-as-error turns a warning into 1, which wins over 4; without it the
// warning leaves 4.
func TestUndetermined_WarnAsErrorAffectsOnlyOne(t *testing.T) {
	t.Parallel()
	files := map[string]string{"_defaults.yaml": udDefaults, "tx.yaml": udTenant + udRouting}
	// A predicted count within a cap of 3 but above a warn floor of 0.
	warn := []string{"--cardinality-limit", "3", "--cardinality-warn-ratio", "0.2"}
	got := runJudged(t, files, "", warn...)
	if got.code != exitUndetermined || !hasWarn(got.findings) {
		t.Fatalf("exit %d, findings %v; want 4 beside a warning", got.code, got.findings)
	}
	if got := runJudged(t, files, "", append(warn, "--warn-as-error")...); got.code != exitFindings {
		t.Errorf("--warn-as-error: exit %d, want %d", got.code, exitFindings)
	}
}

func hasWarn(lines []string) bool {
	for _, l := range lines {
		if strings.HasPrefix(l, "warn ") {
			return true
		}
	}
	return false
}

// #2991: no tenant in scope and no finding is 0 only when the tree holds no
// routing; with routing anywhere in the tree it is 4, the evidence listed and
// the report not called safe.
func TestUndetermined_ZeroTenants(t *testing.T) {
	t.Parallel()
	base := map[string]string{"_defaults.yaml": udDefaults, "empty/": ""}
	if got := runJudged(t, base, "empty"); got.code != exitOK || len(got.undetermined) != 0 || !got.hasField {
		t.Errorf("no routing: exit %d, undetermined %v (field present %v); want 0 and []", got.code, got.undetermined, got.hasField)
	}
	withRouting := map[string]string{"_defaults.yaml": udDefaults + "_routing_defaults:\n  receiver: {type: webhook, url: \"https://t.example/h\"}\n", "empty/": ""}
	got := runJudged(t, withRouting, "empty")
	if got.code != exitUndetermined || !reflect.DeepEqual(reasonsOf(got.undetermined), []string{"_defaults.yaml _routing_defaults routing_key"}) {
		t.Errorf("routing outside the scope: exit %d, undetermined %v; want 4 naming _defaults.yaml _routing_defaults", got.code, got.undetermined)
	}
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{"conf.d/_defaults.yaml": withRouting["_defaults.yaml"]})
	code, stdout, _ := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitUndetermined || strings.Contains(stdout, "vacuously safe") || !strings.Contains(stdout, "NOT a safe result (exit 4)") ||
		!strings.Contains(stdout, "generate-routes --validate --strict") {
		t.Errorf("md: exit %d, report %q; want 4, no all-clear, the remedy", code, stdout)
	}
}

// Each reason, alone in a tree, makes the run undetermined; its control
// (the same tree without it) is 0.
func TestUndetermined_EveryReason(t *testing.T) {
	t.Parallel()
	recv := "{type: webhook, url: \"https://t.example/h\"}"
	base := func(over map[string]string) map[string]string {
		files := map[string]string{"_defaults.yaml": udDefaults, "tx.yaml": udTenant}
		for k, v := range over {
			files[k] = v
		}
		return files
	}
	cases := []struct {
		name  string
		files map[string]string
		want  []string // "file field reason"
	}{
		{"routing key in a tenant", base(map[string]string{"tx.yaml": udTenant + udRouting}),
			[]string{"tx.yaml tenants.tx._routing routing_key"}},
		{"misspelled routing key", base(map[string]string{"tx.yaml": udTenant + "    _routing_profle: p\n"}),
			[]string{"tx.yaml tenants.tx._routing_profle routing_key"}},
		{"_severity_dedup", base(map[string]string{"tx.yaml": udTenant + "    _severity_dedup: disable\n"}),
			[]string{"tx.yaml tenants.tx._severity_dedup routing_key"}},
		{"routing in a threshold profile", base(map[string]string{"_profiles.yaml": "profiles:\n  p1:\n    _routing:\n      receiver: " + recv + "\n"}),
			[]string{"_profiles.yaml profiles.p1._routing routing_key"}},
		{"routing in a sequence", base(map[string]string{"_noc.yaml": "notes:\n- a: 1\n- _routing_enforced: {enabled: true}\n"}),
			[]string{"_noc.yaml notes[1]._routing_enforced routing_key"}},
		{"routing_profiles key", base(map[string]string{"_extra.yaml": "routing_profiles:\n  p1: {}\n"}),
			[]string{"_extra.yaml routing_profiles routing_key"}},
		{"routing defaults below the root", base(map[string]string{"team/_defaults.yaml": "_routing_defaults:\n  receiver: " + recv + "\n"}),
			[]string{"team/_defaults.yaml _routing_defaults routing_key"}},
		{"domain policy file", base(map[string]string{"_domain_policy.yaml": "domain_policies: {}\n"}),
			[]string{"_domain_policy.yaml  domain_policy"}},
		{"nested domain policy file, .yml", base(map[string]string{"team/_domain_policy.yml": "domain_policies: {}\n"}),
			[]string{"team/_domain_policy.yml  domain_policy"}},
		{"routing profiles file", base(map[string]string{"team/_routing_profiles.yaml": "routing_profiles: {}\n"}),
			[]string{"team/_routing_profiles.yaml  routing_profiles_file"}},
		// A nested `_` file the exporter does not read (a syntax error there
		// would be its exit 3), with a shape parse-tree refuses.
		{"a file the generator cannot read", base(map[string]string{"team/_notes.yaml": "note: ! 1\n"}),
			[]string{"team/_notes.yaml non_specific_tag generator_unreadable"}},
		{"an invalid tenant id", base(map[string]string{"tx.yaml": "tenants:\n  Tx_1:\n    mysql_connections: \"50\"\n"}),
			[]string{"tx.yaml tenants.Tx_1 invalid_tenant_id"}},
	}
	if got := runJudged(t, base(nil), ""); got.code != exitOK || len(got.undetermined) != 0 {
		t.Fatalf("control: exit %d, undetermined %v; want 0 and none", got.code, got.undetermined)
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := runJudged(t, tc.files, "")
			if !reflect.DeepEqual(reasonsOf(got.undetermined), tc.want) {
				t.Errorf("undetermined %q, want %q", reasonsOf(got.undetermined), tc.want)
			}
			if got.code != exitUndetermined {
				t.Errorf("exit %d, want %d; findings %v\nstderr=%s", got.code, exitUndetermined, got.findings, got.stderr)
			}
		})
	}
}

// --scope does not narrow the evidence: the generator judges the whole tree.
func TestUndetermined_ScopeSeesTheWholeTree(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": udDefaults,
		"a/tx.yaml":      udTenant,
		"b/ty.yaml":      "tenants:\n  ty:\n    mysql_connections: \"50\"\n" + udRouting,
	}
	got := runJudged(t, files, "a")
	if got.code != exitUndetermined || !reflect.DeepEqual(reasonsOf(got.undetermined), []string{"b/ty.yaml tenants.ty._routing routing_key"}) {
		t.Errorf("exit %d, undetermined %v; want 4 naming b/ty.yaml", got.code, got.undetermined)
	}
}

// --required-fields under a routing key is a caller error, never a check
// that silently passes.
func TestUndetermined_RequiredRoutingFieldIsACallerError(t *testing.T) {
	t.Parallel()
	files := map[string]string{"_defaults.yaml": udDefaults, "tx.yaml": udTenant}
	for _, field := range []string{"_routing", "_routing.receiver", "_routing.receiver.type", "_routing_defaults.receiver",
		"_severity_dedup", "mysql_connections,_routing.receiver.type"} {
		got := runJudged(t, files, "", "--required-fields", field)
		if got.code != exitCallerErr || !strings.Contains(got.stderr, "routing is no longer judged by da-guard") {
			t.Errorf("--required-fields %s: exit %d, stderr %q; want 2 naming why", field, got.code, got.stderr)
		}
	}
	if got := runJudged(t, files, "", "--required-fields", "mysql_connections"); got.code != exitOK {
		t.Errorf("non-routing fields: exit %d, want 0\nstderr=%s", got.code, got.stderr)
	}
}

// Every report the default check writes carries `undetermined` — the report,
// the empty-scope one and the decode-stop one — [] when there is none.
func TestUndetermined_EveryJSONReportCarriesTheField(t *testing.T) {
	t.Parallel()
	routed := udTenant + udRouting
	cases := []struct {
		name      string
		files     map[string]string
		scope     string
		wantCode  int
		wantItems int
	}{
		{"report, none", map[string]string{"_defaults.yaml": udDefaults, "tx.yaml": udTenant}, "", exitOK, 0},
		{"report, some", map[string]string{"_defaults.yaml": udDefaults, "tx.yaml": routed}, "", exitUndetermined, 1},
		{"empty scope, none", map[string]string{"_defaults.yaml": udDefaults, "e/": ""}, "e", exitOK, 0},
		{"empty scope, some", map[string]string{"_defaults.yaml": udDefaults, "tx.yaml": routed, "e/": ""}, "e", exitUndetermined, 1},
		// A chain carrier that fails the decode stops the run before any
		// tenant is checked (writeDecodeStopReport); the evidence is still
		// listed, the exit stays 3.
		{"decode stop, some", map[string]string{"_defaults.yaml": udDefaults, "team/_defaults.yaml": "defaults: [1\n",
			"team/tx.yaml": routed}, "team", exitParseFailed, -1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := runJudged(t, tc.files, tc.scope)
			if got.code != tc.wantCode || !got.hasField {
				t.Fatalf("exit %d (want %d), undetermined present %v\nstdout=%s\nstderr=%s", got.code, tc.wantCode, got.hasField, got.stdout, got.stderr)
			}
			if !strings.Contains(got.stdout, `"undetermined": [`) {
				t.Errorf("undetermined is not a JSON array: %s", got.stdout)
			}
			if tc.wantItems >= 0 && len(got.undetermined) != tc.wantItems {
				t.Errorf("undetermined %v, want %d item(s)", got.undetermined, tc.wantItems)
			}
			if tc.wantItems < 0 {
				found := false
				for _, it := range got.undetermined {
					found = found || (it.File == "team/tx.yaml" && it.Reason == undeterminedRoutingKey)
				}
				if !found {
					t.Errorf("decode stop: undetermined %v, want team/tx.yaml's routing listed", got.undetermined)
				}
			}
		})
	}
}

// The Markdown report names the evidence and the remedy, and never the
// all-clear line.
func TestUndetermined_Markdown(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": udDefaults,
		"conf.d/tx.yaml":        udTenant + udRouting,
	})
	out := filepath.Join(tmp, "report.md")
	code, _, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"), "--output", out)
	body, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if code != exitUndetermined {
		t.Errorf("exit %d, want %d", code, exitUndetermined)
	}
	for _, want := range []string{"### Routing and domain policy: not judged", "`tx.yaml` `tenants.tx._routing` (routing_key)",
		"run `da-tools generate-routes --validate --strict` — da-guard no longer judges routing or domain policy (ADR-036 §9)",
		noFindingsUndeterminedLine} {
		if !bytes.Contains(body, []byte(want)) {
			t.Errorf("report lacks %q:\n%s", want, body)
		}
	}
	if bytes.Contains(body, []byte("safe to merge")) {
		t.Errorf("report calls an undetermined run safe:\n%s", body)
	}
	for _, want := range []string{"undetermined=1", "routing and domain policy not judged: 1 item(s)", "(exit 4)"} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr lacks %q: %s", want, stderr)
		}
	}
}

// The Markdown list is capped; the JSON lists every item.
func TestUndetermined_MarkdownListIsCapped(t *testing.T) {
	t.Parallel()
	var items []undeterminedItem
	for i := 0; i < undeterminedMarkdownLimit+3; i++ {
		items = append(items, undeterminedItem{File: "t.yaml", Field: "tenants.t" + string(rune('a'+i)) + "._routing", Reason: undeterminedRoutingKey})
	}
	md := undeterminedMarkdown(items)
	if n := strings.Count(md, "(routing_key)"); n != undeterminedMarkdownLimit || !strings.Contains(md, "… and 3 more") {
		t.Errorf("%d items listed, md=%s; want %d and the rest counted", n, md, undeterminedMarkdownLimit)
	}
}

// Every tree of the routing-policy parity matrix, run through the CLI: no
// routing-plane finding, and every tree on which the Go routing checks
// (routingGuardFindings) report anything is undetermined.
func TestUndetermined_ParityMatrixTreesAreNotJudged(t *testing.T) {
	t.Parallel()
	routing := routingPlaneKinds()
	for _, tree := range loadRoutingPolicyMatrix(t) {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			goFindings, _, stop := routingGuardFindings(t, writeParityTree(t, tree.Files))
			got := runJudged(t, tree.Files, "")
			for _, f := range got.findings {
				if routing[findingKind(f)] {
					t.Errorf("routing-plane finding %q", f)
				}
			}
			goRouting := stop != nil
			for _, f := range goFindings {
				goRouting = goRouting || routing[f.Kind]
			}
			if goRouting && got.code != exitCallerErr && len(got.undetermined) == 0 {
				t.Errorf("the Go routing checks report on this tree (%v, %v) but the CLI run is not undetermined (exit %d)",
					goFindings, stop, got.code)
			}
		})
	}
}

// A tenant id two tenant files declare: the exporter refuses the tree on a
// whole-tree run (exit 2); a --scope run that sees neither file is
// undetermined, never the vacuous 0 (the generator refuses the tree).
func TestUndetermined_DuplicateTenantOutsideTheScope(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": udDefaults,
		"a/tx.yaml":      udTenant,
		"b/tx.yaml":      udTenant,
		"empty/":         "",
	}
	if got := runJudged(t, files, ""); got.code != exitCallerErr {
		t.Errorf("whole tree: exit %d, want %d (the exporter refuses it)", got.code, exitCallerErr)
	}
	got := runJudged(t, files, "empty")
	if got.code != exitUndetermined || !reflect.DeepEqual(reasonsOf(got.undetermined), []string{"b/tx.yaml tenants.tx duplicate_tenant"}) {
		t.Errorf("scoped: exit %d, undetermined %v; want 4 naming b/tx.yaml", got.code, got.undetermined)
	}
}

// The evidence is sorted (file, field, reason), the same list on every run:
// a tree whose items come from every source (unreadable files, the file loop
// over a map, nested maps, the duplicate pass after it), with a file and a
// field that tie on the earlier keys, so no insertion order passes for it.
func TestUndetermined_EvidenceOrderIsPinned(t *testing.T) {
	t.Parallel()
	tenants := "tenants:\n  Tx_1:\n    mysql_connections: \"50\"\n  tx:\n    mysql_connections: \"50\"\n" +
		"    _routing: {receiver: {type: webhook, url: \"https://t.example/h\"}}\n    _severity_dedup: disable\n"
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"_defaults.yaml":              udDefaults,
		"_domain_policy.yaml":         "domain_policies: {}\n",
		"a/tx.yaml":                   tenants,
		"b/tx.yaml":                   tenants,
		"team/_notes.yaml":            "note: ! 1\n",
		"team/_routing_profiles.yaml": "routing_profiles: {}\n",
	})
	want := []string{
		"_domain_policy.yaml  domain_policy",
		"a/tx.yaml tenants.Tx_1 invalid_tenant_id",
		"a/tx.yaml tenants.tx._routing routing_key",
		"a/tx.yaml tenants.tx._severity_dedup routing_key",
		"b/tx.yaml tenants.Tx_1 duplicate_tenant",
		"b/tx.yaml tenants.Tx_1 invalid_tenant_id",
		"b/tx.yaml tenants.tx duplicate_tenant",
		"b/tx.yaml tenants.tx._routing routing_key",
		"b/tx.yaml tenants.tx._severity_dedup routing_key",
		"team/_notes.yaml non_specific_tag generator_unreadable",
		"team/_routing_profiles.yaml  routing_profiles_file",
	}
	for run := 0; run < 20; run++ {
		var errOut bytes.Buffer
		got := reasonsOf(undeterminedEvidence(tmp, false, &errOut))
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("run %d: evidence\n%q\nwant\n%q", run, got, want)
		}
		if errOut.Len() != 0 {
			t.Fatalf("run %d: stderr %q, want none", run, errOut.String())
		}
	}
}

// A tree the walk cannot scan at all is one generator_unreadable item for
// ".", never an empty list (fail toward undetermined), the reason on stderr.
func TestUndetermined_UnscannableTreeIsUndetermined(t *testing.T) {
	t.Parallel()
	root := filepath.Join(t.TempDir(), "missing")
	var errOut bytes.Buffer
	got := undeterminedEvidence(root, false, &errOut)
	want := []undeterminedItem{{File: ".", Field: "read_failure", Reason: undeterminedGeneratorUnreadable}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("evidence %+v, want %+v", got, want)
	}
	if !strings.Contains(errOut.String(), "cannot read the tree as the route generator does") {
		t.Errorf("stderr %q, want the reason", errOut.String())
	}
	// The root the exporter's walk already reported: same evidence, the
	// reason not printed a second time (#2627: named once).
	var again bytes.Buffer
	if got := undeterminedEvidence(root, true, &again); !reflect.DeepEqual(got, want) {
		t.Errorf("rootReported: evidence %+v, want %+v", got, want)
	}
	if again.Len() != 0 {
		t.Errorf("rootReported: stderr %q, want none", again.String())
	}
}
