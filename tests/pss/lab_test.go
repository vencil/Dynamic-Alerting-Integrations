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
// this is where the namespace is held to it.
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
	seen := 0
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
			seen++
			name, l := ns.Metadata.Name, ns.Metadata.Labels
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
	if seen == 0 {
		t.Fatal("no Namespace found under k8s/00-namespaces: nothing was checked")
	}
}

var (
	heredocOpenRE = regexp.MustCompile(`<<-?\s*['"]?([A-Za-z_]+)['"]?`)
	shValueRE     = regexp.MustCompile(`(?m)(:[ \t]+)\$\{[^}]*\}[ \t]*$`)
	shVarRE       = regexp.MustCompile(`\$\{[^}]*\}|\$\([^)]*\)|\\\$\([^)]*\)`)
)

// heredocs returns the bodies of the shell here-documents in b: from the line
// after `<<DELIM` up to the first line that is DELIM alone (Go's regexp has no
// back-references, so the closing line is found here).
func heredocs(b []byte) [][]byte {
	var out [][]byte
	lines := bytes.Split(b, []byte("\n"))
	for i := 0; i < len(lines); i++ {
		m := heredocOpenRE.FindSubmatch(lines[i])
		if m == nil {
			continue
		}
		delim := string(m[1])
		for j := i + 1; j < len(lines); j++ {
			if strings.TrimSpace(string(lines[j])) == delim {
				out = append(out, bytes.Join(lines[i+1:j], []byte("\n")))
				i = j
				break
			}
		}
	}
	return out
}

// TestScriptManifestsAreRestricted: a Kubernetes manifest a shell script under
// scripts/ applies from a heredoc runs in a lab namespace that enforces
// restricted. Shell expansions are replaced with "1" before judging.
func TestScriptManifestsAreRestricted(t *testing.T) {
	root := repoRoot(t)
	_, minor := clusterVersion(t)
	lv := api.LevelVersion{Level: api.LevelRestricted, Version: api.MajorMinorVersion(1, minor)}
	evaluated := 0
	err := filepath.WalkDir(filepath.Join(root, "scripts"), func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(p, ".sh") {
			return err
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		for _, body := range heredocs(b) {
			if !bytes.Contains(body, []byte("apiVersion:")) || !bytes.Contains(body, []byte("\nkind:")) && !bytes.HasPrefix(body, []byte("kind:")) {
				continue
			}
			// A whole-value expansion becomes the string "1" (labels and names
			// must stay strings, a quantity accepts "1"); any other becomes 1.
			body = shValueRE.ReplaceAll(body, []byte(`${1}"1"`))
			body = shVarRE.ReplaceAll(body, []byte("1"))
			v := evaluate(t, body, lv)
			evaluated += v.evaluated
			rel, _ := filepath.Rel(root, p)
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

var kubectlRunRE = regexp.MustCompile(`(?m)^[ \t]*(?:\$ )?kubectl\b.*\brun\b.*$`)

// TestRunbookDebugPodsAreRestricted: the `kubectl run` commands in
// docs/internal (operator runbooks for the lab's namespaces, which enforce
// restricted) must produce a pod that passes restricted, after --overrides is
// applied the way kubectl applies it. Customer-facing docs are out of scope:
// they target the customer's namespaces and workloads.
func TestRunbookDebugPodsAreRestricted(t *testing.T) {
	root := repoRoot(t)
	_, minor := clusterVersion(t)
	lv := api.LevelVersion{Level: api.LevelRestricted, Version: api.MajorMinorVersion(1, minor)}
	found := 0
	err := filepath.WalkDir(filepath.Join(root, "docs", "internal"), func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(p, ".md") {
			return err
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		text := regexp.MustCompile(`\\\n[ \t]*`).ReplaceAllString(string(b), " ")
		rel, _ := filepath.Rel(root, p)
		for _, line := range kubectlRunRE.FindAllString(text, -1) {
			words := shellWords(strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "$ ")))
			if subcommand(words) != "run" {
				continue
			}
			name, image, overrides, overrideType := "", "", "", "merge"
			for i, w := range words {
				switch {
				case w == "run" && name == "" && i+1 < len(words):
					name = words[i+1]
				case strings.HasPrefix(w, "--image="):
					image = strings.TrimPrefix(w, "--image=")
				case w == "--image" && i+1 < len(words):
					image = words[i+1]
				case strings.HasPrefix(w, "--overrides="):
					overrides = strings.TrimPrefix(w, "--overrides=")
				case w == "--overrides" && i+1 < len(words):
					overrides = words[i+1]
				case strings.HasPrefix(w, "--override-type="):
					overrideType = strings.TrimPrefix(w, "--override-type=")
				}
			}
			if name == "" || image == "" {
				t.Errorf("%s: cannot read the pod name and image of: %s", rel, strings.TrimSpace(line))
				continue
			}
			found++
			pod := map[string]interface{}{
				"apiVersion": "v1", "kind": "Pod",
				"metadata": map[string]interface{}{"name": name},
				"spec": map[string]interface{}{"containers": []interface{}{
					map[string]interface{}{"name": name, "image": image},
				}},
			}
			if overrides != "" {
				var patch map[string]interface{}
				if err := json.Unmarshal([]byte(overrides), &patch); err != nil {
					t.Errorf("%s: --overrides of `kubectl run %s` is not JSON: %v", rel, name, err)
					continue
				}
				if overrideType != "merge" && overrideType != "strategic" {
					t.Errorf("%s: `kubectl run %s` uses --override-type=%s, which this check does not model", rel, name, overrideType)
					continue
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
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if found == 0 {
		t.Fatal("no `kubectl run` found under docs/internal: the scan is broken, not the docs clean")
	}
	t.Logf("docs/internal: %d kubectl run commands judged", found)
}
