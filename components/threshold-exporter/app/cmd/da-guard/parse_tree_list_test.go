package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// wireEntry / wireIndex are the list- and names-mode document as a reader
// decodes it (every field named, unknown fields refused: a list document
// with "files" fails the decode).
type wireEntry struct {
	Path             string            `json:"path"`
	Depth            int               `json:"depth"`
	Class            string            `json:"class"`
	TenantNamed      *string           `json:"tenant_named"`
	DefaultsDocument bool              `json:"defaults_document"`
	Readers          []string          `json:"readers"`
	NotRead          *string           `json:"not_read"`
	Role             *string           `json:"role"`
	Problem          *parseTreeProblem `json:"problem"`
	Verdict          *string           `json:"verdict"`
}

type wireIndex struct {
	Schema           string            `json:"schema"`
	DaGuardVersion   string            `json:"da_guard_version"`
	Mode             string            `json:"mode"`
	DefaultsCarriers map[string]string `json:"defaults_carriers"`
	Entries          []wireEntry       `json:"entries"`
}

// runPTList runs `parse-tree --list-only …` and decodes its one document.
func runPTList(t *testing.T, stdin string, args ...string) (int, *wireIndex, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := runParseTree(append([]string{"--list-only"}, args...), strings.NewReader(stdin), &out, &errOut)
	if code != exitOK {
		if out.Len() != 0 {
			t.Fatalf("exit %d with stdout %q", code, out.String())
		}
		return code, nil, errOut.String()
	}
	raw := out.Bytes()
	var doc wireIndex
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		t.Fatalf("stdout is not one list document: %v", err)
	}
	if dec.More() {
		t.Fatal("stdout holds more than one JSON document")
	}
	if doc.Schema != parseTreeSchema {
		t.Fatalf("schema %q", doc.Schema)
	}
	// Every entry writes every field (null where it has none).
	var loose struct {
		Entries []map[string]any `json:"entries"`
	}
	if err := json.Unmarshal(raw, &loose); err != nil {
		t.Fatal(err)
	}
	for _, e := range loose.Entries {
		if len(e) != 10 {
			t.Errorf("%v: %d fields, want 10", e["path"], len(e))
		}
	}
	return code, &doc, errOut.String()
}

func str(s string) *string { return &s }

// TestParseTree_ListEntriesAreTreeEntries: list mode and tree mode give the
// same entries for the same tree, field for field — the same function builds
// both — and differ only in what reading finds: tree mode's content problems
// (syntax_error here) and its "files" / "rendered_by". Each path's class,
// role and name fields are pinned beside.
func TestParseTree_ListEntriesAreTreeEntries(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"t1.yaml":                  "tenants: {t1: {}}\n",
		"Upper.YAML":               "tenants: {T2: {}}\n",
		"_defaults.yaml":           "defaults: {}\n",
		"_defaults.yml":            "defaults: {}\n",
		"team/_domain_policy.yaml": "x: 1\n",
		"team/_defaults.YML":       "defaults: {}\n",
		"team/sub/t3.yml":          "tenants: {t3: {}}\n",
		"broken.yaml":              ": bad: [\n",
		".git/a.yaml":              "x\n",
		"readme.md":                "x\n",
		"notes.yaml/inner.yaml":    "tenants: {t4: {}}\n",
		"_defaults-multidb.yaml":   "defaults: {}\n",
		"_defaults-x.yaml/t.yaml":  "tenants: {t6: {}}\n",
	})
	outside := t.TempDir()
	for link, target := range map[string]string{
		"dangling.yaml": "nowhere.yaml",
		"loop-a.yaml":   "loop-b.yaml",
		"loop-b.yaml":   "loop-a.yaml",
		"elsewhere":     outside,
		"null.yaml":     os.DevNull,
	} {
		if err := os.Symlink(target, filepath.Join(root, link)); err != nil {
			t.Skipf("symlink: %v", err)
		}
	}

	var treeOut, listOut, errOut bytes.Buffer
	if code := runParseTree([]string{"--config-dir", root}, strings.NewReader(""), &treeOut, &errOut); code != exitOK {
		t.Fatalf("tree mode: exit %d: %s", code, errOut.String())
	}
	if code := runParseTree([]string{"--list-only", "--config-dir", root}, strings.NewReader(""), &listOut, &errOut); code != exitOK {
		t.Fatalf("list mode: exit %d: %s", code, errOut.String())
	}
	var tree, list map[string]any
	if err := json.Unmarshal(treeOut.Bytes(), &tree); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(listOut.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	checkSelectedInvariant(t, tree)
	for _, k := range []string{"files", "rendered_by"} {
		if _, ok := tree[k]; !ok {
			t.Errorf("tree mode has no %q", k)
		}
		if _, ok := list[k]; ok {
			t.Errorf("list mode has %q", k)
		}
	}
	if !reflect.DeepEqual(list["defaults_carriers"], tree["defaults_carriers"]) {
		t.Errorf("defaults_carriers list %v, tree %v", list["defaults_carriers"], tree["defaults_carriers"])
	}
	// The tree's entries minus what only reading finds: a content problem,
	// and the verdict of a file the exporter's load drops (parse_failed) —
	// list mode's verdict knows the walk's records alone.
	sawContentProblem := false
	want := []any{}
	for _, raw := range tree["entries"].([]any) {
		e := map[string]any{}
		for k, v := range raw.(map[string]any) {
			e[k] = v
		}
		walkRecord := false
		if pr, ok := e["problem"].(map[string]any); ok {
			k := parsetree.Kind(pr["kind"].(string))
			walkRecord = k == parsetree.KindReadFailure || k == parsetree.KindNotRegularFile
		}
		if e["verdict"] == verdictUnservable && !walkRecord {
			e["verdict"] = nil
		}
		if pr, ok := e["problem"].(map[string]any); ok {
			switch parsetree.Kind(pr["kind"].(string)) {
			case parsetree.KindReadFailure, parsetree.KindNotRegularFile:
			default:
				sawContentProblem = true
				e["problem"] = nil
			}
		}
		want = append(want, e)
	}
	if !sawContentProblem {
		t.Fatal("fixture: tree mode found no content problem (broken.yaml)")
	}
	if !reflect.DeepEqual(list["entries"], want) {
		t.Errorf("entries\n list %v\n tree %v", list["entries"], want)
	}

	_, doc, _ := runPTList(t, "", "--config-dir", root)
	byPath := map[string]wireEntry{}
	for i, e := range doc.Entries {
		if i > 0 && doc.Entries[i-1].Path >= e.Path {
			t.Errorf("entries not sorted: %q before %q", doc.Entries[i-1].Path, e.Path)
		}
		byPath[e.Path] = e
	}
	const cfg, naf = string(config.RoleConfig), string(config.RoleNotAFile)
	for _, c := range []struct {
		path, class, tenant string
		depth               int
		document            bool
		role, kind          string
	}{
		{"t1.yaml", classTenant, "t1", 0, false, cfg, ""},
		{"Upper.YAML", classTenant, "Upper", 0, false, cfg, ""}, // the name's case, not the `tenants:` key
		{"_defaults.yml", classDefaults, "", 0, true, cfg, ""},  // unselected: a config file nobody reads
		{"team/_defaults.YML", classDefaults, "", 1, true, cfg, ""},
		{"_defaults-multidb.yaml", classPlatform, "", 0, true, cfg, ""}, // a document no chain reads
		{"team/_domain_policy.yaml", classPlatform, "", 1, false, cfg, ""},
		{"team/sub/t3.yml", classTenant, "t3", 2, false, cfg, ""},
		{"broken.yaml", classTenant, "broken", 0, false, cfg, ""}, // list mode reads nothing
		// A config-named directory is descended (role dir); its problem says
		// its name looks like a file's.
		{"notes.yaml", classDirectory, "", 0, false, "dir", "not_regular_file"},
		// A directory is no document, whatever its name.
		{"_defaults-x.yaml", classDirectory, "", 0, false, "dir", "not_regular_file"},
		{"notes.yaml/inner.yaml", classTenant, "inner", 1, false, cfg, ""},
		{"elsewhere", classDirectory, "", 0, false, naf, "not_regular_file"},
		// A stat that found nothing (ENOENT, ELOOP): provably no file.
		{"dangling.yaml", classTenant, "dangling", 0, false, naf, "read_failure"},
		{"loop-a.yaml", classTenant, "loop-a", 0, false, naf, "read_failure"},
		{"null.yaml", classTenant, "null", 0, false, naf, "not_regular_file"},
	} {
		e, ok := byPath[c.path]
		if !ok {
			t.Errorf("%s: not listed", c.path)
			continue
		}
		tenant := ""
		if e.TenantNamed != nil {
			tenant = *e.TenantNamed
		}
		kind := ""
		if e.Problem != nil {
			kind = string(e.Problem.Kind)
		}
		if e.Class != c.class || tenant != c.tenant || e.Depth != c.depth || e.DefaultsDocument != c.document ||
			e.Role == nil || *e.Role != c.role || kind != c.kind {
			t.Errorf("%s: %+v, want class %q tenant_named %q depth %d defaults_document %v role %q problem %q",
				c.path, e, c.class, c.tenant, c.depth, c.document, c.role, c.kind)
		}
	}
	if e := byPath["_defaults.yml"]; e.NotRead == nil || *e.NotRead != notReadUnselectedCarrier || len(e.Readers) != 0 {
		t.Errorf("_defaults.yml: not_read %v readers %v", e.NotRead, e.Readers)
	}
	for _, hidden := range []string{".git/a.yaml", "readme.md"} {
		if _, ok := byPath[hidden]; ok {
			t.Errorf("%s: listed, but the walk never reads it", hidden)
		}
	}
}

// TestParseTree_RoleOfEachWalkRecord: every kind of walk record reaches the
// document with config.Role's verdict, in list and in tree mode alike — the
// cases a uid-0 test cannot build with real modes (a stat refused by an
// unsearchable directory, an open refused, EIO), and a reason nobody named
// here, all config files whose read fails; only what proves no file is
// not_a_file; a walk error and the root unlistable_dir (#3206). The walker's
// Missing is pinned in pkg/config (TestScanRecordsWhetherAStatFoundNothing).
func TestParseTree_RoleOfEachWalkRecord(t *testing.T) {
	t.Parallel()
	scan := &config.TreeScan{AbsRoot: t.TempDir(),
		Unreadable: []config.UnreadableFile{
			{RelKey: "locked.yaml", Reason: config.UnreadableReadError},
			{RelKey: "denied.yaml", Reason: config.UnreadableStatError}, // EACCES / EIO / unknown errno
			{RelKey: "_defaults.yaml", Reason: config.UnreadableStatError},
			{RelKey: "gone.yaml", Reason: config.UnreadableStatError, Missing: true}, // ENOENT / ELOOP
			{RelKey: "queue.yaml", Reason: config.UnreadableSpecialFile},
			{RelKey: "sub", Reason: config.UnreadableWalkError},
			{RelKey: "future.yaml", Reason: "a_reason_added_later"},
		},
		NotFiles: []config.NotFile{
			{RelKey: "d.yaml", Reason: config.NotFileDirectory},
			{RelKey: "l.yaml", Reason: config.NotFileDirLink},
		},
		RootWalkErr: errors.New("permission denied"),
	}
	want := map[string]string{
		"locked.yaml": "config", "denied.yaml": "config", "_defaults.yaml": "config", "future.yaml": "config",
		"gone.yaml": "not_a_file", "queue.yaml": "not_a_file", "d.yaml": "dir", "l.yaml": "not_a_file",
		"sub": "unlistable_dir", ".": "unlistable_dir",
	}
	list, err := parseTreeListOf(scan)
	if err != nil {
		t.Fatal(err)
	}
	tree, err := parseTreeOf(scan)
	if err != nil {
		t.Fatal(err)
	}
	// No file here has content to refuse, so validation's exit-3 lists are
	// the walk's alone, and the two modes give one verdict per path.
	if err := setVerdicts(tree.Entries, reportedPaths(nil, unreadableEntries(config.TreeUnreadable(scan)))); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(list.Entries, tree.Entries) {
		t.Errorf("list and tree entries differ:\n list %+v\n tree %+v", list.Entries, tree.Entries)
	}
	got := map[string]string{}
	verdicts := map[string]string{}
	for _, e := range list.Entries {
		got[e.Path] = string(e.Role)
		verdicts[e.Path] = e.Verdict
		if e.Problem == nil {
			t.Errorf("%s: no problem", e.Path)
		}
		if e.Role == config.RoleUnlistableDir && e.Class != classDirectory {
			t.Errorf("%s: unlistable_dir of class %q", e.Path, e.Class)
		}
		if e.Role == config.RoleUnlistableDir && (len(e.Readers) != 0 || e.NotRead != "") {
			// A directory, listed or not, is no file a reader reads.
			t.Errorf("%s: unlistable_dir with readers %v, not_read %q", e.Path, e.Readers, e.NotRead)
		}
		if e.Path == "_defaults.yaml" && !e.DefaultsDocument {
			t.Errorf("%s: a config file named as a defaults document is one, readable or not", e.Path)
		}
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("roles\n got  %v\n want %v", got, want)
	}
	// Every walk record is in validation's unreadable (exit 3) but the two
	// paths that are no file the walk reads (exit 4); "." too, as no file
	// was kept (config.RootUnreadable).
	wantVerdicts := map[string]string{
		"locked.yaml": verdictUnservable, "denied.yaml": verdictUnservable, "_defaults.yaml": verdictUnservable,
		"future.yaml": verdictUnservable, "gone.yaml": verdictUnservable, "queue.yaml": verdictUnservable,
		"sub": verdictUnservable, ".": verdictUnservable,
		"d.yaml": verdictTreeRefused, "l.yaml": verdictTreeRefused,
	}
	if !reflect.DeepEqual(verdicts, wantVerdicts) {
		t.Errorf("verdicts\n got  %v\n want %v", verdicts, wantVerdicts)
	}
}

// TestParseTreeList_Names classifies given names by pkg/confdname alone: no
// disk, so no role and no readers.
func TestParseTreeList_Names(t *testing.T) {
	t.Parallel()
	_, doc, _ := runPTList(t, `["db-a.yaml","MiXeD.YmL",".h.yaml","_defaults.YML","_x.yml","_defaults-multidb.yaml","n.txt","x.yaml.bak"]`, "--names", "-")
	if doc.Mode != "names" || len(doc.DefaultsCarriers) != 0 {
		t.Fatalf("mode %q carriers %v", doc.Mode, doc.DefaultsCarriers)
	}
	want := []wireEntry{
		{Path: "db-a.yaml", Class: classTenant, TenantNamed: str("db-a")},
		{Path: "MiXeD.YmL", Class: classTenant, TenantNamed: str("MiXeD")},
		{Path: ".h.yaml", Class: classHidden},
		{Path: "_defaults.YML", Class: classDefaults, DefaultsDocument: true},
		{Path: "_x.yml", Class: classPlatform},
		// A platform-defaults DOCUMENT no chain reads: class platform, held
		// to the platform-defaults schema (confdname.IsDefaultsDocument).
		{Path: "_defaults-multidb.yaml", Class: classPlatform, DefaultsDocument: true},
		{Path: "n.txt", Class: classNotConfigName},
		{Path: "x.yaml.bak", Class: classNotConfigName},
	}
	if !reflect.DeepEqual(doc.Entries, want) {
		t.Errorf("entries\n got  %+v\n want %+v", doc.Entries, want)
	}
}

func TestParseTreeList_CallerErrors(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"t1.yaml": "tenants: {t1: {}}\n"})
	for _, tc := range []struct {
		stdin string
		args  []string
	}{
		{"", nil}, // neither --config-dir nor --names
		{"", []string{"--config-dir", root, "--path", "t1.yaml", "--input", "-"}},
		{`["a.yaml"]`, []string{"--config-dir", root, "--names", "-"}},
		{`"a.yaml"`, []string{"--names", "-"}},     // not an array
		{`["a/b.yaml"]`, []string{"--names", "-"}}, // not a base name
		{`[""]`, []string{"--names", "-"}},
		// A byte that is not UTF-8: refused before decoding, never read as
		// U+FFFD (json.Unmarshal would) and so never passed as valid.
		{"[\"bad\xff.yaml\"]", []string{"--names", "-"}},
		// One entry per name: a name given twice is refused, never written
		// as two entries a reader refuses whole (#3206).
		{`["a.yaml","b.yaml","a.yaml"]`, []string{"--names", "-"}},
		{"", []string{"--config-dir", filepath.Join(root, "missing")}},
	} {
		if code, _, _ := runPTList(t, tc.stdin, tc.args...); code != exitCallerErr {
			t.Errorf("--list-only %v: exit %d, want %d", tc.args, code, exitCallerErr)
		}
	}
	// The name given twice is refused by names mode itself, as the caller's
	// input — not only by the emission guard (a da-guard defect's message).
	if _, _, msg := runPTList(t, `["a.yaml","a.yaml"]`, "--names", "-"); !strings.Contains(msg, `"a.yaml" is given twice`) {
		t.Errorf("a name given twice: %q, want it named as given twice", msg)
	}
	var out, errOut bytes.Buffer
	if code := runParseTree([]string{"--names", "-"}, strings.NewReader(`["a.yaml"]`), &out, &errOut); code != exitCallerErr {
		t.Errorf("--names without --list-only: exit %d, want %d", code, exitCallerErr)
	}
	// A name JSON cannot carry refuses the whole listing, as in tree mode.
	bad := t.TempDir()
	if err := os.WriteFile(filepath.Join(bad, "t\xff.yaml"), []byte("a: 1\n"), 0o600); err == nil {
		if code, _, msg := runPTList(t, "", "--config-dir", bad); code != exitCallerErr || !strings.Contains(msg, "UTF-8") {
			t.Errorf("non-UTF-8 name: exit %d, %q", code, msg)
		}
	}
}

// TestWalkEntries_RefusesAPathRecordedTwice: one entry per path is the
// document's contract. A walk that records a path twice (as one once did for
// a config-named directory it could not list) fails here, naming the path,
// in every mode — instead of writing a document every reader refuses whole.
func TestWalkEntries_RefusesAPathRecordedTwice(t *testing.T) {
	scan := &config.TreeScan{
		AbsRoot:  t.TempDir(),
		Keys:     []string{"a.yaml"},
		NotFiles: []config.NotFile{{RelKey: "a.yaml", Reason: config.NotFileDirectory}},
	}
	if _, err := parseTreeListOf(scan); err == nil || !strings.Contains(err.Error(), `"a.yaml" twice`) {
		t.Fatalf("list mode: err = %v, want the path named as recorded twice", err)
	}
	if _, err := parseTreeOf(scan); err == nil || !strings.Contains(err.Error(), `"a.yaml" twice`) {
		t.Fatalf("tree mode: err = %v, want the path named as recorded twice", err)
	}
	scan.NotFiles = nil
	if _, err := parseTreeListOf(scan); err != nil {
		t.Fatalf("control, one record per path: %v", err)
	}
}

// TestWriteParseTree_RefusesAPathHeldTwice: the emission point checks the
// one-entry-per-path contract for every mode, whatever built the entries
// (names mode lists them in the order given, unsorted) — nothing is written.
func TestWriteParseTree_RefusesAPathHeldTwice(t *testing.T) {
	t.Parallel()
	entries := []parseTreeEntry{nameEntry("a.yaml"), nameEntry("b.yaml"), nameEntry("a.yaml")}
	var out bytes.Buffer
	err := writeParseTree(&out, entries, entries)
	if err == nil || !strings.Contains(err.Error(), `"a.yaml" twice`) {
		t.Fatalf("err = %v, want the path named as held twice", err)
	}
	if out.Len() != 0 {
		t.Errorf("wrote %q before refusing", out.String())
	}
	if err := writeParseTree(&out, entries[:2], entries[:2]); err != nil {
		t.Fatalf("control, one entry per path: %v", err)
	}
}

// TestWalkedDirEntry_IsTreeModesEntry: file mode looks the walk record up
// directly (not a whole walkEntries index per call); for every record that
// is no config file it is exactly tree mode's entry, and nil for the rest.
func TestWalkedDirEntry_IsTreeModesEntry(t *testing.T) {
	t.Parallel()
	scan := &config.TreeScan{AbsRoot: t.TempDir(),
		Keys:  []string{"t.yaml"},
		Files: map[string]*config.TreeFile{"t.yaml": {}},
		Unreadable: []config.UnreadableFile{
			{RelKey: "denied.yaml", Reason: config.UnreadableStatError},
			{RelKey: "gone.yaml", Reason: config.UnreadableStatError, Missing: true},
			{RelKey: "queue.yaml", Reason: config.UnreadableSpecialFile},
			{RelKey: "sub", Reason: config.UnreadableWalkError},
		},
		NotFiles: []config.NotFile{
			{RelKey: "d.yaml", Reason: config.NotFileDirectory},
			{RelKey: "l.yaml", Reason: config.NotFileDirLink},
		},
		Dirs: []string{"team"},
	}
	idx := newParseTreeIndex("tree")
	if err := walkEntries(scan, &idx); err != nil {
		t.Fatal(err)
	}
	for _, want := range idx.Entries {
		got := walkedDirEntry(scan, want.Path)
		if want.Role == config.RoleConfig {
			if got != nil {
				t.Errorf("%s: a config file, got entry %+v", want.Path, *got)
			}
			continue
		}
		if want.Path == "." {
			continue // never a --path (cleanRelPath)
		}
		if got == nil || !reflect.DeepEqual(*got, want) {
			t.Errorf("%s:\n got  %+v\n want %+v", want.Path, got, want)
		}
	}
	if got := walkedDirEntry(scan, "absent.yaml"); got != nil {
		t.Errorf("absent.yaml: got %+v, want nil", *got)
	}
}

// TestParseTree_EveryEnteredDirectoryHasAnEntry: the index is complete
// (#3206) — every directory the walk enters is an entry of role "dir", in
// list and tree mode alike: a plain one with no problem, a config-named one
// (`x.yaml/`, descended, its files read) with a not_regular_file problem. A
// symlink to a directory is never entered: not_a_file. A hidden directory is
// not walked, so not listed. Without this a reader had to list directories
// itself and judge them, and judged a skipped link "already nested".
func TestParseTree_EveryEnteredDirectoryHasAnEntry(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	for _, d := range []string{"team/sub", "x.yaml", ".git", "outside"} {
		if err := os.MkdirAll(filepath.Join(root, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for p, body := range map[string]string{"team/sub/t.yaml": "tenants: {t: {}}\n",
		"x.yaml/i.yaml": "tenants: {i: {}}\n", ".git/h.yaml": "x: 1\n"} {
		if err := os.WriteFile(filepath.Join(root, p), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "link")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	want := map[string][2]string{ // path → role, problem kind
		"team": {"dir", ""}, "team/sub": {"dir", ""}, "outside": {"dir", ""},
		"x.yaml": {"dir", "not_regular_file"}, "link": {"not_a_file", "not_regular_file"},
		"team/sub/t.yaml": {"config", ""}, "x.yaml/i.yaml": {"config", ""},
	}
	for _, mode := range [][]string{nil, {"--list-only"}} {
		code, doc, msg := runPT(t, "", append(mode, "--config-dir", root)...)
		if code != exitOK {
			t.Fatalf("%v: exit %d: %s", mode, code, msg)
		}
		got := map[string][2]string{}
		for p, e := range entriesByPath(t, doc) {
			kind := ""
			if pr, ok := e["problem"].(map[string]any); ok {
				kind = pr["kind"].(string)
			}
			got[p] = [2]string{e["role"].(string), kind}
			if e["role"] == "dir" && (e["class"] != classDirectory || len(e["readers"].([]any)) != 0 || e["not_read"] != nil) {
				t.Errorf("%v %s: a dir entry %v", mode, p, e)
			}
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%v: entries\n got  %v\n want %v", mode, got, want)
		}
	}
}
