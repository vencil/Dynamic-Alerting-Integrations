//go:build unix

package main

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// buildCatalogRow applies the catalogue's base (when the row asks) and the
// row's ops under dir — the recipe language of
// tests/shared/confd_fixture_catalog.json, which the Python matrix applies
// the same way — and returns the tree.
func buildCatalogRow(t *testing.T, c fixtureCatalog, row catalogRow, dir string) string {
	t.Helper()
	ops := row.Ops
	if row.Base {
		ops = append(append([]catalogOp(nil), c.Base...), row.Ops...)
	}
	var chmodded []string
	t.Cleanup(func() {
		for _, p := range chmodded {
			_ = os.Chmod(p, 0o755) // so t.TempDir can remove the tree
		}
	})
	for i, op := range ops {
		p := filepath.Join(dir, filepath.FromSlash(op.Path))
		if op.PathHex != "" {
			raw, err := hex.DecodeString(op.PathHex)
			if err != nil {
				t.Fatalf("%s op %d: path_hex: %v", row.ID, i, err)
			}
			p = filepath.Join(dir, string(raw))
		}
		var err error
		switch op.Op {
		case "mkdir":
			err = os.MkdirAll(p, 0o755)
		case "write":
			data := []byte(op.Text)
			if op.B64 != "" {
				data, err = base64.StdEncoding.DecodeString(op.B64)
			}
			if err == nil {
				err = os.WriteFile(p, data, 0o644) // #nosec G306 -- a fixture other uids read
			}
		case "symlink":
			err = os.Symlink(op.Target, p)
		case "symlink_chain":
			via := filepath.Join(dir, filepath.FromSlash(op.Via))
			prev := op.Target
			for j := 0; j < op.Length && err == nil; j++ {
				err = os.Symlink(prev, filepath.Join(via, fmt.Sprintf("l%d", j)))
				prev = fmt.Sprintf("l%d", j)
			}
			if err == nil {
				var rel string
				if rel, err = filepath.Rel(filepath.Dir(p), via); err == nil {
					err = os.Symlink(filepath.Join(rel, prev), p)
				}
			}
		case "mkfifo":
			err = syscall.Mkfifo(p, 0o644)
		case "chmod":
			var m uint64
			if m, err = strconv.ParseUint(op.Mode, 8, 32); err == nil {
				chmodded = append(chmodded, p)
				err = os.Chmod(p, os.FileMode(m))
			}
		default:
			err = fmt.Errorf("unknown op %q", op.Op)
		}
		if err != nil {
			t.Fatalf("%s op %d (%s %s): %v", row.ID, i, op.Op, op.Path, err)
		}
	}
	return filepath.Join(dir, c.Root)
}

// assertValidationClass holds the row's `matrix` class to da-guard's
// validation exit on the same tree — the behaviour matrix's oracle: control
// → 0 (every tool must exit 0 on it), flagged → 3 or 4 (every tool must
// fail and name what the report names), go_only → 3 (content kinds the
// matrix does not run: every one is a file the exporter's load drops).
// Refused rows are judged by parse-tree's exit 2.
func assertValidationClass(t *testing.T, row catalogRow, conf string) {
	t.Helper()
	var want []int
	switch row.Matrix {
	case "control":
		want = []int{exitOK}
	case "flagged":
		want = []int{exitParseFailed, exitUndetermined}
	case "go_only":
		want = []int{exitParseFailed}
	default:
		return
	}
	var out, errOut strings.Builder
	code := run([]string{"--config-dir", conf, "--format", "json", "--cardinality-limit", "0"}, &out, &errOut)
	for _, w := range want {
		if code == w {
			return
		}
	}
	t.Errorf("matrix %q: validation exit %d, want one of %v (the matrix's oracle): %s",
		row.Matrix, code, want, errOut.String())
}

// assertVerdictIsValidationsLists is the invariant of parse-tree's
// "verdict" (verdict.go, #3206): on the tree at conf, the (path, verdict)
// pairs of tree mode are exactly the paths validation's JSON report lists —
// parse_failed and unreadable as unservable, undetermined items of reason
// unreadable_outside_run as tree_refused — and validation's exit is 3 iff
// some path is unservable, else 4 iff some is tree_refused. The other
// undetermined reasons (routing_key, domain_policy, routing_profiles_file,
// invalid_tenant_id, duplicate_tenant) are evidence about content, not a
// path the exporter or the generator cannot read: left out of the equality,
// they may make the exit 4 on their own. List mode's verdicts are tree
// mode's but for what only reading finds: a file the load drops whose
// problem is no walk record is null there.
func assertVerdictIsValidationsLists(t *testing.T, conf string, doc map[string]any) {
	t.Helper()
	var out, errOut strings.Builder
	code := run([]string{"--config-dir", conf, "--format", "json", "--cardinality-limit", "0"}, &out, &errOut)
	var rep struct {
		ParseFailed  []string           `json:"parse_failed"`
		Unreadable   []skippedFile      `json:"unreadable"`
		Undetermined []undeterminedItem `json:"undetermined"`
	}
	if err := json.Unmarshal([]byte(out.String()), &rep); err != nil {
		t.Fatalf("validation (exit %d) wrote no JSON report: %v: %s", code, err, errOut.String())
	}
	want := map[string]string{}
	add := func(p, v string) {
		if have, ok := want[p]; ok && have != v {
			t.Errorf("validation lists %s as %s and as %s", p, have, v)
		}
		want[p] = v
	}
	for _, p := range rep.ParseFailed {
		add(p, verdictUnservable)
	}
	for _, u := range rep.Unreadable {
		add(u.File, verdictUnservable)
	}
	otherEvidence := false
	for _, it := range rep.Undetermined {
		if it.Reason == undeterminedUnreadableOutsideRun {
			add(it.File, verdictTreeRefused)
		} else {
			otherEvidence = true
		}
	}
	got := map[string]string{}
	anyUnservable, anyRefused := false, false
	tree := entriesByPath(t, doc)
	for p, e := range tree {
		v, _ := e["verdict"].(string)
		if e["verdict"] != nil && v != verdictUnservable && v != verdictTreeRefused {
			t.Errorf("%s: verdict %v, not one of the closed set", p, e["verdict"])
		}
		if v != "" {
			got[p] = v
		}
		anyUnservable = anyUnservable || v == verdictUnservable
		anyRefused = anyRefused || v == verdictTreeRefused
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("parse-tree's verdicts are not validation's lists:\n parse-tree %v\n validation %v", got, want)
	}
	wantExit := exitOK
	switch {
	case anyUnservable:
		wantExit = exitParseFailed
	case anyRefused || otherEvidence:
		wantExit = exitUndetermined
	}
	if code != wantExit {
		t.Errorf("validation exit %d, want %d (unservable %v, tree_refused %v, other evidence %v): %s",
			code, wantExit, anyUnservable, anyRefused, otherEvidence, errOut.String())
	}

	lcode, list, msg := runPT(t, "", "--list-only", "--config-dir", conf)
	if lcode != exitOK {
		t.Fatalf("list mode: exit %d: %s", lcode, msg)
	}
	for p, e := range entriesByPath(t, list) {
		te, ok := tree[p]
		if !ok {
			continue // TestParseTree_ListEntriesAreTreeEntries holds the paths equal
		}
		w := te["verdict"]
		if w == verdictUnservable {
			walkRecord := false
			if pr, ok := te["problem"].(map[string]any); ok {
				k := parsetree.Kind(pr["kind"].(string))
				walkRecord = k == parsetree.KindReadFailure || k == parsetree.KindNotRegularFile
			}
			if !walkRecord {
				w = nil
			}
		}
		if e["verdict"] != w {
			t.Errorf("%s: list mode verdict %v, tree mode %v (want %v)", p, e["verdict"], te["verdict"], w)
		}
	}
}

// assertEverySubcommandRefuses: on a tree parse-tree refuses (a name that is
// not UTF-8), every other subcommand that walks conf.d exits 2 too
// (utf8_names.go, #3206) — never the 0 that read as "the tree is fine".
func assertEverySubcommandRefuses(t *testing.T, conf string) {
	t.Helper()
	for _, args := range [][]string{
		{"--config-dir", conf, "--format", "json", "--cardinality-limit", "0"},
		{servedValuesCmd, "--config-dir", conf},
		{effectiveCmd, "--config-dir", conf},
		{keyRefsCmd, "--config-dir", conf, "--metric", "mysql_connections"},
		{compileCustomAlertsCmd, "--config-dir", conf},
	} {
		var out, errOut strings.Builder
		if code := run(args, &out, &errOut); code != exitCallerErr || !strings.Contains(errOut.String(), "not valid UTF-8") {
			t.Errorf("%v: exit %d, want %d naming the path: %s", args[0], code, exitCallerErr, errOut.String())
		}
	}
}

// TestFixtureCatalog_RecipesGiveTheirEntries: each catalogue recipe, built
// and walked as uid 65534 (re-executed under setpriv when run as root, so a
// mode bit really denies), gives exactly the entries the row expects — path,
// role and problem kind — or, for a refused row, exit 2. The Python matrix
// asks da-guard itself on every cell; this is what keeps the catalogue's
// labels true.
func TestFixtureCatalog_RecipesGiveTheirEntries(t *testing.T) {
	if os.Geteuid() == 0 && os.Getenv(testutil.UnprivChildEnv) == "" {
		// The run as uid 65534 cannot read a checkout in a 0700 home: hand
		// it a copy of the catalogue in a directory every uid can read.
		raw, err := os.ReadFile(fixtureCatalogPath())
		if err != nil {
			t.Fatalf("read the fixture catalogue: %v", err)
		}
		dir, err := os.MkdirTemp("/var/tmp", "vibe-catalog-")
		if err != nil {
			testutil.SkipOrFail(t, fmt.Sprintf("cannot make a directory under /var/tmp: %v", err))
			return
		}
		t.Cleanup(func() { _ = os.RemoveAll(dir) })
		copyPath := filepath.Join(dir, "confd_fixture_catalog.json")
		if err := os.WriteFile(copyPath, raw, 0o644); err != nil { // #nosec G306 -- nobody reads it
			t.Fatal(err)
		}
		if err := os.Chmod(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv(fixtureCatalogEnv, copyPath)
		// Nor pkg/config's source: hand it the records that source allows.
		records := possibleWalkRecords(t)
		t.Setenv(walkRecordsEnv, strings.Join(records, ","))
		t.Setenv(walkRecordsCountEnv, strconv.Itoa(len(records)))
	}
	if !testutil.RunUnprivileged(t, "TestFixtureCatalog_RecipesGiveTheirEntries") {
		return
	}
	c := loadFixtureCatalog(t)
	seen := map[string][]string{} // walk record → the rows whose walk wrote it
	for _, row := range c.Rows {
		t.Run(row.ID, func(t *testing.T) {
			conf := buildCatalogRow(t, c, row, t.TempDir())
			for _, rec := range walkRecordsOf(t, conf) {
				seen[rec] = append(seen[rec], row.ID)
			}
			code, doc, msg := runPT(t, "", "--config-dir", conf)
			if row.Expect.Refused {
				if code != exitCallerErr {
					t.Fatalf("exit %d, want %d (a tree parse-tree refuses): %s", code, exitCallerErr, msg)
				}
				assertEverySubcommandRefuses(t, conf)
				return
			}
			if code != exitOK {
				t.Fatalf("exit %d: %s", code, msg)
			}
			assertValidationClass(t, row, conf)
			assertVerdictIsValidationsLists(t, conf, doc)
			want := map[string]string{}
			expected := row.Expect.Entries
			if row.Base {
				expected = append(append([]catalogEntry(nil), c.BaseEntries...), expected...)
			}
			for _, e := range expected {
				k := "-"
				if e.Kind != nil {
					k = *e.Kind
				}
				want[e.Path] = e.Role + "/" + k
			}
			got := map[string]string{}
			for p, e := range entriesByPath(t, doc) {
				k := "-"
				if pr, ok := e["problem"].(map[string]any); ok {
					k, _ = pr["kind"].(string)
				}
				role, _ := e["role"].(string)
				got[p] = role + "/" + k
			}
			var diff []string
			for p, w := range want {
				if got[p] != w {
					diff = append(diff, fmt.Sprintf("%s: got %q, want %q", p, got[p], w))
				}
			}
			for p, g := range got {
				if _, ok := want[p]; !ok {
					diff = append(diff, fmt.Sprintf("%s: got %q, not in the row", p, g))
				}
			}
			sort.Strings(diff)
			for _, d := range diff {
				t.Error(d)
			}
		})
	}
	// The walk's own closed sets, finer than (role, kind): two reasons can
	// share a pair (a FIFO's special_file and a directory link's
	// directory_symlink are both not_a_file/not_regular_file), and a tool can
	// treat them differently (open the FIFO and hang). Every reason the walk
	// can record needs a tree of its own.
	var missing []string
	possible := possibleWalkRecords
	if env := os.Getenv(walkRecordsEnv); env != "" {
		// Handed over by the parent run: held to the count it sent and to a
		// floor, so a smaller set set from outside cannot pass.
		recs := strings.Split(env, ",")
		n, err := strconv.Atoi(os.Getenv(walkRecordsCountEnv))
		if err != nil || n != len(recs) || n < minWalkRecords {
			t.Fatalf("%s holds %d records (count %q, floor %d): not the set the parent computed",
				walkRecordsEnv, len(recs), os.Getenv(walkRecordsCountEnv), minWalkRecords)
		}
		possible = func(*testing.T) []string { return recs }
	}
	for _, rec := range possible(t) {
		if len(seen[rec]) == 0 {
			missing = append(missing, rec)
		}
	}
	if len(missing) > 0 {
		t.Errorf("the walk can record %v, and no catalogue row's walk does: add a row "+
			"(the behaviour matrix runs every tool on every row)", missing)
	}

}

// walkRecordsEnv carries possibleWalkRecords to the run as uid 65534, which
// cannot read pkg/config's source in a 0700 home.
const (
	walkRecordsEnv      = "VIBE_CONFD_WALK_RECORDS"
	walkRecordsCountEnv = "VIBE_CONFD_WALK_RECORDS_N"
	// minWalkRecords: four Unreadable* reasons (stat_error split in two) and
	// two NotFile* reasons today; fewer means the set was not pkg/config's.
	minWalkRecords = 6
)

// walkRecordsOf is every record the exporter's full walk (config.ScanDirTreeFiles)
// writes for the tree at conf, as possibleWalkRecords spells them.
func walkRecordsOf(t *testing.T, conf string) []string {
	t.Helper()
	scan, err := config.ScanDirTreeFiles(conf, log.New(io.Discard, "", 0))
	if err != nil {
		return nil // a tree the walk refuses whole records nothing
	}
	var out []string
	for _, u := range scan.Unreadable {
		out = append(out, unreadableRecord(u.Reason, u.Missing))
	}
	for _, nf := range scan.NotFiles {
		out = append(out, "not_file/"+nf.Reason)
	}
	return out
}

func unreadableRecord(reason string, missing bool) string {
	if reason == config.UnreadableStatError {
		if missing {
			return "unreadable/" + reason + "/missing"
		}
		return "unreadable/" + reason + "/not_missing"
	}
	return "unreadable/" + reason
}

// possibleWalkRecords is every record the walk can write, from pkg/config's
// closed sets read from its source (the Unreadable* and NotFile* constants):
// a stat failure splits on Missing (statErrorFile, the only writer of it),
// which decides its role.
func possibleWalkRecords(t *testing.T) []string {
	t.Helper()
	var out []string
	for _, r := range constsWithPrefix(t, "Unreadable") {
		if r == config.UnreadableStatError {
			out = append(out, unreadableRecord(r, true), unreadableRecord(r, false))
			continue
		}
		out = append(out, unreadableRecord(r, false))
	}
	for _, r := range constsWithPrefix(t, "NotFile") {
		out = append(out, "not_file/"+r)
	}
	sort.Strings(out)
	return out
}
