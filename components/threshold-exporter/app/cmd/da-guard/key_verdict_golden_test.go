package main

// key_verdict_golden_test.go — `da-guard key-verdict`'s whole document over a
// few trees, pinned byte for byte (#3489). The Python tools read this
// document (scripts/tools/_lib_key_verdict.py) and judge nothing themselves,
// so a change to any field of it — a verdict, a severity, a finding's
// grouping, the JSON shape — is a change every caller sees. Each tree is one
// the shared verdict's own tests build (tests/shared/test_key_verdict.py):
// before the Python verdict was retired, tests/shared/test_key_verdict_go_parity.py
// held Go and Python to the same document over every such tree.
//
//	UPDATE_GOLDEN=1 go test ./cmd/da-guard -run TestKeyVerdict_Golden
//
// and review every changed line: each is a changed answer. Tenant ids are
// fixture names (CLAUDE.md #9).

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

const keyVerdictGoldenAt = "2026-07-01T03:00:00Z"

const kvgADRDefaults = "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"

const kvgADRTenant = "tenants:\n  db-a:\n" +
	"    mysql_conections: \"70\"\n" +
	"    mysql_slave_lagg: disable\n" +
	"    _routng: disable\n" +
	"    redis_memory: disable\n" +
	"    redis_memory_critical: \"90\"\n" +
	"    _namespaces: [prod-ns]\n"

var keyVerdictGoldenTrees = map[string]map[string]string{
	// ADR-038's worked example: a misspelt key, a noop disable, a misspelt
	// reserved key, a disable that switches its own critical tier off with
	// it (#3046: disable_cascade), an annotation.
	"adr-example": {"_defaults.yaml": kvgADRDefaults, "db-a.yaml": kvgADRTenant},
	// A platform file's typo reaching two tenants is one notice; a tenant's
	// own typo is one error per tenant; a disable switches off the critical
	// tier it inherits too, with no finding (#3046).
	"platform-and-cascade": {
		"_defaults.yaml":      "defaults:\n  mysql_connection_usage_percent: 80\n  redis_memory: 70\n",
		"_platform.yaml":      "tenants:\n  ta:\n    mysql_conections: 5\n  tb:\n    mysql_conections: 5\n",
		"team/_defaults.yaml": "defaults:\n  redis_memory_critical: 95\n",
		"team/ta.yaml":        "tenants:\n  ta:\n    redis_memory: disable\n",
		"tb.yaml":             "tenants:\n  tb:\n    redis_memroy: 3\n",
	},
	// A profile's `_` key no reader reads in the profile layer is one notice
	// on the root file, naming the tenants that bind it; a rule names a key
	// for the tenants it does not exclude.
	"profile-and-policy": {
		"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\nprofiles:\n  gold:\n    _namespaces: [a]\n    _routng: x\n" +
			"_policies:\n  - {name: p, target: owner_tag, operator: required, severity: error, exclude_tenants: [db-b]}\n",
		"db-a.yaml": "tenants:\n  db-a:\n    _profile: gold\n    owner_tag: x\n",
		"db-b.yaml": "tenants:\n  db-b:\n    _profile: gold\n    owner_tag: x\n",
	},
	// #3591: a reserved value the exporter does not read is judged in the
	// file and layer that writes it — an error in a tenant file, a notice
	// in a profile (naming the tenants that bind it) or a root platform
	// file's `tenants:` entry; a `_profile` naming no profile is one too.
	"value-unread": {
		"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\nprofiles:\n  gold:\n    _silent_mode: purple\n",
		"_platform.yaml": "tenants:\n  ta:\n    _metadata: {owner: a, bogus_field: x}\n",
		"ta.yaml":        "tenants:\n  ta:\n    _profile: gold\n    _severity_dedup: maybe\n",
		"tb.yaml":        "tenants:\n  tb:\n    _profile: silver\n",
	},
	// #3834: a reserved key written with a schedule its readers ignore —
	// a window or an `expires:` — is judged where it is written, as an
	// unread value is: an error in a tenant file, a notice in a profile or
	// a root platform file's `tenants:` entry; a profile's `_severity_dedup`,
	// which no reader reads in a profile (#2914), is that key's unread
	// finding alone, not a schedule one too; a threshold key's schedule is
	// read.
	"schedule-unread": {
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\nprofiles:\n  gold:\n" +
			"    _silent_mode: {default: disable, overrides: [{window: \"01:00-02:00\", value: all}]}\n" +
			"    _severity_dedup: {default: disable, overrides: [{window: \"01:00-02:00\", value: enable}]}\n",
		"_platform.yaml": "tenants:\n  tb:\n    _silent_mode: {default: disable, expires: \"2030-01-01T00:00:00Z\"}\n",
		"ta.yaml": "tenants:\n  ta:\n    _profile: gold\n" +
			"    _severity_dedup: {default: disable, overrides: [{window: \"00:00-23:59\", value: enable}]}\n" +
			"    mysql_connections: {default: \"70\", overrides: [{window: \"01:00-02:00\", value: \"90\"}]}\n",
		"tb.yaml": "tenants:\n  tb:\n    mysql_connections: 60\n",
	},
	// No root platform file: threshold keys are undecidable, `_` keys are
	// judged as usual.
	"undecidable": {"ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n    _routng: disable\n"},
}

func TestKeyVerdict_Golden(t *testing.T) {
	t.Parallel()
	update := os.Getenv("UPDATE_GOLDEN") != ""
	for name, files := range keyVerdictGoldenTrees {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			root := writeTree(t, files)
			var out, errOut bytes.Buffer
			if code := run([]string{keyVerdictCmd, "--config-dir", root, "--at", keyVerdictGoldenAt}, &out, &errOut); code != exitOK {
				t.Fatalf("exit %d: %s", code, errOut.String())
			}
			path := filepath.Join("testdata", "key_verdict", name+".json")
			if update {
				if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(path, out.Bytes(), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read golden: %v (regenerate with UPDATE_GOLDEN=1)", err)
			}
			if len(want) == 0 {
				t.Fatal("golden file is empty — regenerate with UPDATE_GOLDEN=1")
			}
			if !bytes.Equal(out.Bytes(), want) {
				t.Errorf("%s differs from %s (UPDATE_GOLDEN=1 regenerates; review every changed line):\n%s",
					name, path, out.String())
			}
		})
	}
}
