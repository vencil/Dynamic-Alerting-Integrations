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
	writeCmd:           "writes the tree: its pre- and post-write identities are #3965's second part",
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
