package pss

// The lab cluster's side of #1018: the namespaces enforce, and the pods this
// repository creates at run time (a script's manifests, a runbook's debug
// pod) pass the level those namespaces enforce. Same judge as pss_test.go.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"k8s.io/pod-security-admission/api"
	"sigs.k8s.io/yaml"
)

// TestLabNamespacesEnforce: every Namespace under k8s/00-namespaces enforces,
// at the level the chart of the same name declares (restricted when there is
// no such chart, or it declares nothing), pinned to the kind node image's
// minor. The chart annotation is the single place an exception is written;
// this is where the namespace is held to it. A chart that declares a level
// looser than restricted must have a namespace of its own name: otherwise it
// would be installed into a restricted namespace and rejected there, while
// both this test and the chart gate stay green.
func TestLabNamespacesEnforce(t *testing.T) {
	root := repoRoot(t)
	_, minor := clusterVersion(t)
	wantVersion := fmt.Sprintf("v1.%d", minor)
	declared := map[string]api.Level{}
	for _, c := range charts(t) {
		declared[c.name] = c.level
	}
	files, err := filepath.Glob(filepath.Join(root, "k8s", "00-namespaces", "*.yaml"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no namespace manifests (%v)", err)
	}
	namespaces := map[string]bool{}
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		for _, doc := range bytes.Split(b, []byte("\n---")) {
			var ns struct {
				Kind     string `json:"kind"`
				Metadata struct {
					Name   string            `json:"name"`
					Labels map[string]string `json:"labels"`
				} `json:"metadata"`
			}
			if err := yaml.Unmarshal(doc, &ns); err != nil {
				t.Fatalf("%s: %v", f, err)
			}
			if ns.Kind != "Namespace" {
				continue
			}
			name, l := ns.Metadata.Name, ns.Metadata.Labels
			namespaces[name] = true
			want := api.LevelRestricted
			if lvl, ok := declared[name]; ok {
				want = lvl
			}
			if got := l["pod-security.kubernetes.io/enforce"]; got != string(want) {
				t.Errorf("namespace %s: enforce=%q, want %q (chart %q declares it, or restricted)", name, got, want, name)
			}
			if got := l["pod-security.kubernetes.io/enforce-version"]; got != wantVersion {
				t.Errorf("namespace %s: enforce-version=%q, want %q (the kind node image's minor)", name, got, wantVersion)
			}
		}
	}
	if len(namespaces) == 0 {
		t.Fatal("no Namespace found under k8s/00-namespaces: nothing was checked")
	}
	for chartName, lvl := range declared {
		if lvl != api.LevelRestricted && !namespaces[chartName] {
			t.Errorf("chart %s declares %s but k8s/00-namespaces has no namespace %q of its own: it would land in a restricted one", chartName, lvl, chartName)
		}
	}
}

var (
	heredocOpenRE = regexp.MustCompile(`<<(-?)\s*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?`)
	shValueRE     = regexp.MustCompile(`(?m)(:[ \t]+)\$\{[^}]*\}[ \t]*$`)
	shVarRE       = regexp.MustCompile(`\$\{[^}]*\}|\$\([^)]*\)|\\\$\([^)]*\)`)
	podKindLineRE = regexp.MustCompile(`(?m)(?:^|\\n)[ \t]*kind:[ \t]*(Pod|PodTemplate|ReplicationController|ReplicaSet|Deployment|DaemonSet|StatefulSet|Job|CronJob)\b`)
)

// heredocs returns the bodies of the shell here-documents in b: from the line
// after `<<DELIM` up to the first line that is DELIM alone (Go's regexp has no
// back-references, so the closing line is found here). With `<<-`, leading
// tabs are stripped as bash does. An opener without a closer is reported.
func heredocs(b []byte) (bodies [][]byte, unclosed []string) {
	lines := bytes.Split(b, []byte("\n"))
	for i := 0; i < len(lines); i++ {
		if bytes.HasPrefix(bytes.TrimSpace(lines[i]), []byte("#")) {
			continue // a comment that mentions `<<EOF` opens nothing
		}
		m := heredocOpenRE.FindSubmatch(lines[i])
		if m == nil {
			continue
		}
		dash, delim := len(m[1]) > 0, string(m[2])
		closed := false
		for j := i + 1; j < len(lines); j++ {
			line := lines[j]
			if dash {
				line = bytes.TrimLeft(line, "\t")
			}
			if strings.TrimSpace(string(line)) == delim {
				body := make([][]byte, 0, j-i-1)
				for _, l := range lines[i+1 : j] {
					if dash {
						l = bytes.TrimLeft(l, "\t")
					}
					body = append(body, l)
				}
				bodies = append(bodies, bytes.Join(body, []byte("\n")))
				i, closed = j, true
				break
			}
		}
		if !closed {
			unclosed = append(unclosed, fmt.Sprintf("line %d: <<%s", i+1, delim))
		}
	}
	return bodies, unclosed
}

// TestScriptManifestsAreRestricted: a Kubernetes manifest a shell script under
// scripts/ applies from a heredoc runs in a lab namespace that enforces
// restricted. Shell expansions are replaced before judging. The pod-bearing
// `kind:` lines in the scripts (including printf's `\nkind:` form) are
// counted and must equal the pod specs judged, so a manifest the extractor
// misses turns this red instead of shrinking a count.
func TestScriptManifestsAreRestricted(t *testing.T) {
	root := repoRoot(t)
	_, minor := clusterVersion(t)
	lv := api.LevelVersion{Level: api.LevelRestricted, Version: api.MajorMinorVersion(1, minor)}
	evaluated, declared := 0, 0
	err := filepath.WalkDir(filepath.Join(root, "scripts"), func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(p, ".sh") {
			return err
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, p)
		declared += len(podKindLineRE.FindAll(b, -1))
		bodies, unclosed := heredocs(b)
		for _, u := range unclosed {
			t.Errorf("%s: heredoc %s has no closing line", rel, u)
		}
		for _, body := range bodies {
			if !podKindLineRE.Match(body) {
				continue
			}
			// A whole-value expansion becomes the string "1" (labels and names
			// must stay strings, a quantity accepts "1"); any other becomes 1.
			body = shValueRE.ReplaceAll(body, []byte(`${1}"1"`))
			body = shVarRE.ReplaceAll(body, []byte("1"))
			v := evaluate(t, body, lv)
			evaluated += v.evaluated
			for _, msg := range v.violations {
				t.Errorf("%s at %s: %s", rel, lv.String(), msg)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if evaluated == 0 {
		t.Fatal("no pod-bearing heredoc manifest found under scripts/: nothing was checked")
	}
	if evaluated != declared {
		t.Errorf("scripts/ declare %d pod-bearing `kind:` lines but %d pod specs were judged: a manifest is built in a shape this test does not extract", declared, evaluated)
	}
	t.Logf("scripts/: %d pod specs evaluated", evaluated)
}

// shellWords splits a command line the way sh would for quoting purposes:
// quotes group, adjacent quoted and unquoted parts join into one word, and an
// unquoted $( ... ) stays inside the word it belongs to. Expansions are left
// as text.
func shellWords(s string) []string {
	var out []string
	var cur strings.Builder
	in := false
	var q rune
	depth := 0
	rs := []rune(s)
	for i := 0; i < len(rs); i++ {
		r := rs[i]
		if depth > 0 {
			cur.WriteRune(r)
			switch r {
			case '(':
				depth++
			case ')':
				depth--
			}
			continue
		}
		if q != 0 {
			if r == q {
				q = 0
			} else {
				cur.WriteRune(r)
			}
			continue
		}
		switch {
		case r == '$' && i+1 < len(rs) && rs[i+1] == '(':
			cur.WriteString("$(")
			i++
			depth, in = 1, true
		case r == '\'' || r == '"':
			q, in = r, true
		case r == ' ' || r == '\t':
			if in {
				out = append(out, cur.String())
				cur.Reset()
				in = false
			}
		default:
			cur.WriteRune(r)
			in = true
		}
	}
	if in {
		out = append(out, cur.String())
	}
	return out
}

// mergePatch applies `kubectl run --overrides` the way kubectl does. With
// --override-type=merge (the default) it is an RFC 7386 JSON merge patch: maps
// merge, lists are replaced whole. With strategic, the pod-spec lists keyed by
// `name` (containers, initContainers, volumes) merge element by element — the
// part of a strategic merge patch these commands use.
func mergePatch(dst, patch map[string]interface{}, strategic bool) map[string]interface{} {
	for k, v := range patch {
		if v == nil {
			delete(dst, k)
			continue
		}
		pm, ok := v.(map[string]interface{})
		dm, dok := dst[k].(map[string]interface{})
		if ok && dok {
			dst[k] = mergePatch(dm, pm, strategic)
			continue
		}
		pl, pok := v.([]interface{})
		dl, dlok := dst[k].([]interface{})
		if strategic && pok && dlok && (k == "containers" || k == "initContainers" || k == "volumes") {
			dst[k] = mergeByName(dl, pl)
			continue
		}
		dst[k] = v
	}
	return dst
}

func mergeByName(dst, patch []interface{}) []interface{} {
	for _, pe := range patch {
		pm, _ := pe.(map[string]interface{})
		merged := false
		for i, de := range dst {
			if dm, ok := de.(map[string]interface{}); ok && pm != nil && dm["name"] == pm["name"] {
				dst[i] = mergePatch(dm, pm, true)
				merged = true
			}
		}
		if !merged {
			dst = append(dst, pe)
		}
	}
	return dst
}

// subcommand is the first word after `kubectl` that is not a flag or a flag's
// value (`-n ns`, `--namespace ns`, `--context x`; `--flag=v` is one word).
func subcommand(words []string) string {
	takesValue := map[string]bool{"-n": true, "--namespace": true, "--context": true, "--kubeconfig": true, "-s": true, "--server": true}
	for i := 1; i < len(words); i++ {
		w := words[i]
		if takesValue[w] {
			i++
			continue
		}
		if strings.HasPrefix(w, "-") {
			continue
		}
		return w
	}
	return ""
}

// flagValue returns the value of --name, written as `--name=v` or `--name v`,
// looking only before `--` (what follows `--` is the container's command).
func flagValue(words []string, name string) (string, bool) {
	for i, w := range words {
		if w == "--" {
			break
		}
		if strings.HasPrefix(w, name+"=") {
			return strings.TrimPrefix(w, name+"="), true
		}
		if w == name && i+1 < len(words) && words[i+1] != "--" {
			return words[i+1], true
		}
	}
	return "", false
}

var (
	continuationRE = regexp.MustCompile(`\\\n[ \t]*`)
	// A kubectl invocation anywhere in a line — a code block, inline code,
	// behind an env-var prefix — up to the end of the line or inline code.
	kubectlCallRE = regexp.MustCompile("\\bkubectl\\b[^\\n`]*")
)

// debugPodSources are the operator docs whose kubectl commands run in the
// lab's namespaces (docs/internal) or in a chart's own namespace
// (components/*/README.md). Customer-facing docs are out of scope: they target
// the customer's namespaces and workloads.
func debugPodSources(t *testing.T, root string) []string {
	t.Helper()
	var files []string
	err := filepath.WalkDir(filepath.Join(root, "docs", "internal"), func(p string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() && strings.HasSuffix(p, ".md") {
			files = append(files, p)
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	readmes, _ := filepath.Glob(filepath.Join(root, "components", "*", "README.md"))
	return append(files, readmes...)
}

// TestRunbookDebugPodsAreRestricted: a `kubectl run` in an operator doc must
// produce a pod that passes restricted after --overrides is applied the way
// kubectl applies it, and a `kubectl debug` must use --profile=restricted
// (kubectl's default profile adds SYS_PTRACE, which restricted forbids — on a
// --copy-to pod as well as an ephemeral container).
func TestRunbookDebugPodsAreRestricted(t *testing.T) {
	root := repoRoot(t)
	_, minor := clusterVersion(t)
	lv := api.LevelVersion{Level: api.LevelRestricted, Version: api.MajorMinorVersion(1, minor)}
	runs, debugs := 0, 0
	for _, p := range debugPodSources(t, root) {
		b, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		rel, _ := filepath.Rel(root, p)
		text := continuationRE.ReplaceAllString(string(b), " ")
		for _, call := range kubectlCallRE.FindAllString(text, -1) {
			words := shellWords(call)
			switch subcommand(words) {
			case "debug":
				if len(words) < 3 {
					continue // prose naming the command, not running it
				}
				debugs++
				if prof, _ := flagValue(words, "--profile"); prof != "restricted" {
					t.Errorf("%s: `kubectl debug` without --profile=restricted (the default adds SYS_PTRACE): %s", rel, strings.TrimSpace(call))
				}
			case "run":
				runs++
				judgeRun(t, rel, words, lv)
			}
		}
	}
	if runs == 0 {
		t.Fatal("no `kubectl run` found in the operator docs: the scan is broken, not the docs clean")
	}
	t.Logf("operator docs: %d kubectl run, %d kubectl debug judged", runs, debugs)
}

func judgeRun(t *testing.T, rel string, words []string, lv api.LevelVersion) {
	t.Helper()
	name := ""
	for i, w := range words {
		if w == "run" && i+1 < len(words) && !strings.HasPrefix(words[i+1], "-") {
			name = words[i+1]
			break
		}
	}
	image, _ := flagValue(words, "--image")
	if name == "" && image == "" {
		return // prose that names the command without running it
	}
	if name == "" || image == "" {
		t.Errorf("%s: cannot read the pod name and image of: %s", rel, strings.Join(words, " "))
		return
	}
	pod := map[string]interface{}{
		"apiVersion": "v1", "kind": "Pod",
		"metadata": map[string]interface{}{"name": name},
		"spec": map[string]interface{}{"containers": []interface{}{
			map[string]interface{}{"name": name, "image": image},
		}},
	}
	if overrides, ok := flagValue(words, "--overrides"); ok {
		overrideType, _ := flagValue(words, "--override-type")
		if overrideType == "" {
			overrideType = "merge"
		}
		if overrideType != "merge" && overrideType != "strategic" {
			t.Errorf("%s: `kubectl run %s` uses --override-type=%s, which this check does not model", rel, name, overrideType)
			return
		}
		var patch map[string]interface{}
		if err := json.Unmarshal([]byte(overrides), &patch); err != nil {
			t.Errorf("%s: --overrides of `kubectl run %s` is not JSON: %v", rel, name, err)
			return
		}
		if overrideType == "strategic" {
			spec, _ := patch["spec"].(map[string]interface{})
			cs, _ := spec["containers"].([]interface{})
			for _, c := range cs {
				if cm, _ := c.(map[string]interface{}); cm == nil || cm["name"] != name {
					t.Errorf("%s: `kubectl run %s` strategic override names a container other than %q: it would ADD a container, outside this check's model", rel, name, name)
					return
				}
			}
		}
		pod = mergePatch(pod, patch, overrideType == "strategic")
	}
	raw, _ := json.Marshal(pod)
	v := evaluate(t, raw, lv)
	if v.evaluated != 1 {
		t.Errorf("%s: `kubectl run %s` did not yield one judged pod", rel, name)
	}
	for _, msg := range v.violations {
		t.Errorf("%s: `kubectl run %s` at %s: %s", rel, name, lv.String(), msg)
	}
}
