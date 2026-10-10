package main

// key_refs_test.go — `da-guard key-refs` (#1822): the document, and the exit
// codes it shares with served-values / effective.

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

func runKeyRefsOn(t *testing.T, args ...string) (int, keyRefsDoc, string) {
	t.Helper()
	code, stdout, stderr := runOnce(t, append([]string{keyRefsCmd}, args...)...)
	var doc keyRefsDoc
	if stdout != "" {
		if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
			t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
		}
	}
	return code, doc, stderr
}

// The alias shape of #1822's first gap: the tenant writes the retired
// spelling of the metric being retired.
func TestKeyRefs_LegacyAliasListed(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults:\n  disk_usage: 80\n  mysql_threads_running: 30\n",
		"t1.yaml":        "tenants:\n  t1:\n    mysql_cpu: \"50\"\n",
		"t2.yaml":        "tenants:\n  t2:\n    mysql_threads_running: \"40\"\n",
	})
	code, doc, stderr := runKeyRefsOn(t, "--config-dir", dir,
		"--metric", "mysql_threads_running", "--metric", "disk_usage")
	if code != exitOK {
		t.Fatalf("exit %d, want 0; stderr:\n%s", code, stderr)
	}
	if doc.Schema != keyRefsSchema {
		t.Errorf("schema %q", doc.Schema)
	}
	if doc.ParseFailed == nil || len(doc.ParseFailed) != 0 || doc.Unreadable == nil || doc.Unscanned == nil {
		t.Errorf("parse_failed / unreadable / unscanned must be present and empty: %+v", doc)
	}
	want := map[string][]config.KeyRef{
		"mysql_threads_running": {
			{File: "_defaults.yaml", Section: "defaults", Key: "mysql_threads_running"},
			{File: "t1.yaml", Section: "tenants", Owner: "t1", Key: "mysql_cpu"},
			{File: "t2.yaml", Section: "tenants", Owner: "t2", Key: "mysql_threads_running"},
		},
		"disk_usage": {
			{File: "_defaults.yaml", Section: "defaults", Key: "disk_usage"},
		},
	}
	if !reflect.DeepEqual(doc.Refs, want) {
		t.Errorf("refs:\n got %+v\nwant %+v", doc.Refs, want)
	}
}

// The second gap's shape: the root carrier fails the exporter's decode. Exit
// 3, the JSON still written, the carrier in parse_failed.
func TestKeyRefs_RootCarrierDropped_ExitsThree(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\noptional_overrides: 5\n",
		"t2.yaml":        "tenants:\n  t2:\n    mysql_threads_running: \"40\"\n",
	})
	code, doc, stderr := runKeyRefsOn(t, "--config-dir", dir, "--metric", "mysql_threads_running")
	if code != exitParseFailed {
		t.Fatalf("exit %d, want 3; stderr:\n%s", code, stderr)
	}
	if !reflect.DeepEqual(doc.ParseFailed, []string{"_defaults.yaml"}) {
		t.Errorf("parse_failed = %v", doc.ParseFailed)
	}
	if !strings.Contains(stderr, "cannot unmarshal") || !strings.Contains(stderr, "_defaults.yaml") {
		t.Errorf("stderr does not carry the exporter's reason:\n%s", stderr)
	}
	want := []config.KeyRef{{File: "t2.yaml", Section: "tenants", Owner: "t2", Key: "mysql_threads_running"}}
	if !reflect.DeepEqual(doc.Refs["mysql_threads_running"], want) {
		t.Errorf("refs = %+v", doc.Refs["mysql_threads_running"])
	}
}

func TestKeyRefs_CallerErrors(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, map[string]string{
		"t1.yaml": "tenants:\n  t1:\n    disk_usage: 1\n",
	})
	dup := t.TempDir()
	testutil.WriteTree(t, dup, map[string]string{
		"a.yaml": "tenants:\n  t1:\n    disk_usage: 1\n",
		"b.yaml": "tenants:\n  t1:\n    disk_usage: 2\n",
	})
	for name, args := range map[string][]string{
		"no --config-dir":  {"--metric", "disk_usage"},
		"no --metric":      {"--config-dir", dir},
		"empty --metric":   {"--config-dir", dir, "--metric", ""},
		"stray argument":   {"--config-dir", dir, "--metric", "disk_usage", "extra"},
		"unknown flag":     {"--config-dir", dir, "--metric", "disk_usage", "--nope"},
		"missing dir":      {"--config-dir", dir + "/missing", "--metric", "disk_usage"},
		"duplicate tenant": {"--config-dir", dup, "--metric", "disk_usage"},
	} {
		if code, _, stderr := runKeyRefsOn(t, args...); code != exitCallerErr {
			t.Errorf("%s: exit %d, want 2; stderr:\n%s", name, code, stderr)
		}
	}
	code, stdout, stderr := runOnce(t, keyRefsCmd, "-h")
	if code != exitOK || stdout != "" || !strings.Contains(stderr, "Usage: da-guard key-refs") {
		t.Errorf("-h: exit %d, stdout %q, stderr:\n%s", code, stdout, stderr)
	}
}
