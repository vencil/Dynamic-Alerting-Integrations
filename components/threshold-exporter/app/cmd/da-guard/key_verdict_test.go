package main

// The shared key verdict (key_verdict.go, pkg/keyverdict, #3489) over real
// trees — its ERROR layer and `da-guard key-verdict`'s document — and `da-guard
// write` refusing a batch that adds such an error (worsens_tree, kind
// unread). tests/shared/test_key_verdict.py pins its answers over the trees the
// Python tools build, through `da-guard key-verdict`.
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// verdictErrors is keyVerdictOf of the tree at root on disk, each error as
// "tenant key reason file".
func verdictErrors(t *testing.T, root string) []string {
	t.Helper()
	list, err := config.ScanDirTreeFiles(root, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	doc, err := parseTreeOf(list)
	if err != nil {
		t.Fatal(err)
	}
	res, err := keyVerdictOf(root, keyVerdictOpts{rootPlatform: hasRootPlatformFile(doc), profiles: rootProfilesOf(doc)},
		time.Now().UTC())
	if err != nil || !res.judged {
		t.Fatalf("not judged: %v %v", err, res.why)
	}
	out := []string{}
	for _, e := range res.errors {
		out = append(out, strings.Join([]string{e.Tenant, e.Key, e.Reason, e.File}, " "))
	}
	return out
}

const kvRoot = "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"

func TestKeyVerdict_TheErrorLayer(t *testing.T) {
	t.Parallel()
	a, b := "t1", "t2" // fixture tenant ids
	tenant := func(id, body string) string { return "tenants:\n  " + id + ":\n" + body }
	cases := []struct {
		name  string
		files map[string]string
		want  []string
	}{
		{"a misspelt threshold key is no_row",
			map[string]string{"_defaults.yaml": kvRoot, "a.yaml": tenant(a, "    mysql_conections: \"70\"\n")},
			[]string{a + " mysql_conections no_row a.yaml"}},
		{"a dimensional key whose labels the root does not declare",
			map[string]string{"_defaults.yaml": kvRoot,
				"a.yaml": tenant(a, "    'redis_memory{queue=\"q\"}': 5\n")},
			[]string{a + ` redis_memory{queue="q"} no_row a.yaml`}},
		{"a dimensional key the root declares is read",
			map[string]string{"_defaults.yaml": "dimensions:\n  redis_queue_length: [queue]\n" + kvRoot,
				"a.yaml": tenant(a, "    'redis_queue_length{queue=\"q\"}': 5\n")},
			[]string{}},
		// #3395: the profile's key only optional_overrides declares, with no
		// platform value, is not filled in (#1189): unread on the profile file.
		{"a bound profile's key the exporter does not fill in",
			map[string]string{"_defaults.yaml": kvRoot + "optional_overrides:\n  - redis_evicted_keys\n",
				"_profiles.yaml": "profiles:\n  gold:\n    redis_evicted_keys: \"11\"\n",
				"a.yaml":         tenant(a, "    _profile: gold\n")},
			[]string{a + " redis_evicted_keys profile_not_filled _profiles.yaml"}},
		{"an unregistered reserved key in the tenant's file",
			map[string]string{"_defaults.yaml": kvRoot, "a.yaml": tenant(a, "    _routng: disable\n")},
			[]string{a + " _routng unregistered a.yaml"}},
		{"a disable that switches nothing off is a warning, not an error",
			map[string]string{"_defaults.yaml": kvRoot, "a.yaml": tenant(a, "    mysql_slave_lagg: disable\n")},
			[]string{}},
		{"a key a policy rule names is read; a wildcard names none; an excluded tenant is not credited",
			map[string]string{
				"_defaults.yaml": kvRoot + "_policies:\n" +
					"  - {name: p, target: owner_tag.team, operator: required, exclude_tenants: [" + b + "]}\n" +
					"  - {name: w, target: 'mysql_*', operator: required}\n",
				"a.yaml": tenant(a, "    owner_tag: x\n    mysql_conections: \"1\"\n"),
				"b.yaml": tenant(b, "    owner_tag: y\n")},
			[]string{a + " mysql_conections no_row a.yaml", b + " owner_tag no_row b.yaml"}},
		{"a when.target head names its key",
			map[string]string{
				"_defaults.yaml": kvRoot + "_policies:\n" +
					"  - {name: p, target: mysql_connection_usage_percent, operator: lte, value: 100, when: {target: owner_tag, operator: required}}\n",
				"a.yaml": tenant(a, "    owner_tag: x\n")},
			[]string{}},
		{"no root platform file: threshold keys are undecidable, reserved keys judged",
			map[string]string{"a.yaml": tenant(a, "    mysql_conections: \"70\"\n    _routng: x\n")},
			[]string{a + " _routng unregistered a.yaml"}},
		{"a key from a platform file or a profile is an error too (#3394)",
			map[string]string{"_defaults.yaml": kvRoot,
				"_platform.yaml": tenant(a, "    mysql_conections: \"70\"\n    _routng: x\n"),
				"_profiles.yaml": "profiles:\n  p1:\n    redis_memroy: 5\n",
				"a.yaml":         tenant(a, "    _profile: p1\n")},
			[]string{a + " _routng unregistered _platform.yaml", a + " mysql_conections no_row _platform.yaml",
				a + " redis_memroy no_row _profiles.yaml"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := verdictErrors(t, writeTree(t, tc.files)); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("errors\n got %q\nwant %q", got, tc.want)
			}
		})
	}
}

func TestPolicyHeads_ReadAsPolicyAsCodeReadsThem(t *testing.T) {
	t.Parallel()
	heads := func(doc string) []string {
		out := []string{}
		for _, h := range policyHeadsOf([]byte(doc)) {
			out = append(out, fmt.Sprintf("%s%v", h.Head, sortedKeys(h.Excluded)))
		}
		return out
	}
	cases := []struct{ doc, want string }{
		// `policies` wins over `_policies`, even when it holds no rule.
		{"policies:\n  - {target: a}\n_policies:\n  - {target: b}\n", "[a[]]"},
		{"policies:\n_policies:\n  - {target: b}\n", "[]"},
		// exclude_tenants as source text (#2114): 010 stays "010".
		{"_policies:\n  - {target: a.b, exclude_tenants: [010, x]}\n", "[a[010 x]]"},
		// What Python refuses (InvalidPolicyRules) is skipped here.
		{"_policies:\n  - target: 5\n  - x\n  - {target: c, when: {target: d.e}}\n", "[c[] d[]]"},
		{"_policies: oops\n", "[]"},
		{"- not a mapping\n", "[]"},
	}
	for _, tc := range cases {
		if got := fmt.Sprint(heads(tc.doc)); got != tc.want {
			t.Errorf("%q: heads %s, want %s", tc.doc, got, tc.want)
		}
	}
}

func TestKeyVerdict_Subcommand(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
		"a.yaml": "tenants:\n  t1:\n    mysql_conections: \"70\"\n"})
	var out, errOut bytes.Buffer
	if code := run([]string{keyVerdictCmd, "--config-dir", root, "--at", "2026-07-01T03:00:00Z"}, &out, &errOut); code != exitOK {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	var doc map[string]any
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	if doc["schema"] != keyVerdictSchema || doc["at"] != "2026-07-01T03:00:00Z" || doc["undecidable"] != false ||
		doc["blocks"] != true {
		t.Fatalf("head %v %v %v %v", doc["schema"], doc["at"], doc["undecidable"], doc["blocks"])
	}
	misspelt := map[string]any{"key": "mysql_conections", "layer": "tenant", "file": "a.yaml", "read_by": []any{},
		"verdict": "unread", "severity": "error", "reason": "no_row", "disable": nil, "hint": nil, "switched_off_with": nil,
		"dead_entries": []any{}, "value": "70", "served": nil}
	rows := doc["tenants"].(map[string]any)["t1"].([]any)
	if got := fmt.Sprintf("%d %v", len(rows), rows[0].(map[string]any)["key"]); got != "4 mysql_conections" {
		t.Fatalf("t1's keys: %s", got)
	}
	if !reflect.DeepEqual(rows[0], misspelt) {
		t.Fatalf("t1's misspelt key %v, want %v", rows[0], misspelt)
	}
	finding := map[string]any{"severity": "error", "kind": "unread", "key": "mysql_conections", "file": "a.yaml",
		"tenants": []any{"t1"}, "reason": "no_row", "hint": nil, "base": nil, "dead_entries": nil, "blocks": true}
	if fs := doc["findings"].([]any); len(fs) != 1 || !reflect.DeepEqual(fs[0], finding) {
		t.Fatalf("findings %v, want [%v]", fs, finding)
	}
	if _, subset := doc["tenants_requested"]; subset {
		t.Fatalf("a whole-tree verdict carries tenants_requested")
	}
	// --tenant: only the requested tenants, the absent one named.
	out.Reset()
	if code := run([]string{keyVerdictCmd, "--config-dir", root, "--tenant", "t9", "--tenant", "t1"}, &out, &errOut); code != exitOK {
		t.Fatalf("--tenant: exit %d: %s", code, errOut.String())
	}
	doc = nil
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprint(doc["tenants_requested"], doc["tenants_absent"], len(doc["tenants"].(map[string]any))); got != "[t1 t9] [t9] 1" {
		t.Fatalf("--tenant: %s", got)
	}
	// A tree the verdict cannot judge is no empty answer.
	dup := writeTree(t, map[string]string{"_defaults.yaml": kvRoot, "a.yaml": "tenants:\n  t1: {}\n",
		"b.yaml": "tenants:\n  t1: {}\n"})
	out.Reset()
	if code := run([]string{keyVerdictCmd, "--config-dir", dup}, &out, &errOut); code != exitCallerErr || out.Len() != 0 {
		t.Fatalf("a duplicate tenant: exit %d stdout %q", code, out.String())
	}
	if code := run([]string{keyVerdictCmd, "--config-dir", root, "--at", "yesterday"}, &out, &errOut); code != exitCallerErr {
		t.Fatalf("a bad --at: exit %d", code)
	}
}

// A batch that writes a key nobody reads into a tenant file is refused, kind
// unread; one that leaves such a key where it was, or removes it, is not.
func TestWrite_ANewUnreadKeyWorsensTheTree(t *testing.T) {
	t.Parallel()
	set := func(key, value string) string {
		return `{"files":[{"path":"a.yaml","ops":[{"op":"set","path":["tenants","t1",` +
			strconvQuote(key) + `],"value":{"str":` + strconvQuote(value) + `}}]}]}`
	}
	t.Run("a misspelt key is refused", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot, "a.yaml": "tenants:\n  t1:\n    mysql_connection_usage_percent: \"70\"\n"})
		before := snapshot(t, root)
		code, res := runBatch(t, writeCmd, set("mysql_conections", "60"), "--config-dir", root)
		if code != exitFindings || callReason(res) != writeReasonWorsensTree {
			t.Fatalf("exit %d reason %q worsened %v", code, callReason(res), worsenedOf(res))
		}
		if got := worsenedOf(res); !reflect.DeepEqual(got, []string{"unread a.yaml t1"}) || res.Worsened[0].Finding != "no_row" ||
			!strings.Contains(res.Worsened[0].Message, `"mysql_conections"`) {
			t.Fatalf("worsened %v %+v", got, res.Worsened)
		}
		sameTree(t, before, snapshot(t, root))
	})
	t.Run("an undeclared dimensional key is refused", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot, "a.yaml": "tenants:\n  t1: {}\n"})
		code, res := runBatch(t, writeCmd, set(`redis_memory{queue="q"}`, "5"), "--config-dir", root)
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread a.yaml t1"}) {
			t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
		}
	})
	t.Run("a new misspelt key in a profile file is refused (#3016, #3394)", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"_profiles.yaml": "profiles:\n  p1:\n    mysql_connection_usage_percent: \"70\"\n",
			"a.yaml":         "tenants:\n  t1:\n    _profile: p1\n"})
		in := `{"files":[{"path":"_profiles.yaml","ops":[{"op":"set","path":["profiles","p1","mysql_conections"],` +
			`"value":{"str":"60"}}]}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root)
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread _profiles.yaml t1"}) {
			t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
		}
		// The same key already there: an edit beside it is not refused.
		root = writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"_profiles.yaml": "profiles:\n  p1:\n    mysql_conections: \"70\"\n",
			"a.yaml":         "tenants:\n  t1:\n    _profile: p1\n"})
		in = `{"files":[{"path":"_profiles.yaml","ops":[{"op":"set","path":["profiles","p1","redis_memory"],` +
			`"value":{"str":"60"}}]}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitOK {
			t.Fatalf("an existing error blocked another key: exit %d worsened %v", code, worsenedOf(res))
		}
	})
	t.Run("unbinding a profile's last tenant does not make its old error new (#3016)", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"_profiles.yaml": "profiles:\n  p1:\n    _routng: x\n",
			"a.yaml":         "tenants:\n  t1:\n    _profile: p1\n"})
		in := `{"files":[{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","_profile"]}]},` +
			`{"path":"_profiles.yaml","ops":[{"op":"set","path":["profiles","p1","redis_memory"],"value":{"str":"60"}}]}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitOK {
			t.Fatalf("exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
	})
	t.Run("fixing a tenant entry beside a profile with the same error in one file passes (#3016)", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"_profiles.yaml": "profiles:\n  p1:\n    _routng: x\ntenants:\n  t1:\n    _routng: x\n",
			"a.yaml":         "tenants:\n  t1:\n    _profile: p1\n"})
		in := `{"files":[{"path":"_profiles.yaml","ops":[{"op":"unset","path":["tenants","t1","_routng"]}]}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitOK {
			t.Fatalf("exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
	})
	t.Run("a profile's new error is refused though the tenant's file has the same one (#3016)", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"_profiles.yaml": "profiles:\n  p1:\n    redis_memory: \"60\"\n",
			"a.yaml":         "tenants:\n  t1:\n    _profile: p1\n    _routng: x\n"})
		in := `{"files":[{"path":"_profiles.yaml","ops":[{"op":"set","path":["profiles","p1","_routng"],` +
			`"value":{"str":"y"}}]}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root)
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread _profiles.yaml t1"}) {
			t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
		}
	})
	t.Run("a key a policy rule names in the other #1231 spelling is read, not refused", func(t *testing.T) {
		t.Parallel()
		// #3489 review: the exporter serves a dimensional retired `_critical`
		// as written, but Policy-as-Code reads it in the canonical spelling.
		root := writeTree(t, map[string]string{
			"_defaults.yaml": kvRoot + "_policies:\n  - {name: p, target: 'mysql_threads_running_critical{db=\"a\"}', " +
				"operator: required, severity: error}\n",
			"a.yaml": "tenants:\n  t1: {}\n"})
		code, res := runBatch(t, writeCmd, set(`mysql_cpu_critical{db="a"}`, "3"), "--config-dir", root)
		if code != exitOK || len(res.Worsened) != 0 {
			t.Fatalf("exit %d reason %q worsened %v", code, callReason(res), worsenedOf(res))
		}
	})
	t.Run("an error already there does not block another key, and a fix passes", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"a.yaml": "tenants:\n  t1:\n    mysql_conections: \"70\"\n"})
		if code, res := runBatch(t, writeCmd, set("mysql_connection_usage_percent", "60"), "--config-dir", root); code != exitOK {
			t.Fatalf("an unrelated key: exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
		unset := `{"files":[{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","mysql_conections"]}]}]}`
		if code, res := runBatch(t, writeCmd, unset, "--config-dir", root); code != exitOK {
			t.Fatalf("the fix: exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
	})
	t.Run("a tenant moved between files keeps its baseline", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"a.yaml": "tenants:\n  t1:\n    mysql_conections: \"70\"\n", "b.yaml": "tenants:\n  t2: {}\n"})
		in := `{"files":[
			{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1"]}]},
			{"path":"b.yaml","ops":[{"op":"set","path":["tenants","t1"],"value":{"map":[["mysql_conections",{"str":"70"}]]}}]}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitOK {
			t.Fatalf("exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
	})
	t.Run("a platform-only batch is not judged on other tenants' keys", func(t *testing.T) {
		t.Parallel()
		// Parity with tenant-api's gate (keyVerdictRefusal): dropping a
		// root default and a `dimensions:` declaration leaves t1's keys in
		// a.yaml read by nothing, but a.yaml is not written; validate-config
		// reports them, this write does not refuse on them.
		root := writeTree(t, map[string]string{
			"_defaults.yaml": "dimensions:\n  redis_queue_length: [queue]\n" + kvRoot + "  owner_x: 1\n",
			"a.yaml":         "tenants:\n  t1:\n    owner_x: \"5\"\n    'redis_queue_length{queue=\"q\"}': 5\n"})
		in := `{"files":[{"path":"_defaults.yaml","ops":[{"op":"unset","path":["dimensions"]},{"op":"unset","path":["defaults","owner_x"]}]}]}`
		if before := verdictErrors(t, root); len(before) != 0 {
			t.Fatalf("before: %v", before)
		}
		code, res := runBatch(t, writeCmd, in, "--config-dir", root)
		if code != exitOK {
			t.Fatalf("exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
		if after := verdictErrors(t, root); len(after) != 2 {
			t.Fatalf("the write did leave t1 two unread keys, which it is not judged on: %v", after)
		}
	})
	t.Run("a tenant file written with a new unread key is refused beside another tenant's errors", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"a.yaml": "tenants:\n  t1:\n    mysql_connection_usage_percent: \"70\"\n",
			"b.yaml": "tenants:\n  t2:\n    redis_memroy: \"1\"\n"})
		code, res := runBatch(t, writeCmd, set("mysql_conections", "60"), "--config-dir", root)
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread a.yaml t1"}) {
			t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
		}
	})
	t.Run("a tenant moved into a batch file: its old error is no new one, a new error is", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"a.yaml": "tenants:\n  t1:\n    mysql_conections: \"70\"\n", "b.yaml": "tenants:\n  t2: {}\n"})
		in := `{"files":[
			{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1"]}]},
			{"path":"b.yaml","ops":[{"op":"set","path":["tenants","t1"],"value":{"map":[["mysql_conections",{"str":"70"}],["redis_memroy",{"str":"1"}]]}}]}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root)
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread b.yaml t1"}) ||
			!strings.Contains(res.Worsened[0].Message, "redis_memroy") {
			t.Fatalf("exit %d worsened %v %+v", code, worsenedOf(res), res.Worsened)
		}
	})
	t.Run("a tenant declared twice elsewhere does not turn the check off", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"a.yaml": "tenants:\n  tz: {}\n", "b.yaml": "tenants:\n  tz: {}\n",
			"sub/t.yaml": "tenants:\n  t1:\n    mysql_connection_usage_percent: \"70\"\n"})
		in := `{"files":[{"path":"sub/t.yaml","ops":[{"op":"set","path":["tenants","t1","mysql_conections"],"value":{"str":"1"}}]}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root, "--dry-run")
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread sub/t.yaml t1"}) {
			t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
		}
	})
	t.Run("removing a duplicate copy: the tenant had no baseline, its old error is not new", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"a.yaml": "tenants:\n  tz:\n    mysql_conections: \"1\"\n", "b.yaml": "tenants:\n  tz: {}\n"})
		in := `{"files":[{"path":"b.yaml","delete":true},
			{"path":"a.yaml","ops":[{"op":"set","path":["tenants","tz","mysql_connection_usage_percent"],"value":{"str":"5"}}]}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root, "--dry-run"); code != exitOK {
			t.Fatalf("exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
	})
	t.Run("an empty tree is an empty baseline", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"README": "no config yet\n"})
		in := `{"files":[{"path":"_defaults.yaml","create":{"map":[["defaults",{"map":[["mysql_connection_usage_percent",{"int":"80"}]]}]]}},
			{"path":"t.yaml","create":{"map":[["tenants",{"map":[["t1",{"map":[["_routng",{"str":"x"}],["mysql_conections",{"str":"1"}]]}]]}]]}}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root, "--dry-run")
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread t.yaml t1", "unread t.yaml t1"}) {
			t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
		}
	})
	t.Run("a new threshold key beside the root carrier the batch adds is judged", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"u.yaml": "tenants:\n  t9:\n    mysql_conections: \"1\"\n"})
		in := `{"files":[{"path":"_defaults.yaml","create":{"map":[["defaults",{"map":[["mysql_connection_usage_percent",{"int":"80"}]]}]]}},
			{"path":"t.yaml","create":{"map":[["tenants",{"map":[["t1",{"map":[["mysql_conections",{"str":"1"}]]}]]}]]}}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root, "--dry-run")
		if code != exitFindings || !reflect.DeepEqual(worsenedOf(res), []string{"unread t.yaml t1"}) {
			t.Fatalf("exit %d worsened %v", code, worsenedOf(res))
		}
	})
	t.Run("a before-tree the verdict cannot judge is a note, not a silent pass", func(t *testing.T) {
		t.Parallel()
		// tx's /metrics cannot be gathered (two keys, one series) before the
		// call; the batch repairs that and writes a typo beside it.
		root := writeTree(t, map[string]string{"_defaults.yaml": kvRoot,
			"a.yaml": "tenants:\n  tx:\n    mysql_connection_usage_percent: \"70:critical\"\n    mysql_connection_usage_percent_critical: 95\n"})
		in := `{"files":[{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","tx","mysql_connection_usage_percent_critical"]},
			{"op":"set","path":["tenants","tx","mysql_conections"],"value":{"str":"1"}}]}]}`
		var out, errOut bytes.Buffer
		runWrite(writeCmd, []string{"--config-dir", root, "--dry-run", "--json", "--input", "-"}, strings.NewReader(in), &out, &errOut)
		notes := 0
		for _, line := range strings.Split(errOut.String(), "\n") {
			if strings.Contains(line, ": note: the unread-key check judged no before-call tree") {
				notes++
			}
		}
		if notes != 1 {
			t.Fatalf("stderr carries %d notes, want 1:\n%s", notes, errOut.String())
		}
	})
	t.Run("adding the root carrier is not charged with the threshold keys it makes judgeable", func(t *testing.T) {
		t.Parallel()
		root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t1:\n    mysql_conections: \"70\"\n"})
		in := `{"files":[{"path":"_defaults.yaml","create":{"map":[["defaults",{"map":[["mysql_connection_usage_percent",{"int":"80"}]]}]]}}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitOK {
			t.Fatalf("exit %d worsened %v (%v)", code, worsenedOf(res), res.Message)
		}
	})
}

func strconvQuote(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

// kvWarnTree is a tree whose every reserved-key and row resolver logs a WARN
// line on the process log when served-values reads it.
var kvWarnTree = map[string]string{
	"_defaults.yaml": "state_filters:\n  maintenance: {reasons: [X], severity: critical}\n" + kvRoot,
	"a.yaml": "tenants:\n  t1:\n    _profile: nope\n    nosuch_critical: \"5\"\n" +
		"    mysql_connection_usage_percent: {default: \"5\", expires: \"bad\"}\n    _silent_mode: loud\n    _severity_dedup: maybe\n" +
		"    _routing: {receiver: {type: webhook, url: \"https://a.example\"}, group_wait: \"1x\"}\n    _metadata: {environment: {a: b}}\n" +
		"    _state_maintenance: {expires: \"zz\"}\n    _custom_alerts: [{recipe: nope, name: q}]\n",
}

// childStderr runs da-guard's run(args) in a child process (the binary's own
// process-global log on its own stderr, as served_values_warn_test.go does)
// and returns its stderr.
func childStderr(t *testing.T, args ...string) string {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestServedValuesChildProcess$")
	cmd.Env = append(os.Environ(), servedValuesChildEnv+"="+strings.Join(args, "\n"))
	var stdout, stderr strings.Builder
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	_ = cmd.Run() // the exit code is the caller's to judge from the output
	return stderr.String()
}

// The key verdict reads the tree quietly (#3489): the two served-values
// readings and the effective resolve a `da-guard write` now adds write no
// resolver line to its stderr. The control is served-values over the same
// tree, which prints them.
func TestKeyVerdict_ReadsTheTreeQuietly(t *testing.T) {
	t.Parallel()
	root := writeTree(t, kvWarnTree)
	control := childStderr(t, servedValuesCmd, "--config-dir", root, "--disable-effects")
	for _, w := range []string{`has no matching default`, `unknown silent mode "loud"`, `unknown severity_dedup`,
		`failed to parse _metadata`, `invalid group_wait`, `in _state_maintenance`, `invalid expires "bad"`} {
		if !strings.Contains(control, w) {
			t.Fatalf("control: served-values does not log %q, so the tree proves nothing:\n%s", w, control)
		}
	}
	if got := childStderr(t, keyVerdictCmd, "--config-dir", root); got != "" {
		t.Errorf("key-verdict stderr is not empty:\n%s", got)
	}
	// A write's stderr: each line as often as the same call without the key
	// verdict's readings would print it — validation's own (treeValidation,
	// twice: before and after) — never one more.
	in := filepath.Join(t.TempDir(), "in.json")
	if err := os.WriteFile(in, []byte(`{"files":[{"path":"a.yaml","ops":[{"op":"set","path":["tenants","t1","mysql_conections"],"value":{"str":"1"}}]}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	write := childStderr(t, writeCmd, "--config-dir", root, "--dry-run", "--json", "--input", in)
	guard := childStderr(t, "--config-dir", root, "--format", "json")
	stamp := regexp.MustCompile(`^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} `)
	count := func(text string) map[string]int {
		out := map[string]int{}
		for _, line := range strings.Split(text, "\n") {
			if line = stamp.ReplaceAllString(line, ""); strings.HasPrefix(line, "WARN") || strings.HasPrefix(line, "ERROR") {
				out[line]++
			}
		}
		return out
	}
	g := count(guard)
	for line, n := range count(write) {
		if n > 2*g[line] {
			t.Errorf("write logs %q %d times; validation of the two trees logs it %d", line, n, 2*g[line])
		}
	}
}

// #3395: "no tenant binds the profile" is said only over the whole tree —
// not when a tenant file the load drops may be the one binding it.
func TestKeyVerdict_AnUnboundProfileNeedsTheWholeTree(t *testing.T) {
	t.Parallel()
	notJudged := func(files map[string]string) (int, int) {
		root := writeTree(t, files)
		var out, errOut bytes.Buffer
		code := run([]string{keyVerdictCmd, "--config-dir", root}, &out, &errOut)
		var doc struct {
			Findings []struct{ Kind string } `json:"findings"`
		}
		if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
			t.Fatalf("exit %d: %v\n%s", code, err, errOut.String())
		}
		n := 0
		for _, f := range doc.Findings {
			if f.Kind == "profile_not_judged" {
				n++
			}
		}
		return code, n
	}
	profiles := "profiles:\n  gold:\n    mysql_connection_usage_percent: \"70\"\n"
	if _, n := notJudged(map[string]string{"_defaults.yaml": kvRoot, "_profiles.yaml": profiles,
		"b.yaml": "tenants:\n  t2: {}\n"}); n != 1 {
		t.Errorf("unbound over the whole tree: %d notice(s), want 1", n)
	}
	if code, n := notJudged(map[string]string{"_defaults.yaml": kvRoot, "_profiles.yaml": profiles,
		"a.yaml": "tenants:\n  t1:\n    _profile: gold\n  bad: [\n", "b.yaml": "tenants:\n  t2: {}\n"}); code != exitParseFailed || n != 0 {
		t.Errorf("with a dropped file: exit %d, %d notice(s), want exit %d and none", code, n, exitParseFailed)
	}
}
