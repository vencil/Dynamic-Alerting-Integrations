package main

// config_hash_parity_test.go — #3965: every document da-guard computes from a
// walk of --config-dir names that walk's identity as `config_hash`, the value
// `parse-tree` reports for the same tree (pkg/config treeIdentity). A tool
// that judged a tree with parse-tree reads another document of it later and
// can tell whether the second walk read the same tree.
//
// ⛔ The dispatch table is the population: every subcommand is either in
// walksTheTree (its document must carry the parse-tree value, on exit 0 and
// on exit 3) or in readsNoTree with the reason. A subcommand added to
// `subcommands` in neither turns this red.
//
// Seams: none — t.TempDir() trees, dispatch() in process.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// walksTheTree: the dispatch name ("" for the default check) → the argv after
// `--config-dir <dir>` that makes it print one JSON document.
var walksTheTree = map[string][]string{
	"":                     {"--format", "json"},
	parseTreeCmd:           nil,
	servedValuesCmd:        nil,
	effectiveCmd:           nil,
	keyVerdictCmd:          nil,
	keyRefsCmd:             {"--metric", "mysql_connections"},
	compileCustomAlertsCmd: nil,
	severityDedupCmd:       nil,
}

// readsNoTree: the subcommands no walk of --config-dir answers, and why.
var readsNoTree = map[string]string{
	thresholdValuesCmd: "reads values on stdin, no tree",
	keySpellingsCmd:    "reads keys on stdin, no tree",
	writeCmd:           "its own document, through --input: TestConfigHash_WriteTakesTheJudgedIdentity",
}

func TestConfigHash_EverySubcommandIsClassified(t *testing.T) {
	t.Parallel()
	var missing []string
	for name := range subcommands {
		_, walks := walksTheTree[name]
		_, not := readsNoTree[name]
		if walks == not {
			missing = append(missing, name)
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		t.Fatalf("subcommands in neither (or both) walksTheTree and readsNoTree: %q", missing)
	}
}

func configHashTree(t *testing.T, files map[string]string, dangling ...string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for _, rel := range dangling {
		if err := os.Symlink(filepath.Join(dir, "nowhere"), filepath.Join(dir, rel)); err != nil {
			t.Skipf("os.Symlink unavailable here (%v)", err)
		}
	}
	return dir
}

// runForConfigHash runs one dispatch entry on dir and returns its exit code
// and the document's config_hash ("<absent>" when the field is missing,
// "<null>" when it is null).
func runForConfigHash(t *testing.T, name, dir string, extra ...string) (int, string) {
	t.Helper()
	args := []string{}
	if name != "" {
		args = append(args, name)
	}
	args = append(append(args, "--config-dir", dir), append(walksTheTree[name], extra...)...)
	var out, errOut bytes.Buffer
	rc := dispatch(args, strings.NewReader(""), &out, &errOut, guard.CheckDefaultsImpact)
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatalf("%q: rc %d, stdout is not one JSON document (%v); stderr:\n%s", args, rc, err, errOut.String())
	}
	raw, ok := doc["config_hash"]
	switch {
	case !ok:
		return rc, "<absent>"
	case string(raw) == "null":
		return rc, "<null>"
	}
	var h string
	if err := json.Unmarshal(raw, &h); err != nil {
		t.Fatalf("%q: config_hash %s is not a string", args, raw)
	}
	return rc, h
}

func TestConfigHash_EveryWalkNamesTheParseTreeIdentity(t *testing.T) {
	t.Parallel()
	defaults := "defaults:\n  mysql_connections: 70\n"
	tenant := func(id string) string { return "tenants:\n  " + id + ":\n    mysql_connections: \"80\"\n" }
	for _, tc := range []struct {
		name     string
		files    map[string]string
		dangling []string
		rc       int // every walking subcommand's exit code but parse-tree's (0)
	}{
		{"a served tree", map[string]string{"_defaults.yaml": defaults,
			"team-a/a.yaml": tenant("ta"), "team-b/b.yaml": tenant("tb")}, nil, exitOK},
		// Exit 3: a file the load drops — the identity is still the walk's.
		{"a tree with an unreadable file", map[string]string{"_defaults.yaml": defaults,
			"a.yaml": tenant("ta")}, []string{"c.yaml"}, exitParseFailed},
		{"a tree with a file that does not decode", map[string]string{"_defaults.yaml": defaults,
			"a.yaml": tenant("ta"), "b.yaml": "tenants: [this is not\n"}, nil, exitParseFailed},
		// The load's early return (no readable config file): exit 3, and
		// still the walk's identity, never "".
		{"a tree whose every config file is unreadable", map[string]string{"notes.txt": "x\n"},
			[]string{"a.yaml", "b.yaml"}, exitParseFailed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := configHashTree(t, tc.files, tc.dangling...)
			_, want := runForConfigHash(t, parseTreeCmd, dir)
			if len(want) != 64 {
				t.Fatalf("parse-tree config_hash = %s", want)
			}
			for name := range walksTheTree {
				rc, got := runForConfigHash(t, name, dir)
				if got != want {
					t.Errorf("%q (rc %d): config_hash %s, parse-tree's %s", name, rc, got, want)
				}
				if wantRC := tc.rc; name != parseTreeCmd && rc != wantRC {
					t.Errorf("%q: rc %d, want %d — the case no longer reaches the path it names", name, rc, wantRC)
				}
			}
		})
	}
}

// An overlay makes effective read a tree that is not the one on disk: it
// claims no identity, never the overlaid tree's as if it were the disk's.
func TestConfigHash_AnOverlaidEffectiveClaimsNone(t *testing.T) {
	t.Parallel()
	dir := configHashTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a.yaml": "tenants:\n  ta:\n    mysql_connections: \"80\"\n"})
	over := filepath.Join(t.TempDir(), "a.yaml")
	if err := os.WriteFile(over, []byte("tenants:\n  ta:\n    mysql_connections: \"81\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if rc, got := runForConfigHash(t, effectiveCmd, dir, "--overlay", "a.yaml="+over); got != "<null>" {
		t.Errorf("effective --overlay (rc %d): config_hash %s, want null", rc, got)
	}
}

// key-verdict over the caller's overlays (`da-guard write` judging its batch)
// reads a tree that is not on disk: no identity; without them, the disk's.
func TestConfigHash_KeyVerdictOverOverlaysClaimsNone(t *testing.T) {
	t.Parallel()
	dir := configHashTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a.yaml": "tenants:\n  ta:\n    mysql_connections: \"80\"\n"})
	_, want := runForConfigHash(t, parseTreeCmd, dir)
	at := time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC)
	plain, err := keyVerdictOf(dir, keyVerdictOpts{}, at)
	if err != nil || plain.identity != want {
		t.Fatalf("key-verdict on disk: identity %q (err %v), parse-tree's %s", plain.identity, err, want)
	}
	over, err := keyVerdictOf(dir, keyVerdictOpts{scan: config.ScanOptions{Overlays: []config.FileOverlay{
		{Rel: "a.yaml", Data: []byte("tenants:\n  ta:\n    mysql_connections: \"81\"\n")}}}}, at)
	if err != nil || over.identity != "" {
		t.Errorf("key-verdict over an overlay: identity %q (err %v), want none", over.identity, err)
	}
}

// The default check's decode-stop report (a chain carrier that fails the
// decode stops the run before any tenant, exit 3) names the walk too.
func TestConfigHash_TheDecodeStopReportNamesTheWalk(t *testing.T) {
	t.Parallel()
	dir := configHashTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"team/_defaults.yaml": "defaults: [1\n",
		"team/tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"80\"\n"})
	_, want := runForConfigHash(t, parseTreeCmd, dir)
	rc, got := runForConfigHash(t, "", dir, "--scope", "team")
	if rc != exitParseFailed || got != want {
		t.Errorf("decode stop: rc %d (want %d), config_hash %s, parse-tree's %s", rc, exitParseFailed, got, want)
	}
}

// key-verdict with a duplicate tenant set aside (`da-guard write`'s baseline)
// judges the tree with the declaring files removed — not the disk's tree, so
// it claims no identity.
func TestConfigHash_ASetAsideKeyVerdictClaimsNone(t *testing.T) {
	t.Parallel()
	dir := configHashTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a.yaml": "tenants:\n  ta:\n    mysql_connections: \"80\"\n",
		"b.yaml": "tenants:\n  ta:\n    mysql_connections: \"81\"\n",
		"c.yaml": "tenants:\n  tc:\n    mysql_connections: \"82\"\n"})
	res, err := keyVerdictOf(dir, keyVerdictOpts{scan: config.ScanOptions{SkipDuplicated: true}},
		time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	if err != nil || len(res.setAside) == 0 {
		t.Fatalf("the fixture set nothing aside (setAside %v, err %v)", res.setAside, err)
	}
	if res.identity != "" {
		t.Errorf("key-verdict with %v set aside: identity %q, want none", res.setAside, res.identity)
	}
}

// writeBatchWithExpect is a batch of one edit of t1.yaml (set
// mysql_connections) with expect_config_hash = expect ("" = none given).
func writeBatchWithExpect(expect, value string) string {
	b := `{"files":[{"path":"t1.yaml","ops":[{"op":"set","path":["tenants","t1","mysql_connections"],"value":{"str":"` + value + `"}}]}]`
	if expect != "" {
		b += `,"expect_config_hash":"` + expect + `"`
	}
	return b + `}`
}

// #3965: `write` writes only to the tree the caller judged. Given the
// identity the caller judged (expect_config_hash), a tree whose walk is
// another is refused before anything is judged or written; on a write the
// document names the tree the call left — what a parse-tree of it gives.
func TestConfigHash_WriteTakesTheJudgedIdentity(t *testing.T) {
	t.Parallel()
	files := map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"t1.yaml": "tenants:\n  t1:\n    mysql_connections: \"80\"\n"}

	dir := configHashTree(t, files)
	_, judged := runForConfigHash(t, parseTreeCmd, dir)
	if err := os.WriteFile(filepath.Join(dir, "t2.yaml"), []byte("tenants:\n  t2: {}\n"), 0o600); err != nil {
		t.Fatal(err) // the tree changes after the caller judged it
	}
	before := snapshot(t, dir)
	code, res := runBatch(t, writeCmd, writeBatchWithExpect(judged, "90"), "--config-dir", dir)
	if code != exitCallerErr || callReason(res) != writeReasonTreeChanged {
		t.Fatalf("a tree changed since the caller judged it: exit %d reason %q, want %d %q",
			code, callReason(res), exitCallerErr, writeReasonTreeChanged)
	}
	sameTree(t, before, snapshot(t, dir))

	dir = configHashTree(t, files)
	_, judged = runForConfigHash(t, parseTreeCmd, dir)
	code, res = runBatch(t, writeCmd, writeBatchWithExpect(judged, "90"), "--config-dir", dir)
	if code != exitOK || res.Status != writeStatusWritten || res.ConfigHash == nil {
		t.Fatalf("the judged tree: exit %d status %q config_hash %v", code, res.Status, res.ConfigHash)
	}
	if _, now := runForConfigHash(t, parseTreeCmd, dir); *res.ConfigHash != now || now == judged {
		t.Errorf("write's config_hash %s; the tree it left walks as %s (judged %s)", *res.ConfigHash, now, judged)
	}

	// Nothing written: the tree it walked.
	code, res = runBatch(t, writeCmd, writeBatchWithExpect("", "90"), "--config-dir", dir, "--dry-run")
	if _, now := runForConfigHash(t, parseTreeCmd, dir); code != exitOK || res.ConfigHash == nil || *res.ConfigHash != now {
		t.Errorf("dry run: exit %d config_hash %v, the tree walks as %s", code, res.ConfigHash, now)
	}
}

// #3965: the walks of one run read one tree. A tree changed between two of
// them (betweenWalks, at the nth call) ends the run with exit 2 and no
// document — never an answer mixed from two trees. Not parallel: the hook
// is package state, and parallel tests start only after these end.
func TestConfigHash_ATreeChangedBetweenWalksIsNotJudged(t *testing.T) {
	addFile := func(t *testing.T, dir string) {
		if err := os.WriteFile(filepath.Join(dir, "b.yaml"),
			[]byte("tenants:\n  tb:\n    mysql_connections: \"81\"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	// The root gone: the later walk fails, and names no identity.
	removeRoot := func(t *testing.T, dir string) {
		if err := os.RemoveAll(dir); err != nil {
			t.Fatal(err)
		}
	}
	// Every config file a dangling link: the load's early return (no
	// readable file), which answers before the effective walk.
	dangleAll := func(t *testing.T, dir string) {
		for _, name := range []string{"_defaults.yaml", "a.yaml"} {
			p := filepath.Join(dir, name)
			if err := os.Remove(p); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(filepath.Join(dir, "nowhere"), p); err != nil {
				t.Skipf("os.Symlink unavailable here (%v)", err)
			}
		}
	}
	repeatsInherited := map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 70\n",
		"a.yaml": "tenants:\n  ta:\n    pg_connections: \"70\"\n"}
	for _, tc := range []struct {
		name   string
		cmd    string
		nth    int
		change func(*testing.T, string)
		extra  []string
		files  map[string]string // nil: the tree below
	}{
		{"default check: between its first and last walk", "", 1, addFile, nil, nil},
		{"default check: the root gone before its last walk", "", 1, removeRoot, []string{"--cardinality-limit", "0"}, nil},
		{"key-verdict: between the facts and the served reading", keyVerdictCmd, 1, addFile, nil, nil},
		{"key-verdict: every file unreadable after the facts", keyVerdictCmd, 1, dangleAll, nil, nil},
		{"key-verdict: between the served reading and the effective one", keyVerdictCmd, 2, addFile, nil, nil},
		// #4330: a line deleting which serves the same has redundant_override
		// ask the key verdict, which walks the tree after the run's last
		// bracketing walk.
		{"default check: before redundant_override's key verdict", "", 2, addFile, nil, repeatsInherited},
		{"default check: inside redundant_override's key verdict", "", 3, addFile, nil, repeatsInherited},
	} {
		t.Run(tc.name, func(t *testing.T) {
			files := tc.files
			if files == nil {
				files = map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
					"a.yaml": "tenants:\n  ta:\n    mysql_connections: \"80\"\n"}
			}
			dir := configHashTree(t, files)
			calls := 0
			betweenWalks = func(string) {
				calls++
				if calls == tc.nth {
					tc.change(t, dir)
				}
			}
			t.Cleanup(func() { betweenWalks = nil })
			args := []string{}
			if tc.cmd != "" {
				args = append(args, tc.cmd)
			}
			args = append(append(append(args, "--config-dir", dir), walksTheTree[tc.cmd]...), tc.extra...)
			var out, errOut bytes.Buffer
			rc := dispatch(args, strings.NewReader(""), &out, &errOut, guard.CheckDefaultsImpact)
			if calls < tc.nth {
				t.Fatalf("the run walked the tree %d time(s) before its end; the case needs %d", calls, tc.nth)
			}
			if rc != exitCallerErr || out.Len() != 0 || !strings.Contains(errOut.String(), "changed during this run") {
				t.Errorf("rc %d, stdout %q, stderr %q: want exit 2, no document, the change named", rc, out.String(), errOut.String())
			}
		})
	}
}
