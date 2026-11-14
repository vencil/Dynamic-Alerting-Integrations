package config

// #3502: the closed reason sets a reader of da-guard's documents may meet —
// NotServedKey.Reason and DeadEntry.Reason — are this package's lists, and
// the Python reader (scripts/tools/_lib_tenant_values.py) refuses anything
// else. Both halves are pinned here, from structure: the constants from this
// package's syntax tree, the Python sets from the imported module itself.

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// TestNotServedReasons_EveryConstant: every NotServed* string constant of
// not_served.go is in NotServedReasons, and nothing else is.
func TestNotServedReasons_EveryConstant(t *testing.T) {
	f, err := parser.ParseFile(token.NewFileSet(), "not_served.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	var consts []string
	for _, d := range f.Decls {
		g, ok := d.(*ast.GenDecl)
		if !ok || g.Tok != token.CONST {
			continue
		}
		for _, spec := range g.Specs {
			vs := spec.(*ast.ValueSpec)
			for i, name := range vs.Names {
				if !strings.HasPrefix(name.Name, "NotServed") || i >= len(vs.Values) {
					continue
				}
				lit, ok := vs.Values[i].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					continue
				}
				v, err := strconv.Unquote(lit.Value)
				if err != nil {
					t.Fatal(err)
				}
				consts = append(consts, v)
			}
		}
	}
	got := NotServedReasons()
	sort.Strings(consts)
	sort.Strings(got)
	if len(consts) == 0 || !reflect.DeepEqual(got, consts) {
		t.Errorf("NotServedReasons() = %v, the NotServed* constants are %v", got, consts)
	}
}

// TestNotServedReasons_PythonReaderIsThisList: the Python reader's closed
// sets are NotServedReasons and DeadEntryReasons, read off the imported
// module (a child process).
func TestNotServedReasons_PythonReaderIsThisList(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		requireOrSkip(t, "python3 is not on PATH: the Python reader's reason sets cannot be read")
		return
	}
	tools := filepath.Join(findRepoRootForRegistry(t), "scripts", "tools")
	cmd := exec.Command(python, "-X", "utf8", "-c",
		"import json, _lib_tenant_values as tv; "+
			"print(json.dumps({'not_served': sorted(tv.NOT_SERVED_REASONS), 'dead': sorted(tv.DEAD_ENTRY_REASONS)}))")
	cmd.Dir = tools
	cmd.Env = append(os.Environ(), "PYTHONPATH="+tools)
	out, err := cmd.Output()
	if err != nil {
		msg := ""
		if ee, ok := err.(*exec.ExitError); ok {
			msg = string(ee.Stderr)
		}
		t.Fatalf("python3 could not read the sets: %v: %s", err, msg)
	}
	var sets struct {
		NotServed []string `json:"not_served"`
		Dead      []string `json:"dead"`
	}
	if err := json.Unmarshal(out, &sets); err != nil {
		t.Fatalf("not JSON: %v: %s", err, out)
	}
	want := NotServedReasons()
	sort.Strings(want)
	if !reflect.DeepEqual(sets.NotServed, want) {
		t.Errorf("_lib_tenant_values.NOT_SERVED_REASONS = %v, pkg/config's NotServedReasons() = %v", sets.NotServed, want)
	}
	dead := DeadEntryReasons()
	sort.Strings(dead)
	if !reflect.DeepEqual(sets.Dead, dead) {
		t.Errorf("_lib_tenant_values.DEAD_ENTRY_REASONS = %v, pkg/config's DeadEntryReasons() = %v", sets.Dead, dead)
	}
}
