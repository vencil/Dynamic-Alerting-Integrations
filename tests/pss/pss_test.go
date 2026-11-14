// Package pss holds every pod this repository ships to the Pod Security level
// it declares, using the upstream Pod Security Admission library as the
// judge (#1018).
//
// Why the library: the API server's PodSecurity admission plugin is built
// from k8s.io/pod-security-admission. Evaluating rendered manifests with the
// same code, at the minor version pinned by scripts/ops/kind-node-image.env,
// means there is no hand-written copy of the restricted profile to drift
// from upstream.
//
// What is held:
//   - every chart under helm/, rendered with its defaults (when they render)
//     and with each file in its ci/ directory, at the level the chart declares
//     in Chart.yaml (annotation levelAnnotation; absent = restricted);
//   - every manifest under k8s/, at restricted.
//
// Honest scope:
//   - A conditional in a workload template is exercised only if some value set
//     turns it on (TestWorkloadConditionalsAreExercised checks truthiness of
//     each .Values path an if/with/range reads). An `eq .Values.mode "x"`
//     branch other than the one the values select is NOT covered by that check.
//   - Pods created at run time (kubectl run in runbooks, scripts) are not here.
//   - Mutating webhooks in a customer's cluster run before admission and are
//     not modelled.
package pss

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/runtime"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/pod-security-admission/admission"
	"k8s.io/pod-security-admission/api"
	"k8s.io/pod-security-admission/policy"
	"sigs.k8s.io/yaml"
)

// levelAnnotation is where a chart declares the Pod Security level its pods
// need. It is the single place an exception is written; the namespace
// manifests and docs follow it.
const levelAnnotation = "dynamic-alerting.io/pod-security-level"

func repoRoot(t *testing.T) string {
	t.Helper()
	if r := os.Getenv("PSS_REPO_ROOT"); r != "" {
		return r
	}
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Clean(filepath.Join(wd, "..", ".."))
}

var nodeImageRE = regexp.MustCompile(`(?m)^KIND_NODE_IMAGE=kindest/node:v(\d+)\.(\d+)\.\d+@sha256:[0-9a-f]{64}$`)

// clusterVersion is the minor the PSS profile is evaluated at: the Kubernetes
// version of the kind node image, the repo's single source for it.
func clusterVersion(t *testing.T) (int, int) {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(repoRoot(t), "scripts", "ops", "kind-node-image.env"))
	if err != nil {
		t.Fatal(err)
	}
	m := nodeImageRE.FindSubmatch(b)
	if m == nil {
		t.Fatal("scripts/ops/kind-node-image.env: no KIND_NODE_IMAGE=kindest/node:vX.Y.Z@sha256:<digest> line")
	}
	major, _ := strconv.Atoi(string(m[1]))
	minor, _ := strconv.Atoi(string(m[2]))
	return major, minor
}

var psaRequireRE = regexp.MustCompile(`(?m)^\s*k8s\.io/pod-security-admission\s+v0\.(\d+)\.\d+`)

// TestLibraryTracksTheNodeImage: the library judges at the version it was
// built for; an older library would silently skip checks a newer cluster
// enforces. k8s.io/* v0.N.x ships with Kubernetes 1.N. go.mod is what the
// test binary is built from, so it is the version that judged.
func TestLibraryTracksTheNodeImage(t *testing.T) {
	major, minor := clusterVersion(t)
	b, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatal(err)
	}
	m := psaRequireRE.FindSubmatch(b)
	if m == nil {
		t.Fatal("go.mod does not require k8s.io/pod-security-admission v0.N.x")
	}
	if got, _ := strconv.Atoi(string(m[1])); major != 1 || got != minor {
		t.Fatalf("go.mod pins k8s.io/pod-security-admission v0.%d.x; the kind node image is v%d.%d (scripts/ops/kind-node-image.env), so pin v0.%d.x",
			got, major, minor, minor)
	}
}

// podKinds are the kinds that carry a pod spec. One of them failing to decode
// or extract is a test failure, not a skip.
var podKinds = map[string]bool{
	"Pod": true, "PodTemplate": true, "ReplicationController": true, "ReplicaSet": true,
	"Deployment": true, "DaemonSet": true, "StatefulSet": true, "Job": true, "CronJob": true,
}

type verdict struct {
	evaluated  int
	violations []string
}

func evaluate(t *testing.T, stream []byte, lv api.LevelVersion) verdict {
	t.Helper()
	eval, err := policy.NewEvaluator(policy.DefaultChecks(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var v verdict
	dec := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(stream), 4096)
	for {
		var raw runtime.RawExtension
		if err := dec.Decode(&raw); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			t.Fatalf("decode: %v", err)
		}
		if len(bytes.TrimSpace(raw.Raw)) == 0 || string(bytes.TrimSpace(raw.Raw)) == "null" {
			continue
		}
		var tm struct {
			APIVersion string            `json:"apiVersion"`
			Kind       string            `json:"kind"`
			Items      []json.RawMessage `json:"items"`
		}
		if err := yaml.Unmarshal(raw.Raw, &tm); err != nil {
			t.Fatalf("decode: %v", err)
		}
		if tm.Kind == "List" || strings.HasSuffix(tm.Kind, "List") && len(tm.Items) > 0 {
			// A List hides its items from a per-document decoder: judge each.
			for _, item := range tm.Items {
				sub := evaluate(t, item, lv)
				v.evaluated += sub.evaluated
				v.violations = append(v.violations, sub.violations...)
			}
			continue
		}
		obj, gvk, err := scheme.Codecs.UniversalDeserializer().Decode(raw.Raw, nil, nil)
		if err != nil {
			// Not a built-in type (CRDs, ServiceMonitor) carries no pod spec
			// PSA reads. A pod-bearing KIND that does not decode (an old or
			// misspelled apiVersion) must not be skipped as if it were one.
			if podKinds[tm.Kind] {
				t.Errorf("%s %q: cannot decode (%v); it carries a pod spec and was not judged", tm.Kind, tm.APIVersion, err)
			}
			continue
		}
		podMeta, spec, err := admission.DefaultPodSpecExtractor{}.ExtractPodSpec(obj)
		if err != nil {
			if podKinds[gvk.Kind] {
				t.Errorf("%s: pod spec not extracted: %v", gvk.Kind, err)
			}
			continue
		}
		if spec == nil {
			continue
		}
		name := ""
		if o, ok := obj.(interface{ GetName() string }); ok {
			name = o.GetName()
		}
		v.evaluated++
		for _, r := range eval.EvaluatePod(lv, podMeta, spec) {
			if !r.Allowed {
				v.violations = append(v.violations,
					fmt.Sprintf("%s/%s: %s: %s", gvk.Kind, name, r.ForbiddenReason, r.ForbiddenDetail))
			}
		}
	}
	return v
}

type chart struct {
	name, dir string
	level     api.Level
	valueSets []string // "" = chart defaults
}

func charts(t *testing.T) []chart {
	t.Helper()
	root := repoRoot(t)
	paths, err := filepath.Glob(filepath.Join(root, "helm", "*", "Chart.yaml"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no charts under helm/ (%v)", err)
	}
	var out []chart
	for _, p := range paths {
		b, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		var meta struct {
			Annotations map[string]string `json:"annotations"`
		}
		if err := yaml.Unmarshal(b, &meta); err != nil {
			t.Fatalf("%s: %v", p, err)
		}
		level := api.LevelRestricted
		if s, ok := meta.Annotations[levelAnnotation]; ok {
			if level, err = api.ParseLevel(s); err != nil {
				t.Fatalf("%s: %s: %v", p, levelAnnotation, err)
			}
		}
		dir := filepath.Dir(p)
		ci, _ := filepath.Glob(filepath.Join(dir, "ci", "*.yaml"))
		sort.Strings(ci)
		out = append(out, chart{name: filepath.Base(dir), dir: dir, level: level, valueSets: append([]string{""}, ci...)})
	}
	return out
}

func helmBin(t *testing.T) string {
	t.Helper()
	h, err := exec.LookPath("helm")
	if err != nil {
		if os.Getenv("PSS_REQUIRE_HELM") == "1" {
			t.Fatal("helm is required (PSS_REQUIRE_HELM=1) but not on PATH")
		}
		t.Skip("helm not on PATH: charts were not checked")
	}
	return h
}

// readTree opens every file under dir. helm is a child process, so without
// this Go's test cache would not see an edited chart as an input and could
// report a cached pass. (go_test_reads.py classes these opens as "walked" and
// does not check them; the leg's `go` filter carries helm/** and k8s/**.)
func readTree(t *testing.T, dir string) {
	t.Helper()
	err := filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			_, err = os.ReadFile(p)
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

func render(t *testing.T, helm string, c chart, values string) ([]byte, error) {
	args := []string{"template", "pss-gate", c.dir, "--namespace", "pss-gate"}
	if values != "" {
		args = append(args, "-f", values)
	}
	var stdout, stderr bytes.Buffer
	cmd := exec.Command(helm, args...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("%v: %s", err, strings.TrimSpace(stderr.String()))
	}
	return stdout.Bytes(), nil
}

// defaultsDoNotRender names the charts whose defaults are refused on purpose,
// and the words that refusal must contain. Any other render failure is a
// test failure: a chart whose defaults stop rendering for another reason
// would otherwise drop out of the gate with a log line.
var defaultsDoNotRender = map[string]string{
	// writeBack.mode has no default by design (#2855): the operator must choose.
	"tenant-api": "writeBack.mode must be",
	// auth.mode defaults to enforce (#1295): every consumer must name its Secret.
	"victorialogs": "needs a Secret for every consumer in auth.existingSecrets",
}

// needsPrivilegedPrefix marks a ci/ value set as a DECLARED exception: a
// configuration the chart offers that cannot run under restricted. It is
// judged at privileged, and must really violate restricted — otherwise the
// exception has gone stale and the file must go.
const needsPrivilegedPrefix = "pss-needs-privileged-"

func TestChartsMeetTheirDeclaredLevel(t *testing.T) {
	helm := helmBin(t)
	_, minor := clusterVersion(t)
	for _, c := range charts(t) {
		c := c
		t.Run(c.name, func(t *testing.T) {
			readTree(t, c.dir)
			lv := api.LevelVersion{Level: c.level, Version: api.MajorMinorVersion(1, minor)}
			total, rendered := 0, 0
			for _, vs := range c.valueSets {
				label := "defaults"
				if vs != "" {
					label = filepath.Base(vs)
				}
				out, err := render(t, helm, c, vs)
				if err != nil {
					if want, ok := defaultsDoNotRender[c.name]; ok && vs == "" && strings.Contains(err.Error(), want) {
						t.Logf("%s: defaults are refused on purpose (%s); judged through ci/ values", c.name, want)
						continue
					}
					t.Fatalf("%s (%s): helm template failed: %v", c.name, label, err)
				}
				rendered++
				setLV := lv
				if strings.HasPrefix(label, needsPrivilegedPrefix) {
					strict := evaluate(t, out, api.LevelVersion{Level: api.LevelRestricted, Version: lv.Version})
					if len(strict.violations) == 0 {
						t.Errorf("%s (%s) is declared as needing privileged but passes restricted: the exception is stale, drop the prefix", c.name, label)
					}
					setLV = api.LevelVersion{Level: api.LevelPrivileged, Version: lv.Version}
				}
				v := evaluate(t, out, setLV)
				total += v.evaluated
				for _, msg := range v.violations {
					t.Errorf("%s (%s) at %s: %s", c.name, label, setLV.String(), msg)
				}
			}
			if want, ok := defaultsDoNotRender[c.name]; ok {
				if _, err := render(t, helm, c, ""); err == nil {
					t.Errorf("%s: listed in defaultsDoNotRender (%q) but its defaults render: drop the entry so they are judged", c.name, want)
				}
			}
			if rendered == 0 || total == 0 {
				t.Fatalf("%s: %d value sets rendered, %d pod specs evaluated; nothing was checked", c.name, rendered, total)
			}
		})
	}
}

func TestRawManifestsAreRestricted(t *testing.T) {
	root := repoRoot(t)
	_, minor := clusterVersion(t)
	lv := api.LevelVersion{Level: api.LevelRestricted, Version: api.MajorMinorVersion(1, minor)}
	// k8s/ is the lab's manifests; docs/internal/examples/ holds manifests the
	// runbooks tell an operator to apply into the same namespaces.
	var stream bytes.Buffer
	for _, dir := range []string{"k8s", filepath.Join("docs", "internal", "examples")} {
		err := filepath.WalkDir(filepath.Join(root, dir), func(p string, d os.DirEntry, err error) error {
			if err != nil || d.IsDir() || (!strings.HasSuffix(p, ".yaml") && !strings.HasSuffix(p, ".yml")) {
				return err
			}
			b, err := os.ReadFile(p)
			stream.Write(b)
			stream.WriteString("\n---\n")
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	v := evaluate(t, stream.Bytes(), lv)
	if v.evaluated == 0 {
		t.Fatal("no pod spec found under k8s/ or docs/internal/examples/: nothing was checked")
	}
	for _, msg := range v.violations {
		t.Errorf("k8s/ or docs/internal/examples/ at %s: %s", lv.String(), msg)
	}
	t.Logf("k8s/ + docs/internal/examples/: %d pod specs evaluated", v.evaluated)
}

var (
	workloadKindRE = regexp.MustCompile(`(?m)^kind:\s*(Deployment|DaemonSet|StatefulSet|ReplicaSet|CronJob|Job|Pod)\b`)
	// A branching action, or a variable assignment (`$x := not .Values.y`
	// feeds a later `if $x`).
	actionRE   = regexp.MustCompile(`\{\{-?\s*(?:(?:else\s+)?(?:if|with|range)\b|\$[A-Za-z0-9_]+\s*:?=)([^}]*)\}\}`)
	failNextRE = regexp.MustCompile(`^\s*\{\{-?\s*(?:fail|required)\b`)
	// Only `fail` ends a render unconditionally; `required` returns its value
	// when set and the template carries on rendering after it.
	failOnlyNextRE = regexp.MustCompile(`^\s*\{\{-?\s*fail\b`)
	negatedPathRE  = regexp.MustCompile(`\bnot\s+\(?\s*\$?\.Values\.([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)`)
	valuesPathRE   = regexp.MustCompile(`\$?\.Values\.([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)`)
)

func truthy(v interface{}) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case float64:
		return x != 0
	case int64:
		return x != 0
	case map[string]interface{}:
		return len(x) > 0
	case []interface{}:
		return len(x) > 0
	}
	return true
}

func lookup(m map[string]interface{}, path string) interface{} {
	var cur interface{} = m
	for _, k := range strings.Split(path, ".") {
		mm, ok := cur.(map[string]interface{})
		if !ok {
			return nil
		}
		cur = mm[k]
	}
	return cur
}

func merge(dst, src map[string]interface{}) map[string]interface{} {
	out := map[string]interface{}{}
	for k, v := range dst {
		out[k] = v
	}
	for k, v := range src {
		if sm, ok := v.(map[string]interface{}); ok {
			if dm, ok := out[k].(map[string]interface{}); ok {
				out[k] = merge(dm, sm)
				continue
			}
		}
		out[k] = v
	}
	return out
}

func loadValues(t *testing.T, p string) map[string]interface{} {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	m := map[string]interface{}{}
	if err := yaml.Unmarshal(b, &m); err != nil {
		t.Fatalf("%s: %v", p, err)
	}
	return m
}

// TestWorkloadConditionalsAreExercised: a value that adds a container, volume
// or securityContext behind a branch is only judged when some value set takes
// that branch. Every .Values path a branching action (or a variable assignment
// feeding one) reads — in a template that renders a workload, or in a .tpl
// helper those templates include — must be truthy in the defaults or one ci/
// file; a path read under `not` must also be falsy in one of them. A branch
// whose body is a guard is judged by the arm that renders: under `not` (fail or
// required) only "on" is required; read without `not` and followed by `fail`,
// the path adds no requirement (its "on" arm never renders). `required`
// without `not` stays a shaping branch — it renders when its value is set.
//
// Limits (this reads template text): an `eq .Values.x "a"` branch is covered
// only for the values the sets choose; a path reached through an alias other
// than a `$var :=` assignment, or through `.` rebound by `with`, is not seen.
func TestWorkloadConditionalsAreExercised(t *testing.T) {
	for _, c := range charts(t) {
		defaults := loadValues(t, filepath.Join(c.dir, "values.yaml"))
		// Only value sets that are judged count: a chart whose defaults are
		// refused on purpose (defaultsDoNotRender) contributes no defaults set.
		var sets []map[string]interface{}
		if _, refused := defaultsDoNotRender[c.name]; !refused {
			sets = append(sets, defaults)
		}
		for _, vs := range c.valueSets[1:] {
			// A declared exception is judged at privileged, so the branches it
			// takes are not judged at the chart's own level: it covers nothing.
			if strings.HasPrefix(filepath.Base(vs), needsPrivilegedPrefix) {
				continue
			}
			sets = append(sets, merge(defaults, loadValues(t, vs)))
		}
		var tpls []string
		err := filepath.WalkDir(filepath.Join(c.dir, "templates"), func(p string, d os.DirEntry, err error) error {
			if err == nil && !d.IsDir() && (strings.HasSuffix(p, ".yaml") || strings.HasSuffix(p, ".tpl")) {
				tpls = append(tpls, p)
			}
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
		type need struct{ on, off bool }
		paths := map[string]*need{}
		where := map[string]string{}
		for _, tp := range tpls {
			b, err := os.ReadFile(tp)
			if err != nil {
				t.Fatal(err)
			}
			if strings.HasSuffix(tp, ".yaml") && !workloadKindRE.Match(b) {
				continue
			}
			for _, idx := range actionRE.FindAllSubmatchIndex(b, -1) {
				a := [][]byte{b[idx[0]:idx[1]], b[idx[2]:idx[3]]}
				// An action whose body is a `fail`/`required` is a guard: the
				// arm that takes the body refuses to render, it shapes no pod.
				//   `if not .Values.x` → fail: x's "off" arm fails, so only
				//     "on" is a pod shape (still required below).
				//   `if .Values.x` → fail (x read WITHOUT `not`): x's "on" arm
				//     fails and its "off" arm is every other render, already
				//     judged — so it adds no requirement at all. Requiring "on"
				//     here would demand a values set that cannot render (#1018:
				//     the removed confDir.hostPath guard). ⛔ `fail` only:
				//     `{{ if .Values.x }}{{ required "…" .Values.y | toYaml }}`
				//     RENDERS when y is set, so it stays a shaping branch.
				guard := failNextRE.Match(b[idx[1]:])
				failOnly := failOnlyNextRE.Match(b[idx[1]:])
				negated := map[string]bool{}
				for _, nm := range negatedPathRE.FindAllSubmatch(a[1], -1) {
					negated[string(nm[1])] = true
				}
				for _, pm := range valuesPathRE.FindAllSubmatch(a[1], -1) {
					k := string(pm[1])
					if failOnly && !negated[k] {
						continue
					}
					if paths[k] == nil {
						paths[k] = &need{}
						where[k] = filepath.Base(tp)
					}
					paths[k].on = true
					if negated[k] && !guard {
						paths[k].off = true
					}
				}
			}
		}
		var missing []string
		for p, n := range paths {
			on, off := false, false
			for _, s := range sets {
				if truthy(lookup(s, p)) {
					on = true
				} else {
					off = true
				}
			}
			if n.on && !on {
				missing = append(missing, fmt.Sprintf(".Values.%s (%s) is never on", p, where[p]))
			}
			if n.off && !off {
				missing = append(missing, fmt.Sprintf(".Values.%s (%s, read under `not`) is never off", p, where[p]))
			}
		}
		sort.Strings(missing)
		for _, m := range missing {
			t.Errorf("%s: %s in values.yaml or ci/*.yaml, so the pods that branch shapes are never judged", c.name, m)
		}
	}
}
