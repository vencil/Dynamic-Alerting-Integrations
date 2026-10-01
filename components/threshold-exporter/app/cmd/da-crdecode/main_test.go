package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// runOn writes *content* to a temp file and runs the CLI on it.
func runOn(t *testing.T, content string) (int, string, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cr.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	rc := run([]string{path}, &stdout, &stderr)
	return rc, stdout.String(), stderr.String()
}

func TestDecodesAsKubernetesClientsDo(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		// YAML 1.1 (PyYAML) reads these names as strings; the Kubernetes
		// client conversion does not (#2476).
		{"octal-0o", "name: 0o17\n", `{"documents":[{"name":15}]}` + "\n"},
		{"exponent", "name: 1e3\n", `{"documents":[{"name":1000}]}` + "\n"},
		{"leading-zero", "name: 08\n", `{"documents":[{"name":8}]}` + "\n"},
		{"y-is-bool", "name: y\n", `{"documents":[{"name":true}]}` + "\n"},
		// Keys become strings; JSON objects come out with sorted keys.
		{"keys", "b: 1\n010: 2\n", `{"documents":[{"8":2,"b":1}]}` + "\n"},
		{"empty-file", "", `{"documents":[null]}` + "\n"},
		{"leading-separator", "---\na: 1\n", `{"documents":[{"a":1}]}` + "\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runOn(t, tc.in)
			if rc != exitOK || out != tc.want || errOut != "" {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 0 stdout %q", rc, out, errOut, tc.want)
			}
		})
	}
}

func TestRefusesWhatKubernetesClientsRefuse(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, wantErr string
	}{
		{"null-key", "{null: 1}\n", "unsupported map key"},
		{"tagged-null-with-value", "a: !!null team\n", "as a !!null"},
		{"not-utf8", "a: \xff\n", "invalid leading UTF-8"},
		{"self-referencing-anchor", "a: &a [1, *a]\n", "contains itself"},
		{"inf-has-no-json", "a: .inf\n", "unsupported value"},
		// YAMLToJSON alone would read only the first document.
		{"two-documents", "a: 1\n---\nb: 2\n", "more than one YAML document"},
		{"trailing-separator", "a: 1\n---\n", "more than one YAML document"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runOn(t, tc.in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, no stdout, stderr with %q",
					rc, out, errOut, tc.wantErr)
			}
			if !strings.HasPrefix(errOut, programName+": ") {
				t.Fatalf("stderr %q does not start with %q", errOut, programName+": ")
			}
		})
	}
}

func TestCallerErrors(t *testing.T) {
	t.Parallel()
	missing := filepath.Join(t.TempDir(), "missing.yaml")
	for name, args := range map[string][]string{
		"no-args":      nil,
		"two-args":     {"a", "b"},
		"help":         {"-h"},
		"empty":        {""},
		"missing-file": {missing},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			var stdout, stderr bytes.Buffer
			if rc := run(args, &stdout, &stderr); rc != exitCallerErr || stdout.Len() != 0 || stderr.Len() == 0 {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, a message, no stdout", rc, stdout.String(), stderr.String())
			}
		})
	}
}
