package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func logProjFixture(t *testing.T) (confd, state, tpl string) {
	t.Helper()
	root := t.TempDir()
	confd = filepath.Join(root, "conf.d")
	state = filepath.Join(root, "state")
	for _, d := range []string{confd, state} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	write := func(p, s string) {
		if err := os.WriteFile(p, []byte(s), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(filepath.Join(confd, "_defaults.yaml"), "defaults:\n  cpu: 80\n")
	write(filepath.Join(confd, "t1.yaml"), "tenants:\n  t1:\n    cpu: 70\n    _log_projection: enable\n")
	write(filepath.Join(state, "_account_registry.yaml"), "schema_version: v1\nnext_account_id: 1001\nallocations:\n  t1: 1000\n")
	tpl = filepath.Join("..", "..", "internal", "federation", "logprojection", "testdata", "template.yaml")
	return confd, state, tpl
}

func TestLogProjRender_WritesTheConfig(t *testing.T) {
	confd, state, tpl := logProjFixture(t)
	var out, errb bytes.Buffer
	rc := runLogProjCmd([]string{"render", "--config-dir", confd, "--state-from", state, "--template", tpl}, nil, &out, &errb)
	if rc != 0 {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
	if !strings.Contains(out.String(), `"t1":1000`) || !strings.Contains(out.String(), "vl_tenant_1000:") {
		t.Errorf("%s", out.String())
	}
}

func TestLogProjRender_ExitCodes(t *testing.T) {
	confd, state, tpl := logProjFixture(t)
	bad := filepath.Join(t.TempDir(), "bad")
	if err := os.MkdirAll(bad, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bad, "_account_registry.yaml"),
		[]byte("schema_version: v1\nnext_account_id: 1001\nallocations: {t1: 1000, t2: 1000}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, tc := range map[string]struct {
		args []string
		rc   int
	}{
		"no subcommand":     {nil, 2},
		"unknown":           {[]string{"show"}, 2},
		"missing flag":      {[]string{"render", "--config-dir", confd, "--template", tpl}, 2},
		"stray argument":    {[]string{"render", "--config-dir", confd, "--state-from", state, "--template", tpl, "x"}, 2},
		"damaged ledger":    {[]string{"render", "--config-dir", confd, "--state-from", bad, "--template", tpl}, 1},
		"missing template":  {[]string{"render", "--config-dir", confd, "--state-from", state, "--template", "/nonexistent"}, 1},
		"missing conf.d":    {[]string{"render", "--config-dir", "/nonexistent", "--state-from", state, "--template", tpl}, 1},
		"run missing flags": {[]string{"run", "--template", tpl}, 2},
	} {
		var out, errb bytes.Buffer
		if rc := runLogProjCmd(tc.args, nil, &out, &errb); rc != tc.rc {
			t.Errorf("%s: rc %d, want %d (%s)", name, rc, tc.rc, errb.String())
		}
		if tc.rc != 0 && out.Len() != 0 {
			t.Errorf("%s: wrote output on failure", name)
		}
	}
}

func TestLoadConfdFiles_RefusesANonFileKey(t *testing.T) {
	for _, k := range []string{"../x.yaml", "a/b.yaml", "..", ""} {
		if _, err := loadConfdFiles(map[string]string{k: "tenants: {}\n"}); err == nil {
			t.Errorf("key %q loaded", k)
		}
	}
	c, err := loadConfdFiles(map[string]string{"_defaults.yaml": "defaults:\n  cpu: 80\n",
		"t1.yaml": "tenants:\n  t1:\n    _log_projection: enable\n"})
	if err != nil {
		t.Fatal(err)
	}
	if c.Tenants["t1"]["_log_projection"].Default != "enable" {
		t.Errorf("%v", c.Tenants["t1"])
	}
}
